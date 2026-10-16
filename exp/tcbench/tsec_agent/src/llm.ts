/** OpenAI 兼容 chat client（fetch 实现，零第三方依赖）+ key 候选发现 + 401 热切换。
 *
 * 对应 Python 版 agent.py 的 _client_with_key / discover_gateway_key / _try_reauth / call_llm。
 * 网关是 OpenAI 兼容协议，直接 fetch 打 /chat/completions 即可；
 * 匿名（无 key）= 不带 Authorization 头（Python 版要靠 httpx 钩子剥头，这里天然支持）。
 */
import { CFG } from "./config.js";
import { rec } from "./records.js";
import { SHELL_TOOL_SCHEMA } from "./shell_tool.js";
import type { ChatMessage, ChatResponse, LlmClient } from "./types.js";

/** HTTP/协议层错误：携带 status（401/429/5xx…），供重试与热切换判定。 */
export class LlmHttpError extends Error {
  status: number | null;
  body: string;
  constructor(message: string, status: number | null, body = "") {
    super(message);
    this.name = "LlmHttpError";
    this.status = status;
    this.body = body;
  }
}

export class FetchLlmClient implements LlmClient {
  baseUrl: string;
  apiKey: string;
  timeout: number;

  constructor(baseUrl: string, apiKey: string, timeout?: number) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    this.timeout = timeout ?? CFG.request_timeout;
  }

  async create(opts: {
    model: string;
    messages: ChatMessage[];
    tools?: unknown[];
    tool_choice?: string;
    temperature?: number;
    max_tokens?: number;
    signal?: AbortSignal;
  }): Promise<ChatResponse> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.apiKey) headers.Authorization = `Bearer ${this.apiKey}`; // 匿名 = 不带头
    const body: Record<string, unknown> = {
      model: opts.model,
      messages: opts.messages,
      temperature: opts.temperature ?? 0.2,
    };
    if (opts.tools) {
      body.tools = opts.tools;
      body.tool_choice = opts.tool_choice ?? "auto";
    }
    if (opts.max_tokens !== undefined) body.max_tokens = opts.max_tokens;

    const timeoutSig = AbortSignal.timeout(this.timeout * 1000);
    // AbortSignal.any 需 Node ≥20.3；Kali apt 的 nodejs 可能更旧 → 降级为只用超时信号
    const signal =
      opts.signal && typeof (AbortSignal as any).any === "function"
        ? (AbortSignal as any).any([timeoutSig, opts.signal])
        : timeoutSig;
    let resp: Response;
    try {
      resp = await fetch(`${this.baseUrl}/chat/completions`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal,
      });
    } catch (e) {
      // 网络类错误（超时/连接/DNS）：转成带 status=null 的错误，走重试
      const msg = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
      throw new LlmHttpError(`connection error: ${msg}`, null);
    }
    const text = await resp.text();
    if (!resp.ok) {
      let parsed: any = null;
      try {
        parsed = JSON.parse(text);
      } catch {
        /* raw body */
      }
      const apiMsg = parsed?.error?.message ?? text;
      throw new LlmHttpError(`HTTP ${resp.status}: ${apiMsg}`, resp.status, text);
    }
    return JSON.parse(text) as ChatResponse;
  }
}

// key 候选发现只完整跑一次；401 热切换也只做一次，避免坏 key 死循环
export let REAUTH_TRIED = false;
export let DISCOVERY_DONE = false;
/** 全局当前 client：任一处 401 热切换后，其他调用也用新 client。 */
export let CURRENT_CLIENT: LlmClient | null = null;

export function resetAuthState(): void {
  REAUTH_TRIED = false;
  DISCOVERY_DONE = false;
  CURRENT_CLIENT = null;
}
export function setReauthTried(v: boolean): void {
  REAUTH_TRIED = v;
}
export function setDiscoveryDone(v: boolean): void {
  DISCOVERY_DONE = v;
}
export function setCurrentClient(c: LlmClient | null): void {
  CURRENT_CLIENT = c;
}

/** key 掩码：只显示尾 4 位，绝不输出完整 key（含 BENCHMARK_TOKEN 这类平台凭据）。 */
export function keyMask(k: string): string {
  return k ? `****${k.slice(-4)}` : "(none)";
}

/** 按 key 构造 client；key=="" 表示匿名（请求不带 Authorization 头）。 */
export function clientWithKey(key: string, timeout?: number): LlmClient {
  return new FetchLlmClient(CFG.llm_base_url, key, timeout);
}

