/** 全部配置来自 ARENA_* 环境变量；arenaRoot() 从模块位置上溯包根（src/ 与 dist/ 双形态同源）。 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export interface ArenaConfig {
  bind: string;
  /** 0 = 系统分配端口（测试用） */
  port: number;
  token: string;
  /** 返回给客户端的宿主机地址：agent 在宿主机 = 127.0.0.1；agent 在容器里 = docker0 网关（如 172.17.0.1） */
  advertiseHost: string;
  portMin: number;
  portMax: number;
  maxActive: number;
  /** 任务总时限（秒），0 = 不限时；到点全接口 409 invalid_state（message 含 "finished"，agent 判终态即停） */
  totalLimitS: number;
  challengesDir: string;
  stateFile: string;
  readyTimeoutS: number;
  readyIntervalMs: number;
  driver: "cli" | "fake";
}

/** 从模块文件位置上溯找到 arena 包根（package.json name=tcbench-arena）。 */
export function arenaRoot(): string {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 6; i++) {
    const pkg = resolve(dir, "package.json");
    if (existsSync(pkg)) {
      try {
        const meta = JSON.parse(readFileSync(pkg, "utf8")) as { name?: string };
        if (meta.name === "tcbench-arena") return dir;
      } catch {
        // 坏 package.json：继续上溯
      }
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return process.cwd();
}

type Env = Record<string, string | undefined>;

function str(env: Env, name: string, dflt: string): string {
  const raw = env[name];
  return raw === undefined || raw === "" ? dflt : raw;
}

function num(env: Env, name: string, dflt: number): number {
  const raw = env[name];
  if (raw === undefined || raw === "") return dflt;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0) throw new Error(`${name} 必须是非负数字，收到 ${JSON.stringify(raw)}`);
  return n;
}

export function loadConfig(env: Env = process.env): ArenaConfig {
  const root = arenaRoot();
  const driver = str(env, "ARENA_DRIVER", "cli");
  if (driver !== "cli" && driver !== "fake") {
    throw new Error(`ARENA_DRIVER 只能是 cli|fake，收到 ${JSON.stringify(driver)}`);
  }
  const cfg: ArenaConfig = {
    bind: str(env, "ARENA_BIND", "127.0.0.1"),
    port: num(env, "ARENA_PORT", 8899),
    token: str(env, "ARENA_TOKEN", "arena-dev-token"),
    advertiseHost: str(env, "ARENA_ADVERTISE_HOST", "127.0.0.1"),
    portMin: num(env, "ARENA_PORT_MIN", 9100),
    portMax: num(env, "ARENA_PORT_MAX", 9199),
    maxActive: num(env, "ARENA_MAX_ACTIVE", 3),
    totalLimitS: num(env, "ARENA_TOTAL_LIMIT_S", 0),
    challengesDir: str(env, "ARENA_CHALLENGES_DIR", resolve(root, "challenges")),
    stateFile: str(env, "ARENA_STATE_FILE", resolve(root, "state.json")),
    readyTimeoutS: num(env, "ARENA_READY_TIMEOUT_S", 60),
    readyIntervalMs: num(env, "ARENA_READY_INTERVAL_MS", 400),
    driver,
  };
  if (cfg.portMin > cfg.portMax) {
    throw new Error(`ARENA_PORT_MIN(${cfg.portMin}) 不能大于 ARENA_PORT_MAX(${cfg.portMax})`);
  }
  if (cfg.maxActive < 1) throw new Error("ARENA_MAX_ACTIVE 至少为 1");
  return cfg;
}
