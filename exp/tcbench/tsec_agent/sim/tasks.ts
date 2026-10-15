/** 模拟用任务模型：字段（平台 API 可见）+ 隐藏真值（只有模拟器知道）。
 *
 * 设计原则：调度策略【只允许】读 public 字段（与真实平台一致），
 * trueWork/flagCurve 等真值只有 engine 推进世界时用——保证模拟不作弊。
 */

export interface TaskPublic {
  code: string;
  difficulty: "easy" | "medium" | "hard";
  level: number;
  flag_count: number;
  total_score: number;
}

export interface TaskTruth {
  /** 解完这道题需要的 agent 忙碌分钟数（含工具等待，墙钟代理） */
  totalWorkMin: number;
  /** flag 进度曲线：workMin 分钟后应得 flag 数（单调不减，终点=flag_count） */
  flagCurve: (workMin: number) => number;
  /** 模型中途输出"收尾总结"但没通关的概率（每段末判定） */
  giveupP: number;
  /** 模型知道已满 flag 后多久自然收尾（分钟）；A 策略下通关检查更快 */
  realizeDelayMin: number;
}

export interface SimTask extends TaskPublic {
  truth: TaskTruth;
  // ---- 调度器可变状态（engine 维护）----
  workDone: number; // 已投入 agent 分钟
  flagsGot: number;
  containerOpen: boolean;
  containerStarts: number; // 平台 start 次数（打断指标）
  agentBusy: boolean;
  status: "pending" | "running" | "paused" | "completed" | "failed" | "cut";
  lastStartAt: number | null; // 首次启动时刻（饿死检测）
  segments: number; // 已消耗段数
  zeroProgressSegs: number; // 连续零新 flag 段数（B 用）
  giveups: number; // 连续"没通关就收尾"次数
  classifiedLong: boolean; // D 的分类（可被动态改判）
  pausedAt: number | null;
}

/** 生成进度曲线：前 X% 工作量不产 flag（侦察期），之后线性到满。 */
function curve(totalWork: number, flagCount: number, reconFrac: number) {
  const recon = totalWork * reconFrac;
  return (workMin: number): number => {
    if (workMin <= recon) return 0;
    const frac = Math.min(1, (workMin - recon) / Math.max(1, totalWork - recon));
    return Math.max(0, Math.min(flagCount, Math.floor(frac * flagCount + 1e-9)));
  };
}

export interface GenOptions {
  seed: number;
  count?: number;
  /** 字段可信度 0..1：0=字段与真值完全无关（正式比赛可能如此），1=完全一致 */
  fieldFidelity?: number;
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 对数正态工作量：短题中位 ~3min，长尾到 200min（贴合归档实测 p50=3/p90=26/max=158）。 */
function lognormal(rnd: () => number, median: number, sigma: number): number {
  const u = Math.max(1e-9, rnd());
  const z = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rnd());
  return Math.max(0.3, median * Math.exp(sigma * z));
}

/** 真实配比：60 单 flag 短题 + 3 多 flag 长题（b 系列），字段基本可信。 */
export function genRealistic(opts: GenOptions): { pub: TaskPublic; truth: TaskTruth }[] {
  const rnd = mulberry32(opts.seed);
  const fidelity = opts.fieldFidelity ?? 0.85;
  const out: { pub: TaskPublic; truth: TaskTruth }[] = [];
  const n = opts.count ?? 63;
  for (let i = 0; i < n; i++) {
    const isLong = i >= n - 3; // 3 道多 flag 长题
    const flagCount = isLong ? (i === n - 2 ? 6 : 4) : 1;
    const trueWork = isLong
      ? lognormal(rnd, 90, 0.5) // 36~200min
      : lognormal(rnd, 3, 1.1); // 0.3~30min，偶发长尾
    // 字段：fidelity 概率下与真值一致，否则随机糊一个
    const easy = trueWork < 12;
    const rndEasy = rnd() < 0.4;
    const difficulty = (rnd() < fidelity ? (easy ? "easy" : trueWork < 60 ? "medium" : "hard") : rndEasy ? "easy" : "hard") as TaskPublic["difficulty"];
    const level = rnd() < fidelity ? Math.min(3, Math.ceil(trueWork / 40)) : 1 + Math.floor(rnd() * 3);
    out.push({
      pub: {
        code: `${String(i + 1).padStart(2, "0")}-${isLong ? "L" : "S"}`,
        difficulty,
        level,
        flag_count: flagCount,
        total_score: flagCount * (difficulty === "hard" ? 500 : difficulty === "medium" ? 250 : 100),
      },
      truth: {
        totalWorkMin: trueWork,
        flagCurve: curve(trueWork, flagCount, 0.35),
        giveupP: 0.12 + (isLong ? 0.18 : 0), // 长题更易中途放弃
        realizeDelayMin: 1 + rnd() * 3,
      },
    });
  }
  return out;
}

