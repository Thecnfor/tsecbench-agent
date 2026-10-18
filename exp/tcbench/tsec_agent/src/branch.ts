/** fork 多分支执行面：分支状态、护栏（上限/冷却/剪枝）、轮转调度、广播。
 *
 * 设计契约见 ../docs/fork-planner-design.md §1/§3/§4。
 * 缓存红线（../cache_exp/README.md 实测）：新分支 = 复制父尾部 + append 方向指令；
 * 广播/planner 只做尾部追加；分支间 LLM 调用天然串行（本调度单飞轮转）。
 * 依赖注入（cfg + now + cwdFor）→ 纯逻辑，离线单测（test/branch.test.ts）。
 *
 * fork 分两阶段（协议安全）：
 *   reserveFork    分发时调：跑全部护栏 + 预留 id（不建分支）；
 *   materializeFork 整轮工具结果回填完后调：拷父上下文（此时 tool 配对完整）+ append 指令。
 */
import type { ChatMessage } from "./types.js";

export interface BranchCfg {
  enabled: boolean;
  maxBranches: number; // 存活 Attempt 上限（含初始 Attempt 0）
  maxForks: number; // 每题 fork 总数硬上限
  forkCooldownSec: number;
  pruneSec: number; // corelog/flag 双停滞剪枝阈值
  plannerNudgeMax: number; // 每段每 Attempt 的 planner 提醒上限
  plannerNudgeCooldownSec: number;
}

/** attempt 生命周期。active=可调度；done/deadend/blocked=已主动收束。 */
export type BranchStatus = "active" | "done" | "deadend" | "blocked";

/** 一个 scope 下所有 attempt 的聚合状态（OR 语义）。 */
export type ScopeStatus = "open" | "satisfied" | "refuted" | "unresolved";

export interface BranchResult {
  status: Exclude<BranchStatus, "active">;
  summary: string;
  evidence: string[];
  artifacts: string[];
  next: string[];
  at: number;
}

/** attempt 分组：共享同一段前文/同一个子问题。 */
export interface ScopeState {
  id: number;
  parent: number | null;
  label: string;
  members: number[];
  status: ScopeStatus;
}

export interface BranchState {
  id: number; // 0 = 初始 Attempt
  parent: number | null;
  scopeId: number; // 所属 attempt 组（scope）
  label: string; // fork 方向（一句话）
  cwd: string;
  messages: ChatMessage[];
  /** 并发 turn 的注入邮箱：branch 正忙于 LLM/tool 时先排队，回合开始再并入 messages。 */
  inbox: ChatMessage[];
  /** 是否正有一个 turn 在跑（仅运行时，不持久化）。 */
  busy: boolean;
  /** fork 后的一次性调度优先级（运行时，不持久化）。 */
  priority: number;
  /** fork/分裂时刻的上下文前缀：spawn 相似尝试时复制它，而不是复制失败后的尾部。 */
  checkpointMessages: ChatMessage[];
  active: boolean; // parked=false 后跳过轮转
  pruned: boolean;
  status: BranchStatus; // active / done / deadend / blocked
  result?: BranchResult; // report 产出的结果卡
  createdAt: number;
  forkSeq: number; // fork 时刻 fork 源的 conv 记录序号（续跑重建的分界）
  plannerNudges: number;
  lastPlannerNudgeAt: number;
  lastCoreTs: number; // 最近一次 log / flag 事件（剪枝与停滞信号共用）
  recentCmds: string[]; // 最近 shell 命令（重复模式信号）
  recentHardFails: boolean[]; // 最近 shell 结果是否「连接/解析/命令缺失」类硬失败（信号④）
  wrongSubmits: number; // 连续 correct:false 次数（信号⑤ 幻觉提交）
  roundsSinceCore: number; // 连续无 log/flag 的轮数（轮级轻推信号）
  nudgesUsed: number; // 本段 log 轻推次数
}

/** 序列化给 worker 跨段持有的分支状态（计数器不跨段，恢复即重置）。 */
export interface BranchLite {
  id: number;
  parent: number | null;
  scopeId?: number;
  label: string;
  cwd: string;
  forkSeq: number;
  messages: ChatMessage[];
  checkpointMessages?: ChatMessage[];
  inbox?: ChatMessage[];
  status?: BranchStatus;
  active?: boolean;
  result?: BranchResult;
}

