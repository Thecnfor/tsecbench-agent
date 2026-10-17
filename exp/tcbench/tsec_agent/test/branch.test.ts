/** branch.ts / corelog.ts / bystander.ts / records 分支重建 的离线单测。
 * 全部纯逻辑，无网络、无 LLM。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { type BranchCfg, BranchManager, normalizeNote } from "../src/branch.js";
import {
  BYSTANDER_SYSTEM,
  buildBystanderInput,
  bystanderMessage,
  bystanderShouldFire,
  evaluateStall,
  isOverreachingAdvice,
  recentDigest,
} from "../src/bystander.js";
import {
  buildBystanderLogView,
  CorelogWriter,
  extractFlagIndex,
  FlagLedger,
  readCorelog,
} from "../src/corelog.js";
import { FORK_PROMPT_EXTRA } from "../src/prompt.js";
import { rebuildBranches, rebuildMessages } from "../src/records.js";
import type { ChatMessage } from "../src/types.js";

const CFG: BranchCfg = {
  enabled: true,
  maxBranches: 3,
  maxForks: 5,
  forkCooldownSec: 180,
  pruneSec: 600,
  broadcastMax: 8,
  bystanderMax: 2,
  bystanderCooldownSec: 400,
};

function trunkMsgs(): ChatMessage[] {
  return [
    { role: "system", content: "sys" },
    { role: "user", content: "task card" },
    { role: "assistant", content: "ok" },
  ];
}
function mkBm(now = 1000): BranchManager {
  return new BranchManager(CFG, trunkMsgs(), now, "/work/tasks/x");
}

describe("BranchManager 护栏", () => {
  it("fork 未启用时拒绝", () => {
    const bm = new BranchManager({ ...CFG, enabled: false }, trunkMsgs(), 1000);
    expect(bm.reserveFork(0, 1000).ok).toBe(false);
  });

  it("存活分支上限（含 trunk）", () => {
    const bm = mkBm();
    expect(bm.reserveFork(0, 1001).ok).toBe(true);
    bm.materializeFork(1, 0, "方向A", 3, 1001, (id) => `/w/br/${id}`);
    expect(bm.reserveFork(0, 1200).ok).toBe(true); // 跳过冷却
    bm.materializeFork(2, 0, "方向B", 3, 1200, (id) => `/w/br/${id}`);
    const denied = bm.reserveFork(0, 1400);
    expect(denied.ok).toBe(false);
    if (!denied.ok) expect(denied.reason).toContain("上限 3");
  });

  it("fork 总数硬上限（剪枝后也不恢复）", () => {
    const bm = new BranchManager({ ...CFG, maxForks: 1 }, trunkMsgs(), 1000);
    expect(bm.reserveFork(0, 1001).ok).toBe(true);
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    bm.prune(1); // 回收后总数护栏仍生效
    expect(bm.reserveFork(0, 1002).ok).toBe(false);
  });

  it("冷却期内拒绝并告知剩余时间", () => {
    const bm = mkBm();
    expect(bm.reserveFork(0, 1100).ok).toBe(true);
    const denied = bm.reserveFork(0, 1150); // 冷却 180s，只过了 50s
    expect(denied.ok).toBe(false);
    if (!denied.ok) expect(denied.reason).toContain("130s");
    expect(bm.reserveFork(0, 1100 + 181).ok).toBe(true);
  });

  it("轮转调度：park/unpark 与全 parked 收束", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    const seen: number[] = [];
    for (let i = 0; i < 4; i++) {
      const b = bm.pick();
      if (b) seen.push(b.id);
    }
    // trunk 与 branch 1 交替（a1 b1 a2 b2 …）
    expect(seen).toEqual([0, 1, 0, 1]);
    bm.park(0);
    bm.park(1);
    expect(bm.pick()).toBeNull(); // 全 parked → 段收束
  });

  it("trunk 永不剪枝", () => {
    const bm = mkBm();
    expect(bm.prune(0)).toBeNull();
  });

  it("分支可再分叉（深度不设限，护栏兜底）", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    const r = bm.reserveFork(1, 1200); // 分支 1 再分叉
    expect(r.ok).toBe(true);
    bm.materializeFork(2, 1, "A2", 5, 1200, (id) => `/w/br/${id}`);
    const b2 = bm.get(2);
    expect(b2?.parent).toBe(1);
    // 子分支继承父分支上下文（含父的 directive）
    expect(b2?.messages.some((m) => String(m.content).includes("分支 1 · 方向"))).toBe(true);
  });
});

describe("materializeFork 协议安全", () => {
  it("复制父上下文 + 尾部 append 方向指令（不动 system）", () => {
    const bm = mkBm();
    const made = bm.materializeFork(1, 0, "探测 redis", 3, 1001, (id) => `/w/br/${id}`);
    expect(made).not.toBeNull();
    const msgs = made!.branch.messages;
    // system 逐字节一致（缓存红线：不动前缀）
    expect(msgs[0]).toEqual(trunkMsgs()[0]);
    // 父消息完整拷贝 + 尾部 directive
    expect(msgs.length).toBe(trunkMsgs().length + 1);
    expect(msgs.at(-1)?.role).toBe("user");
    expect(String(msgs.at(-1)?.content)).toContain("分支 1");
    expect(String(msgs.at(-1)?.content)).toContain("探测 redis");
    expect(String(msgs.at(-1)?.content)).toContain("/w/br/1");
  });

  it("重复 materialize 同 id 无效（防重放）", () => {
    const bm = mkBm();
    expect(bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`)).not.toBeNull();
    expect(bm.materializeFork(1, 0, "A", 3, 1002, (id) => `/w/br/${id}`)).toBeNull();
  });
});

describe("broadcast", () => {
  it("尾部追加到目标并激活 parked 分支", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    bm.park(1);
    const res = bm.broadcast(0, "6379 有未授权访问", "all", 1100);
    expect(res.ok).toBe(true);
    expect(res.delivered).toEqual([1]);
    const b1 = bm.get(1)!;
    expect(b1.messages.at(-1)?.content).toContain("[分支 0 广播] 6379 有未授权访问");
    expect(b1.active).toBe(true); // parked 分支被广播激活
  });

  it("to=trunk / 数组 / 无效目标", () => {
    const bm = mkBm();
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    expect(bm.broadcast(1, "收到", "trunk", 1100).delivered).toEqual([0]);
    expect(bm.broadcast(1, "hi", [0], 1101).delivered).toEqual([0]);
    expect(bm.broadcast(1, "hi", [99], 1102).ok).toBe(false);
  });

  it("子分支广播只上行主分支（横向改投 0；主→任意不受限）", () => {
    const bm = mkBm();
    const f1 = bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    const f2 = bm.materializeFork(2, 0, "B", 3, 1002, (id) => `/w/br/${id}`);
    expect(f1?.directive).toContain("只上行给主分支（分支 0）");
    expect(f2).not.toBeNull();
    // 子 → "all"：只落主分支，兄弟收不到（星型拓扑）
    const all = bm.broadcast(1, "全局发现需要扩散", "all", 1100);
    expect(all.ok).toBe(true);
    expect(all.delivered).toEqual([0]);
    expect(all.message).toContain("子分支仅可上行主分支");
    expect(bm.get(2)!.messages.some((m) => String(m.content ?? "").includes("[分支 1 广播]"))).toBe(
      false,
    );
    expect(bm.get(0)!.messages.at(-1)?.content).toContain("[分支 1 广播] 全局发现需要扩散");
    // 子 → 具体兄弟 [2]：改投主分支，由它转发
    const lat = bm.broadcast(1, "点对点给兄弟分支", [2], 1101);
    expect(lat.ok).toBe(true);
    expect(lat.delivered).toEqual([0]);
    expect(lat.message).toContain("横向协同由主分支转发");
    // 子 → "trunk"：原语义原文案
    expect(bm.broadcast(1, "直接上行汇报", "trunk", 1102).message).toBe("已广播给分支 0（1 个）");
    // 无效 id 照旧拒绝（不因夹取变通投）
    expect(bm.broadcast(1, "无效目标", [99], 1103).ok).toBe(false);
    // 主分支下行全连保留
    const down = bm.broadcast(0, "统筹：先暂停副作用操作", "all", 1104);
    expect(down.ok).toBe(true);
    expect([...down.delivered].sort()).toEqual([1, 2]);
  });

  it("限频 + 近似重复去重", () => {
    const bm = new BranchManager({ ...CFG, broadcastMax: 2 }, trunkMsgs(), 1000);
    bm.materializeFork(1, 0, "A", 3, 1001, (id) => `/w/br/${id}`);
    expect(bm.broadcast(0, "发现X", "all", 1100).ok).toBe(true);
    // 近似重复（大小写/标点差异）被抑制且不占额度
    const dup = bm.broadcast(0, "发现x！", "all", 1101);
    expect(dup.ok).toBe(false);
    expect(dup.message).toContain("重复");
    expect(bm.broadcast(0, "发现Y", "all", 1102).ok).toBe(true); // 额度还剩
    const capped = bm.broadcast(0, "发现Z", "all", 1103);
    expect(capped.ok).toBe(false);
    expect(capped.message).toContain("上限 2");
  });
});

describe("normalizeNote", () => {
  it("去标点去空白小写（去重口径更激进）", () => {
    expect(normalizeNote("Hello，  World！")).toBe("helloworld");
  });
});

describe("FlagLedger", () => {
  it("索引去重 + 进度表示 + 完成判定", () => {
    const led = new FlagLedger();
    led.setTotal(3);
    expect(led.add(1, 0)).toBe(true);
    expect(led.add(1, 1)).toBe(false); // 同 index 重复
    expect(led.add(3, 1)).toBe(true);
    expect(led.progress()).toBe("2/3 (idx:1,3)");
    expect(led.done()).toBe(false);
    led.add(2, 0);
    expect(led.done()).toBe(true);
  });

  it("extractFlagIndex 解析 submit 响应（含 null）", () => {
    const ok = '{"correct":true,"matched_flag_index":2,"total_flag_count":5}';
    expect(extractFlagIndex(ok)).toBe(2);
    const bad = '{"correct":false,"matched_flag_index":null}';
    expect(extractFlagIndex(bad)).toBeNull();
    expect(extractFlagIndex("409 duplicate")).toBeNull();
  });
});

describe("corelog", () => {
  it("写读一致 + 重启 seq 接续", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "corelog_"));
    const file = path.join(dir, "corelog.jsonl");
    const w = new CorelogWriter(file, "t-01");
    w.write({ branch: 0, kind: "core", note: "发现注入点" });
    w.write({ branch: 1, kind: "flag", flag_index: 1, correct_flag_count: 1, total_flag_count: 2 });
    const w2 = new CorelogWriter(file, "t-01"); // 模拟重启
    w2.write({ branch: 1, kind: "core", note: "继续" });
    const entries = readCorelog(file);
    expect(entries.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(entries[1].kind).toBe("flag");
  });

  it("buildBystanderLogView 按分支分组（交错写入 → 分组可读）", () => {
    const entries = [
      {
        ts: "2026-10-16 10:00:01",
        seq: 1,
        code: "t",
        branch: 0,
        kind: "core" as const,
        note: "A1",
      },
      {
        ts: "2026-10-16 10:00:02",
        seq: 2,
        code: "t",
        branch: 1,
        kind: "core" as const,
        note: "B1",
      },
      {
        ts: "2026-10-16 10:00:03",
        seq: 3,
        code: "t",
        branch: 0,
        kind: "core" as const,
        note: "A2",
      },
      {
        ts: "2026-10-16 10:00:04",
        seq: 4,
        code: "t",
        branch: 1,
        kind: "flag" as const,
        flag_index: 1,
      },
    ];
    const view = buildBystanderLogView(entries, {
      branches: new Map([[1, "探测 redis"]]),
    });
    // 分支 0 的两条连续、分支 1 的两条连续，不按全局时间交错
    expect(view.indexOf("A1")).toBeLessThan(view.indexOf("A2"));
    expect(view.indexOf("B1")).toBeLessThan(view.indexOf("[flag] idx=1"));
    expect(view).toContain("探测 redis");
    expect(view).toContain("主分支");
  });
});

describe("旁观者信号与输入", () => {
  it("evaluateStall 五信号：flag 主时钟一票 + 分支级任二", () => {
    const base = {
      sinceCoreSec: 0,
      stallSec: 300,
      flagSinceSec: 0,
      flagStallSec: 900,
      recentCmds: [] as string[],
    };
    expect(evaluateStall(base).count).toBe(0);
    expect(bystanderShouldFire(evaluateStall(base))).toBe(false);
    // ⓪ flag 主时钟：长时间没 flag 一票触发（以 flag 为核心的介入锚；
    //    即使分支刚记过卡——run3 记卡型空转靠它抓）
    const fs0 = evaluateStall({ ...base, flagSinceSec: 901 });
    expect(fs0.flagStall).toBe(true);
    expect(bystanderShouldFire(fs0)).toBe(true);
    expect(evaluateStall({ ...base, flagSinceSec: 800 }).flagStall).toBe(false);
    // ① 只停滞 → 1（不触发）
    const s1 = evaluateStall({ ...base, sinceCoreSec: 301 });
    expect(s1.count).toBe(1);
    expect(bystanderShouldFire(s1)).toBe(false);
    // ①+② 停滞+重复 → 触发
    const s12 = evaluateStall({
      ...base,
      sinceCoreSec: 301,
      recentCmds: ["nmap -sT x", "nmap -sT x", "nmap -sT x"],
    });
    expect(s12.count).toBe(2);
    expect(bystanderShouldFire(s12)).toBe(true);
    // ③ 近8次连接类硬失败≥3（幻觉目标空转）
    const hf = evaluateStall({ ...base, recentHardFails: [false, true, true, true] });
    expect(hf.hardFail).toBe(true);
    expect(hf.count).toBe(1);
    expect(evaluateStall({ ...base, recentHardFails: [true, true] }).hardFail).toBe(false);
    // ④ 连续 correct:false ≥2（幻觉提交）
    expect(evaluateStall({ ...base, wrongSubmits: 2 }).hallucSubmit).toBe(true);
    expect(evaluateStall({ ...base, wrongSubmits: 1 }).hallucSubmit).toBe(false);
    // ③+④：错提交+连接空转（无任何时间信号也触发）
    expect(
      bystanderShouldFire(
        evaluateStall({ ...base, wrongSubmits: 2, recentHardFails: [true, true, true] }),
      ),
    ).toBe(true);
    // 新 flag 后主时钟由调用方刷新（flagSinceSec 归零 → 不再一票）
    expect(bystanderShouldFire(evaluateStall({ ...base, flagSinceSec: 0 }))).toBe(false);
  });
  it("buildBystanderInput 组装完整要素", () => {
    const led = new FlagLedger();
    led.setTotal(3);
    led.add(1, 0);
    const input = buildBystanderInput({
      taskCard:
        "请渗透以下题目并提交其全部 flag：\n- unique_code: t-99\n- 题目描述: 一个带登录的 CMS",
      entries: [
        {
          ts: "2026-10-16 10:00:01",
          seq: 1,
          code: "t",
          branch: 0,
          kind: "core",
          note: "SQLi 疑似",
        },
      ],
      branchId: 0,
      branchNames: new Map([[0, "主分支"]]),
      ledger: led,
      recentDigest: "- assistant: 继续注",
      recentCmds: ["sqlmap -u http://x"],
      segElapsedSec: 400,
      signals: {
        flagStall: true,
        stall: true,
        repeat: false,
        hardFail: false,
        hallucSubmit: false,
        count: 1,
      },
    });
    expect(input).toContain("t-99"); // 题面必须在（run5 缺题面致旁观者臆测换题）
    expect(input).toContain("1/3 (idx:1)");
    expect(input).toContain("SQLi 疑似");
    expect(input).toContain("sqlmap");
    expect(input).toContain("分支停滞✓");
    expect(input).toContain("无flag✓");
  });

  it("recentDigest 截断并标注 tool_calls", () => {
    const digest = recentDigest(
      [
        {
          role: "assistant",
          content: "x".repeat(500),
          tool_calls: [{ function: { name: "shell" } }],
        },
        { role: "tool", content: "output" },
      ],
      5,
    );
    expect(digest).toContain("[tool_calls: shell]");
    expect(digest.length).toBeLessThan(600);
  });

  it("bystanderMessage 规整前缀", () => {
    expect(bystanderMessage("  换抽象层想  ")).toBe("[旁观者提示] 换抽象层想");
  });
});

describe("records 分支重建", () => {
  /** 构造一个带 fork 的 conversation.jsonl。 */
  function writeConv(): string {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "branch_rec_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "assistant",
        output: "先侦察",
        tool_calls: [{ id: "c1", name: "shell", arguments: '{"command":"nmap"}' }],
        branch: 0,
      },
      {
        ts: "t",
        seq: 4,
        code: "x",
        kind: "tool",
        tool_call_id: "c1",
        result: "open 80",
        branch: 0,
      },
      {
        ts: "t",
        seq: 5,
        code: "x",
        kind: "user",
        branch: 1,
        kind_note: "fork_directive",
        parent_branch: 0,
        fork_seq: 4,
        fork_label: "redis 方向",
        content: "【分支 1 · 方向】你由分支 0 分出",
      },
      {
        ts: "t",
        seq: 6,
        code: "x",
        kind: "assistant",
        branch: 1,
        output: "看 redis",
        tool_calls: [{ id: "c2", name: "shell", arguments: '{"command":"redis-cli"}' }],
      },
      { ts: "t", seq: 7, code: "x", kind: "tool", branch: 1, tool_call_id: "c2", result: "PONG" },
      // fork 之后 trunk 继续跑的轮次（不得进入分支 1 的重建）
      {
        ts: "t",
        seq: 8,
        code: "x",
        kind: "assistant",
        branch: 0,
        output: "继续 web",
        tool_calls: [{ id: "c3", name: "shell", arguments: '{"command":"curl"}' }],
      },
      { ts: "t", seq: 9, code: "x", kind: "tool", branch: 0, tool_call_id: "c3", result: "200" },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    return file;
  }

  it("trunk 重建排除其他分支记录", () => {
    const file = writeConv();
    const trunk = rebuildMessages(file);
    expect(trunk.length).toBe(6); // seq 1,2,3,4,8,9（分支 1 的 5,6,7 被排除）
    expect(trunk.filter((m) => m.role === "tool").length).toBe(2);
    expect(trunk.some((m) => String(m.content).includes("看 redis"))).toBe(false);
  });

  it("分支重建 = 父分支 fork_seq 快照 + 分支自身记录（fork 后父轮次不进入）", () => {
    const file = writeConv();
    const branches = rebuildBranches(file);
    expect(branches.length).toBe(1);
    const b1 = branches[0];
    expect(b1.id).toBe(1);
    expect(b1.parent).toBe(0);
    expect(b1.forkSeq).toBe(4);
    expect(b1.label).toBe("redis 方向");
    // sys+card+asst(c1)+tool(c1) = 4（seq 8/9 的 trunk 轮次不得出现）
    expect(b1.messages.length).toBe(4 + 1 /*directive*/ + 2 /*asst+tool*/);
    expect(b1.messages[0].role).toBe("system");
    expect(String(b1.messages[4].content)).toContain("分支 1 · 方向");
    expect(b1.messages.some((m) => String(m.content).includes("继续 web"))).toBe(false);
  });

  it("嵌套 fork：孙分支只拿祖父 fork_seq 内的快照", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "nest_rec_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "user",
        branch: 1,
        kind_note: "fork_directive",
        parent_branch: 0,
        fork_seq: 2,
        content: "【分支 1】",
      },
      { ts: "t", seq: 4, code: "x", kind: "assistant", branch: 1, output: "b1 work" },
      // fork 孙分支（从分支 1，fork_seq=4）之后分支 1 又跑了一轮
      {
        ts: "t",
        seq: 5,
        code: "x",
        kind: "user",
        branch: 2,
        kind_note: "fork_directive",
        parent_branch: 1,
        fork_seq: 4,
        content: "【分支 2】",
      },
      { ts: "t", seq: 6, code: "x", kind: "assistant", branch: 1, output: "b1 more（不该进 b2）" },
      { ts: "t", seq: 7, code: "x", kind: "assistant", branch: 2, output: "b2 work" },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    const branches = rebuildBranches(file);
    const b2 = branches.find((b) => b.id === 2)!;
    expect(b2.messages.some((m) => String(m.content).includes("b1 work"))).toBe(true);
    expect(b2.messages.some((m) => String(m.content).includes("不该进 b2"))).toBe(false);
    expect(b2.messages.at(-1)?.content).toBe("b2 work");
  });

  it("坏记录（孤儿 tool/半截 tool_calls）被裁剪", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bad_rec_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "assistant",
        branch: 0,
        tool_calls: [{ id: "cx", name: "shell", arguments: "{}" }], // 结果缺失
      },
      { ts: "t", seq: 4, code: "x", kind: "tool", tool_call_id: "orphan", result: "x", branch: 0 },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    const msgs = rebuildMessages(file);
    expect(msgs.length).toBe(2); // 半截 assistant 与孤儿 tool 都被裁掉
  });

  it("中段孤儿 assistant（tool_calls 无结果、其后还有别的消息）截断后续（防 400）", () => {
    // arena 事故形态：多写者交错/半截段重建 → 中段出现"有 tool_calls 无紧跟结果"的
    // assistant，旧逻辑只裁尾部 → 漏过去发给 DeepSeek → 400 abandoned。
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mid_orphan_"));
    const file = path.join(dir, "conversation.jsonl");
    const recs = [
      { ts: "t", seq: 1, code: "x", kind: "system", content: "sys", branch: 0 },
      { ts: "t", seq: 2, code: "x", kind: "user", content: "card", branch: 0 },
      {
        ts: "t",
        seq: 3,
        code: "x",
        kind: "assistant",
        branch: 0,
        tool_calls: [{ id: "cx", name: "shell", arguments: "{}" }], // 结果缺失（中段孤儿）
      },
      // 其后还有完整配对的正常轮次（旧逻辑会保留它们，导致 cx 悬空 → 400）
      {
        ts: "t",
        seq: 4,
        code: "x",
        kind: "assistant",
        branch: 0,
        output: "继续",
        tool_calls: [{ id: "cy", name: "shell", arguments: "{}" }],
      },
      { ts: "t", seq: 5, code: "x", kind: "tool", branch: 0, tool_call_id: "cy", result: "ok" },
      { ts: "t", seq: 6, code: "x", kind: "assistant", branch: 0, output: "总结" },
    ];
    fs.writeFileSync(file, recs.map((r) => JSON.stringify(r)).join("\n"));
    const msgs = rebuildMessages(file);
    // 从孤儿 assistant（seq 3）截断：只剩 sys + card
    expect(msgs.length).toBe(2);
    expect(msgs.at(-1)?.content).toBe("card");
  });
});

