# TSecBench 能力优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/superpowers/specs/2026-10-17-tsecbench-capability-optimization-design.md` 落地执行层、策略层与 v2 接线，直接改善漏洞利用/多阶段/Web 三类短板。

**Architecture:** 线上入口是 `solver/main.py`（由轨迹反证）。Phase A 改 recon 预算与 URL 推导、长上下文裁剪、结构化 findings/prior、调度短片闸门；Phase B 增加通用攻击循环与 KB 多阶段 cheatsheet；Phase C 在 `HARNESS_V2=on` 时把 `V2Controller + StagePlanner` 接到 `_pipeline_inner`，默认关闭。

**Tech Stack:** Python 3.13、pytest、ruff、FastAPI mock 平台（离线测试）、现有 `orchestrator`/`solver` 模块。

## Global Constraints

- Python 3.13 硬约束；禁止 PEP 758 无括号 `except A, B:`，统一 `except (A, B):`。
- ruff：line-length 100、target py313、规则 `E,F,I,UP,B,SIM`；提交前 `make lint && make test`。
- 单测完全离线：不依赖真实 LLM、真实平台 API、真实靶标。
- `BENCHMARK_TOKEN` 绝不进 solver 环境；本计划不修改该防线。
- 镜像与 KB/prompt 不得含 flag 字面量、题目答案或题目特化内容。
- `prior.json` 只做**附加键**扩展；缺键/畸形按空处理，绝不阻塞出题。
- `HARNESS_V2=off` 是默认值，v1 路径零改动。
- solver 改动对下一题立即生效；orchestrator 改动需要 `make rerun` 才生效。
- 每个 commit 信息末尾加：`Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`。

---

### Task 1: recon 预算感知、URL 推导与 KB 自动命中

**Files:**
- Modify: `solver/recon_recipe.py`
- Test: `orchestrator/tests/test_recon_budget.py`（新建）

**Interfaces:**
- Produces:
  - `resolve_budget_seconds(explicit: float | None = None) -> float | None`
  - `derive_web_url(target: str, open_ports: list[str]) -> str`
  - `recon_host(target: str, budget_s: float | None = None) -> dict`
  - `recon_web(url: str, budget_s: float | None = None) -> dict`
  - `recon_web(...)` 结果新增可选键 `dirbust_skipped`、`kb_hits`。

- [ ] **Step 1: 写失败测试**

```python
"""recon 预算与 URL 推导（轨迹证据：85s 片内固定侦察开销 + mode=all 参数错误）。"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def load_recipe():
    sys.path.insert(0, str(SOLVER_DIR))
    sys.modules.pop("recon_recipe", None)
    return importlib.import_module("recon_recipe")


def test_resolve_budget_prefers_explicit():
    rr = load_recipe()
    assert rr.resolve_budget_seconds(42) == 42


def test_resolve_budget_reads_deadline_env(monkeypatch):
    rr = load_recipe()
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", str(time.time() + 30))
    budget = rr.resolve_budget_seconds()
    assert budget is not None and 20 <= budget <= 31


def test_resolve_budget_absent(monkeypatch):
    rr = load_recipe()
    monkeypatch.delenv("SOLVER_DEADLINE_EPOCH", raising=False)
    assert rr.resolve_budget_seconds() is None


def test_derive_web_url_prefers_common_ports():
    rr = load_recipe()
    assert rr.derive_web_url("h", ["8080"]) == "http://h:8080"
    assert rr.derive_web_url("h", ["443"]) == "https://h"
    assert rr.derive_web_url("h", ["8443"]) == "https://h:8443"
    assert rr.derive_web_url("h", []) == "http://h"


def test_low_budget_skips_dirbust(monkeypatch):
    rr = load_recipe()
    monkeypatch.setattr(
        rr, "_which", lambda name: "feroxbuster" if name == "feroxbuster" else "curl"
    )
    monkeypatch.setattr(rr, "_curl_page", lambda curl, url: "HTTP/1.1 200 OK\r\nServer: x\r\n\r\n")
    called = []
    monkeypatch.setattr(rr, "_run", lambda argv, timeout=120: called.append(argv) or "")
    result = rr.recon_web("http://h:8080", budget_s=5)
    assert result["dirbust_skipped"] == "low-budget"
    assert not any("feroxbuster" in " ".join(map(str, argv)) for argv in called)


def test_low_budget_uses_small_nmap(monkeypatch):
    rr = load_recipe()
    monkeypatch.setattr(rr, "_which", lambda name: "nmap")
    seen = {}

    def fake_run(argv, timeout=120):
        seen["argv"] = argv
        seen["timeout"] = timeout
        return "80/tcp open http nginx 1.18.0\n"

    monkeypatch.setattr(rr, "_run", fake_run)
    rr.recon_host("10.0.0.1", budget_s=10)
    assert "--top-ports" in seen["argv"] and "100" in seen["argv"]
    assert seen["timeout"] <= 10
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_recon_budget.py -v`
Expected: FAIL（`AttributeError: module 'recon_recipe' has no attribute 'resolve_budget_seconds'`）

- [ ] **Step 3: 实现**

在 `solver/recon_recipe.py` 的 `TOOL_TIMEOUT` 后加入：

```python
_PREFERRED_PORTS = ("443", "8443", "80", "8080", "8000", "3000", "7860", "5000")


def resolve_budget_seconds(explicit: float | None = None) -> float | None:
    """显式预算 > SOLVER_DEADLINE_EPOCH - now；两者皆无返回 None（保持旧行为）。"""
    if explicit is not None:
        return max(1.0, float(explicit))
    raw = os.environ.get("SOLVER_DEADLINE_EPOCH", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw) - time.time())
    except ValueError:
        return None


def derive_web_url(target: str, open_ports: list[str]) -> str:
    """从 nmap 开放端口推导最可能的 Web 入口。"""
    ports = {str(port) for port in open_ports}
    for port in _PREFERRED_PORTS:
        if port not in ports:
            continue
        scheme = "https" if port in ("443", "8443") else "http"
        if (scheme == "https" and port == "443") or (scheme == "http" and port == "80"):
            return f"{scheme}://{target}"
        return f"{scheme}://{target}:{port}"
    return f"http://{target}"


def _kb_hits(*parts: str, top_k: int = 2) -> list[dict]:
    query = " ".join(part for part in parts if part).strip()
    if not query:
        return []
    try:
        from kb.index import Index

        return Index().query(query, top_k=top_k)
    except Exception:  # noqa: BLE001 - KB 是增强，不是必需路径
        return []
```

