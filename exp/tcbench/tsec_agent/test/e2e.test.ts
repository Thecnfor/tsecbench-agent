/** 离线端到端自测（对应 selftest.py）：不依赖 VPN / 网关 / 平台，
 * 验证「控制器 + 单题独立上下文 + 分段解题 + 持久化记录」主逻辑。
 *
 * 用假的 LLM client 模拟两道题：
 *   题 t-01：第 1 轮发起 shell tool call，第 2 轮输出总结（模拟通关）
 *   题 t-02：每段第 1 轮直接输出总结（模拟"未通关就想收尾"）→ 关题暂停、带上下文续跑，
 *            连续 3 段仍不通关 → 判 fail（验证 paused→resume→abandon 链路与记录断言）
 * 平台层（bench.*）用内存 fake 替换（vi.mock）。
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import { CFG } from "../src/config.js";

// ---- 环境隔离（必须在 import agent/config 之前生效）----
const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "tsec_rec_"));
process.env.AGENT_MODE = "local";
process.env.AGENT_TASKS = "";
process.env.AGENT_TASK_FILTER = "";
process.env.AGENT_TASKS_FILE = "";
process.env.RECORDS_DIR = recdir;
process.env.AGENT_PLATFORM_WATCH = "0"; // 离线：不开平台轮询
process.env.VPN_GUARD = "0"; // 离线：不探测预检地址
process.env.AGENT_GIVEUP_MAX = "3";
process.env.AGENT_MAX_PARALLEL = "3";

// ---- 内存版平台（对应 selftest.py: install_fake_bench）----
const fake = vi.hoisted(() => {
  const chals: any[] = [
    {
      unique_code: "t-01",
      description: "简单 web",
      difficulty: "easy",
      level: 1,
      total_score: 100,
      flag_count: 1,
      correct_flag_count: 0,
      is_completed: false,
      container_status: "stopped",
      container_addr: [],
    },
    {
      unique_code: "t-02",
      description: "waf 题",
      difficulty: "medium",
      level: 1,
      total_score: 300,
      flag_count: 1,
      correct_flag_count: 0,
      is_completed: false,
      container_status: "stopped",
      container_addr: [],
    },
  ];
  const calls: Array<[string, string]> = [];
  const state = { t01_started: false };
  return { chals, calls, state };
});

vi.mock("../src/bench.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/bench.js")>();
  return {
    ...actual,
    fetchChallenges: async () => {
      // 模拟：t-01 被 start 后、解题期间 flag 提交成功
      if (fake.state.t01_started) {
        for (const c of fake.chals) {
          if (c.unique_code === "t-01") {
            c.is_completed = true;
            c.correct_flag_count = 1;
          }
        }
      }
      return fake.chals.map((c) => ({ ...c }));
    },
    startChallenge: async (code: string) => {
      fake.calls.push(["start", code]);
      if (code === "t-01") fake.state.t01_started = true;
      for (const c of fake.chals) {
        if (c.unique_code === code) {
          c.container_status = "available";
          c.container_addr = ["10.9.9.9:80"];
        }
      }
      return { unique_code: code, container_addr: ["10.9.9.9:80"] };
    },
    closeChallenge: async (code: string) => {
      fake.calls.push(["close", code]);
      for (const c of fake.chals) {
        if (c.unique_code === code) c.container_status = "stopped";
      }
      return { unique_code: code, closed: true };
    },
    releaseCompleted: async () => [] as string[],
  };
});

// ---- 假 LLM client（对应 selftest.py: make_fake_client）----
function makeFakeClient() {
  const state = { n: 0 };
  return {
    state,
    async create(opts: {
      model: string;
      messages: any[];
      tools?: unknown[];
      tool_choice?: string;
      temperature?: number;
      max_tokens?: number;
    }) {
      const msgs = opts.messages;
      const sysPrompt = String(msgs.find((m) => m.role === "system")?.content ?? "");
      expect(opts.model, "model missing").toBeTruthy();
      expect(sysPrompt, "system prompt missing").toBeTruthy();

      // 复盘总结轮：无 tools（useTools=false），返回总结文本
      if (opts.tools === undefined) {
        const lastUser = [...msgs].reverse().find((m) => m.role === "user")?.content ?? "";
        expect(String(lastUser)).toContain("工具");
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content:
                  "复盘：1. 思路：默认凭据进后台拿 flag。2. 工具困难：dirb 字典路径难记。3. 优化建议：预装 seclists 常用子集。",
                reasoning_content: "（假思考）先侦察再利用。",
              },
            },
          ],
        };
      }

      expect(JSON.stringify(opts.tools)).toContain("shell");
      // 校验消息序列：tool 消息必须紧跟带 tool_calls 的 assistant
      let last: any = null;
      for (const m of msgs) {
        if (m.role === "tool") {
          expect(last?.tool_calls, "orphan tool message").toBeTruthy();
        }
        last = m;
      }

      state.n++;
      // 上下文隔离验证：本会话的 user 消息只包含当前题
      const userMsg = String(msgs.find((m) => m.role === "user")?.content ?? "");
      if (userMsg.includes("t-02")) {
        // 第二题：直接总结（放弃）
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: "t-02 未能完成：卡在 WAF 绕过。",
                reasoning_content: "（假思考）先侦察再利用。",
              },
            },
          ],
        };
      }
      // 第一题：第 1 轮工具调用，第 2 轮总结
      const toolMsgs = msgs.filter((m) => m.role === "tool").length;
      if (toolMsgs === 0) {
        return {
          choices: [
            {
              message: {
                role: "assistant",
                content: null,
                reasoning_content: "（假思考）先侦察再利用。",
                tool_calls: [
                  {
                    id: "call_abc123",
                    type: "function",
                    function: {
                      name: "shell",
                      arguments:
                        '{"action": "run", "command": "echo flag{fake} > /tmp/f", "wait_ms": 500}',
                    },
                  },
                ],
              },
            },
          ],
        };
      }
      return {
        choices: [
          {
            message: {
              role: "assistant",
              content: "t-01 完成：提交 flag{fake} 成功。",
              reasoning_content: "（假思考）先侦察再利用。",
            },
          },
        ],
      };
    },
  };
}

function readJsonl(file: string): any[] {
  if (!fs.existsSync(file)) return [];
  return fs
    .readFileSync(file, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
}

describe("端到端：控制器 + 单题独立上下文 + 分段解题 + 记录", () => {
  it("完整跑分自测", async () => {
    const agent = await import("../src/agent.js");
    const records = await import("../src/records.js");

    const client = makeFakeClient();
    // 与 selftest.py 一致：CFG.resolve() 填充 model/base_url（AGENT_MODE=local → 零网络）
    const { resolveAsync } = await import("../src/config.js");
    await resolveAsync();
    expect(CFG.model, "config 未 resolve").toBeTruthy();

    const rc = await agent.run(client as any);

    // 断言：两题都被 start+close，t-01 通过 t-02 失败（允许失败重试导致的多次 start）
    const started = fake.calls.filter(([op]) => op === "start").map(([, c]) => c);
    const closed = fake.calls.filter(([op]) => op === "close").map(([, c]) => c);
    expect(rc).toBe(0);
    expect(started).toContain("t-01");
    expect(started).toContain("t-02");
    expect(closed).toContain("t-01");
    expect(closed).toContain("t-02");

    // ---- meta.json：run 起止齐全 ----
    const meta = JSON.parse(fs.readFileSync(path.join(recdir, "meta.json"), "utf8"));
    expect(meta.mode).toBe("local");
    expect(meta.model).toBeTruthy();
    expect(meta.runs[0].started_at).toBeTruthy();
    expect(meta.runs[0].finished_at).toBeTruthy();
    // 选题模式必须落盘（panel 据此把平台题数限定到选题范围；空 = 全部题目）
    expect("task_filter" in meta.runs[0]).toBe(true);

    // ---- tasks.jsonl：生命周期 ----
    const phases = new Map<string, Set<string>>();
    const taskRecs = readJsonl(path.join(recdir, "tasks.jsonl"));
    for (const r of taskRecs) {
      if (!phases.has(r.code)) phases.set(r.code, new Set());
      phases.get(r.code)?.add(r.phase);
      if (r.phase === "task_start") {
        expect(r.started_at).toBeTruthy();
        expect(r.target).toBeTruthy();
      }
      if (r.phase === "task_close") {
        expect(r.closed_at).toBeTruthy();
        expect(r.duration_sec).not.toBeUndefined();
        // paused = 分段解题：本轮到点关题保留上下文，稍后续跑（不判死）
        expect(["pass", "fail", "paused"]).toContain(r.outcome);
      }
    }
    expect(phases.get("t-01")?.has("task_start")).toBe(true);
    expect(phases.get("t-01")?.has("task_close")).toBe(true);

    // ---- conversation.jsonl：全量不裁剪 ----
    const convT01 = readJsonl(path.join(recdir, "tasks", "t-01", "conversation.jsonl"));
    const kinds = convT01.map((r) => r.kind);
    const sawFull = { thinking: false, output: false, tool_calls: false, result: false };
    for (const r of convT01) {
      // 甘特图数据：每条消息必须有成对起止时间戳 + duration_ms
      expect(typeof r.started_at).toBe("number");
      expect(typeof r.ended_at).toBe("number");
      expect(r.ended_at).toBeGreaterThanOrEqual(r.started_at);
      expect(typeof r.duration_ms).toBe("number");
      if (r.kind === "assistant") {
        expect(
          String(r.thinking ?? "").includes("先侦察") || String(r.thinking ?? "").length > 10,
          `thinking: ${r.thinking}`,
        ).toBe(true);
        expect(r.duration_ms).toBeGreaterThanOrEqual(0);
        if (r.thinking) sawFull.thinking = true;
        if (r.output) sawFull.output = true;
        if (r.tool_calls?.length) {
          sawFull.tool_calls = true;
          expect(
            r.tool_calls[0].arguments.includes('"echo flag{fake}"') ||
              r.tool_calls[0].arguments.includes("flag{fake}"),
          ).toBe(true);
        }
      }
      if (r.kind === "tool") {
        expect(r.ended_at).toBeGreaterThanOrEqual(r.started_at);
        expect(String(r.arguments?.command ?? "").includes("flag{fake}") || !!r.result).toBe(true);
        if (r.result) sawFull.result = true;
      }
      if (r.kind === "system") {
        expect(String(r.content).length).toBeGreaterThan(500); // system prompt 未截断
      }
    }
    for (const k of ["system", "user", "assistant", "tool"] as const) {
      expect(kinds).toContain(k);
    }
    expect(
      Object.values(sawFull).every(Boolean),
      `conversation missing: ${JSON.stringify(sawFull)}`,
    ).toBe(true);

    // 通关复盘：同一 jsonl 里有 closing_summary 标记的 assistant 条目（t-01 通关）
    const closing = convT01.filter(
      (r) => r.kind === "assistant" && r.kind_note === "closing_summary",
    );
    expect(closing.length).toBeGreaterThan(0);
    expect(String(closing[0].output)).toContain("优化建议");
    // t-02 未通关无复盘
    const convT02 = readJsonl(path.join(recdir, "tasks", "t-02", "conversation.jsonl"));
    expect(convT02.filter((r) => r.kind_note === "closing_summary").length).toBe(0);
    // 对话按题隔离
    expect(fs.existsSync(path.join(recdir, "tasks", "t-02", "conversation.jsonl"))).toBe(true);

    // ---- events.jsonl ----
    const evs = readJsonl(path.join(recdir, "events.jsonl"));
    const evTypes = evs.map((e) => e.type);
    expect(evTypes).toContain("run_start");
    expect(evTypes).toContain("run_finish");
    expect(evTypes).toContain("final_summary");
    // 分段解题：t-02 每次"未通关就收尾" → 无人等名额时原地续段（不 close/start），
    // 连续收尾达上限判 fail。
    expect(evTypes).toContain("giveup_continue");
    expect(evTypes).toContain("task_abandoned");
    const t02Events = evs.filter((e) => e.code === "t-02");
    expect(
      t02Events.some((e) => e.type === "task_abandoned" && e.status === "giveup_loop"),
      `t02 events: ${JSON.stringify(t02Events)}`,
    ).toBe(true);

    // 原地续段：t-02 的对话里应有"继续解题"通知；system + 任务卡只在首段写一次。
    expect(convT02.filter((r) => r.kind === "system").length).toBe(1);
    const resumeNotes = convT02.filter(
      (r) => r.kind === "user" && String(r.content ?? "").includes("继续解题"),
    );
    expect(resumeNotes.length).toBe(2); // 两次原地拉回，第 3 次达到 giveup_max 后 abandon
    expect(resumeNotes.every((r) => String(r.content).includes("10.9.9.9:80"))).toBe(true);
    const continues = evs.filter((e) => e.type === "giveup_continue" && e.code === "t-02");
    expect(continues.length).toBe(2);

    // 续跑时上下文从记录重建：断言重建结果自洽
    const rebuilt = records.rebuildMessages(
      path.join(recdir, "tasks", "t-02", "conversation.jsonl"),
    );
    expect(rebuilt.length).toBeGreaterThan(0);
    expect(rebuilt[0].role).toBe("system");
    for (let i = 0; i < rebuilt.length; i++) {
      const m = rebuilt[i];
      if (m.role === "assistant" && m.tool_calls?.length) {
        const ids = new Set(m.tool_calls.map((t) => t.id));
        const got = new Set(
          rebuilt
            .slice(i + 1)
            .filter((x) => x.role === "tool")
            .map((x) => x.tool_call_id),
        );
        if (i === rebuilt.length - 1) continue; // 尾部总结性 assistant（无后续）
        for (const id of ids) expect(got.has(id), `rebuild: orphan tool_calls at ${i}`).toBe(true);
      }
    }

    // tasks.jsonl：t-02 无人等名额时原地续段 → 只 start 一次，不 close/重启
    const t02Starts = taskRecs.filter((r) => r.phase === "task_start" && r.code === "t-02");
    expect(t02Starts.length).toBe(1);
    expect(t02Starts.some((r) => r.resume)).toBe(false);
  }, 300_000);
});
