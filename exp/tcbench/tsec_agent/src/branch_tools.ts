/** fork / core_log / broadcast 三工具的 schema 与分发。
 *
 * 全分支同一套 schema（稳定前缀 → 缓存不炸，见 ../cache_exp/README.md 结论 2）；
 * 只有 AGENT_FORK / AGENT_BYSTANDER 打开时才把对应工具挂进请求（callLlm 动态组装）。
 * 分发返回给模型的文本；副作用（corelog/messages）经 deps 回调，无循环依赖。
 *
 * fork 两阶段（协议安全，见 branch.ts）：
 *   dispatch 时 reserveFork 跑护栏 → 返回 pending fork；
 *   solveTask 在整轮工具结果回填完后 materializeFork（拷到的上下文 tool 配对完整）。
 */
import type { BranchManager, ForkReservation } from "./branch.js";
import type { CorelogWriter, FlagLedger } from "./corelog.js";

export const FORK_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "fork",
    description:
      "Split a new exploration branch from your CURRENT context to pursue an independent " +
      "direction in parallel (another attack vector, a different service, a speculative lead). " +
      "The new branch inherits everything you have seen so far and gets its own working " +
      "directory; you keep going down your current path. Only a few branches are allowed " +
      "per task (refusals return a reason — continue then). Communicate findings to other " +
      "branches with broadcast; record key discoveries with core_log.",
    parameters: {
      type: "object",
      properties: {
        direction: {
          type: "string",
          description:
            "One sentence: what this new branch will explore that you are NOT doing " +
            "(e.g. 'brute-force the redis service on 6379' while you keep web SQLi).",
        },
      },
      required: ["direction"],
    },
  },
};

export const CORE_LOG_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "core_log",
    description:
      "Record a key finding, new direction, dead end, or a captured flag as ONE short line " +
      "in the task's core log. Use it often: the log is how other branches and the outer " +
      "observer understand progress, and branches that go quiet without core_log/flag " +
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

export const BROADCAST_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "broadcast",
    description:
      "Send a one-line message (tail-appended into targets' conversations at the next " +
      "turn). Topology: a non-trunk branch may only send UP to the trunk (branch 0) — " +
      "lateral/sibling targets are rerouted there, the trunk coordinates redistribution " +
      "(children never message each other directly); the trunk may broadcast to any branch. " +
      "Use for: sharing a discovery, warning about a destructive action you are about to " +
      "take on the shared target (service restarts, port binding, config changes — outside " +
      "your namespace, keep read-only unless coordinated via trunk), or asking for help. " +
      "Near-duplicate broadcasts are suppressed; rate-limited per segment.",
    parameters: {
      type: "object",
      properties: {
        note: { type: "string", description: "One line to share." },
        to: {
          description:
            'Targets: "all" (default), "trunk", or an array of branch ids like [0,2]. ' +
            "For non-trunk senders everything resolves to trunk (0).",
          oneOf: [
            { type: "string", enum: ["all", "trunk"] },
            { type: "array", items: { type: "integer" } },
          ],
        },
      },
      required: ["note"],
    },
  },
};

export const BRANCH_TOOL_NAMES = ["fork", "core_log", "broadcast"];

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
  /** broadcast 送达目标分支时回调（solveTask 负责给每个目标分支落 conv 记录）。 */
  onBroadcast?: (fromId: number, toIds: number[], note: string) => void;
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
        `Findings flow between branches via broadcast/core_log.`,
      pendingFork: { reservation: res, parentId: branchId, direction, callId },
    };
  }
  if (name === "core_log") {
    const note = String(args.note ?? "").trim();
    if (!note) return { result: "error: `note` is required for core_log" };
    corelog?.write({ branch: branchId, kind: "core", note: note.slice(0, 400) });
    bm.recordCore(branchId, now());
    // flag idx=N 的自述也进账本（权威仍以 watcher 快照 + submit 响应对账）
    const m = /flag\s+idx\s*=?\s*(\d+)/i.exec(note);
    if (m && ledger) ledger.add(Number(m[1]), branchId);
    return {
      result:
        `logged to core log (branch ${branchId}): ${note.slice(0, 120)}` +
        `（黑板 tasks/<题目编号>/corelog.jsonl 全分支可读）`,
    };
  }
  if (name === "broadcast") {
    const note = String(args.note ?? "").trim();
    if (!note) return { result: "error: `note` is required for broadcast" };
    const res = bm.broadcast(branchId, note, args.to ?? "all", now());
    if (res.ok) {
      corelog?.write({
        branch: branchId,
        kind: "broadcast",
        note: note.slice(0, 400),
        to: res.delivered,
      });
      deps.onBroadcast?.(branchId, res.delivered, note);
      deps.log(
        `[branch ${branchId}] broadcast -> ${res.delivered.join(",")}: ${note.slice(0, 80)}`,
      );
    }
    return { result: res.message };
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
