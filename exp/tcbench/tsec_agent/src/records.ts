/** bench 级持久化记录。
 *
 * 布局（每个 bench 一个目录，唯一键 = token，无时间戳）：
 *     runs/<TOKEN>/
 *         meta.json          本次 bench 元信息 + 每次运行的起止与结果（runs[] 数组）
 *         events.jsonl       run 级事件（llm_retry、platform_poll_error、final_summary…）
 *         tasks.jsonl        题目生命周期（task_start/task_close + 起止时间/耗时/结果/flags）
 *         tasks/<code>/
 *             conversation.jsonl   该题完整 LLM 对话：全量、不裁剪、忠实记录
 *
 * 原则：
 * - 对话忠诚：发给 AI 什么、AI 输出什么，原样落盘，绝不截断。conversation.jsonl 同时是
 *   「分段解题」恢复上下文的唯一可信来源（rebuildMessages 由它重建消息序列）。
 * - 目录挂载即隔离：容器只挂当前 bench 目录。不做 shell 拦截。
 */
import fs from "node:fs";
import path from "node:path";
import type { ChatMessage, ContentPart, ConvRecord } from "./types.js";

/** 记录根目录解析（对应 Python 版 RECORDS_DIR 语义）：
 *  1. 显式 RECORDS_DIR 环境变量优先；
 *  2. 容器内 /work（run.sh 挂载的 runs/<TOKEN>；hosted 形态 entrypoint 自建）——
 *     agent 记录必须落在挂载点，面板/归档才看得到；
 *  3. 宿主机开发时 /work 通常不存在 → 回退当前目录。
 * 踩过的坑：重写时曾把兜底直接写成 cwd——容器里 cwd=/agent（源码挂载点），
 * 记录全写进源码目录，面板对着空的 runs/<TOKEN> 报"没有任务没有监控"。
 * 目录模型：runs/<TOKEN> 是记录根（=容器内 /work）；runs/<TOKEN>/tasks/<code>/
 * 既放 conversation.jsonl 也放该题 shell 工作目录（与 Python 版一致）。 */
export function pickRecordsDir(envVal: string | undefined, workExists: boolean): string {
  if (envVal && isRealDir(envVal)) return envVal;
  if (workExists) return "/work";
  return process.cwd();
}

function isRealDir(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

export let RECORDS_DIR = pickRecordsDir(process.env.RECORDS_DIR, isRealDir("/work"));
export function setRecordsDir(dir: string): void {
  RECORDS_DIR = dir;
}

function now(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function dumpAtomic(file: string, obj: unknown): void {
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2));
  fs.renameSync(tmp, file);
}

/** 单题对话 writer：逐条 append，全量不裁剪。单题单写者，无锁。 */
export class ConversationWriter {
  path: string;
  code: string;
  private seq = 0;

  constructor(file: string, code: string) {
    this.path = file;
    this.code = code;
    // 计数既有条目（同题重启 append 时 seq 继续）
    try {
      const lines = fs.readFileSync(file, "utf8").split("\n");
      this.seq = lines.filter((l) => l.trim()).length;
    } catch {
      this.seq = 0;
    }
    fs.mkdirSync(path.dirname(file), { recursive: true });
  }

  /** kind: system | user | assistant | tool | note | error
   *
   * 时间戳（为甘特图准备）：
   * - startedAt / endedAt：epoch 秒（毫秒精度），该消息/动作的开始与结束；
   *   assistant = LLM 调用起止；tool = 工具执行起止。未传则默认写入时刻。
   * - duration_ms：自动计算（ended - started）。
   * - ts：原可读本地时间（兼容旧消费方）。
   */
  write(kind: ConvRecord["kind"], fields: Record<string, unknown> = {}): void {
    this.seq += 1;
    const nowSec = Date.now() / 1000;
    const started = typeof fields.started_at === "number" ? (fields.started_at as number) : nowSec;
    let ended = typeof fields.ended_at === "number" ? (fields.ended_at as number) : started;
    if (ended < started) ended = started;
    const rec: ConvRecord = {
      ts: now(),
      seq: this.seq,
      code: this.code,
      kind,
      started_at: Math.round(started * 1000) / 1000,
      ended_at: Math.round(ended * 1000) / 1000,
      duration_ms: Math.round((ended - started) * 1000),
      ...(fields as object),
    } as ConvRecord;
    fs.appendFileSync(this.path, `${JSON.stringify(rec)}\n`);
  }
}

/** 从 conversation.jsonl 重建可发送给模型的消息序列（分段解题：暂停→恢复用）。
 *
 * 只重建会话必需字段；thinking/usage 等仅记录字段不回传。尾部裁掉「assistant 已发
 * tool_calls 但结果不齐」的半截对话——OpenAI 协议要求每个 tool_call 后紧跟同 id 的
 * tool 结果，否则 400。返回 [] 表示没有可用上下文。
 */
