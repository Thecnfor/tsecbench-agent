/** 面板数据读取与统计（只读记录 + 平台 API；对应 panel.py 的数据层）。
 *
 * 数据源：
 *  1. bench 持久化记录 runs/<TOKEN>/（meta.json / tasks.jsonl / events.jsonl / conversation.jsonl）
 *  2. 平台 API（token = 目录名）：题数/通关数/flag 数/总分——有选题时主数字只统计选中的题
 *  3. docker/archives/*.tar.gz 历史归档 → 每题历史耗时基线（预测对比）
 *
 * 注意：记录里的时间字符串（started_at/closed_at/ts）由容器写入，容器无时区 = **UTC**；
 * 解析必须按 UTC（Date.parse + 'Z'），否则"已跑时长"会虚高一个时区差（如 +8h）。
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { blackboardPath } from "../corelog.js";
import type { Challenge, ConvRecord, TaskRecord } from "../types.js";

export const REFRESH_DEFAULT = 5;

// ---------------- 数据读取 ----------------

/** 读取 jsonl 末尾 n 条（容忍写入中的半行）。 */
export function readJsonlTail(file: string, n = 40): any[] {
  if (!fs.existsSync(file)) return [];
  try {
    const stat = fs.statSync(file);
    const fd = fs.openSync(file, "r");
    try {
      const size = stat.size;
      const start = Math.max(0, size - 256 * 1024);
      const len = size - start;
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, start);
      const out: any[] = [];
      for (const line of buf.toString("utf8").split("\n")) {
        const s = line.trim();
        if (!s) continue;
        try {
          out.push(JSON.parse(s));
        } catch {
          /* 半行容忍 */
        }
      }
      return out.slice(-n);
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return [];
  }
}

export function readJsonlAll(file: string): any[] {
  if (!fs.existsSync(file)) return [];
  const out: any[] = [];
  try {
    for (const line of fs.readFileSync(file, "utf8").split("\n")) {
      if (!line.trim()) continue;
      try {
        out.push(JSON.parse(line));
      } catch {
        /* skip broken line */
      }
    }
  } catch {
    /* ignore */
  }
  return out;
}

export interface TaskView {
  code: string;
  duration: number;
  segments: number;
  started_at?: string;
  closed_at?: string;
  target?: string;
  attempt?: number;
  flags?: string;
  outcome?: string;
  summary?: string;
  running: boolean;
}

/** tasks.jsonl → {code: 生命周期视图}；分段解题的多段 close 耗时累加。 */
export function loadTasks(runsDir: string): Record<string, TaskView> {
  const tasks: Record<string, TaskView> = {};
  for (const r of readJsonlAll(path.join(runsDir, "tasks.jsonl")) as TaskRecord[]) {
    const code = r.code ?? "?";
    let t = tasks[code];
    if (!t) {
      t = { code, duration: 0, segments: 0, running: false };
      tasks[code] = t;
    }
    if (r.phase === "task_start") {
      t.started_at = r.started_at;
      t.target = r.target;
      t.attempt = r.attempt;
      t.running = true;
      t.segments = Number(t.segments || 0) + 1;
    } else if (r.phase === "task_close") {
      t.closed_at = r.closed_at;
      t.outcome = r.outcome;
      t.flags = r.flags;
      t.summary = (r.summary || "").slice(0, 200);
      t.running = false;
      // 分段解题：暂停/续跑会产生多段 close，耗时必须累加而不是覆盖
      t.duration = Number(t.duration || 0) + Number(r.duration_sec || 0);
    }
  }
  return tasks;
}

/** 记录里的时间字符串（UTC，容器无时区）→ epoch 秒；解析失败返回 null。 */
export function utcEpoch(s?: string | null): number | null {
  if (!s) return null;
  const t = Date.parse(`${s.replace(" ", "T")}Z`);
  return Number.isNaN(t) ? null : t / 1000;
}