改 `recon_host(target: str)` 为 `recon_host(target: str, budget_s: float | None = None)`，并在 nmap 调用前计算：

```python
    budget = resolve_budget_seconds(budget_s)
    ports = "1000"
    timeout = TOOL_TIMEOUT
    if budget is not None:
        if budget < 45:
            ports = "100"
        timeout = max(10, min(TOOL_TIMEOUT, int(budget * 0.6)))
    out = _run([nmap, "-Pn", "--top-ports", ports, "-sV", "--open", target], timeout=timeout)
```

改 `recon_web(url: str)` 为 `recon_web(url: str, budget_s: float | None = None)`；在 dirbust 分支前：

```python
    budget = resolve_budget_seconds(budget_s)
    if dirbust and budget is not None and budget < 20:
        result["dirs"] = []
        result["dirbust_skipped"] = "low-budget"
        dirbust = None
```

在 `recon_web` 的 `return result` 前追加：

```python
hits = _kb_hits(
    result.get("title", ""),
    " ".join(result.get("tech") or []),
    result.get("headers", {}).get("server", ""),
)
if hits:
    result["kb_hits"] = [
        {"doc": hit["doc"], "section": hit["section"], "body": hit["body"][:800]} for hit in hits
    ]
```

CLI 入口给三个子命令都加 `--budget-seconds`（`type=float, default=None`），并传给 `recon_host`/`recon_web`；`mode=all` 分支改为：

```python
            host = recon_host(args.target, budget_s=args.budget_seconds)
            report["host"] = host
            url = args.url or derive_web_url(args.target, host.get("open_ports") or [])
            report["web"] = recon_web(url, budget_s=args.budget_seconds)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_recon_budget.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solver/recon_recipe.py orchestrator/tests/test_recon_budget.py
git commit -m "$(cat <<'EOF'
feat(recon): 预算感知侦察 + URL 推导 + KB 命中

85s 片内固定 nmap/dirbust 会吃光预算；mode=all 缺 url 直接报错。
按剩余片降级端口集、跳过 dirbust，并从开放端口推导入口。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `run_recon` 接受 target-only 的 all 模式

**Files:**
- Modify: `solver/common.py:264-288`
- Test: `orchestrator/tests/test_solver_harness_tools.py`

**Interfaces:**
- Consumes: Task 1 的 `derive_web_url`。
- Produces: `run_recon({"mode": "all", "target": ...})` 不再要求 `url`。

- [ ] **Step 1: 写失败测试**

在 `orchestrator/tests/test_solver_harness_tools.py` 追加：

```python
def test_recon_all_derives_url_from_open_ports(common, monkeypatch):
    fake = types.SimpleNamespace(
        recon_host=lambda target: {"target": target, "open_ports": ["8080"]},
        recon_web=lambda url: {"url": url, "dirs": []},
        derive_web_url=lambda target, ports: f"http://{target}:{ports[0]}",
    )
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    out = common.run_recon({"mode": "all", "target": "10.0.0.1"})
    assert out["web"]["url"] == "http://10.0.0.1:8080"
    assert out["host"]["target"] == "10.0.0.1"
```

同时把既有 `test_recon_dispatch_host_and_web` 的 fake 加上
`derive_web_url=lambda target, ports: "http://x/"`。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_solver_harness_tools.py -k "recon_all or recon_dispatch" -v`
Expected: FAIL（`target and url required for mode=all`）

- [ ] **Step 3: 实现**

把 `common.run_recon` 的 `all` 分支替换为：

```python
if mode == "all":
    if not target:
        return {"error": "target required for mode=all"}
    host = recon_recipe.recon_host(target)
    web_url = url or recon_recipe.derive_web_url(target, host.get("open_ports") or [])
    return {"host": host, "web": recon_recipe.recon_web(web_url)}
```

`web` 分支增加 target 兜底：

```python
        if mode == "web":
            if not url and target.startswith(("http://", "https://")):
                url = target
            if not url:
                return {"error": "url required for mode=web"}
            return {"web": recon_recipe.recon_web(url)}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_solver_harness_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solver/common.py orchestrator/tests/test_solver_harness_tools.py
git commit -m "$(cat <<'EOF'
fix(recon): mode=all 只给 target 也能执行

轨迹 2405355 实测：缺 url 直接报错浪费一回合。改为从开放端口推导入口。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `solver/state.py` —— findings 提取与 prior 合成

**Files:**
- Create: `solver/state.py`
- Test: `orchestrator/tests/test_solver_state.py`（新建）

**Interfaces:**
- Produces:
  - `extract_findings(text: str) -> dict`，键固定为
    `credentials/endpoints/versions/errors`，值均为 `list[str]`。
  - `synthesize_prior(events: list[dict], candidates: list[str]) -> dict`，键
    `assets/explored/open_leads/credentials/findings`。
  - `append_findings(path: Path, findings: dict) -> None`，按 `kind: value` 去重追加。

- [ ] **Step 1: 写失败测试**

```python
"""findings 提取 —— 服务多阶段/重投的结构化状态，不靠模型记忆。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def load_state():
    sys.path.insert(0, str(SOLVER_DIR))
    sys.modules.pop("state", None)
    return importlib.import_module("state")


def test_extracts_credentials_and_endpoints():
    state = load_state()
    text = (
        '{"DATABASE_PASSWORD":"flag{not_a_real_flag}","DATABASE_USER":"dbadmin"}\n'
        "GET http://10.0.0.5:8080/api/functions/order/config\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij\n"
        "Server: nginx/1.18.0\n"
    )
    out = state.extract_findings(text)
    assert any("flag{not_a_real_flag}" in item for item in out["credentials"])
    assert any("10.0.0.5:8080" in item for item in out["endpoints"])
    assert any("nginx/1.18.0" in item for item in out["versions"])


def test_synthesize_prior_keeps_contract_keys():
    state = load_state()
    events = [
        {"text": "$ curl -i http://10.0.0.5:8080/\nHTTP/1.1 200 OK"},
        {"text": "admin:Passw0rd@10.0.0.6"},
    ]
    prior = state.synthesize_prior(events, candidates=[])
    for key in ("assets", "explored", "open_leads", "credentials", "findings"):
        assert key in prior and isinstance(prior[key], list)
    assert prior["explored"] == ["curl -i http://10.0.0.5:8080/"]
    assert any("admin:Passw0rd" in item for item in prior["credentials"])


def test_append_findings_is_deduplicated(tmp_path):
    state = load_state()
    path = tmp_path / "findings.txt"
    state.append_findings(path, {"credentials": ["admin:pass"], "endpoints": []})
    state.append_findings(path, {"credentials": ["admin:pass"], "endpoints": []})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["credentials: admin:pass"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_solver_state.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'state'`）

- [ ] **Step 3: 实现**

```python
"""结构化执行状态 —— 从工具输出提取可跨 attempt 复用的事实。

