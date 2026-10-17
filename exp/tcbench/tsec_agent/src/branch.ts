/** fork 多分支执行面：分支状态、护栏（上限/冷却/剪枝）、轮转调度、广播。
 *
 * 设计契约见 ../docs/fork-bystander-design.md §1/§3/§4。
 * 缓存红线（../cache_exp/README.md 实测）：新分支 = 复制父尾部 + append 方向指令；
 * 广播/旁观者只做尾部追加；分支间 LLM 调用天然串行（本调度单飞轮转）。
 * 依赖注入（cfg + now + cwdFor）→ 纯逻辑，离线单测（test/branch.test.ts）。
 *
 * fork 分两阶段（协议安全）：
 *   reserveFork    分发时调：跑全部护栏 + 预留 id（不建分支）；
 *   materializeFork 整轮工具结果回填完后调：拷父上下文（此时 tool 配对完整）+ append 指令。
 */
import type { ChatMessage } from "./types.js";

export interface BranchCfg {
  enabled: boolean;
  maxBranches: number; // 存活分支上限（含 trunk）
  maxForks: number; // 每题 fork 总数硬上限
  forkCooldownSec: number;
  pruneSec: number; // corelog/flag 双停滞剪枝阈值
  broadcastMax: number; // 每段每分支广播上限
  bystanderMax: number; // 每段每分支旁观者上限
  bystanderCooldownSec: number;
}

export interface BranchState {
  id: number; // 0 = trunk
  parent: number | null;
  label: string; // fork 方向（一句话）
  cwd: string;
  messages: ChatMessage[];
  active: boolean; // parked=false 后跳过轮转
  pruned: boolean;
  createdAt: number;
  forkSeq: number; // fork 时刻父分支的 conv 记录序号（续跑重建的分界）
  broadcastsUsed: number;
  bystanderUsed: number;
  lastBystanderAt: number;
  lastCoreTs: number; // 最近一次 core_log / flag 事件（剪枝与停滞信号共用）
  recentCmds: string[]; // 最近 shell 命令（重复模式信号）
  recentHardFails: boolean[]; // 最近 shell 结果是否「连接/解析/命令缺失」类硬失败（信号④）
  wrongSubmits: number; // 连续 correct:false 次数（信号⑤ 幻觉提交）
  roundsSinceCore: number; // 连续无 core_log/flag 的轮数（轮级轻推信号）
  /** 多 flag 压缩素材（flag_summary 轮累积）：压缩重写 = head + 这条链 + 继续指令 */
  flagSummaries: Array<{ idx: number; text: string }>;
  nudgesUsed: number; // 本段 core_log 轻推次数
}

/** 序列化给 worker 跨段持有的分支状态（计数器不跨段，恢复即重置）。 */
export interface BranchLite {
  id: number;
  parent: number | null;
  label: string;
  cwd: string;
  forkSeq: number;
  messages: ChatMessage[];
}

export type ForkReservation = { ok: true; id: number } | { ok: false; reason: string };

export interface BroadcastResult {
  ok: boolean;
  message: string;
  delivered: number[];
}

/** 归一化（重复广播去重）：小写 + 折叠空白 + 去标点，取前 400 字。 */
export function normalizeNote(s: string): string {
  return s
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[^\p{L}\p{N}]+/gu, "")
    .slice(0, 400);
}

export class BranchManager {
  cfg: BranchCfg;
  branches = new Map<number, BranchState>();
  forkCount = 0;
  lastForkAt = 0;
  lastFlagTs: number; // 题级 flag 时钟（旁观者核心：距上个新 flag 的分母；非分支级）
  private nextId = 1;
  private rr = -1;
  private recentBroadcasts: string[] = [];

  constructor(cfg: BranchCfg, trunkMessages: ChatMessage[], now: number, trunkCwd = "") {
    this.cfg = cfg;
    this.lastFlagTs = now;
    this.branches.set(0, {
      id: 0,
      parent: null,
      label: "trunk",
      cwd: trunkCwd,
      messages: trunkMessages,
      active: true,
      pruned: false,
      createdAt: now,
      forkSeq: 0,
      broadcastsUsed: 0,
      bystanderUsed: 0,
      lastBystanderAt: 0,
      lastCoreTs: now,
      recentCmds: [],
      recentHardFails: [],
      wrongSubmits: 0,
      roundsSinceCore: 0,
      flagSummaries: [],
      nudgesUsed: 0,
    });
  }