// ---- 历史归档基线（tar.gz）----
const BASE_CACHE: { key: string; value: Record<string, number> } | null = null;
let baseCache: { key: string; value: Record<string, number> } | null = BASE_CACHE;

/** 从 docker/archives/*.tar.gz 读历史每题耗时 → {code: 秒}（预测对比的基线）。
 * - 同名题以后打包的归档为准（文件名带时间戳，字典序即时间序）；
 * - 只算已 close 的段；tar.gz 逐帧重读会卡 → 按"文件指纹"缓存，只解一次。 */
export function loadBaseline(runsDir: string): Record<string, number> {
  const root = path.resolve(runsDir.replace(/\/+$/, ""));
  // archives 是 runs/ 的兄弟目录（真实布局 docker/archives）；同时接受扁平布局
  const cands = [
    path.join(path.dirname(path.dirname(root)), "archives"),
    path.join(path.dirname(root), "archives"),
  ];
  let adir: string | null = null;
  for (const d of cands) {
    try {
      if (fs.statSync(d).isDirectory() && fs.readdirSync(d).some((n) => n.endsWith(".tar.gz"))) {
        adir = d;
        break;
      }
    } catch {
      /* not this one */
    }
  }
  if (!adir) return {};
  const files = fs
    .readdirSync(adir)
    .filter((n) => n.endsWith(".tar.gz"))
    .sort()
    .map((n) => path.join(adir!, n));
  let key: string;
  try {
    key = files
      .map((p) => {
        const st = fs.statSync(p);
        return `${p}:${st.mtimeMs}:${st.size}`;
      })
      .join("|");
  } catch {
    return {};
  }
  if (baseCache && baseCache.key === key) return baseCache.value;

  const base: Record<string, number> = {};
  for (const p of files) {
    try {
      // 用系统 tar 解出 tasks.jsonl（Node 无内置 tar；容器/宿主机均有 GNU tar）
      const out = execFileSync("tar", ["-xOzf", p, "--wildcards", "*/tasks.jsonl"], {
        maxBuffer: 64 * 1024 * 1024,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      });
      const dur: Record<string, number> = {};
      for (const line of out.split("\n")) {
        if (!line.trim()) continue;
        let r: any;
        try {
          r = JSON.parse(line);
        } catch {
          continue;
        }
        if (r.phase === "task_close") {
          const c = r.code ?? "?";
          dur[c] = Number(dur[c] || 0) + Number(r.duration_sec || 0);
        }
      }
      Object.assign(base, dur); // 后写的归档覆盖先写的（更新的基线优先）
    } catch {}
  }
  baseCache = { key, value: base };
  return base;
}

export interface PredictItem {
  code: string;
  state: "done" | "fail" | "run" | "todo";
  pred: number;
  base: number;
  actual: number;
  delta: number;
}

export interface PredictResult {
  total_pred: number;
  total_base: number;
  items: PredictItem[];
}

/** 实时预测本题集总耗时，与历史归档对比。
 *
 * 规则：默认每题与历史**等长**；提前完成直接扣掉省下的时间；还在跑则预测取
 * `max(历史, 已耗时)`——超出部分随每次刷新实时累加。无基线时返回 null。 */
export function predictVsBaseline(
  tasks: Record<string, TaskView>,
  base: Record<string, number>,
  patterns: string[],
  now: number,
): PredictResult | null {
  const codes = new Set(Object.keys(tasks));
  for (const c of Object.keys(base)) {
    // 显式选中的题即使还没开始也按基线计入（"未开始"预测）；
    // 没设选题时不补历史码——下个题集的题号可能完全不同。
    if (patterns.length && patterns.some((p) => globMatch(p, c))) codes.add(c);
  }
  const items: PredictItem[] = [];
  for (const c of [...codes].sort()) {
    const b = base[c];
    if (b === undefined) continue; // 历史没有的新题：无从对比，不计入
    const t = tasks[c] ?? ({} as TaskView);
    let actual = Number(t.duration || 0);
    let pred: number;
    let state: PredictItem["state"];
    if (t.running || t.outcome === "paused") {
      const st = utcEpoch(t.started_at);
      if (st !== null) actual += Math.max(0, now - st); // 当前段已跑时长，刷新即增长
      pred = Math.max(b, actual);
      state = "run";
    } else if (t.outcome === "pass") {
      pred = actual;
      state = "done";
    } else if (t.outcome === "fail") {
      pred = actual;
      state = "fail";
    } else {
      pred = b;
      state = "todo";
    }
    items.push({ code: c, state, pred, base: b, actual, delta: pred - b });
  }
  if (!items.length) return null;
  return {
    total_pred: items.reduce((s, i) => s + i.pred, 0),
    total_base: items.reduce((s, i) => s + i.base, 0),
    items,
  };
}

