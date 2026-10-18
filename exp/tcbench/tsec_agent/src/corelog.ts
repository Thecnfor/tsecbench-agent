/** 黑板日志 log.jsonl（曾用名 corelog.jsonl，2026-10-17 更名减负）：
 *  分支抽象层——planner、面板与剪枝摘要的唯一读取源。
 *
 * 设计契约见 ../docs/fork-planner-design.md §1（flag 事件）/§5（planner/卡塞读取）/§6（记录）。
 * - 写入方：log 工具（kind=core）、planner 注入
 *   （kind=planner）、flag 事件（kind=flag，controller 以 matched_flag_index / watcher
 *   快照为权威自动记，不依赖模型自述）。
 * - flag 稳定 ID = matched_flag_index：flag 内容随靶机重启变，索引（1..total_flag_count）不变，
 *   重启续跑按 index 对账「还差哪几个」。
 * - 读取方：planner 按分支分组（交错写入没问题，读取侧分组解决可读性）。
 */
import fs from "node:fs";
import path from "node:path";
import { nowStamp } from "./records_util.js";

/** 黑板文件名解析：新 run 一律写 `log.jsonl`；读旧 run（只有 `corelog.jsonl`）
 *  时回退旧名——续跑与面板读历史归档都兼容。 */
export function blackboardPath(tasksDir: string, code: string): string {
  const fresh = path.join(tasksDir, code, "log.jsonl");
  if (fs.existsSync(fresh)) return fresh;
  const legacy = path.join(tasksDir, code, "corelog.jsonl");
  return fs.existsSync(legacy) ? legacy : fresh;
}

export type CorelogKind =
  | "core"
  | "broadcast"
  | "planner"
  | "flag"
  | "prune"
  | "fork"
  | "result"
  | "mutation"
  | "artifact";

export interface CorelogEntry {
  ts: string;
  seq: number;
  code: string;
  branch: number;
  kind: CorelogKind;
  note?: string;
  flag_index?: number | null; // flag 稳定 ID（matched_flag_index；未知记 null）
  correct_flag_count?: number;
  total_flag_count?: number;
  to?: number[]; // 旧 broadcast 记录兼容
  status?: string; // result 卡状态（done/deadend/blocked）
  mutation_id?: string;
  action?: string;
  recovery?: string;
  scope_id?: number;
  mutation_status?: "pending" | "recovered";
  artifact_hash?: string;
  artifact_path?: string;
  artifact_size?: number;
  shared?: boolean;
  [k: string]: unknown;
}

/** 单题 corelog writer：append-only，seq 单调（重启后接续）。 */
export class CorelogWriter {
  path: string;
  code: string;
  private seq = 0;

  constructor(file: string, code: string) {
    this.path = file;
    this.code = code;
    try {
      const lines = fs.readFileSync(file, "utf8").split("\n");
      this.seq = lines.filter((l) => l.trim()).length;
    } catch {
      this.seq = 0;
    }
    fs.mkdirSync(path.dirname(file), { recursive: true });
  }

  /** 写一条（参数不用 Omit：索引签名会让 Omit 丢掉具名字段的类型检查）。 */
  write(
    entry: Omit<CorelogEntry, "ts" | "seq" | "code"> & {
      branch: number;
      kind: CorelogKind;
    },
  ): CorelogEntry {
    this.seq += 1;
    const rec = { ts: nowStamp(), seq: this.seq, code: this.code, ...entry } as CorelogEntry;
    fs.appendFileSync(this.path, `${JSON.stringify(rec)}\n`);
    return rec;
  }
}

export function readCorelog(file: string): CorelogEntry[] {
  let lines: string[];
  try {
    lines = fs.readFileSync(file, "utf8").split("\n");
  } catch {
    return [];
  }
  const out: CorelogEntry[] = [];
  for (const constLine of lines) {
    if (!constLine.trim()) continue;
    try {
      out.push(JSON.parse(constLine) as CorelogEntry);
    } catch {
      /* 跳过坏行 */
    }
  }
  return out;
}

/** 黑板视图：按 Attempt 分组（各 Attempt 内保持时序），交错写入在这里被还原成可读结构。
 * 返回 markdown 片段。flag 进度用索引账本 `2/5 (idx:1,3)` 表示。 */
