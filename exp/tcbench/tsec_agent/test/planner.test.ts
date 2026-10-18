/** Planner 启发式单测：只做“deadend + next → checkpoint spawn”的最小决策，
 * 与 BranchManager 的真实执行解耦。 */
import { describe, expect, it } from "vitest";
import {
  type CoverageEvent,
  heuristicPlan,
  type ReportEvent,
  selectRecoveryOwner,
} from "../src/planner.js";

const base: ReportEvent = {
  type: "report",
  attempt: 1,
  status: "deadend",
  summary: "HTTP 走死",
  next: ["动态 trace"],
  scopeId: 1,
  scopeStatus: "refuted",
};

const ctx = {
  enabled: true,
  canSpawn: () => true,
  nudgeAvailable: () => true,
  nudgeExhausted: () => false,
  remainingFlags: () => 2,
};

describe("heuristic planner", () => {
  it("recovery owner：优先共享写最多的 scope，其次 id 最小，只选一个", () => {
    const attempts = [
      { id: 0, scopeId: 0, status: "active" },
      { id: 1, scopeId: 1, status: "active" },
      { id: 2, scopeId: 2, status: "active" },
      { id: 3, scopeId: 1, status: "active" },
    ];
    const mutations = [
      {
        id: "a",
        action: "x",
        recovery: "r",
        scopeId: 1,
        branch: 1,
        shared: true,
        status: "pending" as const,
      },
      {
        id: "b",
        action: "y",
        recovery: "r",
        scopeId: 1,
        branch: 3,
        shared: true,
        status: "pending" as const,
      },
      {
        id: "c",
        action: "z",
        recovery: "r",
        scopeId: 2,
        branch: 2,
        shared: true,
        status: "pending" as const,
      },
    ];
    expect(selectRecoveryOwner(mutations, attempts)).toBe(1);
    expect(selectRecoveryOwner([], attempts)).toBe(0);
    expect(selectRecoveryOwner(mutations, [{ id: 2, scopeId: 2, status: "deadend" }], 0)).toBe(0);
  });

  it("deadend + next：retire source + 把 next 放入待定队列", () => {
    expect(heuristicPlan(base, ctx)).toEqual([
      { type: "retire", attempt: 1, reason: "deadend; free slot for queued next direction" },
      {
        type: "enqueue_next",
        source: 1,
        direction: "动态 trace",
        scopeId: 1,
        reason: "deadend direction has next surface; queue for a free slot",
      },
    ]);
  });

  it("done + next：还有 flag 时进入待定队列，不能因方向完成丢掉下一条面", () => {
    const done = { ...base, status: "done" as const, next: ["file-server FTP/SSH"] };
    const actions = heuristicPlan(done, { ...ctx, remainingFlags: () => 3 });
    expect(actions).toEqual([
      {
        type: "enqueue_next",
        source: 1,
        direction: "file-server FTP/SSH",
        scopeId: 1,
        reason: "done direction has next surface; queue for a free slot",
      },
    ]);
    expect(heuristicPlan(done, { ...ctx, remainingFlags: () => 0 })).toEqual([]);
    expect(heuristicPlan({ ...done, next: [] }, { ...ctx, remainingFlags: () => 2 })).toEqual([]);
  });

  it("无 next / 无剩余 flag / blocked / 禁用 → 不动作", () => {
    expect(heuristicPlan({ ...base, next: [] }, ctx)).toEqual([]);
    expect(heuristicPlan({ ...base, next: [] }, { ...ctx, remainingFlags: () => 0 })).toEqual([]);
    expect(heuristicPlan({ ...base, status: "blocked" }, ctx)).toEqual([]);
    expect(heuristicPlan(base, { ...ctx, remainingFlags: () => 0 })).toEqual([]);
    expect(heuristicPlan(base, { ...ctx, enabled: false })).toEqual([]);
  });

  it("flag 日志事件 → 给该 Attempt 加权", () => {
    const actions = heuristicPlan(
      { type: "log", attempt: 2, scopeId: 2, note: "flag idx=1", kind: "flag" },
      ctx,
    );
    expect(actions).toContainEqual({
      type: "set_weight",
      attempt: 2,
      weight: 2,
      reason: "flag progress; give this attempt more scheduling share",
    });
  });

  it("coverage：active/scope 不足 → 提醒按 fork 分类补全；覆盖到位不动作", () => {
    const coverage: CoverageEvent = {
      type: "coverage",
      attempt: 0,
      scopeId: 0,
      active: 1,
      scopes: 1,
      desired: 2,
    };
    const actions = heuristicPlan(coverage, ctx);
    expect(actions).toHaveLength(1);
    expect(actions[0].type).toBe("remind");
    expect(JSON.stringify(actions)).toContain("覆盖检查");
    expect(JSON.stringify(actions)).toContain("fork");
    expect(heuristicPlan({ ...coverage, active: 2, scopes: 2 }, ctx)).toEqual([]);
    expect(heuristicPlan({ ...coverage, active: 2, scopes: 1 }, ctx)[0]?.type).toBe("remind");
    expect(heuristicPlan(coverage, { ...ctx, canSpawn: () => false })).toEqual([]);
    expect(heuristicPlan(coverage, { ...ctx, nudgeAvailable: () => false })).toEqual([]);
  });

  it("stall：有额度→remind；额度耗尽→retire（含初始 Attempt）", () => {
    const stall = {
      type: "stall" as const,
      attempt: 1,
      signals: {
        flagStall: false,
        stall: true,
        repeat: true,
        hardFail: false,
        hallucSubmit: false,
        count: 2,
      },
      sinceCoreSec: 300,
      recentCmds: ["nmap x", "nmap x", "nmap x"],
      digest: "- assistant: ...",
      scopeId: 1,
      scopeStatus: "open" as const,
    };
    const nudge = heuristicPlan(stall, ctx);
    expect(nudge.length).toBe(1);
    expect(nudge[0].type).toBe("remind");
    expect(JSON.stringify(nudge)).toContain("[planner 提醒]");

    const close = heuristicPlan(stall, {
      ...ctx,
      nudgeAvailable: () => false,
      nudgeExhausted: () => true,
    });
    expect(close).toEqual([
      { type: "retire", attempt: 1, reason: "stalled after planner nudge quota" },
    ]);
    expect(
      heuristicPlan(
        { ...stall, attempt: 0 },
        {
          ...ctx,
          nudgeAvailable: () => false,
          nudgeExhausted: () => true,
        },
      ),
    ).toEqual([{ type: "retire", attempt: 0, reason: "stalled after planner nudge quota" }]);
    // 额度未耗尽但冷却中 → 不提醒也不关闭
    expect(
      heuristicPlan(stall, {
        ...ctx,
        nudgeAvailable: () => false,
        nudgeExhausted: () => false,
      }),
    ).toEqual([]);
  });

  it("槽满时 next 仍然入队；Attempt 0 不 retire", () => {
    expect(heuristicPlan(base, { ...ctx, canSpawn: () => false })).toEqual([
      { type: "retire", attempt: 1, reason: "deadend; free slot for queued next direction" },
      {
        type: "enqueue_next",
        source: 1,
        direction: "动态 trace",
        scopeId: 1,
        reason: "deadend direction has next surface; queue for a free slot",
      },
    ]);
    expect(heuristicPlan({ ...base, attempt: 0 }, ctx)).toEqual([
      {
        type: "enqueue_next",
        source: 0,
        direction: "动态 trace",
        scopeId: 1,
        reason: "deadend direction has next surface; queue for a free slot",
      },
    ]);
  });
});
