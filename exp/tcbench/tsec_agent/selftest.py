#!/usr/bin/env python3
"""离线自测：不依赖 VPN / 网关 / 平台，验证「控制器 + 单题独立上下文 + 分段解题」主逻辑。

用假的 OpenAI client 模拟两道题的解题过程：
  题 t-01：第 1 轮发起 shell tool call，第 2 轮输出总结（模拟通关）
  题 t-02：每段第 1 轮直接输出总结（模拟"未通关就想收尾"）→ 关题暂停、带上下文续跑，
           连续 3 段仍不通关 → 判 fail（验证 paused→resume→abandon 链路与记录断言）
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
    # .env 泄漏隔离：config.py 在 import 时加载项目根 .env，其中 AGENT_TASKS/
    # AGENT_TASK_FILTER 会把自测的假题 t-01/t-02 过滤掉（本地 .env 一设就红）。
    # 自测必须与宿主 .env 无关，这里强制清空选题相关变量。
    for _k in ("AGENT_TASKS", "AGENT_TASK_FILTER", "AGENT_TASKS_FILE"):
        os.environ.pop(_k, None)
    CFG.tasks = ""
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
    # 选题模式必须落盘（panel 据此把平台题数/flag 数限定到选题范围；空 = 全部题目）
    assert "task_filter" in meta["runs"][0], "run_start 未落盘 task_filter"
    # tasks.jsonl：生命周期含 task_start(started_at) 与 task_close(closed_at/duration)
    phases = {}
    for line in open(os.path.join(recdir, "tasks.jsonl")):
        r = json.loads(line)
        phases.setdefault(r["code"], set()).add(r["phase"])
        if r["phase"] == "task_start":
            assert r.get("started_at") and r.get("target"), f"task_start fields: {r}"
        if r["phase"] == "task_close":
            assert r.get("closed_at") and r.get("duration_sec") is not None, f"task_close fields: {r}"
            # paused = 分段解题：本轮到点关题保留上下文，稍后续跑（不判死）
            assert r.get("outcome") in ("pass", "fail", "paused"), f"outcome: {r}"
    assert phases.get("t-01") >= {"task_start", "task_close"}, f"t-01 lifecycle: {phases.get('t-01')}"
    # conversation.jsonl：全量不裁剪——assistant 含 thinking/output/tool_calls，tool 含 result
    conv_t01 = os.path.join(recdir, "tasks", "t-01", "conversation.jsonl")
    kinds = []
    saw_full = {"thinking": False, "output": False, "tool_calls": False, "result": False}
    for line in open(conv_t01):
        r = json.loads(line)
        kinds.append(r["kind"])
        # 甘特图数据：每条消息必须有成对起止时间戳 + duration_ms
        assert isinstance(r.get("started_at"), (int, float)), f"started_at missing: {r}"
        assert isinstance(r.get("ended_at"), (int, float)), f"ended_at missing: {r}"
        assert r["ended_at"] >= r["started_at"], f"ended<started: {r}"
        assert isinstance(r.get("duration_ms"), int), f"duration_ms missing: {r}"
        if r["kind"] == "assistant":
            # LLM 调用应有真实耗时（fake client 有 sleep/计算开销）
            assert "先侦察" in (r.get("thinking") or "") or len(r.get("thinking") or "") > 10, r.get("thinking")
            assert r["duration_ms"] >= 0
        if r["kind"] == "tool":
            # 工具执行起止独立于 LLM 调用
            assert r["ended_at"] >= r["started_at"]
            assert "flag{fake}" in (r.get("arguments") or {}).get("command", "") or r.get("result")
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
    # 分段解题：t-02 每段"未通关就收尾" → 暂停关题、带上下文续跑，直到连续收尾达上限判 fail
    assert "task_paused" in evs, f"pause event missing: {evs}"
    assert "task_abandoned" in evs, f"abandon event missing: {evs}"
    t02_events = [r for r in (json.loads(l) for l in open(os.path.join(recdir, "events.jsonl")))
                  if r.get("code") == "t-02"]
    assert any(e["type"] == "task_abandoned" and
               e.get("status") == "giveup_loop" for e in t02_events), t02_events
    # 续跑：t-02 的对话里应有"继续解题"通知，且排在重建出的上下文之后
    # （system + 任务卡只在首段写一次，续跑不重写 → 文件里仍是 1 条 system）
    recs_t02 = [json.loads(l) for l in open(conv_t02)]
    assert sum(1 for r in recs_t02 if r["kind"] == "system") == 1, "resume must not rewrite system"
    resume_notes = [r for r in recs_t02 if r["kind"] == "user" and "继续解题" in (r.get("content") or "")]
    assert resume_notes, "resume note missing in t-02 conversation"
    # 续跑时上下文从记录重建：断言重建结果自洽（每个 tool_calls 都紧跟同 id 的 tool 结果）
    rebuilt = records.rebuild_messages(conv_t02)
    assert rebuilt and rebuilt[0]["role"] == "system", "rebuild: system missing"
    for i, m in enumerate(rebuilt):
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = {t["id"] for t in m["tool_calls"]}
            got = {x.get("tool_call_id") for x in rebuilt[i + 1:]
                   if x["role"] == "tool"} if i + 1 < len(rebuilt) else set()
            if m is rebuilt[-1]:
                continue  # 尾部总结性 assistant（无后续）由结束约定负责
            assert ids <= got, f"rebuild: orphan tool_calls at {i}"
    # tasks.jsonl：t-02 至少有一次 resume=True 的 task_start（续跑重新 start）
    t02_starts = [r for r in (json.loads(l) for l in open(os.path.join(recdir, "tasks.jsonl")))
                  if r.get("phase") == "task_start" and r.get("code") == "t-02"]
    assert len(t02_starts) >= 3, f"t-02 should pause+resume, starts={len(t02_starts)}"
    assert any(r.get("resume") for r in t02_starts), "resume task_start missing"

    # ---- 题目选择（AGENT_TASKS / --task）：只解部分题做测试用 ----
    assert agent.select_tasks("") == []
    assert agent.select_tasks("b-01,b-02  c-03") == ["b-01", "b-02", "c-03"]
    assert agent.task_selected("b-01", []), "空选择应等于全部"
    assert agent.task_selected("c-03", ["b-01", "c-0*"]), "通配未命中"
    assert agent.task_selected("e1-05", ["e1-05"]), "精确匹配失败"
    assert not agent.task_selected("d-01", ["b-01", "c-0*"]), "越界题目不该被选中"
    print("  ok: 题目选择解析与通配（AGENT_TASKS/--task）")

    # ---- shell 新参数：边沿等待 / 分页 / 落盘 / wait_any / job 上限 ----
    import shell_tool as _st
    from config import CFG as _CFG
    _w = tempfile.mkdtemp(prefix="selftest_shell_")
    _st.set_task_cwd(_w)
    _p = _st.SHELL.run("for i in $(seq 1 200); do printf 'L%03d abcdefghij\\n' $i; sleep 0.05; done",
                       wait_ms=0, wait_sec=4)
    assert "hard limit" in _p, _p[:200]
    _jid = _p.split("[job ")[1].split("]")[0]
    if "running" in _p:
        assert int(_p.split("out=")[1].split("B")[0]) > 0    # 提前返回也带回了输出
    _pg = _st.SHELL.status(_jid, lines=3)
    assert "output[tail]:" in _pg and "next_offset=" in _pg, _pg[:200]
    _st.SHELL.kill(_jid)
    _of = _st.SHELL.run("echo ONDISK; sleep 1", out_file="s.log", wait_ms=0, wait_sec=2)
    assert os.path.isfile(os.path.join(_w, "s.log")), "out_file 未落盘"
    _st.SHELL.kill(_of.split("[job ")[1].split("]")[0])
    assert _st.dispatch_shell({"job_id": "nonexist"}).startswith("error:"), "省略 action 未兜底"
    _old = _CFG.shell_max_jobs
    _CFG.shell_max_jobs = 1
    _sl = _st.SHELL.run("sleep 10", wait_ms=0)
    assert _st.SHELL.run("echo x", wait_ms=0).startswith("error: 后台 job 已满"), "job 上限未生效"
    _st.SHELL.kill(_sl.split("[job ")[1].split("]")[0])
    _CFG.shell_max_jobs = _old
    _st.set_task_cwd("")
    print("  ok: shell 新参数（边沿等待/分页/落盘/上限/省略 action）")

    # ---- 多模态：action=view 返回图片 parts / 文本降级 / 错误分支 ----
    import base64 as _b64
    from shell_tool import dispatch_shell as _ds
    _imgdir = tempfile.mkdtemp(prefix="selftest_view_")
    _st.set_task_cwd(_imgdir)
    # 1x1 PNG（真实文件，尺寸解析应得 1x1）
    open(os.path.join(_imgdir, "x.png"), "wb").write(_b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    _img = _ds({"action": "view", "path": "x.png"})
    assert isinstance(_img, list), type(_img)
    assert [p2["type"] for p2 in _img] == ["text", "image_url"], _img
    assert "1x1" in _img[0]["text"], _img[0]["text"]
    assert _img[1]["image_url"]["url"].startswith("data:image/png;base64,"), "data URL 缺失"
    assert _img[1]["image_url"]["detail"] == "low"
    # 网关拒图/显式关闭 → 文本模式（含 OCR 兜底提示）
    _old_vision = _CFG.vision
    _CFG.vision = "off"
    _txt = _ds({"action": "view", "path": "x.png"})
    assert isinstance(_txt, str) and "文本模式" in _txt, _txt[:200]
    _CFG.vision = _old_vision
    # 错误分支：文件不存在 / 格式不支持
    assert _ds({"action": "view", "path": "nope.png"}).startswith("error:")
    assert _ds({"action": "view", "path": "/etc/hostname"}).startswith("error:")
    # 修复说明前缀合入图片 parts 的首个 text 段（不破坏结构）
    _m = agent._with_fix_note(_img, "测试修复")
    assert _m[0]["text"].startswith("[参数已自动修复") and _m[1]["type"] == "image_url"
    _st.set_task_cwd("")
    # 重建上下文时图片 parts 原样还原（不能被转成 JSON 字符串）
    _jl = os.path.join(_imgdir, "conv.jsonl")
    with open(_jl, "w", encoding="utf-8") as _f:
        _f.write(json.dumps({"kind": "system", "content": "S"}, ensure_ascii=False) + "\n")
        _f.write(json.dumps({"kind": "assistant", "output": "",
                             "tool_calls": [{"id": "c1", "name": "shell", "arguments": "{}"}]},
                            ensure_ascii=False) + "\n")
        _f.write(json.dumps({"kind": "tool", "tool_call_id": "c1", "result": _img},
                            ensure_ascii=False) + "\n")
    _rebuilt = records.rebuild_messages(_jl)
    assert isinstance(_rebuilt[-1]["content"], list), type(_rebuilt[-1]["content"])
    assert _rebuilt[-1]["content"][1]["type"] == "image_url", "图片 part 重建丢失"
    print("  ok: 多模态 view（图片 parts / 文本降级 / 错误分支 / 重建保真）")

    # ---- 工具参数序列化容错（形状取自上一轮真实跑分里的损坏样本）----
    import json as _json
    from shell_tool import parse_tool_arguments
    Q, NL = chr(34), chr(10)
    TAGO = chr(60) + "parameter name=" + Q   # XML 开标签（避免在源码里写字面量）

    # 1) action 混入 XML 参数片段（run + command 在里面），其余参数要保留
    xml_action = "run" + Q + ">" + NL + TAGO + "command" + Q + ">" + \
        "cd /work/tasks/x; ffuf -u http://t/FUZZ -w big.txt"
    args, fix = parse_tool_arguments(_json.dumps({"action": xml_action, "timeout_sec": 60}))
    assert args and args["action"] == "run", args
    assert args["command"].startswith("cd /work/tasks/x; ffuf"), args
    assert args["timeout_sec"] == 60 and fix, (args, fix)
    # 2) 同类损坏但动作是 wait（job_id 在片段里）
    xml_wait = "wait" + Q + ">" + NL + TAGO + "job_id" + Q + ">" + "a5daca2b"
    args, fix = parse_tool_arguments(_json.dumps({"action": xml_wait, "timeout_sec": 120}))
    assert args and args["action"] == "wait" and args["job_id"] == "a5daca2b", args
    # 3) action 写成别的工具名，但 command 正常
    args, fix = parse_tool_arguments(_json.dumps({"action": "shell", "command": "cat /tmp/x"}))
    assert args["action"] == "run" and args["command"] == "cat /tmp/x" and fix, (args, fix)
    # 4) action 整段是命令（没有 command 键），wait_ms 要保留
    args, fix = parse_tool_arguments(_json.dumps({"action": "cd /work; nmap -p- 10.0.0.1",
                                                  "wait_ms": 8000}))
    assert args["action"] == "run" and args["command"] == "cd /work; nmap -p- 10.0.0.1"
    assert args["wait_ms"] == 8000 and fix, (args, fix)
    # 5) JSON 值没加引号（解析直接失败）
    bare = "{" + Q + "action" + Q + ": " + Q + "wait" + Q + ", " + Q + "job_id" + Q + \
        ": 829f40d3, " + Q + "timeout_sec" + Q + ": 90}"
    args, fix = parse_tool_arguments(bare)
    assert args["action"] == "wait" and args["job_id"] == "829f40d3", args
    # 6) 正常参数不应被改动（不产生修复说明）
    args, fix = parse_tool_arguments(_json.dumps({"action": "run", "command": "echo ok"}))
    assert args["command"] == "echo ok" and fix is None, (args, fix)
    # 7) 无任何可恢复参数 → 原样返回，dispatch 给出可操作的报错
    from shell_tool import dispatch_shell
    args, fix = parse_tool_arguments(_json.dumps({"action": "shell"}))
    assert args.get("action") == "shell" and fix is None, (args, fix)
    err = dispatch_shell(dict(args))
    assert err.startswith("error: unknown action") and "action" in err, err

    # ---- 模式判定与转写（hosted 自动检测的核心，全离线断言）----
    import config as _cfg
    # to_gateway_url：官方规则 https→.tsecbench.gw；http/IP/已转写 一律原样
    assert _cfg.to_gateway_url("https://api.deepseek.com/v1") == \
        "http://api.deepseek.com.tsecbench.gw/v1", "https 转写失败"
    assert _cfg.to_gateway_url("https://dashscope.aliyuncs.com/compatible-mode/v1") == \
        "http://dashscope.aliyuncs.com.tsecbench.gw/compatible-mode/v1", "带路径转写失败"
    assert _cfg.to_gateway_url("http://api.deepseek.com.tsecbench.gw/v1") == \
        "http://api.deepseek.com.tsecbench.gw/v1", "已转写应幂等"
    assert _cfg.to_gateway_url("http://10.0.0.5:8080/v1") == "http://10.0.0.5:8080/v1", \
        "http 内网地址不能被转写"
    assert _cfg.to_gateway_url("https://10.0.0.5/v1") == "https://10.0.0.5/v1", \
        "IP 字面量不能拼 .tsecbench.gw"
    # 结构信号：托管注入转写形态 → hosted；本地原始形态 → 不触发
    # hosted 模型名与本地同名（实测：托管也是 flash，上届 v4.1/expires 后缀不再使用）
    assert "expires-on" not in _cfg.HOSTED_MODEL, \
        f"hosted 模型名不该带上届 expires-on 后缀: {_cfg.HOSTED_MODEL}"
    _old_bench = os.environ.get("BENCHMARK_BASE_URL")
    os.environ["BENCHMARK_BASE_URL"] = "http://tsecbench.zc.tencent.com.tsecbench.gw"
    assert _cfg._bench_looks_hosted(), "转写形态 bench URL 应触发 hosted 信号"
    os.environ["BENCHMARK_BASE_URL"] = "https://tsecbench.zc.tencent.com"
    assert not _cfg._bench_looks_hosted(), "原始形态 bench URL 不该触发 hosted 信号"
    os.environ["BENCHMARK_BASE_URL"] = "https://10.0.0.5:8443"  # IP+端口也不能误触发
    assert not _cfg._bench_looks_hosted(), "IP bench URL 不该触发 hosted 信号"
    if _old_bench is None:
        os.environ.pop("BENCHMARK_BASE_URL", None)
    else:
        os.environ["BENCHMARK_BASE_URL"] = _old_bench
    # 结构信号在无网络时也能直接判 hosted（AGENT_MODE=auto，探测全部失败也不带偏）
    _old_mode = os.environ.get("AGENT_MODE")
    os.environ.pop("AGENT_MODE", None)
    os.environ["BENCHMARK_BASE_URL"] = "http://tsecbench.zc.tencent.com.tsecbench.gw"
    assert _cfg.detect_mode() == "hosted", "结构信号应无网络直判 hosted"
    # 统一还原
    if _old_mode is None:
        os.environ.pop("AGENT_MODE", None)
    else:
        os.environ["AGENT_MODE"] = _old_mode
    if _old_bench is None:
        os.environ.pop("BENCHMARK_BASE_URL", None)
    else:
        os.environ["BENCHMARK_BASE_URL"] = _old_bench
    print("  ok: 模式判定（结构信号/转写规则/IP 与 http 保护）")

    # ---- hosted key 候选发现 + 401 热切换（离线：假 OpenAI，匿名被拒、真 key 放行）----
    from types import SimpleNamespace as _SN
    import config
    assert config.HOSTED_API_KEY, "hosted key 不该为空（回落链：显式 > 本地 key > tsec-bench）"
    _old_key, _old_mode2 = agent.CFG.llm_api_key, agent.CFG.mode
    _old_reauth = agent._REAUTH_TRIED
    _old_disc = agent._DISCOVERY_DONE
    agent.CFG.mode = "hosted"
    agent.CFG.llm_api_key = "tsec-bench"
    agent._REAUTH_TRIED = False
    agent._DISCOVERY_DONE = False
    agent._CURRENT_CLIENT = None
    os.environ["DEEPSEEK_API_KEY"] = "real-key-1234"

    # 候选清单：匿名在前、真实 key 在后、当前 key 兜底；标签只带尾4位掩码
    cands = agent._key_candidates()
    assert cands[0] == ("anonymous", ""), cands[0]
    labels = [c[0] for c in cands]
    assert any("DEEPSEEK_API_KEY" in l for l in labels), labels
    # 当前 key（tsec-bench）应出现在清单里；若与某 env 值相同则按值去重并入该槽位
    assert "tsec-bench" in [k for _, k in cands], f"当前配置应在候选里: {cands}"
    # 独一无二的当前 key（不与 env 重复时）以 current-config 兜底在最后
    _cur_only = agent.CFG.llm_api_key
    agent.CFG.llm_api_key = "unique-current-key-xyz"
    c3 = agent._key_candidates()
    assert c3[-1] == ("current-config(****-xyz)", "unique-current-key-xyz"), c3[-1]
    agent.CFG.llm_api_key = _cur_only
    assert all("real-key-1234" not in l and "tsec-bench" not in l for l in labels), \
        f"候选标签泄露完整 key: {labels}"
    # exclude：401 热切换时跳过刚被拒的 key
    c2 = agent._key_candidates(exclude={"tsec-bench"})
    assert all(k != "tsec-bench" for _, k in c2), c2
    assert agent._key_mask("real-key-1234") == "****1234"
    assert agent._key_mask("") == "(none)"
    print("  ok: key 候选清单（匿名优先/掩码/exclude）")

    # 假网关：匿名和 tsec-bench 被 401，BENCHMARK_TOKEN/DEEPSEEK_API_KEY 视 key 放行
    class _Err(Exception):
        status_code = 401

    _PROBE_LOG: list[str] = []

    class _FakeCompletions:
        def __init__(self, accept_key):
            self.accept_key = accept_key

        def create(self, **kwargs):
            # 凭构造时的 api_key 判定（匿名走 http_client 钩子路径，api_key 是占位串）
            if self.accept_key != "real-key-1234":
                raise _Err("Authentication Fails, Your api key is invalid")
            return _SN(choices=[_SN(message=_SN(content="正常", role="assistant"))])

    def _fake_openai(base_url=None, api_key=None, timeout=None, max_retries=None,
                     http_client=None):
        # 匿名路径 api_key 是占位串（真实 wire 上被 httpx2 钩子剥掉）
        _PROBE_LOG.append("<anonymous>" if api_key == "anonymous-placeholder"
                          else (api_key or "<none>"))
        return _SN(chat=_SN(completions=_FakeCompletions(api_key)))

    _orig_openai = agent.OpenAI
    agent.OpenAI = _fake_openai
    try:
        # probe_all：全部候选都探一遍（匿名/显式/token 被拒，真实 key OK），按优先级选中
        nc = agent.discover_gateway_key("selftest", probe_all=True)
        assert nc is not None, "discover 应命中 DEEPSEEK_API_KEY"
        assert agent.CFG.llm_api_key == "real-key-1234", agent.CFG.llm_api_key
        assert agent._CURRENT_CLIENT is not None
        assert "<anonymous>" in _PROBE_LOG, "匿名候选应被探测"
        # 401 热切换：排除当前 key 后其余全被拒 → None，且保持已验证 key 不变
        agent._REAUTH_TRIED = False
        nc2 = agent._try_reauth(_Err())
        assert nc2 is None, "排除当前 key 后其余候选应全被拒"
        assert agent.CFG.llm_api_key == "real-key-1234", "失败时不该改 key"
        # 只热切换一次（防死循环）：_REAUTH_TRIED 置位后直接 None
        agent._REAUTH_TRIED = True
        assert agent._try_reauth(_Err()) is None, "_REAUTH_TRIED 置位后不该再探测"
        # 全候选被拒 → None（不动 CFG）
        agent.CFG.llm_api_key = "tsec-bench"
        agent._REAUTH_TRIED = False
        agent._DISCOVERY_DONE = False
        agent.OpenAI = lambda **kw: _SN(chat=_SN(completions=_FakeCompletions("never")))
        assert agent.discover_gateway_key("selftest-allfail") is None, "全候选被拒应 None"
        assert agent.CFG.llm_api_key == "tsec-bench", "失败时不该改 key"
        # 非 401 不触发
        agent._REAUTH_TRIED = False
        assert agent._try_reauth(Exception("connection reset")) is None
        assert not agent._REAUTH_TRIED, "非 401 不该置位 _REAUTH_TRIED"
        # 匿名 client 构造：真发无 Authorization 请求（httpx2 钩子路径不抛即可）
        anon = agent._client_with_key("")
        assert anon is not None, "匿名 client 应能构造（openai 3.x/httpx2）"
    finally:
        agent.OpenAI = _orig_openai
        agent.CFG.llm_api_key, agent.CFG.mode = _old_key, _old_mode2
        agent._REAUTH_TRIED = _old_reauth
        agent._DISCOVERY_DONE = _old_disc
        agent._CURRENT_CLIENT = None
    print("  ok: key 候选发现（匿名探测/probe_all/全拒/非401/热切换）")

    shutil.rmtree(recdir, ignore_errors=True)
    print("PASS: controller + per-task context + persistent records end-to-end OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
