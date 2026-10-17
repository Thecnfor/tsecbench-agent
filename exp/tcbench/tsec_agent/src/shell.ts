/** 异步（background）shell 工具。
 *
 * 设计要点（与 Python 版一致）：
 * - 所有命令默认在后台启动，立即返回 job_id；模型按需通过 wait/status 拉取输出。
 * - start 时允许一个很小的同步等待窗口（默认 1.5s），命令若在窗口内结束则直接带回完整输出。
 * - 每个 job 的 stdout/stderr 合并写入临时文件，读取时做大小截断。
 * - 支持 kill；进程超时自动杀死（整进程组）。
 * - **工具参数容错**：模型偶发把参数序列化坏 → parseToolArguments 自动修复并执行，
 *   附修复说明落 events.jsonl（tool_arg_repaired），避免整轮作废。
 * - 每题独立 cwd 用 AsyncLocalStorage（Python 版是 thread-local；Node 并发是 async，
 *   用 ALS 等价隔离，避免并行解题互相串目录）。
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { CFG } from "./config.js";

// async 上下文"当前任务工作目录"：并行解题时每个任务有专属 cwd，互不干扰
const cwdStore = new AsyncLocalStorage<string>();

export function setTaskCwd(p: string): void {
  cwdStore.enterWith(p || "");
}
export function getTaskCwd(): string | null {
  const v = cwdStore.getStore();
  return v ? v : null;
}

export class Job {
  jobId: string;
  /** 模型给 job 起的语义名（如 "oa-brute"）：list/status 显示，wait/status/kill 也可按名指认。 */
  label: string;
  command: string;
  cwd: string;
  child: ChildProcess;
  outPath: string;
  startedAt: number;
  timeout: number | null;
  finished = false;
  exitCode: number | null = null;
  killed = false;
  private watcher: (() => void) | null = null;

  constructor(init: {
    jobId: string;
    label?: string | null;
    command: string;
    cwd: string;
    child: ChildProcess;
    outPath: string;
    timeout: number | null;
  }) {
    this.jobId = init.jobId;
    this.label = init.label ?? "";
    this.command = init.command;
    this.cwd = init.cwd;
    this.child = init.child;
    this.outPath = init.outPath;
    this.startedAt = Date.now() / 1000;
    this.timeout = init.timeout;
    this.ensureWatched();
  }

  ensureWatched(): void {
    if (this.watcher) return;
    this.watcher = () => {};
    const deadline = this.timeout && this.timeout > 0 ? this.startedAt + this.timeout : null;
    const timer =
      deadline !== null
        ? setInterval(() => {
            if (Date.now() / 1000 > deadline && !this.finished) {
              this.killed = true;
              this.kill("timeout");
            }
          }, 200)
        : null;
    this.child.on("exit", (code) => {
      if (timer) clearInterval(timer);
      this.exitCode = code;
      this.finished = true;
    });
    this.child.on("error", () => {
      if (timer) clearInterval(timer);
      this.exitCode = -1;
      this.finished = true;
    });
  }

  kill(reason = "user"): void {
    if (this.finished) return;
    this.killed = true;
    try {
      // 杀整个进程组，避免 curl/python 子进程残留（detached → 独立 pgid）
      if (this.child.pid) process.kill(-this.child.pid, "SIGKILL");
    } catch {
      try {
        this.child.kill("SIGKILL");
      } catch {
        /* ignore */
      }
    }
    void reason;
  }

  output(maxBytes = CFG.shell_max_output): string {
    let data: Buffer;
    try {
      data = fs.readFileSync(this.outPath);
    } catch {
      return "";
    }
    if (data.length <= maxBytes) return data.toString("utf8");
    const headLen = Math.floor(maxBytes / 2);
    const head = data.subarray(0, headLen);
    const tail = data.subarray(data.length - (maxBytes - headLen - 200));
    const omitted = data.length - head.length - tail.length;
    return (
      head.toString("utf8") +
      `\n...[output truncated, ${omitted} bytes omitted]...\n` +
      tail.toString("utf8")
    );
  }

  /** 当前输出文件字节数（用于增量/边沿等待）。 */
  size(): number {
    try {
      return fs.statSync(this.outPath).size;
    } catch {
      return 0;
    }
  }
}