export type ForkReservation = { ok: true; id: number } | { ok: false; reason: string };

export interface ReportResult {
  ok: boolean;
  message: string;
  delivered: number[];
  scopeStatus: ScopeStatus;
  card: string;
}

export class BranchManager {
  cfg: BranchCfg;
  branches = new Map<number, BranchState>();
  scopes = new Map<number, ScopeState>();
  forkCount = 0;
  lastForkAt = 0;
  lastFlagTs: number; // 题级 flag 时钟（planner 核心：距上个新 flag 的分母；非分支级）
  private nextId = 1;
  private nextScopeId = 1;
  private rr = -1;
  private cmdHistory: Array<{ branch: number; cmd: string; at: number }> = [];
  private artifacts = new Map<string, Array<{ branch: number; path: string }>>();

  constructor(
    cfg: BranchCfg,
    trunkMessages: ChatMessage[],
    now: number,
    trunkCwd = "",
    trunkForkSeq = 0,
  ) {
    this.cfg = cfg;
    this.lastFlagTs = now;
    this.branches.set(0, {
      id: 0,
      parent: null,
      scopeId: 0,
      label: "attempt-0",
      cwd: trunkCwd,
      messages: trunkMessages,
      inbox: [],
      busy: false,
      priority: 0,
      checkpointMessages: trunkMessages.slice(),
      active: true,
      pruned: false,
      status: "active",
      createdAt: now,
      forkSeq: trunkForkSeq,
      plannerNudges: 0,
      lastPlannerNudgeAt: 0,
      lastCoreTs: now,
      recentCmds: [],
      recentHardFails: [],
      wrongSubmits: 0,
      roundsSinceCore: 0,
      nudgesUsed: 0,
    });
    this.scopes.set(0, {
      id: 0,
      parent: null,
      label: "task",
      members: [0],
      status: "open",
    });
  }

  /** 续跑恢复（fork 开启且有历史分支时由 solveTask 调用）。计数器按段重置。 */
  restore(list: BranchLite[], now: number): void {
    for (const st of list) {
      if (st.id === 0 || this.branches.has(st.id)) continue;
      const scopeId = st.scopeId ?? st.id;
      const status = st.status ?? "active";
      this.branches.set(st.id, {
        id: st.id,
        parent: st.parent,
        scopeId,
        label: st.label,
        cwd: st.cwd,
        messages: st.messages,
        inbox: st.inbox ?? [],
        busy: false,
        priority: 0,
        checkpointMessages: st.checkpointMessages ?? st.messages.slice(),
        active: status === "active", // 续段恢复：未结束的 Attempt 重新获得调度机会（旧语义）
        pruned: false,
        status,
        result: st.result,
        createdAt: now,
        forkSeq: st.forkSeq,
        plannerNudges: 0,
        lastPlannerNudgeAt: 0,
        lastCoreTs: now, // 恢复后给一个剪枝宽限期
        recentCmds: [],
        recentHardFails: [],
        wrongSubmits: 0,
        roundsSinceCore: 0,
        nudgesUsed: 0,
      });
      if (!this.scopes.has(scopeId)) {
        this.scopes.set(scopeId, {
          id: scopeId,
          parent: null,
          label: st.label,
          members: [st.id],
          status: "open",
        });
      } else {
        const sc = this.scopes.get(scopeId)!;
        if (!sc.members.includes(st.id)) sc.members.push(st.id);
      }
      this.nextId = Math.max(this.nextId, st.id + 1);
      this.nextScopeId = Math.max(this.nextScopeId, scopeId + 1);
      this.forkCount = Math.max(this.forkCount, st.id);
    }
    for (const id of this.scopes.keys()) this.refreshScope(id, now);
  }

  all(): BranchState[] {
    return [...this.branches.values()];
  }
  alive(): BranchState[] {
    return this.all().filter((b) => !b.pruned);
  }
  active(): BranchState[] {
    return this.alive().filter((b) => b.active);
  }
  get(id: number): BranchState | undefined {
    const b = this.branches.get(id);
    return b && !b.pruned ? b : undefined;
  }
  /** Attempt 名册（planner 输入用）：id → label。 */
  names(): Map<number, string> {
    const m = new Map<number, string>();
    for (const b of this.alive()) m.set(b.id, b.id === 0 ? "Attempt 0（初始）" : b.label);
    return m;
  }

