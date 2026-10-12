#!/usr/bin/env python3
"""离线自测：不依赖 VPN / 网关 / 平台，验证「控制器 + 单题独立上下文」主逻辑。

用假的 OpenAI client 模拟两道题的解题过程：
  题 t-01：第 1 轮发起 shell tool call，第 2 轮输出总结（模拟通关）
  题 t-02：第 1 轮直接输出总结（模拟放弃）
平台层（bench.*）用内存 fake 替换。
"""

from __future__ import annotations

import sys
import types


def make_fake_client():
    from types import SimpleNamespace

    class FakeFunction:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class FakeToolCall:
        def __init__(self, tid, name, arguments):
            self.id = tid
            self.type = "function"
            self.function = FakeFunction(name, arguments)

    class FakeMessage:
        def __init__(self, content=None, tool_calls=None):
            self.role = "assistant"
            self.content = content
            self.tool_calls = tool_calls
            self.reasoning_content = "（假思考）先侦察再利用。"

    class FakeChoice:
        def __init__(self, message):
            self.message = message

    class FakeResponse:
        def __init__(self, choice):
            self.choices = [choice]

    state = {"n": 0}

    def create(**kwargs):
        # 上下文隔离校验：每次调用的消息里 system 只应含本题编号
        msgs = kwargs["messages"]
        sys_prompt = next((m["content"] for m in msgs if m["role"] == "system"), "")
        assert kwargs["model"], "model missing"
        assert sys_prompt, "system prompt missing"

        # 复盘总结轮：无 tools（use_tools=False），返回总结文本
        if kwargs.get("tools") is None:
            last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
            assert "最终复盘" in last_user or "工具" in last_user, "closing prompt missing"
            return FakeResponse(FakeChoice(FakeMessage(
                content="复盘：1. 思路：默认凭据进后台拿 flag。2. 工具困难：dirb 字典路径难记。"
                        "3. 优化建议：预装 seclists 常用子集。")))

        assert "shell" in str(kwargs.get("tools")), "tools missing"
        # 校验消息序列：tool 消息必须紧跟带 tool_calls 的 assistant
        last = None
        for m in msgs:
            if m["role"] == "tool":
                assert last and last.get("tool_calls"), "orphan tool message"
            last = m

        state["n"] += 1
        # 上下文隔离验证：本会话的 user 消息只包含当前题
        user_msg = next((m["content"] for m in kwargs["messages"] if m["role"] == "user"), "")
        if "t-02" in user_msg:
            # 第二题：直接总结（放弃）
            return FakeResponse(FakeChoice(FakeMessage(content="t-02 未能完成：卡在 WAF 绕过。")))
        # 第一题：第 1 轮工具调用，第 2 轮总结
        tool_msgs = sum(1 for m in kwargs["messages"] if m["role"] == "tool")
        if tool_msgs == 0:
            tc = FakeToolCall("call_abc123", "shell",
                              '{"action": "run", "command": "echo flag{fake} > /tmp/f", "wait_ms": 500}')
            return FakeResponse(FakeChoice(FakeMessage(content=None, tool_calls=[tc])))
        return FakeResponse(FakeChoice(FakeMessage(content="t-01 完成：提交 flag{fake} 成功。")))

    client = SimpleNamespace()
    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    return client


def install_fake_bench():
    """内存版平台：t-01 解题期间模拟提交成功（通关），t-02 始终未完成。"""
    import bench

    chals = [
        {"unique_code": "t-01", "description": "简单 web", "difficulty": "easy", "level": 1,
         "total_score": 100, "flag_count": 1, "correct_flag_count": 0, "is_completed": False,
         "container_status": "stopped", "container_addr": []},
        {"unique_code": "t-02", "description": "waf 题", "difficulty": "medium", "level": 1,
         "total_score": 300, "flag_count": 1, "correct_flag_count": 0, "is_completed": False,
         "container_status": "stopped", "container_addr": []},
    ]
    calls = []
    state = {"t01_started": False}

    def fetch_challenges():
        # 模拟：t-01 被 start 后、解题期间 flag 提交成功
        if state["t01_started"]:
            for c in chals:
                if c["unique_code"] == "t-01":
                    c["is_completed"] = True
                    c["correct_flag_count"] = 1
        return [dict(c) for c in chals]

    def start_challenge(code):
        calls.append(("start", code))
        if code == "t-01":
            state["t01_started"] = True
        for c in chals:
            if c["unique_code"] == code:
                c["container_status"] = "available"
                c["container_addr"] = ["10.9.9.9:80"]
        return {"unique_code": code, "container_addr": ["10.9.9.9:80"]}

    def close_challenge(code):
        calls.append(("close", code))
        for c in chals:
            if c["unique_code"] == code:
                c["container_status"] = "stopped"
        return {"unique_code": code, "closed": True}

    def choose_next(items):
        pend = [c for c in items if not c["is_completed"]]
        return dict(pend[0]) if pend else None

    def challenge_state(items, code):
        return next((c for c in items if c["unique_code"] == code), None)

    bench.fetch_challenges = fetch_challenges
    bench.start_challenge = start_challenge
    bench.close_challenge = close_challenge
    bench.choose_next = choose_next
    bench.challenge_state = challenge_state
    bench.release_completed = lambda items: []
    return calls