/** 从文件头解析图片尺寸（PNG/JPEG/GIF/WebP/BMP），零依赖。返回 [w, h] 或 [null, null]。 */
export function imageSize(data: Buffer): [number | null, number | null] {
  try {
    if (
      data.length >= 24 &&
      data.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) &&
      data.subarray(12, 16).toString("latin1") === "IHDR"
    ) {
      return [data.readUInt32BE(16), data.readUInt32BE(20)];
    }
    const sig6 = data.subarray(0, 6).toString("latin1");
    if (sig6 === "GIF87a" || sig6 === "GIF89a") {
      return [data.readUInt16LE(6), data.readUInt16LE(8)];
    }
    if (
      data.subarray(0, 4).toString("latin1") === "RIFF" &&
      data.subarray(8, 12).toString("latin1") === "WEBP"
    ) {
      const chunk = data.subarray(12, 16).toString("latin1");
      if (chunk === "VP8X") {
        return [1 + (data.readUIntLE(24, 3) & 0xffffff), 1 + (data.readUIntLE(27, 3) & 0xffffff)];
      }
      if (chunk === "VP8 ") {
        return [data.readUInt16LE(26) & 0x3fff, data.readUInt16LE(28) & 0x3fff];
      }
      if (chunk === "VP8L") {
        const b0 = data[21],
          b1 = data[22],
          b2 = data[23],
          b3 = data[24];
        return [
          1 + (((b1 & 0x3f) << 8) | b0),
          1 + (((b3 & 0x0f) << 10) | (b2 << 2) | ((b1 & 0xc0) >> 6)),
        ];
      }
    }
    if (data.subarray(0, 2).toString("latin1") === "BM" && data.length >= 26) {
      return [data.readUInt32LE(18), Math.abs(data.readInt32LE(22))];
    }
    if (data[0] === 0xff && data[1] === 0xd8) {
      // JPEG：扫段找 SOF
      let i = 2;
      while (i + 9 < data.length) {
        if (data[i] !== 0xff) {
          i++;
          continue;
        }
        const marker = data[i + 1];
        if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) {
          i += 2;
          continue;
        }
        const seglen = data.readUInt16BE(i + 2);
        if (
          marker >= 0xc0 &&
          marker <= 0xcf &&
          marker !== 0xc4 &&
          marker !== 0xc8 &&
          marker !== 0xcc
        ) {
          return [data.readUInt16BE(i + 7), data.readUInt16BE(i + 5)];
        }
        i += 2 + seglen;
      }
    }
  } catch {
    /* fallthrough */
  }
  return [null, null];
}

