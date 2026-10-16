/** 平台 API 客户端：选题/start/close/查询。控制器用它做硬编排，模型不参与平台流程决策。 */
import { CFG } from "./config.js";
import type { Challenge } from "./types.js";

export const DIFF_RANK: Record<string, number> = { easy: 0, medium: 1, hard: 2 };

export class PlatformError extends Error {
  code: string;
  /** 原始业务错误消息（不含 code 前缀） */
  detail: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(`${code}: ${message}`);
    this.name = "PlatformError";
    this.code = code;
    this.detail = message;
    this.status = status;
  }
}

/** 平台任务已结束（评测截止）——终态，应立即停止整个 agent，不重试。 */
export class TaskFinishedError extends PlatformError {
  constructor(code: string, message: string, status: number) {
    super(code, message, status);
    this.name = "TaskFinishedError";
  }
}

/** 返回解析后的 JSON；平台业务错误抛 PlatformError。
 * 网络类错误（超时/DNS/连接）有限重试，避免调度层被瞬时故障打断。 */
export async function api(
  path: string,
  method = "GET",
  timeout = 20_000,
  retries = 3,
  body?: unknown,
): Promise<any> {
  const url = CFG.benchmark_base_url.replace(/\/+$/, "") + path;
  let lastErr: unknown;
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const resp = await fetch(url, {
        method,
        headers: {
          BENCHMARK_TOKEN: CFG.benchmark_token,
          ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeout),
      });
      const text = await resp.text();
      if (resp.ok) return JSON.parse(text);
      // HTTP 错误：尝试解析业务错误体
      let err: any;
      try {
        err = JSON.parse(text);
      } catch {
        throw new PlatformError(`http_${resp.status}`, text, resp.status);
      }
      const code = String(err?.code ?? resp.status);
      const msg = String(err?.message ?? text);
      // invalid_state + "finished" = 评测截止（终态）→ 专用异常，上层立即停止
      if (code === "invalid_state" && msg.toLowerCase().includes("finished")) {
        throw new TaskFinishedError(code, msg, resp.status);
      }
      throw new PlatformError(code, msg, resp.status);
    } catch (e) {
      if (e instanceof PlatformError) throw e;
      lastErr = e;
      if (attempt < retries) {
        await new Promise((r) => setTimeout(r, 2000 * attempt));
        continue;
      }
      break;
    }
  }
  const detail = lastErr instanceof Error ? `${lastErr.name}: ${lastErr.message}` : String(lastErr);
  throw new Error(`${method} ${path} failed after ${retries} attempts: ${detail}`);
}

export async function fetchChallenges(): Promise<Challenge[]> {
  return (await api("/openapi/v1/challenges")) as Challenge[];
}

export async function startChallenge(code: string): Promise<any> {
  return await api(`/openapi/v1/challenges/start?unique_code=${code}`, "POST");
}

export async function closeChallenge(code: string): Promise<any> {
  return await api(`/openapi/v1/challenges/close?unique_code=${code}`, "POST");
}

/** 提交 flag（submit_flag toolcall 的后端；唯一提交通道）。
 * 成功返回平台原始 JSON（correct/awarded/cumulative_score/correct_flag_count/
 * total_flag_count/matched_flag_index）；业务错误（409 duplicate 等）转
 * {correct:false, error:...} 返回给模型看（业务结果不抛）——评测截止终态仍穿透。 */
export async function submitFlag(uniqueCode: string, flag: string): Promise<any> {
  try {
    return await api("/openapi/v1/challenges/submit", "POST", 20_000, 2, {
      unique_code: uniqueCode,
      flag,
    });
  } catch (e) {
    if (e instanceof TaskFinishedError) throw e;
    if (e instanceof PlatformError) {
      return { correct: false, error: `${e.code}: ${e.message}` };
    }
    throw e; // 网络类：由上层按 LLM/工具错误处理
  }
}

export function challengeState(items: Challenge[], code: string): Challenge | null {
  return items.find((c) => c.unique_code === code) ?? null;
}

/** 未通关题目中选下一题：由易到难（difficulty → level → 题号），可通关题优先。 */
export function chooseNext(items: Challenge[]): Challenge | null {
  const pending = items.filter((c) => !c.is_completed);
  if (!pending.length) return null;
  pending.sort((a, b) => {
    const ac = a.container_status === "available" ? 0 : 1;
    const bc = b.container_status === "available" ? 0 : 1;
    if (ac !== bc) return ac - bc;
    const ad = DIFF_RANK[a.difficulty] ?? 9;
    const bd = DIFF_RANK[b.difficulty] ?? 9;
    if (ad !== bd) return ad - bd;
    const al = a.level ?? 9;
    const bl = b.level ?? 9;
    if (al !== bl) return al - bl;
    return a.unique_code < b.unique_code ? -1 : a.unique_code > b.unique_code ? 1 : 0;
  });
  return pending[0];
}

/** 通关但容器仍 available 的题全部 close，释放活跃名额。返回被 close 的 code 列表。 */
export async function releaseCompleted(items: Challenge[]): Promise<string[]> {
  const closed: string[] = [];
  for (const c of items) {
    if (c.is_completed && c.container_status === "available") {
      try {
        await closeChallenge(c.unique_code);
        closed.push(c.unique_code);
      } catch {
        // PlatformError：跳过（幂等/状态不符）
      }
    }
  }
  return closed;
}

export function activeCount(items: Challenge[]): number {
  return items.filter((c) => c.container_status === "available").length;
}
