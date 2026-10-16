/** 状态机 + 计分 + 持久化 + 重启对账。
 *
 * 语义来源：CHALLENGES_API.md §5（错误码/状态/响应字段）。业务错误一律抛 ArenaError，
 * server.ts 只做 HTTP 映射；docker 副作用全部经 Driver 接口（测试可替换 FakeDriver）。
 *
 * 关键语义决策（官方未定义处，与根仓库 mock/ 保持一致或按最省事且不破坏 agent 的方式）：
 * - start 对 pending/available 幂等（返回当前视图）；close 对 stopped 幂等（closed:true）；
 * - active 计数 = pending + available + stop_pending（start 先同步置 pending 再跑 docker，
 *   并发闸门不会被 docker 启动耗时穿透）；
 * - 就绪探测失败/超时 → 回收容器回 stopped（agent 侧表现为启动失败，可重试）；
 * - 任务时限到点 → 全接口 409 invalid_state，message 含 "finished"（bench.ts 判终态即停）。
 *
 * 持久化（state.json，原子写）：flag 进度 / hint 状态 / 任务起始与截止时间。
 * 容器状态不持久化——重启后靠 driver.listManaged() 对账恢复。
 */
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import type { ArenaConfig } from "./config.js";
import type { ChallengeSpec, ChallengeView, ContainerStatus, Driver, DriverHandle, SubmitResult } from "./types.js";

/** 业务错误：code = 官方错误码，http = 状态码 */
export class ArenaError extends Error {
  code: string;
  http: number;
  constructor(http: number, code: string, message: string) {
    super(message);
    this.name = "ArenaError";
    this.http = http;
    this.code = code;
  }
}

interface Runtime {
  spec: ChallengeSpec;
  status: ContainerStatus;
  handle: DriverHandle | null;
  correctFlags: Set<number>;
  hintViewed: boolean;
  cumulative: number;
}

interface PersistShape {
  version: 1;
  task_started_at: number;
  deadline_at: number | null;
  challenges: Record<string, { correct_flags: number[]; hint_viewed: boolean; cumulative: number }>;
}

export interface StateOptions {
  /** 测试用：固定构造时刻（毫秒） */
  now?: number;
}

export class ArenaState {
  private driver: Driver;
  private cfg: ArenaConfig;
  private rt = new Map<string, Runtime>();
  private usedPorts = new Set<number>();
  readonly taskStartedAt: number;
  readonly deadlineAt: number | null;

  constructor(packs: Map<string, ChallengeSpec>, driver: Driver, cfg: ArenaConfig, opts: StateOptions = {}) {
    this.driver = driver;
    this.cfg = cfg;
    const saved = this.load();
    this.taskStartedAt = saved?.task_started_at ?? opts.now ?? Date.now();
    if (cfg.totalLimitS > 0) {
      this.deadlineAt = saved?.deadline_at ?? (opts.now ?? Date.now()) + cfg.totalLimitS * 1000;
    } else {
      this.deadlineAt = null;
    }
    for (const spec of packs.values()) {
      const progress = saved?.challenges?.[spec.unique_code];
      this.rt.set(spec.unique_code, {
        spec,
        status: "stopped",
        handle: null,
        correctFlags: new Set(progress?.correct_flags ?? []),
        hintViewed: progress?.hint_viewed ?? false,
        cumulative: progress?.cumulative ?? 0,
      });
    }
  }

  /** 任务终态守卫（server 每个请求先过这里） */
  ensureLive(): void {
    if (this.deadlineAt !== null && Date.now() >= this.deadlineAt) {
      throw new ArenaError(409, "invalid_state", "benchmark task finished: total time limit exceeded");
    }
  }