设计约束：纯正则/集合运算，零 LLM；任何失败都不抛出（调用方按增强处理）。
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_ITEMS = 50

_ASSET_RE = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9.-]+\.[a-z]{2,})(?::\d{2,5})?\b")
_URL_RE = re.compile(r"https?://[^\s\"'<>]{4,200}")
_CMD_RE = re.compile(r"^\$\s+(.{1,200})")
_CRED_RE = [
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*"
        r"[\"']?([^\s\"'<>]{3,120})"
    ),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+([A-Za-z0-9._\-]{10,})"),
    re.compile(r"\b([A-Za-z0-9._%+-]{2,32}):([^\s:@]{3,80})@"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
_VERSION_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.-]*/\d+\.\d+(?:\.\d+)?)\b")
_ERROR_RE = re.compile(
    r"(?i)\b(?:401|403|404|500|denied|forbidden|timeout|timed out|refused|error)\b"
)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def extract_findings(text: str) -> dict:
    credentials: list[str] = []
    for pattern in _CRED_RE:
        for match in pattern.finditer(text):
            if match.lastindex == 2 and ":" in match.group(0) and match.group(0).endswith("@"):
                credentials.append(f"{match.group(1)}:{match.group(2)}")
            else:
                credentials.append(match.group(1) if match.lastindex else match.group(0))
    endpoints = [match.group(0) for match in _URL_RE.finditer(text)]
    endpoints.extend(match.group(0) for match in _ASSET_RE.finditer(text.lower()))
    versions = [match.group(1) for match in _VERSION_RE.finditer(text)]
    errors = [line.strip()[:160] for line in text.splitlines() if _ERROR_RE.search(line)]
    return {
        "credentials": _dedupe(credentials),
        "endpoints": _dedupe(endpoints),
        "versions": _dedupe(versions),
        "errors": _dedupe(errors),
    }


def synthesize_prior(events: list[dict], candidates: list[str]) -> dict:
    assets: list[str] = []
    explored: list[str] = []
    leads: list[str] = []
    credentials: list[str] = []
    findings: list[str] = []
    for event in events:
        text = str(event.get("text", ""))
        if not text:
            continue
        assets.extend(match.group(0) for match in _ASSET_RE.finditer(text.lower()))
        leads.extend(match.group(0) for match in _URL_RE.finditer(text))
        for line in text.splitlines():
            match = _CMD_RE.match(line.strip())
            if match:
                explored.append(match.group(1)[:120])
        extracted = extract_findings(text)
        credentials.extend(extracted["credentials"])
        findings.extend(extracted["versions"])
    known = set(candidates)
    leads = [url for url in leads if not any(url in c or c in url for c in known)]
    return {
        "assets": _dedupe(assets),
        "explored": _dedupe(explored),
        "open_leads": _dedupe(leads),
        "credentials": _dedupe(credentials),
        "findings": _dedupe(findings),
    }


def append_findings(path: Path, findings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.is_file():
        existing = set(path.read_text(encoding="utf-8", errors="replace").splitlines())
    new_lines: list[str] = []
    for key in ("credentials", "endpoints", "versions", "errors"):
        for value in findings.get(key) or []:
            line = f"{key}: {value}"
            if line not in existing and line not in new_lines:
                new_lines.append(line)
    if new_lines:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines) + "\n")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_solver_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solver/state.py orchestrator/tests/test_solver_state.py
git commit -m "$(cat <<'EOF'
feat(state): 结构化 findings 与 prior 合成

从工具输出提取凭据/端点/版本/错误，跨 attempt 复用，不再依赖模型记忆。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: main.py 裁剪阈值 + 追加式状态落盘

