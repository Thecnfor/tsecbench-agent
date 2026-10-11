#!/usr/bin/env python3
"""Pi 适配器薄壳（v1 实验，refs/OpenHands 的 ACP 文档定位到的正主）。

契约与 solver/main.py 编排器模式一致：读 SOLVER_TASK → 用
@earendil-works/pi-coding-agent headless 解题 → 候选写 out/flags/*.txt。
切换方式：SOLVER_CMD 指向本文件（编排器 config.solver_cmd.split()）。

复用 main.py 的：task.json 解析/prompt 组装（setup_orch_mode）、flag 检测
（find_flags）、落盘（report_candidate/write_result）—— 适配器只负责「跑 Pi」。

Pi 侧配置（全部进程内生成到临时 agent dir，不污染 ~/.pi）：
  PI_CODING_AGENT_DIR=<tmp>/   models.json（deepseek openai-completions + $LLM_API_KEY）
  APPEND_SYSTEM.md              复用 main.py 的执行契约（本地模式变体）
  pi -p --approve --no-session --no-context-files --no-skills --no-mcp
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PI_PKG = "@earendil-works/pi-coding-agent@1.1.0"


def load_solver_main():
    """以文件路径加载 solver/main.py（solver 非安装包），复用其契约逻辑。"""
    spec = importlib.util.spec_from_file_location("solver_main", HERE / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_agent_dir(smain, agent_dir: Path) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "deepseek": {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": "$LLM_API_KEY",
                        "models": [{"id": os.environ.get("LLM_MODEL", "deepseek-chat")}],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 契约走 APPEND_SYSTEM.md（user 指令位），不覆盖 Pi 默认系统提示
    (agent_dir / "APPEND_SYSTEM.md").write_text(smain.build_system_prompt(), encoding="utf-8")


def main() -> int:
    if not os.environ.get("SOLVER_TASK"):
        print("[pi-adapter] FATAL: SOLVER_TASK not set", file=sys.stderr)
        return 2
    smain = load_solver_main()
    smain.setup_orch_mode()  # 组装 TASK_PROMPT / TARGET_INFO / 时间片

    deadline_epoch = os.environ.get("SOLVER_DEADLINE_EPOCH")
    budget_s = smain.MAX_MINUTES * 60
    if deadline_epoch:
        budget_s = max(60.0, min(budget_s, float(deadline_epoch) - time.time()))

    prompt = (
        "Solve the pentest challenge described in your instructions now. "
        + (f"Target: {smain.TARGET_INFO}. " if smain.TARGET_INFO else "")
        + "Work autonomously; when you have verified the flag, output it exactly "
        "(e.g. FLAG{...}) as your final answer and stop."
    )

    with tempfile.TemporaryDirectory(prefix="pi-agent-") as tmp:
        agent_dir = Path(tmp)
        build_agent_dir(smain, agent_dir)
        cmd = [
            "npx",
            "-y",
            PI_PKG,
            "-p",
            "--approve",
            "--no-session",
            "--no-context-files",
            "--no-skills",
            "--no-mcp",
            "--model",
            f"deepseek/{os.environ.get('LLM_MODEL', 'deepseek-chat')}",
            "--",
            prompt,
        ]
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)}
        print(
            f"[pi-adapter] model=deepseek budget_s={int(budget_s)} target={smain.TARGET_INFO}",
            flush=True,
        )
        started = time.time()
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=budget_s + 30)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            print(f"[pi-adapter] killed at budget {int(budget_s)}s", flush=True)

    if out:
        print(out, flush=True)
    for flag in smain.find_flags(out or "", include_generic=True):
        smain.report_candidate(flag)
    smain.write_result("solved" if smain._reported else "failed")
    print(
        f"[pi-adapter] finished exit={proc.returncode} elapsed={int(time.time() - started)}s "
        f"candidates={sorted(smain._reported)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
