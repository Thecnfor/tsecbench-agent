/** 产物指纹：扫描 shell 命令后新写入的文件，按 sha256 做跨 Attempt 去重。
 *
 * 只做有界扫描：深度/条目/单文件大小都有限，避免每次 shell 都遍历大目录。
 * 记录文件本身（conversation/log/meta）不参与，避免把记录当成产物。
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export interface Artifact {
  path: string;
  hash: string;
  size: number;
}

const MAX_ENTRIES = 400;
const MAX_DEPTH = 4;
const MAX_FILE_BYTES = 8 * 1024 * 1024;
const MAX_FILES = 5;
const RECORD_NAMES = new Set([
  "conversation.jsonl",
  "log.jsonl",
  "corelog.jsonl",
  "meta.json",
  "events.jsonl",
  "tasks.jsonl",
  "planner.json",
  "planner.json.tmp",
]);

/** 只对可能写文件的命令做扫描，普通输出/只读命令直接跳过。 */
export function looksLikeFileCommand(cmd: string): boolean {
  return /(?:^|\s)(?:curl|wget|scp|rsync|git\s+clone|unzip|tar|cp|mv|tee|dd|sqlmap|ffuf|gobuster|feroxbuster|nmap|masscan|nuclei|hydra|john|hashcat|ghidra|objdump|readelf|strings|base64|python|python3|go\s+build|gcc|g\+\+|chmod|chown|mkdir|touch)\b|>{1,2}|-o\s+\S|--output\s+\S/i.test(
    cmd,
  );
}

/** 共享靶机写操作启发式：只用于提醒模型调 state_change，不做硬拦截（避免误伤正常利用）。 */
export function looksLikeSharedWrite(cmd: string): boolean {
  return /(?:systemctl|service)\s+.*(?:restart|start|stop)|iptables|\bufw\b|\buseradd\b|\badduser\b|(?:^|\s)passwd(?:\s|$)|chpasswd|mysql\s+.*(?:create|drop|alter|insert|update)|redis-cli\s+.*(?:set|del|flush|config)|\bdocker\s|\bmount\s|sed\s+-i\s+\S*\/(?:etc|var|opt)|\bssh\b.*(?:systemctl|service\s+\S+\s+(?:restart|start)|sed\s+-i|tee\s+\/etc)|\bscp\b|\brsync\b|curl\b.*(?:-X\s*(?:POST|PUT|DELETE|PATCH)|--data)/i.test(
    cmd,
  );
}

/** 返回 sinceSec（epoch 秒）之后修改的普通文件指纹，按 mtime 取最近 MAX_FILES 个。 */
export function collectNewArtifacts(
  cwd: string,
  sinceSec: number,
  maxFiles = MAX_FILES,
): Artifact[] {
  const root = path.resolve(cwd || ".");
  const cutoff = sinceSec * 1000 - 2000;
  const found: Array<{ full: string; rel: string; mtime: number; size: number }> = [];
  let seen = 0;

  const walk = (dir: string, depth: number): void => {
    if (depth > MAX_DEPTH || seen >= MAX_ENTRIES) return;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const ent of entries) {
      if (seen++ >= MAX_ENTRIES) return;
      const full = path.join(dir, ent.name);
      if (ent.isDirectory()) {
        if (ent.name === ".git" || ent.name === "node_modules" || ent.name === "dist") continue;
        walk(full, depth + 1);
        continue;
      }
      if (!ent.isFile() || RECORD_NAMES.has(ent.name)) continue;
      let st: fs.Stats;
      try {
        st = fs.statSync(full);
      } catch {
        continue;
      }
      if (st.mtimeMs < cutoff || st.size <= 0 || st.size > MAX_FILE_BYTES) continue;
      found.push({ full, rel: path.relative(root, full), mtime: st.mtimeMs, size: st.size });
    }
  };
  walk(root, 0);
  found.sort((a, b) => b.mtime - a.mtime);
  const out: Artifact[] = [];
  for (const f of found.slice(0, maxFiles)) {
    try {
      const hash = crypto.createHash("sha256").update(fs.readFileSync(f.full)).digest("hex");
      out.push({ path: f.rel, hash, size: f.size });
    } catch {
      /* 读失败/被删：跳过 */
    }
  }
  return out;
}