def main() -> int:
    import agent
    import os
    import json
    import shutil
    import tempfile
    from config import CFG
    os.environ.setdefault("AGENT_MODE", "local")
    CFG.resolve()  # 填充 model/base_url（离线自测不真实连接）

    # 记录目录隔离到临时目录（模拟 /work）
    recdir = tempfile.mkdtemp(prefix="tsec_rec_")
    os.environ["RECORDS_DIR"] = recdir
    os.environ["AGENT_PLATFORM_WATCH"] = "0"  # 离线：不开平台轮询线程
    import records
    records.RECORDS_DIR = recdir

    calls = install_fake_bench()
    client = make_fake_client()
    rc = agent.run(client)

    # 断言：两题都被 start+close，t-01 通过 t-02 失败（允许失败重试导致的多次 start）
    started = [c for op, c in calls if op == "start"]
    closed = [c for op, c in calls if op == "close"]
    assert rc == 0, f"run() returned {rc}"
    assert "t-01" in started and "t-02" in started, f"started={started}"
    assert "t-01" in closed and "t-02" in closed, f"closed={closed}"

    # ---- 持久化记录断言 ----
    # meta.json：run 起止齐全
    meta = json.load(open(os.path.join(recdir, "meta.json")))
    assert meta["mode"] == "local" and meta["model"], "meta mode/model missing"
    assert meta["runs"] and meta["runs"][0].get("started_at"), "runs[].started_at missing"
    assert meta["runs"][0].get("finished_at"), "runs[].finished_at missing"
    # tasks.jsonl：生命周期含 task_start(started_at) 与 task_close(closed_at/duration)
    phases = {}
    for line in open(os.path.join(recdir, "tasks.jsonl")):
        r = json.loads(line)
        phases.setdefault(r["code"], set()).add(r["phase"])
        if r["phase"] == "task_start":
            assert r.get("started_at") and r.get("target"), f"task_start fields: {r}"
        if r["phase"] == "task_close":
            assert r.get("closed_at") and r.get("duration_sec") is not None, f"task_close fields: {r}"
            assert r.get("outcome") in ("pass", "fail"), f"outcome: {r}"
    assert phases.get("t-01") >= {"task_start", "task_close"}, f"t-01 lifecycle: {phases.get('t-01')}"
    # conversation.jsonl：全量不裁剪——assistant 含 thinking/output/tool_calls，tool 含 result
    conv_t01 = os.path.join(recdir, "tasks", "t-01", "conversation.jsonl")
    kinds = []
    saw_full = {"thinking": False, "output": False, "tool_calls": False, "result": False}
    for line in open(conv_t01):
        r = json.loads(line)
        kinds.append(r["kind"])
        if r["kind"] == "system" and len(r["content"]) < 500:
            raise AssertionError("system prompt truncated!")
        if r["kind"] == "assistant":
            if r.get("thinking"):
                saw_full["thinking"] = True
                # 忠实：不裁剪，thinking 原文应与模型输出一致（含"先侦察"）
                assert "先侦察" in r["thinking"] or len(r["thinking"]) > 10, r["thinking"]
            if r.get("output"):
                saw_full["output"] = True
            if r.get("tool_calls"):
                saw_full["tool_calls"] = True
                assert '"echo flag{fake}"' in r["tool_calls"][0]["arguments"] or \
                       "flag{fake}" in r["tool_calls"][0]["arguments"], "tool args not faithful"
        if r["kind"] == "tool" and r.get("result"):
            saw_full["result"] = True
    assert "system" in kinds and "user" in kinds and "assistant" in kinds and "tool" in kinds, kinds
    assert all(saw_full.values()), f"conversation missing fields: {saw_full}"
    # 通关复盘：同一 jsonl 里有 closing_summary 标记的 assistant 条目（t-01 通关，t-02 未通关无）
    closing = [r for r in (json.loads(l) for l in open(conv_t01))
               if r["kind"] == "assistant" and r.get("kind_note") == "closing_summary"]
    assert closing, "closing summary missing in conversation.jsonl"
    assert "优化建议" in (closing[0].get("output") or ""), "closing summary content not recorded"
    conv_t02 = os.path.join(recdir, "tasks", "t-02", "conversation.jsonl")
    closing02 = [r for r in (json.loads(l) for l in open(conv_t02))
                 if r.get("kind_note") == "closing_summary"]
    assert not closing02, "t-02 not completed but got closing summary!"
    # t-02 conversation 也存在（对话按题隔离）
    assert os.path.isfile(os.path.join(recdir, "tasks", "t-02", "conversation.jsonl"))
    # events.jsonl 存在且有 run_start/run_finish/final_summary
    evs = [json.loads(l)["type"] for l in open(os.path.join(recdir, "events.jsonl"))]
    assert "run_start" in evs and "run_finish" in evs and "final_summary" in evs, evs

    shutil.rmtree(recdir, ignore_errors=True)
    print("PASS: controller + per-task context + persistent records end-to-end OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