/** 极端：字段说 easy 但实际要 200min（字段失真压测）。 */
export function genEasySlow(opts: GenOptions): { pub: TaskPublic; truth: TaskTruth }[] {
  const rnd = mulberry32(opts.seed);
  const out = genRealistic({ seed: opts.seed + 7, count: opts.count ?? 40, fieldFidelity: 1 });
  for (const t of out.slice(0, 5)) {
    // 5 道 easy 标签的巨兽
    const w = 80 + rnd() * 140;
    t.pub.difficulty = "easy";
    t.pub.level = 1;
    t.truth.totalWorkMin = w;
    t.truth.flagCurve = curve(w, 1, 0.45);
    t.truth.giveupP = 0.3;
  }
  return out;
}

/** 极端：全短题洪水（看调度是否无谓占槽）。 */
export function genAllShort(opts: GenOptions): { pub: TaskPublic; truth: TaskTruth }[] {
  const rnd = mulberry32(opts.seed);
  const n = opts.count ?? 60;
  return Array.from({ length: n }, (_, i) => {
    const w = lognormal(rnd, 2, 0.6);
    return {
      pub: {
        code: `S${String(i + 1).padStart(2, "0")}`,
        difficulty: "easy" as const,
        level: 1,
        flag_count: 1,
        total_score: 100,
      },
      truth: {
        totalWorkMin: w,
        flagCurve: curve(w, 1, 0.3),
        giveupP: 0.08,
        realizeDelayMin: 1,
      },
    };
  });
}

/** 极端：长题占多数且部分物理上 6h 内完不成。 */
export function genAllLong(opts: GenOptions): { pub: TaskPublic; truth: TaskTruth }[] {
  const rnd = mulberry32(opts.seed);
  const n = opts.count ?? 18;
  return Array.from({ length: n }, (_, i) => {
    const w = i < 3 ? 400 + rnd() * 200 : 40 + rnd() * 120; // 3 道必然超 6h 槽容量
    const flags = 2 + Math.floor(rnd() * 5);
    return {
      pub: {
        code: `L${String(i + 1).padStart(2, "0")}`,
        difficulty: "hard" as const,
        level: 3,
        flag_count: flags,
        total_score: flags * 500,
      },
      truth: {
        totalWorkMin: w,
        flagCurve: curve(w, flags, 0.3),
        giveupP: 0.25,
        realizeDelayMin: 2,
      },
    };
  });
}

/** 随机 fuzz：参数全随机（性质测试用）。 */
export function genRandom(opts: GenOptions): { pub: TaskPublic; truth: TaskTruth }[] {
  const rnd = mulberry32(opts.seed);
  const n = opts.count ?? 10 + Math.floor(rnd() * 50);
  const fidelity = opts.fieldFidelity ?? rnd();
  const d: TaskPublic["difficulty"][] = ["easy", "medium", "hard"];
  return Array.from({ length: n }, (_, i) => {
    const w = lognormal(rnd, Math.exp(rnd() * 4 + 1), 1.2); // 中位 2.7~150
    const flags = rnd() < 0.15 ? 2 + Math.floor(rnd() * 5) : 1;
    const diff = d[Math.floor(rnd() * 3)];
    return {
      pub: {
        code: `F${String(i + 1).padStart(3, "0")}`,
        difficulty: (rnd() < fidelity ? (w < 12 ? "easy" : w < 60 ? "medium" : "hard") : diff) as TaskPublic["difficulty"],
        level: 1 + Math.floor(rnd() * 3),
        flag_count: flags,
        total_score: flags * 250,
      },
      truth: {
        totalWorkMin: w,
        flagCurve: curve(w, flags, 0.35),
        giveupP: rnd() * 0.4,
        realizeDelayMin: 1 + rnd() * 5,
      },
    };
  });
}

export const SCENARIOS: Record<string, (opts: GenOptions) => { pub: TaskPublic; truth: TaskTruth }[]> = {
  realistic: genRealistic,
  easySlow: genEasySlow,
  allShort: genAllShort,
  allLong: genAllLong,
  fuzz: genRandom,
};

export function toSimTasks(gen: { pub: TaskPublic; truth: TaskTruth }[]): SimTask[] {
  return gen.map((g) => ({
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
  }));
}
