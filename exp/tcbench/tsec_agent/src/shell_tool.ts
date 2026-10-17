/** shell 工具的 schema、参数容错与路由（dispatch）。 */

import { attachPyLint } from "./py_lint.js";
import { IMAGE_EXT, ShellManager } from "./shell.js";
import { isSubmitCurl } from "./submit_flag.js";
import type { ContentPart } from "./types.js";

export const SHELL_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "shell",
    description:
      "Run a shell command on the local machine. Commands run ASYNCHRONOUSLY: the " +
      "call returns a job_id quickly (commands finishing in the ~1.5s sync window " +
      "return their full output immediately). `command` is the only field you must " +
      "send — action is optional and defaults to run (sending only job_id means " +
      "status). Waiting is edge-triggered: wait returns as soon as the command " +
      "FINISHES, or has accumulated >=2KB of NEW output, or the timeout hits — " +
      "whichever comes first, so it never blocks the full timeout for nothing. " +
      "On run, pass wait_sec to let the tool decide when to wake you; meanwhile go " +
      "do other work instead of idling. Read output incrementally: each response " +
      "carries next_offset, continue with offset=next_offset (or lines for the " +
      "tail). For long scans pass out_file to write straight into your workdir so " +
      "the output survives kills and you can tail it. Jobs are killed at the hard " +
      "limit (default 1800s); kill sooner if stuck — output stays on disk. Max 8 " +
      "concurrent jobs (new run is rejected until you wait/kill). " +
      "Multiple shell calls in ONE assistant message run in parallel (no ordering " +
      "between them — merge dependent steps into a single command). Pass `label` " +
      'to name a job semantically (e.g. "oa-brute"); labels show in list/status ' +
      "and can replace job_id in wait/status/kill when unique. " +
      "Multimodal: to look at an image (screenshot, chart, captcha, photo) call " +
      "action=view with path — the picture itself is returned to you, so you can " +
      "read/understand it directly; if the gateway rejects images it degrades to " +
      "text (metadata) automatically. " +
      "Actions: run (default), wait, wait_any (block until ANY of several jobs " +
      "makes progress), status, kill, list, view. " +
      "Environment already contains BENCHMARK_TOKEN and BENCHMARK_BASE_URL.",
    parameters: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ["run", "wait", "wait_any", "status", "kill", "list", "view"],
          description:
            "optional (default run); wait=block for progress; " +
            "wait_any=block until any of job_ids progresses; " +
            "status=non-blocking peek; kill=stop; list=recent jobs; " +
            "view=show an image to yourself (multimodal)",
        },
        command: { type: "string", description: "shell command (action=run)" },
        label: {
          type: "string",
          description:
            "action=run: short semantic name for this job (shown in list/status/wait " +
            'as label=..., e.g. "oa-brute"), also usable as job_id in ' +
            "wait/status/kill when no other running job shares the name",
        },
        path: {
          type: "string",
          description: "image file path (action=view), absolute or relative to cwd",
        },
        detail: {
          type: "string",
          enum: ["low", "high"],
          description: "action=view: image detail level (default low)",
        },
        job_id: { type: "string", description: "job id (wait/status/kill)" },
        job_ids: {
          type: "array",
          items: { type: "string" },
          description: "job ids to watch together (action=wait_any)",
        },
        cwd: { type: "string", description: "working directory (action=run)" },
        out_file: {
          type: "string",
          description:
            "action=run: write output straight to this path " +
            "(absolute or relative to cwd), e.g. nmap.log in your " +
            "workdir — survives kills, you can tail it later",
        },
        wait_sec: {
          type: "number",
          description:
            "action=run: block up to N sec (max 300) so this single " +
            "call already returns real output when the command " +
            "finishes or streams >=2KB new output. Use it instead " +
            "of calling wait right after run. Try 60 for scans.",
        },
        wait_ms: {
          type: "integer",
          description: "action=run: initial sync window in ms (default 1500, max 30000)",
        },
        timeout_sec: {
          type: "number",
          description:
            "action=run: hard kill after N sec (default 1800, 0=never); " +
            "action=wait/wait_any: max block N sec (wait default 30, " +
            "wait_any default 60, max 300)",
        },
        offset: {
          type: "integer",
          description:
            "wait/status: byte offset to read output from — use the " +
            "next_offset returned earlier to page without re-reading",
        },
        lines: {
          type: "integer",
          description:
            "wait/status/wait_any/kill: number of trailing lines to " + "return (default 200)",
        },
      },
    },
  },
};

// --- 工具参数容错（模型偶发把参数序列化坏，实测样本） ---
//   1) action 混入 XML 参数片段：run"> + <parameter name="command">...（命令在里面）
//   2) action 被写成整条命令：cd /work/...; ffuf ...（没有 command 键）
//   3) action 写成别的工具名：shell（command 正常）
//   4) JSON 值没加引号：job_id: 829f40d3 -> JSON 解析直接失败
export const KNOWN_ACTIONS = ["run", "wait", "wait_any", "status", "kill", "list", "view"];