  /** 重启对账：接管仍在跑的托管容器（重探就绪），无主容器清掉。 */
  async reconcile(): Promise<void> {
    let managed: Map<string, number[]>;
    try {
      managed = await this.driver.listManaged();
    } catch (e) {
      console.log(`[arena] 对账失败（docker 不可用？）：${(e as Error).message}`);
      return;
    }
    for (const [code, ports] of managed) {
      const rt = this.rt.get(code);
      if (!rt || rt.handle || !ports.length) {
        console.log(`[arena] 清理无主容器 ${code}`);
        await this.driver.rm({ code, name: `tcbench-target-${code}`, host_ports: ports }).catch(() => undefined);
        continue;
      }
      // 正常路径：state 里 status 均为 stopped 且 handle 为 null（内存态不跨进程）——直接接管
      rt.status = "pending";
      rt.handle = { code, name: `tcbench-target-${code}`, host_ports: ports };
      for (const p of ports) this.usedPorts.add(p);
      this.spawnProbe(rt);
      console.log(`[arena] 接管容器 ${code} @ ${ports.join(",")}`);
    }
  }

  list(): ChallengeView[] {
    this.ensureLive();
    const views = [...this.rt.values()]
      .map((rt) => this.viewOf(rt))
      .sort((a, b) => (a.unique_code < b.unique_code ? -1 : 1));
    return views;
  }

  async start(code: string): Promise<{ unique_code: string; container_addr: string[] }> {
    this.ensureLive();
    const rt = this.rtOf(code);
    // 幂等：pending/available 直接返回当前视图（官方未定义，选幂等——agent 重试安全）
    if (rt.status === "pending" || rt.status === "available") return this.startView(rt);
    if (this.activeCount() >= this.cfg.maxActive) {
      throw new ArenaError(409, "invalid_state", "active container limit reached, close a challenge first");
    }
    const hostPorts = this.allocPorts(rt.spec.container_ports.length);
    // 先同步置 pending：并发闸门不被 docker 启动耗时穿透
    rt.status = "pending";
    try {
      rt.handle = await this.driver.run(rt.spec, hostPorts);
    } catch (e) {
      rt.status = "stopped";
      rt.handle = null;
      this.freePorts(hostPorts);
      throw new ArenaError(503, "resource_unavailable", `target container failed to start: ${(e as Error).message}`);
    }
    this.spawnProbe(rt);
    return this.startView(rt);
  }

  async close(code: string): Promise<{ unique_code: string; closed: true }> {
    this.ensureLive();
    const rt = this.rtOf(code);
    if (rt.status === "stopped") return { unique_code: code, closed: true }; // 幂等
    rt.status = "stop_pending";
    const h = rt.handle;
    rt.handle = null;
    if (h) {
      await this.driver.rm(h).catch((e) => {
        console.log(`[arena] rm ${h.name} 失败（按已关闭处理）：${(e as Error).message}`);
      });
      this.freePorts(h.host_ports);
    }
    rt.status = "stopped";
    return { unique_code: code, closed: true };
  }

  hint(code: string): { unique_code: string; hint: string | null } {
    this.ensureLive();
    const rt = this.rtOf(code);
    if (this.isCompleted(rt)) {
      throw new ArenaError(409, "invalid_state", "challenge already completed");
    }
    if (!rt.hintViewed) {
      rt.hintViewed = true;
      this.save();
    }
    return { unique_code: code, hint: rt.spec.hint };
  }

  submit(code: string, flag: string): SubmitResult {
    this.ensureLive();
    const rt = this.rtOf(code);
    const total = rt.spec.flags.length;
    const idx = rt.spec.flags.indexOf(flag);
    if (idx >= 0) {
      if (rt.correctFlags.has(idx)) throw new ArenaError(409, "duplicate", "flag already submitted");
      rt.correctFlags.add(idx);
      const per = perFlagScore(rt.spec, idx);
      const awarded = rt.hintViewed ? Math.round(per * (1 - rt.spec.hint_cost_ratio)) : per;
      rt.cumulative += awarded;
      this.save();
      return {
        correct: true,
        awarded,
        cumulative_score: rt.cumulative,
        correct_flag_count: rt.correctFlags.size,
        total_flag_count: total,
        matched_flag_index: idx,
      };
    }
    return {
      correct: false,
      awarded: 0,
      cumulative_score: rt.cumulative,
      correct_flag_count: rt.correctFlags.size,
      total_flag_count: total,
      matched_flag_index: null,
    };
  }

  // ---- 内部 ------------------------------------------------------------------

  private rtOf(code: string): Runtime {
    const rt = this.rt.get(code);
    if (!rt) throw new ArenaError(404, "challenge_not_found", `unknown unique_code ${JSON.stringify(code)}`);
    return rt;
  }

