/** 真实跑分样本 → 调度模拟任务。
 *
 * `fixtures/realrun.json` 由归档与线上事件 CSV 生成：
 * - 63 道题的 difficulty/level/flag_count/total_score 来自 a10cc… 归档的题目卡；
 * - current_sec 来自 run-36470-events.csv 的 start/close 配对累计；
 * - archive_sec 来自 3853c731… 归档的 tasks.jsonl（有则保留作第二组实测样本）。
 *
 * 这里只提交元数据与耗时、不提交题目描述/flag/答案；真值只给模拟器推进世界用，
 * 调度策略仍只能读 public 字段。
 */
import realrun from "./fixtures/realrun.json" with { type: "json" };
import { flagCurve, type SimTask, type TaskPublic, type TaskTruth } from "./tasks.js";

export interface RealRecord {
  code: string;
  difficulty: "easy" | "medium" | "hard";
  level: number;
  flag_count: number;
  total_score: number;
  current_sec: number | null;
  archive_sec: number | null;
}

export const REAL_RECORDS = realrun.records as RealRecord[];
/** 当前 run 原始事件里的 close→下一 start 间隔（用于证明“空档”确实发生过）。 */
export const REAL_GAPS = realrun.gaps;

function fromRecord(
  rec: RealRecord,
  opts: { code?: string; workMin?: number; giveupP?: number } = {},
): { pub: TaskPublic; truth: TaskTruth } {
  const workMin = Math.max(0.2, opts.workMin ?? (rec.current_sec ?? 120) / 60);
  return {
    pub: {
      code: opts.code ?? rec.code,
      difficulty: rec.difficulty,
      level: rec.level,
      flag_count: rec.flag_count,
      total_score: rec.total_score,
    },
    truth: {
      totalWorkMin: workMin,
      flagCurve: flagCurve(workMin, rec.flag_count, 0.35),
      giveupP: opts.giveupP ?? (rec.flag_count >= 2 ? 0.22 : 0.1),
      realizeDelayMin: 1,
    },
  };
}

/** 当前 run 的 63 题实测耗时。 */
export function realCurrentTasks(): SimTask[] {
  return REAL_RECORDS.map((r) => fromRecord(r)).map(toTask);
}

/** 3853 归档中有实测耗时的题；缺失的用 current 耗时补齐。 */
export function realArchiveTasks(): SimTask[] {
  return REAL_RECORDS.map((r) =>
    fromRecord(r, {
      workMin: r.archive_sec !== null ? r.archive_sec / 60 : undefined,
    }),
  ).map(toTask);
}

/** 对抗集：保留真实元数据分布，耗时做 lognormal 扰动，level/difficulty 随机洗牌，
 *  flag 数/分数也做少量变化；用于压测“字段与真值不一致”的调度器。 */
export function adversarialTasks(
  seed: number,
  opts: { count?: number; workScaleSigma?: number; scrambleMeta?: boolean } = {},
): SimTask[] {
  let a = seed >>> 0;
  const rnd = () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const norm = () => {
    const u = Math.max(1e-9, rnd());
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rnd());
  };
  const n = opts.count ?? REAL_RECORDS.length;
  const sigma = opts.workScaleSigma ?? 0.55;
  const diffs: Array<TaskPublic["difficulty"]> = ["easy", "medium", "hard"];
  const out: SimTask[] = [];
  for (let i = 0; i < n; i++) {
    const rec = REAL_RECORDS[Math.floor(rnd() * REAL_RECORDS.length)]!;
    const baseWork = (rec.current_sec ?? 120) / 60;
    const work = Math.max(0.3, baseWork * Math.exp(norm() * sigma));
    const scramble = opts.scrambleMeta ?? rnd() < 0.65;
    const flagCount = rnd() < 0.16 ? 2 + Math.floor(rnd() * 5) : 1;
    const difficulty = scramble
      ? diffs[Math.floor(rnd() * diffs.length)]!
      : rec.difficulty;
    const level = scramble ? 1 + Math.floor(rnd() * 3) : rec.level;
    out.push(
      toTask({
        pub: {
          code: `A${String(i + 1).padStart(2, "0")}-${rec.code}`,
          difficulty,
          level,
          flag_count: flagCount,
          total_score: Math.max(100, flagCount * (difficulty === "hard" ? 500 : 300)),
        },
        truth: {
          totalWorkMin: work,
          flagCurve: flagCurve(work, flagCount, 0.28 + rnd() * 0.3),
          giveupP: rnd() * (flagCount > 1 ? 0.4 : 0.25),
          realizeDelayMin: 1,
        },
      }),
    );
  }
  return out;
}

function toTask(g: { pub: TaskPublic; truth: TaskTruth }): SimTask {
  return {
    ...g.pub,
    truth: g.truth,
    workDone: 0,
    flagsGot: 0,
    containerOpen: false,
    containerStarts: 0,
    agentBusy: false,
    status: "pending",
    lastStartAt: null,
    segments: 0,
    zeroProgressSegs: 0,
    giveups: 0,
    classifiedLong: g.pub.flag_count >= 2 || g.pub.difficulty === "hard" || g.pub.level >= 3,
    pausedAt: null,
  };
}
