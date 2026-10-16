/** HTTP 层：路由、鉴权、422（框架形状）、错误映射。
 *
 * 守卫次序：① token → 404 task_not_found（官方语义：token 错不是 401/403）
 * ② 业务守卫在 state 内（时限终态 / 并发闸门 / 计分），错误统一映射 {code,message,detail}。
 * 422 用 FastAPI 形状 detail:[{loc,msg,type}]（与业务异常信封不同，官方 §4.3）。
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import type { ArenaConfig } from "./config.js";
import { ArenaError, type ArenaState } from "./state.js";

const BODY_LIMIT = 64 * 1024;

type Json = Record<string, unknown>;

function send(res: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(Buffer.byteLength(body)),
  });
  res.end(body);
}

function sendErr(res: ServerResponse, status: number, code: string, message: string): void {
  send(res, status, { code, message, detail: {} });
}

/** 框架级验证错误（FastAPI 形状） */
function send422(res: ServerResponse, loc: string[], msg: string, type: string): void {
  send(res, 422, { detail: [{ loc, msg, type }] });
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > BODY_LIMIT) throw new Error("request body too large");
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function qsCode(url: URL): string {
  return url.searchParams.get("unique_code") ?? "";
}

export function buildServer(state: ArenaState, cfg: ArenaConfig): Server {
  return createServer((req, res) => {
    handle(state, cfg, req, res).catch((e: unknown) => {
      if (e instanceof ArenaError) {
        sendErr(res, e.http, e.code, e.message);
        return;
      }
      console.error("[arena] 未预期异常", e);
      sendErr(res, 500, "internal_error", "unexpected internal error");
    });
  });
}

async function handle(state: ArenaState, cfg: ArenaConfig, req: IncomingMessage, res: ServerResponse): Promise<void> {
  // ① token 守卫（官方：无效/缺失 → 404 task_not_found）
  if (req.headers["benchmark_token"] !== cfg.token) {
    sendErr(res, 404, "task_not_found", "missing or invalid BENCHMARK_TOKEN");
    return;
  }
  const url = new URL(req.url ?? "/", "http://arena.local");
  const path = `${url.pathname}`;
  const method = req.method ?? "GET";
  const code = qsCode(url);

  if (path === "/openapi/v1/challenges" && method === "GET") {
    send(res, 200, state.list() as unknown as Json);
    return;
  }
  if (path === "/openapi/v1/challenges/start" && method === "POST") {
    if (!code) return send422(res, ["query", "unique_code"], "Field required", "missing");
    send(res, 200, await state.start(code));
    return;
  }
  if (path === "/openapi/v1/challenges/hint" && method === "GET") {
    if (!code) return send422(res, ["query", "unique_code"], "Field required", "missing");
    send(res, 200, state.hint(code));
    return;
  }
  if (path === "/openapi/v1/challenges/close" && method === "POST") {
    if (!code) return send422(res, ["query", "unique_code"], "Field required", "missing");
    send(res, 200, await state.close(code));
    return;
  }
  if (path === "/openapi/v1/challenges/submit" && method === "POST") {
    const raw = await readBody(req);
    let body: Json;
    try {
      body = JSON.parse(raw || "") as Json;
    } catch {
      return send422(res, ["body"], "JSON decode error", "json_invalid");
    }
    const ucode = body.unique_code;
    const flag = body.flag;
    if (typeof ucode !== "string" || ucode === "") {
      return send422(res, ["body", "unique_code"], "Input should be a valid string", "string_type");
    }
    if (typeof flag !== "string") {
      return send422(res, ["body", "flag"], "Input should be a valid string", "string_type");
    }
    if (flag.length < 1) return send422(res, ["body", "flag"], "String should have at least 1 character", "string_too_short");
    if (flag.length > 4096) {
      return send422(res, ["body", "flag"], "String should have at most 4096 characters", "string_too_long");
    }
    send(res, 200, state.submit(ucode, flag));
    return;
  }
  sendErr(res, 404, "not_found", `no route ${method} ${path}`);
}

/** 监听（port=0 → 系统分配），返回实际端口。 */
export function listen(server: Server, cfg: ArenaConfig): Promise<number> {
  return new Promise((resolveP, reject) => {
    server.once("error", reject);
    server.listen(cfg.port, cfg.bind, () => {
      resolveP((server.address() as AddressInfo).port);
    });
  });
}
