#!/usr/bin/env node
/** TSec Benchmark 自动跑分 agent 主循环。
 *
 * - 模型：deepseek-flash（OpenAI 兼容网关；local/hosted 同名，hosted 走转写网关）
 * - 工具：仅一个 `shell`（异步后台执行），无 MCP、无 subagent。
 * - 流程：VPN 预检 → 拉题列表 → 逐题 start/渗透/submit/close → 总结。
 *
 * 用法：
 *   node dist/agent.js            # 运行到完成或超时
 *   node dist/agent.js --smoke    # 冒烟测试：仅验证网关连通 + shell 工具
 */
import fs from "node:fs";
import path from "node:path";
import { CFG, env, resolveAsync } from "./config.js";
import {
  CLOSING_SUMMARY_PROMPT,
  fill,
  formatTaskCard,
  GIVEUP_NOTE,
  RESUME_NOTE,
  SYSTEM_PROMPT,
} from "./prompt.js";
import { getTaskCwd, setTaskCwd } from "./shell.js";
import { dispatchShell, parseToolArguments } from "./shell_tool.js";

export { getTaskCwd, setTaskCwd };

import * as bench from "./bench.js";
import { PlatformError, TaskFinishedError } from "./bench.js";
import {
  callLlm,
  discoverGatewayKey,
  ExitSignal,
  keyMask,
  makeClient,
  setLog,
  setRemainingSec,
  setStopCheck,
  tryReauth,
} from "./llm.js";
import { initRecords, type RunRecords, rec } from "./records.js";
import type { Challenge, ChatMessage, ChatResponse, ContentPart, LlmClient } from "./types.js";

const LOG_PATH = "agent.log";

// 全局停止事件：平台任务结束（already finished 等终态）时置位，
// 调度器/worker/watcher 全部收敛退出，不再重试。
let STOP = false;
// 题目启动时间戳（code -> unix ts），用于 task_close 计算 duration
const TASK_START_TS = new Map<string, number>();

// --- VPN 运行时守卫：VPN 中途挂 → 暂停派发新题、worker 挂起等待 ---
// 不判死刑：VPN 恢复即自动继续；等待期间不烧轮次、不消耗 attempts。
let VPN_DOWN = false;

