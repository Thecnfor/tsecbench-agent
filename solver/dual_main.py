#!/usr/bin/env python3
"""Dual-kernel solver 适配器（治理薄版 v0）。

双内核耦合（.trae/documents/dual-kernel-solver-plan.md）：
  治理层（本文件，v0 = 动态提示词注入的雏形）：
    读 SOLVER_TASK → setup_orch_mode 组装契约/目标/prior/hint →
    APPEND_SYSTEM.md 进程内注入（不覆盖 pi 默认系统提示）
  执行层：@earendil-works/pi-coding-agent headless 单发解题

契约与 main.py 完全一致（solver/common.py 单一真值源）：
  out/flags/NNN.txt + out/result.json，deadline 自 SOLVER_DEADLINE_EPOCH，
  进程组整杀，正常结束恒 exit 0。

可测性钩子：SOLVER_PI_CMD（命中 supervisor 白名单 SOLVER_ 前缀，config 无需改）。
设置后整条 pi 命令被替换为 `shlex.split(SOLVER_PI_CMD) + [prompt]`，
测试用假 pi stub 注入 flag 输出即可离线走通全链路。

pi 侧配置（全部进程内生成到临时 agent dir，不污染 ~/.pi）：
  PI_CODING_AGENT_DIR=<tmp>/   models.json（deepseek openai-completions + $LLM_API_KEY）
  APPEND_SYSTEM.md              common.build_system_prompt()（契约 + 动态注入）
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common  # noqa: E402  （solver 非安装包，脚本目录入 sys.path 后导入）
from gov import planner, prior, profiles  # noqa: E402
from gov import session as gov_session  # noqa: E402
from kb.index import Index as KBIndex  # noqa: E402

# 版本唯一真值源在 toolchain.env（PI_PACKAGE / PI_VERSION）；此处默认值仅是
# 本地直跑（未 source toolchain.env）时的兜底。
PI_PACKAGE = os.environ.get("PI_PACKAGE", "@earendil-works/pi-coding-agent")
PI_VERSION = os.environ.get("PI_VERSION", "1.1.0")


def install_extensions(agent_dir: Path) -> None:
    """部署 pi 扩展（recon 工具）：jiti 从 <agentDir>/extensions/ 发现已验证（§1.6）。"""
    ext_dir = agent_dir / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    src = (HERE / "pi_ext" / "recon.ts").read_text(encoding="utf-8")
    (ext_dir / "recon.ts").write_text(
        src.replace("__RECON_RECIPE__", str(HERE / "recon_recipe.py")), encoding="utf-8"
    )


def install_skills(agent_dir: Path) -> None:
    """把 kb/ cheatsheet 生成 pi skills（SKILL.md frontmatter 格式，渐进披露：
    description 常驻可检索，正文按需加载，不占初始上下文）。"""
    skills_dir = agent_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for md in sorted((HERE / "kb").glob("*.md")):
        body = md.read_text(encoding="utf-8", errors="replace")
        stem = md.stem
        # 描述取首个 '## ' 小节标题拼接（截 512 字符内，pi 上限 1024）
        heads = [ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("## ")]
        joined = "; ".join(heads)[:500]
        desc = f"Use when solving: {joined}" if joined else f"cheatsheet {stem}"
        skill_dir = skills_dir / stem
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_dir.joinpath("SKILL.md").write_text(
            f"---\nname: {stem}\ndescription: {desc}\n"
            "disable-model-invocation: false\n---\n\n" + body,
            encoding="utf-8",
        )


def kb_snippets(task_text: str, top_k: int = 2) -> str:
    """治理层知识检索：task 关键词 → kb top-k 片段（APPEND_SYSTEM 注入）。"""
    try:
        hits = KBIndex().query(task_text, top_k=top_k)
    except Exception:
        return ""
    if not hits:
        return ""
    parts = [f"## KB: {h['doc']}/{h['section']}\n{h['body']}" for h in hits]
    return "# KNOWLEDGE (offline cheatsheet hits for this task)\n" + "\n\n".join(parts)


def build_agent_dir(agent_dir: Path, strategy: str = "") -> None:
    """进程内生成 pi agent dir：models.json + APPEND_SYSTEM.md（动态注入位）。

    strategy 为治理层产物（profile 策略包 + planner 要点），追加在契约之后。
    """
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
    append = common.build_system_prompt()
    append += (
        "\n\n# TOOLING\n"
        "- A `recon` tool is available: it runs a deterministic recipe (nmap top-ports, "
        "HTTP headers/title/tech fingerprint, directory enumeration) and returns compact "
        "JSON. PREFER `recon` over hand-typing nmap/gobuster for first-pass recon."
    )
    if strategy:
        append += "\n\n" + strategy
    (agent_dir / "APPEND_SYSTEM.md").write_text(append, encoding="utf-8")
    # 注入快照（debug-ui 观测台展示「动态提示词注入」用；失败不影响解题）
    try:
        out_dir = Path(common.SOLVER_TASK_PATH).parent / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "append_system.md").write_text(append, encoding="utf-8")
    except OSError:
        pass


def build_pi_cmd(prompt: str) -> list[str]:
    """SOLVER_PI_CMD 测试钩子优先；其次 PI_BIN（镜像内 vendored pi，离线、禁 npx）；
    兜底 npx（本地直跑场景）。"""
    override = os.environ.get("SOLVER_PI_CMD", "").strip()
    if override:
        return [*shlex.split(override), prompt]
    flags = [
        "-p",
        "--approve",
        "--no-session",
        "--no-context-files",
        "--no-mcp",
        "--model",
        f"deepseek/{os.environ.get('LLM_MODEL', 'deepseek-chat')}",
        "--",
        prompt,
    ]
    pi_bin = os.environ.get("PI_BIN", "").strip()
    if pi_bin:
        return [pi_bin, *flags]
    return ["npx", "-y", f"{PI_PACKAGE}@{PI_VERSION}", *flags]


def main() -> int:
    if not os.environ.get("SOLVER_TASK"):
        print("[dual-solver] FATAL: SOLVER_TASK not set", file=sys.stderr)
        return 2
    common.setup_orch_mode()  # 组装 TASK_PROMPT / TARGET_INFO / 时间片
    workspace = Path(common.SOLVER_TASK_PATH).parent
    out_dir = workspace / "out"
    sess = gov_session.Session(out_dir)
    sess.log("meta", solver="dual_main", model=os.environ.get("LLM_MODEL", "deepseek-chat"))

    deadline_epoch = os.environ.get("SOLVER_DEADLINE_EPOCH")
    budget_s = common.MAX_MINUTES * 60
    if deadline_epoch:
        budget_s = max(60.0, min(budget_s, float(deadline_epoch) - time.time()))

    # 治理层：profile 选择（确定性）+ planner 规划（全程唯一一次额外 LLM 调用）
    task = common.load_task()
    category, strategy = profiles.select(task)
    sess.log("profile", category=category)
    decision = planner.plan(task, deadline_epoch)
    if decision:
        sess.log("plan", category=decision.get("category"), strategy=decision.get("strategy"))
        if decision.get("strategy"):
            strategy += (
                f"\n\n# PLANNER (category={decision.get('category') or category})\n"
                + (decision["strategy"])
            )
    # 治理层知识检索（离线 kb，零 LLM）
    task_text = " ".join(
        str(x) for x in (task.get("description") or "", task.get("hint") or "", common.TARGET_INFO)
    )
    knowledge = kb_snippets(task_text)
    if knowledge:
        sess.log("kb", sections=len(knowledge.split("## KB:")) - 1)
        strategy += "\n\n" + knowledge

    prompt = (
        "Solve the pentest challenge described in your instructions now. "
        + (f"Target: {common.TARGET_INFO}. " if common.TARGET_INFO else "")
        + "Work autonomously; when you have verified the flag, output it exactly "
        "(e.g. FLAG{...}) as your final answer and stop."
    )

    with tempfile.TemporaryDirectory(prefix="pi-agent-") as tmp:
        agent_dir = Path(tmp)
        build_agent_dir(agent_dir, strategy)
        install_extensions(agent_dir)
        install_skills(agent_dir)
        cmd = build_pi_cmd(prompt)
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)}
        print(
            f"[dual-solver] exec={cmd[0]} budget_s={int(budget_s)} target={common.TARGET_INFO}",
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
            print(f"[dual-solver] killed at budget {int(budget_s)}s", flush=True)

    if out:
        print(out, flush=True)
        # 事件流整批入 session（一行一事件，供 prior 合成与复盘）
        for line in out.splitlines():
            if line.strip():
                sess.log("pi_stdout", text=line[:500])
    flags = common.find_flags(out or "", include_generic=True)
    for flag in flags:
        common.report_candidate(flag)
        sess.log("candidate", flag=flag)
    common.write_result("solved" if common._reported else "failed")
    prior.write(out_dir, sess.lines(), sorted(common._reported))
    sess.log("done", exit_code=proc.returncode)
    sess.close()
    print(
        f"[dual-solver] finished exit={proc.returncode} elapsed={int(time.time() - started)}s "
        f"candidates={sorted(common._reported)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
