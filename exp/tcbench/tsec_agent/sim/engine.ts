/** 离散事件引擎：1 分钟粒度推进，策略可插拔（消融对比用）。
 *
 * 世界规则（贴真实实现）：
 * - 平台容器槽 = 3（max active），agent 并发 = 5；总结阶段脱槽，不阻塞容器补位（A）。
 * - 容器 start 可以配置为随机耗时（startupMinMin/Max），用于压测 start 空档。
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
  /** 只有 fresh（从未启动）题算 waiter；暂停题不触发 giveup 的 close→start。 */
  noWaiterFreshOnly?: boolean;
  /** 有 flag 进展的暂停题优先于 fresh 题。 */
  pausedProgressPriority?: boolean;
  /** 无得分题收尾时给有进展的暂停题让位，但这次让位不累计 giveup。 */
  yieldProgress?: boolean;
  /** 长题续段自适应：上一段有新 flag → progressSec，零新 flag → stalledSec；0=关闭。 */
  longProgressSec?: number;
  longStalledSec?: number;
  /** 无用重启熔断：连续 N 段零新 flag → 暂停并冷却 cooldownMin；0=关闭。 */
  noProgressPauseLimit?: number;
  noProgressCooldownMin?: number;
}

export interface SimConfig {
  totalMin: number;
  containerCap: number;
  agentCap: number;
  giveupMax: number;
  resumePenaltyMin: number; // 续跑重定向代价（agent 分钟，探新容器/重建状态）
  restartSlotMin: number; // 平台 start 占槽时长（固定模式的兼容值）
  /** 随机容器启动耗时（分钟）；设置后每次 start 独立抽样，覆盖 restartSlotMin。 */
  startupMinMin?: number;
  startupMinMax?: number;
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
  /** 未完成题存在、但没有任何容器打开的分钟数（真正的调度空档）。 */
  idleGapMin: number;
  /** 上述空档的连续 episode 数。 */
  idleEpisodes: number;
  /** 未完成题存在、但没有一个 worker 在真正解题（start 等待也算空档）的分钟数。 */
  noRunningGapMin: number;
  /** 被关闭/暂停的未完成题 episode 数（可重复计同一题）。 */
  pausedEpisodes: number;
  /** 至少暂停过一次的题数。 */
  pausedTasks: number;
  /** 暂停后至少重新启动过一次的题数。 */
  pausedResumed: number;
  /** 暂停后直到时限结束都没有再启动的题数。 */
  pausedNeverResumed: number;
  /** 无进展熔断触发次数（连续 N 段零新 flag → 冷却）。 */
  noProgressBreaks: number;
  /** 单题连续零新 flag 段数的最大值（诊断：熔断是否根本没用上）。 */
  maxZeroFlagSegs: number;
  /** 关闭→重新启动的最大等待（分钟）。 */
  maxResumeGapMin: number;
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
  let idleGapMin = 0;
  let idleEpisodes = 0;
  let idleEP = false;
  let noRunningGapMin = 0;
  let pausedEpisodes = 0;
  let noProgressBreaks = 0;
  let maxResumeGapMin = 0;
  const pausedEver = new Set<string>();
  const resumedAfterPause = new Set<string>();
  const lastPauseAt = new Map<string, number>();

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
  /** fresh（从未启动）waiter：giveup 决策只看它，暂停题不再互相触发 close→start。 */
  const freshWaiters = (self: Sched) =>
    S.some((o) => o !== self && o.status === "pending");
  /** 已拿到部分 flag 的暂停题；无得分题应把槽位让给它。 */
  const pausedProgress = (self: Sched) =>
    S.filter((o) => o !== self && o.status === "paused" && o.flagsGot > 0);
  /** 无进展熔断：连续零新 flag 段数（有 flag 清零；跨暂停保持）。 */
  const zeroSegs = new Map<string, number>();
  // 首段前的侦察期不算“无进展段”：从第二轮段开始后才计数（避免把 0.3~0.9 分钟的小段算成熔断）。
  const maxZeroSegs = new Map<string, number>();
  const cooldownUntil = new Map<string, number>();

