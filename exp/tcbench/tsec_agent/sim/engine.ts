/** 离散事件引擎：1 分钟粒度推进，策略可插拔（消融对比用）。
 *
 * 世界规则（贴真实实现）：
 * - 平台容器槽 = 3（max active），agent 并发 = 5；总结阶段占 agent 不占容器（A）。
 * - 段闸 C：turnProxy=60轮的墙钟代理（每题抽样 ~17min 中位）；fixed=600s/1800s。
 * - 通关判定 A：每 tick 查缓存快照（watcher 20s 代理延迟）→ 立即关容器；
 *   非 A：模型自然收尾（realizeDelay×伯努利）或段末检查。
 * - segment_continue 原语义（保留）：段到点且**无等待者** → 不关不启原地续，
 *   直到 bench 时限（设计意图：收尾只剩长任务时不打断它们）。
 *   有等待者 → close+requeue（打断 +1，付 resume 重定向代价）。
 * - 模型放弃：段末 giveupP → 关题重排；连续 giveupMax 次 → failed。
 * - bench 总时限到：在跑/在排的未完成题 → cut（按设计接受）；
 *   从未启动的短题 → neverStartedShort（唯一"可惜"指标）。
 *
 * 防作弊：调度器（pickNext/reclassify）只能读 public 字段与可观测状态；
 * truth.totalWorkMin 只在世界推进处使用。
 */
import type { SimTask } from "./tasks.js";

export interface Policy {
  name: string;
  /** A：通关每 tick 检查 + 关容器 */
  earlyCloseOnComplete: boolean;
  /** C：段闸模式 */
  segmentMode: "turnProxy" | "fixed";
  fixedShortSec: number;
  fixedLongSec: number;
  /** B'（待验证变体）：原地续上限 Infinity=原语义保留 */
  inPlaceMax: number;
  forceRotateOnZeroProgress: boolean;
  /** D：槽分区 + 长题早启动 + 暂停 aging + 动态改判 */
  slotPartition: boolean;
  resumeAgingMin: number;
  reclassify: boolean;
  /** 配额内长题段到点不轮转（有等者也原地续；false=轮转） */
  rotateQuotaLong: boolean;
}

export interface SimConfig {
  totalMin: number;
  containerCap: number;
  agentCap: number;
  giveupMax: number;
  resumePenaltyMin: number; // 续跑重定向代价（agent 分钟，探新容器/重建状态）
  restartSlotMin: number; // 平台 start 占槽时长
  summaryMaxMin: number; // 总结超时上限（用户定：5min）
  watcherDelayMin: number; // A 的快照缓存延迟（20s watcher 代理）
  rng: () => number;
}

export const DEFAULT_SIM: Omit<SimConfig, "rng"> = {
  totalMin: 360, // bench 总时限 6h
  containerCap: 3,
  agentCap: 5,
  giveupMax: 3,
  resumePenaltyMin: 2,
  restartSlotMin: 1,
  summaryMaxMin: 5,
  watcherDelayMin: 0.35,
};

export interface SimResult {
  policy: string;
  scenario: string;
  seed: number;
  completed: number;
  score: number;
  /** 时限到仍未完成（含在跑在排）——终态长任务按设计接受 */
  cut: number;
  failed: number;
  /** 从未启动 */
  neverStarted: number;
  /** 唯一"可惜"指标：短题到时限都没排上 */
  neverStartedShort: number;
  /** SHORT 题首次启动最大延迟(min) */
  shortStartLagMax: number;
  /** 打断数 = 容器 start 次数 - 首启题数 */
  restarts: number;
  containerStarts: number;
  /** token 代理：agent 忙碌分钟 */
  agentBusyMin: number;
  /** 槽位压力：容器占用分钟 */
  containerOpenMin: number;
  makeSpanMin: number;
  // ---- 泄漏探针（诊断用）----
  agentBusyAtEnd: number;
  containerOpenAtEnd: number;
  neverStartedCodes: string[];
}

interface Sched extends SimTask {
  inPlaceCount: number;
  segStartWork: number;
  segStartFlags: number;
  paceSec: number; // turnProxy 段长
  pausedAt: number | null;
  pendingStart: boolean;
  startDoneAt: number | null;
  readyAt: number | null; // A：通关检测时刻
  naturalDoneAt: number | null; // 非 A：模型自然收尾时刻
  summaryLeft: number;
  summaryHoldsContainer: boolean;
  waitingSince: number | null; // aging
}

