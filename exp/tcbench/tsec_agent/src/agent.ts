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
import { type BranchCfg, type BranchLite, BranchManager } from "./branch.js";
import {
  afterForkMaterialized,
  BRANCH_TOOL_NAMES,
  BROADCAST_TOOL_SCHEMA,
  CORE_LOG_TOOL_SCHEMA,
  dispatchBranchTool,
  FORK_TOOL_SCHEMA,
  type PendingFork,
} from "./branch_tools.js";
import {
  BYSTANDER_SYSTEM,
  buildBystanderInput,
  bystanderMessage,
  evaluateStall,
  recentDigest,
} from "./bystander.js";
import { CFG, env, resolveAsync } from "./config.js";
import { extractFlagIndex, FlagLedger, readCorelog } from "./corelog.js";
import {
  CLOSING_SUMMARY_PROMPT,
  fill,
  formatTaskCard,
  GIVEUP_NOTE,
  RESUME_NOTE,
  systemPromptWithExtras,
} from "./prompt.js";
import { getTaskCwd, setTaskCwd } from "./shell.js";
import { dispatchShell, parseToolArguments, SHELL_TOOL_SCHEMA } from "./shell_tool.js";

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
import { initRecords, RECORDS_DIR, type RunRecords, rec } from "./records.js";
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

// 平台最新快照缓存（watcher 每 20s 刷新）：A 策略的每轮通关检测读这里，
// 零额外 API 调用；最坏检测延迟 = watcher 间隔 + 当前轮工具时长。
let latestSnapshot: Challenge[] | null = null;

/** A 策略：某题是否已被平台确认通关（读 watcher 缓存）。 */
function confirmedComplete(code: string): boolean {
  const c = latestSnapshot?.find((x) => x.unique_code === code);
  return !!c?.is_completed;
}

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
    latestSnapshot = items; // A 策略：给 worker 的每轮通关检测提供零成本缓存
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
        text: "[网关不支持图像输入：本条图片已移除，请改用上一条文本里的元数据，或用 shell 分析原文件]",
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
  branches: BranchLite[]; // fork 模式的跨段分支状态（disabled 时为空）
}

/** fork/旁观者配置（从 CFG 组装；测试可整体覆盖 CFG）。 */
function branchCfgFromConfig(): BranchCfg {
  return {
    enabled: CFG.fork_enabled,
    maxBranches: CFG.max_branches,
    maxForks: CFG.fork_max,
    forkCooldownSec: CFG.fork_cooldown_sec,
    pruneSec: CFG.branch_prune_sec,
    broadcastMax: CFG.broadcast_max,
    bystanderMax: CFG.bystander_max,
    bystanderCooldownSec: CFG.bystander_cooldown_sec,
  };
}

/** 单题分段解题：本段直到模型输出最终总结（无工具调用）或【段墙钟预算】到点。
 *
 * 状态: done(全部分支收束) / paused(段到点) / stopped(平台终态) /
 * vpn_abandon / deadline / llm_error / context_overflow
 * messages/startTurn：续跑时传入重建好的上下文与轮次偏移。
 * budgetSec：段墙钟预算（C 策略：短 600s/长 1800s）——轮数只当安全网（防极端空转，
 * 因为实测墙钟由工具等待主导，同样 60 轮墙钟可差 30 倍）。
 * completionCheck：A 策略——每轮开头问一次"平台是否已标记通关"（读 watcher 缓存，
 * 零额外 API 调用，最坏延迟 20s）。通关即收束，不等段边界、不等模型自己想起来收尾
 * （b-02 实测通关后白挂 22 分钟就是等模型收尾）。
 * 对话全量、忠实落盘（conversation.jsonl，带 branch 字段）。
 *
 * fork 多分支（AGENT_FORK=1，默认关）：单飞轮转交错推进（a1 b1 a2 b2，缓存零代价
 * 已实测）；护栏与调度见 branch.ts；旁观者（AGENT_BYSTANDER=1）见 bystander.ts。
 * 分支/旁观者开关关闭时行为与旧单分支版逐语义一致。
 */