export function platformEnded(where: string, e: Error, results?: Record<string, string>): void {
  if (STOP) return;
  log(`platform task FINISHED (${where}): ${e.message} — stopping all agents/containers`);
  rec().event("platform_ended", { where, error: e.message });
  rec().runFinish({ reason: "platform_ended", detail: e.message, results: results ?? {} });
  STOP = true;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** 探测靶场预检地址（与 entrypoint 预检同一标准）。 */
async function vpnOk(timeoutMs = 4000): Promise<boolean> {
  try {
    const resp = await fetch(CFG.vpn_health_url, { signal: AbortSignal.timeout(timeoutMs) });
    const body = await resp.text();
    return body.includes('"status"') && body.includes('"ok"');
  } catch {
    return false;
  }
}

export function log(msg: string): void {
  const line = `[${new Date().toTimeString().slice(0, 8)}] ${msg}`;
  console.log(line);
  try {
    fs.appendFileSync(LOG_PATH, `${line}\n`);
  } catch {
    /* ignore */
  }
}
setLog(log);

const START_TS = Date.now() / 1000;
export function remainingSec(): number | null {
  if (CFG.deadline_sec <= 0) return null;
  return CFG.deadline_sec - (Date.now() / 1000 - START_TS);
}
setRemainingSec(remainingSec);
setStopCheck(() => STOP);

/** 平台后台轮询：题目容器生命周期与 flag 数变化写入 trace。 */
export function startPlatformWatcher(intervalSec = 20): void {
  if (["0", "off", "false"].includes(env("AGENT_PLATFORM_WATCH", "1"))) {
    rec().event("platform_watcher_disabled", {});
    return;
  }
  let prev = new Map<string, Challenge>();
  let first = true;
  const tick = async () => {
    if (STOP) return;
    let items: Challenge[] | null = null;
    try {
      items = await bench.fetchChallenges();
    } catch (e) {
      if (e instanceof TaskFinishedError) {
        platformEnded("watcher", e);
        return;
      }
      rec().event("platform_poll_error", {
        error: `${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`,
      });
      return;
    }
    if (!items) return;
    const cur = new Map(items.map((c) => [c.unique_code, c]));
    if (first) {
      rec().event("platform_snapshot", {
        total: cur.size,
        completed: [...cur.values()].filter((v) => v.is_completed).map((v) => v.unique_code),
      });
      first = false;
    }
    for (const [code, c] of cur) {
      const p = prev.get(code);
      if (!p) continue;
      if (p.container_status !== c.container_status) {
        rec().event("challenge_change", {
          code,
          field: "container_status",
          old: p.container_status,
          new: c.container_status,
        });
      }
      if (p.correct_flag_count !== c.correct_flag_count) {
        rec().event("challenge_change", {
          code,
          field: "flags",
          old: `${p.correct_flag_count}/${p.flag_count}`,
          new: `${c.correct_flag_count}/${c.flag_count}`,
          completed: c.is_completed,
        });
      }
      if (p.is_completed !== c.is_completed) {
        rec().event("challenge_change", {
          code,
          field: "is_completed",
          old: p.is_completed,
          new: c.is_completed,
        });
      }
    }
    prev = cur;
  };
  const timer = setInterval(async () => {
    await tick();
  }, intervalSec * 1000);
  timer.unref?.();
  rec().event("platform_watcher_started", { interval_sec: intervalSec });
}

/** 验证：网关连通、模型可用、shell 工具可用、VPN 预检、平台题库可达。 */
export async function smokeTest(client: LlmClient): Promise<number> {
  let ok = true;

  // 1. 模型网关（hosted：先跑 key 候选发现；local：直连官方 API，失败时 401 热切换兜底）
  if (CFG.mode === "hosted") {
    const nc = await discoverGatewayKey("smoke", undefined, true);
    if (nc) {
      client = nc;
      log(
        `model gateway OK (key selected; see key probe lines above), active key=${keyMask(CFG.llm_api_key)}`,
      );
    } else {
      log(
        "model gateway FAILED: 网关拒绝了全部 key 候选——托管需在平台环境变量 " +
          "DEEPSEEK_API_KEY 注入**真实 DeepSeek key**（内置 tsec-bench 是上届约定，" +
          "本届转写网关透传上游鉴权；匿名也被拒说明网关强制鉴权）",
      );
      ok = false;
    }
  } else {
    try {
      const r = await client.create({
        model: CFG.model,
        messages: [{ role: "user", content: "回复两个字：正常" }],
        max_tokens: 512,
      });
      log(`model gateway OK: ${JSON.stringify(r.choices[0].message.content)}`);
    } catch (e) {
      const nc = await tryReauth(e);
      if (nc) {
        try {
          const r = await nc.create({
            model: CFG.model,
            messages: [{ role: "user", content: "回复两个字：正常" }],
            max_tokens: 512,
          });
          log(
            `model gateway OK (after key switch): ${JSON.stringify(r.choices[0].message.content)}`,
          );
        } catch (e2) {
          log(
            `model gateway FAILED (after key switch): ${e2 instanceof Error ? e2.name : "Error"}: ${(e2 as Error).message}`,
          );
          ok = false;
        }
      } else {
        log(
          `model gateway FAILED: ${e instanceof Error ? e.name : "Error"}: ${e instanceof Error ? e.message : e}`,
        );
        ok = false;
      }
    }
  }

  // 2. shell 工具
  const out = await dispatchShell({
    action: "run",
    command: "echo shell-ok && uname -m",
    wait_ms: 3000,
  });
  const outStr = String(out);
  if (outStr.includes("shell-ok")) {
    log(`shell tool OK: ${outStr.split("\n").filter(Boolean).pop()}`);
  } else {
    log(`shell tool FAILED: ${outStr}`);
    ok = false;
  }

  const vpnOut = String(
    await dispatchShell({
      action: "run",
      command: `curl -s -m 10 ${CFG.vpn_health_url}`,
      wait_ms: 12000,
    }),
  );
  const vpnOkPre = vpnOut.includes('"status"') && vpnOut.includes('"ok"');
  if (vpnOkPre) {
    log(`VPN precheck OK: ${vpnOut.trim().split("\n").filter(Boolean).pop()}`);
  } else if (CFG.mode === "hosted") {
    log(`VPN precheck FAILED (hosted mode is mandatory): ${vpnOut}`);
    ok = false;
  } else {
    // 本地模式：模型走公网，VPN 仅靶场渗透需要；开发调试可未连
    log(
      `VPN precheck not OK (local mode, non-fatal for smoke; ` +
        `run.sh/entrypoint enforces it before full runs): ${vpnOut.split("\n")[0] || "empty"}`,
    );
  }

  // 4. 平台题库可达性：任何 HTTP 响应（含 4xx 业务错误）都算网络可达；
  //    只有连接/DNS/超时失败才是"题库没访问"。
  if (CFG.benchmark_token) {
    try {
      const items = await bench.fetchChallenges();
      log(`platform API OK: ${items.length} challenges visible`);
    } catch (e) {
      if (e instanceof TaskFinishedError) {
        log("platform API reachable (task already finished)");
      } else if (e instanceof PlatformError) {
        log(`platform API reachable (business error at smoke, non-fatal): ${e.message}`);
      } else if (CFG.mode === "hosted") {
        log(
          `platform API UNREACHABLE (hosted, fatal): ${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`,
        );
        ok = false;
      } else {
        log(
          `platform API unreachable (local mode, non-fatal for smoke): ` +
            `${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`,
        );
      }
    }
  } else if (CFG.mode === "hosted") {
    log(
      "BENCHMARK_TOKEN empty in hosted mode — platform should inject it; " +
        "题库请求将全部 401/404，检查平台注入",
    );
    ok = false;
  } else {
    log("BENCHMARK_TOKEN empty (local mode, non-fatal for smoke)");
  }

  return ok ? 0 : 1;
}

/** LLM 上下文超限判定：这类错误重试/续跑都无意义，只能终止本题。 */
export function isContextOverflow(e: unknown): boolean {
  const text =
    `${e instanceof Error ? e.name : "Error"}: ${e instanceof Error ? e.message : e}`.toLowerCase();
  return [
    "context_length_exceeded",
    "context_length",
    "context window",
    "maximum context length",
    "maximum context window",
    "too many tokens",
    "reduce the length",
    "input length and `max_tokens`",
    "string too long",
    "prompt is too long",
  ].some((k) => text.includes(k));
}

/** 给修复后的工具结果加一行说明；结果是图片 parts 时改写首个 text 段。 */
export function withFixNote(res: unknown, fix: string): unknown {
  const note = `[参数已自动修复：${fix}] `;
  if (Array.isArray(res)) {
    const parts = res as ContentPart[];
    for (const part of parts) {
      if (part && typeof part === "object" && part.type === "text") {
        part.text = note + part.text;
        return parts;
      }
    }
    return [{ type: "text", text: note.trim() } as ContentPart, ...parts];
  }
  return note + String(res);
}

/** 把消息内容里的图片 part 剥掉（网关不支持图像时降级），返回移除的图片数。 */
export function stripImages(messages: ChatMessage[]): number {
  let removed = 0;
  for (const m of messages) {
    const content = m.content;
    if (!Array.isArray(content)) continue;
    const keep: ContentPart[] = [];
    let had = false;
    for (const part of content) {
      if (part && typeof part === "object" && part.type.startsWith("image")) {
        removed++;
        had = true;
        continue;
      }
      keep.push(part);
    }
    if (had) {
      keep.unshift({
        type: "text",
        text: "[网关不支持图像输入：本条图片已移除，请改用上一条文本里的OCR/元数据，或用 shell 分析原文件]",
      });
      m.content = keep;
    }
  }
  return removed;
}

function firstText(res: unknown): string {
  if (typeof res === "string") return res;
  if (Array.isArray(res)) {
    for (const p of res as ContentPart[]) {
      if (p && typeof p === "object" && p.type === "text") return p.text ?? "";
    }
  }
  return "";
}

export type SolveStatus =
  | "done"
  | "paused"
  | "stopped"
  | "vpn_abandon"
  | "deadline"
  | "llm_error"
  | "context_overflow";

export interface SolveResult {
  status: SolveStatus;
  lastContent: string;
  turnsUsed: number;
  messages: ChatMessage[];
}

/** 单题分段解题：本段直到模型输出最终总结（无工具调用）或本段轮次预算到点。
 *
 * status: done(模型输出无工具总结) / paused(本段预算到点) / stopped(平台终态) /
 * vpn_abandon / deadline / llm_error / context_overflow
 * messages/startTurn：续跑时传入重建好的上下文与轮次偏移。
 * 对话全量、忠实落盘（conversation.jsonl）。
 */
export async function solveTask(
  client: LlmClient,
  code: string,
  taskCard: string,
  budget: number,
  conv: RunRecords["conversation"] extends (c: string) => infer R ? R : never,
  opts: { messages?: ChatMessage[]; startTurn?: number } = {},
): Promise<SolveResult> {
  let messages: ChatMessage[];
  if (!opts.messages) {
    messages = [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: taskCard },
    ];
    // 忠实记录本题输入（system + 任务卡）
    conv.write("system", { turn: `${code}:0`, content: SYSTEM_PROMPT });
    conv.write("user", { turn: `${code}:0`, content: taskCard });
  } else {
    messages = opts.messages;
  }
  const startTurn = opts.startTurn ?? 0;
  let lastContent = "";
  let turnsUsed = 0;
  let turn = 0;
  while (turn < budget) {
    if (STOP) return { status: "stopped", lastContent, turnsUsed, messages };
    // VPN 守卫：VPN 挂起 → 本题挂起等待恢复（不烧轮次预算），恢复后原地继续。
    // 等待超过 AGENT_VPN_WAIT_MAX（默认 600s）仍未恢复才放弃本题。
    if (VPN_DOWN) {
      const waitMax = Number(env("AGENT_VPN_WAIT_MAX", "600"));
      const t0 = Date.now() / 1000;
      conv.write("note", {
        turn: `${code}:${turn}`,
        content: `VPN down detected; suspending task (max ${waitMax.toFixed(0)}s)`,
      });
      log(`[${code}] VPN down; task suspended (waiting for recovery)`);
      while (VPN_DOWN && !STOP && Date.now() / 1000 - t0 < waitMax) {
        await sleep(3000);
      }
      const waited = Date.now() / 1000 - t0;
      if (VPN_DOWN) {
        conv.write("error", {
          turn: `${code}:${turn}`,
          error: `VPN still down after ${waited.toFixed(0)}s; abandoning task`,
        });
        log(`[${code}] VPN still down after ${waited.toFixed(0)}s; abandoning`);
        return { status: "vpn_abandon", lastContent, turnsUsed, messages };
      }
      conv.write("note", {
        turn: `${code}:${turn}`,
        content: `VPN recovered after ${waited.toFixed(0)}s; resuming`,
      });
      log(`[${code}] VPN recovered after ${waited.toFixed(0)}s; resuming task`);
      continue; // 恢复后从当前 turn 继续（不烧预算）
    }
    const rem = remainingSec();
    if (rem !== null && rem <= 0) return { status: "deadline", lastContent, turnsUsed, messages };
    turn++;
    const tLlmStart = Date.now() / 1000;
    let resp: ChatResponse | null = null;
    let err: unknown = null;
    try {
      resp = await callLlm(client, messages);
    } catch (e) {
      err = e;
      // 多模态降级：网关拒收图片 → 剥掉 image part 改发纯文本，本会话后续不再附图
      const dropped = CFG.vision_ok !== false ? stripImages(messages) : 0;
      if (dropped) {
        CFG.vision_ok = false;
        log(
          `[${code}] gateway rejected image input (${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}); ` +
            `dropped ${dropped} image(s), retrying text-only`,
        );
        conv.write("note", {
          turn: `${code}:${turn}`,
          content: `网关拒图，降级为文本模式：${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`,
        });
        rec().event("vision_degraded", {
          code,
          dropped,
          error: `${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`.slice(0, 400),
        });
        try {
          resp = await callLlm(client, messages);
        } catch (e2) {
          err = e2;
        }
      }
      if (!resp) {
        const emsg = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
        log(`[${code}] fatal LLM error: ${emsg}`);
        conv.write("error", { turn: `${code}:${turn}`, error: emsg, started_at: tLlmStart });
        const status: SolveStatus = isContextOverflow(err) ? "context_overflow" : "llm_error";
        return { status, lastContent, turnsUsed, messages };
      }
    }
    turnsUsed++;
    const tLlmEnd = Date.now() / 1000;

    const msg = resp.choices[0].message;
    const tlabel = `${code}:${startTurn + turn}`;
    const toolCallsRaw = msg.tool_calls ?? [];
    // AI 输出全量落盘：thinking / output / tool_calls 原文（started_at/ended_at = LLM 调用起止）
    conv.write("assistant", {
      turn: tlabel,
      thinking: msg.reasoning_content ?? null,
      output: msg.content ?? null,
      tool_calls: toolCallsRaw.map((tc) => ({
        id: tc.id,
        name: tc.function.name,
        arguments: tc.function.arguments,
      })),
      usage: resp.usage ?? null,
      started_at: tLlmStart,
      ended_at: tLlmEnd,
    });
    if (msg.content) lastContent = msg.content;

    const clean: ChatMessage = { role: "assistant" as const };
    if (msg.content) clean.content = msg.content;
    if (toolCallsRaw.length) {
      clean.tool_calls = toolCallsRaw.map((tc) => ({
        id: tc.id,
        type: "function" as const,
        function: { name: tc.function.name, arguments: tc.function.arguments },
      }));
    }
    messages.push(clean);

    if (!toolCallsRaw.length) {
      // 本题上下文的最终总结 → 交还控制器（是否通关由控制器查平台）
      return { status: "done", lastContent: msg.content || "", turnsUsed, messages };
    }

    for (const tc of toolCallsRaw) {
      let args: Record<string, unknown> = {};
      const rawArgs = tc.function.arguments || "{}";
      const tToolStart = Date.now() / 1000;
      let result: unknown;
      if (tc.function.name === "shell") {
        const [parsed, fix] = parseToolArguments(rawArgs);
        if (parsed === null) {
          result = `error: ${fix || "invalid tool arguments"}`;
        } else {
          args = parsed;
          if (fix) {
            // 模型把参数序列化坏了：直接修复执行，避免整轮作废；原样记录在 assistant.tool_calls 里
            log(`[${code}] tool args repaired: ${fix}`);
            rec().event("tool_arg_repaired", {
              code,
              tool_call_id: tc.id,
              fix,
              raw: rawArgs.slice(0, 400),
            });
            result = withFixNote(await dispatchShell(args), fix);
          } else {
            result = await dispatchShell(args);
          }
        }
      } else {
        result = `error: unknown tool ${JSON.stringify(tc.function.name)}`;
      }
      const tToolEnd = Date.now() / 1000;
      // 工具结果全量落盘（与 assistant 条目通过 tool_call_id 关联；起止 = 工具执行）
      conv.write("tool", {
        turn: tlabel,
        tool_call_id: tc.id,
        tool: tc.function.name,
        arguments: args,
        result,
        started_at: tToolStart,
        ended_at: tToolEnd,
      });
      const first = firstText(result);
      log(
        `tool#${tc.id.slice(-6)} ${tc.function.name} -> ` +
          `${first ? (first.split("\n")[0] || "").slice(0, 160) : ""}`,
      );
      messages.push({ role: "tool", tool_call_id: tc.id, content: result as string });
    }
  }

  log(
    `[${code}] segment turn budget (${budget}) reached ` +
      `(total ${startTurn + turn} turns) — pausing, context kept`,
  );
  return { status: "paused", lastContent, turnsUsed, messages };
}