  /** 轮转：优先返回 fork 后 boosted 的 Attempt；否则 RR（exclude 跳过 inflight）。 */
  pickExcept(exclude: Set<number> = new Set()): BranchState | null {
    const list = this.active().filter((b) => !exclude.has(b.id));
    if (!list.length) return null;
    const boosted = list.filter((b) => b.priority > 0);
    if (boosted.length) {
      const chosen = boosted.sort((a, b) => b.priority - a.priority || a.id - b.id)[0];
      chosen.priority--;
      return chosen;
    }
    this.rr = (this.rr + 1) % list.length; // a1 b1 a2 b2 …（交错缓存代价已实测为零）
    return list[this.rr];
  }

  /** 一次性调度优先级：fork 的父 Attempt 与新 Attempt 都加一票。 */
  boost(id: number, n = 1): void {
    const b = this.branches.get(id);
    if (b && !b.pruned) b.priority = Math.max(0, b.priority + n);
  }

  /** 并发 scheduler 从 ranked 取用后消费优先票。 */
  consumePriority(id: number): void {
    const b = this.branches.get(id);
    if (b && b.priority > 0) b.priority--;
  }

  /** 按优先级 + 轮转排序的 active 列表（不消费优先票；并发 fill 用）。
   *  同优先级按“rr 之后”的循环距离排序，避免刚跑完的 branch 立刻再次被选中，
   *  也避免高 id 新分支被 id 顺序永久饿死。 */
  ranked(exclude: Set<number> = new Set()): BranchState[] {
    const active = this.active();
    const list = active.filter((b) => !exclude.has(b.id));
    const boosted = list
      .filter((b) => b.priority > 0)
      .sort((a, b) => b.priority - a.priority || a.id - b.id);
    const normal = list.filter((b) => b.priority === 0);
    if (!normal.length) return boosted;
    const pos = new Map(active.map((b, i) => [b.id, i]));
    const last = this.rr >= 0 && this.rr < active.length ? this.rr : -1;
    normal.sort((a, b) => {
      const ia = pos.get(a.id)!;
      const ib = pos.get(b.id)!;
      const da = last < 0 ? ia : (ia - last - 1 + active.length) % active.length;
      const db = last < 0 ? ib : (ib - last - 1 + active.length) % active.length;
      return da - db || a.id - b.id;
    });
    return [...boosted, ...normal];
  }

  /** 并发 scheduler 选中一轮后推进轮转游标（与 pickExcept 的 rr 同源）。 */
  noteScheduled(id: number): void {
    const idx = this.active().findIndex((b) => b.id === id);
    if (idx >= 0) this.rr = idx;
  }

  /** 单飞轮转（兼容旧调用）。 */
  pick(): BranchState | null {
    return this.pickExcept();
  }

  park(id: number): void {
    const b = this.branches.get(id);
    if (b) b.active = false;
  }
  unpark(id: number): void {
    const b = this.branches.get(id);
    if (b && !b.pruned && b.status === "active") b.active = true;
  }

  /** 并发安全注入：branch 正忙时进邮箱，回合开始再并入 messages。 */
  enqueueMessage(id: number, content: string): boolean {
    const b = this.get(id);
    if (!b) return false;
    if (b.busy) b.inbox.push({ role: "user", content });
    else b.messages.push({ role: "user", content });
    return true;
  }

  /** 回合开始：把邮箱并入 messages（必须在 LLM 请求前调用）。 */
  drainInbox(id: number): void {
    const b = this.get(id);
    if (!b || !b.inbox.length) return;
    b.messages.push(...b.inbox);
    b.inbox.length = 0;
  }

  /** 新建一个 Attempt 组（scope）；fork 默认给新方向开新组。 */
  newScope(parent: number | null, label: string): number {
    const id = this.nextScopeId++;
    this.scopes.set(id, {
      id,
      parent,
      label: label.slice(0, 200),
      members: [],
      status: "open",
    });
    return id;
  }

  scope(id: number): ScopeState | undefined {
    return this.scopes.get(id);
  }

  /** 组内 Attempt 状态聚合：任一 done=satisfied；全 deadend=refuted；有 blocked=unresolved。 */
  refreshScope(id: number, now: number): ScopeStatus {
    const sc = this.scopes.get(id);
    if (!sc) return "open";
    const states = sc.members
      .map((mid) => this.branches.get(mid))
      .filter((b): b is BranchState => !!b)
      .map((b) => (b.pruned ? "deadend" : b.status));
    if (states.includes("done")) sc.status = "satisfied";
    else if (states.length > 0 && states.every((s) => s === "deadend")) sc.status = "refuted";
    else if (states.includes("blocked")) sc.status = "unresolved";
    else sc.status = "open";
    void now;
    return sc.status;
  }

