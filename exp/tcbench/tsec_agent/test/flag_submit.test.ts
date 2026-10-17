/** submit_flag toolcall + 多 flag 总结压缩 的离线测试（防代码爆破 + 上下文压缩两新动作）。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { solveTask } from "../src/agent.js";
import { CFG } from "../src/config.js";
import { FlagLedger } from "../src/corelog.js";
import {
  CLOSING_SUMMARY_PROMPT,
  COMPACT_NOTE,
  COMPACTION_PROMPT,
  systemPromptWithExtras,
} from "../src/prompt.js";
import { initRecords, rec, resetRecords, setRecordsDir } from "../src/records.js";
import { dispatchShell } from "../src/shell_tool.js";
import { FLAG_SUMMARY_HINT_SINGLE, integrateSubmit, isSubmitCurl } from "../src/submit_flag.js";
import type { ChatMessage, LlmClient } from "../src/types.js";

const SNAP = { ...CFG };
beforeEach(() => {
  Object.assign(CFG, SNAP);
  CFG.model = "deepseek-flash";
});
afterEach(() => {
  Object.assign(CFG, SNAP);
  resetRecords();
  vi.unstubAllGlobals();
});

function tc(id: string, name: string, args: Record<string, unknown>) {
  return { id, type: "function" as const, function: { name, arguments: JSON.stringify(args) } };
}

describe("防代码爆破：curl submit 执行层拦截", () => {
  it("isSubmitCurl 命中提交形态、放过普通 curl", () => {
    expect(isSubmitCurl('curl -s -X POST http://x/openapi/v1/challenges/submit -d "{}"')).toBe(
      true,
    );
    expect(
      isSubmitCurl("curl -s http://target:80/ && curl http://p/openapi/v1/challenges/submit"),
    ).toBe(true);
    expect(isSubmitCurl("curl -sI http://172.17.0.1:9100/")).toBe(false);
    expect(isSubmitCurl("echo 'challenges/submit'")).toBe(false); // 非 curl
  });

  it("shell run 出口拒绝 curl 提交，指引 submit_flag", async () => {
    const out = String(
      await dispatchShell({
        action: "run",
        command:
          'curl -s -X POST -d \'{"flag":"x"}\' http://127.0.0.1:8899/openapi/v1/challenges/submit',
        wait_ms: 3000,
      }),
    );
    expect(out).toContain("error:");
    expect(out).toContain("submit_flag toolcall");
  });
});

describe("submitFlag 平台响应集成", () => {
  it("integrateSubmit：correct+idx → 新 flag/账本/总数；重复 idx 不重记", () => {
    const led = new FlagLedger();
    const out = integrateSubmit(
      { correct: true, matched_flag_index: 1, correct_flag_count: 1, total_flag_count: 2 },
      led,
      0,
    );
    expect(out.newFlag).toBe(true);
    expect(out.flagIndex).toBe(1);
    expect(out.totalCount).toBe(2);
    expect(led.count()).toBe(1);
    // 同 idx 再提交（duplicate 平台又回 correct）→ 不重复计
    const again = integrateSubmit(
      { correct: true, matched_flag_index: 1, correct_flag_count: 1, total_flag_count: 2 },
      led,
      0,
    );
    expect(again.newFlag).toBe(false);
    // 错误提交
    const bad = integrateSubmit({ correct: false, matched_flag_index: null }, led, 0);
    expect(bad.newFlag).toBe(false);
  });

  it("单 flag 收尾总结指示 + 压缩交接文档要求（治太简略/shell 无交接）", () => {
    expect(FLAG_SUMMARY_HINT_SINGLE).toContain("≤120");
    expect(FLAG_SUMMARY_HINT_SINGLE).toContain("不调用工具");
    expect(FLAG_SUMMARY_HINT_SINGLE).toContain("作为本题收尾");
    // 交接文档五节结构：凭据原值保留 + 产物文件清单 + 死路 + 进度 + flag 进度
    expect(COMPACTION_PROMPT).toContain("产物与在途");
    expect(COMPACTION_PROMPT).toContain("未完成的后台 shell");
    expect(COMPACTION_PROMPT).toContain("生命周期系统自管");
    expect(COMPACTION_PROMPT).toContain("原值保留");
    expect(COMPACTION_PROMPT).toContain("已证伪死路");
    expect(COMPACT_NOTE).toContain("{summary}");
    expect(COMPACT_NOTE).toContain("不要重新收集");
  });
});

describe("复盘精简（单 flag 收尾省输出）", () => {
  it("CLOSING_SUMMARY_PROMPT 一段 ≤150 字、含「工具」（e2e 兼容）", () => {
    expect(CLOSING_SUMMARY_PROMPT.length).toBeLessThan(400); // 原三段式 ~600+
    expect(CLOSING_SUMMARY_PROMPT).toContain("≤150 字");
    expect(CLOSING_SUMMARY_PROMPT).toContain("工具");
  });
});

describe("上下文压缩：通用交接文档（solveTask 集成）", () => {
  const readJsonl = (recdir: string, rel: string): Record<string, unknown>[] => {
    try {
      return fs
        .readFileSync(path.join(recdir, rel), "utf8")
        .split("\n")
        .filter(Boolean)
        .map((l) => JSON.parse(l) as Record<string, unknown>);
    } catch {
      return [];
    }
  };
  /** 按最后一条消息分流：压缩元调用（COMPACTION_PROMPT）→ 交接文档；任务轮 → 脚本序列。 */
  function makeClient(script: Array<Record<string, unknown>>, compactDoc: string) {
    let i = 0;
    return {
      client: {
        async create(opts: { messages: ChatMessage[]; tools?: unknown[] }) {
          const last = String(opts.messages.at(-1)?.content ?? "");
          if (last.includes("即将被压缩为一份")) {
            return { choices: [{ message: { role: "assistant", content: compactDoc } }] };
          }
          const r = script[Math.min(i, script.length - 1)];
          i++;
          return { choices: [{ message: { role: "assistant", ...r } }] };
        },
      } as LlmClient,
      taskCalls: () => i,
    };
  }
  const COMPACT_DOC =
    "交接：①已确认凭据 admin:Admin@123（/admin/login.php 登录成功）②产物 " +
    "/work/tasks/t-c2/ffuf.log（目录爆破结果）③死路：news.php 无注入（intval 判定）" +
    "④进度：后台已登录待枚举 ⑤flag 进度 0/2";

  it("多 flag：找到 flag 即压缩交接（主触发，阈值不参与）+ 最后 flag 响应即收束", async () => {
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "compact_"));
    setRecordsDir(recdir);
    resetRecords();
    initRecords("local", "deepseek-flash");
    let submitN = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        submitN++;
        return new Response(
          JSON.stringify({
            correct: true,
            matched_flag_index: submitN - 1,
            correct_flag_count: submitN,
            total_flag_count: 2,
          }),
          { status: 200 },
        );
      }),
    );
    CFG.benchmark_token = "t";
    CFG.benchmark_base_url = "http://bench.test";
    const { client, taskCalls } = makeClient(
      [
        // 任务轮 1：submit flag#0（1/2）→ 找到 flag → 回合末强制压缩交接
        { content: null, tool_calls: [tc("s2", "submit_flag", { flag: "flag{aaa}" })] },
        // 任务轮 2：submit flag#1（2/2）→ 响应即收束（不等模型尾轮）
        { content: null, tool_calls: [tc("s3", "submit_flag", { flag: "flag{bbb}" })] },
      ],
      COMPACT_DOC,
    );
    const conv = rec().conversation("t-c2");
    const r = await solveTask(client, "t-c2", "卡", 600, conv, { flagTotal: 2 });
    expect(r.status).toBe("done");
    expect(r.turnsUsed).toBe(2); // 压缩是元调用，不占轮次
    // 压缩发生：末尾 = [head…, 交接文档, 最后 flag 的 submit, tool]；文档前无过程残留
    const notes = r.messages.filter((m) =>
      String(m.content ?? "").startsWith("【系统·上下文压缩】"),
    );
    expect(notes.length).toBeGreaterThanOrEqual(1);
    const lastNoteIdx = r.messages.lastIndexOf(notes.at(-1)!);
    expect(r.messages.slice(0, lastNoteIdx).some((m) => (m.tool_calls?.length ?? 0) > 0)).toBe(
      false,
    ); // 压缩点前的过程（轮1 shell）已被替换
    expect(r.messages.slice(lastNoteIdx + 1).some((m) => (m.tool_calls?.length ?? 0) > 0)).toBe(
      true,
    ); // 文档后是最后一次提交
    // conv 锚 + 提交记录 + corelog【压缩交接】卡（凭据原值不截断）
    const convRecs = readJsonl(recdir, "tasks/t-c2/conversation.jsonl");
    expect(convRecs.filter((x) => x.kind_note === "context_compact").length).toBe(1); // 仅 flag#0 后一次
    expect(convRecs.filter((x) => x.kind === "tool" && x.tool === "submit_flag").length).toBe(2);
    const coreRecs = readJsonl(recdir, "tasks/t-c2/log.jsonl");
    const handoff = coreRecs.filter((x) => String(x.note ?? "").startsWith("【压缩交接】"));
    expect(handoff.length).toBeGreaterThanOrEqual(1);
    expect(String(handoff[0].note)).toContain("Admin@123");
    // 重建等价（锚 = 最后一条 context_compact）
    const rebuilt = rec().loadMessages("t-c2");
    expect(rebuilt.map((m) => m.role + "|" + String(m.content ?? "").slice(0, 8))).toEqual(
      r.messages.map((m) => m.role + "|" + String(m.content ?? "").slice(0, 8)),
    );
    expect(submitN).toBe(2);
  });

  it("单 flag：默认阈值不压（无交接文档），收尾总结轮照旧", async () => {
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "compact1_"));
    setRecordsDir(recdir);
    resetRecords();
    initRecords("local", "deepseek-flash");
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              correct: true,
              matched_flag_index: 0,
              correct_flag_count: 1,
              total_flag_count: 1,
            }),
            { status: 200 },
          ),
      ),
    );
    CFG.benchmark_token = "t";
    CFG.benchmark_base_url = "http://bench.test";
    // 单 flag 脚本：submit → 收尾总结（无工具）
    const scripted: LlmClient = {
      async create(opts: { messages: ChatMessage[]; tools?: unknown[] }) {
        const last = String(opts.messages.at(-1)?.content ?? "");
        if (last.includes("即将被压缩为一份")) {
          throw new Error("单 flag 短上下文不应触发压缩");
        }
        const hasSubmitTool = opts.messages.some(
          (m) =>
            (m.tool_calls?.length ?? 0) > 0 &&
            String(m.tool_calls?.[0]?.function?.name) === "submit_flag",
        );
        if (!hasSubmitTool) {
          return {
            choices: [
              {
                message: {
                  role: "assistant",
                  content: null,
                  tool_calls: [tc("s1", "submit_flag", { flag: "flag{single}" })],
                },
              },
            ],
          };
        }
        return { choices: [{ message: { role: "assistant", content: "注入拿到，收束。" } }] };
      },
    } as LlmClient;
    const conv = rec().conversation("t-c1");
    const r = await solveTask(scripted, "t-c1", "卡", 600, conv, { flagTotal: 1 });
    expect(r.status).toBe("done");
    expect(r.flagSummaryDone).toBe(true);
    expect(r.messages.some((m) => String(m.content ?? "").startsWith("【系统·上下文压缩】"))).toBe(
      false,
    );
    const convRecs = readJsonl(recdir, "tasks/t-c1/conversation.jsonl");
    expect(convRecs.filter((x) => x.kind_note === "context_compact").length).toBe(0);
    expect(convRecs.filter((x) => x.kind_note === "flag_summary").length).toBe(1);
    // 过程完整保留（单 flag 短上下文不压缩）
    expect(rec().loadMessages("t-c1").length).toBe(r.messages.length);
  });

  it("阈值兜底：无 flag 也压（长 shell 越线）；自动黑板把进展叙述落卡", async () => {
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "compact3_"));
    setRecordsDir(recdir);
    resetRecords();
    initRecords("local", "deepseek-flash");
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              correct: true,
              matched_flag_index: 0,
              correct_flag_count: 1,
              total_flag_count: 1,
            }),
            { status: 200 },
          ),
      ),
    );
    CFG.benchmark_token = "t";
    CFG.benchmark_base_url = "http://bench.test";
    // token 兜底：轮 1 响应回报实测 600k（≥默认 500k）→ 轮 2 开头触发压缩；
    // 之后 usage 回落 20k → 不再压（压缩成功后计数清零，真实 usage 重建口径）
    const script = [
      {
        usage: { prompt_tokens: 600_000 },
        message: {
          role: "assistant",
          content: null,
          tool_calls: [tc("s1", "shell", { command: "echo recon-done", wait_ms: 3000 })],
        },
      },
      {
        usage: { prompt_tokens: 20_000 },
        message: {
          role: "assistant",
          content: null,
          tool_calls: [tc("s2", "submit_flag", { flag: "flag{single}" })],
        },
      },
      {
        usage: { prompt_tokens: 20_000 },
        message: { role: "assistant", content: "注入拿到，本题收束：后台弱口令已验证。" },
      },
    ];
    let i = 0;
    const client = {
      async create(opts: { messages: ChatMessage[]; tools?: unknown[] }) {
        const last = String(opts.messages.at(-1)?.content ?? "");
        if (last.includes("即将被压缩为一份")) {
          return { choices: [{ message: { role: "assistant", content: COMPACT_DOC } }] };
        }
        const e = script[Math.min(i, script.length - 1)];
        i++;
        return { choices: [{ message: e.message }], usage: e.usage };
      },
    } as LlmClient;
    const conv = rec().conversation("t-c3");
    const r = await solveTask(client, "t-c3", "卡", 600, conv, { flagTotal: 1 });
    expect(r.status).toBe("done");
    expect(r.flagSummaryDone).toBe(true);
    expect(r.turnsUsed).toBe(3); // 阈值压缩发生在轮 2 开头（元调用不占轮次）
    const convRecs = readJsonl(recdir, "tasks/t-c3/conversation.jsonl");
    // 紧阈值下每个轮起点都可能再压（元调用不占轮次）——兜底路径触发即可，次数不作断言
    expect(convRecs.filter((x) => x.kind_note === "context_compact").length).toBeGreaterThanOrEqual(
      1,
    );
    expect(convRecs.filter((x) => x.kind_note === "flag_summary").length).toBe(1);
    // 自动黑板：进展叙述（≥8 字 content）落卡；【压缩交接】卡同步
    const core = readJsonl(recdir, "tasks/t-c3/log.jsonl");
    expect(core.some((x) => String(x.note ?? "").includes("注入拿到"))).toBe(true);
    expect(core.some((x) => String(x.note ?? "").startsWith("【压缩交接】"))).toBe(true);
  });
});
