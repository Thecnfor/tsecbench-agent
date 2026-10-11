/**
 * TSecBench debug-ui —— 零依赖观测台（Node 26 原生 TS，正式镜像内置）。
 *
 * 数据源（全部是 solver 已落盘的契约产物，只读）：
 *   workspaces 下各 run 的 out/session.jsonl     双 solver 统一事件流
 *   out/prior.json                              动态记忆 assets/explored/open_leads
 *   out/result.json                             收尾状态
 *   out/append_system.md                        动态提示词注入快照
 *   solver/kb 下的 md                           RAG 语料 / LLM wiki / skills 源
 *
 * 端点：
 *   GET /                        观测台 UI
 *   GET /api/runs                全部 run 列表（含统计）
 *   GET /api/run/:id/events      事件全量
 *   GET /api/run/:id/stream      SSE 实时尾随（长任务调试）
 *   GET /api/run/:id/state       prior/result/flags/注入快照
 *   GET /api/kb                  wiki 文档树
 *   GET /api/kb/search?q=        走 kb/index.py 真实检索（验证 RAG 链路）
 */
import fs from "node:fs"
import http from "node:http"
import path from "node:path"
import { execFile } from "node:child_process"
import { promisify } from "node:util"

const execFileAsync = promisify(execFile)

const REPO = path.resolve(import.meta.dirname, "..")
const WORKSPACES = path.join(REPO, "workspaces")
const KB_DIR = path.join(REPO, "solver", "kb")
const PORT = Number(process.env.UI_PORT ?? 7878)
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
}

function listRuns(): Array<Record<string, unknown>> {
  if (!fs.existsSync(WORKSPACES)) return []
  return fs
    .readdirSync(WORKSPACES)
    .map((name) => {
      const outDir = path.join(WORKSPACES, name, "out")
      const eventsPath = path.join(outDir, "session.jsonl")
      if (!fs.existsSync(eventsPath)) return null
      const events = readEvents(eventsPath)
      let result = null
      try {
        result = JSON.parse(fs.readFileSync(path.join(outDir, "result.json"), "utf8"))
      } catch {
        /* 无 result */
      }
      return {
        id: name,
        events: events.length,
        kinds: countKinds(events),
        flags: countFlags(outDir),
        status: result?.status ?? "running",
        firstTs: events[0]?.ts ?? null,
        lastTs: events.at(-1)?.ts ?? null,
      }
    })
    .filter(Boolean)
}

function readEvents(eventsPath: string): Array<Record<string, unknown>> {
  try {
    return fs
      .readFileSync(eventsPath, "utf8")
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => {
        try {
          return JSON.parse(l)
        } catch {
          return { kind: "corrupt", raw: l.slice(0, 200) }
        }
      })
  } catch {
    return []
  }
}

function countKinds(events: Array<Record<string, unknown>>): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const e of events) counts[String(e.kind)] = (counts[String(e.kind)] ?? 0) + 1
  return counts
}

function countFlags(outDir: string): number {
  try {
    return fs.readdirSync(path.join(outDir, "flags")).filter((f) => f.endsWith(".txt")).length
  } catch {
    return 0
  }
}

function readState(id: string): Record<string, unknown> | null {
  const outDir = path.join(WORKSPACES, id, "out")
  if (!fs.existsSync(outDir)) return null
  const readJson = (name: string) => {
    try {
      return JSON.parse(fs.readFileSync(path.join(outDir, name), "utf8"))
    } catch {
      return null
    }
  }
  const flags: string[] = []
  try {
    for (const f of fs.readdirSync(path.join(outDir, "flags")).sort()) {
      flags.push(fs.readFileSync(path.join(outDir, "flags", f), "utf8").trim())
    }
  } catch {
    /* 无 flags */
  }
  let injection = null
  try {
    injection = fs.readFileSync(path.join(outDir, "append_system.md"), "utf8")
  } catch {
    /* 无快照 */
  }
  return {
    prior: readJson("prior.json"),
    result: readJson("result.json"),
    flags,
    injection,
  }
}