**Files:**
- Modify: `solver/common.py`（新增 `TRIM_TOKENS` / `TRIM_KEEP_MSGS` / `TASK`）
- Modify: `solver/main.py:62-73, 265-280, 560-601`
- Test: `orchestrator/tests/test_solver_state_persist.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `extract_findings` / `synthesize_prior` / `append_findings`。
- Produces:
  - `common.TRIM_TOKENS`（默认 90000）、`common.TRIM_KEEP_MSGS`（默认 60）
  - `common.TASK: dict`（`setup_orch_mode` 写入）
  - `main._persist_harness_state(messages)` 追加 `out/findings.txt` 并写 `out/prior.json`

- [ ] **Step 1: 写失败测试**

```python
"""跨轮状态落盘：prior.json + findings.txt 必须携带凭据/命令。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def test_persist_writes_prior_and_findings(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps({"unique_code": "x1", "targets": ["10.0.0.1:8080"]}), encoding="utf-8"
    )
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps(
                            {"command": "curl -s http://10.0.0.1:8080/api/functions/order/config"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "a",
            "content": '{"DATABASE_PASSWORD":"flag{pwd_test}","DATABASE_USER":"dbadmin"}',
        },
    ]
    main._persist_harness_state(messages)
    prior = json.loads((tmp_path / "out" / "prior.json").read_text(encoding="utf-8"))
    assert any("curl -s http://10.0.0.1:8080" in item for item in prior["explored"])
    assert any("flag{pwd_test}" in item for item in prior["credentials"])
    assert "credentials:" in (tmp_path / "out" / "findings.txt").read_text(encoding="utf-8")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_solver_state_persist.py -v`
Expected: FAIL（`FileNotFoundError: out/prior.json`）

- [ ] **Step 3: 实现**

`common.py` 在 `OUTPUT_LIMIT` 后加：

```python
# 上下文裁剪：DeepSeek 128k 上下文下 45k 过早；长利用链需要更多历史。
TRIM_TOKENS = int(cfg("SOLVER_TRIM_TOKENS", "90000"))
TRIM_KEEP_MSGS = int(cfg("SOLVER_TRIM_KEEP_MSGS", "60"))
```

`common.setup_orch_mode` 增加 `global TASK` 并在函数开头赋值：

```python
    global TARGET_INFO, TASK_PROMPT, MAX_MINUTES, DEADLINE_EPOCH, TASK
...
    task = load_task()
    TASK = task
```

模块级初始化 `TASK: dict = {}`（放在 `TOTALS` 附近）。

`main.py` 顶部导入:

```python
import state
```

把 `_persist_harness_state` 替换为：

```python
def _messages_as_events(messages: list[dict]) -> list[dict]:
    events: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                if fn.get("name") == "run_command":
                    events.append({"text": "$ " + str(args.get("command") or "")})
                else:
                    events.append({"text": f"{fn.get('name')} {args}"})
        elif role == "tool":
            events.append({"text": str(message.get("content") or "")})
    return events


def _persist_harness_state(messages: list[dict]) -> None:
    """跨 attempt 落盘：命令摘要 + 结构化 prior + 追加式 findings。"""
    if not common.SOLVER_TASK_PATH:
        return
    try:
        out_dir = Path(common.SOLVER_TASK_PATH).parent / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "harness_state.txt").write_text(
            common.summarize_history(messages, limit=16), encoding="utf-8"
        )
        events = _messages_as_events(messages)
        prior = state.synthesize_prior(events, sorted(common._reported))
        (out_dir / "prior.json").write_text(
            json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        blob = "\n".join(event["text"] for event in events)
        state.append_findings(out_dir / "findings.txt", state.extract_findings(blob))
    except OSError:
        pass
```

裁剪条件改为：

```python
        if last_prompt_tokens > common.TRIM_TOKENS or len(messages) > 140:
            head = messages[:2]
            tail = messages[-common.TRIM_KEEP_MSGS :]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_solver_state_persist.py orchestrator/tests/test_solver_harness_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solver/common.py solver/main.py orchestrator/tests/test_solver_state_persist.py
git commit -m "$(cat <<'EOF'
feat(solver): 提高裁剪阈值并落盘结构化 prior/findings

45k 过早丢弃长利用链；每轮追加凭据/端点/命令状态供重投复用。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `gov/prior.py` 复用 `solver/state.py`

**Files:**
- Modify: `solver/gov/prior.py`
- Test: `orchestrator/tests/test_gov.py`（既有）

**Interfaces:**
- Produces: `prior.synthesize(events, candidates)` 返回与 Task 3 相同的键集合。

- [ ] **Step 1: 写失败测试**

在 `orchestrator/tests/test_gov.py` 追加：

```python
def test_prior_synthesize_includes_credentials():
    from gov import prior

    result = prior.synthesize([{"text": '{"DATABASE_PASSWORD":"flag{pwd_test}"}'}], candidates=[])
    assert "credentials" in result
    assert any("flag{pwd_test}" in item for item in result["credentials"])
```

（该文件既有 fixture 已把 `solver/` 加入 `sys.path`；若没有，测试内用
`sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solver"))`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_gov.py -k credential -v`
Expected: FAIL（`KeyError: 'credentials'`）

- [ ] **Step 3: 实现**

`gov/prior.py` 的 `synthesize` 改为薄委托：

```python
def synthesize(events: list[dict], candidates: list[str]) -> dict:
    import sys

    solver_dir = str(Path(__file__).resolve().parents[1])
    if solver_dir not in sys.path:
        sys.path.insert(0, solver_dir)
    from state import synthesize_prior

    return synthesize_prior(events, candidates)
```

删除不再使用的 `_ASSET_RE` / `_URL_RE` / `_CMD_RE` / `_dedupe` / `MAX_ITEMS`
（保留 `write` 不变）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_gov.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solver/gov/prior.py orchestrator/tests/test_gov.py
git commit -m "$(cat <<'EOF'
refactor(prior): 双内核共用 solver/state.py 的合成逻辑

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: supervisor 回收新 prior 键

**Files:**
- Modify: `orchestrator/src/orchestrator/supervisor.py:75-95, 55-73`
- Test: `orchestrator/tests/test_supervisor.py`

**Interfaces:**
- Produces: `collect_prior(ws)` 返回
  `assets/explored/open_leads/credentials/findings/history`。
- `prepare_workspace` 的默认 prior 同步包含新键。

- [ ] **Step 1: 写失败测试**

修改 `test_collect_prior_tolerates_missing_and_garbage` 的期望：

```python
    assert collect_prior(ws) == {
        "assets": [],
        "explored": [],
        "open_leads": [],
        "credentials": [],
        "findings": [],
        "history": "",
    }
```

追加：

```python
def test_collect_prior_includes_credentials_and_findings(tmp_path):
    ws = prepare_workspace(tmp_path, rec(), attempt=1)
    (ws / "out" / "prior.json").write_text(
        '{"assets": [], "credentials": ["admin:pass"], "findings": ["nginx/1.18.0"]}',
        encoding="utf-8",
    )
    prior = collect_prior(ws)
    assert prior["credentials"] == ["admin:pass"]
    assert prior["findings"] == ["nginx/1.18.0"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_supervisor.py -k prior -v`
Expected: FAIL（字典缺 `credentials`）

- [ ] **Step 3: 实现**

`collect_prior`：

```python
    prior: dict = {
        "assets": [],
        "explored": [],
        "open_leads": [],
        "credentials": [],
        "findings": [],
        "history": "",
    }
...
        for key in ("assets", "explored", "open_leads", "credentials", "findings"):
```

`prepare_workspace`：

```python
        "prior": prior
        or {
            "assets": [],
            "explored": [],
            "open_leads": [],
            "credentials": [],
            "findings": [],
        },
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/supervisor.py orchestrator/tests/test_supervisor.py
git commit -m "$(cat <<'EOF'
feat(supervisor): 回收 credentials/findings 跨轮状态

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: 合并与注入新 prior 键

**Files:**
- Modify: `orchestrator/src/orchestrator/main.py:188-207`
- Modify: `solver/common.py:580-593`
- Test: `orchestrator/tests/test_prior_merge.py`（新建）

**Interfaces:**
- Consumes: Task 6 的 prior 键。
- Produces: `_merge_prior(old, new)` 合并五键；`setup_orch_mode` 注入
  `# Confirmed credentials` / `# Findings (versions/endpoints)`。

- [ ] **Step 1: 写失败测试**

```python
"""prior 新键的合并与提示注入。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from orchestrator.main import _merge_prior

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def test_merge_prior_merges_credentials_and_findings():
    merged = _merge_prior(
        {"credentials": ["a:b"], "findings": ["nginx/1.18.0"]},
        {"credentials": ["a:b", "c:d"], "findings": ["tomcat/9.0.1"]},
    )
    assert merged["credentials"] == ["a:b", "c:d"]
    assert merged["findings"] == ["nginx/1.18.0", "tomcat/9.0.1"]