/** 通关后复盘：在同一题上下文里追加一条总结请求（无工具），回复作为普通消息
 * 与其它对话一样忠实写入 conversation.jsonl。返回总结文本。 */
export async function closingSummary(
  client: LlmClient,
  code: string,
  messages: ChatMessage[],
  conv: RunRecords["conversation"] extends (c: string) => infer R ? R : never,
): Promise<string> {
  messages.push({ role: "user", content: CLOSING_SUMMARY_PROMPT });
  conv.write("user", { turn: `${code}:closing`, content: CLOSING_SUMMARY_PROMPT });
  const tLlmStart = Date.now() / 1000;
  let resp: ChatResponse;
  try {
    resp = await callLlm(client, messages, false);
  } catch (e) {
    log(
      `[${code}] closing summary LLM error: ${e instanceof Error ? `${e.name}: ${e.message}` : e}`,
    );
    conv.write("error", {
      turn: `${code}:closing`,
      error: e instanceof Error ? `${e.name}: ${e.message}` : String(e),
      started_at: tLlmStart,
    });
    return "";
  }
  const tLlmEnd = Date.now() / 1000;
  const msg = resp.choices[0].message;
  messages.push({ role: "assistant", content: msg.content ?? "" });
  conv.write("assistant", {
    turn: `${code}:closing`,
    thinking: msg.reasoning_content ?? null,
    output: msg.content ?? null,
    kind_note: "closing_summary", // 标记本条为通关复盘
    usage: resp.usage ?? null,
    started_at: tLlmStart,
    ended_at: tLlmEnd,
  });
  log(`[${code}] closing summary recorded (${(msg.content || "").length} chars)`);
  return msg.content || "";
}