export async function solveTask(
  client: LlmClient,
  code: string,
  taskCard: string,
  budgetSec: number,
  conv: RunRecords["conversation"] extends (c: string) => infer R ? R : never,
  opts: {
    messages?: ChatMessage[];
    startTurn?: number;
    completionCheck?: () => boolean;
    branches?: BranchLite[]; // 续段恢复的分支状态
    taskClass?: "long" | "short"; // 旁观者停滞阈值分档
    flagTotal?: number; // flag 索引账本总数
  } = {},
): Promise<SolveResult> {
  const forkOn = CFG.fork_enabled;
  const multiOn = forkOn || CFG.bystander_enabled;
  let messages: ChatMessage[];
  if (!opts.messages) {
    messages = [
      { role: "system", content: systemPromptWithExtras(forkOn, CFG.bystander_enabled) },
      { role: "user", content: taskCard },
    ];
    // 忠实记录本题输入（system + 任务卡）
    conv.write("system", { branch: 0, turn: `${code}:0`, content: messages[0].content });
    conv.write("user", { branch: 0, turn: `${code}:0`, content: taskCard });
  } else {
    messages = opts.messages;
  }
  const startTurn = opts.startTurn ?? 0;
  let lastContent = "";
  let turnsUsed = 0;
  let turn = 0;
  let segStartTs = Date.now() / 1000; // VPN 挂起等待会补偿（等待不烧段预算）
  const nowSec = () => Date.now() / 1000;

  // --- fork/旁观者基础设施（默认关：单 trunk、无额外工具、无额外调用） ---
  const workdir = getTaskCwd() || "";
  const branchCwd = (id: number): string => {
    const d = `${workdir}/branches/${id}`;
    try {
      fs.mkdirSync(d, { recursive: true });
    } catch {
      /* 目录已存在/不可建：shell 会回退到默认 cwd */
    }
    return d;
  };
  const bm = new BranchManager(branchCfgFromConfig(), messages, nowSec(), workdir);
  if (opts.branches?.length) bm.restore(opts.branches, nowSec());
  const corelog = multiOn ? rec().corelog(code) : null;
  const ledger = new FlagLedger();
  if (opts.flagTotal) ledger.setTotal(opts.flagTotal);
  const stallSec =
    (opts.taskClass ?? "short") === "long" ? CFG.stall_long_sec : CFG.stall_short_sec;

  // 工具集：shell 恒在；fork/broadcast 仅 fork 开；core_log 两者任一开
  const tools: unknown[] = [SHELL_TOOL_SCHEMA];
  if (forkOn) tools.push(FORK_TOOL_SCHEMA, BROADCAST_TOOL_SCHEMA);
  if (multiOn) tools.push(CORE_LOG_TOOL_SCHEMA);
  const toolDeps = {
    bm,
    corelog,
    ledger,
    now: nowSec,
    log,
    // broadcast 送达 → 目标分支的注入消息忠实落盘（对话忠实不变量）
    onBroadcast: (fromId: number, toIds: number[], note: string) => {
      for (const tid of toIds) {
        conv.write("user", {
          branch: tid,
          kind_note: "broadcast",
          content: `[分支 ${fromId} 广播] ${note}`,
        });
      }
    },
  };

  const branchLite = (): BranchLite[] =>
    bm
      .alive()
      .filter((b) => b.id !== 0)
      .map((b) => ({
        id: b.id,
        parent: b.parent,
        label: b.label,
        cwd: b.cwd,
        forkSeq: b.forkSeq,
        messages: b.messages,
      }));
  const finish = (status: SolveStatus): SolveResult => ({
    status,
    lastContent,
    turnsUsed,
    messages,
    branches: branchLite(),
  });

  // 剪枝：corelog/flag 双停滞 → 回收分支 + 摘要注入 trunk（尾部追加保缓存）
  const pruneStalled = (): void => {
    if (!forkOn) return;
    const t = nowSec();
    for (const b of bm.alive()) {
      if (b.id === 0) continue;
      if (t - b.lastCoreTs < CFG.branch_prune_sec) continue;
      const pruned = bm.prune(b.id);
      if (!pruned) continue;
      const tail = corelog
        ? readCorelog(corelog.path)
            .filter((e) => e.branch === b.id && (e.kind === "core" || e.kind === "flag"))
            .slice(-10)
            .map((e) => (e.kind === "flag" ? `flag idx=${e.flag_index ?? "?"}` : (e.note ?? "")))
        : [];
      const summary =
        `[分支 ${b.id} 已回收] 方向：${b.label}；停滞 ${Math.round(t - b.lastCoreTs)}s 无核心事件。\n` +
        `核心日志摘要：\n${tail.map((s) => `- ${s}`).join("\n") || "-（无）"}\n` +
        `（该分支的工作目录 ${b.cwd} 与产物仍在，可读取）`;
      bm.get(0)?.messages.push({ role: "user", content: summary });
      conv.write("user", { branch: 0, kind_note: "branch_pruned", content: summary });
      corelog?.write({
        branch: b.id,
        kind: "prune",
        note: `stalled ${Math.round(t - b.lastCoreTs)}s`,
      });
      log(`[${code}] branch ${b.id} pruned (stalled); summary injected into trunk`);
    }
  };

  // 旁观者：组合信号任二成立 → 独立短上下文调用 → 尾部插嘴（限频在 bm）
  const maybeBystander = async (): Promise<void> => {
    if (!CFG.bystander_enabled) return;
    const t = nowSec();
    const active = bm.active();
    if (!active.length) return;
    const stalled = active.filter(
      (b) =>
        evaluateStall({
          sinceCoreSec: t - b.lastCoreTs,
          stallSec,
          recentCmds: b.recentCmds,
          segElapsedSec: t - segStartTs,
          segBudgetSec: budgetSec,
          coreThisSegment: b.lastCoreTs > segStartTs,
        }).count >= 2,
    );
    if (!stalled.length) return;
    // 全分支停滞 → 插 trunk；否则插第一个停滞分支
    const target = stalled.length === active.length ? bm.get(0) : stalled[0];
    if (!target || !bm.bystanderAvailable(target.id, t)) return;
    const sig = evaluateStall({
      sinceCoreSec: t - target.lastCoreTs,
      stallSec,
      recentCmds: target.recentCmds,
      segElapsedSec: t - segStartTs,
      segBudgetSec: budgetSec,
      coreThisSegment: target.lastCoreTs > segStartTs,
    });
    const entries = corelog ? readCorelog(corelog.path) : [];
    const input = buildBystanderInput({
      entries,
      branchId: target.id,
      branchLabel: target.label,
      branchNames: bm.names(),
      ledger: multiOn ? ledger : null,
      recentDigest: recentDigest(target.messages),
      recentCmds: target.recentCmds,
      segElapsedSec: t - segStartTs,
      signals: sig,
    });
    let advice = "";
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 60_000);
    try {
      const resp = await callLlm(
        client,
        [
          { role: "system", content: BYSTANDER_SYSTEM },
          { role: "user", content: input },
        ],
        false,
        // 只要 ≤120 字建议：关思考省 completion（thinking 占成本大头，实测 37-69%）
        { signal: ac.signal, thinking: { type: "disabled" } },
      );
      advice = (resp.choices[0].message.content ?? "").trim();
    } catch (e) {
      log(`[${code}] bystander call failed: ${e instanceof Error ? `${e.name}: ${e.message}` : e}`);
      return; // 失败不消耗限频额度
    } finally {
      clearTimeout(timer);
    }
    if (!advice) return;
    const content = bystanderMessage(advice);
    target.messages.push({ role: "user", content });
    target.active = true; // 插嘴 = 新方向，激活 parked 分支
    bm.noteBystander(target.id, t);
    conv.write("user", {
      branch: target.id,
      kind_note: "bystander",
      content,
    });
    corelog?.write({ branch: target.id, kind: "bystander", note: advice.slice(0, 400) });
    log(
      `[${code}] bystander injected into ${target.id === 0 ? "trunk" : `branch ${target.id}`}: ` +
        `${advice.slice(0, 100)}`,
    );
  };

  while (true) {
    if (STOP) return finish("stopped");
    // A 策略：每轮开头查通关（watcher 缓存）——平台已确认满 flag 就立刻收束，
    // 不再等模型自己输出总结（它可能根本想不起来，b-02 白挂 22 分钟的教训）。
    if (opts.completionCheck?.()) {
      log(`[${code}] platform confirmed completion mid-segment — closing segment early`);
      return finish("done");
    }
    // C 策略：段墙钟预算到点即收段（转 paused 让调度处置：原地续或关题让位）。
    // 安全网：轮数超上限也收段（0=关闭轮数闸），防模型纯说话空转烧 token。
    if (nowSec() - segStartTs >= budgetSec) {
      log(`[${code}] segment wall-clock budget ${budgetSec}s reached (turn ${turn})`);
      return finish("paused");
    }
    if (CFG.task_max_turns > 0 && turn >= CFG.task_max_turns) {
      log(`[${code}] turn safety-net ${CFG.task_max_turns} reached`);
      return finish("paused");
    }
    // VPN 守卫：VPN 挂起 → 本题挂起等待恢复（不烧轮次预算），恢复后原地继续。
    // 等待超过 AGENT_VPN_WAIT_MAX（默认 600s）仍未恢复才放弃本题。
    if (VPN_DOWN) {
      const waitMax = Number(env("AGENT_VPN_WAIT_MAX", "600"));
      const t0 = nowSec();
      conv.write("note", {
        branch: 0,
        turn: `${code}:${turn}`,
        content: `VPN down detected; suspending task (max ${waitMax.toFixed(0)}s)`,
      });
      log(`[${code}] VPN down; task suspended (waiting for recovery)`);
      while (VPN_DOWN && !STOP && nowSec() - t0 < waitMax) {
        await sleep(3000);
      }
      const waited = nowSec() - t0;
      if (VPN_DOWN) {
        conv.write("error", {
          branch: 0,
          turn: `${code}:${turn}`,
          error: `VPN still down after ${waited.toFixed(0)}s; abandoning task`,
        });
        log(`[${code}] VPN still down after ${waited.toFixed(0)}s; abandoning`);
        return finish("vpn_abandon");
      }
      conv.write("note", {
        branch: 0,
        turn: `${code}:${turn}`,
        content: `VPN recovered after ${waited.toFixed(0)}s; resuming`,
      });
      log(`[${code}] VPN recovered after ${waited.toFixed(0)}s; resuming task`);
      // VPN 挂起等待不计入段墙钟预算（保持"等待不烧预算"语义）
      segStartTs += waited;
      continue; // 恢复后从当前 turn 继续（不烧预算）
    }
    const rem = remainingSec();
    if (rem !== null && rem <= 0) return finish("deadline");

    pruneStalled();
    await maybeBystander();

    const br = bm.pick();
    if (!br) {
      // 全部分支 parked（输出无工具总结）→ 本段收束（是否通关由控制器查平台）
      log(`[${code}] all branches parked (turn ${turn}); closing segment`);
      return finish("done");
    }

    turn++;
    const roundsAtTurnStart = br.roundsSinceCore; // core_log 轮计数基准（轮末比较用）
    const tLlmStart = nowSec();
    let resp: ChatResponse | null = null;
    let err: unknown = null;
    try {
      resp = await callLlm(client, br.messages, true, { tools });
    } catch (e) {
      err = e;
      // 多模态降级：网关拒收图片 → 剥掉 image part 改发纯文本，本会话后续不再附图
      const dropped = CFG.vision_ok !== false ? stripImages(br.messages) : 0;
      if (dropped) {
        CFG.vision_ok = false;
        log(
          `[${code}] gateway rejected image input (${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}); ` +
            `dropped ${dropped} image(s), retrying text-only`,
        );
        conv.write("note", {
          branch: br.id,
          turn: `${code}:${turn}`,
          content: `网关拒图，降级为文本模式：${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`,
        });
        rec().event("vision_degraded", {
          code,
          dropped,
          error: `${e instanceof Error ? e.name : "Error"}: ${(e as Error).message}`.slice(0, 400),
        });
        try {
          resp = await callLlm(client, br.messages, true, { tools });
        } catch (e2) {
          err = e2;
        }
      }
      if (!resp) {
        const emsg = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
        log(`[${code}] fatal LLM error: ${emsg}`);
        conv.write("error", {
          branch: br.id,
          turn: `${code}:${turn}`,
          error: emsg,
          started_at: tLlmStart,
        });
        const status: SolveStatus = isContextOverflow(err) ? "context_overflow" : "llm_error";
        return finish(status);
      }
    }
    turnsUsed++;
    const tLlmEnd = nowSec();

    const msg = resp.choices[0].message;
    const tlabel = `${code}:${startTurn + turn}`;
    const toolCallsRaw = msg.tool_calls ?? [];
    // AI 输出全量落盘：thinking / output / tool_calls 原文（started_at/ended_at = LLM 调用起止）
    conv.write("assistant", {
      branch: br.id,
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
    br.messages.push(clean);

    if (!toolCallsRaw.length) {
      if (!multiOn) return finish("done"); // 单分支语义不变：无工具总结 = 本题上下文收束
      // fork 模式：该分支 parked（无收尾权），其他分支继续；全 parked 时循环顶部收束
      bm.park(br.id);
      log(`[${code}] branch ${br.id} parked (no tool call)`);
      continue;
    }

    const pendingForks: PendingFork[] = [];
    for (const tc of toolCallsRaw) {
      let args: Record<string, unknown> = {};
      const rawArgs = tc.function.arguments || "{}";
      const tToolStart = nowSec();
      let result: unknown;
      if (tc.function.name === "shell") {
        const [parsed, fix] = parseToolArguments(rawArgs);
        if (parsed === null) {
          result = `error: ${fix || "invalid tool arguments"}`;
        } else {
          args = parsed;
          // 分支命名空间：shell 默认 cwd = 本分支目录（显式传 cwd 优先；trunk 不改）
          if (br.id !== 0 && !args.cwd) args.cwd = br.cwd;
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
          if (typeof args.command === "string") bm.pushCmd(br.id, args.command);
          // flag 事件（权威之一）：submit 响应的 matched_flag_index 作稳定 ID 进账本
          const idx = extractFlagIndex(firstText(result));
          if (idx !== null && ledger.add(idx, br.id)) {
            const cfc = /"correct_flag_count"\s*:\s*(\d+)/.exec(firstText(result));
            const tfc = /"total_flag_count"\s*:\s*(\d+)/.exec(firstText(result));
            if (tfc) ledger.setTotal(Number(tfc[1]));
            corelog?.write({
              branch: br.id,
              kind: "flag",
              flag_index: idx,
              correct_flag_count: cfc ? Number(cfc[1]) : ledger.count(),
              total_flag_count: tfc ? Number(tfc[1]) : undefined,
            });
            bm.recordCore(br.id, nowSec());
            log(`[${code}] branch ${br.id} flagged idx=${idx} (${ledger.progress()})`);
          }
        }
      } else if (BRANCH_TOOL_NAMES.includes(tc.function.name)) {
        let parsedArgs: Record<string, unknown> = {};
        try {
          parsedArgs = JSON.parse(rawArgs || "{}") as Record<string, unknown>;
        } catch {
          parsedArgs = {};
        }
        args = parsedArgs;
        const out = dispatchBranchTool(tc.function.name, parsedArgs, toolDeps, br.id, tc.id);
        if (!out) {
          result = `error: unknown tool ${JSON.stringify(tc.function.name)}`;
        } else {
          result = out.result;
          if (out.pendingFork) pendingForks.push(out.pendingFork);
        }
      } else {
        result = `error: unknown tool ${JSON.stringify(tc.function.name)}`;
      }
      // core_log 轮级轻推（执行层兜底，零 LLM 调用）：本轮最后一个工具结果尾部附提醒。
      // multiOn 闸 = core_log 工具在场（常规模式无此工具，提醒会诱导 unknown tool）。
      // 实时判定（core_log 若发生在本分支本轮更早的 tc 上，rounds 已清零 → 不注入）。
      if (
        multiOn &&
        tc === toolCallsRaw[toolCallsRaw.length - 1] &&
        typeof result === "string" &&
        br.roundsSinceCore >= CFG.core_log_nudge_rounds &&
        br.nudgesUsed < CFG.core_log_nudge_max
      ) {
        const n =
          result +
          `\n[系统提醒·核心日志] 已连续 ${br.roundsSinceCore} 轮无 core_log：` +
          `刚获得的证据（凭据/密钥/响应泄露/2xx 路径）或已证伪的路径，` +
          `请立即 core_log 一张证据卡再继续——黑板空白会被判定停滞（分支回收、旁观者无从插手）。`;
        result = n;
        br.nudgesUsed++;
        br.roundsSinceCore = 0; // 给模型 3 轮响应期
        log(
          `[${code}] core_log nudge injected for branch ${br.id} (${br.nudgesUsed}/${CFG.core_log_nudge_max})`,
        );
      }
      const tToolEnd = nowSec();
      // 工具结果全量落盘（与 assistant 条目通过 tool_call_id 关联；起止 = 工具执行）
      conv.write("tool", {
        branch: br.id,
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
        `tool#${tc.id.slice(-6)} ${tc.function.name}[b${br.id}] -> ` +
          `${first ? (first.split("\n")[0] || "").slice(0, 160) : ""}`,
      );
      br.messages.push({ role: "tool", tool_call_id: tc.id, content: result as string });
    }

    // 轮末 core_log 轮计数：本轮发生过 core/flag（rounds 被 recordCore 清零）或刚被
    // 轻推归零时保持；否则累计（连续 N 轮触发下一轮的轻推判定）。
    if (br.roundsSinceCore === roundsAtTurnStart) br.roundsSinceCore++;

    // 轮末 materialize fork（此时父分支上下文 tool 配对完整，拷贝协议安全）
    for (const pf of pendingForks) {
      const t = nowSec();
      const forkSeq = conv.currentSeq;
      const made = bm.materializeFork(
        pf.reservation.id,
        pf.parentId,
        pf.direction,
        forkSeq,
        t,
        branchCwd,
      );
      if (!made) continue;
      conv.write("user", {
        branch: made.branch.id,
        kind_note: "fork_directive",
        parent_branch: pf.parentId,
        fork_seq: forkSeq,
        fork_label: pf.direction.slice(0, 200),
        content: made.directive,
        turn: tlabel,
      });
      afterForkMaterialized(corelog, pf.parentId, made.branch.id, pf.direction);
      log(
        `[${code}] fork materialized: branch ${made.branch.id} from ${pf.parentId}: ${pf.direction.slice(0, 80)}`,
      );
    }
  }
}

/** 通关后复盘：在同一题上下文里追加一条总结请求（无工具），回复作为普通消息
 * 与其它对话一样忠实写入 conversation.jsonl。返回总结文本。 */
/** 通关复盘：同一上下文追加总结请求（无工具），回复原样落 conversation.jsonl。
 *  限时（AGENT_SUMMARY_TIMEOUT_SEC，默认 300s）：容器此时已关、槽位已释放，总结
 *  脱槽跑不占并发名额；超时中止调用、丢弃总结，照常记 pass——总结绝不能拖累通关，
 *  也不能让 agent 无限烧 token。 */
export async function closingSummary(
  client: LlmClient,
  code: string,
  messages: ChatMessage[],
  conv: RunRecords["conversation"] extends (c: string) => infer R ? R : never,
): Promise<string> {
  messages.push({ role: "user", content: CLOSING_SUMMARY_PROMPT });
  conv.write("user", { turn: `${code}:closing`, content: CLOSING_SUMMARY_PROMPT });
  const tLlmStart = Date.now() / 1000;
  const timeoutMs = Math.max(1, CFG.summary_timeout_sec) * 1000;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  let resp: ChatResponse;
  try {
    resp = await callLlm(client, messages, false, {
      signal: ac.signal,
      thinking: { type: "disabled" }, // 复盘是纯文本总结：关思考省 completion
    });
  } catch (e) {
    const timedOut = ac.signal.aborted;
    const emsg = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
    log(
      timedOut
        ? `[${code}] closing summary TIMED OUT after ${CFG.summary_timeout_sec}s — dropped (pass unaffected)`
        : `[${code}] closing summary LLM error: ${emsg}`,
    );
    conv.write("note", {
      turn: `${code}:closing`,
      content: timedOut
        ? `通关复盘超时（>${CFG.summary_timeout_sec}s），已丢弃；不影响通关判定`
        : `通关复盘失败：${emsg}`,
      started_at: tLlmStart,
    });
    rec().event(timedOut ? "summary_timeout" : "summary_error", { code });
    return "";
  } finally {
    clearTimeout(timer);
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
    await runParallel(client, taskFilter, results, attempts, snapshot);
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

/** D 策略：题目分类——只用平台 API 字段（flag_count/difficulty/level），不依赖历史基线
 *（正式比赛题目必变，基线只能当参考不能当信号）。
 * 长题 ⇔ 多 flag（归档实测多 flag 题段 p50=19min、历史总耗时 36~197min）∨ hard ∨ level≥3；
 * 其余视为短题。运行中若短题整段零新 flag → 段末改判长题（治"字段说 easy 实际 200min"）。 */
export function classifyTask(c: Challenge): "long" | "short" {
  return c.flag_count >= 2 || c.difficulty === "hard" || (c.level ?? 0) >= 3 ? "long" : "short";
}

/** D 策略估算（只用公开字段，越大越"长/值钱"先启动）：flag 数主导，level 与难度加权。 */
export function estLong(c: Challenge): number {
  return c.flag_count * 20 + (c.level ?? 0) * 10 + (bench.DIFF_RANK[c.difficulty] ?? 9) * 5;
}

/** D 调度排序（纯函数，可单测）。给定一组候选，返回启动顺序：
 *  1) 长题早启动——开局就把长题顶上去跑（不能最后才启动），但有短题在等时最多占 cap-1 槽
 *     （保底 1 槽给短题，防饿死）；
 *  2) 短题最短优先（est 升序）；
 *  3) 暂停题 aging——排队超过 agingSec 的提权插到新题前（防 fresh 无限插队，
 *     实测曾有题暂停后等 118 分钟才续跑），组内按等待时长降序。
 *  历史基线不进策略（正式比赛题目必变，只能当参考）。 */
export function orderCandidates(opts: {
  freshLong: Challenge[];
  freshShort: Challenge[];
  paused: Challenge[]; // 已按等待时长降序排好
  cap: number;
  agingSec: number;
  nowSec: number;
  waitAgeOf: (c: Challenge) => number; // 暂停题已等秒数
}): Challenge[] {
  const { freshLong, freshShort, paused, cap, agingSec, nowSec, waitAgeOf } = opts;
  const waitingShort = freshShort.length + paused.filter((c) => classifyTask(c) === "short").length;
  const longQuota = waitingShort > 0 ? Math.max(1, cap - 1) : cap;
  const longOrdered = [...freshLong].sort((a, b) => estLong(b) - estLong(a));
  const shortOrdered = [...freshShort].sort((a, b) => estLong(a) - estLong(b));
  const agedPaused = paused.filter((c) => nowSec - waitAgeOf(c) >= agingSec);
  const freshPaused = paused.filter((c) => nowSec - waitAgeOf(c) < agingSec);
  return [
    ...longOrdered.slice(0, longQuota),
    ...shortOrdered,
    ...agedPaused, // aging 提权：暂停够久的插队到新题前
    ...longOrdered.slice(longQuota),
    ...freshPaused,
  ];
}

/** 每题独立工作目录（async 局部 cwd 的落点），避免并行读写冲突。
 * 基目录派生自 RECORDS_DIR——保证 `tasks/<code>/` 里 conversation.jsonl 与 shell 产物
 * 永远同居一处（若这里另用 /work 判断，RECORDS_DIR 被显式改指别处时两者会分家）。 */
function taskWorkdir(code: string): string {
  const base = env("TASK_WORKDIR", "") || path.join(RECORDS_DIR, "tasks");
  const d = path.join(base, code);
  fs.mkdirSync(d, { recursive: true });
  return d;
}

/** 并行调度器：并发解多题，自动扩容直到平台拒绝（max active）。 */
async function runParallel(
  client: LlmClient,
  taskFilter: string[],
  results: Record<string, string>,
  attempts: Record<string, number>,
  snapshot: () => Promise<Challenge[] | null>,
): Promise<void> {
  const running = new Map<string, Promise<void>>();
  // A 策略下通关题会先从 running 摘除（释放槽位）再跑总结——worker Promise 还活着。
  // 收尾 join 要等的是 Promise 本身而不是槽位，所以另存全量清单，否则 run 结束会掐掉
  // 正在进行的总结。
  const allWorkers: Promise<void>[] = [];
  // 并发上限 = min(本地意愿, 平台 max-active 硬上限)。
  // 教训（用户实测 a-03…c-03 八题）：本地意愿默认 5 虚高于平台的 3，首轮长题配额按
  // cap-1=4 发 start → 平台只放行 3 个、全被长题（b-01/b-02/b-03）占满，短题一个没轮到；
  // 被 max-active 打回后 cap 才收缩，但槽已经被占死。所以一开始就取两者较小值，
  // 平台后续再拒绝（容量变化）时照旧收缩。
  let cap = Math.min(Number(env("AGENT_MAX_PARALLEL", "5")), CFG.platform_capacity);
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
  // D 策略：题目分类（long/short），启动时按 API 字段定类，段末可动态改判（治字段失真）。
  const taskClass = new Map<string, "long" | "short">();
  const cls = (c: Challenge): "long" | "short" => {
    let v = taskClass.get(c.unique_code);
    if (!v) {
      v = classifyTask(c);
      taskClass.set(c.unique_code, v);
    }
    return v;
  };

  /** 是否还有别的题在等这个并发名额（新题或待续跑的题）。 */
  const slotWaiters = (items: Challenge[] | null, selfCode: string): boolean => {
    if (!items?.length) return true;
    for (const c of items) {
      const cc = c.unique_code;
      if (cc === selfCode || c.is_completed) continue;
      if (!taskSelected(cc, taskFilter)) continue;
      if (running.has(cc) || results[cc] === "pass") continue;
      if ((failures[cc] ?? 0) >= Math.max(CFG.max_failures, 1)) continue;
      return true;
    }
    return false;
  };

  /** 有短题在等（决定长题配额 cap-1 还是 cap）——与 slotWaiters 同口径但只看短题。 */
  const anyWaitingShort = (items: Challenge[] | null, selfCode: string): boolean => {
    if (!items?.length) return false;
    const maxFail = Math.max(CFG.max_failures, 1);
    return items.some((c) => {
      const cc = c.unique_code;
      if (cc === selfCode || c.is_completed) return false;
      if (!taskSelected(cc, taskFilter)) return false;
      if (running.has(cc) || results[cc] === "pass") return false;
      if ((failures[cc] ?? 0) >= maxFail) return false;
      return cls(c) === "short";
    });
  };

  /** 当前在跑的长题数（含自己——自己在 running 里）。 */
  const countRunningLong = (): number => {
    let n = 0;
    for (const cc of running.keys()) if ((taskClass.get(cc) ?? "long") === "long") n++;
    return n;
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
    let containerReleased = false; // A：通关即关容器后置位，finally 里不再重复 close
    const conv = rec().conversation(code);
    let messages: ChatMessage[] | null = null;
    let branches: BranchLite[] = []; // fork 模式跨段分支状态
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
      // C 策略：段预算按分类取墙钟（短 600s/长 1800s）；时限临近时压缩到剩余的 1/6。
      const segBudgetSec = (): number => {
        const base = cls(task) === "long" ? CFG.segment_long_sec : CFG.segment_short_sec;
        const rem = remainingSec();
        return rem !== null ? Math.max(60, Math.min(base, Math.trunc(rem / 6))) : base;
      };
      let budgetSec = segBudgetSec();

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
            budget_min: Math.round(budgetSec / 60),
            turns: startTurn,
          });
          messages.push({ role: "user", content: note });
          conv.write("user", { branch: 0, turn: `${code}:${startTurn}`, content: note });
          // fork 分支重建（fork 关闭时为空）：父上下文按 fork_seq 截断，见 records.rebuildBranches
          if (CFG.fork_enabled) {
            branches = rec()
              .loadBranches(code)
              .map((b) => ({
                ...b,
                // cwd 不落盘（可由 workdir 确定性推导）：恢复时重建同一路径，产物不丢
                cwd: `${getTaskCwd() || taskWorkdir(code)}/branches/${b.id}`,
              }));
            for (const b of branches) {
              b.messages.push({ role: "user", content: note });
              conv.write("user", { branch: b.id, turn: `${code}:${startTurn}`, content: note });
            }
          }
          log(
            `[${code}] resume segment: ${startTurn} prior turns restored` +
              `${branches.length ? `, ${branches.length} branch(es)` : ""}, ` +
              `target=${addr} (was ${oldTarget || "?"})`,
          );
        } else {
          log(`[${code}] resume context unavailable; starting fresh segment`);
        }
      }

      // 本段起始 flag 数（D 改判判据）：首段用调度快照里的值，续段用上轮校验后的 state
      let segStartFlags = task.correct_flag_count ?? 0;
      for (;;) {
        const r = await solveTask(client, code, card, budgetSec, conv, {
          messages: messages ?? undefined,
          startTurn: startTurn,
          // A 策略：每轮问一次平台快照缓存（watcher 20s 刷新，零额外 API 调用）——
          // 通关即收束，不等段边界、不等模型自己想起来（b-02 白挂 22 分钟的教训）。
          completionCheck: () => confirmedComplete(code),
          branches: branches.length ? branches : undefined,
          taskClass: cls(task),
          flagTotal: task.flag_count,
        });
        status = r.status;
        last = r.lastContent;
        used = r.turnsUsed;
        messages = r.messages;
        branches = r.branches;

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

        // ---- 段到点处置（保留 segment_continue 收尾语义 + D 配额规则）----
        if (status === "paused" && !completed) {
          const segNewFlags = (state?.correct_flag_count ?? segStartFlags) - segStartFlags;
          // D 动态改判：字段说短、整段零新 flag → 实际是长题（治"easy 标签 200min"）
          let isLong = cls(task) === "long";
          if (!isLong && segNewFlags <= 0) {
            taskClass.set(code, "long");
            isLong = true;
            rec().event("task_reclassified", {
              code,
              to: "long",
              reason: "zero_new_flags_segment",
            });
            log(`[${code}] reclassified to LONG (segment produced 0 new flags)`);
          }
          // D 配额：有短题在等时长题最多占 cap-1 槽。
          // 配额内长题段到点不轮转（bench：轮走后 pickNext 立刻用另一道长题补回同槽，
          // 等待的短题一无所获，纯重启浪费——打断 88→31.6、完成持平）；
          // 超配额长题（怪兽题改判后鸠占鹊巢）必须让位；短题有等待者时照旧轮转。
          const quotaNow = anyWaitingShort(itemsNow, code) ? Math.max(1, cap - 1) : cap;
          const overQuota = isLong && countRunningLong() > quotaNow;
          if (!overQuota && (isLong || !slotWaiters(itemsNow, code))) {
            startTurn += used;
            segStartFlags = state?.correct_flag_count ?? segStartFlags;
            budgetSec = segBudgetSec();
            rec().event("segment_continue", {
              code,
              turns: startTurn,
              target: addr,
              class: isLong ? "long" : "short",
            });
            log(
              `[${code}] segment budget hit; continue in place ` +
                `(${startTurn} turns so far, no close/restart)`,
            );
            continue;
          }
        }
        break;
      }

      if (completed) {
        outcome = "pass";
        delete resumeMeta[code];
        // A 策略：通关即关容器——平台槽位立刻归还调度器（开新题不用等总结）；
        // 复盘总结脱槽跑（纯 LLM 调用不碰容器），限时 AGENT_SUMMARY_TIMEOUT_SEC。
        try {
          await bench.closeChallenge(code);
          containerReleased = true;
          rec().event("challenge_change", {
            code,
            field: "controller_close",
            old: "available",
            new: "closed",
            completed: true,
            note: "released immediately on completion (summary off-slot)",
          });
        } catch (e) {
          if (e instanceof PlatformError) {
            if (e.code !== "invalid_state") log(`[${code}] early close error: ${e.message}`);
          } else {
            log(`[${code}] early close network error: ${e instanceof Error ? e.message : e}`);
          }
        }
        running.delete(code); // 槽位从并发计数中摘除——总结期间调度器可开新题
        if (messages) {
          closingText = await closingSummary(client, code, messages, conv);
        }
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
            pausedAt: Date.now() / 1000, // D-aging：记录暂停时刻，续跑排序按等待时长提权
          };
          rec().event("task_paused", {
            code,
            pauses,
            gives,
            reason: giveup ? "giveup" : "segment_budget",
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
        if (!containerReleased) {
          await bench.closeChallenge(code);
          rec().event("challenge_change", {
            code,
            field: "controller_close",
            old: "available",
            new: "closed",
            completed: completedNow,
          });
        }
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
      if (running.has(c.unique_code)) return false; // Map 去重：必须用 has（in 检查属性，对 Map 恒 false）
      if (results[c.unique_code] === "pass") return false;
      if ((failures[c.unique_code] ?? 0) >= Math.max(CFG.max_failures, 1)) return false;
      return taskSelected(c.unique_code, taskFilter);
    };
    const eligibleTasks = items.filter(eligible);
    const freshLong = eligibleTasks.filter(
      (c) => !(c.unique_code in resumeMeta) && cls(c) === "long",
    );
    const freshShort = eligibleTasks.filter(
      (c) => !(c.unique_code in resumeMeta) && cls(c) === "short",
    );
    // 暂停题按等待时长降序（aging 组内最老先续）
    const pausedTasks = eligibleTasks
      .filter((c) => c.unique_code in resumeMeta)
      .sort((a, b) => {
        const at = resumeMeta[b.unique_code]?.pausedAt;
        const bt = resumeMeta[a.unique_code]?.pausedAt;
        return (typeof at === "number" ? at : 0) - (typeof bt === "number" ? bt : 0);
      });
    const candidates = orderCandidates({
      freshLong,
      freshShort,
      paused: pausedTasks,
      cap,
      agingSec: CFG.resume_aging_sec,
      nowSec: Date.now() / 1000,
      waitAgeOf: (c) => {
        const at = resumeMeta[c.unique_code]?.pausedAt;
        return typeof at === "number" ? Date.now() / 1000 - at : 0;
      },
    });

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
      allWorkers.push(p);
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
  for (const p of allWorkers) {
    const timeout = new Promise<void>((r) => setTimeout(r, 180_000));
    const done = await Promise.race([p.then(() => true), timeout.then(() => false)]);
    if (!done) log(`worker still running at shutdown; detached`);
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
