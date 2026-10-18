/** fork / report / log / state_change 四工具的 schema 与分发（broadcast 已移除）。
 *
 * 全 Attempt 同一套 schema（稳定前缀 → 缓存不炸，见 ../cache_exp/README.md 结论 2）；
 * 只有 AGENT_FORK 打开时才把对应工具挂进请求（callLlm 动态组装）。
 * 分发返回给模型的文本；副作用（corelog/messages）经 deps 回调，无循环依赖。
 *
 * fork 两阶段（协议安全，见 branch.ts）：
 *   dispatch 时 reserveFork 跑护栏 → 返回 pending fork；
 *   solveTask 在整轮工具结果回填完后 materializeFork（拷到的上下文 tool 配对完整）。
 */
import type { BranchManager, ForkReservation, ScopeStatus } from "./branch.js";
import { type CorelogWriter, type FlagLedger, pendingMutations, readCorelog } from "./corelog.js";

export const FORK_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "fork",
    description:
      "Split a new exploration attempt from your CURRENT context to pursue an independent " +
      "direction in parallel (another attack vector, a different service, a speculative lead). " +
      "The new attempt inherits everything you have seen so far and gets its own working " +
      "directory; you keep going down your current path. Only a few attempts are allowed " +
      "per task (refusals return a reason — continue then). Share findings via log " +
      "(planner reads it); end your direction with report.",
    parameters: {
      type: "object",
      properties: {
        direction: {
          type: "string",
          description:
            "One sentence: what this new attempt will explore that you are NOT doing " +
            "(e.g. 'brute-force the redis service on 6379' while you keep web SQLi).",
        },
      },
      required: ["direction"],
    },
  },
};

export const REPORT_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "report",
    description:
      "End THIS attempt; the result card is posted to the shared blackboard and planner. " +
      "Use it when your assigned direction is done, proven dead, or externally blocked. " +
      "The attempt is parked after reporting and will not be scheduled again; do not call " +
      "report just to pause and continue later. Report does not merge conversation " +
      "histories; it only shares the card.",
    parameters: {
      type: "object",
      properties: {
        status: {
          type: "string",
          enum: ["done", "deadend", "blocked"],
          description:
            "done = direction completed / goal reached; deadend = direction disproven or " +
            "exhausted; blocked = cannot continue because of an external dependency.",
        },
        summary: {
          type: "string",
          description: "One short paragraph: what was done, what was found, and why it ends.",
        },
        evidence: {
          type: "array",
          items: { type: "string" },
          description: "Optional key evidence lines (facts, commands, outputs).",
        },
        artifacts: {
          type: "array",
          items: { type: "string" },
          description: "Optional artifact paths/hashes that other attempts can reuse.",
        },
        next: {
          type: "array",
          items: { type: "string" },
          description: "Optional suggested next directions for the planner/blackboard.",
        },
      },
      required: ["status", "summary"],
    },
  },
};

export const STATE_CHANGE_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "state_change",
    description:
      "Record a SHARED TARGET mutation (service restart, config/file write, account/DB change) " +
      "and how to restore it after the target container restarts. Planner picks ONE recovery " +
      "owner on resume; other Attempts must not concurrently perform shared writes. After you " +
      "restore an assigned mutation, call this with status='recovered' and the mutation id.",
    parameters: {
      type: "object",
      properties: {
        action: {
          type: "string",
          description: "What you changed on the shared target (one line).",
        },
        recovery: {
          type: "string",
          description:
            "How to rebuild it after restart (commands, credentials, files, order). Required when status=done.",
        },
        id: {
          type: "string",
          description: "Mutation id returned by the done call; required when status=recovered.",
        },
        status: {
          type: "string",
          enum: ["done", "recovered"],
          description: "done = record a pending mutation; recovered = mark it restored.",
        },
        scope: {
          type: "string",
          enum: ["shared", "local"],
          description:
            "shared (default) = target mutation that needs recovery; local = only your workdir (no recovery).",
        },
      },
      required: ["action"],
    },
  },
};

