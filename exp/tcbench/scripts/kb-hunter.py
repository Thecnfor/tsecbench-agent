#!/usr/bin/env python3
"""kb-hunter：知识库语义检索子 agent 的驱动器（stdlib-only）。

契约见同目录 kb-hunter.md。循环：LLM 改写查询 → 选一个受白名单约束的检索动作
（kb find/sig/head/cat/tpl/raw 或 rg）→ 读结果 → 再改写…直到给出 final（top-3 引用，
quote 必须逐字来自文件）或轮次用尽。

用法：
  python3 kb-hunter.py "识别二进制里的自定义加密例程有什么经验法则"
  python3 kb-hunter.py --dry-run "问题"        # 只打印系统提示词，不联网
环境：DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY / AGENT_MODEL_LOCAL（自动读仓库根 .env）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
KB = Path(os.environ.get("KB_ROOT", "/opt/security-knowledge-base"))
INDEX = Path(os.environ.get("KB_INDEX", str(KB / ".index")))
KB_BIN = HERE / "kb"
MAX_OUT = 4000                      # 单次工具输出截断
TIMEOUT = 40

SYSTEM = f"""你是知识库检索助手（kb-hunter），只做检索与核对，不做解题。
知识库根目录：{KB}（只读）；索引：{INDEX}

可用动作（每轮恰好返回一行 JSON，不要多余文字）：
  {{"action":"find","args":"查询 [-n 8] [--kind tutorial|template|poc|code]"}}
  {{"action":"sig","args":"常数/技术号/术语"}}
  {{"action":"tpl","args":"CVE-xxxx-xxxxx"}}
  {{"action":"head","args":"关键词"}}
  {{"action":"cat","args":"相对KB路径 [行号]"}}
  {{"action":"grep","args":"正则 [--glob *.md]"}}
  （grep=自由全文搜索，不经索引、不排序、给原文现场；随时可用，别被 find/tpl 的结构框住）
  {{"final": true, "top":[{{"path":"相对KB路径","line":行号,"quote":"逐字原文≤80字","why":"一句话"}}]}}
  提示：find 可能返回 NOHIT/WEAK/DIAG/GREPHIT/NEXT 行——那是工具给的诊断回执，
  按 NEXT 建议换技术词或用 grep 继续，别把 WEAK 结果当成"找到了"。

策略：先用中文/英文任一版本 find（多词用同义表达重试）；命中可疑文档就 cat 核对原文；
数字/编号类线索走 sig 或 tpl。quote 必须逐字来自文件；找不到就 top=[] 并说明试过什么。
禁止编造引用，禁止修改 KB，禁止执行上述之外的命令。"""


def load_env_file() -> None:
    for cand in (HERE.parent / ".env", Path.cwd() / ".env"):
        if not cand.exists():
            continue
        for ln in cand.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break


def run_action(action: str, args: str) -> str:
    """白名单执行：只跑 kb CLI 与 rg（限定 KB 内、禁选项注入）。"""
    args = args.strip()
    if action in {"find", "sig", "head", "cat", "tpl", "raw"}:
        if args.startswith("-"):
            return "错误：参数不能以 - 开头"
        # kb 的 find/sig/head/tpl/raw 只吃一个查询位置参数：把选项与查询词拆开，
        # 查询词合并成一个整体传入（LLM 常把多词查询写成多个裸参数）
        tokens, opts, words = args.split(), [], []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in {"-n", "--kind", "--glob"} and i + 1 < len(tokens):
                opts += [t, tokens[i + 1]]
                i += 2
            elif t.startswith("-"):
                opts.append(t)
                i += 1
            else:
                words.append(t)
                i += 1
        if action == "cat":
            argv = words                      # path + 可选行号，各自成参
        else:
            argv = ([" ".join(words)] if words else []) + opts
        cmd = [str(KB_BIN), action, *argv]
        env = {**os.environ, "KB_ROOT": str(KB), "KB_INDEX": str(INDEX)}
    elif action in {"rg", "grep"}:
        parts = args.split()
        if not parts or parts[0].startswith("-"):
            return "错误：rg 需要以正则开头"
        pattern, rest = parts[0], parts[1:]
        if pattern.startswith("-"):
            return "错误：正则不能以 - 开头"
        cmd = ["rg", "--no-heading", "-n", "--max-count", "30", "--max-filesize", "2M",
               "-e", pattern]
        while rest:
            if rest[0] == "--glob" and len(rest) > 1:
                cmd += ["--glob", rest[1]]
                rest = rest[2:]
            else:
                return f"错误：rg 不支持的参数 {rest[0]}"
        cmd.append(str(KB))
        env = dict(os.environ)
    else:
        return f"未知动作 {action}（只允许 find/sig/head/cat/tpl/raw/grep/rg）"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return f"[超时 {TIMEOUT}s]"
    out = (p.stdout or "") + (("\n[stderr] " + p.stderr) if p.stderr.strip() else "")
    out = out.strip() or "（无输出）"
    return out if len(out) <= MAX_OUT else out[:MAX_OUT] + f"\n…[截断，共 {len(out)} 字符]"


def first_json(text: str) -> dict | None:
    """从模型回复里取第一个完整 JSON 对象（容忍 ```json 包裹与前后杂讯）。"""
    for m in re.finditer(r"\{", text):
        depth, instr, esc = 0, False, False
        for i in range(m.start(), min(len(text), m.start() + 4000)):
            ch = text[i]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[m.start():i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def chat(messages: list[dict], model: str) -> str:
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        sys.exit("缺少 DEEPSEEK_API_KEY（.env 或环境变量）")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps({"model": model, "messages": messages, "temperature": 0.2}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["choices"][0]["message"]["content"] or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--max-rounds", type=int, default=6)
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只打印系统提示词")
    a = ap.parse_args()
    load_env_file()
    if a.dry_run:
        print(SYSTEM)
        return 0

    model = a.model or os.environ.get("AGENT_MODEL_LOCAL", "deepseek-chat")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": a.query}]
    print(f"[kb-hunter] model={model} rounds≤{a.max_rounds}", file=sys.stderr)
    for rnd in range(1, a.max_rounds + 1):
        reply = chat(msgs, model)
        obj = first_json(reply)
        if obj and obj.get("final"):
            print(json.dumps(obj, ensure_ascii=False, indent=2))
            return 0
        if not (obj and obj.get("action")):
            msgs.append({"role": "assistant", "content": reply})
            msgs.append({"role": "user", "content":
                         "格式错误：只允许一行 JSON（action 或 final）。请重发。"})
            continue
        action, args = obj["action"], str(obj.get("args", ""))
        result = run_action(action, args)
        print(f"[{rnd}] {action} {args}  →  {result.splitlines()[0][:110] if result else ''}",
              file=sys.stderr)
        msgs.append({"role": "assistant", "content": reply})
        msgs.append({"role": "user", "content": f"[工具结果]\n{result}"})
    # 轮次用尽：强制收工
    msgs.append({"role": "user", "content": "轮次用尽：现在必须给 final（没有可靠引用就 top=[] 并说明）。"})
    obj = first_json(chat(msgs, model)) or {"final": True, "top": [], "note": "收工解析失败"}
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