function taskTarget(state: { container_addr?: string[] | string | null } | null): string {
  const addr = state?.container_addr ?? [];
  if (typeof addr === "string") return addr;
  return Array.isArray(addr) && addr.length ? addr[0] : "";
}

/** 控制器主循环：选题 → start → 单题独立上下文解题 → 校验 → close → 下一题。 */
export async function run(client: LlmClient): Promise<number> {
  const R = initRecords(CFG.mode, CFG.model);
  STOP = false; // 进程复用场景下重置终态标记
  // 只解部分题目（测试用）：优先级 --task/AGENT_TASK_FILTER > AGENT_TASKS > 配置默认
  const rawSel = env("AGENT_TASK_FILTER", "").trim() || env("AGENT_TASKS", "").trim() || CFG.tasks;
  const taskFilter = selectTasks(rawSel);
  if (taskFilter.length) log(`task selection: ${taskFilter.join(", ")}（其余题目跳过）`);
  else log("task selection: 全部题目");
  R.event("run_start", {
    mode: CFG.mode,
    model: CFG.model,
    max_turns: CFG.max_turns,
    deadline_sec: CFG.deadline_sec,
    task_filter: taskFilter,
  });
  // task_filter 落盘：panel 用它把平台统计限定到选题范围
  R.runStart({
    mode: CFG.mode,
    model: CFG.model,
    max_turns: CFG.max_turns,
    task_filter: taskFilter,
  });
  log(`records dir: ${R.dir}`);
  startPlatformWatcher();
  const taskBudget = CFG.task_max_turns;
  const results: Record<string, string> = {}; // code -> pass/fail；暂停题不写
  const attempts: Record<string, number> = {};

  const snapshot = async (): Promise<Challenge[] | null> => {
    try {
      return await bench.fetchChallenges();
    } catch (e) {
      if (e instanceof TaskFinishedError) {
        platformEnded("snapshot", e, results);
        return null;
      }
      if (e instanceof PlatformError && e.code === "task_not_found") throw e;
      if (e instanceof PlatformError) {
        log(`platform fetch error: ${e.message}`);
        rec().event("platform_poll_error", { error: e.message });
      } else {
        log(
          `platform fetch error: ${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`,
        );
        rec().event("platform_poll_error", {
          error: `${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`,
        });
      }
      return null;
    }
  };

  // 并行调度：每题独立 async 任务 + 独立上下文 + 独立工作目录
  try {
    await runParallel(client, taskBudget, taskFilter, results, attempts, snapshot);
  } catch (e) {
    // 调度层任何未预期异常都不允许"静默死亡"：落日志并收尾
    const emsg = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
    rec().event("run_finish", { reason: "controller_error", error: emsg });
    rec().runFinish({ reason: "controller_error", error: emsg, results });
    log(`controller error: ${emsg}`);
    console.error(e);
    process.exitCode = 4;
    return 4;
  }

  const finalItems = await snapshot();
  printSummary(results, finalItems);
  if (STOP) {
    // platformEnded 已写过 runFinish，这里只补最终汇总事件
    rec().event("final_summary", { note: "after platform ended", results });
  } else {
    rec().event("run_finish", { reason: "controller_done", results });
    rec().runFinish({ reason: "controller_done", results });
  }
  return 0;
}