export const CORE_LOG_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "log",
    description:
      "Record a key finding, new direction, dead end, or a captured flag as ONE short line " +
      "in the task's core log. Use it often: the log is how other Attempts and the planner " +
      "understand progress, and Attempts that go quiet without log/flag " +
      "activity get recycled. After submitting a flag, log `flag idx=N` (the " +
      "matched_flag_index from the submit response).",
    parameters: {
      type: "object",
      properties: {
        note: {
          type: "string",
          description:
            "One line: what was found / ruled out / decided. For flags use 'flag idx=N'.",
        },
      },
      required: ["note"],
    },
  },
};

export const BRANCH_TOOL_NAMES = ["fork", "report", "log", "state_change"];

/** fork 分发的结果：待 materialize 的 pending（由 solveTask 在轮末处理）。 */
export interface PendingFork {
  reservation: Extract<ForkReservation, { ok: true }>;
  parentId: number;
  direction: string;
  callId: string; // 工具结果回填用
}

export interface BranchToolDeps {
  bm: BranchManager;
  corelog: CorelogWriter | null;
  ledger: FlagLedger | null;
  now: () => number;
  log: (msg: string) => void;
  /** report 结果卡送达目标分支时回调（落 conv / event；messages 已由 bm 追加）。 */
  onReport?: (
    fromId: number,
    toIds: number[],
    card: string,
    status: "done" | "deadend" | "blocked",
    scopeStatus: ScopeStatus,
  ) => void;
  /** 重建完成：通知相关 scope 的 Attempt（agent 负责广播）。 */
  onMutationRecovered?: (id: string, scopeId: number, action: string) => void;
  /** log 条目交给 planner（不再有 broadcast 工具；信息先到 planner/黑板）。 */
  onLog?: (entry: {
    branch: number;
    scopeId: number;
    note: string;
    kind: "core" | "mutation" | "result" | "flag" | "artifact";
  }) => void;
}

export interface BranchToolOutcome {
  result: string; // 给模型的工具结果文本
  pendingFork?: PendingFork;
}

