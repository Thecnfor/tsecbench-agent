"""端到端测试：mock server + 假 solver，编排器全链路真实测试（runbook.md §4.2）。

假 solver 不调 LLM：读 SOLVER_TASK 指向的 task.json，按 SOLVER_FAKE_MODE
模拟正常 / 只打 stdout / 超时不退 / 崩溃 / 第二轮才解出等行为。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest
from mock_server.server import make_server
from mock_server.state import ChallengeSpec
from orchestrator.config import RuntimeConfig
from orchestrator.journal import Journal
from orchestrator.main import run_agent

TOKEN = "mock-token"
PY = sys.executable

FAKE_SOLVER = r"""
import json, os, sys, time
from pathlib import Path

task = json.loads(Path(os.environ["SOLVER_TASK"]).read_text(encoding="utf-8"))
mode = os.environ.get("SOLVER_FAKE_MODE", "ok")
flag = os.environ.get("SOLVER_FAKE_FLAG", "FLAG{fake}")
ws = Path(os.environ["SOLVER_TASK"]).parent
out = ws / "out" / "flags"
out.mkdir(parents=True, exist_ok=True)

if mode == "ok":
    (out / "000.txt").write_text(flag + "\n", encoding="utf-8")
elif mode == "stdout":
    print("candidate found: " + flag, flush=True)
elif mode == "slow":
    (out / "000.txt").write_text(flag + "\n", encoding="utf-8")
    print("wrote flag, hanging forever", flush=True)
    time.sleep(999)
elif mode == "crash":
    (out / "000.txt").write_text(flag + "\n", encoding="utf-8")
    sys.exit(3)
elif mode == "attempt2":
    if task.get("attempt", 1) >= 2:
        (out / "000.txt").write_text(flag + "\n", encoding="utf-8")
    else:
        print("no idea yet (attempt 1)", flush=True)
elif mode == "nothing":
    print("no idea", flush=True)
# 总是写 result.json（编排器不信任它）
status = "solved" if mode == "ok" else "unknown"
(ws / "out" / "result.json").write_text(json.dumps({"status": status}))
"""


@pytest.fixture
def env_factory(tmp_path):
    servers: list = []

    def make(specs, *, env=None):
        server, state = make_server(specs, env=env, token=TOKEN)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        solver = tmp_path / "fake_solver.py"
        solver.write_text(FAKE_SOLVER, encoding="utf-8")
        return base, state, solver

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def make_config(total_s=120, reserve=5, concurrency=3) -> RuntimeConfig:
    return RuntimeConfig(
        mode="managed",
        base_url="",
        token=TOKEN,
        llm_base_url="",
        llm_api_key="sk-test",
        llm_model="deepseek-chat",
        total_time_limit_s=total_s,
        reserve_seconds=reserve,
        concurrency_limit=concurrency,
        api_max_qps=50.0,
        hint_cost_threshold=0.1,
        slice_min_s=2,  # e2e 用极短时间片，让超时杀/多轮重试在秒级发生
        slice_max_s=6,
    )


def run(
    base,
    solver,
    tmp_path,
    config,
    *,
    env=None,
    journal_path=None,
    initial_hints=None,
):
    return asyncio.run(
        run_agent(
            base_url=base,
            token=TOKEN,
            solver_cmd=[PY, str(solver)],
            config=config,
            workspace=tmp_path / "ws",
            journal_path=journal_path or (tmp_path / "journal.jsonl"),
            initial_hints=initial_hints,
            env=env,
            qps=50.0,
            poll_interval_s=0.1,
        )
    )


def fake_env(flag: str, mode: str = "ok") -> dict[str, str]:
    return {"SOLVER_FAKE_FLAG": flag, "SOLVER_FAKE_MODE": mode, "PATH": str(Path(PY).parent)}


# ---- 1. 单题全流程 ----
def test_single_challenge_full_flow(env_factory, tmp_path):
    base, state, solver = env_factory([ChallengeSpec("c1", flags=["FLAG{one}"])])
    summary = run(base, solver, tmp_path, make_config(), env=fake_env("FLAG{one}"))
    assert summary["awarded"] == 100
    assert summary["cumulative_score"] == 100
    assert summary["completed"] == 1
    assert ("c1", "FLAG{one}") in state.correct_submitted
    assert summary["per_challenge"]["c1"]["state"] in ("CLOSED", "SOLVED")
    # 容器被 close 释放
    assert state.live["c1"]["container_status"] == "stopped"


# ---- 2. flag 只在 stdout ----
def test_flag_only_in_stdout(env_factory, tmp_path):
    """不信任 result.json：result.json 说 unknown，stdout 里的 flag 照样提交。"""
    base, state, solver = env_factory([ChallengeSpec("c1", flags=["FLAG{stdout}"])])
    summary = run(
        base, solver, tmp_path, make_config(), env=fake_env("FLAG{stdout}", mode="stdout")
    )
    assert summary["awarded"] == 100


# ---- 3. 多 flag 题 ----
def test_multi_flag_all_collected(env_factory, tmp_path):
    """一题多 flag：两个都提交，correct_flag_count 递增到齐，不提前放弃。"""
    solver_multi = tmp_path / "fake_solver_multi.py"
    solver_multi.write_text(
        FAKE_SOLVER.replace(
            """if mode == "ok":
    (out / "000.txt").write_text(flag + "\\n", encoding="utf-8")""",
            """if mode == "multi":
    code = task["unique_code"]
    (out / "000.txt").write_text(f"FLAG{{{code}_1}}\\n", encoding="utf-8")
    (out / "001.txt").write_text(f"FLAG{{{code}_2}}\\n", encoding="utf-8")