describe("旁观者越权建议检测（run5 真实判例集）", () => {
  it("题内合法建议放行", () => {
    // run5 [1][2][3]：题内换层面/换方法/纠错
    expect(
      isOverreachingAdvice(
        "别再换参数。关键在 VM：31 字节数据与 4 字节 key 的运算才是来源，动态 dump 中间值",
      ),
    ).toBe(false);
    expect(isOverreachingAdvice("B 的偏移可能错了：先核对真实文件偏移与段映射，再试解压")).toBe(
      false,
    );
    expect(isOverreachingAdvice("改攻 8080 端口的注入点")).toBe(false); // 题内换目标合法
  });

  it("换题类越权命中（run5 条4-12 形态）", () => {
    expect(isOverreachingAdvice("平台确认还有 web_sqli_01 未动，应转向该 SQLi 分支")).toBe(true);
    expect(isOverreachingAdvice("若 8899 无果，转 web_sqli_01。")).toBe(true);
    expect(isOverreachingAdvice("立即止损，转攻未动的 web_sqli_01：从注入点直接取库内 flag")).toBe(
      true,
    );
    expect(isOverreachingAdvice("停掉 f2-05 全部动作，转攻完全未动的题")).toBe(true);
    expect(isOverreachingAdvice("主分支已明确拒绝换题——继续观察")).toBe(true); // 含"换题"字样即触发重写（保守）
    expect(isOverreachingAdvice("直接调用平台 start 接口换题")).toBe(true);
  });

  it("输入尾锚定声明存在（buildBystanderInput 末尾）", () => {
    const input = buildBystanderInput({
      taskCard: "拿掉目标全部 flag",
      entries: [],
      branchId: 0,
      branchNames: new Map([[0, "主分支"]]),
      ledger: null,
      recentDigest: "",
      recentCmds: [],
      segElapsedSec: 10,
      signals: {
        flagStall: false,
        stall: false,
        repeat: false,
        hardFail: false,
        hallucSubmit: false,
        count: 0,
      },
    });
    expect(input).toContain("只准是当前题目内");
    expect(input).toContain("越权");
  });

  it("输入带「各分支分工」+ 硬约束禁止改派（界由 fork 划定）", () => {
    const input = buildBystanderInput({
      taskCard: "拿掉目标全部 flag",
      entries: [],
      branchId: 1,
      branchNames: new Map([
        [0, "主分支"],
        [1, "穷举 31 字节 blob 的流密码/LFSR"],
        [2, "服务端响应头与隐藏路径"],
      ]),
      ledger: null,
      recentDigest: "",
      recentCmds: [],
      segElapsedSec: 10,
      signals: {
        flagStall: false,
        stall: false,
        repeat: false,
        hardFail: false,
        hallucSubmit: false,
        count: 0,
      },
    });
    expect(input).toContain("各分支分工");
    expect(input).toContain("分支 0（主分支）：题面整体，无专属方向");
    expect(input).toContain("分支 1：穷举 31 字节 blob 的流密码/LFSR");
    expect(input).toContain("分支 2：服务端响应头与隐藏路径");
    // 系统硬约束：不得让已分工分支弃用方向/转做他人工作（实测教训回锁）
    expect(BYSTANDER_SYSTEM).toContain("不得重分配已分工的分支");
    expect(BYSTANDER_SYSTEM).toContain("服务它的方向");
    // 执行侧同样声明：界由 fork 划定、broadcast 只上行主分支
    expect(FORK_PROMPT_EXTRA).toContain("不要轻易跨界");
    expect(FORK_PROMPT_EXTRA).toContain("只上行给主分支");
  });
});