  scopeStatus(id: number): ScopeStatus {
    return this.scopes.get(id)?.status ?? "open";
  }

  /** 某 scope 及其后代 scope 下的存活 Attempt（重建结果广播用）。 */
  attemptsInScope(scopeId: number): BranchState[] {
    const ids = new Set<number>([scopeId]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const sc of this.scopes.values()) {
        if (sc.parent !== null && ids.has(sc.parent) && !ids.has(sc.id)) {
          ids.add(sc.id);
          changed = true;
        }
      }
    }
    return this.alive().filter((b) => ids.has(b.scopeId));
  }

  /** fork 护栏（存活上限 / 总数上限 / 冷却）：通过则预留 id（不建分支）。 */
  reserveFork(parentId: number, now: number): ForkReservation {
    if (!this.cfg.enabled) return { ok: false, reason: "fork 未启用（AGENT_FORK=0）" };
    if (!this.get(parentId)) return { ok: false, reason: `fork 源 Attempt ${parentId} 不存在` };
    if (this.alive().length >= this.cfg.maxBranches) {
      return {
        ok: false,
        reason: `存活分支数已达上限 ${this.cfg.maxBranches}（用 log 告知 planner 协同即可，无需新 Attempt）`,
      };
    }
    if (this.forkCount >= this.cfg.maxForks) {
      return { ok: false, reason: `本题 fork 总数已达上限 ${this.cfg.maxForks}` };
    }
    if (this.lastForkAt > 0 && now - this.lastForkAt < this.cfg.forkCooldownSec) {
      const left = Math.ceil(this.cfg.forkCooldownSec - (now - this.lastForkAt));
      return { ok: false, reason: `fork 冷却中，${left}s 后可再试` };
    }
    const id = this.nextId++;
    this.forkCount++;
    this.lastForkAt = now;
    return { ok: true, id };
  }

  /** 整轮工具结果回填后调用：拷父上下文 + append 方向指令（协议配对完整）。
   *  默认给新方向开新 scope；传入已有 scopeId 则把新 attempt 加入该组（spawn 用）。 */
  materializeFork(
    id: number,
    parentId: number,
    direction: string,
    forkSeq: number,
    now: number,
    cwdFor: (branchId: number) => string,
    scopeId?: number,
  ): { branch: BranchState; directive: string } | null {
    const parent = this.branches.get(parentId);
    if (!parent || this.branches.has(id)) return null;
    const scope =
      scopeId !== undefined && this.scopes.has(scopeId)
        ? scopeId
        : this.newScope(parent.scopeId, direction);
    const directive =
      `【分支 ${id} · 方向】你由 fork 源 Attempt ${parentId} 的 checkpoint 分出，负责：${direction}\n` +
      `（实现语义：本 Attempt 是独立 session，与 fork 源共享分裂前的前缀。）\n` +
      `- 你的专属工作目录：${cwdFor(id)}（shell 默认 cwd；产物写这里）\n` +
      `- 读任意路径都允许（含 fork 源/其他 Attempt 目录，通报产物路径时写全路径）；` +
      `**写入只写自己的目录**；命名空间之外与容器级操作（重启服务/监听共享端口/改系统/iptables）` +
      `默认只读，确需改动先 log 告知 planner 协调。\n` +
      `- 关键发现/新方向随手 \`log\` 一句话；log 写黑板并送 planner，` +
      `由 planner 统筹转告其他 Attempt（没有 broadcast 工具）；` +
      `**停滞过久（无 log/flag）的 Attempt 会被系统回收**。\n` +
      `- 方向完成/走死/被外部阻塞时，必须调用 \`report(status, summary, evidence?, artifacts?, next?)\`：` +
      `done=方向完成，deadend=已证伪，blocked=外部阻塞。report 后本 Attempt 会被 park，` +
      `结果卡写黑板并通知 planner；不要无声停住，也不要跨界改做别的方向。`;
    const branch: BranchState = {
      id,
      parent: parentId,
      scopeId: scope,
      label: direction,
      cwd: cwdFor(id),
      messages: [...parent.messages, { role: "user", content: directive }],
      inbox: [],
      busy: false,
      priority: 0,
      checkpointMessages: parent.messages.slice(),
      active: true,
      pruned: false,
      status: "active",
      createdAt: now,
      forkSeq,
      plannerNudges: 0,
      lastPlannerNudgeAt: 0,
      lastCoreTs: now,
      recentCmds: [],
      recentHardFails: [],
      wrongSubmits: 0,
      roundsSinceCore: 0,
      nudgesUsed: 0,
    };
    this.branches.set(id, branch);
    this.nextId = Math.max(this.nextId, id + 1);
    // fork 后父与新 Attempt 都优先一轮，避免新分支排队等待。
    this.boost(parentId, 1);
    this.boost(id, 2);
    const sc = this.scopes.get(scope)!;
    if (!sc.members.includes(id)) sc.members.push(id);
    if (sc.label === "task") sc.label = direction.slice(0, 200);
    this.refreshScope(scope, now);
    return { branch, directive };
  }

  /** planner 预检：是否还能从该 source 开兄弟（不会真的分配 id）。 */
  canSpawnFrom(sourceId: number): boolean {
    if (!this.cfg.enabled) return false;
    const source = this.branches.get(sourceId);
    if (!source) return false;
    if (this.forkCount >= this.cfg.maxForks) return false;
    const sourceFreesSlot = sourceId !== 0 && !source.pruned && source.status !== "active";
    const alive = this.alive().length - (sourceFreesSlot ? 1 : 0);
    return alive < this.cfg.maxBranches;
  }

  /** 从某个 Attempt 的 checkpoint（分裂前前缀）开一条兄弟尝试；用于 planner 回溯。
   *  source 的失败尾部不会进入新 Attempt；只复制 checkpointMessages + 新方向指令。 */
  spawnFromCheckpoint(
    sourceId: number,
    direction: string,
    now: number,
    cwdFor: (branchId: number) => string,
    scopeId?: number,
  ): { branch: BranchState; directive: string } | null {
    if (!this.cfg.enabled) return null;
    const source = this.branches.get(sourceId);
    if (!source) return null;
    if (this.forkCount >= this.cfg.maxForks) return null;
    // 结束的 source 可以让位；仍在跑的 source 不主动挤掉。
    if (this.alive().length >= this.cfg.maxBranches) {
      if (sourceId !== 0 && !source.pruned && source.status !== "active") this.prune(sourceId);
      if (this.alive().length >= this.cfg.maxBranches) return null;
    }
    let id = this.nextId++;
    while (this.branches.has(id)) id = this.nextId++;
    this.forkCount++;
    this.lastForkAt = now;
    const scope = scopeId !== undefined && this.scopes.has(scopeId) ? scopeId : source.scopeId;
    const parent = source.parent ?? 0;
    const forkSeq = source.forkSeq;
    const base = source.checkpointMessages ?? source.messages;
    const negative = source.result
      ? `\n- 已确认的负知识（来源 Attempt ${sourceId}）：${source.result.summary.slice(0, 600)}` +
        `${source.result.next.length ? `；建议下一步：${source.result.next.join("；").slice(0, 400)}` : ""}`
      : "";
    const directive =
      `【分支 ${id} · 方向】planner 从 Attempt ${sourceId} 的 checkpoint 分出，负责：${direction}\n` +
      `- 你继承的是分裂前的前缀，不包含 Attempt ${sourceId} 后续的失败轨迹。\n` +
      negative +
      `\n- 你的专属工作目录：${cwdFor(id)}（shell 默认 cwd；产物写这里）\n` +
      `- 读任意路径都允许；**写入只写自己的目录**；容器级/共享靶机操作默认只读，` +
      `确需改动先 log 告知 planner 协调。\n` +
      `- 关键发现随手 \`log\`；方向完成/走死/阻塞必须 \`report\`，不要无声停住，也不要跨界。`;
    const branch: BranchState = {
      id,
      parent,
      scopeId: scope,
      label: direction,
      cwd: cwdFor(id),
      messages: [...base, { role: "user", content: directive }],
      inbox: [],
      busy: false,
      priority: 0,
      checkpointMessages: base.slice(),
      active: true,
      pruned: false,
      status: "active",
      createdAt: now,
      forkSeq,
      plannerNudges: 0,
      lastPlannerNudgeAt: 0,
      lastCoreTs: now,
      recentCmds: [],
      recentHardFails: [],
      wrongSubmits: 0,
      roundsSinceCore: 0,
      nudgesUsed: 0,
    };
    this.branches.set(id, branch);
    const sc = this.scopes.get(scope)!;
    if (!sc.members.includes(id)) sc.members.push(id);
    this.refreshScope(scope, now);
    return { branch, directive };
  }

  /** planner 回收任意 Attempt（含初始 Attempt 0；timer prune 仍保留 0 作为兜底锚点）。 */
  retire(id: number): BranchState | null {
    const b = this.branches.get(id);
    if (!b || b.pruned) return null;
    b.pruned = true;
    b.active = false;
    this.refreshScope(b.scopeId, Date.now() / 1000);
    return b;
  }

  /** 结果卡文本（report 写黑板用）。 */
  private resultCardText(b: BranchState): string {
    const r = b.result;
    if (!r) return "";
    const lines = [`[Attempt ${b.id} 已结束 · ${r.status}] 方向：${b.label}`, `摘要：${r.summary}`];
    if (r.evidence.length) lines.push(`证据：${r.evidence.join("；")}`);
    if (r.artifacts.length) lines.push(`产物：${r.artifacts.join("；")}`);
    if (r.next.length) lines.push(`建议下一步：${r.next.join("；")}`);
    lines.push("（上下文与产物仍保留；需要细节可由 planner 按黑板读取）");
    return lines.join("\n");
  }

  /** 结束一个 attempt：落结果卡、park、更新 scope；卡片写黑板并通知 planner。 */
  report(
    id: number,
    status: Exclude<BranchStatus, "active">,
    summary: string,
    evidence: string[],
    artifacts: string[],
    next: string[],
    now: number,
  ): ReportResult {
    const b = this.get(id);
    if (!b)
      return {
        ok: false,
        message: "error: attempt 不存在或已回收",
        delivered: [],
        scopeStatus: "open",
        card: "",
      };
    if (b.status !== "active") {
      return {
        ok: false,
        message: `attempt ${id} 已经以 ${b.status} 结束，不能重复 report`,
        delivered: [],
        scopeStatus: this.scopeStatus(b.scopeId),
        card: b.result ? this.resultCardText(b) : "",
      };
    }
    b.status = status;
    b.active = false;
    b.result = {
      status,
      summary: summary.slice(0, 1200),
      evidence: evidence.slice(0, 20).map((s) => s.slice(0, 500)),
      artifacts: artifacts.slice(0, 20).map((s) => s.slice(0, 500)),
      next: next.slice(0, 10).map((s) => s.slice(0, 500)),
      at: now,
    };
    b.lastCoreTs = now;
    const scopeStatus = this.refreshScope(b.scopeId, now);
    const card = this.resultCardText(b);

    // 结果卡只写黑板 + planner 事件；不自动回“父 Attempt”。
    const targets: number[] = [];
    const delivered: number[] = [];
    for (const tid of new Set(targets)) {
      const t = this.get(tid);
      if (!t || tid === id) continue;
      this.enqueueMessage(tid, card);
      // 收到结果卡 = 有新信息；只唤醒仍可工作的 attempt，不复活已结束的。
      if (t.status === "active") t.active = true;
      delivered.push(tid);
    }
    return {
      ok: true,
      message:
        `reported ${status} for attempt ${id}; attempt parked; result card posted to ` +
        `blackboard; planner notified; scope ${b.scopeId}=${scopeStatus}`,
      delivered,
      scopeStatus,
      card,
    };
  }

  /** 剪枝（solveTask 按 cfg.pruneSec 判定后调用）：返回被剪 Attempt。初始 Attempt 0 永不剪。 */
  prune(id: number): BranchState | null {
    if (id === 0) return null;
    const b = this.branches.get(id);
    if (!b || b.pruned) return null;
    b.pruned = true;
    b.active = false;
    this.refreshScope(b.scopeId, Date.now() / 1000);
    return b;
  }

  /** 新 flag 到达 → 刷新题级 flag 时钟（planner主信号的分母）。 */
  recordFlag(now: number): void {
    this.lastFlagTs = now;
  }

  /** log / flag 事件到达 → 刷新停滞时钟 + 轮级轻推计数。 */
  recordCore(id: number, now: number): void {
    const b = this.branches.get(id);
    if (b && !b.pruned) {
      b.lastCoreTs = now;
      b.roundsSinceCore = 0;
    }
  }

  /** 记录 shell 结果的硬失败（连接/解析/命令缺失=幻觉目标空转信号④，滚动12次）。 */
  recordShellOutcome(id: number, hardFail: boolean): void {
    const b = this.branches.get(id);
    if (!b) return;
    b.recentHardFails.push(hardFail);
    if (b.recentHardFails.length > 12) b.recentHardFails.shift();
  }

  /** 幻觉提交计数（信号⑤）：correct:false 连增、correct:true 清零。 */
  recordSubmitResult(id: number, correct: boolean): void {
    const b = this.branches.get(id);
    if (!b) return;
    if (correct) b.wrongSubmits = 0;
    else b.wrongSubmits++;
  }

  /** 记录 shell 命令（重复模式信号 + 跨 Attempt 去重）。 */
  pushCmd(id: number, cmd: string): void {
    const b = this.branches.get(id);
    if (!b) return;
    const norm = cmd.replace(/\s+/g, " ").trim().slice(0, 300);
    b.recentCmds.push(norm);
    if (b.recentCmds.length > 12) b.recentCmds.shift();
    if (norm) {
      this.cmdHistory.push({ branch: id, cmd: norm.slice(0, 120), at: Date.now() });
      if (this.cmdHistory.length > 200) this.cmdHistory.shift();
    }
  }

  /** 记录产物指纹；返回其他 Attempt 已有的同 sha256 产物（用于跨 Attempt 去重提示）。 */
  recordArtifacts(
    branchId: number,
    list: Array<{ path: string; hash: string }>,
  ): Array<{ branch: number; path: string; hash: string }> {
    const dup: Array<{ branch: number; path: string; hash: string }> = [];
    for (const a of list) {
      if (!a.hash) continue;
      const hits = this.artifacts.get(a.hash) ?? [];
      for (const h of hits) {
        if (h.branch !== branchId) dup.push({ branch: h.branch, path: h.path, hash: a.hash });
      }
      hits.push({ branch: branchId, path: a.path });
      this.artifacts.set(a.hash, hits.slice(-20)); // 同一文件多 Attempt 复用，保留少量审计
    }
    return dup;
  }

  /** 续段恢复产物索引（读旧的 log.jsonl artifact 记录）；只用于去重提示。 */
  restoreArtifacts(list: Array<{ branch: number; hash: string; path: string }>): void {
    for (const a of list) {
      if (!a.hash) continue;
      const hits = this.artifacts.get(a.hash) ?? [];
      if (!hits.some((h) => h.branch === a.branch && h.path === a.path)) {
        hits.push({ branch: a.branch, path: a.path });
      }
      this.artifacts.set(a.hash, hits.slice(-20));
    }
  }

  /** 跨 Attempt 命令去重：返回最近执行过同一命令的其他 Attempt（无 → null）。 */
  commandSeenElsewhere(id: number, cmd: string): number | null {
    const key = cmd.replace(/\s+/g, " ").trim().slice(0, 120);
    if (!key) return null;
    for (let i = this.cmdHistory.length - 1; i >= 0; i--) {
      const h = this.cmdHistory[i];
      if (h.branch !== id && h.cmd === key) return h.branch;
    }
    return null;
  }

  /** planner 提醒限频检查（不消耗额度）。 */
  plannerNudgeAvailable(id: number, now: number): boolean {
    const b = this.branches.get(id);
    if (!b || b.pruned || b.status !== "active") return false;
    if (b.plannerNudges >= this.cfg.plannerNudgeMax) return false;
    if (b.lastPlannerNudgeAt > 0 && now - b.lastPlannerNudgeAt < this.cfg.plannerNudgeCooldownSec)
      return false;
    return true;
  }

  /** planner 提醒额度是否已用尽（不考虑冷却）。 */
  plannerNudgeExhausted(id: number): boolean {
    const b = this.branches.get(id);
    return !!b && b.plannerNudges >= this.cfg.plannerNudgeMax;
  }

  /** planner 提醒注入成功后消耗额度（每 Attempt 每段上限 + 冷却）。 */
  notePlannerNudge(id: number, now: number): void {
    const b = this.branches.get(id);
    if (!b) return;
    b.plannerNudges++;
    b.lastPlannerNudgeAt = now;
  }
}
