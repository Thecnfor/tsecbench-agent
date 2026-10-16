/** 就绪探测：TCP connect 宿主机发布端口；ready_path 存在时再 HTTP GET（收到任何响应即视为就绪）。 */
import net from "node:net";

export function tcpProbe(host: string, port: number, timeoutMs = 1000): Promise<boolean> {
  return new Promise((resolveP) => {
    const sock = net.connect({ host, port });
    let settled = false;
    const done = (ok: boolean) => {
      if (settled) return;
      settled = true;
      sock.destroy();
      resolveP(ok);
    };
    sock.once("connect", () => done(true));
    sock.once("error", () => done(false));
    sock.setTimeout(timeoutMs, () => done(false));
  });
}

export interface ReadyOptions {
  host: string;
  ports: number[];
  timeoutMs: number;
  intervalMs: number;
  readyPath?: string | null;
}

/** 轮询直到全部端口 TCP 通（可选 HTTP 探测）；超时 reject。 */
export async function waitPortsReady(opts: ReadyOptions): Promise<void> {
  const deadline = Date.now() + opts.timeoutMs;
  for (;;) {
    const results = await Promise.all(opts.ports.map((p) => tcpProbe(opts.host, p)));
    if (results.every(Boolean)) {
      if (!opts.readyPath) return;
      try {
        await fetch(`http://${opts.host}:${opts.ports[0]}${opts.readyPath}`, {
          signal: AbortSignal.timeout(2000),
        });
        return; // 收到任何 HTTP 响应（含 4xx/5xx）都算服务已起
      } catch {
        // HTTP 还没起：继续轮询
      }
    }
    if (Date.now() >= deadline) {
      throw new Error(`ready probe timeout: ${opts.host}:${opts.ports.join(",")}`);
    }
    await new Promise((r) => setTimeout(r, opts.intervalMs));
  }
}