const DIFF_RANK = { easy: 0, medium: 1, hard: 2 } as const;

/** 公开字段估算（调度唯一允许的"预估"，不用真值）。 */
function est(t: SimTask): number {
  return t.flag_count * 20 + t.level * 10 + DIFF_RANK[t.difficulty] * 5;
}

export function simulate(
  tasks: SimTask[],
  policy: Policy,
  cfg: SimConfig,
  scenario: string,
  seed: number,
): SimResult {
  const rng = cfg.rng;
  const S: Sched[] = tasks.map((t) => ({
    ...t,
    inPlaceCount: 0,
    segStartWork: 0,
    segStartFlags: 0,
    paceSec: 17 * 60 * Math.exp((rng() + rng() + rng() - 1.5) * 0.55),
    pausedAt: null,
    pendingStart: false,
    startDoneAt: null,
    readyAt: null,
    naturalDoneAt: null,
    summaryLeft: 0,
    summaryHoldsContainer: false,
    waitingSince: null,
  }));

  let agentBusy = 0;
  let completed = 0;
  let score = 0;
  let failed = 0;
  let starts = 0;
  let agentBusyMin = 0;
  let containerOpenMin = 0;
  let makeSpan = 0;
  let shortLagMax = 0;

  const isOpen = (t: Sched) => t.containerOpen;
  const openCount = () => S.filter(isOpen).length;
  const longRunning = () => S.filter((t) => isOpen(t) && t.classifiedLong).length;
  /** 有短题在等（pending/paused）——配额判定与 pickNext 保持一致。 */
  const waitShortAny = () =>
    S.some(
      (o) =>
        (o.status === "pending" || o.status === "paused") &&
        !o.classifiedLong,
    );
  const waiters = (self: Sched) =>
    S.some((o) => o !== self && (o.status === "pending" || o.status === "paused"));

  const budgetMin = (t: Sched): number =>
    policy.segmentMode === "fixed"
      ? (t.classifiedLong ? policy.fixedLongSec : policy.fixedShortSec) / 60
      : t.paceSec / 60;

  const closeContainer = (t: Sched) => {
    t.containerOpen = false;
    t.pendingStart = false;
    t.startDoneAt = null;
  };

  /** 通关处理（A 立即关容器；非 A 总结期间占着容器）。
   *  总结复用解题 worker 的 agent 槽（同线程）——不释放，总结完才释放。 */
  const complete = (t: Sched, now: number) => {
    t.status = "completed";
    completed++;
    score += t.total_score;
    makeSpan = Math.max(makeSpan, now);
    t.summaryLeft = Math.min(cfg.summaryMaxMin, 1 + rng() * 2.5);
    t.summaryHoldsContainer = !policy.earlyCloseOnComplete;
    if (policy.earlyCloseOnComplete) closeContainer(t);
    // agentBusy 保持（解题→总结同线程），summary 推进处释放
  };

  /** 重排队。givesReset=true：非 giveup 的段到点（忠实真实实现 gives=0）。 */
  const requeue = (t: Sched, now: number, givesReset = true) => {
    closeContainer(t);
    t.status = "paused";
    t.pausedAt = now;
    t.waitingSince = now;
    t.segStartWork = t.workDone;
    t.inPlaceCount = 0;
    t.readyAt = null;
    t.naturalDoneAt = null;
    if (givesReset) t.giveups = 0;
    t.segStartFlags = t.flagsGot;
    t.agentBusy = false;
    agentBusy = Math.max(0, agentBusy - 1);
  };

  /** D：选下一个启动的题（只读 public 字段与可观测状态）。 */
  const pickNext = (): Sched | null => {
    const pending = S.filter((t) => t.status === "pending" && !t.pendingStart);
    const paused = S.filter((t) => t.status === "paused" && !t.pendingStart);
    if (!pending.length && !paused.length) return null;
    const byEst = (a: Sched, b: Sched) => est(a) - est(b) || (a.code < b.code ? -1 : 1);

    if (!policy.slotPartition) {
      // current：fresh 绝对优先，题内由易到难；暂停题按已耗段数（pauses 代理）
      const freshOrder = (a: Sched, b: Sched) =>
        DIFF_RANK[a.difficulty] - DIFF_RANK[b.difficulty] || a.level - b.level || (a.code < b.code ? -1 : 1);
      if (pending.length) return [...pending].sort(freshOrder)[0];
      return [...paused].sort((a, b) => a.segments - b.segments || freshOrder(a, b))[0];
    }

    // D：长题早启动（配额 cap-1 当有短题在等），队内最老等待优先（round-robin 防同题反复选回）
    const waitShort = [...pending, ...paused].filter((t) => !t.classifiedLong);
    const waitLong = [...pending, ...paused].filter((t) => t.classifiedLong);
    const longQuota = waitShort.length ? cfg.containerCap - 1 : cfg.containerCap;
    if (longRunning() < Math.min(longQuota, waitLong.length)) {
      // 首启长题：估算大者先（长任务不能最后才启动）；续排按等待时长
      const isFreshL = (t: Sched) => t.lastStartAt === null;
      const c = [...waitLong].sort((a, b) => {
        const fa = isFreshL(a);
        const fb = isFreshL(b);
        if (fa !== fb) return fa ? -1 : 1;
        if (fa) return est(b) - est(a); // 估算大者先（长任务早启动）
        return waitAge(b) - waitAge(a); // 暂停题最老先
      })[0];
      if (c) return c;
    }
    const pool = [...waitShort, ...(longRunning() >= longQuota ? waitLong : [])];
    if (!pool.length) return pending[0] ?? paused[0] ?? null;
    const isFresh = (t: Sched) => t.lastStartAt === null;
    pool.sort((a, b) => {
      // fresh 优先（从未启动的先跑，短者先）；暂停题按等待时长降序（最老先，防 LIFO 饿死）
      const fa = isFresh(a);
      const fb = isFresh(b);
      if (fa !== fb) return fa ? -1 : 1;
      if (fa) return byEst(a, b);
      return waitAge(b) - waitAge(a);
    });
    return pool[0];
  };

  /** 等待时长：pending(从未启动)=now-0；paused=now-暂停时刻。 */
  const waitAge = (t: Sched): number =>
    t.waitingSince !== null ? nowRef.v - t.waitingSince : nowRef.v;

  const nowRef = { v: 0 };

  for (let now = 0; now < cfg.totalMin; now++) {
    nowRef.v = now;

    // ---------- 1. 推进 ----------
    for (const t of S) {
      if (["completed", "failed", "cut"].includes(t.status)) {
        // 总结阶段独立推进（完成题继续持有其 agent 槽直到总结完）
        if (t.status === "completed" && t.agentBusy && t.summaryLeft > 0) {
          t.summaryLeft -= 1;
          agentBusyMin++;
          if (t.summaryLeft <= 0) {
            t.agentBusy = false;
            agentBusy--;
            if (t.summaryHoldsContainer) closeContainer(t);
          }
        }
        continue;
      }
      if (t.status === "paused" || t.status === "pending") continue;

      // running
      if (t.pendingStart) {
        if (t.startDoneAt !== null && now >= t.startDoneAt) {
          t.pendingStart = false;
          t.startDoneAt = null;
          t.agentBusy = true;
          agentBusy++;
        } else {
          containerOpenMin++;
          continue; // 平台 start 中，占槽不干活
        }
      }
      if (!t.agentBusy) continue;

      // 1 分钟工作
      t.workDone += 1;
      agentBusyMin++;
      containerOpenMin++;
      t.flagsGot = Math.max(t.flagsGot, t.truth.flagCurve(t.workDone));
      const allFlags = t.flagsGot >= t.flag_count;
      const doneWork = t.workDone >= t.truth.totalWorkMin - 1e-9;

      // --- A：通关检测（watcher 缓存延迟） ---
      if (policy.earlyCloseOnComplete && allFlags && doneWork) {
        if (t.readyAt === null) t.readyAt = now + Math.ceil(cfg.watcherDelayMin);
        if (now >= t.readyAt) {
          complete(t, now);
          continue;
        }
      }
      // --- 非 A：模型自然收尾 ---
      if (!policy.earlyCloseOnComplete && allFlags && doneWork) {
        if (t.naturalDoneAt === null) {
          t.naturalDoneAt = rng() < 0.5 ? now + t.truth.realizeDelayMin : Number.POSITIVE_INFINITY;
        }
        if (now >= t.naturalDoneAt) {
          complete(t, now);
          continue;
        }
      }

      // --- 段闸 ---
      if (t.workDone - t.segStartWork < budgetMin(t)) continue;
      t.segments++;
      const segFlagsNew = t.flagsGot - t.segStartFlags; // 本段新 flag（整段增量）

      // 段末通关检查（两种策略的兜底）
      if (allFlags && doneWork) {
        complete(t, now);
        continue;
      }

      // 模型放弃（没通关就收尾总结）
      if (rng() < t.truth.giveupP) {
        t.giveups++;
        if (t.giveups >= cfg.giveupMax) {
          t.status = "failed";
          closeContainer(t);
          failed++;
          t.agentBusy = false;
          agentBusy--;
          continue;
        }
        requeue(t, now, false); // giveup 路径：保留计数
        continue;
      }

      // --- 段到点改判（D，可观测：本段 0 新 flag 且非首段→字段说短其实慢） ---
      if (policy.reclassify && !t.classifiedLong && segFlagsNew <= 0 && !allFlags) {
        t.classifiedLong = true; // 字段说短、整段零新 flag → 提前改判
      }

      // --- 段到点处置 ---
      // 超配额长题必须让位（如怪兽题以短题身份占槽、段末改判长题后 longRunning>cap-1）；
      // 配额内长题一律原地续——把它轮走后 pickNext 的配额分支会立刻用另一道长题
      // 补回同一槽，等待的短题什么也得不到，纯重启浪费（bench 实测：打断 14.6→3）。
      const quotaNow = waitShortAny() ? cfg.containerCap - 1 : cfg.containerCap;
      const overQuota = t.classifiedLong && longRunning() > quotaNow;
      const noRotateQuota = policy.slotPartition && !policy.rotateQuotaLong && !overQuota;
      const noWaiter = !waiters(t);
      const overCap = t.inPlaceCount + 1 > policy.inPlaceMax;
      const rotateZero =
        policy.forceRotateOnZeroProgress && segFlagsNew <= 0 && t.inPlaceCount >= 1;
      if ((noWaiter || noRotateQuota) && !overCap && !rotateZero) {
        // segment_continue 原语义：原地续，不关不启，直到 bench 时限
        t.inPlaceCount++;
        t.segStartWork = t.workDone;
        continue;
      }
      requeue(t, now); // 有等待者 → 关题让位（打断）
    }

    // ---------- 3. 调度 ----------
    if (agentBusy < cfg.agentCap) {
      for (let slot = openCount(); slot < cfg.containerCap; slot++) {
        const nxt = pickNext();
        if (!nxt) break;
        nxt.status = "running";
        nxt.containerOpen = true;
        nxt.pendingStart = true;
        nxt.startDoneAt = now + cfg.restartSlotMin;
        nxt.lastStartAt = nxt.lastStartAt ?? now;
        nxt.waitingSince = null;
        nxt.containerStarts++;
        starts++;
        if (nxt.segments > 0) {
          // 续跑重定向代价（可观测的重启成本）
          nxt.workDone = Math.min(nxt.truth.totalWorkMin, nxt.workDone + cfg.resumePenaltyMin);
          nxt.segStartWork = nxt.workDone;
        }
        nxt.segStartFlags = nxt.flagsGot;
      }
    }
  }

  // ---------- 4. 时限收尾 ----------
  let cut = 0;
  for (const t of S) {
    if (t.status === "running" || t.status === "paused") cut++;
  }
  const neverStarted = S.filter((t) => t.lastStartAt === null).length;
  const neverStartedShort = S.filter((t) => t.lastStartAt === null && !t.classifiedLong).length;
  for (const t of S) {
    if (t.lastStartAt !== null && !t.classifiedLong) shortLagMax = Math.max(shortLagMax, t.lastStartAt);
  }

  return {
    policy: policy.name,
    scenario,
    seed,
    agentBusyAtEnd: agentBusy,
    containerOpenAtEnd: openCount(),
    neverStartedCodes: S.filter((t) => t.lastStartAt === null).map((t) => t.code),
    completed,
    score,
    cut,
    failed,
    neverStarted,
    neverStartedShort,
    shortStartLagMax: shortLagMax,
    restarts: Math.max(0, starts - S.filter((t) => t.lastStartAt !== null).length),
    containerStarts: starts,
    agentBusyMin,
    containerOpenMin,
    makeSpanMin: Math.round(makeSpan),
  };
}
