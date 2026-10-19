/** bench 运行器：多场景 × 多策略 × N 种子 → 指标表（纯内存，秒级）。
 *
 * 由 test/sim.bench.test.ts 调用（表随测试输出），
 * 或 `pnpm bench`（scripts 里 node --experimental-strip-types 直跑 TS）。
 */
import { SCENARIOS, toSimTasks } from "./tasks.js";
import { DEFAULT_SIM, simulate, type Policy, type SimConfig, type SimResult } from "./engine.js";
import { POLICIES } from "./policies.js";

export interface BenchOptions {
  seeds?: number;
  scenarios?: string[];
  policies?: Policy[];
  config?: Partial<SimConfig>;
  seedBase?: number;
}

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

export function runBench(opts: BenchOptions = {}): SimResult[] {
  const seeds = opts.seeds ?? 50;
  const scenarios = opts.scenarios ?? Object.keys(SCENARIOS);
  const policies = opts.policies ?? POLICIES;
  const out: SimResult[] = [];
  for (const sc of scenarios) {
    for (const p of policies) {
      for (let i = 0; i < seeds; i++) {
        const seed = (opts.seedBase ?? 1000) + i;
        const tasks = toSimTasks(SCENARIOS[sc]({ seed }));
        out.push(
          simulate(
            tasks,
            p,
            { ...DEFAULT_SIM, ...opts.config, rng: mulberry(seed ^ 0x9e37) },
            sc,
            seed,
          ),
        );
      }
    }
  }
  return out;
}

export const METRICS = [
  "completed",
  "score",
  "cut",
  "failed",
  "neverStartedShort",
  "shortStartLagMax",
  "restarts",
  "agentBusyMin",
  "containerOpenMin",
  "idleGapMin",
  "idleEpisodes",
  "noRunningGapMin",
  "pausedTasks",
  "pausedResumed",
  "pausedNeverResumed",
  "maxResumeGapMin",
  "noProgressBreaks",
] as const;

export type MetricName = (typeof METRICS)[number];

/** 均值表：scenario → policy → metric 均值。 */
export function aggregate(results: SimResult[]): Map<string, Map<string, Record<MetricName, number>>> {
  const acc = new Map<string, Map<string, { sum: Partial<Record<MetricName, number>>; n: number }>>();
  for (const r of results) {
    if (!acc.has(r.scenario)) acc.set(r.scenario, new Map());
    const byPol = acc.get(r.scenario)!;
    if (!byPol.has(r.policy)) byPol.set(r.policy, { sum: {}, n: 0 });
    const cell = byPol.get(r.policy)!;
    cell.n++;
    for (const m of METRICS) cell.sum[m] = (cell.sum[m] ?? 0) + (r as unknown as Record<MetricName, number>)[m];
  }
  const out = new Map<string, Map<string, Record<MetricName, number>>>();
  for (const [sc, byPol] of acc) {
    const rowMap = new Map<string, Record<MetricName, number>>();
    for (const [pol, cell] of byPol) {
      const avg = {} as Record<MetricName, number>;
      for (const m of METRICS) avg[m] = Math.round(((cell.sum[m] ?? 0) / cell.n) * 10) / 10;
      rowMap.set(pol, avg);
    }
    out.set(sc, rowMap);
  }
  return out;
}

export function formatTable(results: SimResult[]): string {
  const agg = aggregate(results);
  const lines: string[] = [];
  const seeds = new Set(results.map((r) => r.seed)).size;
  for (const [sc, byPol] of agg) {
    lines.push(`\n### scenario=${sc}（${seeds} seeds 均值）`);
    const header = ["policy", ...METRICS].map((h) => h.padEnd(h === "policy" ? 10 : 17)).join("");
    lines.push(header);
    for (const p of POLICIES) {
      const v = byPol.get(p.name);
      if (!v) continue;
      lines.push(
        [p.name.padEnd(10), ...METRICS.map((m) => String(v[m]).padEnd(17))].join(""),
      );
    }
  }
  return lines.join("\n");
}