function kbDocs(): Array<Record<string, unknown>> {
  if (!fs.existsSync(KB_DIR)) return []
  return fs
    .readdirSync(KB_DIR)
    .filter((f) => f.endsWith(".md"))
    .map((f) => {
      const body = fs.readFileSync(path.join(KB_DIR, f), "utf8")
      return {
        doc: f.replace(/\.md$/, ""),
        sections: [...body.matchAll(/^## (.+)$/gm)].map((m) => m[1]),
        chars: body.length,
        body,
      }
    })
}

async function kbSearch(q: string): Promise<unknown> {
  try {
    const { stdout } = await execFileAsync(
      "python3",
      [path.join(KB_DIR, "index.py"), "query", q, "3"],
      { timeout: 10_000 },
    )
    return JSON.parse(stdout)
  } catch (e) {
    return { error: String(e).slice(0, 200) }
  }
}

function send(res: http.ServerResponse, code: number, body: string, type = "application/json") {
  res.writeHead(code, { "Content-Type": type })
  res.end(body)
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://localhost:${PORT}`)
  try {
    if (url.pathname === "/") {
      send(
        res,
        200,
        fs.readFileSync(path.join(import.meta.dirname, "index.html"), "utf8"),
        MIME[".html"],
      )
    } else if (url.pathname === "/api/runs") {
      send(res, 200, JSON.stringify(listRuns()))
    } else if (url.pathname.startsWith("/api/run/") && url.pathname.endsWith("/events")) {
      const id = url.pathname.slice("/api/run/".length, -"/events".length)
      send(res, 200, JSON.stringify(readEvents(path.join(WORKSPACES, id, "out", "session.jsonl"))))
    } else if (url.pathname.startsWith("/api/run/") && url.pathname.endsWith("/stream")) {
      // SSE 尾随：长任务调试时前端实时追加思维链
      const id = url.pathname.slice("/api/run/".length, -"/stream".length)
      const eventsPath = path.join(WORKSPACES, id, "out", "session.jsonl")
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      })
      // 只尾随「未来」事件：从当前文件末尾开始（历史由 /events 全量拉取，勿重放）
      let offset = fs.existsSync(eventsPath) ? fs.statSync(eventsPath).size : 0
      const timer = setInterval(() => {
        try {
          const stat = fs.existsSync(eventsPath) ? fs.statSync(eventsPath) : null
          if (stat && stat.size > offset) {
            const fd = fs.openSync(eventsPath, "r")
            const buf = Buffer.alloc(stat.size - offset)
            fs.readSync(fd, buf, 0, buf.length, offset)
            fs.closeSync(fd)
            offset = stat.size
            for (const line of buf.toString("utf8").split("\n")) {
              if (line.trim()) res.write(`data: ${line}\n\n`)
            }
          } else if (stat && stat.size < offset) {
            offset = 0 // 文件被截断重写
          }
        } catch {
          /* run 消失即结束 */
        }
        res.write(": ping\n\n")
      }, 1000)
      req.on("close", () => clearInterval(timer))
    } else if (url.pathname.startsWith("/api/run/") && url.pathname.endsWith("/state")) {
      const id = url.pathname.slice("/api/run/".length, -"/state".length)
      const state = readState(id)
      state ? send(res, 200, JSON.stringify(state)) : send(res, 404, "{}")
    } else if (url.pathname === "/api/kb") {
      send(res, 200, JSON.stringify(kbDocs()))
    } else if (url.pathname === "/api/kb/search") {
      send(res, 200, JSON.stringify(await kbSearch(url.searchParams.get("q") ?? "")))
    } else {
      send(res, 404, "not found")
    }
  } catch (e) {
    send(res, 500, JSON.stringify({ error: String(e).slice(0, 300) }))
  }
})

server.listen(PORT, () => {
  console.log(`[debug-ui] http://127.0.0.1:${PORT}  workspaces=${WORKSPACES}`)
})