def test_setup_orch_mode_injects_new_sections(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "description": "d",
                "targets": ["10.0.0.1:80"],
                "prior": {"credentials": ["admin:pass"], "findings": ["nginx/1.18.0"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    common = importlib.import_module("common")
    common.setup_orch_mode()
    assert "# Confirmed credentials" in common.TASK_PROMPT
    assert "admin:pass" in common.TASK_PROMPT
    assert "# Findings (versions/endpoints)" in common.TASK_PROMPT
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_prior_merge.py -v`
Expected: FAIL（新键被丢弃 / prompt 无 section）

- [ ] **Step 3: 实现**

`main._merge_prior` 的初始字典与循环键：

```python
    merged = {
        "assets": [],
        "explored": [],
        "open_leads": [],
        "credentials": [],
        "findings": [],
    }
    for key in merged:
```

`common.setup_orch_mode` 的键表扩展为：

```python
    for key, title in (
        ("assets", "Confirmed assets"),
        ("explored", "Already explored (do NOT repeat)"),
        ("open_leads", "Open leads"),
        ("credentials", "Confirmed credentials"),
        ("findings", "Findings (versions/endpoints)"),
    ):
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_prior_merge.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/main.py solver/common.py orchestrator/tests/test_prior_merge.py
git commit -m "$(cat <<'EOF'
feat(prior): 合并并注入凭据/发现，服务多阶段重投

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: 调度器短片闸门

**Files:**
- Modify: `orchestrator/src/orchestrator/scheduler.py`
- Modify: `orchestrator/src/orchestrator/main.py:488-515`
- Test: `orchestrator/tests/test_registry_scheduler.py`

**Interfaces:**
- Produces: `should_start(rec, slice_s, *, has_progress=False, min_useful_s=None, gate=None) -> bool`
- 环境变量：`SHORT_SLICE_GATE`（默认 on）、`MIN_USEFUL_SLICE_S`（默认 150）。

- [ ] **Step 1: 写失败测试**

在 `test_registry_scheduler.py` 追加：

```python
def test_short_slice_gate_skips_fresh_hard_task():
    from orchestrator.scheduler import should_start

    assert should_start(rec(difficulty="hard"), 80, has_progress=False, gate=True) is False
    assert should_start(rec(difficulty="hard"), 200, has_progress=False, gate=True) is True


def test_short_slice_gate_exempts_easy_and_progress():
    from orchestrator.scheduler import should_start

    assert should_start(rec(difficulty="easy"), 80, has_progress=False, gate=True) is True
    assert should_start(rec(difficulty="hard"), 80, has_progress=True, gate=True) is True
    assert should_start(rec(difficulty="hard"), 80, has_progress=False, gate=False) is True
```

（该文件已有 `rec()` 辅助；若没有，用 `ChallengeRecord(unique_code="t")`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_registry_scheduler.py -k short_slice -v`
Expected: FAIL（`ImportError: cannot import name 'should_start'`）

- [ ] **Step 3: 实现**

`scheduler.py` 在 `can_start` 前加：

```python
MIN_USEFUL_SLICE_S = float(os.environ.get("MIN_USEFUL_SLICE_S", "150"))
SHORT_SLICE_GATE = os.environ.get("SHORT_SLICE_GATE", "on").strip().lower() in (
    "1",
    "on",
    "true",
    "yes",
)


def should_start(
    rec: ChallengeRecord,
    slice_s: float,
    *,
    has_progress: bool = False,
    min_useful_s: float | None = None,
    gate: bool | None = None,
) -> bool:
    """尾段短片闸门：无进展的新题/盲重投不值得启动（只能被 recon 吃光）。"""
    min_useful = MIN_USEFUL_SLICE_S if min_useful_s is None else min_useful_s
    enabled = SHORT_SLICE_GATE if gate is None else gate
    if not enabled or slice_s >= min_useful:
        return True
    if rec.difficulty == "easy":
        return True
    return has_progress
```

`main.py` 导入：

```python
from .scheduler import can_start, eligible, plan_phase, should_start, slice_for
```

主循环创建 task 前加：

```python
prior = ctx.priors.get(rec.unique_code) or {}
has_progress = any(
    prior.get(key) for key in ("assets", "explored", "open_leads", "credentials", "findings")
)
if not should_start(rec, attempt_slice, has_progress=has_progress):
    rec.next_slice_s = attempt_slice  # 保留加权片，下次再试
    continue
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_registry_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/scheduler.py orchestrator/src/orchestrator/main.py orchestrator/tests/test_registry_scheduler.py
git commit -m "$(cat <<'EOF'
feat(scheduler): 短片闸门，停止无进展新题/盲重投

轨迹显示 85s 片只剩一次 recon；把尾段时间留给有资产的题。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: 契约中的 ATTACK LOOP

**Files:**
- Modify: `solver/common.py:540-558`
- Test: `orchestrator/tests/test_solver_harness_tools.py`

**Interfaces:**
- Produces: `build_system_prompt()` 含 `# ATTACK LOOP` 段。

- [ ] **Step 1: 写失败测试**

```python
def test_prompt_contains_attack_loop(common):
    prompt = common.build_system_prompt()
    assert "# ATTACK LOOP" in prompt
    assert "reuse" in prompt.lower()
    assert "flag" in prompt.lower()
    assert len(prompt) < 2800
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_solver_harness_tools.py -k attack_loop -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `common.build_system_prompt` 的 contract 字符串末尾、三引号结束前加：

```text
# ATTACK LOOP
- New fingerprint or credential: try the fastest viable exploit first, then widen recon.
- Any credential/token in output: immediately reuse it on other discovered services and endpoints.
- Same vector fails twice: switch to the next concrete candidate; never repeat the same request shape.
- Multi-flag: every flag is a new starting point — re-enumerate what the current assets can reach.
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_solver_harness_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solver/common.py orchestrator/tests/test_solver_harness_tools.py
git commit -m "$(cat <<'EOF'
feat(prompt): 通用 ATTACK LOOP（利用优先/凭据复用/失败切换）

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: 多阶段与利用链 KB cheatsheet

**Files:**
- Create: `solver/kb/exploit_chains.md`
- Create: `solver/kb/multi_stage.md`

**Interfaces:**
- Consumes: 无。
- Produces: 两个可被 `kb.index` 检索的 `## ` 小节文档。

- [ ] **Step 1: 写内容**

`exploit_chains.md`：

```markdown
# Exploit Chains

## Web auth bypass then lateral
1. Enumerate every object id and role boundary; try missing authorization on admin endpoints.
2. Reuse session cookies/tokens across sibling services and hostnames found in JS/config.
3. If an upload or template injection lands, use it for file read first, command execution second.

## Leaked credential reuse
1. Treat every password/token as a candidate for all discovered services (SSH, DB, HTTP basic, API).
2. Try the exact value first, then common transforms only when the source implies them.
3. Record each successful credential with the service that accepted it.

## Binary primitive to exploitation
1. Confirm protections (checksec) and the exact input-to-corruption offset.
2. Build the leak stage first; verify it locally before sending to the remote target.
3. Keep the exploit scripted; one payload per run, log the response bytes.
```

`multi_stage.md`：

```markdown
# Multi-Stage Workflow

## State ledger
1. After every stage, write down: assets, credentials, accepted endpoints, failed vectors.
2. Never restart from zero: feed the ledger into the next stage's first command.

## Pivot discipline
1. A flag is a pivot, not the end. Re-run service discovery from the current foothold.
2. Test the newest credential against every previously discovered endpoint before new recon.
3. If two stages fail on the same surface, change surface: another host, port, or role.

## Time budget
1. Short slice: one high-information command at a time; skip broad directory sweeps.
2. Long slice: after a foothold, spend time on lateral reachability, not on re-fingerprinting.
```

- [ ] **Step 2: 验证索引可检索**

Run: `uv run python solver/kb/index.py query "credential reuse lateral" 3`
Expected: JSON 输出包含 `exploit_chains` 或 `multi_stage` 小节。

- [ ] **Step 3: Commit**

```bash
git add solver/kb/exploit_chains.md solver/kb/multi_stage.md
git commit -m "$(cat <<'EOF'
docs(kb): 通用利用链与多阶段 cheatsheet

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: 多 flag 发现后的结构化提示

**Files:**
- Modify: `solver/main.py`
- Test: `orchestrator/tests/test_solver_state_persist.py`

**Interfaces:**
- Produces: `main._flag_nudge(common_mod, reported_before: int) -> str | None`

- [ ] **Step 1: 写失败测试**

在 `test_solver_state_persist.py` 追加：

```python
def test_flag_nudge_only_when_new_flag_and_flags_remain(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"flag_count": 3, "correct_flag_count": 0}), encoding="utf-8")
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    import common

    common.TASK = {"flag_count": 3, "correct_flag_count": 0}
    monkeypatch.setattr(common, "_reported", {"FLAG{a}"})
    assert "FLAG{a}" not in (main._flag_nudge(common, 0) or "")
    assert "2" in (main._flag_nudge(common, 0) or "")
    assert main._flag_nudge(common, 1) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_solver_state_persist.py -k flag_nudge -v`
Expected: FAIL（`AttributeError: module 'main' has no attribute '_flag_nudge'`）

- [ ] **Step 3: 实现**

在 `main.py` 的 `_persist_harness_state` 后加：

```python
def _flag_nudge(common_mod, reported_before: int) -> str | None:
    task = getattr(common_mod, "TASK", {}) or {}
    remaining = int(task.get("flag_count", 1)) - int(task.get("correct_flag_count", 0))
    new_flags = len(common_mod._reported) - reported_before
    if remaining <= 0 or new_flags <= 0:
        return None
    return (
        f"{new_flags} new flag(s) found; {remaining} still outstanding. "
        "Reuse confirmed credentials against every discovered service, and treat "
        "this flag as a new pivot. Do not stop."
    )
```

主循环中，在每次 LLM 调用前记录：

```python
        reported_before = len(common._reported)
```

在整组 tool 结果追加完后：

```python
        nudge = _flag_nudge(common, reported_before)
        if nudge:
            messages.append({"role": "user", "content": nudge})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_solver_state_persist.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solver/main.py orchestrator/tests/test_solver_state_persist.py
git commit -m "$(cat <<'EOF'
feat(solver): 多 flag 进度提示（凭据复用/继续 pivot）

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: StagePlanner

**Files:**
- Modify: `orchestrator/src/orchestrator/v2_controller.py`
- Test: `orchestrator/tests/test_v2_controller.py`

**Interfaces:**
- Produces:
  - `STAGE_DIRECTIVES: dict[str, str]`
  - `StagePlanner(*, stages=("recon","probe","exploit","verify"), directives=None, multi_flag=False)`
  - `StagePlanner.next_action(snapshot) -> Delegate | Finish`

- [ ] **Step 1: 写失败测试**

在 `test_v2_controller.py` 追加：

```python
def test_stage_planner_orders_stages_and_skips_exploring():
    from orchestrator.v2_bridge import Delegate, Finish
    from orchestrator.v2_controller import StagePlanner
    from orchestrator.v2_state import NodeState

    planner = StagePlanner()
    first = planner.next_action({})
    assert isinstance(first, Delegate) and first.key == "recon" and first.parent is None
    snapshot = {
        "recon": {"state": NodeState.EXPLORED, "parent": None},
        "probe": {"state": NodeState.EXPLORING, "parent": "recon"},
    }
    action = planner.next_action(snapshot)
    assert isinstance(action, Delegate) and action.key == "exploit"
    assert action.parent == "probe"
    done = {key: {"state": NodeState.EXPLORED, "parent": None} for key in planner.stages}
    assert isinstance(planner.next_action(done), Finish)


def test_stage_planner_adds_pivot_for_multi_flag():
    from orchestrator.v2_controller import StagePlanner

    planner = StagePlanner(multi_flag=True)
    assert planner.stages[-1] == "pivot"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_v2_controller.py -k stage_planner -v`
Expected: FAIL（`ImportError: cannot import name 'StagePlanner'`）

- [ ] **Step 3: 实现**

在 `v2_controller.py` 的 `StubPlanner` 后加：

```python
STAGE_DIRECTIVES = {
    "recon": "Map every reachable service, endpoint and technology; record assets.",
    "probe": "Test each discovered surface against likely weakness classes; pick the strongest lead.",
    "exploit": "Weaponize the strongest lead into a working exploit and collect the flag.",
    "verify": "Confirm the flag exactly as found; do not invent or transform it.",
    "pivot": "Reuse confirmed credentials/assets to reach the next stage; find remaining flags.",
}


class StagePlanner:
    """确定性阶段 planner：按 recon→probe→exploit→verify(→pivot) 顺序委派。"""

    def __init__(
        self,
        *,
        stages: tuple[str, ...] = ("recon", "probe", "exploit", "verify"),
        directives: dict[str, str] | None = None,
        multi_flag: bool = False,
    ) -> None:
        self.stages = list(stages) + (["pivot"] if multi_flag else [])
        self.directives = {**STAGE_DIRECTIVES, **(directives or {})}

    def next_action(self, snapshot: dict) -> Delegate | Finish:
        previous: str | None = None
        for key in self.stages:
            info = snapshot.get(key)
            if info is None:
                return Delegate(key=key, directive=self.directives.get(key, ""), parent=previous)
            if info["state"].value == "UNEXPLORED":
                return Delegate(key=key, directive=self.directives.get(key, ""), parent=previous)
            previous = key
        return Finish("all stages explored")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_v2_controller.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/v2_controller.py orchestrator/tests/test_v2_controller.py
git commit -m "$(cat <<'EOF'
feat(v2): 确定性 StagePlanner（recon/probe/exploit/verify/pivot）

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: HARNESS_V2 接线 + 节点 workspace 状态传递

**Files:**
- Modify: `orchestrator/src/orchestrator/v2_controller.py`（`make_stream_runner`）
- Modify: `orchestrator/src/orchestrator/main.py`
- Test: `orchestrator/tests/test_v2_wiring.py`（新建）

**Interfaces:**
- Consumes: Task 12 的 `StagePlanner`、Task 6 的 `collect_prior`。
- Produces: `HARNESS_V2=on` 时 `_pipeline_inner` 走图探索；节点 task.json、
  flags 与 prior 回传父 workspace。

- [ ] **Step 1: 写失败测试**

```python
"""HARNESS_V2 节点 runner：task.json 继承、flag 回传、prior 累积。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from orchestrator.registry import ChallengeRecord
from orchestrator.supervisor import prepare_workspace
from orchestrator.v2_controller import make_stream_runner


class FakeClient:
    async def submit(self, code, flag):
        return {"matched_flag_index": 0, "total_flag_count": 1, "correct_flag_count": 1}


class FakeJournal:
    def append(self, *args, **kwargs):
        pass


def test_node_runner_inherits_task_and_returns_flags(tmp_path):
    rec = ChallengeRecord(unique_code="t1", container_addr=["10.0.0.1:80"])
    ws = prepare_workspace(tmp_path, rec, attempt=1)
    solver = tmp_path / "fake_solver.py"
    solver.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "ws = Path(os.environ['SOLVER_TASK']).parent\n"
        "(ws / 'out' / 'flags').mkdir(parents=True, exist_ok=True)\n"
        "(ws / 'out' / 'flags' / '000.txt').write_text('FLAG{node_ok}\\n')\n",
        encoding="utf-8",
    )
    runner = make_stream_runner(
        solver_cmd=[sys.executable, str(solver)],
        base_environ={"PATH": os.environ["PATH"]},
        slice_s=10,
        ws=ws,
        client=FakeClient(),
        journal=FakeJournal(),
        rec=rec,
        deadline_epoch=__import__("time").time() + 60,
    )
    text, code, ok = asyncio.run(runner(key="recon", directive="map it"))
    assert code == 0 and ok
    node_task = json.loads((ws / "node-recon" / "task.json").read_text(encoding="utf-8"))
    assert "# NODE FOCUS" in node_task["description"]
    assert (ws / "out" / "flags" / "recon_000.txt").read_text(
        encoding="utf-8"
    ).strip() == "FLAG{node_ok}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest orchestrator/tests/test_v2_wiring.py -v`
Expected: FAIL（节点目录无 task.json / flag 未回传）

- [ ] **Step 3: 实现**

`v2_controller.make_stream_runner` 的 runner 内，在 `node_dir.mkdir(...)` 后加入：

```python
        (node_dir / "out" / "flags").mkdir(parents=True, exist_ok=True)
        task: dict = {}
        parent_task = ws / "task.json"
        try:
            task = json.loads(parent_task.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            task = {}
        task = dict(task)
        if directive:
            task["description"] = (
                str(task.get("description") or "") + f"\n\n# NODE FOCUS\n{directive}"
            ).strip()
        accum = ws / "out" / "v2_prior.json"
        try:
            task["prior"] = json.loads(accum.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        (node_dir / "task.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
```

在 `run_solver_with_stream` 返回后、`return` 前加入：

```python
        dest = ws / "out" / "flags"
        dest.mkdir(parents=True, exist_ok=True)
        for flag_file in (node_dir / "out" / "flags").glob("*.txt"):
            (dest / f"{key}_{flag_file.name}").write_text(
                flag_file.read_text(encoding="utf-8"), encoding="utf-8"
            )
        from .supervisor import collect_prior

        prior_path = ws / "out" / "v2_prior.json"
        old: dict = {}
        try:
            old = json.loads(prior_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        new = collect_prior(node_dir)
        merged: dict = {}
        for field in ("assets", "explored", "open_leads", "credentials", "findings"):
            values = list(old.get(field) or []) + list(new.get(field) or [])
            merged[field] = list(dict.fromkeys(str(value) for value in values))
        prior_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
```

在文件顶部补 `import json`（若尚未导入）。

`main.py` 导入：

```python
from . import config as config_module
```

（放在现有 `from .config import RuntimeConfig, resolve_config` 旁。）

`_pipeline_inner` 中，把现有那段 `outcome, early_new, early_completed = await streams...`
替换为分支：

```python
    if config_module.HARNESS_V2:
        from types import SimpleNamespace

        from .v2_controller import StagePlanner, V2Controller

        v2_started = time.monotonic()
        planner = StagePlanner(multi_flag=rec.flag_count > rec.correct_flag_count)
        controller = V2Controller(
            runner=None,
            max_parallel=min(3, ctx.config.concurrency_limit),
            node_timeout_s=max(60.0, slice_s),
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
        outcome = SimpleNamespace(
            exit_code=0, timed_out=False, duration_s=time.monotonic() - v2_started
        )
        early_new, early_completed = 0, False
    else:
        outcome, early_new, early_completed = await streams.run_solver_with_stream(
            solver_cmd=ctx.solver_cmd,
            ws=ws,
            deadline_epoch=deadline_epoch,
            slice_s=slice_s,
            environ=solver_env,
            client=ctx.client,
            journal=ctx.journal,
            unique_code=code,
            rec=rec,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest orchestrator/tests/test_v2_wiring.py orchestrator/tests/test_v2_e2e.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/v2_controller.py orchestrator/src/orchestrator/main.py orchestrator/tests/test_v2_wiring.py
git commit -m "$(cat <<'EOF'
feat(v2): HARNESS_V2 接入 StagePlanner 图探索（默认 off）

节点继承 task.json、回传 flags、累积 prior；v1 路径不变。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: 全量回归与 selfbench

**Files:**
- 无新增；验证 `make lint`、`make test`、`scripts/selfbench.sh`。

- [ ] **Step 1: lint**

Run: `make lint`
Expected: `ruff check` 与 `ruff format --check` 均通过。

- [ ] **Step 2: 单测**

Run: `make test`
Expected: 全绿（新增用例计入）。

- [ ] **Step 3: mock 端到端**

Run: `MAX_ITERS=1 TOTAL_TIME_LIMIT=180 scripts/selfbench.sh`
Expected: `results.log` 出现满分/无回归；若 Docker daemon 未运行，记录为环境限制而非代码失败。

- [ ] **Step 4: 记录结果**

把 lint/test/selfbench 的实际输出摘要写入 `docs/delivery-2026-10-17.md` 的“优化沉淀”节（只写结论与数字，不写 flag/token）。

- [ ] **Step 5: Commit**

```bash
git add docs/delivery-2026-10-17.md
git commit -m "$(cat <<'EOF'
test: 能力优化全量回归（lint/test/selfbench）

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage**

| Spec 项 | Task |
|---|---|
| A1 recon 预算/URL/KB | 1, 2 |
| A2 trim + findings/prior | 3, 4, 5 |
| A3 supervisor/merge/inject | 6, 7 |
| A4 调度短片闸门 | 8 |
| B1 ATTACK LOOP | 9 |
| B2 KB cheatsheet | 10 |
| B3 多 flag 提示 | 11 |
| C StagePlanner + 接线 | 12, 13 |
| 回归 | 14 |

**Placeholder scan:** 无 TBD/TODO；每个代码步骤含实际实现。

**Type consistency:** `extract_findings` 四键、`synthesize_prior` 五键、`collect_prior`
六键（含 history）在 Task 3/4/5/6/7 一致；`StagePlanner.stages` 在 Task 12/13 一致。
