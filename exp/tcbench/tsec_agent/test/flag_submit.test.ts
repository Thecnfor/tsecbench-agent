/** submit_flag toolcall + 多 flag 总结压缩 的离线测试（防代码爆破 + 上下文压缩两新动作）。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { solveTask } from "../src/agent.js";
import { CFG } from "../src/config.js";
import { FlagLedger } from "../src/corelog.js";
import { CLOSING_SUMMARY_PROMPT, FLAG_CONTINUE_NOTE } from "../src/prompt.js";
import { initRecords, rec, resetRecords, setRecordsDir } from "../src/records.js";
import { dispatchShell } from "../src/shell_tool.js";
import {
  FLAG_SUMMARY_HINT_MULTI,
  FLAG_SUMMARY_HINT_SINGLE,
  integrateSubmit,
  isSubmitCurl,
} from "../src/submit_flag.js";
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

  it("两版总结指示：多 flag（压缩+继续）/ 单 flag（即收尾）", () => {
    expect(FLAG_SUMMARY_HINT_MULTI).toContain("≤120");
    expect(FLAG_SUMMARY_HINT_MULTI).toContain("不调用工具");
    expect(FLAG_SUMMARY_HINT_MULTI).toContain("压缩");
    expect(FLAG_SUMMARY_HINT_MULTI).toContain("继续下一个 flag");
    expect(FLAG_SUMMARY_HINT_SINGLE).toContain("≤120");
    expect(FLAG_SUMMARY_HINT_SINGLE).toContain("不调用工具");
    expect(FLAG_SUMMARY_HINT_SINGLE).toContain("作为本题收尾");
    expect(FLAG_SUMMARY_HINT_SINGLE).not.toContain("压缩");
  });
});

describe("复盘精简（单 flag 收尾省输出）", () => {
  it("CLOSING_SUMMARY_PROMPT 一段 ≤150 字、含「工具」（e2e 兼容）", () => {
    expect(CLOSING_SUMMARY_PROMPT.length).toBeLessThan(400); // 原三段式 ~600+
    expect(CLOSING_SUMMARY_PROMPT).toContain("≤150 字");
    expect(CLOSING_SUMMARY_PROMPT).toContain("工具");
  });
});

describe("多 flag 总结压缩（solveTask 集成）", () => {
  /** submit 响应脚本 + 总结轮配合的假 client。 */
  function scriptedClient(script: Array<(msgs: ChatMessage[], tools: boolean) => any>) {
    let i = 0;
    return {
      client: {
        async create(opts: { messages: ChatMessage[]; tools?: unknown[] }) {
          const r = script[i % script.length];
          i++;
          return {
            choices: [{ message: { role: "assistant", ...r(opts.messages, !!opts.tools) } }],
          };
        },
      } as LlmClient,
      calls: () => i,
    };
  }

  it("双 flag：submit→总结轮→压缩→继续→第二 flag→正常收尾；rebuild 等价", async () => {
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "compact_"));
    setRecordsDir(recdir);
    resetRecords();
    initRecords("local", "deepseek-flash");
    // 平台 API stub：只 submitFlag 走 fetch
    const submitResp = (idx: number, cnt: number) =>
      new Response(
        JSON.stringify({
          correct: true,
          matched_flag_index: idx,
          correct_flag_count: cnt,
          total_flag_count: 2,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    let submitN = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        submitN++;
        return submitResp(submitN - 1, submitN);
      }),
    );
    CFG.benchmark_token = "t";
    CFG.benchmark_base_url = "http://bench.test";

    const { client } = scriptedClient([
      // 轮1：submit flag#0 → correct 1/2 → 带总结指示
      () => ({ content: null, tool_calls: [tc("s1", "submit_flag", { flag: "flag{aaa}" })] }),
      // 轮2：配合输出总结（无 tool）
      () => ({ content: "走登录注入拿到 flag0：弱口令 admin 注入进后台。" }),
      // 轮3：submit flag#1 → correct 2/2 → 无总结指示（全部完成）
      () => ({ content: null, tool_calls: [tc("s2", "submit_flag", { flag: "flag{bbb}" })] }),
      // 轮4：收尾（无 pending → 普通 park → done）
      () => ({ content: "两枚 flag 均已提交，收束。" }),
    ]);
    const conv = rec().conversation("t-c2");
    const r = await solveTask(client, "t-c2", "卡", 600, conv, { flagTotal: 2 });

    expect(r.status).toBe("done");
    // 立即收束（衔接优化）：最后 flag 的 submit 响应即权威，回合末直接 done——
    // 模型尾轮（脚本第 4 条）不被消耗，不再等 watcher 缓存
    expect(r.turnsUsed).toBe(3);
    expect(r.messages.some((m) => String(m.content ?? "").includes("两枚 flag"))).toBe(false);
    expect(r.messages[0].role).toBe("system");
    expect(r.messages.some((m) => String(m.content ?? "") === FLAG_CONTINUE_NOTE)).toBe(true);
    // 压缩边界：总结 assistant 在头之后、继续指令之前，其后才是轮3 的 submit（tool_calls）
    const contIdx = r.messages.findIndex((m) => String(m.content ?? "") === FLAG_CONTINUE_NOTE);
    const sumIdx = r.messages.findIndex(
      (m, i) =>
        i >= 2 &&
        m.role === "assistant" &&
        !m.tool_calls?.length &&
        String(m.content ?? "").includes("登录注入"),
    );
    expect(sumIdx).toBeGreaterThan(1);
    expect(contIdx).toBe(sumIdx + 1);
    const firstToolAssistant = r.messages.findIndex(
      (m) => m.role === "assistant" && (m.tool_calls?.length ?? 0) > 0,
    );
    expect(firstToolAssistant).toBeGreaterThan(contIdx); // 轮1 的 submit 已被压掉，轮3 的在继续之后
    // 中间探索过程（轮1 的 submit assistant 不在头部后……）——等价断言走 rebuild
    const rebuilt = rec().loadMessages("t-c2");
    expect(rebuilt.map((m) => `${m.role}|${String(m.content ?? "").slice(0, 10)}`)).toEqual(
      r.messages.map((m) => `${m.role}|${String(m.content ?? "").slice(0, 10)}`),
    );
    // conv：flag_summary 恰 1、flag_continue 恰 1（第二 flag 无总结=设计）
    const convRecs = fs
      .readFileSync(path.join(recdir, "tasks", "t-c2", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convRecs.filter((x) => x.kind_note === "flag_summary").length).toBe(1);
    expect(convRecs.filter((x) => x.kind_note === "flag_continue").length).toBe(1);
    expect(convRecs.filter((x) => x.kind === "tool" && x.tool === "submit_flag").length).toBe(2);
    expect(submitN).toBe(2); // 平台只被调了2次（结构化通道）
  });

  it("单 flag：不压（无总结轮/无继续指令），收尾走精简复盘语义", async () => {
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
    const { client } = scriptedClient([
      () => ({ content: null, tool_calls: [tc("s1", "submit_flag", { flag: "flag{single}" })] }),
      () => ({ content: "注入拿到，收束。" }),
    ]);
    const conv = rec().conversation("t-c1");
    const r = await solveTask(client, "t-c1", "卡", 600, conv, { flagTotal: 1 });
    expect(r.status).toBe("done");
    // 单 flag：同样产出总结（机制一致），但不执行替换——无继续指令、原文保留
    expect(r.flagSummaryDone).toBe(true);
    expect(r.messages.some((m) => String(m.content ?? "") === FLAG_CONTINUE_NOTE)).toBe(false);
    expect(r.messages.some((m) => m.role === "assistant" && (m.tool_calls?.length ?? 0) > 0)).toBe(
      true,
    ); // submit 的过程 assistant 仍在（原文未被替换）
    const convRecs = fs
      .readFileSync(path.join(recdir, "tasks", "t-c1", "conversation.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as Record<string, unknown>);
    expect(convRecs.filter((x) => x.kind_note === "flag_summary").length).toBe(1);
    expect(convRecs.filter((x) => x.kind_note === "flag_continue").length).toBe(0);
    // 过程完整保留（单 flag 不压缩）；rebuild 仍等价（flag_summary 无 continue 锚 → 原样规则）
    expect(rec().loadMessages("t-c1").length).toBe(r.messages.length);
  });
});
