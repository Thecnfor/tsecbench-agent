/** 真实归档/CSV 调度回归：
 *
 * 数据来源（见 sim/fixtures/realrun.json）：
 * - a10cc… 归档题目卡：63 题的 difficulty/level/flag_count/total_score；
 * - run-36470-events.csv：current_sec（每个 start/close 段累计）；
 * - 3853c731… 归档：archive_sec 作为第二组实测耗时。
 *
 * 断言的是调度器不变量，不是“跑分越高越好”：
 * 1. 只要还有未完成题，不允许出现没有容器打开的调度空档（idleGapMin）；
 *    `noRunningGapMin` 单独记录启动等待成本，不能把容器 start 当成“没有调度”；
 * 2. 未完成题被 close/暂停后必须能重新排上（pausedNeverResumed == 0）；
 * 3. 随机容器启动耗时、字段失真的对抗集也必须满足上述不变量。
 */
import { describe, expect, it } from "vitest";
import { DEFAULT_SIM, type Policy, simulate } from "../sim/engine.js";
import { POLICIES } from "../sim/policies.js";
import {
  adversarialTasks,
  REAL_GAPS,
  realArchiveTasks,
  realCurrentTasks,
} from "../sim/realdata.js";

const PROD = POLICIES.find((p) => p.name === "PROD")!;
const ACD = POLICIES.find((p) => p.name === "ACD")!;
const CURRENT = POLICIES.find((p) => p.name === "current")!;

function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function run(tasks: ReturnType<typeof realCurrentTasks>, policy: Policy, seed: number) {
  return simulate(
    tasks,
    policy,
    {
      ...DEFAULT_SIM,
      totalMin: 360,
      startupMinMin: 0.15,
      startupMinMax: 1.8,
      rng: rng(seed ^ 0x51ed),
    },
    "real-current",
    seed,
  );
}

describe("真实归档/CSV 调度不变量", () => {
  it("PROD：当前 run 的 63 题，无容器空档且暂停题能重新排上", () => {
    // 原始 CSV 确实出现过 close→下一 start 的长空档（fixture.source 有来源说明）。
    expect(REAL_GAPS.max_sec).toBeGreaterThanOrEqual(60);
    for (let seed = 0; seed < 20; seed++) {
      const r = run(realCurrentTasks(), PROD, seed);
      expect(r.idleGapMin, `seed=${seed} idleGap`).toBe(0);
      expect(r.idleEpisodes, `seed=${seed} idleEpisodes`).toBe(0);
      expect(r.pausedNeverResumed, `seed=${seed} pausedNeverResumed`).toBe(0);
      expect(r.pausedTasks, `seed=${seed} should exercise pause/requeue`).toBeGreaterThan(0);
    }
  });

  it("PROD：3853 归档耗时组合同样通过", () => {
    for (let seed = 0; seed < 20; seed++) {
      const r = run(realArchiveTasks(), PROD, seed);
      expect(r.idleGapMin, `seed=${seed} idleGap`).toBe(0);
      expect(r.idleEpisodes, `seed=${seed} idleEpisodes`).toBe(0);
      expect(r.pausedNeverResumed, `seed=${seed} pausedNeverResumed`).toBe(0);
    }
  });

  it("段预算收窄对照：短 420 + 长题续段 1500/900 vs 旧 600/1800", () => {
    const narrow: Policy = {
      ...PROD,
      fixedShortSec: 420,
      longProgressSec: 1500,
      longStalledSec: 900,
    };
    const legacy: Policy = { ...PROD, fixedShortSec: 600 };
    const rows: string[] = [];
    for (const [name, policy] of [
      ["legacy 600/1800", legacy],
      ["narrow 420/1500/900", narrow],
    ] as const) {
      let done = 0;
      let restarts = 0;
      let busy = 0;
      let idle = 0;
      const seeds = 20;
      for (let seed = 0; seed < seeds; seed++) {
        const r = run(realCurrentTasks(), policy, seed);
        done += r.completed;
        restarts += r.restarts;
        busy += r.agentBusyMin;
        idle += r.idleGapMin;
      }
      rows.push(
        `${name}: done=${done / seeds} restart=${(restarts / seeds).toFixed(1)} ` +
          `busy=${(busy / seeds).toFixed(1)} idle=${idle}`,
      );
      expect(idle, `${name} idle`).toBe(0);
    }
    console.log(rows.join("\n"));
    expect(rows).toHaveLength(2);
  });

  it("无进展熔断：归档长尾题不再无限原地续段，冷却仍保留续排", () => {
    const noBreaker: Policy = { ...PROD, noProgressPauseLimit: 0 };
    let onBreaks = 0;
    let offBreaks = 0;
    for (let seed = 0; seed < 20; seed++) {
      // 归档耗时组有 33% 的长尾段；关闭熔断时会反复原地续段。
      const a = run(realArchiveTasks(), PROD, seed);
      const b = run(realArchiveTasks(), noBreaker, seed);
      onBreaks += a.noProgressBreaks;
      offBreaks += b.noProgressBreaks;
      expect(a.idleGapMin, `seed=${seed} idleGap`).toBe(0);
      expect(a.pausedNeverResumed, `seed=${seed} pausedNeverResumed`).toBe(0);
    }
    console.log(`breaker=${onBreaks} no-breaker=${offBreaks}`);
    expect(onBreaks).toBeGreaterThan(0);
    expect(offBreaks).toBe(0);
  });

  it("对抗：真实元数据 + 随机耗损/字段失真 + 随机容器启动", () => {
    for (let seed = 0; seed < 200; seed++) {
      const r = run(adversarialTasks(seed), PROD, seed);
      expect(r.idleGapMin, `seed=${seed} idleGap`).toBe(0);
      expect(r.idleEpisodes, `seed=${seed} idleEpisodes`).toBe(0);
      expect(r.pausedNeverResumed, `seed=${seed} pausedNeverResumed`).toBe(0);
    }
  });

  it("对照组：标出旧策略的空档/不可续排问题（不要求旧策略通过）", () => {
    const rows: string[] = [];
    for (const policy of [CURRENT, ACD, PROD]) {
      let idle = 0;
      let stuck = 0;
      for (let seed = 0; seed < 10; seed++) {
        const r = run(realCurrentTasks(), policy, seed);
        idle += r.idleGapMin;
        stuck += r.pausedNeverResumed;
      }
      rows.push(`${policy.name}: idle=${idle} pausedNeverResumed=${stuck}`);
    }
    console.log(rows.join("\n"));
    // 只记录对照；真正 ship 的 PROD 由上面断言保证。
    expect(rows).toHaveLength(3);
  });
});