export function rebuildMessages(file: string): ChatMessage[] {
  const msgs: ChatMessage[] = [];
  const pending = new Set<string>(); // 已发出、尚未收到结果的 tool_call id
  let lines: string[];
  try {
    lines = fs.readFileSync(file, "utf8").split("\n");
  } catch {
    return [];
  }
  const parse = (l: string): ConvRecord | null => {
    try {
      return JSON.parse(l) as ConvRecord;
    } catch {
      return null;
    }
  };
  // 只从「最后一次 system」开始重建：真失败后重启会重新写 system+任务卡（新上下文）。
  let startAt = 0;
  lines.forEach((l, i) => {
    const r = parse(l);
    if (r && r.kind === "system") startAt = i;
  });

  for (const line of lines.slice(startAt)) {
    const r = parse(line);
    if (!r) continue;
    const kind = r.kind;
    if (kind === "system" || kind === "user") {
      const content = r.content;
      if (typeof content === "string" && content) {
        msgs.push({ role: kind, content });
      }
    } else if (kind === "assistant") {
      const toolCalls = (r.tool_calls as ConvRecord["tool_calls"]) ?? [];
      const content = r.output;
      if (!toolCalls?.length && !content) continue;
      const m: ChatMessage = { role: "assistant" };
      if (content) m.content = content;
      if (toolCalls?.length) {
        m.tool_calls = toolCalls.map((t) => ({
          id: t.id,
          type: "function" as const,
          function: { name: t.name, arguments: t.arguments || "{}" },
        }));
        for (const t of toolCalls) pending.add(t.id);
      }
      msgs.push(m);
    } else if (kind === "tool") {
      const tid = r.tool_call_id as string | undefined;
      if (tid === undefined || !pending.has(tid)) continue; // 孤儿结果
      pending.delete(tid);
      const result = r.result;
      let content: string | ContentPart[];
      if (Array.isArray(result)) {
        content = result as ContentPart[]; // 多模态：图片 parts 原样还原
      } else if (typeof result !== "string") {
        content = JSON.stringify(result ?? null);
      } else {
        content = result;
      }
      msgs.push({ role: "tool", tool_call_id: tid, content });
    }
  }

  // 尾部一致性：最后一个带 tool_calls 的 assistant，其调用必须全部有结果
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant" && msgs[i].tool_calls?.length) {
      const ids = new Set(msgs[i].tool_calls!.map((t) => t.id));
      const got = new Set(
        msgs
          .slice(i + 1)
          .filter((m) => m.role === "tool")
          .map((m) => m.tool_call_id),
      );
      let ok = true;
      for (const id of ids) if (!got.has(id)) ok = false;
      if (!ok) msgs.splice(i);
      break;
    }
  }
  if (!msgs.length || msgs[0].role !== "system") return [];
  return msgs;
}

/** 当前 bench 的记录门面。 */
export class RunRecords {
  dir: string;
  metaPath: string;
  eventsPath: string;
  tasksPath: string;
  private meta: Record<string, any>;

  constructor(baseDir?: string, mode = "", model = "") {
    this.dir = baseDir ?? RECORDS_DIR;
    fs.mkdirSync(this.dir, { recursive: true });
    this.metaPath = path.join(this.dir, "meta.json");
    this.eventsPath = path.join(this.dir, "events.jsonl");
    this.tasksPath = path.join(this.dir, "tasks.jsonl");

    // meta：首建或补齐缺省字段；历史 runs[] 保留（同 token 重启 append）
    let meta: Record<string, any> = {};
    try {
      meta = JSON.parse(fs.readFileSync(this.metaPath, "utf8"));
    } catch {
      meta = {};
    }
    if (meta.bench_token_hint === undefined) meta.bench_token_hint = ""; // 明文 token 不落盘
    if (!meta.mode) meta.mode = mode || "";
    if (!meta.model) meta.model = model || "";
    if (!Array.isArray(meta.runs)) meta.runs = [];
    meta.last_opened_at = now();
    dumpAtomic(this.metaPath, meta);
    this.meta = meta;
  }

  /** run 级事件 */
  event(etype: string, data: Record<string, unknown> = {}): void {
    const rec = { ts: now(), type: etype, ...data };
    fs.appendFileSync(this.eventsPath, `${JSON.stringify(rec)}\n`);
  }

  /** 题目生命周期（append-only） */
  taskEvent(code: string, phase: string, fields: Record<string, unknown> = {}): void {
    const rec = { ts: now(), phase, code, ...fields };
    fs.appendFileSync(this.tasksPath, `${JSON.stringify(rec)}\n`);
  }

  /** 运行起止（meta.runs[]） */
  runStart(fields: Record<string, unknown>): void {
    this.meta.runs.push({ started_at: now(), ...fields });
    dumpAtomic(this.metaPath, this.meta);
  }

  runFinish(fields: Record<string, unknown>): void {
    if (this.meta.runs.length) {
      Object.assign(this.meta.runs[this.meta.runs.length - 1], { finished_at: now(), ...fields });
      dumpAtomic(this.metaPath, this.meta);
    }
  }

  /** 对话 writer */
  conversation(code: string): ConversationWriter {
    const file = path.join(this.dir, "tasks", code, "conversation.jsonl");
    return new ConversationWriter(file, code);
  }

  /** 从对话记录重建消息序列（暂停→恢复续跑用），见 rebuildMessages。 */
  loadMessages(code: string): ChatMessage[] {
    const file = path.join(this.dir, "tasks", code, "conversation.jsonl");
    return rebuildMessages(file);
  }
}

/** REC 未初始化时的空实现（smoke/离线场景）。 */
class Noop {
  event(): void {}
  taskEvent(): void {}
  runStart(): void {}
  runFinish(): void {}
  conversation(_code: string): ConversationWriter {
    throw new Error("records not initialized");
  }
  loadMessages(_code: string): ChatMessage[] {
    return [];
  }
}

let REC: RunRecords | Noop | null = null;

export function rec(): RunRecords | Noop {
  if (!REC) {
    if (!NOOP) NOOP = new Noop();
    return NOOP;
  }
  return REC;
}
let NOOP: Noop | null = null;

export function initRecords(mode = "", model = ""): RunRecords {
  REC = new RunRecords(undefined, mode, model);
  return REC;
}

/** 测试用：清空全局 REC。 */
export function resetRecords(): void {
  REC = null;
}