  /** 续跑恢复（fork 开启且有历史分支时由 solveTask 调用）。计数器按段重置。 */
  restore(list: BranchLite[], now: number): void {
    for (const st of list) {
      if (st.id === 0 || this.branches.has(st.id)) continue;
      this.branches.set(st.id, {
        id: st.id,
        parent: st.parent,
        label: st.label,
        cwd: st.cwd,
        messages: st.messages,
        active: true,
        pruned: false,
        createdAt: now,
        forkSeq: st.forkSeq,
        broadcastsUsed: 0,
        bystanderUsed: 0,
        lastBystanderAt: 0,
        lastCoreTs: now, // 恢复后给一个剪枝宽限期
        recentCmds: [],
        recentHardFails: [],
        wrongSubmits: 0,
        roundsSinceCore: 0,
        flagSummaries: [],
        nudgesUsed: 0,
      });
      this.nextId = Math.max(this.nextId, st.id + 1);
      this.forkCount = Math.max(this.forkCount, st.id);
    }
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
  /** 分支名册（旁观者输入用）：id → label。 */
  names(): Map<number, string> {
    const m = new Map<number, string>();
    for (const b of this.alive()) m.set(b.id, b.id === 0 ? "主分支" : b.label);
    return m;
  }

  /** 单飞轮转：返回下一个可调度分支（无 → null = 全部 parked，本段收束）。 */
  pick(): BranchState | null {
    const list = this.active();
    if (!list.length) return null;
    this.rr = (this.rr + 1) % list.length; // a1 b1 a2 b2 …（交错缓存代价已实测为零）
    return list[this.rr];
  }

  park(id: number): void {
    const b = this.branches.get(id);
    if (b) b.active = false;
  }
  unpark(id: number): void {
    const b = this.branches.get(id);
    if (b && !b.pruned) b.active = true;
  }

  /** fork 护栏（存活上限 / 总数上限 / 冷却）：通过则预留 id（不建分支）。 */
  reserveFork(parentId: number, now: number): ForkReservation {
    if (!this.cfg.enabled) return { ok: false, reason: "fork 未启用（AGENT_FORK=0）" };
    if (!this.get(parentId)) return { ok: false, reason: `父分支 ${parentId} 不存在` };
    if (this.alive().length >= this.cfg.maxBranches) {
      return {
        ok: false,
        reason: `存活分支数已达上限 ${this.cfg.maxBranches}（用 broadcast 协同即可，无需新分支）`,
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

  /** 整轮工具结果回填后调用：拷父上下文 + append 方向指令（协议配对完整）。 */
  materializeFork(
    id: number,
    parentId: number,
    direction: string,
    forkSeq: number,
    now: number,
    cwdFor: (branchId: number) => string,
  ): { branch: BranchState; directive: string } | null {
    const parent = this.branches.get(parentId);
    if (!parent || this.branches.has(id)) return null;
    const directive =
      `【分支 ${id} · 方向】你由分支 ${parentId} 分出，负责：${direction}\n` +
      `- 你的专属工作目录：${cwdFor(id)}（shell 默认 cwd；产物写这里）\n` +
      `- 读任意路径都允许（含父/其他分支目录，通报产物路径时写全路径）；` +
      `**写入只写自己的目录**；命名空间之外与容器级操作（重启服务/监听共享端口/改系统/iptables）` +
      `默认只读，确需改动先用 broadcast 协调。\n` +
      `- 关键发现/新方向随手 \`core_log\` 一句话；\`broadcast\` **只上行给主分支（分支 0）**——` +
      `子分支之间不直连，横向协同由主分支统筹转发；` +
      `**停滞过久（无 core_log/flag）的分支会被系统回收**，发现摘要会汇入主分支。\n` +
      `- 你没有收尾权：不要输出无工具总结（那会暂停你的分支）；持续推进，进度用 core_log 记录。`;
    const branch: BranchState = {
      id,
      parent: parentId,
      label: direction,
      cwd: cwdFor(id),
      messages: [...parent.messages, { role: "user", content: directive }],
      active: true,
      pruned: false,
      createdAt: now,
      forkSeq,
      broadcastsUsed: 0,
      bystanderUsed: 0,
      lastBystanderAt: 0,
      lastCoreTs: now,
      recentCmds: [],
      recentHardFails: [],
      wrongSubmits: 0,
      roundsSinceCore: 0,
      flagSummaries: [],
      nudgesUsed: 0,
    };
    this.branches.set(id, branch);
    return { branch, directive };
  }

  /** 广播：尾部追加到目标分支（缓存红线），激活 parked 分支；限频 + 近似重复去重。 */
  broadcast(fromId: number, note: string, to: unknown, now: number): BroadcastResult {
    void now;
    const from = this.get(fromId);
    if (!from) return { ok: false, message: "error: 发起分支不存在", delivered: [] };
    if (from.broadcastsUsed >= this.cfg.broadcastMax) {
      return {
        ok: false,
        message: `广播已达本段上限 ${this.cfg.broadcastMax} 次，把发现写进 core_log 即可`,
        delivered: [],
      };
    }
    const norm = normalizeNote(note);
    if (norm && this.recentBroadcasts.includes(norm)) {
      return { ok: false, message: "重复广播已抑制（与近期广播重复），未计入限额", delivered: [] };
    }
    let targets: number[];
    if (to === undefined || to === null || to === "all") {
      targets = this.alive()
        .filter((b) => b.id !== fromId)
        .map((b) => b.id);
    } else if (to === "trunk") {
      targets = [0];
    } else if (Array.isArray(to)) {
      targets = to.map((x) => Number(x)).filter((n) => Number.isFinite(n));
    } else {
      targets = [Number(to)];
    }
    // 拓扑夹取（思想：界由 fork 划定，子分支只**上行主分支**）——横向目标（活着的兄弟）
    // 改投 0、由主分支统筹转发，子分支之间不直连；无效 id 照旧过滤（拒绝语义不变）；
    // 主分支（0）→ 任意的下行广播不改（全连保留）。
    let clamped = false;
    if (fromId !== 0) {
      const up = new Set<number>();
      for (const t of new Set(targets)) {
        if (t === fromId) continue; // 自投照旧无效
        if (t === 0) up.add(0);
        else if (this.get(t)) {
          up.add(0);
          clamped = true;
        }
      }
      targets = [...up];
    }
    const content = `[分支 ${fromId} 广播] ${note}`;
    const delivered: number[] = [];
    for (const tid of new Set(targets)) {
      const t = this.get(tid);
      if (!t || tid === fromId) continue;
      t.messages.push({ role: "user", content });
      t.active = true; // 收到广播 = 有新信息，激活 parked 分支
      delivered.push(tid);
    }
    if (!delivered.length) {
      return { ok: false, message: "没有有效广播目标（分支已回收或只有自己）", delivered };
    }
    from.broadcastsUsed++;
    if (norm) {
      this.recentBroadcasts.push(norm);
      if (this.recentBroadcasts.length > 20) this.recentBroadcasts.shift();
    }
    return {
      ok: true,
      message: clamped
        ? "已广播给分支 0（子分支仅可上行主分支；横向协同由主分支转发）"
        : `已广播给分支 ${delivered.join(", ")}（${delivered.length} 个）`,
      delivered,
    };
  }

  /** 剪枝（solveTask 按 cfg.pruneSec 判定后调用）：返回被剪分支。trunk 永不剪。 */
  prune(id: number): BranchState | null {
    if (id === 0) return null;
    const b = this.branches.get(id);
    if (!b || b.pruned) return null;
    b.pruned = true;
    b.active = false;
    return b;
  }

  /** 新 flag 到达 → 刷新题级 flag 时钟（旁观者主信号的分母）。 */
  recordFlag(now: number): void {
    this.lastFlagTs = now;
  }

  /** core_log / flag 事件到达 → 刷新停滞时钟 + 轮级轻推计数。 */
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

  /** 记录 shell 命令（重复模式信号）。 */
  pushCmd(id: number, cmd: string): void {
    const b = this.branches.get(id);
    if (!b) return;
    b.recentCmds.push(cmd.replace(/\s+/g, " ").trim().slice(0, 300));
    if (b.recentCmds.length > 12) b.recentCmds.shift();
  }

  /** 旁观者限频检查（不消耗额度）。 */
  bystanderAvailable(id: number, now: number): boolean {
    const b = this.branches.get(id);
    if (!b || b.pruned) return false;
    if (b.bystanderUsed >= this.cfg.bystanderMax) return false;
    if (b.lastBystanderAt > 0 && now - b.lastBystanderAt < this.cfg.bystanderCooldownSec)
      return false;
    return true;
  }

  /** 旁观者调用成功后消耗额度（每分支每段上限 + 冷却）。 */
  noteBystander(id: number, now: number): void {
    const b = this.branches.get(id);
    if (!b) return;
    b.bystanderUsed++;
    b.lastBystanderAt = now;
  }
}