/** 分发非 shell 工具。返回 null = 不是本模块的工具（调用方按 unknown tool 处理）。 */
export function dispatchBranchTool(
  name: string,
  args: Record<string, unknown>,
  deps: BranchToolDeps,
  branchId: number,
  callId: string,
): BranchToolOutcome | null {
  const { bm, corelog, ledger, now } = deps;
  if (name === "fork") {
    const direction = String(args.direction ?? "").trim();
    if (!direction) return { result: "error: `direction` is required for fork" };
    const res = bm.reserveFork(branchId, now());
    if (!res.ok) return { result: `fork refused: ${res.reason}` };
    return {
      result:
        `branch ${res.id} reserved for direction: ${direction.slice(0, 120)}. ` +
        `It will start with your context copied so far; you continue on branch ${branchId}. ` +
        `Findings go to the planner/blackboard via log.`,
      pendingFork: { reservation: res, parentId: branchId, direction, callId },
    };
  }
  if (name === "report") {
    const rawStatus = String(args.status ?? "")
      .trim()
      .toLowerCase();
    if (!["done", "deadend", "blocked"].includes(rawStatus)) {
      return { result: "error: `status` must be one of done/deadend/blocked" };
    }
    const summary = String(args.summary ?? "").trim();
    if (!summary) return { result: "error: `summary` is required for report" };
    const asList = (v: unknown): string[] =>
      Array.isArray(v)
        ? v
            .map((x) => String(x).trim())
            .filter(Boolean)
            .slice(0, 20)
        : typeof v === "string" && v.trim()
          ? [v.trim()]
          : [];
    const status = rawStatus as "done" | "deadend" | "blocked";
    const res = bm.report(
      branchId,
      status,
      summary,
      asList(args.evidence),
      asList(args.artifacts),
      asList(args.next),
      now(),
    );
    if (res.ok) {
      corelog?.write({
        branch: branchId,
        kind: "result",
        status,
        note: summary.slice(0, 800),
      });
      deps.onReport?.(branchId, res.delivered, res.card, status, res.scopeStatus);
      deps.log(
        `[attempt ${branchId}] report ${status} -> ${res.delivered.join(",") || "-"} ` +
          `(scope ${res.scopeStatus})`,
      );
    }
    return { result: res.message };
  }
  if (name === "state_change") {
    const status = String(args.status ?? "done").toLowerCase();
    if (!["done", "recovered"].includes(status)) {
      return { result: "error: `status` must be done or recovered" };
    }
    const action = String(args.action ?? "").trim();
    if (!action) return { result: "error: `action` is required for state_change" };
    if (!corelog) return { result: "error: records not initialized" };
    if (status === "recovered") {
      const id = String(args.id ?? "").trim();
      const hit = pendingMutations(readCorelog(corelog.path)).find((m) =>
        id ? m.id === id : m.action === action,
      );
      if (!hit) {
        return { result: `error: no pending shared mutation for ${id || action}` };
      }
      corelog.write({
        branch: branchId,
        kind: "mutation",
        mutation_id: hit.id,
        action: hit.action,
        recovery: hit.recovery,
        scope_id: hit.scopeId,
        mutation_status: "recovered",
        shared: true,
        note: `recovered by attempt ${branchId}`,
      });
      deps.onMutationRecovered?.(hit.id, hit.scopeId, hit.action);
      deps.log(`[attempt ${branchId}] recovered mutation ${hit.id}: ${hit.action.slice(0, 80)}`);
      return { result: `recovered mutation ${hit.id}: ${hit.action.slice(0, 120)}` };
    }
    const recovery = String(args.recovery ?? "").trim();
    if (!recovery) {
      return { result: "error: `recovery` is required when status=done" };
    }
    const shared = args.scope !== "local";
    const b = bm.get(branchId);
    const id = `mu-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    corelog.write({
      branch: branchId,
      kind: "mutation",
      mutation_id: id,
      action: action.slice(0, 500),
      recovery: recovery.slice(0, 1000),
      scope_id: b?.scopeId ?? 0,
      mutation_status: "pending",
      shared,
      note: action.slice(0, 400),
    });
    deps.log(
      `[attempt ${branchId}] mutation ${id} (${shared ? "shared" : "local"}): ${action.slice(0, 80)}`,
    );
    return {
      result:
        `recorded mutation ${id} (${shared ? "shared" : "local"}). ` +
        `After target restart, planner will assign one recovery owner; ` +
        `finish recovery with state_change(status:"recovered", id:"${id}").`,
    };
  }
  if (name === "log") {
    const note = String(args.note ?? "").trim();
    if (!note) return { result: "error: `note` is required for log" };
    corelog?.write({ branch: branchId, kind: "core", note: note.slice(0, 400) });
    bm.recordCore(branchId, now());
    deps.onLog?.({
      branch: branchId,
      scopeId: bm.get(branchId)?.scopeId ?? 0,
      note: note.slice(0, 400),
      kind: "core",
    });
    // flag idx=N 的自述也进账本（权威仍以 watcher 快照 + submit 响应对账）
    const m = /flag\s+idx\s*=?\s*(\d+)/i.exec(note);
    if (m && ledger) ledger.add(Number(m[1]), branchId);
    return {
      result:
        `logged (branch ${branchId}): ${note.slice(0, 120)}` +
        `（黑板 tasks/<题目编号>/log.jsonl 全 Attempt 可读）`,
    };
  }
  return null;
}

/** fork 成功 materialize 后的落盘（fork 事件进 corelog；指令本身由调用方写 conv）。 */
export function afterForkMaterialized(
  corelog: CorelogWriter | null,
  parentId: number,
  newBranchId: number,
  direction: string,
): void {
  corelog?.write({
    branch: parentId,
    kind: "fork",
    note: `fork #${newBranchId}: ${direction.slice(0, 200)}`,
  });
}