  private activeCount(): number {
    let n = 0;
    for (const rt of this.rt.values()) if (rt.status !== "stopped") n++;
    return n;
  }

  private isCompleted(rt: Runtime): boolean {
    return rt.spec.flags.length > 0 && rt.correctFlags.size === rt.spec.flags.length;
  }

  private viewOf(rt: Runtime): ChallengeView {
    return {
      unique_code: rt.spec.unique_code,
      description: rt.spec.description,
      difficulty: rt.spec.difficulty,
      level: rt.spec.level,
      total_score: rt.spec.total_score,
      flag_count: rt.spec.flags.length,
      correct_flag_count: rt.correctFlags.size,
      is_completed: this.isCompleted(rt),
      container_status: rt.status,
      // 官方硬语义：container_addr 仅 available 时非空
      container_addr:
        rt.status === "available" && rt.handle
          ? rt.handle.host_ports.map((p) => `${this.cfg.advertiseHost}:${p}`)
          : [],
    };
  }

  /** start 响应（官方 §5.2）：容器已起，地址即时给出（哪怕还在 pending） */
  private startView(rt: Runtime): { unique_code: string; container_addr: string[] } {
    return {
      unique_code: rt.spec.unique_code,
      container_addr: rt.handle ? rt.handle.host_ports.map((p) => `${this.cfg.advertiseHost}:${p}`) : [],
    };
  }

  private spawnProbe(rt: Runtime): void {
    const h = rt.handle;
    if (!h) return;
    void this.driver.waitReady(rt.spec, h).then(
      () => {
        if (rt.status === "pending" && rt.handle === h) {
          rt.status = "available";
          console.log(`[arena] ${rt.spec.unique_code} available @ ${h.host_ports.join(",")}`);
        }
      },
      async (e: Error) => {
        if (rt.handle !== h) return; // 已被 close/重 start，不重复回收
        console.log(`[arena] ${rt.spec.unique_code} 就绪探测失败，回收容器：${e.message}`);
        await this.driver.rm(h).catch(() => undefined);
        this.freePorts(h.host_ports);
        rt.handle = null;
        rt.status = "stopped";
      },
    );
  }

  private allocPorts(n: number): number[] {
    const ports: number[] = [];
    for (let p = this.cfg.portMin; p <= this.cfg.portMax && ports.length < n; p++) {
      if (!this.usedPorts.has(p)) {
        this.usedPorts.add(p);
        ports.push(p);
      }
    }
    if (ports.length < n) {
      this.freePorts(ports);
      throw new ArenaError(503, "resource_unavailable", "host port pool exhausted");
    }
    return ports;
  }

  private freePorts(ports: number[]): void {
    for (const p of ports) this.usedPorts.delete(p);
  }

  // ---- 持久化 ------------------------------------------------------------------

  private load(): PersistShape | null {
    try {
      const raw = readFileSync(this.cfg.stateFile, "utf8");
      const data = JSON.parse(raw) as PersistShape;
      if (data.version !== 1) return null;
      return data;
    } catch {
      return null; // 无文件/坏文件：全新开始
    }
  }

  private save(): void {
    const data: PersistShape = {
      version: 1,
      task_started_at: this.taskStartedAt,
      deadline_at: this.deadlineAt,
      challenges: {},
    };
    for (const rt of this.rt.values()) {
      data.challenges[rt.spec.unique_code] = {
        correct_flags: [...rt.correctFlags].sort((a, b) => a - b),
        hint_viewed: rt.hintViewed,
        cumulative: rt.cumulative,
      };
    }
    const tmp = `${this.cfg.stateFile}.tmp`;
    writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`);
    renameSync(tmp, this.cfg.stateFile); // 原子替换
  }
}

/** 每 flag 得分：均分 + 余数给最后一个 flag（sum(perFlag) === total_score，awarded 为整数） */
export function perFlagScore(spec: ChallengeSpec, idx: number): number {
  const n = spec.flags.length;
  const base = Math.floor(spec.total_score / n);
  return idx < n - 1 ? base : spec.total_score - base * (n - 1);
}