elif mode == "ok":
    (out / "000.txt").write_text(flag + "\\n", encoding="utf-8")""",
        ),
        encoding="utf-8",
    )
    base, state, solver = env_factory([ChallengeSpec("m1", flags=["FLAG{m1_1}", "FLAG{m1_2}"])])
    summary = run(
        base,
        solver_multi,
        tmp_path,
        make_config(),
        env={"SOLVER_FAKE_MODE": "multi", "PATH": str(Path(PY).parent)},
    )
    assert summary["completed"] == 1
    assert summary["total_flags"] == 2  # 两个 flag 都已提交
    assert summary["awarded"] == 100  # total_score=100 的题全解 = 100


# ---- 4. solver 超时被杀仍收割 ----
def test_timeout_still_harvests(env_factory, tmp_path):
    base, state, solver = env_factory([ChallengeSpec("c1", flags=["FLAG{hung}"])])
    summary = run(base, solver, tmp_path, make_config(), env=fake_env("FLAG{hung}", mode="slow"))
    assert summary["awarded"] == 100
    assert ("c1", "FLAG{hung}") in state.correct_submitted


# ---- 5. solver 崩溃 ----
def test_solver_crash_still_harvests(env_factory, tmp_path):
    base, state, solver = env_factory([ChallengeSpec("c1", flags=["FLAG{crash}"])])
    summary = run(base, solver, tmp_path, make_config(), env=fake_env("FLAG{crash}", mode="crash"))
    assert summary["awarded"] == 100


# ---- 6. 3 并发 + 第 4 题 start 409 腾位 ----
def test_concurrency_gate_with_four(env_factory, tmp_path):
    """并发闸门 ≤3：第 4 题会撞 409，靠 close 腾位后仍要全部解完。"""
    solver_by_code = tmp_path / "fake_solver_by_code.py"
    solver_by_code.write_text(
        FAKE_SOLVER.replace(
            'flag = os.environ.get("SOLVER_FAKE_FLAG", "FLAG{fake}")',
            'flag = "FLAG{" + task["unique_code"] + "}"',
        ),
        encoding="utf-8",
    )
    specs = [ChallengeSpec(f"c{i}", flags=[f"FLAG{{c{i}}}"]) for i in range(4)]
    base, state, solver = env_factory(specs)
    summary = run(
        base,
        solver_by_code,
        tmp_path,
        make_config(total_s=60, concurrency=3),
        env={"SOLVER_FAKE_MODE": "ok", "PATH": str(Path(PY).parent)},
    )
    assert summary["completed"] == 4
    assert summary["awarded"] == 400


# ---- 7. 403 LOCKED → 解锁 → 重新入队 ----
def test_level_gate_lock_and_unlock(env_factory, tmp_path):
    specs = [
        ChallengeSpec("easy", flags=["FLAG{easy}"]),
        ChallengeSpec("hard", level=2, flags=["FLAG{hard}"]),
    ]
    base, state, solver = env_factory(
        specs, env={"MOCK_LEVEL_GATE": "on", "MOCK_CURRENT_LEVEL": "1"}
    )
    # 每个 solver 用各自 flag：通过 task.json 的 unique_code 区分
    solver2 = tmp_path / "fake_solver_by_code.py"
    solver2.write_text(
        FAKE_SOLVER.replace(
            'flag = os.environ.get("SOLVER_FAKE_FLAG", "FLAG{fake}")',
            'flag = "FLAG{" + task["unique_code"] + "}"',
        ),
        encoding="utf-8",
    )
    summary = run(
        base,
        solver2,
        tmp_path,
        make_config(total_s=90),
        env={"SOLVER_FAKE_MODE": "ok", "PATH": str(Path(PY).parent)},
    )
    assert summary["completed"] == 2
    assert summary["awarded"] == 200


# ---- 8. hint 路径：零 flag → 取 hint → 第二轮解出 ----
def test_hint_taken_then_solved(env_factory, tmp_path):
    base, state, solver = env_factory([ChallengeSpec("c1", flags=["FLAG{late}"])])
    journal_path = tmp_path / "journal.jsonl"
    summary = run(
        base,
        solver,
        tmp_path,
        make_config(total_s=60, reserve=3),
        env=fake_env("FLAG{late}", mode="attempt2"),
        journal_path=journal_path,
    )
    assert summary["awarded"] == 100
    assert "c1" in state.hint_viewed  # 确实取过 hint
    events = [
        json.loads(ln)["event"] for ln in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "hint" in events


# ---- 9. 崩溃恢复：Journal 记过 hint → 重启不重复取 ----
def test_crash_recovery_no_double_hint(env_factory, tmp_path):
    base, state, solver = env_factory([ChallengeSpec("c1", flags=["FLAG{never}"])])
    journal_path = tmp_path / "journal.jsonl"
    j = Journal(journal_path)
    j.append_hint("c1", attempt=1)  # 上次运行已取过
    j.close()
    summary = run(
        base,
        solver,
        tmp_path,
        make_config(total_s=30, reserve=3),
        env=fake_env("FLAG{never}", mode="nothing"),
        journal_path=journal_path,
    )
    assert summary["awarded"] == 0
    # 平台侧从未收到 hint 请求 —— Journal 恢复生效
    assert "c1" not in state.hint_viewed


# ---- 10. 极短 TOTAL：收尾保护 ----
def test_short_total_finalize(env_factory, tmp_path):
    specs = [
        ChallengeSpec("c1", flags=["FLAG{a}"]),
        ChallengeSpec("c2", flags=["FLAG{b}"], start_delay_s=0.3),
    ]
    base, state, solver = env_factory(specs)
    summary = run(
        base,
        solver,
        tmp_path,
        make_config(total_s=3, reserve=1),
        env=fake_env("FLAG{a}"),
    )
    # 收尾保护触发后正常退出并出报告（不挂死、不抛异常）
    assert "awarded" in summary
    for entry in state.live.values():
        assert entry["container_status"] == "stopped"  # 全部被 close


# ---- 11. 多 flag 去重：duplicate 不算失败、不重复计分 ----
def test_duplicates_not_double_counted(env_factory, tmp_path):
    specs = [
        ChallengeSpec("c1", flags=["FLAG{dup}"]),
        ChallengeSpec("c2", flags=["FLAG{dup}"]),  # 两题同 flag 值，互不干扰
    ]
    base, state, solver = env_factory(specs)
    solver3 = tmp_path / "fake_solver_dup.py"
    solver3.write_text(
        FAKE_SOLVER.replace(
            'flag = os.environ.get("SOLVER_FAKE_FLAG", "FLAG{fake}")',
            'flag = "FLAG{dup}"',
        ).replace('mode = os.environ.get("SOLVER_FAKE_MODE", "ok")', 'mode = "ok"'),
        encoding="utf-8",
    )
    summary = run(base, solver3, tmp_path, make_config(), env={"PATH": str(Path(PY).parent)})
    assert summary["awarded"] == 200


# ---- 12. 活锁回归：开局残留 active 容器占满平台上限 ----
def _raw(base: str, method: str, path: str, *, code: str = "") -> dict:
    """绕过编排器直连 mock API（为回归测试播种残留现场）。"""
    url = f"{base}{path}" + (f"?unique_code={code}" if code else "")
    req = urllib.request.Request(url, method=method, headers={"BENCHMARK_TOKEN": TOKEN})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"{}")


def test_reconcile_close_unblocks_active_limit(env_factory, tmp_path):
    """round2 实测活锁：上进程残留 3 个 active（=平台上限）时，若开局不关，
    start 持续 409 StartFull、eligible 又非空 → 2s 轮询空转到终局零解。
    开局对账必须先关残留，释放槽位后全部题恢复可排。"""
    specs = [ChallengeSpec(f"c{i}", flags=["FLAG{free}"]) for i in range(4)]
    base, state, solver = env_factory(specs)
    for i in range(3):  # 播种 3 个残留（占满 mock CONCURRENCY_LIMIT=3）
        _raw(base, "POST", "/openapi/v1/challenges/start", code=f"c{i}")
    _raw(base, "GET", "/openapi/v1/challenges")  # 推进 pending → available
    assert state.active_count() == 3

    journal_path = tmp_path / "journal.jsonl"
    summary = run(
        base,
        solver,
        tmp_path,
        make_config(total_s=8, reserve=2),
        env=fake_env("FLAG{free}"),
        journal_path=journal_path,
    )
    events = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    closed = sorted(e["unique_code"] for e in events if e["event"] == "reconcile_close")
    assert closed == ["c0", "c1", "c2"]  # 残留被开局清场（修复前为空）
    assert summary["completed"] >= 3  # 不再空转到终局（修复前为 0）
    for entry in state.live.values():
        assert entry["container_status"] == "stopped"