/** AGENT_TASKS / AGENT_TASK_FILTER → 模式列表（逗号或空白分隔，支持 * ? 通配）。 */
export function selectTasks(raw: string): string[] {
  return (raw || "")
    .replace(/[,\s]+/g, " ")
    .split(" ")
    .filter(Boolean);
}

/** glob 通配匹配（fnmatch case-sensitive 等价：* 与 ?）。 */
export function globMatch(pattern: string, s: string): boolean {
  const esc = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${esc}$`).test(s);
}

/** 题目编号是否在选择范围内；patterns 为空表示全部选中。 */
export function taskSelected(code: string, patterns: string[]): boolean {
  if (!patterns.length) return true;
  return patterns.some((p) => globMatch(p, code));
}

/** 每题独立工作目录（async 局部 cwd 的落点），避免并行读写冲突。 */
function taskWorkdir(code: string): string {
  const base =
    env("TASK_WORKDIR", "") ||
    (isWritable("/work") ? "/work/tasks" : path.join(process.cwd(), "tasks"));
  const d = path.join(base, code);
  fs.mkdirSync(d, { recursive: true });
  return d;
}

function isWritable(p: string): boolean {
  try {
    fs.accessSync(p, fs.constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

/** 并行调度器：并发解多题，自动扩容直到平台拒绝（max active）。 */
async function runParallel(
  client: LlmClient,
  taskBudget: number,
  taskFilter: string[],
  results: Record<string, string>,
  attempts: Record<string, number>,
  snapshot: () => Promise<Challenge[] | null>,
): Promise<void> {
  const running = new Map<string, Promise<void>>();
  let cap = Number(env("AGENT_MAX_PARALLEL", "5"));
  let checkedSelection = false; // 首次快照时核对一次题目选择是否命中（打错字要出声）

  // VPN 守卫：auto=模式感知（hosted 强制；local 关），1/0 显式开关。
  let vpnMode = env("VPN_GUARD", "auto").toLowerCase();
  if (vpnMode === "auto") vpnMode = CFG.mode === "hosted" ? "1" : "0";
  let vpnProbeFail = 0;
  const vpnGuardOn = ["1", "on", "true"].includes(vpnMode);
  let lastVpnProbe = 0;
  const VPN_PROBE_INTERVAL = Number(env("VPN_PROBE_INTERVAL", "20"));
  if (vpnGuardOn) rec().event("vpn_guard_enabled", { mode: CFG.mode, vpn_mode: vpnMode });

  // 分段解题状态（调度器与 worker 共享；Node 单线程无竞态）：
  //   failures:  真失败次数，达上限才判死
  //   resumeMeta: 暂停题的续跑状态 {pauses, gives, target, turns}
  const failures: Record<string, number> = {};
  const resumeMeta: Record<string, Record<string, unknown>> = {};

  /** 是否还有别的题在等这个并发名额（新题或待续跑的题）。 */
  const slotWaiters = (items: Challenge[] | null, selfCode: string): boolean => {
    if (!items?.length) return true;
    for (const c of items) {
      const cc = c.unique_code;
      if (cc === selfCode || c.is_completed) continue;
      if (!taskSelected(cc, taskFilter)) continue;
      if (cc in running || results[cc] === "pass") continue;
      if ((failures[cc] ?? 0) >= Math.max(CFG.max_failures, 1)) continue;
      return true;
    }
    return false;
  };

  const worker = async (
    task: Challenge,
    addr: string,
    _attempt: number,
    isResume: boolean,
  ): Promise<void> => {
    const code = task.unique_code;
    setTaskCwd(taskWorkdir(code));
    let last = "";
    let state: Challenge | null = null;
    let closingText = "";
    let status: SolveStatus = "llm_error";
    let outcome = "fail";
    let completed = false;
    const conv = rec().conversation(code);
    let messages: ChatMessage[] | null = null;
    const meta = resumeMeta[code] ?? {};
    let pauses = Number(meta.pauses ?? 0);
    let gives = Number(meta.gives ?? 0);
    const oldTarget = (meta.target as string) ?? "";
    let startTurn = 0;
    let used = 0;
    try {
      const card = formatTaskCard({
        code,
        difficulty: task.difficulty,
        level: task.level ?? 0,
        flag_count: task.flag_count,
        total_score: task.total_score,
        description: task.description,
        target: addr,
        workdir: getTaskCwd() || taskWorkdir(code),
      });
      let budget = taskBudget;
      const rem0 = remainingSec();
      if (rem0 !== null) budget = Math.max(10, Math.min(taskBudget, Math.trunc(rem0 / 6)));

      if (isResume) {
        // 续跑：从 conversation.jsonl 重建全量上下文（记录即事实），再追加续跑通知
        const loaded = rec().loadMessages(code);
        if (loaded.length) {
          messages = loaded;
          startTurn = messages.filter((m) => m.role === "assistant").length;
          const note = fill(meta.reason === "giveup" ? GIVEUP_NOTE : RESUME_NOTE, {
            new_target: addr,
            old_target: oldTarget || "（未知）",
            workdir: getTaskCwd() || taskWorkdir(code),
            budget,
            turns: startTurn,
          });
          messages.push({ role: "user", content: note });
          conv.write("user", { turn: `${code}:${startTurn}`, content: note });
          log(
            `[${code}] resume segment: ${startTurn} prior turns restored, ` +
              `target=${addr} (was ${oldTarget || "?"})`,
          );
        } else {
          log(`[${code}] resume context unavailable; starting fresh segment`);
        }
      }

      for (;;) {
        const r = await solveTask(client, code, card, budget, conv, {
          messages: messages ?? undefined,
          startTurn: startTurn,
        });
        status = r.status;
        last = r.lastContent;
        used = r.turnsUsed;
        messages = r.messages;

        // 校验平台状态（快照失败重试，避免误判）
        let itemsNow: Challenge[] | null = null;
        for (let t = 0; t < 3; t++) {
          itemsNow = await snapshot();
          if (itemsNow) {
            state = bench.challengeState(itemsNow, code);
            if (state) break;
          }
          await sleep(4000);
        }
        completed = !!state?.is_completed;

        // 段到点但**没人等这个名额** → 不关题不重启：原容器、原上下文直接续一段
        if (status === "paused" && !completed && !slotWaiters(itemsNow, code)) {
          startTurn += used;
          budget = taskBudget;
          const rem1 = remainingSec();
          if (rem1 !== null) budget = Math.max(10, Math.min(taskBudget, Math.trunc(rem1 / 6)));
          rec().event("segment_continue", { code, turns: startTurn, target: addr });
          log(
            `[${code}] turn budget hit but nobody waits for this slot → ` +
              `continue in place (${startTurn} turns so far, no close/restart)`,
          );
          continue;
        }
        break;
      }

      // 通关后：同一上下文直接追加复盘请求
      if (completed && messages) {
        closingText = await closingSummary(client, code, messages, conv);
      }

      if (completed) {
        outcome = "pass";
        delete resumeMeta[code];
      } else if (["llm_error", "context_overflow", "vpn_abandon", "deadline"].includes(status)) {
        // 真失败：上下文不保留，计入失败次数，达上限后不再派发
        failures[code] =
          status === "context_overflow" ? Math.max(CFG.max_failures, 1) : (failures[code] ?? 0) + 1;
        delete resumeMeta[code];
        outcome = "fail";
        rec().event("task_abandoned", { code, status, failures: failures[code] });
        log(
          `[${code}] abandoned (${status}); failures=${failures[code]}` +
            (status === "context_overflow" ? " (context overflow — task stopped)" : ""),
        );
      } else if (status === "stopped") {
        outcome = "fail"; // 平台终态，见 platformEnded
        delete resumeMeta[code];
      } else {
        // paused（本段轮次到点）或 done 但未通关（模型提前收尾）→
        // 关闭题目释放名额，保留上下文排到队尾，等没有新题时再续跑。
        const giveup = status === "done";
        gives = giveup ? gives + 1 : 0;
        pauses += 1;
        const tooMany =
          (CFG.giveup_max > 0 && gives >= CFG.giveup_max) ||
          (CFG.pause_max > 0 && pauses > CFG.pause_max);
        if (tooMany) {
          // 终止式放弃：直接置满失败上限，不再被重新派发
          failures[code] = Math.max(CFG.max_failures, 1);
          delete resumeMeta[code];
          outcome = "fail";
          rec().event("task_abandoned", {
            code,
            status: giveup ? "giveup_loop" : "pause_limit",
            pauses,
            gives,
          });
          log(
            `[${code}] abandoning after ${pauses} segments ` +
              `(${giveup ? "consecutive give-ups" : "pause limit"})`,
          );
        } else {
          outcome = "paused";
          resumeMeta[code] = {
            pauses,
            gives,
            target: addr,
            turns: startTurn + used,
            reason: giveup ? "giveup" : "paused",
          };
          rec().event("task_paused", {
            code,
            pauses,
            gives,
            reason: giveup ? "giveup" : "turn_budget",
            turns: startTurn + used,
            target: addr,
          });
          log(
            `[${code}] segment done (${status}); pausing & releasing slot ` +
              `(segment ${pauses}, ${startTurn + used} total turns)`,
          );
        }
      }
    } catch (e) {
      log(`[${code}] worker error: ${e instanceof Error ? `${e.name}: ${e.message}` : e}`);
      rec().event("task_error", {
        code,
        error: e instanceof Error ? `${e.name}: ${e.message}` : String(e),
      });
      outcome = "fail";
    } finally {
      messages = null; // 该题上下文交还给 resumeMeta/记录，随 GC 回收
      setTaskCwd(""); // 任务结束清 async 局部
      const closedAt = new Date().toISOString().slice(0, 19).replace("T", " ");
      const completedNow = !!state?.is_completed;
      if (outcome === "pass" && !completedNow) outcome = "fail"; // 平台未确认通关不算 pass
      try {
        await bench.closeChallenge(code);
        rec().event("challenge_change", {
          code,
          field: "controller_close",
          old: "available",
          new: "closed",
          completed: completedNow,
        });
      } catch (e) {
        if (e instanceof PlatformError) {
          if (e.code !== "invalid_state") log(`[${code}] close error: ${e.message}`);
        } else {
          log(
            `[${code}] close network error: ${e instanceof Error ? `${e.name}: ${e.message}` : e}`,
          );
          rec().event("platform_poll_error", {
            where: "close",
            code,
            error: e instanceof Error ? e.message : String(e),
          });
        }
      }
      if (outcome !== "paused") {
        // 暂停不算结果：题目保留，排到队尾等续跑
        results[code] = outcome;
      }
      // 题目生命周期：关闭时间 + 耗时 + 结果（task_close 对应 task_start）
      rec().taskEvent(code, "task_close", {
        closed_at: closedAt,
        duration_sec:
          Math.round((Date.now() / 1000 - (TASK_START_TS.get(code) ?? Date.now() / 1000)) * 10) /
          10,
        outcome,
        flags: state ? `${state.correct_flag_count}/${state.flag_count}` : "?",
        summary: closingText || last || "",
        workdir_summary: last || "",
      });
      log(
        `=== task ${code}: ${outcome} ` +
          `${state ? `(${state.correct_flag_count}/${state.flag_count})` : ""} ===`,
      );
      running.delete(code);
    }
  };

  for (;;) {
    if (STOP) {
      log("platform ended; scheduler stopping");
      break;
    }
    const rem = remainingSec();
    if (rem !== null && rem <= 60) {
      log("deadline near; stopping scheduler");
      break;
    }

    // --- VPN 运行时守卫：周期（默认 20s）探测，连续失败 → 置位暂停派发；成功即恢复 ---
    if (vpnGuardOn && !STOP && Date.now() / 1000 - lastVpnProbe >= VPN_PROBE_INTERVAL) {
      lastVpnProbe = Date.now() / 1000;
      if (await vpnOk(5000)) {
        vpnProbeFail = 0;
        if (VPN_DOWN) {
          VPN_DOWN = false;
          rec().event("vpn_recovered", {});
          log("VPN probe OK — resume dispatching new tasks");
        }
      } else {
        vpnProbeFail++;
        if (vpnProbeFail >= 3 && !VPN_DOWN) {
          VPN_DOWN = true;
          rec().event("vpn_down", { consecutive_fails: vpnProbeFail });
          log(
            `VPN DOWN (${vpnProbeFail} consecutive probe failures) — pausing dispatch; running workers will wait`,
          );
        }
      }
    }

    let items = await snapshot();
    if (!items) {
      // 平台终态 → 直接收尾；否则短暂等待重试
      if (STOP) break;
      await sleep(8000);
      continue;
    }
    for (const code of await bench.releaseCompleted(items)) {
      rec().event("challenge_change", {
        code,
        field: "controller_close",
        old: "available",
        new: "closed",
        note: "released after completion",
      });
      results[code] = results[code] ?? "pass";
    }
    items = (await snapshot()) ?? items;

    // 题目选择校验：只跑部分题时，打错字不吭声很难查 → 首次快照核对一次
    if (taskFilter.length && !checkedSelection) {
      checkedSelection = true;
      const matched = items
        .map((c) => c.unique_code)
        .filter((c) => taskSelected(c, taskFilter))
        .sort();
      if (matched.length) {
        log(`task selection matched ${matched.length}: ${matched.join(", ")}`);
      } else {
        log(
          `WARNING: task selection matched nothing: ${taskFilter.join(",")} ` +
            `(platform has ${items.length} tasks)`,
        );
        rec().event("task_selection_empty", { patterns: taskFilter, total: items.length });
      }
    }

    // 候选：未通关、不在跑、未超真失败上限、在题目选择范围内
    const eligible = (c: Challenge): boolean => {
      if (c.is_completed) return false;
      if (c.unique_code in running) return false;
      if (results[c.unique_code] === "pass") return false;
      if ((failures[c.unique_code] ?? 0) >= Math.max(CFG.max_failures, 1)) return false;
      return taskSelected(c.unique_code, taskFilter);
    };
    const key = (c: Challenge): [number, number, number, string] => [
      c.container_status === "available" ? 0 : 1,
      bench.DIFF_RANK[c.difficulty] ?? 9,
      c.level ?? 9,
      c.unique_code,
    ];
    const fresh = items
      .filter((c) => eligible(c) && !(c.unique_code in resumeMeta))
      .sort((a, b) => compareKey(key(a), key(b)));
    const resumable = items
      .filter((c) => eligible(c) && c.unique_code in resumeMeta)
      .sort((a, b) => {
        const ap = Number(resumeMeta[a.unique_code]?.pauses ?? 0);
        const bp = Number(resumeMeta[b.unique_code]?.pauses ?? 0);
        return ap !== bp ? ap - bp : a.unique_code < b.unique_code ? -1 : 1;
      });
    // 分段策略：暂停题只在没有新题可派时才续跑
    const candidates = fresh.length ? fresh : resumable;

    if (!candidates.length && !running.size) break; // 全部完成或全部结束

    if (VPN_DOWN) {
      // 暂停派发（平台 API 走公网不依赖 VPN；只有靶场渗透需要 VPN）：不 start、不消耗 attempts
      await sleep(10_000);
      continue;
    }

    // 扩容：直到槽位用尽或平台拒绝（max active）
    for (const task of candidates) {
      if (running.size >= cap) break;
      const code = task.unique_code;
      let addr = taskTarget(task);
      // 续跑必须重新 start：题目容器要重启（释放名额期间已被关闭），拿到新 target
      if (task.container_status !== "available" || !addr || code in resumeMeta) {
        let started = false;
        let slotFull = false;
        for (let t = 0; t < 3; t++) {
          if (STOP) break;
          try {
            const resp = await bench.startChallenge(code);
            addr = taskTarget(resp) || addr;
            started = true;
            break;
          } catch (e) {
            if (e instanceof TaskFinishedError) {
              platformEnded("start", e, results);
              break;
            }
            if (e instanceof PlatformError) {
              if (e.code === "invalid_state" && e.detail.includes("max active")) {
                // 平台拒绝 → 收缩并发到平台实际容量，本轮不再 start
                cap = Math.max(running.size, bench.activeCount((await snapshot()) ?? items));
                log(`platform max-active reached; cap -> ${cap}`);
                rec().event("parallel_cap", { cap, reason: "max_active" });
                slotFull = true;
                break;
              }
              if (e.code === "invalid_state") {
                // 任务侧状态问题：记 fail，不重试死循环
                log(`[${code}] start invalid_state: ${e.detail}`);
                break;
              }
              if (e.code === "duplicate") {
                started = true; // 幂等：容器已在跑，直接复用现有地址
                break;
              }
              if (e.code === "resource_unavailable") {
                await sleep(5000);
                continue;
              }
              log(`[${code}] start error: ${e.message}`);
              break;
            }
            log(
              `[${code}] start network error (try ${t + 1}): ${e instanceof Error ? `${e.name}: ${e.message}` : e}`,
            );
            rec().event("platform_poll_error", {
              where: "start",
              code,
              error: e instanceof Error ? e.message : String(e),
            });
            await sleep(5000);
          }
        }
        if (STOP) break; // 平台终态 → 退出候选循环，外层收尾
        if (slotFull) break; // 名额满了，等 worker 完成后再扩
        if (!started) {
          // start 失败计入真失败上限，避免同一题无限重试死循环
          attempts[code] = (attempts[code] ?? 0) + 1;
          failures[code] = (failures[code] ?? 0) + 1;
          if (failures[code] >= Math.max(CFG.max_failures, 1)) results[code] = "fail";
          continue;
        }
      }
      if (!addr) {
        log(`[${code}] no container_addr from start`);
        attempts[code] = (attempts[code] ?? 0) + 1;
        failures[code] = (failures[code] ?? 0) + 1;
        results[code] = "fail";
        continue;
      }

      attempts[code] = (attempts[code] ?? 0) + 1;
      const attemptNo = attempts[code];
      const isResume = code in resumeMeta;
      // 题目生命周期：启动时间（task_start，对应 task_close 的 closed_at/duration）
      rec().taskEvent(code, "task_start", {
        started_at: new Date().toISOString().slice(0, 19).replace("T", " "),
        target: addr,
        attempt: attemptNo,
        resume: isResume,
        parallel: running.size + 1,
        flags: `0/${task.flag_count}`,
      });
      TASK_START_TS.set(code, Date.now() / 1000);
      log(
        `=== task ${code} attempt ${attemptNo} target=${addr} ` +
          `${isResume ? "(resume segment) " : ""}` +
          `[parallel ${running.size + 1}/${cap}] ===`,
      );
      const p = worker(task, addr, attemptNo, isResume).catch((e) => {
        log(`[${code}] worker crashed: ${e instanceof Error ? e.stack : e}`);
      });
      running.set(code, p);
      await sleep(300); // 平滑扩容，避免瞬时打满平台
    }

    // 有 worker 在跑则等待；空转保护
    if (running.size) {
      await Promise.race([...running.values(), sleep(2000)]);
      await sleep(2000);
    } else if (candidates.length) {
      await sleep(5000);
    }
  }

  // 收尾：等待所有 worker（最多 3 分钟/题）
  for (const [code, p] of [...running]) {
    const timeout = new Promise<void>((r) => setTimeout(r, 180_000));
    const done = await Promise.race([p.then(() => true), timeout.then(() => false)]);
    if (!done) log(`[${code}] worker still running at shutdown; detached`);
  }

  // 运行终止（平台终态/时限）时仍有暂停中的题：补记 fail
  const pausedCodes = Object.keys(resumeMeta);
  if (pausedCodes.length) {
    for (const code of pausedCodes) {
      results[code] = results[code] ?? "fail";
      rec().event("task_abandoned", {
        code,
        status: "run_ended",
        pauses: resumeMeta[code]?.pauses ?? 0,
      });
      log(`[${code}] still paused at shutdown; recorded as fail`);
      delete resumeMeta[code];
    }
  }

  // 平台终态：所有 worker 已停，尽力关闭其靶场容器释放资源
  if (STOP) {
    for (const code of Object.keys(attempts)) {
      try {
        await bench.closeChallenge(code);
        rec().event("challenge_change", {
          code,
          field: "controller_close",
          old: "available",
          new: "closed",
          note: "platform ended cleanup",
        });
      } catch {
        /* 平台已截止时 close 可能返回 invalid_state——忽略即可 */
      }
    }
    log("all workers stopped; active challenge containers cleanup attempted");
  }
}

function compareKey(a: unknown[], b: unknown[]): number {
  for (let i = 0; i < a.length; i++) {
    const x = a[i];
    const y = b[i];
    if (x === y) continue;
    if (typeof x === "number" && typeof y === "number") return x - y;
    return String(x) < String(y) ? -1 : 1;
  }
  return 0;
}

function printSummary(results: Record<string, string>, items: Challenge[] | null): void {
  const done = (items ?? []).filter((c) => c.is_completed);
  const score = done.reduce((s, c) => s + c.total_score, 0);
  const total = (items ?? []).length;
  const lines = [
    "",
    "=".repeat(46),
    `总进度：已通关 ${done.length}/${total} 题，总分 ${score}`,
    `本轮结果：pass=${Object.values(results).filter((v) => v === "pass").length} ` +
      `fail=${Object.values(results).filter((v) => v === "fail").length}`,
    "=".repeat(46),
  ];
  console.log(lines.join("\n"));
  rec().event("final_summary", { completed: done.length, total, score });
}

/** CLI 入口。返回退出码。 */
export async function main(argv = process.argv.slice(2)): Promise<number> {
  const args = parseArgs(argv);

  if (args.mode) process.env.AGENT_MODE = args.mode;
  await resolveAsync();
  log(
    `mode=${CFG.mode} model=${CFG.model} gateway=${CFG.llm_base_url} bench=${CFG.benchmark_base_url}`,
  );
  let client: LlmClient;
  try {
    client = makeClient();
  } catch (e) {
    if (e instanceof ExitSignal) return e.code;
    throw e;
  }

  if (args.smoke) return await smokeTest(client);

  if (args.task) process.env.AGENT_TASK_FILTER = args.task;
  return await run(client);
}

interface CliArgs {
  smoke: boolean;
  mode: string | null;
  task: string | null;
}

export function parseArgs(argv: string[]): CliArgs {
  const out: CliArgs = { smoke: false, mode: null, task: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--smoke") out.smoke = true;
    else if (a === "--mode") {
      const v = argv[++i];
      if (v === "auto" || v === "local" || v === "hosted") out.mode = v;
    } else if (a === "--task") {
      out.task = argv[++i] ?? null;
    } else if (a === "--help" || a === "-h") {
      console.log(
        "用法: agent [--smoke] [--mode auto|local|hosted] [--task CODE[,CODE...]]\n" +
          "  --smoke   冒烟测试：仅验证网关连通 + shell 工具\n" +
          "  --mode    覆盖 AGENT_MODE（默认 auto 自动探测）\n" +
          "  --task    只解指定题目（调试用）：逗号分隔、支持 * ? 通配",
      );
      process.exit(0);
    }
  }
  return out;
}

const isMain =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname);
if (isMain) {
  main()
    .then((code) => {
      process.exit(code);
    })
    .catch((e) => {
      if (e instanceof ExitSignal) process.exit(e.code);
      console.error(e);
      process.exit(1);
    });
}