export function makeClient(): LlmClient {
  if (!CFG.llm_api_key) {
    if (CFG.mode === "hosted") {
      // 托管下 key 可以为空：匿名本身就是候选之一，由 discover_gateway_key 探测
      log("no api key configured; starting with anonymous client (hosted)");
      return clientWithKey("");
    }
    // 本地模式无 key：公网 API 必然 401，干净报错退出（退出码 2 = 网关不可达）
    log(
      `FATAL: model gateway credentials missing (mode=${CFG.mode}, ` +
        `base_url=${CFG.llm_base_url}) — 本地模式需 .env/环境变量 DEEPSEEK_API_KEY；` +
        `若在托管环境看到此错，说明模式误判为 local，请检查 AGENT_MODE/hosted 网关探测`,
    );
    process.exitCode = 2;
    throw new ExitSignal(2);
  }
  return clientWithKey(CFG.llm_api_key);
}

/** 干净退出（不打栈）。 */
export class ExitSignal extends Error {
  code: number;
  constructor(code: number) {
    super(`exit ${code}`);
    this.name = "ExitSignal";
    this.code = code;
  }
}

// log 在 agent.ts 定义；这里用惰性引用避免循环依赖
let _log: (msg: string) => void = (m) => console.log(m);
export function setLog(fn: (msg: string) => void): void {
  _log = fn;
}
function log(msg: string): void {
  _log(msg);
}

/** 网关 key 候选清单，按优先级：[标签, key]，key=="" 表示匿名。
 *
 * 顺序（实测本届转写网关把 Authorization 透传上游 DeepSeek）：
 * 1. anonymous —— 匿名探测；2. DEEPSEEK_API_KEY_HOSTED；3. BENCHMARK_TOKEN；
 * 4. DEEPSEEK_API_KEY（**最后**：烧自己的额度）；5. 当前已配置 key 兜底。
 * 标签只带尾 4 位掩码；exclude 用于 401 热切换时跳过刚被拒的 key。
 */
export function keyCandidates(exclude?: Set<string>): Array<[string, string]> {
  const ex = exclude ?? new Set<string>();
  const out: Array<[string, string]> = [["anonymous", ""]];
  const seen = new Set<string>(ex);
  for (const name of ["DEEPSEEK_API_KEY_HOSTED", "BENCHMARK_TOKEN", "DEEPSEEK_API_KEY"]) {
    const v = process.env[name] ?? "";
    if (v && !seen.has(v)) {
      seen.add(v);
      out.push([`env:${name}(${keyMask(v)})`, v]);
    }
  }
  if (CFG.llm_api_key && !seen.has(CFG.llm_api_key)) {
    out.push([`current-config(${keyMask(CFG.llm_api_key)})`, CFG.llm_api_key]);
  }
  return out;
}

/** 按候选清单探测网关 key，返回可用 key 的新 client（全败 null）。
 *
 * probeAll=false（401 热切换）：命中第一个就停；probeAll=true（冒烟诊断）：全探一遍，
 * **每个候选一行掩码结果日志**。命中即更新 CFG.llm_api_key 并设 CURRENT_CLIENT。 */
export async function discoverGatewayKey(
  cause = "smoke",
  exclude?: Set<string>,
  probeAll = false,
): Promise<LlmClient | null> {
  const cands = keyCandidates(exclude);
  let winner: [string, string] | null = null;
  for (const [label, key] of cands) {
    try {
      const probe = clientWithKey(key, Math.min(CFG.request_timeout, 30));
      await probe.create({
        model: CFG.model,
        messages: [{ role: "user", content: "回复两个字：正常" }],
        max_tokens: 512, // 推理模型需预留思考 token，太小会 400 造成假阴性
      });
      log(`gateway key probe [${cause}]: ${label} -> OK`);
      rec().event("gateway_key_probe", { cause, candidate: label, result: "ok" });
      if (!winner) {
        winner = [label, key];
        if (!probeAll) break;
      }
    } catch (e) {
      const status = e instanceof LlmHttpError ? e.status : null;
      const result = status
        ? `rejected(${status})`
        : `failed(${e instanceof Error ? e.name : "Error"})`;
      log(`gateway key probe [${cause}]: ${label} -> ${result}`);
      rec().event("gateway_key_probe", { cause, candidate: label, result });
    }
  }
  if (!winner) {
    log(
      `gateway key probe [${cause}]: ALL ${cands.length} candidates failed — ` +
        `网关拒绝了全部 key/匿名（托管需注入真实 DEEPSEEK_API_KEY）`,
    );
    return null;
  }
  const [, key] = winner;
  CFG.llm_api_key = key;
  DISCOVERY_DONE = true;
  CURRENT_CLIENT = makeClient();
  log(`gateway key selected [${cause}]: ${winner[0]} — subsequent calls use it`);
  return CURRENT_CLIENT;
}

/** 网关 401（key 被拒）时按候选清单重探测（排除刚失败的 key），命中则热切换。
 * 只试一次防死循环。 */