const PARAM_RE =
  /<parameter\s+name="(\w+)">\s*([\s\S]*?)(?:(?=<\/parameter>)|(?=\s*<parameter\s)|$)/g;
const LEAD_RE = /^\s*(run|wait|status|kill|list)\b/;
const BARE_RE = /(:\s*)([A-Za-z0-9_.\-+/]+)(?=\s*[,}\n])/g;
const LEAD_STOP = "\"'>"; // 坏 action 里动作名后面的残渣字符

/** 像 shell 命令而不是动作名：含空白/管道/重定向/变量等，或以路径开头。 */
function looksLikeCommand(s: string): boolean {
  if (!s) return false;
  return (
    s.split(/\s+/).length > 1 || [...";|&<>$`\n"].some((c) => s.includes(c)) || s.startsWith("/")
  );
}

/** 识别 run"/wait"> 之类被截断的动作名前缀，返回 [动作名, 剩余文本] 或 null。 */
function leadAction(text: string): [string, string] | null {
  const m = LEAD_RE.exec(text);
  if (!m) return null;
  const rest = text.slice(m[0].length);
  if (rest && !(LEAD_STOP.includes(rest[0]) || /\s/.test(rest[0]))) return null; // 例如 running/runner
  return [m[1], rest.replace(/^[\s">]+/, "")];
}

/** 抽出 XML 参数片段（开标签 name="x" + 值 [+ 闭合标签]），值可能一直到字符串结尾。 */
function extractParams(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  PARAM_RE.lastIndex = 0;
  let m: RegExpExecArray | null = PARAM_RE.exec(text);
  while (m !== null) {
    const v = m[2].trim();
    if (v) out[m[1]] = v;
    m = PARAM_RE.exec(text);
  }
  return out;
}

/** 给未加引号的 JSON 值补引号（true/false/null 与合法数字保持原样）。 */
function quoteBareValues(raw: string): string {
  return raw.replace(BARE_RE, (whole, pre: string, tok: string) => {
    if (tok === "true" || tok === "false" || tok === "null") return whole;
    if (!Number.isNaN(Number(tok))) return whole; // 合法数字，不加引号
    return `${pre}"${tok}"`;
  });
}

/** 修复 dict 形式的工具参数（幂等）。返回 [修复后的 args, 修复说明或 null]。
 * 只在能确定意图时改写：拿不准就原样返回，由 dispatch 报正常错误。 */
export function repairToolArgs(
  toolArgs: Record<string, unknown>,
): [Record<string, unknown>, string | null] {
  let action: unknown = toolArgs.action ?? "run";
  if (action === null) action = "run";
  if (typeof action === "string" && KNOWN_ACTIONS.includes(action.trim())) {
    return [toolArgs, null];
  }
  if (typeof action !== "string") {
    if (toolArgs.command) {
      return [{ ...toolArgs, action: "run" }, "action 不是字符串，按 run 执行"];
    }
    return [toolArgs, null];
  }
  const text = action.trim();
  // 1) action 混入 XML 参数片段
  const lead = leadAction(text);
  if (lead) {
    const [name, rest] = lead;
    const params = extractParams(rest);
    const out: Record<string, unknown> = { ...toolArgs, action: name };
    if (name === "run") {
      const cmd = params.command || (toolArgs.command as string) || rest.trim();
      if (cmd) out.command = cmd;
    } else {
      const jid = params.job_id || (toolArgs.job_id as string) || rest.trim();
      if (jid) out.job_id = jid;
    }
    return [out, `action 字段混入 XML 参数片段，已还原为 ${JSON.stringify(name)}`];
  }
  // 2) action 非法但 command 正常 -> 按 run 执行
  if (toolArgs.command) {
    return [{ ...toolArgs, action: "run" }, "action 非法但存在 command，已按 run 执行"];
  }
  // 3) action 整段是命令 -> run + command=action（其余参数保留）
  if (looksLikeCommand(text)) {
    return [
      { ...toolArgs, action: "run", command: action },
      "action 字段被写成整条命令，已按 run 执行",
    ];
  }
  // 4) 拿不准（如 shell 且无 command）-> 原样返回，报 unknown action
  return [toolArgs, null];
}

/** 解析模型给出的 shell 工具参数 JSON，容错修复已知序列化损坏。
 * 返回 [args, 修复说明或 null]；无法解析返回 [null, 说明]。 */
export function parseToolArguments(raw: string): [Record<string, unknown> | null, string | null] {
  const text = (raw || "").trim();
  if (!text) return [null, "工具参数为空"];
  let data: unknown = null;
  try {
    data = JSON.parse(text);
  } catch {
    data = null;
  }
  if (data && typeof data === "object" && !Array.isArray(data)) {
    return repairToolArgs(data as Record<string, unknown>);
  }
  if (typeof data === "string" && looksLikeCommand(data)) {
    return [{ action: "run", command: data }, "参数是裸命令字符串，已按 run 执行"];
  }
  // JSON 解析失败 -> 依次尝试：补引号 / 重建 XML 片段 / 整段当命令
  try {
    const fixed = JSON.parse(quoteBareValues(text));
    if (fixed && typeof fixed === "object" && !Array.isArray(fixed)) {
      const [args, note] = repairToolArgs(fixed as Record<string, unknown>);
      const parts = ["已为未加引号的 JSON 值补引号", note].filter(Boolean);
      return [args, parts.join("; ")];
    }
  } catch {
    /* fallthrough */
  }
  const params = extractParams(text);
  if (Object.keys(params).length) {
    const lead = leadAction(text);
    const name =
      (params.action as string) || (lead ? lead[0] : null) || (params.command ? "run" : null);
    if (name && KNOWN_ACTIONS.includes(name)) {
      delete params.action;
      const [args, note] = repairToolArgs({ action: name, ...params });
      const parts = ["参数被序列化成 XML 片段，已重建", note].filter(Boolean);
      return [args, parts.join("; ")];
    }
  }
  if (looksLikeCommand(text)) {
    return [{ action: "run", command: text }, "参数不是 JSON，已整段按命令执行"];
  }
  return [null, `JSON 参数解析失败且无法修复：${raw.slice(0, 160)}`];
}

export const SHELL = new ShellManager();

function toInt(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Math.trunc(Number(v));
  return Number.isNaN(n) ? null : n;
}
function toFloat(v: unknown, def: number | null): number | null {
  if (v === null || v === undefined || v === "") return def;
  const n = Number(v);
  return Number.isNaN(n) ? def : n;
}

export type ShellResult =
  | string
  | ContentPart[]
  | Array<{ type: string; text?: string; image_url?: { url: string; detail: string } }>;

/** 把模型给出的 tool arguments 路由到 ShellManager，返回给模型的文本/内容 parts。 */
export async function dispatchShell(toolArgs: Record<string, unknown>): Promise<ShellResult> {
  let args = toolArgs ?? {};
  try {
    args = repairToolArgs(args)[0];
  } catch {
    // 修复失败按原样走，由下面的分支给出正常错误
  }
  // action 可省略：只有 job_id 没 command → 当作 status（省得模型补 action）
  if (!("action" in args) && !args.command && args.job_id) {
    args = { ...args, action: "status" };
  }
  // 与 Python tool_args.get("action","run") 一致：键存在且值为 null 时保持 null（由下面报 unknown action）
  const action = "action" in args ? (args.action as string) : "run";
  try {
    if (action === "run") {
      const command = args.command;
      if (!command) return "error: `command` is required for action=run";
      // 防代码爆破：提交只走 submit_flag toolcall——shell 里 curl submit（脚本化/
      // 批量穷举的通道）直接拒绝
      if (isSubmitCurl(String(command))) {
        return (
          "error: 提交必须用 submit_flag toolcall（shell 禁止 curl submit——" +
          '防止脚本化批量提交）。调用 submit_flag({flag: "<从目标读出的原文>"})。'
        );
      }
      const out = await SHELL.run({
        command: String(command),
        label: (args.label as string) ?? null,
        cwd: (args.cwd as string) ?? null,
        timeout: toFloat(args.timeout_sec, null), // 缺省→配置默认 1800s
        wait_ms: toInt(args.wait_ms),
        wait_sec: toFloat(args.wait_sec, null),
        out_file: (args.out_file as string) ?? null,
      });
      // py 脚本自动 lint（heredoc 形态）：[py-lint] 附结果尾，模型下轮即见（见 py_lint.ts）
      return attachPyLint(String(command), out);
    }
    if (action === "wait") {
      const jobId = args.job_id;
      if (!jobId) return "error: `job_id` is required for action=wait";
      return await SHELL.wait(
        String(jobId),
        toFloat(args.timeout_sec, 30.0) as number,
        toInt(args.offset),
        toInt(args.lines),
      );
    }
    if (action === "wait_any") {
      const ids = args.job_ids ?? args.job_id;
      return await SHELL.waitAny(ids, toFloat(args.timeout_sec, 60.0) as number, toInt(args.lines));
    }
    if (action === "status") {
      const jobId = args.job_id;
      if (!jobId) return "error: `job_id` is required for action=status";
      return SHELL.status(String(jobId), toInt(args.offset), toInt(args.lines));
    }
    if (action === "kill") {
      const jobId = args.job_id;
      if (!jobId) return "error: `job_id` is required for action=kill";
      return await SHELL.kill(String(jobId), toInt(args.lines));
    }
    if (action === "view" || action === "image") {
      const target = args.path ?? args.file ?? args.command;
      if (!target) return "error: `path` is required for action=view";
      return SHELL.view(String(target), (args.detail as string) ?? null);
    }
    if (action === "list") return SHELL.list();
    return (
      `error: unknown action ${JSON.stringify(action)}; 请用 ` +
      `{"action":"run","command":"<命令>"} 或 ` +
      `{"action":"wait","job_id":"<job>"}（可选 wait_any/status/kill/list）`
    );
  } catch (e) {
    // 兜底，绝不让工具异常打断主循环
    return `error: shell tool failed: ${e instanceof Error ? e.name : "Error"}: ${e instanceof Error ? e.message : e}`;
  }
}

export { IMAGE_EXT };