export function buildAttemptLogView(
  entries: CorelogEntry[],
  opts: { branches?: Map<number, string>; flagLedger?: FlagLedger; maxPerBranch?: number } = {},
): string {
  const maxPer = opts.maxPerBranch ?? 40;
  const byBranch = new Map<number, CorelogEntry[]>();
  for (const e of entries) {
    const list = byBranch.get(e.branch) ?? [];
    list.push(e);
    byBranch.set(e.branch, list);
  }
  const lines: string[] = [];
  const ids = [...byBranch.keys()].sort((a, b) => a - b);
  for (const id of ids) {
    const label = opts.branches?.get(id);
    const head = id === 0 ? "Attempt 0（初始）" : `Attempt ${id}`;
    lines.push(`### ${head}${label ? `（${label}）` : ""}`);
    for (const e of byBranch.get(id)!.slice(-maxPer)) {
      lines.push(formatEntry(e));
    }
    lines.push("");
  }
  return lines.join("\n");
}

function formatEntry(e: CorelogEntry): string {
  const t = e.ts.slice(11); // HH:MM:SS
  if (e.kind === "flag") {
    const idx = e.flag_index ?? "?";
    return `- ${t} [flag] idx=${idx} 进度 ${e.correct_flag_count ?? "?"}/${e.total_flag_count ?? "?"}`;
  }
  if (e.kind === "broadcast") {
    return `- ${t} [广播→${(e.to ?? []).join(",") || "?"}] ${e.note ?? ""}`;
  }
  if (e.kind === "planner") return `- ${t} [planner] ${e.note ?? ""}`;
  if (e.kind === "prune") return `- ${t} [已回收] ${e.note ?? ""}`;
  if (e.kind === "fork") return `- ${t} [fork] ${e.note ?? ""}`;
  if (e.kind === "result") return `- ${t} [结果卡·${e.status ?? "?"}] ${e.note ?? ""}`;
  if (e.kind === "mutation")
    return `- ${t} [共享写·${e.mutation_status ?? "?"}] ${e.action ?? e.note ?? ""}`;
  if (e.kind === "artifact") return `- ${t} [产物] ${e.artifact_path ?? e.note ?? ""}`;
  return `- ${t} ${e.note ?? ""}`;
}

/** flag 索引账本：本题已确认的 flag index 集合（内容会变、索引不变）。 */
export class FlagLedger {
  private got = new Set<number>();
  private total = 0;
  private byBranch = new Map<number, number[]>(); // index -> [拿到它的分支]

  setTotal(total: number): void {
    if (total > 0) this.total = total;
  }
  /** 记账；返回 true = 新 flag（重复 index 不计）。 */
  add(index: number | null, branch: number): boolean {
    if (index === null || index < 0) return false;
    if (this.got.has(index)) return false;
    this.got.add(index);
    const list = this.byBranch.get(branch) ?? [];
    list.push(index);
    this.byBranch.set(branch, list);
    return true;
  }
  count(): number {
    return this.got.size;
  }
  /** 该 branch 新增的 flag 数（duplicate index 不重复计）。 */
  newFlagsFor(branch: number): number {
    return this.byBranch.get(branch)?.length ?? 0;
  }
  /** 平台申报的 flag 总数（未知识 0）。响应缺字段时用它兜底进度口径。 */
  totalCount(): number {
    return this.total;
  }
  /** `2/5 (idx:1,3)` 进度表示（未知总数时给 `2/?`）。 */
  progress(): string {
    const idx = [...this.got].sort((a, b) => a - b).join(",");
    return `${this.got.size}/${this.total || "?"}${idx ? ` (idx:${idx})` : ""}`;
  }
  done(): boolean {
    return this.total > 0 && this.got.size >= this.total;
  }
}

/** 从 submit 响应 / shell 输出里提取 matched_flag_index（null/缺失 → null）。 */
export function extractFlagIndex(text: string): number | null {
  const m = /"matched_flag_index"\s*:\s*(\d+)/.exec(text);
  return m ? Number(m[1]) : null;
}

export interface MutationRecord {
  id: string;
  action: string;
  recovery: string;
  scopeId: number;
  branch: number;
  shared: boolean;
  status: "pending" | "recovered";
}

/** 从 log.jsonl 提取每个 mutation_id 的最新状态；pending = 重启后待恢复的共享写。 */
export function pendingMutations(entries: CorelogEntry[]): MutationRecord[] {
  const latest = new Map<string, MutationRecord>();
  for (const e of entries) {
    if (e.kind !== "mutation") continue;
    const id = String(e.mutation_id ?? "");
    if (!id) continue;
    latest.set(id, {
      id,
      action: String(e.action ?? e.note ?? ""),
      recovery: String(e.recovery ?? ""),
      scopeId: Number(e.scope_id ?? 0),
      branch: Number(e.branch ?? 0),
      shared: e.shared !== false,
      status: e.mutation_status === "recovered" ? "recovered" : "pending",
    });
  }
  return [...latest.values()].filter((m) => m.shared && m.status === "pending");
}