export async function tryReauth(e: unknown): Promise<LlmClient | null> {
  const status = e instanceof LlmHttpError ? e.status : null;
  if (REAUTH_TRIED || status !== 401) return null;
  REAUTH_TRIED = true;
  return await discoverGatewayKey("reauth", new Set([CFG.llm_api_key]), false);
}

// remainingSec 在 agent.ts（依赖 START_TS）；这里通过注入避免循环依赖
let _remainingSec: () => number | null = () => null;
export function setRemainingSec(fn: () => number | null): void {
  _remainingSec = fn;
}
let _shouldStop: () => boolean = () => false;
export function setStopCheck(fn: () => boolean): void {
  _shouldStop = fn;
}

/** chat completion 调用，按 AGENT_LLM_RETRY_MODE 重试。
 *
 * - forever（默认）：连接/超时/限流/5xx 无限指数退避（封顶 llm_retry_max_delay），
 *   直到成功或任务时限；每 10 次记一条日志避免刷屏。
 * - finite：最多 api_max_retries 次后抛出。
 * - 4xx（认证/参数错误，非 429）：立即抛出，不重试。
 * - useTools=false：复盘总结轮/旁观者轮用——不下发工具，模型只能输出文本。
 * - extra.tools：动态工具集（fork/core_log/broadcast 开启时由 solveTask 组装）；
 *   缺省仍只有 shell（默认行为不变）。
 */
export async function callLlm(
  client: LlmClient,
  messages: ChatMessage[],
  useTools = true,
  extra: { signal?: AbortSignal; tools?: unknown[] } = {},
): Promise<ChatResponse> {
  let delayMs = 2000;
  let attempt = 0;
  let active = client;
  if (CURRENT_CLIENT) active = CURRENT_CLIENT; // 其他处已热切换过 key，本调用跟随
  for (;;) {
    if (_shouldStop()) throw new Error("stopped: platform ended");
    attempt++;
    try {
      const opts: Parameters<LlmClient["create"]>[0] = {
        model: CFG.model,
        messages,
        temperature: 0.2,
      };
      if (useTools) {
        opts.tools = extra.tools?.length ? extra.tools : [SHELL_TOOL_SCHEMA];
        opts.tool_choice = "auto";
      }
      if (extra.signal) opts.signal = extra.signal; // 限时调用（通关总结）的中止信号
      return await active.create(opts);
    } catch (e) {
      // 外部中止（总结超时）：立即失败不重试——限时语义必须干净
      if (extra.signal?.aborted) throw e;
      // 与 Python call_llm 一致：只对 API/网络类错误重试（openai APIError 家族）；
      // 其他异常（断言、编程错误）原样抛出，由 worker 落 task_error——绝不静默死循环。
      if (!(e instanceof LlmHttpError)) throw e;
      // 401 热切换：转写网关透传 key 给上游，内置 key 被拒时按候选 key 试并重试本轮
      const newClient = await tryReauth(e);
      if (newClient) {
        active = CURRENT_CLIENT = newClient;
        continue;
      }
      // 4xx 认证/参数错误（除 429 限流）重试无意义 → 立即失败
      const status = e instanceof LlmHttpError ? e.status : null;
      if (status !== null && status >= 400 && status < 500 && status !== 429) {
        if (status === 401 && CFG.mode === "hosted") {
          log(
            "LLM fatal auth error (401): 转写网关拒绝了所有候选 key——" +
              "托管模式需在平台环境变量 DEEPSEEK_API_KEY 注入**真实 DeepSeek key**" +
              "（内置 tsec-bench 是上届约定，本届网关透传上游鉴权，会被 401）",
          );
        }
        log(`LLM fatal client error (${status}): ${e instanceof Error ? e.message : e}`);
        throw e;
      }
      // 网络类错误：按模式决定
      if (CFG.llm_retry_mode !== "forever" && attempt >= CFG.api_max_retries) throw e;
      // 任务时限到了就别再等
      const rem = _remainingSec();
      if (rem !== null && rem <= CFG.llm_retry_max_delay) {
        throw new Error(
          `deadline approaching, abort retries: ${e instanceof Error ? e.message : e}`,
        );
      }
      const wait = Math.min(delayMs / 1000, CFG.llm_retry_max_delay);
      if (attempt === 1 || attempt % 10 === 0) {
        const name = e instanceof Error ? e.name : "Error";
        const msg = e instanceof Error ? e.message : String(e);
        log(
          `LLM call failed (attempt ${attempt}, ${CFG.llm_retry_mode} mode): ` +
            `${name}: ${msg}; retry in ${wait.toFixed(0)}s`,
        );
        rec().event("llm_retry", { attempt, error: `${name}: ${msg}`, next_delay_sec: wait });
      }
      await new Promise((r) => setTimeout(r, delayMs));
      delayMs = Math.min(delayMs * 2, CFG.llm_retry_max_delay * 1000);
    }
  }
}
