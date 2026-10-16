/** 题包加载：challenges/<dir>/challenge.json → ChallengeSpec。坏题包 fail-fast（启动即报，不留到赛中）。
 *
 * 语义见 arena/README「题包 schema」。要点：flags 是计分真值（唯一性必须校验，
 * 否则 duplicate 判定失效）；无 image 时必须有 Dockerfile（现场 build）。
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import type { ChallengeSpec } from "./types.js";

const CODE_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/; // docker 容器名安全

function fail(dir: string, msg: string): never {
  throw new Error(`题包 ${dir} 非法：${msg}`);
}

export function loadCatalog(challengesDir: string): Map<string, ChallengeSpec> {
  const packs = new Map<string, ChallengeSpec>();
  if (!existsSync(challengesDir)) throw new Error(`题包目录不存在：${challengesDir}`);
  for (const entry of readdirSync(challengesDir).sort()) {
    const dir = resolve(challengesDir, entry);
    if (!statSync(dir).isDirectory()) continue;
    const spec = loadPack(dir, entry);
    if (packs.has(spec.unique_code)) {
      throw new Error(`unique_code 重复：${spec.unique_code}（${packs.get(spec.unique_code)?.dir} 与 ${dir}）`);
    }
    packs.set(spec.unique_code, spec);
  }
  if (!packs.size) throw new Error(`题包目录 ${challengesDir} 下没有任何题包目录`);
  return packs;
}

/** 解析单个题包目录。缺省值：unique_code=目录名、difficulty=easy、level=1、total_score=100、
 * hint_cost_ratio=0.2、flag_inject=env。 */
export function loadPack(dir: string, fallbackCode: string): ChallengeSpec {
  const file = resolve(dir, "challenge.json");
  if (!existsSync(file)) fail(dir, "缺少 challenge.json");
  let raw: Record<string, unknown>;
  try {
    raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
  } catch (e) {
    fail(dir, `challenge.json 解析失败：${(e as Error).message}`);
  }

  const unique_code = raw.unique_code === undefined ? fallbackCode : String(raw.unique_code);
  if (!CODE_RE.test(unique_code)) fail(dir, `unique_code ${JSON.stringify(unique_code)} 含非法字符`);

  if (!Array.isArray(raw.flags) || raw.flags.length === 0) fail(dir, "flags 必须是非空数组");
  const flags = raw.flags.map((f) => String(f));
  if (flags.some((f) => f.length === 0)) fail(dir, "flags 含空字符串");
  if (new Set(flags).size !== flags.length) fail(dir, "flags 有重复值（duplicate 判定会失效）");

  if (!Array.isArray(raw.container_ports) || raw.container_ports.length === 0) {
    fail(dir, "container_ports 必须是非空数组");
  }
  const container_ports = raw.container_ports.map((p) => Number(p));
  if (container_ports.some((p) => !Number.isInteger(p) || p < 1 || p > 65535)) {
    fail(dir, "container_ports 必须是 1~65535 的整数");
  }
  if (new Set(container_ports).size !== container_ports.length) fail(dir, "container_ports 有重复值");

  const image = raw.image === undefined || raw.image === null ? null : String(raw.image);
  if (image === null && !existsSync(resolve(dir, "Dockerfile"))) {
    fail(dir, "无 image 字段时必须提供同目录 Dockerfile（现场 build）");
  }

  const flag_inject = raw.flag_inject === undefined ? "env" : String(raw.flag_inject);
  if (flag_inject !== "env" && flag_inject !== "none") {
    fail(dir, `flag_inject 只能是 env|none，收到 ${JSON.stringify(raw.flag_inject)}`);
  }

  const total_score = raw.total_score === undefined ? 100 : Number(raw.total_score);
  if (!Number.isInteger(total_score) || total_score < 1) fail(dir, "total_score 必须是正整数");
  const level = raw.level === undefined ? 1 : Number(raw.level);
  if (!Number.isInteger(level) || level < 1) fail(dir, "level 必须是正整数");
  const hint_cost_ratio = raw.hint_cost_ratio === undefined ? 0.2 : Number(raw.hint_cost_ratio);
  if (!Number.isFinite(hint_cost_ratio) || hint_cost_ratio < 0 || hint_cost_ratio > 1) {
    fail(dir, "hint_cost_ratio 必须在 [0,1]");
  }

  const ready_path = raw.ready_path === undefined || raw.ready_path === null ? null : String(raw.ready_path);
  if (ready_path !== null && !ready_path.startsWith("/")) fail(dir, 'ready_path 必须以 "/" 开头');

  return {
    dir,
    unique_code,
    description: raw.description === undefined ? null : String(raw.description),
    difficulty: raw.difficulty === undefined ? "easy" : String(raw.difficulty),
    level,
    total_score,
    hint: raw.hint === undefined ? null : String(raw.hint),
    hint_cost_ratio,
    flags,
    image,
    container_ports,
    flag_inject,
    ready_path,
  };
}
