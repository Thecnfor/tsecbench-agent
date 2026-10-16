/** FakeDriver：内存桩。无 docker 时供测试与 ARENA_DRIVER=fake 演示；可注入延迟与失败。 */
import type { ChallengeSpec, Driver, DriverHandle } from "./types.js";

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export class FakeDriver implements Driver {
  /** waitReady 模拟的启动耗时 */
  readyDelayMs: number;
  /** 非 null 时所有 run 抛它（模拟 docker 不可用） */
  failRun: Error | null = null;
  /** 非 null 时 waitReady 抛它（模拟就绪探测超时） */
  failReady: Error | null = null;
  /** 模拟 docker 对账面：code → 宿主机端口 */
  managed = new Map<string, number[]>();
  private seq = 0;

  constructor(opts: { readyDelayMs?: number } = {}) {
    this.readyDelayMs = opts.readyDelayMs ?? 50;
  }

  async run(spec: ChallengeSpec, hostPorts: number[]): Promise<DriverHandle> {
    if (this.failRun) throw this.failRun;
    this.managed.set(spec.unique_code, [...hostPorts]);
    return { code: spec.unique_code, name: `fake-${spec.unique_code}-${++this.seq}`, host_ports: [...hostPorts] };
  }

  async waitReady(_spec: ChallengeSpec, _h: DriverHandle): Promise<void> {
    await sleep(this.readyDelayMs);
    if (this.failReady) throw this.failReady;
  }

  async rm(h: DriverHandle): Promise<void> {
    this.managed.delete(h.code);
  }

  async listManaged(): Promise<Map<string, number[]>> {
    return new Map(this.managed);
  }
}