/** 超过大小上限时压图：调用系统 python3 的 PIL（容器里已装 python3-pil）。 */
function shrink(src: string, capBytes: number): string | null {
  const script = [
    "import sys, os",
    "from PIL import Image",
    "src, dst, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])",
    "im = Image.open(src)",
    "im.thumbnail((1536, 1536))",
    "if im.mode not in ('RGB', 'L'):",
    "    im = im.convert('RGB')",
    "q = 85",
    "while True:",
    "    im.save(dst, 'JPEG', quality=q, optimize=True)",
    "    if os.path.getsize(dst) <= cap or q <= 30:",
    "        break",
    "    q -= 15",
  ].join("\n");
  const dst = path.join(os.tmpdir(), `view_${crypto.randomBytes(6).toString("hex")}.jpg`);
  try {
    const r = spawnSync("python3", ["-c", script, src, dst, String(capBytes)], {
      timeout: 25_000,
    });
    if (r.status === 0 && fs.existsSync(dst) && fs.statSync(dst).size <= capBytes) return dst;
  } catch {
    /* fallthrough */
  }
  try {
    fs.unlinkSync(dst);
  } catch {
    /* ignore */
  }
  return null;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export class ShellManager {
  private jobs = new Map<string, Job>();
  private cwd = process.cwd();

  // ---- 动作：启动（后台） ----
  async run(opts: {
    command: string;
    label?: string | null;
    cwd?: string | null;
    timeout?: number | null;
    wait_ms?: number | null;
    wait_sec?: number | null;
    out_file?: string | null;
  }): Promise<string> {
    const { command } = opts;
    // 语义名：单行、截 48 字符——只用于识别/显示，不参与寻址歧义之外的逻辑
    const label = String(opts.label ?? "")
      .replace(/[\r\n\t]+/g, " ")
      .trim()
      .slice(0, 48);
    let cwd = opts.cwd || getTaskCwd() || this.cwd;
    if (!isDir(cwd)) {
      try {
        fs.mkdirSync(cwd, { recursive: true });
      } catch {
        cwd = this.cwd;
      }
    }
    let waitMs = opts.wait_ms;
    if (waitMs === undefined || waitMs === null) waitMs = CFG.shell_default_wait_ms;
    waitMs = Math.max(0, Math.min(Math.trunc(waitMs), 30_000));

    // job 上限：满了拒绝新命令，避免后台 job 堆成僵尸
    const active = [...this.jobs.values()].filter((j) => !j.finished);
    if (active.length >= CFG.shell_max_jobs) {
      const ids = active
        .map((j) => `${j.jobId}(${Math.trunc(Date.now() / 1000 - j.startedAt)}s)`)
        .join(", ");
      return (
        `error: 后台 job 已满（${active.length}/${CFG.shell_max_jobs}）：${ids}\n` +
        `先 action=list 看进度，用 wait/wait_any 收结果，或 kill 掉不需要的 job，再启动新命令。`
      );
    }
    // 硬上限：到点整组 kill（输出已落盘，杀掉不丢数据）；未传用配置默认 1800s；显式传 0/负数 = 永不杀
    let timeout = opts.timeout;
    if (timeout === undefined || timeout === null) timeout = CFG.shell_hard_sec;

    const jobId = crypto.randomBytes(4).toString("hex");
    let outPath: string;
    if (opts.out_file) {
      const p = path.isAbsolute(opts.out_file) ? opts.out_file : path.join(cwd, opts.out_file);
      try {
        fs.mkdirSync(path.dirname(p) || cwd, { recursive: true });
        outPath = p; // 直接写到 workdir，模型可随时 tail
      } catch {
        outPath = newLog(jobId);
      }
    } else {
      outPath = newLog(jobId);
    }

    let child: ChildProcess;
    try {
      const fd = fs.openSync(outPath, "a");
      child = spawn(command, {
        shell: true,
        cwd,
        detached: true, // 新建会话/进程组，便于整组 kill
        stdio: ["ignore", fd, fd],
        env: { ...process.env, ...CFG.extra_env },
      });
      child.on("error", () => {
        /* exit handler 置 finished */
      });
    } catch (e) {
      return `error: failed to start command: ${e instanceof Error ? e.message : e}`;
    }

    const job = new Job({ jobId, label, command, cwd, child, outPath, timeout });
    this.jobs.set(jobId, job);

    // 小窗口同步等待：快速命令直接带完整输出返回，减少一轮交互
    const deadline = Date.now() + waitMs;
    while (Date.now() < deadline && !job.finished) await sleep(50);
    // wait_sec：工具替模型决定「什么时候回来叫它」——命令结束 / 攒够一批新输出（>2KB）/ 到点
    if (opts.wait_sec && !job.finished) {
      const limit = Date.now() + Math.max(0, Math.min(Number(opts.wait_sec), 300)) * 1000;
      const base = job.size();
      while (!job.finished && Date.now() < limit) {
        if (job.size() > base + 2048) break;
        await sleep(100);
      }
    }
    const payload = this.statusPayload(job);
    const others = [...this.jobs.values()]
      .filter((j) => !j.finished && j.jobId !== job.jobId)
      .map((j) => (j.label ? `${j.jobId}(${j.label})` : j.jobId));
    const extra: string[] = [];
    if (others.length) {
      extra.push(
        `后台还有 ${others.length} 个 job 在跑 (${others.join(", ")})：` +
          `发完这条就去做别的事，要同时盯多个用 action=wait_any`,
      );
    } else {
      extra.push(
        "命令已在后台：先去做别的事，需要时用 wait/status 读结果（支持 offset 分页）；长命令建议 out_file 落盘",
      );
    }
    return `${payload}\n${extra.join("\n")}`;
  }

  // ---- 动作：等待（边沿触发：结束 / 攒够新输出 / 超时，先到先返回） ----
  async wait(
    jobId: string,
    timeout = 30.0,
    offset?: number | null,
    lines?: number | null,
  ): Promise<string> {
    const job = this.get(jobId);
    if (!job) return this.unknownJobMsg(jobId);
    const deadline = Date.now() + Math.max(0, Math.min(timeout, 300)) * 1000;
    const base = offset !== null && offset !== undefined ? Math.trunc(offset) : job.size();
    while (!job.finished && Date.now() < deadline) {
      if (job.size() > base + 2048) break; // 攒够一批新输出 → 不必等到底
      await sleep(100);
    }
    return this.statusPayload(job, offset, lines);
  }

  // ---- 动作：同时等多个 job，任一有动静就返回 ----
  async waitAny(jobIds: unknown, timeout = 60.0, lines?: number | null): Promise<string> {
    let ids: string[] =
      typeof jobIds === "string" ? [jobIds] : Array.isArray(jobIds) ? (jobIds as string[]) : [];
    ids = ids.filter(Boolean);
    if (!ids.length) return "error: `job_ids` is required for action=wait_any (array of job ids)";
    const pairs: Array<[string, Job]> = [];
    const missing: string[] = [];
    for (const id of ids) {
      const jb = this.get(id);
      if (jb) pairs.push([id, jb]);
      else missing.push(id);
    }
    if (!pairs.length) return `error: unknown job_ids ${JSON.stringify(ids)}`;
    const base = new Map(pairs.map(([id, jb]) => [id, jb.size()]));
    const deadline = Date.now() + Math.max(0, Math.min(Number(timeout), 300)) * 1000;
    while (Date.now() < deadline) {
      if (pairs.some(([, jb]) => jb.finished)) break;
      if (pairs.some(([id, jb]) => jb.size() > (base.get(id) ?? 0) + 512)) break;
      await sleep(100);
    }
    const parts = pairs.map(([, jb]) => this.statusPayload(jb, undefined, lines));
    let tail = "\n未结束的 job 继续在后台跑；继续盯可再用 wait_any 或单独 wait。";
    if (missing.length) tail += `\n(已忽略未知 job_id: ${missing.join(", ")})`;
    return `任一 job 有新输出或结束即返回\n${parts.join("\n---\n")}${tail}`;
  }

  // ---- 动作：查看状态/输出（不阻塞，支持分页） ----
  status(jobId: string, offset?: number | null, lines?: number | null): string {
    const job = this.get(jobId);
    if (!job) return this.unknownJobMsg(jobId);
    return this.statusPayload(job, offset, lines);
  }

  // ---- 动作：列出所有 job ----
  list(): string {
    const jobs = [...this.jobs.values()];
    if (!jobs.length) return "no jobs";
    const lines: string[] = [];
    for (const j of jobs.slice(-20)) {
      const state = j.finished
        ? `done rc=${j.exitCode}`
        : `RUN ${Math.trunc(Date.now() / 1000 - j.startedAt)}s/${Math.trunc(j.timeout ?? 0)}s`;
      const mark = j.finished ? " " : "*";
      const label = j.label ? ` label=${j.label}` : "";
      lines.push(
        `${mark}${j.jobId}${label}  ${state.padEnd(16)} out=${String(j.size()).padStart(8)}B  ` +
          `${j.command.slice(0, 72)}`,
      );
    }
    const nRun = jobs.filter((j) => !j.finished).length;
    return (
      lines.join("\n") +
      `\n(* = 在跑，${nRun}/${CFG.shell_max_jobs}；wait/status 读输出（offset 分页），wait_any 同时等多个，kill 停止)`
    );
  }

  // ---- 动作：杀掉 ----
  async kill(jobId: string, lines?: number | null): Promise<string> {
    const job = this.get(jobId);
    if (!job) return this.unknownJobMsg(jobId);
    job.kill("user");
    await sleep(200);
    const payload = this.statusPayload(job, undefined, lines ?? 40);
    return (
      payload +
      `\nnote: 已终止；完整输出已落盘 ${job.outPath}（可 run "tail -n 200 ${job.outPath}" 继续读）`
    );
  }

  // ---- 动作：读图（多模态，把图片本体交给模型看） ----
  /** 返回 [text, image_url] 两段；网关不支持图像时自动降级为纯文本（OCR+元数据）。 */
  view(
    p: string,
    detail?: string | null,
  ): string | Array<{ type: string; text?: string; image_url?: { url: string; detail: string } }> {
    const d = detail === "low" || detail === "high" ? detail : "low";
    const full = path.isAbsolute(p) ? p : path.join(getTaskCwd() || this.cwd, p);
    if (!isFile(full)) return `error: 图片不存在或不是普通文件: ${full}`;
    const ext = path.extname(full).toLowerCase();
    if (!(ext in IMAGE_EXT)) {
      return (
        "error: 不支持的图片格式 " +
        (ext || "(无扩展名)") +
        "；支持 " +
        Object.keys(IMAGE_EXT).sort().join(", ") +
        "。先用 file 确认类型。"
      );
    }
    let data: Buffer;
    try {
      data = fs.readFileSync(full);
    } catch (e) {
      return `error: 读取图片失败: ${e instanceof Error ? e.message : e}`;
    }
    if (!data.length) return `error: 空文件: ${full}`;
    let [w, h] = imageSize(data);
    let dims = w ? `${w}x${h}` : "尺寸未知";
    let mime = IMAGE_EXT[ext];
    const cap = Math.max(64, CFG.vision_max_kb) * 1024;
    let note = "";
    let file = full;
    if (data.length > cap) {
      const shrunk = shrink(full, cap);
      if (!shrunk) {
        return (
          `error: ${full} 有 ${Math.trunc(data.length / 1024)}KB，超过上限 ` +
          `${CFG.vision_max_kb}KB 且压缩失败。请先自行缩小/裁剪图片再 view。`
        );
      }
      file = shrunk;
      data = fs.readFileSync(shrunk);
      mime = "image/jpeg";
      const [w2, h2] = imageSize(data);
      if (w2) {
        w = w2;
        dims = `${w2}x${h2}`;
      }
      note = `（原图超限，已自动压缩到 ≤${CFG.vision_max_kb}KB）`;
    }
    const header = `[image] ${file}  ${dims}  ${data.length}B  ${mime}  detail=${d} ${note}`;
    if (CFG.vision === "off" || CFG.vision_ok === false) {
      return (
        header +
        "\n当前为文本模式（AGENT_VISION=off 或网关已拒图），没有图片本体。" +
        "请改用 shell 侧分析：file/xxd/strings/exiftool、系统 python3 的 PIL（尺寸/直方图/像素采样），或先裁剪/缩放关键区域再 view。" +
        "验证码类图片可 shell 跑 `tesseract <图> stdout -l eng` 取文字。"
      );
    }
    const url = `data:${mime};base64,${data.toString("base64")}`;
    return [
      {
        type: "text",
        text:
          header +
          "\n下面是图片本体，直接看图作答；需要更细可传 detail=high，或先用 shell 裁剪/缩放。",
      },
      { type: "image_url", image_url: { url, detail: d } },
    ];
  }

  private get(key: string): Job | undefined {
    const byId = this.jobs.get(key);
    if (byId) return byId;
    // label 兜底寻址：唯一同名 job 可直接用语义名指认（重名 → undefined，见 unknownJobMsg）
    const hits = [...this.jobs.values()].filter((j) => j.label !== "" && j.label === key);
    return hits.length === 1 ? hits[0] : undefined;
  }

  /** 找不到 job 时的错误文案：label 重名要提示用 job_id，纯未知给通用文案。 */
  private unknownJobMsg(key: string): string {
    const hits = [...this.jobs.values()].filter((j) => j.label !== "" && j.label === key);
    if (hits.length > 1) {
      return (
        `error: label ${JSON.stringify(key)} 对应多个 job (${hits.map((j) => j.jobId).join(", ")})：` +
        `请改用 job_id，或给 job 换个不重名的 label`
      );
    }
    return `error: unknown job_id/label ${key}`;
  }

  /** 读输出分页：offset 给定 → 从该字节续读；否则 → 末尾 N 行（默认 200）。 */
  private readPage(
    job: Job,
    offset?: number | null,
    lines?: number | null,
  ): [string, number, string] {
    try {
      const total = job.size();
      const data = fs.readFileSync(job.outPath);
      if (offset === null || offset === undefined) {
        const want = Math.max(1, Math.min(Math.trunc(lines ?? 200), 2000));
        // 字节窗必须受 shell_max_output 约束（曾硬编码 want*160 → lines=200 时
        // 32KB 单页绕过截断，arena 实测 32711 字符单条进上下文）；
        // offset 分页分支本就走 CFG，此处对齐——行语义保留（窗内仍按 lines 取尾）。
        const win = Math.min(Math.max(want * 160, 8192), CFG.shell_max_output);
        const start = Math.max(0, total - win);
        const chunk = data.subarray(start);
        const txt = chunk.toString("utf8").split("\n");
        const page = txt.slice(Math.max(0, txt.length - want)).join("\n");
        const omitted = total - chunk.length;
        const more = omitted ? `（更早还有 ${omitted} 字节，offset=0 可从头分页读）` : "";
        return [page, total, more];
      }
      const off = Math.max(0, Math.trunc(offset));
      const chunk = data.subarray(off, off + CFG.shell_max_output);
      const page = chunk.toString("utf8");
      const nxt = off + chunk.length;
      const more = nxt < total ? `（后面还有 ${total - nxt} 字节，offset=${nxt} 续读）` : "";
      return [page, nxt, more];
    } catch {
      return ["", 0, ""];
    }
  }

  private statusPayload(job: Job, offset?: number | null, lines?: number | null): string {
    let state: string;
    if (job.finished) {
      state = `finished exit_code=${job.exitCode}`;
      if (job.killed && job.exitCode !== 0 && job.exitCode !== null) {
        state += " (killed: timeout or manual kill)";
      }
    } else {
      const elapsed = Date.now() / 1000 - job.startedAt;
      state = `running elapsed=${elapsed.toFixed(1)}s (hard limit ${Math.trunc(job.timeout ?? 0)}s)`;
    }
    const [page, nxt, more] = this.readPage(job, offset, lines);
    // cmd 防撑爆：header 里的命令全文是提示语（原命令在 assistant.tool_calls 有完整记录），
    // 超长脚本（数百行 for 循环实测 4KB+）会把单条返回顶穿截断预算 → 截 600。
    const cmd =
      job.command.length > 600
        ? `${job.command.slice(0, 600)} …[命令超长已截，全文见 tool_calls]`
        : job.command;
    const label = job.label ? ` label=${job.label}` : "";
    const header =
      `[job ${job.jobId}]${label} ${state} out=${job.size()}B\n` +
      `log: ${job.outPath}\ncmd: ${cmd}`;
    if (!page) return `${header}\noutput: (empty)`;
    const pos = offset !== null && offset !== undefined ? `from_offset=${offset}` : "tail";
    const tail = `\nnext_offset=${nxt}${more ? ` ${more}` : ""}`;
    return `${header}\noutput[${pos}]:\n${page}${tail}`;
  }
}

function isDir(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}
function isFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}
function newLog(jobId: string): string {
  const f = path.join(os.tmpdir(), `shell_${jobId}_${crypto.randomBytes(4).toString("hex")}.log`);
  fs.closeSync(fs.openSync(f, "a"));
  return f;
}

export const IMAGE_EXT: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".bmp": "image/bmp",
  ".ico": "image/x-icon",
  ".tif": "image/tiff",
  ".tiff": "image/tiff",
};
