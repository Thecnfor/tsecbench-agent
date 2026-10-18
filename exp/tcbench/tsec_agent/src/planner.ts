/** Planner：唯一统筹者（不是 Attempt，不跑 shell）。
 *
 * 输入是 Attempt 主动 report 后的事件；输出是结构化动作，由 controller 校验执行。
 * P1 先用启发式：deadend 且 result card 给了 next，就从该 Attempt 的 checkpoint
 * 开一条兄弟尝试（回溯），并回收 source 释放并发槽。后续可换成 LLM planner，
 * 但动作集与校验仍在这里。
 */
import type { ScopeStatus } from "./branch.js";
import type { MutationRecord } from "./corelog.js";
import type { StallSignals } from "./stall.js";
import { plannerNudgeText } from "./stall.js";

export type ReportStatus = "done" | "deadend" | "blocked";

export interface ReportEvent {
  type: "report";
  attempt: number;
  status: ReportStatus;
  summary: string;
  next: string[];
  scopeId: number;
  scopeStatus: ScopeStatus;
}

/** 程序检测到某个 Attempt 卡塞后投给 planner 的事件（不经过 LLM observer）。 */
export interface StallEvent {
  type: "stall";
  attempt: number;
  signals: StallSignals;
  sinceCoreSec: number;
  recentCmds: string[];
  digest: string;
  scopeId: number;
  scopeStatus: ScopeStatus;
}

export interface LogEvent {
  type: "log";
  attempt: number;
  scopeId: number;
  note: string;
  kind: "core" | "result" | "flag" | "mutation" | "artifact";
}

export type PlannerEvent = ReportEvent | StallEvent | LogEvent;

export type PlannerAction =
  | { type: "retire"; attempt: number; reason: string }
  | { type: "spawn"; source: number; direction: string; scopeId: number; reason: string }
  | { type: "close_scope"; scopeId: number; reason: string }
  | { type: "remind"; attempt: number; text: string; reason: string };

export interface PlannerContext {
  enabled: boolean;
  canSpawn: (source: number) => boolean;
  nudgeAvailable: (attempt: number) => boolean; // 额度 + 冷却都允许
  nudgeExhausted: (attempt: number) => boolean; // 额度已用尽（不看冷却）
}

/** 启发式：report 事件做 checkpoint 回溯；stall 事件先提醒，额度用完则回收（Attempt 0 除外）。 */
export function heuristicPlan(ev: PlannerEvent, ctx: PlannerContext): PlannerAction[] {
  if (!ctx.enabled) return [];
  if (ev.type === "log") return []; // 当前 heuristic 只吸收；LLM planner 可在未来据此路由/提醒
  if (ev.type === "stall") {
    if (ctx.nudgeAvailable(ev.attempt)) {
      return [
        {
          type: "remind",
          attempt: ev.attempt,
          text: plannerNudgeText({
            attempt: ev.attempt,
            sinceCoreSec: ev.sinceCoreSec,
            signals: ev.signals,
            recentCmds: ev.recentCmds,
          }),
          reason: "stall signals; nudge before closing",
        },
      ];
    }
    // 提醒额度用完仍卡：planner 可关闭任意 Attempt（含初始 Attempt）。
    if (ctx.nudgeExhausted(ev.attempt)) {
      return [
        {
          type: "retire",
          attempt: ev.attempt,
          reason: "stalled after planner nudge quota",
        },
      ];
    }
    return [];
  }

  if (ev.status !== "deadend") return [];
  if (ev.scopeStatus === "satisfied") return [];
  const direction = ev.next.map((s) => s.trim()).find((s) => s.length > 0);
  if (!direction) return [];
  if (!ctx.canSpawn(ev.attempt)) return [];

  const actions: PlannerAction[] = [];
  // Attempt 0 是初始 Attempt，不回收；其他 deadend Attempt 让位给回溯兄弟。
  if (ev.attempt !== 0) {
    actions.push({
      type: "retire",
      attempt: ev.attempt,
      reason: "deadend; free slot for checkpoint backtrack",
    });
  }
  actions.push({
    type: "spawn",
    source: ev.attempt,
    direction: direction.slice(0, 200),
    scopeId: ev.scopeId,
    reason: "planner backtrack from checkpoint (no failed tail copied)",
  });
  return actions;
}

/** 重启恢复：从仍 active 的 Attempt 中选一个 recovery owner。
 *  优先留下共享写最多的 scope 的 Attempt；并列取 id 最小；无匹配则初始 Attempt 0。
 *  只选一个，避免多个子 Attempt 同时写共享靶机。 */
export function selectRecoveryOwner(
  mutations: MutationRecord[],
  attempts: Array<{ id: number; scopeId: number; status: string }>,
  fallback = 0,
): number {
  const active = attempts.filter((a) => a.status === "active");
  if (!active.length) return fallback;
  const counts = new Map<number, number>();
  for (const m of mutations) counts.set(m.scopeId, (counts.get(m.scopeId) ?? 0) + 1);
  const candidates = active.filter((a) => counts.has(a.scopeId));
  if (candidates.length) {
    candidates.sort(
      (a, b) => (counts.get(b.scopeId) ?? 0) - (counts.get(a.scopeId) ?? 0) || a.id - b.id,
    );
    return candidates[0].id;
  }
  return active.some((a) => a.id === fallback) ? fallback : active[0].id;
}