/** glob 通配匹配（与 agent 同一套规则，* 与 ?）。 */
export function globMatch(pattern: string, s: string): boolean {
  const esc = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${esc}$`).test(s);
}

export function loadMeta(runsDir: string): Record<string, any> {
  try {
    return JSON.parse(fs.readFileSync(path.join(runsDir, "meta.json"), "utf8"));
  } catch {
    return {};
  }
}

export function loadRecentEvents(runsDir: string, n = 6): any[] {
  return readJsonlTail(path.join(runsDir, "events.jsonl"), n).reverse();
}

export interface LiveView {
  last_output: string;
  last_thinking: string;
  running_cmds: string[];
  last_cmd: string;
  branches: string[]; // fork 分支名册（"1: 方向…"；含已回收标记）
  log_tail: string[]; // 核心日志尾部（旁观者视角缩略）
}

/** 单题对话的实时视角：最近模型输出 + 进行中/最近的 shell 调用 + fork 分支名册。 */
export function taskLiveView(runsDir: string, code: string): LiveView {
  const convPath = path.join(runsDir, "tasks", code, "conversation.jsonl");
  const msgs = readJsonlTail(convPath, 60) as ConvRecord[];
  const view: LiveView = {
    last_output: "",
    last_thinking: "",
    running_cmds: [],
    last_cmd: "",
    branches: [],
    log_tail: [],
  };
  const pending: string[] = []; // 已发出、未回结果的 tool_call
  const finished: string[] = []; // 最近完成的命令
  for (const r of msgs) {
    if (r.kind === "assistant") {
      if (r.output) view.last_output = String(r.output);
      if (r.thinking) view.last_thinking = String(r.thinking);
      for (const tc of r.tool_calls ?? []) {
        let args: any = {};
        try {
          args = JSON.parse(tc.arguments || "{}");
        } catch {
          args = {};
        }
        const action = args.action ?? "shell";
        let desc: string;
        if (args.command) desc = `${action}: ${args.command}`;
        else
          desc =
            `${action} ` +
            Object.entries(args)
              .filter(([k]) => k !== "action")
              .map(([k, v]) => `${k}=${v}`)
              .join(" ");
        pending.push(desc.slice(0, 110));
      }
    } else if (r.kind === "tool") {
      const cmd =
        r.arguments && typeof r.arguments === "object"
          ? String((r.arguments as any).command ?? "")
          : "";
      finished.push(cmd.slice(0, 110));
      // 结果回来了，移除对应 pending（简单法：清掉第一条同类）
      let removed = false;
      for (let i = 0; i < pending.length; i++) {
        const p = pending[i];
        if ((r.tool && p.includes(String(r.tool))) || (cmd && p.includes(cmd.slice(0, 40)))) {
          pending.splice(i, 1);
          removed = true;
          break;
        }
      }
      if (!removed && pending.length) pending.shift();
    }
  }
  view.running_cmds = pending.slice(0, 4);
  view.last_cmd = finished.length ? finished[finished.length - 1] : "";
  // fork 分支名册（conversation 的 fork_directive + corelog 的 prune）+ 核心日志尾部
  try {
    const pruned = new Set<number>();
    for (const r of readJsonlTail(blackboardPath(path.join(runsDir, "tasks"), code), 200)) {
      if ((r as any).kind === "prune") pruned.add(Number((r as any).branch));
    }
    for (const r of msgs) {
      if (r.kind === "user" && r.kind_note === "fork_directive") {
        const id = Number(r.branch ?? 0);
        view.branches.push(
          `${id}: ${String(r.fork_label ?? "").slice(0, 40)}${pruned.has(id) ? "（已回收）" : ""}`,
        );
      }
    }
    const corelog = readJsonlTail(blackboardPath(path.join(runsDir, "tasks"), code), 6) as any[];
    view.log_tail = corelog
      .reverse()
      .map((e) => `b${e.branch} ${e.kind}${e.note ? `: ${String(e.note).slice(0, 60)}` : ""}`);
  } catch {
    /* 无 corelog（fork 未启用）→ 保持空 */
  }
  return view;
}

/** 平台题目列表 → 统计；patterns 非空时只统计选中的题。 */
export function platStats(items: Challenge[], patterns: string[]): Record<string, number> {
  let sel = items;
  if (patterns.length) {
    sel = items.filter((c) => patterns.some((p) => globMatch(p, String(c.unique_code ?? ""))));
  }
  return {
    total: sel.length,
    completed: sel.filter((c) => c.is_completed).length,
    flags: sel.reduce((s, c) => s + (c.correct_flag_count || 0), 0),
    max_flags: sel.reduce((s, c) => s + (c.flag_count || 0), 0),
    score: sel.filter((c) => c.is_completed).reduce((s, c) => s + (c.total_score || 0), 0),
  };
}

/** token = 目录名。返回 {global, sel} 或 null（离线）。 */
export async function fetchPlatform(
  runsDir: string,
  baseUrl: string,
  patterns: string[],
  timeoutMs = 8000,
): Promise<{ global: Record<string, number>; sel: Record<string, number> | null } | null> {
  const token = path.basename(runsDir.replace(/\/+$/, ""));
  if (!token || !baseUrl) return null;
  try {
    const resp = await fetch(`${baseUrl.replace(/\/+$/, "")}/openapi/v1/challenges`, {
      headers: { BENCHMARK_TOKEN: token },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const items = (await resp.json()) as Challenge[];
    if (!Array.isArray(items)) return null;
    return {
      global: platStats(items, []),
      sel: patterns.length ? platStats(items, patterns) : null,
    };
  } catch {
    return null;
  }
}

/** 环境变量优先，其次向上找项目根 .env（不打印值，只给调用方用）。 */
export function envValue(key: string, def = ""): string {
  const v = process.env[key];
  if (v) return v;
  let d = path.resolve(process.cwd());
  for (let i = 0; i < 5; i++) {
    const p = path.join(d, ".env");
    if (fs.existsSync(p)) {
      for (const line of fs.readFileSync(p, "utf8").split("\n")) {
        if (line.startsWith(`${key}=`)) return line.split("=").slice(1).join("=").trim();
      }
    }
    const parent = path.dirname(d);
    if (parent === d) break;
    d = parent;
  }
  return def;
}

export function envBaseUrl(): string {
  return envValue("BENCHMARK_BASE_URL") || "https://tsecbench.zc.tencent.com";
}

/** 本次运行的选题模式：优先 meta.runs[-1].task_filter（agent 落盘），旧记录回退到环境/.env。 */
export function taskFilterOf(meta: Record<string, any>): string[] {
  const runs: any[] = meta.runs?.length ? meta.runs : [{}];
  const sel: string[] = runs[runs.length - 1]?.task_filter ?? [];
  if (sel.length) return sel.map(String);
  const raw = (envValue("AGENT_TASK_FILTER") || envValue("AGENT_TASKS") || "").trim();
  return raw.replace(/,/g, " ").split(/\s+/).filter(Boolean);
}

/** 自动发现最新 runs/<TOKEN>：当前目录、../docker/runs、项目根。 */
/** 该目录是不是一个 bench 记录目录（有 meta.json 或 tasks.jsonl）。 */
export function isBenchDir(p: string): boolean {
  try {
    return (
      fs.statSync(p).isDirectory() &&
      (fs.existsSync(path.join(p, "meta.json")) || fs.existsSync(path.join(p, "tasks.jsonl")))
    );
  } catch {
    return false;
  }
}

/** 目录内记录文件的最新 mtime（ms）——比目录自身 mtime 可靠：
 *  run.sh 先 mkdir 再跑容器，空目录 mtime 反而最新，按目录 mtime 会选中刚建的空目录；
 *  记录文件（meta/events/tasks.jsonl）的 mtime 才反映"哪个 bench 真正在写"。 */
export function recordsMtime(p: string): number {
  let newest = 0;
  for (const f of ["meta.json", "events.jsonl", "tasks.jsonl"]) {
    try {
      newest = Math.max(newest, fs.statSync(path.join(p, f)).mtimeMs);
    } catch {
      /* 文件还没有 */
    }
  }
  return newest;
}

/** 自动发现 bench 记录目录，返回最新写入的那个 <TOKEN> 目录。
 *
 * 搜索策略（修 bug：旧版 `../../../docker/runs` 只在源码布局下正确，构建产物在
 * dist/ 时算到 exp/docker/runs 不存在；且按目录 mtime 排序会选中 run.sh 刚建的空目录）：
 *  1. 从本模块所在目录与 cwd 各自向上逐级找 `docker/runs`（对 src/ 与 dist/ 布局都成立）；
 *  2. 候选里只保留真正的 bench 目录（有 meta.json 或 tasks.jsonl）；
 *  3. 按目录内记录文件的最新 mtime 排序——"正在被写"的排最前。 */
export function findLatestRuns(): string | null {
  const bases = new Set<string>();
  const probe = (start: string) => {
    let dir = start;
    for (let i = 0; i < 6; i++) {
      const cand = path.join(dir, "docker", "runs");
      try {
        if (fs.statSync(cand).isDirectory()) bases.add(cand);
      } catch {
        /* 这一级没有 */
      }
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  };
  probe(fileDir(import.meta.url)); // 模块向上：dist/ 或 src/panel/ 都能命中 tcbench/docker/runs
  probe(process.cwd()); // cwd 向上：无论从哪个目录启动 pnpm serve 都能找到

  let best: string | null = null;
  let bestMtime = -1;
  for (const base of bases) {
    let names: string[];
    try {
      names = fs.readdirSync(base);
    } catch {
      continue;
    }
    for (const name of names) {
      const p = path.join(base, name);
      if (!isBenchDir(p)) continue;
      const m = recordsMtime(p);
      if (m > bestMtime) {
        bestMtime = m;
        best = p;
      }
    }
  }
  return best;
}

/** 把用户给的 --dir 归一化为 bench 目录：给 <TOKEN> 直接用；给 runs/ 父目录则取其中最新。 */
export function resolveRunsDir(dir: string): string | null {
  if (isBenchDir(dir)) return dir;
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return null;
  }
  let best: string | null = null;
  let bestMtime = -1;
  for (const name of names) {
    const p = path.join(dir, name);
    if (!isBenchDir(p)) continue;
    const m = recordsMtime(p);
    if (m > bestMtime) {
      bestMtime = m;
      best = p;
    }
  }
  return best;
}

function fileDir(url: string): string {
  return path.dirname(new URL(url).pathname);
}

// ---------------- 展示格式 ----------------

export function fmtDur(seconds: number): string {
  let s = Math.trunc(seconds);
  const h = Math.trunc(s / 3600);
  s -= h * 3600;
  const m = Math.trunc(s / 60);
  s -= m * 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

/** 带符号时长差：提前（少花）为 `-`，超出为 `+`。 */
export function fmtDelta(seconds: number): string {
  return (seconds < 0 ? "-" : "+") + fmtDur(Math.abs(seconds));
}