  /** 段预算：长题续段按上一段是否产出新 flag 自适应（首段仍用 fixedLongSec）。 */
  const budgetMin = (t: Sched): number => {
    if (policy.segmentMode !== "fixed") return t.paceSec / 60;
    if (!t.classifiedLong) return policy.fixedShortSec / 60;
    if (t.segments > 0 && policy.longProgressSec && policy.longStalledSec) {
      const lastSegNew = t.flagsGot - t.segStartFlags;
      return (lastSegNew > 0 ? policy.longProgressSec : policy.longStalledSec) / 60;
    }
    return policy.fixedLongSec / 60;
  };

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
    pausedEver.add(t.code);
    pausedEpisodes++;
    lastPauseAt.set(t.code, now);
    t.segStartWork = t.workDone;
    t.inPlaceCount = 0;
    t.readyAt = null;
    t.naturalDoneAt = null;
    if (givesReset) t.giveups = 0;
    t.segStartFlags = t.flagsGot;
    t.agentBusy = false;
    agentBusy = Math.max(0, agentBusy - 1);
  };

  /** 段结束后更新无进展计数；返回是否进入冷却（熔断）。 */
  const noteSegProgress = (t: Sched, segNew: number, now: number): boolean => {
    // 首段零 flag 是正常的侦察段；只统计“已经跑过至少一段”后的连续零进展。
    const prevSegs = t.segments;
    const z = segNew > 0 || prevSegs < 2 ? 0 : (zeroSegs.get(t.code) ?? 0) + 1;
    if (z === 0) zeroSegs.delete(t.code);
    zeroSegs.set(t.code, z);
    maxZeroSegs.set(t.code, Math.max(maxZeroSegs.get(t.code) ?? 0, z));
    const limit = policy.noProgressPauseLimit ?? 0;
    if (limit > 0 && z >= limit) {
      if ((cooldownUntil.get(t.code) ?? -1) <= now) noProgressBreaks++;
      cooldownUntil.set(t.code, now + (policy.noProgressCooldownMin ?? 10));
      return true;
    }
    return false;
  };
  const isCooled = (t: Sched, now: number) => (cooldownUntil.get(t.code) ?? -1) > now;

  /** D：选下一个启动的题（只读 public 字段与可观测状态）。 */
  const pickNext = (): Sched | null => {
    const allPending = S.filter((t) => t.status === "pending" && !t.pendingStart);
    const allPaused = S.filter((t) => t.status === "paused" && !t.pendingStart);
    const warm = (t: Sched) => !isCooled(t, nowRef.v);
    const pending = allPending.filter(warm);
    const paused = allPaused.filter(warm);
    if (!pending.length && !paused.length) {
      // 全部都在冷却：仍然运行（题目不能死），但只剩最后一个兜底名额。
      return allPending[0] ?? allPaused[0] ?? null;
    }
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
    const hasProgress = (t: Sched) =>
      !!policy.pausedProgressPriority && t.status === "paused" && t.flagsGot > 0;
    if (longRunning() < Math.min(longQuota, waitLong.length)) {
      // 首启长题：估算大者先（长任务不能最后才启动）；续排按等待时长
      const isFreshL = (t: Sched) => t.lastStartAt === null;
      const c = [...waitLong].sort((a, b) => {
        const pa = hasProgress(a);
        const pb = hasProgress(b);
        if (pa !== pb) return pa ? -1 : 1;
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
      // 有 flag 进展的暂停题优先于 fresh；fresh 内短题先；暂停题最老优先。
      const pa = hasProgress(a);
      const pb = hasProgress(b);
      if (pa !== pb) return pa ? -1 : 1;
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
        const noFresh = !!policy.noWaiterFreshOnly && !freshWaiters(t);
        const progress = !!policy.yieldProgress && pausedProgress(t).length > 0;
        const stalledTooLong = noteSegProgress(t, 0, now);
        // 无 fresh 新题：giveup 原地续，不关题、不累计 giveup（生产 no-waiter 语义）。
        if (noFresh && !progress && !stalledTooLong) {
          t.inPlaceCount++;
          t.segStartWork = t.workDone;
          continue;
        }
        // 有部分 flag 的暂停题在等：让位但不把这次收尾计入 giveup。
        if (progress) {
          requeue(t, now, false);
          continue;
        }
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
      const stalledTooLong = noteSegProgress(t, segFlagsNew, now);
      if ((noWaiter || noRotateQuota) && !overCap && !rotateZero && !stalledTooLong) {
        // segment_continue 原语义：原地续，不关不启，直到 bench 时限
        t.inPlaceCount++;
        t.segStartWork = t.workDone;
        continue;
      }
      requeue(t, now); // 有等待者 → 关题让位（打断）
    }

    // ---------- 3. 调度 ----------
    // 容器槽只受平台 containerCap 限制；通关总结走脱槽调用，不占新的解题槽
    // （真实 runParallel 在通关总结前已把 worker 从 running 摘除）。旧模拟把 summary
    // 计入 agentCap，会出现“容器槽空着但调度器不补题”的假空档。
    const solvingCount = S.filter((t) => t.status === "running" && !t.pendingStart).length;
    if (solvingCount < cfg.agentCap && openCount() < cfg.containerCap) {
      for (let slot = openCount(); slot < cfg.containerCap; slot++) {
        const nxt = pickNext();
        if (!nxt) break;
        if (nxt.status === "paused") {
          resumedAfterPause.add(nxt.code);
          const pausedAt = lastPauseAt.get(nxt.code);
          if (pausedAt !== undefined) {
            maxResumeGapMin = Math.max(maxResumeGapMin, now - pausedAt);
          }
        }
        nxt.status = "running";
        nxt.containerOpen = true;
        nxt.pendingStart = true;
        const startup =
          cfg.startupMinMin !== undefined && cfg.startupMinMax !== undefined
            ? cfg.startupMinMin + rng() * Math.max(0, cfg.startupMinMax - cfg.startupMinMin)
            : cfg.restartSlotMin;
        nxt.startDoneAt = now + startup;
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

    // 空档探针：还有未完成题（pending/paused/running）却没有任何容器打开，
    // 或没有任何 worker 真正进入解题（容器启动等待阶段也算 noRunning）。
    const unresolved = S.some(
      (t) => t.status === "pending" || t.status === "paused" || t.status === "running",
    );
    const solving = S.some((t) => t.status === "running" && !t.pendingStart);
    if (unresolved && openCount() === 0) {
      idleGapMin++;
      if (!idleEP) {
        idleEpisodes++;
        idleEP = true;
      }
    } else {
      idleEP = false;
    }
    if (unresolved && !solving) noRunningGapMin++;
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
    idleGapMin,
    idleEpisodes,
    noRunningGapMin,
    pausedEpisodes,
    pausedTasks: pausedEver.size,
    pausedResumed: resumedAfterPause.size,
    pausedNeverResumed: Math.max(0, pausedEver.size - resumedAfterPause.size),
    maxResumeGapMin,
    noProgressBreaks,
    maxZeroFlagSegs: Math.max(0, ...maxZeroSegs.values()),
  };
}
