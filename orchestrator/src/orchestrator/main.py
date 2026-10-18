"""编排器主循环（architecture.md §3.1 状态机 / §4.3 两阶段预算 / §3.2 崩溃恢复）。

CLI 入口：`python -m orchestrator`。
对 mock 可全链路真实测试：list → start → poll available → solver → harvest →
submit → close，含 LOCKED 重入队、收尾保护、崩溃恢复对账。

**hint 接口已整体移除**（architecture.md §4.4）：扣分不可逆、本届读不到
`hint_cost_radio` 无法做成本判断，实测 10 次取 hint 仅 1 次换来解出 —— 期望收益
为负。删掉的是整条链路：PlatformClient.hint / Hint Broker / HINT_RETRY 状态 /
`hints_taken` 记账 / Journal 的 hint 事件与 fsync / task.json 的 `hint` 字段。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_module
from . import sop as sop_mod
from . import streams
from .api import (
    ChallengeGone,
    LevelLocked,
    PlatformClient,
    RetryableError,
    StartFull,
    TaskEnded,
)
from .config import RuntimeConfig, resolve_config
from .harvester import harvest, submit_candidates
from .journal import Journal, score_summary
from .memory import AttackMemory
from .registry import ChallengeRecord, Registry
from .repair import BASE_WEIGHTS, choose_repair, update_weights
from .scheduler import (
    can_start,
    eligible,
    plan_phase,
    should_start,
    slice_for,
)
from .supervisor import collect_prior, prepare_workspace

POLL_INTERVAL_S = 2.0
# 首片试探：先给 60s，有进展才续片；无进展立即释放并发槽。
PROBE_SLICE_S = float(os.environ.get("PROBE_SLICE_S", "60"))
AVAILABLE_TIMEOUT_S = 120.0
# TCP 就绪预检上限（c-08 实测 7860 冷启 >45s；编排器等待免费，solver 等待烧片）
TARGET_READY_TIMEOUT_S = 120.0
REASONER_MODEL = os.environ.get("SOLVER_REASONER_MODEL", "deepseek-reasoner")
# reasoner 每轮 ~12s（chat ~4-5s）：短片里只会拿到 20-25 轮，实测反而更慢。
# 只在长片（Phase 2 深挖）启用；短片/首片保持 chat。
REASONER_MIN_SLICE_S = float(os.environ.get("REASONER_MIN_SLICE_S", "600"))

REJECTION_NOTE = (
    "A flag candidate from the previous attempt was REJECTED by the platform "
    "(likely a decoy). Do not resubmit it or trust obvious flag locations; "
    "re-verify the exploit chain and the true flag source."
)
LIST_FAIL_LIMIT = 3  # list 连续失败达此数才全局停机（瞬时 409/404 不再一击致命）
# 等 available 的轮询退避（§4.1 修订）。原先固定 0.2s：3 个 pipeline 同时轮询 =
# 15 req/s 需求，而全局令牌桶只有 3 req/s —— 轮询把桶吃满，submit/close 排在
# 轮询后面，实测每题 solver_done→solved 固定多花 ~11s（63 题 ≈ 11 分钟纯开销）。
# 改为自适应：前几拍快（容器常 1~3s 就绪），随后退避，把额度让给写操作。
AVAILABLE_POLL_START_S = 0.3
AVAILABLE_POLL_MAX_S = 2.0
AVAILABLE_POLL_GROWTH = 1.6
# 失败重投上限：难题常需要多轮换策略（实测 60 轮/2 次投递会过早放弃）。
# 可经 MAX_ATTEMPTS 覆盖；调度器的 attempts 惩罚会自然把重投题排到队尾。
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS") or "3")


@dataclass(slots=True)
class RunContext:
    config: RuntimeConfig
    client: PlatformClient
    registry: Registry
    journal: Journal
    workspace_root: Path
    solver_cmd: list[str]
    priors: dict[str, dict] = field(default_factory=dict)  # code → 双图状态（跨轮合并）
    running: set[str] = field(default_factory=set)
    slot_freed: asyncio.Event = field(default_factory=asyncio.Event)
    # 全局 solver 进程并发（含 v2 subagent 节点）；平台硬上限 3。
    solver_slots: streams.FairSlotPool | None = None
    stopped: bool = False
    t0: float = 0.0
    env: dict[str, str] | None = None  # 传给 solver 的基础环境（测试注入用）
    # harness-v2（harness-v2.md §4/§5）：SOP 检索与修复记账；重启丢失，
    # 不参与崩溃恢复 —— 修复权重是启发式，丢了从 BASE 重新演化即可。
    repair_notes: dict[str, str] = field(default_factory=dict)  # code → 换面指令
    repair_weights: dict[str, float] = field(default_factory=lambda: dict(BASE_WEIGHTS))
    last_repair: dict[str, str] = field(default_factory=dict)  # code → 上轮修复动作
    # 攻击图记忆（harness-v2.md §6）：triviumdb 缺失即降级为空操作
    memory: AttackMemory | None = None


# ---------------------------------------------------------------- helpers ----
async def _safe_list(ctx: RunContext) -> list[dict] | None:
    try:
        return await ctx.client.list_challenges()
    except TaskEnded as exc:
        # 实测单次瞬时 409/404 也会走这里（API 事后验证正常）——不立即全局停机，
        # 交给 run_agent 主循环的连续失败计数（LIST_FAIL_LIMIT）裁决。
        ctx.journal.append("list_failed", reason=str(exc))
        return None
    except RetryableError:
        return None


async def _safe_close(ctx: RunContext, rec: ChallengeRecord) -> None:
    if rec.container_status == "stopped" and not rec.active_since_reconcile:
        return
    rec.state = "CLOSING"
    with contextlib.suppress(TaskEnded, RetryableError, ChallengeGone, LevelLocked, StartFull):
        await ctx.client.close(rec.unique_code)  # 尽力而为；下一轮对账以平台真值为准
    rec.container_status = "stopped"
    rec.active_since_reconcile = False


async def _wait_available(ctx: RunContext, rec: ChallengeRecord, timeout_s: float) -> bool:
    """轮询等 container_status == available（platform-api.md §4.1）。

    自适应退避：见 AVAILABLE_POLL_* 常量说明 —— 固定快轮询会吃满令牌桶，
    把 submit/close 挤到队列后面。
    """
    deadline = time.monotonic() + timeout_s
    delay = AVAILABLE_POLL_START_S
    while time.monotonic() < deadline:
        rows = await _safe_list(ctx)
        if ctx.stopped:
            return False
        if rows is not None:
            row = next((r for r in rows if r["unique_code"] == rec.unique_code), None)
            if row is None:
                return False
            rec.container_status = row["container_status"]
            rec.container_addr = list(row.get("container_addr") or [])
            if rec.container_status == "available" and rec.container_addr:
                return True
            if rec.container_status == "stopped":
                return False
        await asyncio.sleep(delay)
        delay = min(AVAILABLE_POLL_MAX_S, delay * AVAILABLE_POLL_GROWTH)
    return False


_HTTP_PORTS = frozenset(
    {80, 443, 3000, 5000, 7001, 7860, 8000, 8008, 8080, 8081, 8888, 9000, 9090, 10000}
)


async def _http_status(host: str, port: int) -> int | None:
    """取 HTTP 状态码；连接失败返回 None。5xx = 应用还没就绪（DB/迁移未完成）。"""
    ssl_ctx = None
    if port == 443:
        import ssl

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx), timeout=3.0
        )
    except (TimeoutError, OSError):
        return None
    try:
        writer.write(f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
        await asyncio.wait_for(writer.drain(), timeout=2.0)
        line = await asyncio.wait_for(reader.readline(), timeout=3.0)
        parts = line.decode(errors="replace").split()
        return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    except (TimeoutError, OSError):
        return 0
    finally:
        writer.close()


async def _wait_target_ready(ctx: RunContext, rec: ChallengeRecord, timeout_s: float) -> bool:
    """就绪预检：available ≠ 服务就绪。

    实测 a-03：容器 TCP 已开，但 Flask `/login` 连续 5xx 数分钟；agent 误判
    “服务坏了”，转去扫同网段别的靶机（那是另一道题的目标），整片烧光。
    所以 HTTP 端口必须看到 <500 才放行；超时仍 best-effort 放行并记 journal。
    """
    addrs = []
    for a in rec.container_addr:
        host, _, port = a.partition(":")
        addrs.append((host, int(port) if port else 80))
    # loopback 目标（mock/e2e）不做预检：它们没有真实服务冷启，空等只会吃掉测试预算
    if timeout_s <= 0 or all(h in ("127.0.0.1", "localhost", "::1") for h, _ in addrs):
        return True
    deadline = time.monotonic() + timeout_s
    last_status: dict[str, int] = {}
    while time.monotonic() < deadline:
        for host, port in addrs:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0
                )
                writer.close()
            except OSError:
                continue
            if port in _HTTP_PORTS:
                status = await _http_status(host, port)
                last_status[f"{host}:{port}"] = status or 0
                if status is not None and 500 <= status < 600:
                    continue  # 应用层未就绪，别放 solver 出去
            return True
        await asyncio.sleep(3.0)
    ctx.journal.append(
        "target_not_ready",
        unique_code=rec.unique_code,
        addrs=[f"{h}:{p}" for h, p in addrs],
        http_status=last_status,
    )
    return False


def solver_env_for(
    env: dict[str, str] | None,
    rec: ChallengeRecord,
    config: RuntimeConfig | None = None,
    *,
    slice_s: float | None = None,
) -> dict[str, str]:
    """solver 子进程环境来源（§4.3 分级模型 / §6.2 网关改写）。

    None = 以进程环境为基底 —— 镜像里没有 solver/.env，LLM_* 全部来自平台注入。
    config 提供**改写后**的 LLM_BASE_URL（托管模式必须是 *.tsecbench.gw）与
    key/model；若不注入，solver 会拿原始公网 URL 直连，平台网关上根本看不到请求
    （run-34162 仿真抓到的第三个 bug）。
    """
    base = env if env is not None else os.environ
    merged = dict(base)
    if config is not None:
        if config.llm_base_url:
            merged["LLM_BASE_URL"] = config.llm_base_url
        if config.llm_api_key:
            merged["LLM_API_KEY"] = config.llm_api_key
        if config.llm_model:
            merged["LLM_MODEL"] = config.llm_model
    deep_slice = slice_s is None or slice_s >= REASONER_MIN_SLICE_S
    if rec.difficulty == "hard" and deep_slice:
        merged["LLM_MODEL"] = REASONER_MODEL
    return merged


def _merge_prior(old: dict | None, new: dict) -> dict:
    """双图跨轮合并：solver 只交原始观察，去重由编排器算（§2.5）。"""
    merged = {
        "assets": [],
        "explored": [],
        "open_leads": [],
        "credentials": [],
        "findings": [],
    }
    for key in merged:
        items = list((old or {}).get(key) or []) + list(new.get(key) or [])
        seen: list[str] = []
        for item in items:
            marker = str(sorted(item.items())) if isinstance(item, dict) else str(item)
            if marker not in seen:
                seen.append(marker)
                merged[key].append(item)
    # 跨 attempt 的 harness 状态：优先最新一轮（solver 每轮覆盖落盘）
    history = str((new or {}).get("history") or (old or {}).get("history") or "")
    merged["history"] = history[-4000:]
    stage_state = (new or {}).get("stage_state") or (old or {}).get("stage_state")
    if isinstance(stage_state, dict) and stage_state:
        merged["stage_state"] = stage_state
    old_obs = (old or {}).get("observer")
    if isinstance(old_obs, dict) and old_obs:
        merged["observer"] = old_obs
    new_obs = (new or {}).get("observer")
    if isinstance(new_obs, dict) and new_obs:
        old_rev = int(old_obs.get("revision") or 0) if isinstance(old_obs, dict) else -1
        if int(new_obs.get("revision") or 0) >= old_rev:
            merged["observer"] = new_obs
    return merged


def _remaining_score(registry: Registry) -> float:
    return sum(r.remaining_score for r in registry.records.values() if not r.is_completed)


# ---------------------------------------------------------------- pipeline ----
_HIGH_VALUE_PRIOR_KEYS = ("credentials", "findings")


def _endpoint_of(value: str) -> tuple[str, int | None]:
    """从 URL / host:port / 裸主机取 (host, port)；无端口时 port=None。"""
    text = str(value).strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else None
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        return host, int(port) if port.isdigit() else None
    return text, None


def _target_endpoints(ws: Path) -> tuple[set[tuple[str, int | None]], set[str]]:
    try:
        task = json.loads((ws / "task.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set()
    endpoints: set[tuple[str, int | None]] = set()
    hosts: set[str] = set()
    for target in task.get("targets") or []:
        host, port = _endpoint_of(str(target))
        if host:
            endpoints.add((host, port))
            hosts.add(host)
    return endpoints, hosts


def _is_new_endpoint(value: str, endpoints: set, hosts: set) -> bool:
    host, port = _endpoint_of(value)
    if not host:
        return False
    if host.startswith(("127.", "169.254.")) or re.match(r"172\.(1[7-9]|2\d|3[01])\.", host):
        return False
    # 目标只给了 host 时，该 host 任何端口都算“目标回显”，不是新信息。
    if (host, None) in endpoints:
        return False
    if port is not None:
        return (host, port) not in endpoints
    return host not in hosts


def _pass_progress(report, early_new: int, ws: Path, since: float | None = None) -> bool:
    """pass 级进展只认 flag 候选/命中；未验证的 credentials 不算。

    c-03 的 prior 里长期留着 JS 串里的 Passw0rd!123，zero-progress 调度
    因此永远认为它“有进展”，把 fresh 的 e1/e2 题挤掉；pass 级必须用
    可验证的 flag 信号，候选凭据只影响 probe 续片，不影响槽位经济学。
    """
    return bool(report.new_matches or early_new) or bool(harvest(ws, since=since))


def _slice_has_progress(ws: Path, since: float | None = None) -> bool:
    """widen/续片门控的进展信号：只认**新信息**。

    explored 命令、session 行数、以及回显目标本身的资产都不算进展——
    任何一次命令都会产生它们，实测让 60s 试探闸门 27 次续片 / 0 次停止。
    真正算进展的只有：新端点（新主机或同主机新端口）、凭据、版本指纹、候选 flag。
    """
    try:
        prior = json.loads((ws / "out" / "prior.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior = {}
    if not isinstance(prior, dict):
        prior = {}
    if any(prior.get(key) for key in _HIGH_VALUE_PRIOR_KEYS):
        return True
    endpoints, hosts = _target_endpoints(ws)
    for key in ("assets", "open_leads"):
        for item in prior.get(key) or []:
            if _is_new_endpoint(str(item), endpoints, hosts):
                return True
    try:
        for path in (ws / "out" / "flags").glob("*.txt"):
            if since is None or path.stat().st_mtime >= since:
                return True
        return False
    except OSError:
        return False


async def _pipeline(ctx: RunContext, code: str, slice_s: float) -> None:
    if ctx.solver_slots is not None:
        ctx.solver_slots.register(code)
    try:
        await _pipeline_inner(ctx, code, slice_s)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - 埋点：pipeline 异常必须留痕
        import traceback

        ctx.journal.append(
            "pipeline_error", unique_code=code, err=repr(exc), tb=traceback.format_exc()[-1500:]
        )
        # 异常可能发生在 start 成功之后：容器若不关会永久占并发槽
        # （active_since_reconcile + 非终态 → eligible 永不重选），此处兜底释放。
        try:
            rec = ctx.registry.records.get(code)
            if rec is not None and rec.active_since_reconcile:
                await _safe_close(ctx, rec)
                rec.state = "QUEUED"
        except Exception:  # noqa: BLE001 - 兜底路径失败不掩盖原始异常
            pass
    finally:
        if ctx.solver_slots is not None:
            await ctx.solver_slots.unregister(code)


def _should_extend(remaining_s: float, completed: bool, has_progress: bool) -> bool:
    """60s 试探后续片条件：未完成、还有预算、且已有资产/凭据/线索。"""
    return not completed and remaining_s > 5.0 and has_progress


def _probe_decision(
    stage: int, has_progress: bool, high_value: bool, remaining_s: float, *, deep: bool = False
) -> str:
    """分层试探：60s 有资产 → 再 60s → 只有凭据/候选才继续吃剩余片。

    deep（多阶段/hard/多 flag）：即使首轮没 flag 也给满两段探测 + 剩余片，
    避免 b-02 这类“22 秒就关”的横向题被饿死。
    """
    if remaining_s <= 5.0:
        return "stop"
    if stage == 1:
        return "second" if (has_progress or deep) else "stop"
    if stage == 2:
        return "full" if (high_value or has_progress or deep) else "stop"
    return "full"


_DEEP_DESC_RE = re.compile(
    r"(?i)穿透|内网|横向|多阶段|全链路|防火墙|firewall|lateral|pivot|multi-stage|breach|chain"
)


def _is_deep_task(rec) -> bool:
    return (
        rec.flag_count > 1
        or rec.difficulty == "hard"
        or any(str(addr).endswith(":23") for addr in (rec.container_addr or []))
        or bool(_DEEP_DESC_RE.search(rec.description or ""))
    )


def _v2_enabled_for(code: str, rec=None, real_solver: bool = False) -> bool:
    """v2 灰度：多 flag 题默认走 subagent；HARNESS_V2 的 V2_CODES 白名单可额外指定。

    白名单存在时只有列出的题走 v2，其余题保持原串行路径 —— 可以在真实
    跑分里只灰度一道 hard/多阶段题，异常时清空白名单即可回退。
    """
    if rec is not None and rec.flag_count > 1 and real_solver:
        return True
    if not config_module.HARNESS_V2:
        return False
    raw = os.environ.get("V2_CODES", "").strip()
    if not raw:
        return True
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    return code in allowed


def _active_slots(ctx: RunContext) -> int:
    """并发槽真值 = 在飞 pipeline 数。

    registry.active_count 是平台对账视图，与 ctx.running 是同一批题；
    两者相加会把同一题算两遍（run-35236 实测：只剩 2 槽时被算成 4，
    第 3 槽空转 474s）。"""
    return len(ctx.running)


async def _salvage_candidates(ctx: RunContext, rec: ChallengeRecord, ws: Path, code: str) -> int:
    """异常路径兜底收割：提交 solver 已写出但尚未入袋的候选。"""
    candidates = [flag for flag in harvest(ws) if flag not in rec.submitted]
    if not candidates:
        return 0
    report = await submit_candidates(ctx.client, rec, candidates)
    ctx.journal.append(
        "salvage_submit",
        unique_code=code,
        candidates=len(candidates),
        matched=report.new_matches,
        rejected=report.rejected,
    )
    return report.new_matches


def _submitted_ledger(ws: Path) -> set[str]:
    found: set[str] = set()
    code = ws.name.rsplit("_attempt", 1)[0]
    for path in ws.parent.glob(f"{code}_attempt*/out/submitted.txt"):
        with contextlib.suppress(OSError):
            found.update(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    return found


def _save_submitted(ws: Path, rec: ChallengeRecord) -> None:
    with contextlib.suppress(OSError):
        (ws / "out" / "submitted.txt").write_text(
            "\n".join(sorted(rec.submitted)), encoding="utf-8"
        )


async def _pipeline_inner(ctx: RunContext, code: str, slice_s: float) -> None:
    rec = ctx.registry.records[code]
    rec.attempts += 1
    rec.reject_budget = 3
    attempt_start = time.time()
    rejected_before = rec.rejected_candidates
    rec.state = "STARTING"
    try:
        await ctx.client.start(code)
    except LevelLocked:
        ctx.registry.mark_locked(code)
        return  # LOCKED 不占额度、不需 close（§3.1）
    except StartFull:
        rec.state = "QUEUED"  # 并发满，下一轮对账后再试
        return
    except ChallengeGone:
        rec.state = "ABANDONED"
        return
    except TaskEnded:
        # start 撞 409 invalid_state ≠ 整场结束（实测单题 start 409 会把 55 道
        # 未跑题全部陪葬）。降级：关该题容器、重排队；token 真死时下一轮
        # _safe_list 返回 None 自然全局退出。
        ctx.journal.append("start_rejected", unique_code=code)
        await _safe_close(ctx, rec)
        if rec.state == "CLOSING":
            rec.state = "CLOSED"
        rec.state = "QUEUED"
        return
    rec.active_since_reconcile = True
    ctx.journal.append("started", unique_code=code)

    rec.state = "POLLING"
    ctx.journal.append("phase", unique_code=code, at="wait_available")
    if not await _wait_available(ctx, rec, AVAILABLE_TIMEOUT_S):
        await _safe_close(ctx, rec)
        if rec.state == "CLOSING":
            rec.state = "CLOSED"
        rec.state = "QUEUED"
        return

    rec.state = "SOLVING"
    ctx.journal.append("phase", unique_code=code, at="target_ready")
    await _wait_target_ready(ctx, rec, TARGET_READY_TIMEOUT_S)
    ctx.journal.append("phase", unique_code=code, at="spawn")
    sops = sop_mod.select(ctx.workspace_root, rec.description or "")  # EvoSOP 检索（§4.3）
    ws = prepare_workspace(
        ctx.workspace_root,
        rec,
        attempt=rec.attempts,
        prior=ctx.priors.get(code),
        sops=sops,
        repair_note=ctx.repair_notes.get(code),
    )
    rec.submitted |= _submitted_ledger(ws)
    deadline_epoch = min(time.time() + slice_s, ctx.t0 + ctx.config.total_time_limit_s)
    # 分级模型调度（实测 reasoner 支持 tool calls 且延迟 ~3s）：hard 题与
    # 失败过的题用 reasoner 攻坚提首片解出率，其余用 chat 保吞吐。
    solver_env = solver_env_for(ctx.env, rec, ctx.config, slice_s=slice_s)
    if solver_env.get("LLM_MODEL") == REASONER_MODEL:
        ctx.journal.append("model_upgraded", unique_code=code, model=REASONER_MODEL)
    # Staircase 流式切片（§2）：片内节拍早收割，不等整片跑完才提交
    solver_started = time.monotonic()
    slice_count = 0
    try:
        real_solver = any("solver/main.py" in str(part) for part in ctx.solver_cmd)
        if _v2_enabled_for(code, rec, real_solver):
            from types import SimpleNamespace

            from .v2_controller import StagePlanner, SurfacePlanner, V2Controller

            v2_started = time.monotonic()
            surfaces = os.environ.get("V2_SURFACES", "").strip().lower() in (
                "1",
                "on",
                "true",
                "yes",
            ) or (rec.flag_count > 1 and real_solver)
            if surfaces:
                planner = SurfacePlanner()
                max_parallel = 2
            else:
                planner = StagePlanner(multi_flag=rec.flag_count > rec.correct_flag_count)
                max_parallel = min(3, ctx.config.concurrency_limit)
            # 单题内允许多节点并行；全局 3 个 solver 进程由 solver_slots 兜底，
            # 普通片和 v2 节点共用同一个 Semaphore。
            controller = V2Controller(
                runner=None,
                max_parallel=max_parallel,
                node_timeout_s=max(60.0, slice_s),
                slots=ctx.solver_slots,
            )
            await controller.run_task(
                task=rec.description or "",
                ws=ws,
                client=ctx.client,
                journal=ctx.journal,
                rec=rec,
                deadline_epoch=deadline_epoch,
                total_budget_s=slice_s,
                planner=planner,
                solver_cmd=ctx.solver_cmd,
                base_environ=solver_env,
                slice_s=min(slice_s, streams.STREAM_INTERVAL_S * 20),
            )
            slice_count = 1
            outcome = SimpleNamespace(
                exit_code=0,
                timed_out=False,
                stalled=False,
                duration_s=time.monotonic() - v2_started,
            )
            early_new, early_completed = 0, False
        else:
            remaining_slice = slice_s
            probe_slice = min(PROBE_SLICE_S, remaining_slice)
            early_new, early_completed = 0, False
            outcome = None
            stage = 1
            while True:
                if stage == 1:
                    run_slice = probe_slice
                elif stage == 2:
                    run_slice = min(PROBE_SLICE_S, remaining_slice)
                else:
                    run_slice = remaining_slice
                run_slice = max(5.0, min(run_slice, remaining_slice))
                slice_count += 1
                sub_deadline = min(time.time() + run_slice, ctx.t0 + ctx.config.total_time_limit_s)
                outcome, sub_new, sub_completed = await streams.run_solver_with_stream(
                    solver_cmd=ctx.solver_cmd,
                    ws=ws,
                    deadline_epoch=sub_deadline,
                    slice_s=run_slice,
                    environ=solver_env,
                    client=ctx.client,
                    journal=ctx.journal,
                    unique_code=code,
                    rec=rec,
                    since=attempt_start,
                    slots=ctx.solver_slots,
                    slot_owner=code,
                )
                early_new += sub_new
                early_completed = early_completed or sub_completed
                remaining_slice -= run_slice
                if early_completed or rec.is_completed or remaining_slice <= 5.0:
                    break
                prior = _merge_prior(ctx.priors.get(code), collect_prior(ws))
                ctx.priors[code] = prior
                has_progress = _slice_has_progress(ws, since=attempt_start) or bool(
                    prior.get("credentials")
                )
                high_value = bool(harvest(ws, since=attempt_start))
                decision = _probe_decision(
                    stage, has_progress, high_value, remaining_slice, deep=_is_deep_task(rec)
                )
                if decision == "stop":
                    ctx.journal.append(
                        "probe_stop",
                        unique_code=code,
                        stage=stage,
                        remaining_s=round(remaining_slice, 1),
                    )
                    break
                ctx.journal.append(
                    "probe_extend",
                    unique_code=code,
                    stage=stage,
                    remaining_s=round(remaining_slice, 1),
                )
                for name in ("stdout.log", "stderr.log"):
                    source = ws / name
                    if source.is_file():
                        with contextlib.suppress(OSError):
                            shutil.copyfile(source, ws / f"{name}.slice{stage}.log")
                prepare_workspace(
                    ctx.workspace_root,
                    rec,
                    attempt=rec.attempts,
                    prior=prior,
                    sops=sops,
                    repair_note=ctx.repair_notes.get(code),
                )
                stage = 2 if decision == "second" else 3
    except BaseException:
        # 提交/WAF/网络异常不能让 solver 已产出的候选随重排丢失（run-35236 c-06）。
        with contextlib.suppress(Exception):
            await _salvage_candidates(ctx, rec, ws, code)
        raise
    ctx.journal.append(
        "solver_done",
        unique_code=code,
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        stalled=outcome.stalled,
        duration_s=round(time.monotonic() - solver_started, 1),
        slices=slice_count,
    )

    rec.state = "HARVESTING"
    ctx.priors[code] = _merge_prior(ctx.priors.get(code), collect_prior(ws))
    if ctx.memory is not None:
        ctx.memory.record(code, ctx.priors[code])  # 攻击图记忆：单写者=编排器
    candidates = [f for f in harvest(ws, since=attempt_start) if f not in rec.submitted]

    rec.state = "SUBMITTING"
    report = await submit_candidates(ctx.client, rec, candidates)
    _save_submitted(ws, rec)
    if report.new_matches or report.rejected or report.duplicates:
        ctx.journal.append(
            "submit_result",
            unique_code=code,
            matched=report.new_matches,
            rejected=report.rejected,
            duplicates=report.duplicates,
            source="final",
        )
    if report.task_ended:
        # 单题 submit 撞 409/404 ≠ 全场结束（与 start 同类：对已停容器提交
        # 也会 409）。降级为关题重排队；token 真死由下一轮 _safe_list 兜底。
        ctx.journal.append("submit_rejected", unique_code=code)
        report.task_ended = False

    # 槽位经济学：pass 级进展信号。无新 flag 且无新事实 → 零进展次数 +1，
    # 调度器会把该题沉底并停止补位；一旦出现事实/flag 则清零。
    pass_progress = _pass_progress(report, early_new, ws, since=attempt_start)
    if pass_progress:
        rec.zero_progress_passes = 0
    elif not (rec.is_completed or report.completed or early_completed):
        rec.zero_progress_passes += 1
        ctx.journal.append(
            "zero_progress_pass",
            unique_code=code,
            count=rec.zero_progress_passes,
        )

    if report.new_matches or early_new:
        # EVOSTER 锚定（§5）：上一轮修复动作命中 → 权重 +25%
        last = ctx.last_repair.get(code)
        if last:
            update_weights(ctx.repair_weights, last, success=True)
        # EvoSOP 蒸馏（§4）：本轮有命中 → 把可复用序列蒸馏入库（fail-closed）
        sop_mod.distill(ws, ctx.workspace_root)
        # 正确提交可能触发关卡解锁 → LOCKED 题重新入队（§5.1）
        for unlocked in ctx.registry.requeue_unlocked():
            ctx.journal.append("requeued_unlocked", unique_code=unlocked)

    # ---- 终局判定 ------------------------------------------------------------
    if rec.is_completed or report.completed or early_completed:
        rec.state = "SOLVED"
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        ctx.repair_notes.pop(code, None)
        ctx.journal.append("solved", unique_code=code, matches=len(rec.matched_indices))
        return
    if ctx.stopped:
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        return
    if report.new_matches or early_new:
        rec.state = "QUEUED"  # 多 flag 题继续（§4.6 不提前放弃）
        await _safe_close(ctx, rec)
        rec.state = "QUEUED"
        return
    max_attempts = MAX_ATTEMPTS + max(0, rec.flag_count - 1) * 2
    if rec.attempts >= max_attempts:
        rec.state = "ABANDONED"
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        ctx.repair_notes.pop(code, None)
        ctx.journal.append("abandoned", unique_code=code)
        return
    # EVOSTER 在线修复（§5）：失败信号 → 有界动作；attempt 预算不动
    repair = choose_repair(
        rec,
        outcome,
        slice_s,
        total_remaining_s=ctx.t0 + ctx.config.total_time_limit_s - time.time(),
        slice_max_s=ctx.config.slice_max_s,
        weights=ctx.repair_weights,
        progress=_slice_has_progress(ws),
    )
    ctx.last_repair[code] = repair.action
    ctx.journal.append("repair", unique_code=code, action=repair.action, note=repair.note)
    if repair.next_slice_s:
        rec.repair_widened = True  # widen 最多一次；切片只覆盖下一轮一次
        rec.next_slice_s = repair.next_slice_s
    if repair.note and repair.action == "recompile":
        ctx.repair_notes[code] = repair.note
    if rec.rejected_candidates > rejected_before:
        ctx.repair_notes[code] = (ctx.repair_notes.get(code, "") + " " + REJECTION_NOTE).strip()
    rec.state = "RETRY"
    await _safe_close(ctx, rec)
    rec.state = "QUEUED"  # 按修复计划重投（换策略/更大时间片）


# ------------------------------------------------------------------ run ----
def _kill_stale_solvers() -> None:
    """启动兜底：杀掉上一个编排器留下的 solver（重启竞态会绕过 Makefile 等待）。"""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "solver/main.py" not in cmd:
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


async def run_agent(
    *,
    base_url: str,
    token: str,
    solver_cmd: list[str],
    config: RuntimeConfig | None = None,
    workspace: Path | None = None,
    journal_path: Path | None = None,
    env: dict[str, str] | None = None,
    qps: float = 3.0,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> dict:
    """编排器主循环。返回计分摘要（awarded / cumulative_score）。"""
    config = config or RuntimeConfig(
        mode="managed",
        base_url=base_url.rstrip("/"),
        token=token,
        llm_base_url="",
        llm_api_key="",
        llm_model="deepseek-chat",
        total_time_limit_s=21600,
        reserve_seconds=600,
        concurrency_limit=3,
        api_max_qps=qps,
    )
    workspace = (workspace or Path("./workspaces")).resolve()
    journal = Journal(journal_path or (workspace / "journal.jsonl"))
    client = PlatformClient(base_url.rstrip("/"), token, qps=qps)
    # 攻击图记忆（harness-v2.md §6）：MEMORY_TDB=off 或 triviumdb 缺失 → 空操作降级
    memory = AttackMemory(
        workspace / "attack.tdb",
        enabled=os.environ.get("MEMORY_TDB", "on").strip().lower() != "off",
    )
    _kill_stale_solvers()
    ctx = RunContext(
        config=config,
        client=client,
        registry=Registry(),
        journal=journal,
        workspace_root=workspace,
        solver_cmd=solver_cmd,
        env=env,
        t0=time.time(),
        memory=memory,
        solver_slots=streams.FairSlotPool(max(1, config.concurrency_limit)),
    )
    tasks: set[asyncio.Task] = set()
    reconciled = False
    list_failures = 0
    try:
        while not ctx.stopped and time.time() - ctx.t0 < config.total_time_limit_s:
            rows = await _safe_list(ctx)
            if rows is None:
                list_failures += 1
                if list_failures >= LIST_FAIL_LIMIT:
                    ctx.journal.append("list_fail_limit", failures=list_failures)
                    break
                await asyncio.sleep(poll_interval_s)
                continue
            list_failures = 0
            if ctx.stopped:
                break
            ctx.registry.sync(rows)
            if not reconciled:
                # 开局对账即关残留（round2 活锁教训）：上进程留下的 active 容器若
                # 占满平台全局 active 上限，start 持续 409 StartFull、eligible 又
                # 非空 → 2s 轮询空转到终局零解。此刻本进程必无在跑题，关掉立即
                # 释放槽位；被关的题下一轮 sync 见 stopped 即恢复 eligible。
                reconciled = True
                for rec in ctx.registry.records.values():
                    if rec.active_since_reconcile:
                        await _safe_close(ctx, rec)
                        rec.state = "QUEUED"
                        ctx.journal.append("reconcile_close", unique_code=rec.unique_code)
            plan = plan_phase(
                elapsed_s=time.time() - ctx.t0,
                total_s=config.total_time_limit_s,
                reserve_s=config.reserve_seconds,
                # 分母 = 真正还要解的题（可启动 + 在跑），在跑的也是预算消耗者
                n_eligible=len(eligible(ctx.registry.records)) + len(ctx.running),
                remaining_score=_remaining_score(ctx.registry),
                slice_min_s=config.slice_min_s,
                slice_max_s=config.slice_max_s,
                concurrency=config.concurrency_limit,
            )
            if plan.finalize:
                ctx.journal.append("finalize")
                break
            # 空闲即退出：没有在跑的题、也没有可启动的题（全部通关/LOCKED/放弃），
            # 继续轮询只会空烧预算。LOCKED 题只能靠自己的正确提交解锁，
            # 没有在跑的题时它永远等不到解锁信号。
            if not ctx.running and not eligible(ctx.registry.records):
                ctx.journal.append("idle_exit")
                break
            for rec in eligible(ctx.registry.records):
                if not can_start(
                    active_count=_active_slots(ctx),
                    concurrency_limit=config.concurrency_limit,
                ):
                    break
                if rec.unique_code in ctx.running:
                    continue
                rec.state = "QUEUED"
                # 分数加权（§4.3 修订）：高分多阶段题拿更长片；EVOSTER widen 的
                # 覆盖值（已在上一轮按加权片算好）优先，用一次即清。
                attempt_slice = rec.next_slice_s or slice_for(
                    rec,
                    plan.slice_s,
                    slice_min_s=config.slice_min_s,
                    slice_max_s=plan.slice_cap_s,
                )
                prior = ctx.priors.get(rec.unique_code) or {}
                has_progress = any(
                    prior.get(key)
                    for key in ("assets", "explored", "open_leads", "credentials", "findings")
                )
                if plan.phase >= 2 and not should_start(
                    rec, attempt_slice, has_progress=has_progress
                ):
                    rec.next_slice_s = attempt_slice  # 保留加权片，下次再试
                    continue
                rec.next_slice_s = None
                task = asyncio.create_task(_pipeline(ctx, rec.unique_code, attempt_slice))
                tasks.add(task)
                ctx.running.add(rec.unique_code)
                task.add_done_callback(
                    lambda t, c=rec.unique_code: (
                        ctx.running.discard(c),
                        ctx.slot_freed.set(),
                    )
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ctx.slot_freed.wait(), timeout=poll_interval_s)
            ctx.slot_freed.clear()
        # 收尾保护（§4.3）：等在跑的 pipeline 结束，再强制 close 一切活跃容器
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for rec in ctx.registry.records.values():
            if rec.active_since_reconcile:
                await _safe_close(ctx, rec)
                rec.state = "CLOSED"
        rows = await _safe_list(ctx)
        summary = score_summary(rows or [])
        summary["per_challenge"] = {
            code: {"state": rec.state, "attempts": rec.attempts}
            for code, rec in sorted(ctx.registry.records.items())
        }
        summary["memory"] = memory.stats()  # 攻击图记忆：节点数/边数（降级时为 enabled: False）
        journal.append("run_end", **{k: v for k, v in summary.items() if k != "per_challenge"})
        return summary
    finally:
        for task in tasks:
            task.cancel()
        memory.close()
        journal.close()


_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".sh")


def _path_like(token: str) -> bool:
    return token.startswith(("/", "./", "../")) or token.endswith(_SCRIPT_SUFFIXES)


def _iter_script_tokens(cmd: list[str]):
    for token in cmd:
        if token.startswith("-"):
            continue
        if _path_like(token):
            yield token


def resolve_solver_cmd(
    config: RuntimeConfig,
    *,
    environ: dict[str, str] | None = None,
    module_file: str | Path | None = None,
    python_exe: str | None = None,
) -> list[str]:
    """解析 solver 入口，镜像与仓库两种布局都成立（启动期硬校验）。

    run-34162 根因：`Path(__file__).parents[3]` 在仓库里是仓库根，在镜像
    （pip install 到 site-packages）里是 `venv/lib`，拼出的 solver/main.py
    不存在。solver 每次 spawn 都 exit 2，编排器只看到失败 → 关靶场重投，
    整轮零提交。解析顺序：SOLVER_CMD（操作者显式覆盖）→ TSECBENCH_SOLVER_DIR
    → 仓库相对路径；全部不存在时启动即失败，绝不静默烧完一轮。
    """
    env = os.environ if environ is None else environ
    python = python_exe or sys.executable
    module = Path(module_file or __file__).resolve()

    if config.solver_cmd:
        cmd = list(config.solver_cmd)
        for token in _iter_script_tokens(cmd):
            if not Path(token).is_file():
                raise FileNotFoundError(f"solver entry not found: {token}")
        return cmd

    tried: list[Path] = []
    solver_dir = env.get("TSECBENCH_SOLVER_DIR", "").strip()
    if solver_dir:
        tried.append(Path(solver_dir) / "main.py")
    if len(module.parents) > 3:
        tried.append(module.parents[3] / "solver" / "main.py")
    for path in tried:
        if path.is_file():
            return [python, str(path)]
    raise FileNotFoundError(
        "solver entry not found; set SOLVER_CMD or TSECBENCH_SOLVER_DIR. tried: "
        + ", ".join(str(p) for p in tried)
    )


def main() -> int:
    config = resolve_config()
    # solver 在题目工作区目录下被启动（cwd ≠ 仓库根），`-m solver.main` 不可导入 ——
    # 用绝对路径直接执行脚本（solver/main.py 自带 .env 定位，不依赖 cwd）。
    try:
        solver_cmd = resolve_solver_cmd(config)
    except FileNotFoundError as exc:
        print(f"[orchestrator] FATAL: {exc}", file=sys.stderr, flush=True)
        return 2
    summary = asyncio.run(
        run_agent(
            base_url=config.base_url,
            token=config.token,
            solver_cmd=solver_cmd,
            config=config,
        )
    )
    print(f"[orchestrator] done: {summary}")
    return 0
