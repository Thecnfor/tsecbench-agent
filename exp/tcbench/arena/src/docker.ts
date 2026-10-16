/** CliDriver：spawn docker CLI（零第三方依赖）。镜像幂等预热构建、容器 label 化、强删回收。
 *
 * 容器固定 label：tcbench.arena=1（对账扫描）+ tcbench.code=<unique_code>。
 * 靶机只发布端口到宿主机，不做网络隔离——agent（宿主机或容器经 docker0 网关）直连。
 */
import { spawn } from "node:child_process";
import { waitPortsReady } from "./probe.js";
import type { ArenaConfig } from "./config.js";
import type { ChallengeSpec, Driver, DriverHandle } from "./types.js";

const MANAGED_LABEL = "tcbench.arena=1";

function docker(args: string[], timeoutMs = 120_000): Promise<string> {
  return new Promise((resolveP, reject) => {
    const child = spawn("docker", args, { stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`docker ${args[0]} 超时 ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (d: Buffer) => (out += d.toString()));
    child.stderr.on("data", (d: Buffer) => (err += d.toString()));
    child.once("error", (e) => {
      clearTimeout(timer);
      reject(new Error(`docker 不可用：${e.message}`));
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolveP(out.trim());
      else reject(new Error(`docker ${args.slice(0, 3).join(" ")}… 退出 ${code}：${(err || out).trim()}`));
    });
  });
}

export function containerName(code: string): string {
  return `tcbench-target-${code}`;
}

export function imageTag(code: string): string {
  return `tcbench-target/${code}:latest`;
}

/** 解析 docker inspect 的 .NetworkSettings.Ports JSON → 宿主机端口（按容器端口排序） */
export function parsePorts(json: string): number[] {
  let parsed: Record<string, Array<{ HostPort: string }>> | null;
  try {
    parsed = JSON.parse(json) as Record<string, Array<{ HostPort: string }>>;
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== "object") return [];
  const rows = Object.entries(parsed)
    .map(([containerPort, bindings]) => ({
      cport: parseInt(containerPort, 10) || 0,
      hport: Number(bindings?.[0]?.HostPort ?? 0),
    }))
    .filter((r) => r.hport > 0)
    .sort((a, b) => a.cport - b.cport);
  return rows.map((r) => r.hport);
}

export class CliDriver implements Driver {
  private cfg: ArenaConfig;
  /** 同一题的 build 去重（并发 start / 预热共用一个 promise） */
  private builds = new Map<string, Promise<void>>();

  constructor(cfg: ArenaConfig) {
    this.cfg = cfg;
  }

  async run(spec: ChallengeSpec, hostPorts: number[]): Promise<DriverHandle> {
    await this.ensureImage(spec);
    const name = containerName(spec.unique_code);
    // 上次异常残留的同名容器：防御性清掉（正常路径由 state.reconcile 处理）
    await docker(["rm", "-f", name], 20_000).catch(() => undefined);
    const args = [
      "run", "-d",
      "--name", name,
      "--label", MANAGED_LABEL,
      "--label", `tcbench.code=${spec.unique_code}`,
    ];
    spec.container_ports.forEach((c, i) => args.push("-p", `${hostPorts[i]}:${c}`));
    if (spec.flag_inject === "env") {
      args.push("-e", `FLAG_COUNT=${spec.flags.length}`);
      spec.flags.forEach((f, i) => args.push("-e", `FLAG_${i}=${f}`));
    }
    args.push(spec.image ?? imageTag(spec.unique_code));
    await docker(args);
    return { code: spec.unique_code, name, host_ports: [...hostPorts] };
  }

  async waitReady(spec: ChallengeSpec, h: DriverHandle): Promise<void> {
    await waitPortsReady({
      host: "127.0.0.1",
      ports: h.host_ports,
      timeoutMs: this.cfg.readyTimeoutS * 1000,
      intervalMs: this.cfg.readyIntervalMs,
      readyPath: spec.ready_path,
    });
  }

  async rm(h: DriverHandle): Promise<void> {
    await docker(["rm", "-f", h.name], 20_000).catch(() => undefined);
  }

  async listManaged(): Promise<Map<string, number[]>> {
    const ids = await docker(["ps", "-aq", "--filter", `label=${MANAGED_LABEL}`], 20_000);
    const map = new Map<string, number[]>();
    if (!ids) return map;
    for (const id of ids.split("\n").filter(Boolean)) {
      const meta = await docker(
        ["inspect", "-f", '{{index .Config.Labels "tcbench.code"}}\t{{json .NetworkSettings.Ports}}', id],
        20_000,
      );
      const [code, portsJson = ""] = meta.split("\t");
      if (!code) continue;
      const ports = parsePorts(portsJson);
      map.set(code, [...(map.get(code) ?? []), ...ports]);
    }
    return map;
  }

  /** 启动预热：缺镜像就后台 build（失败只记日志，不阻塞服务；start 时 ensureImage 兜底） */
  async prebuild(spec: ChallengeSpec): Promise<void> {
    try {
      await this.ensureImage(spec);
      console.log(`[arena] 镜像就绪 ${spec.image ?? imageTag(spec.unique_code)}`);
    } catch (e) {
      console.log(`[arena] 预热构建失败 ${spec.unique_code}：${(e as Error).message}`);
    }
  }

  private async ensureImage(spec: ChallengeSpec): Promise<void> {
    if (spec.image) {
      await docker(["image", "inspect", spec.image], 20_000).catch(() => {
        throw new Error(`镜像 ${spec.image} 不存在且题包未提供 Dockerfile 构建路径（arena 不负责拉取）`);
      });
      return;
    }
    const pending = this.builds.get(spec.unique_code);
    if (pending) return pending;
    const build = docker(["build", "-t", imageTag(spec.unique_code), spec.dir], 300_000).then(() => undefined);
    this.builds.set(spec.unique_code, build);
    try {
      await build;
    } catch (e) {
      this.builds.delete(spec.unique_code); // 失败不缓存，允许重试
      throw e;
    }
  }
}
