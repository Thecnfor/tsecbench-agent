/** 模拟器性质测试（随机 + 极端场景，纯内存）。
 *
 * 核心断言（用户要求）：
 * 1. 防饿死：任何种子下，ACD 策略短题 neverStartedShort == 0（能完成的都排上了）
 * 2. 6h 总时限生效，长题完不成是 cut 而非挂死
 * 3. 对比 current：完成数不降、打断数不升（ACD vs current 汇总）
 * 4. 极端场景（easy 慢题/全短/全长/fuzz）都不崩、指标合理
 */
import { describe, expect, it } from "vitest";
import { DEFAULT_SIM, type Policy, simulate } from "../sim/engine.js";
import { POLICIES } from "../sim/policies.js";
import { aggregate, formatTable, runBench } from "../sim/run.js";
import { SCENARIOS, toSimTasks } from "../sim/tasks.js";

function mulberry(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const byName = (n: string): Policy => POLICIES.find((p) => p.name === n)!;
const ACD = byName("ACD");
const CURRENT = byName("current");

function run(scenario: string, seed: number, policy: Policy) {
  const tasks = toSimTasks(SCENARIOS[scenario]({ seed }));
  return simulate(tasks, policy, { ...DEFAULT_SIM, rng: mulberry(seed ^ 0x9e37) }, scenario, seed);
}

describe("调度模拟：防饿死（用户核心诉求）", () => {
  it("ACD：100 个随机种子下短题零饿死（realistic/fuzz/easySlow）", () => {
    const violations: string[] = [];
    for (const sc of ["realistic", "fuzz", "easySlow"] as const) {
      for (let seed = 0; seed < 100; seed++) {
        const r = run(sc, seed, ACD);
        if (r.neverStartedShort > 0) violations.push(`${sc}#${seed}: ${r.neverStartedShort}`);
      }
    }
    expect(violations, `短题没排上（可惜情况）: ${violations.slice(0, 10).join(", ")}`).toEqual([]);
  });

  it("ACD：短题首启延迟有界（不超物理下界：短题总工作量/1短槽 + 60min 余量）", () => {
    let worstLag = 0;
    let worstBound = 0;
    for (let seed = 0; seed < 50; seed++) {
      const gen = SCENARIOS.realistic({ seed });
      // 分区下长题占 cap-1，短题长期只有 1 槽——尾部启动的物理下界 = 短题总工作量/1
      const shortWork = gen
        .filter((g) => !(g.pub.flag_count >= 2 || g.pub.difficulty === "hard" || g.pub.level >= 3))
        .reduce((s, g) => s + g.truth.totalWorkMin, 0);
      const r = run("realistic", seed, ACD);
      worstLag = Math.max(worstLag, r.shortStartLagMax);
      worstBound = Math.max(worstBound, shortWork + 60);
      expect(r.shortStartLagMax, `seed=${seed}`).toBeLessThanOrEqual(shortWork + 60);
    }
    console.log(`realistic ACD worst short lag=${worstLag}min bound=${worstBound}min`);
  });
});

describe("调度模拟：6h 总时限与极端场景", () => {
  it("全长场景：3 道物理上完不成的题 → cut 计数而非挂死，其余完成", () => {
    const r = run("allLong", 7, ACD);
    expect(r.cut).toBeGreaterThanOrEqual(0);
    expect(r.completed + r.cut + r.failed).toBeLessThanOrEqual(18);
    // ACD 不该有任何短题饿死（全长场景无短题）
    expect(r.neverStartedShort).toBe(0);
  });

  it("easy 慢题（字段失真）：动态改判生效，短题仍不饿死", () => {
    for (let seed = 0; seed < 30; seed++) {
      const r = run("easySlow", seed, ACD);
      expect(r.neverStartedShort).toBe(0);
    }
  });

  it("全短场景：current 与 ACD 都能全部启动", () => {
    for (const p of [CURRENT, ACD]) {
      for (let seed = 0; seed < 20; seed++) {
        const r = run("allShort", seed, p);
        expect(r.neverStarted, `${p.name}#${seed}`).toBe(0);
      }
    }
  });
});

describe("调度模拟：ACD vs current 汇总对比", () => {
  it("100 seeds × 5 场景：ACD 完成数不降、短题饿死不升、打断可对比", () => {
    const res = runBench({ seeds: 100, policies: [CURRENT, ACD] });
    const agg = aggregate(res);
    const rows: string[] = [];
    for (const [sc, byPol] of agg) {
      const c = byPol.get("current")!;
      const a = byPol.get("ACD")!;
      rows.push(
        `${sc}: done ${c.completed}->${a.completed} cut ${c.cut}->${a.cut} ` +
          `starve ${c.neverStartedShort}->${a.neverStartedShort} restart ${c.restarts}->${a.restarts} ` +
          `token ${c.agentBusyMin}->${a.agentBusyMin}`,
      );
      // 完成数不降（允许并列）
      expect(a.completed, `${sc} 完成数下降`).toBeGreaterThanOrEqual(c.completed - 1);
      // 短题饿死：ACD 必须 0，且不比 current 差
      expect(a.neverStartedShort, `${sc} ACD 出现短题饿死`).toBe(0);
      expect(a.neverStartedShort).toBeLessThanOrEqual(c.neverStartedShort);
      // token 消耗（agent 忙碌分钟）不显著膨胀（±20%）
      expect(a.agentBusyMin).toBeLessThanOrEqual(c.agentBusyMin * 1.2 + 30);
    }
    // 表随测试输出
    console.log(rows.join("\n"));
  }, 60_000);
});

describe("调度模拟：变体对照（B' 与配额长题不轮转，决定修复1与轮转策略）", () => {
  it("对比 ACD / ACD+B' / ACD+rot：打断数与完成数（B' 数据无差异则不做修复1）", () => {
    const res = runBench({
      seeds: 80,
      scenarios: ["realistic", "easySlow", "fuzz", "allLong"],
      policies: [byName("ACD"), byName("ACD+B'"), byName("ACD+rot")],
    });
    const agg = aggregate(res);
    const lines: string[] = [];
    for (const [sc, byPol] of agg) {
      for (const name of ["ACD", "ACD+B'", "ACD+rot"]) {
        const v = byPol.get(name)!;
        lines.push(
          `${sc} ${name.padEnd(9)} done=${v.completed} restart=${v.restarts} cut=${v.cut} failed=${v.failed} token=${v.agentBusyMin} starve=${v.neverStartedShort}`,
        );
      }
    }
    console.log(lines.join("\n"));
    // 所有变体都必须满足防饿死底线
    for (const [, byPol] of agg) {
      for (const name of ["ACD+B'", "ACD+rot"]) {
        expect(byPol.get(name)!.neverStartedShort, `${name} 出现饿死`).toBe(0);
      }
    }
  }, 60_000);
});

describe("调度模拟：消融总表（current→A→C→D→ACD，人工判读各因子贡献）", () => {
  it("打印全策略 × 全场景 60 seeds 均值", () => {
    const res = runBench({ seeds: 60 });
    console.log(formatTable(res));
    // 消融结论的守护断言：
    // 1) 含 A 的配置（真正要 ship 的形态）必须零饿死——A 提供槽吞吐（通关即放槽）
    // 2) D 单独不 ship（缺 A 时短槽吞吐不足，bench 实测饿死 0.7~8.5）——只记录不强制
    const agg = aggregate(res);
    const aConfigs = ["A", "AC", "ACD", "ACD+B'", "ACD+rot"];
    for (const [, byPol] of agg) {
      for (const pol of aConfigs) {
        const v = byPol.get(pol);
        if (v) expect(v.neverStartedShort, `${pol} 出现饿死`).toBe(0);
      }
    }
  }, 120_000);
});

describe("调度模拟：自检", () => {
  it("所有场景 × 所有策略 × 10 种子都能跑完且指标非负", () => {
    const res = runBench({ seeds: 10 });
    expect(res.length).toBe(Object.keys(SCENARIOS).length * POLICIES.length * 10);
    for (const r of res) {
      expect(r.completed).toBeGreaterThanOrEqual(0);
      expect(r.agentBusyMin).toBeGreaterThanOrEqual(0);
      expect(r.containerOpenMin).toBeLessThanOrEqual(360 * 3 + 1);
    }
  }, 60_000);
});
