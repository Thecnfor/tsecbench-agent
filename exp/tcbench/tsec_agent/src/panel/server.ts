/** 面板 Node 服务：只读记录目录 + 转发平台 API，为 React 面板提供 JSON。
 *
 * 端点：
 *   GET /api/state        → 全量面板状态（meta/tasks/events/platform/prediction/live）
 *   GET /api/task/:code   → 单题对话尾部（详情抽屉）
 *   GET /                 → React 构建产物（dist/panel/index.html）
 *
 * 用法：node dist/panel-server.js [--dir runs/<TOKEN>] [--port 8791] [--url 平台BASE_URL]
 */
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  envBaseUrl,
  envValue,
  fetchPlatform,
  findLatestRuns,
  fmtDelta,
  fmtDur,
  loadBaseline,
  loadMeta,
  loadRecentEvents,
  loadTasks,
  predictVsBaseline,
  readJsonlTail,
  taskFilterOf,
  taskLiveView,
} from "./data.js";

export interface PanelOptions {
  dir: string;
  baseUrl: string;
  port: number;
}

/** 组装面板一帧状态（与 panel.py render 的数据准备一一对应）。 */
export async function buildState(opts: Pick<PanelOptions, "dir" | "baseUrl">): Promise<any> {
  const runsDir = opts.dir;
  const meta = loadMeta(runsDir);
  const tasks = loadTasks(runsDir);
  const events = loadRecentEvents(runsDir, 8);
  const selPat = taskFilterOf(meta);
  const platform = await fetchPlatform(runsDir, opts.baseUrl, selPat);
  const now = Date.now() / 1000;
  const pred = predictVsBaseline(tasks, loadBaseline(runsDir), selPat, now);

  const list = Object.values(tasks);
  const running = list.filter((t) => t.running);
  const done = list.filter((t) => t.outcome === "pass");
  const failed = list.filter((t) => t.outcome === "fail");
  const paused = list.filter((t) => t.outcome === "paused");

  return {
    ts: new Date().toTimeString().slice(0, 8),
    dir: runsDir,
    token_hint: path.basename(runsDir.replace(/\/+$/, "")).slice(0, 8),
    meta: {
      mode: meta.mode ?? "?",
      model: meta.model ?? "?",
      run: meta.runs?.length ? meta.runs[meta.runs.length - 1] : {},
    },
    counts: {
      running: running.length,
      done: done.length,
      fail: failed.length,
      paused: paused.length,
    },
    platform,
    prediction: pred
      ? {
          total_pred: pred.total_pred,
          total_base: pred.total_base,
          delta: pred.total_pred - pred.total_base,
          fmt_pred: fmtDur(pred.total_pred),
          fmt_base: fmtDur(pred.total_base),
          fmt_delta: fmtDelta(pred.total_pred - pred.total_base),
          n_early: pred.items.filter((i) => i.state === "done" && i.delta < -1).length,
          n_over: pred.items.filter((i) => i.delta > 1 && ["run", "fail", "done"].includes(i.state))
            .length,
          n_todo: pred.items.filter((i) => i.state === "todo").length,
          items: pred.items
            .slice()
            .sort((a, b) => a.delta - b.delta)
            .map((i) => ({
              ...i,
              fmt_delta: fmtDelta(i.delta),
              fmt_pred: fmtDur(i.pred),
              fmt_base: fmtDur(i.base),
              fmt_actual: fmtDur(i.actual),
            })),
        }
      : null,
    tasks: list.map((t) => ({
      ...t,
      fmt_duration: fmtDur(t.duration),
      live: t.running ? taskLiveView(runsDir, t.code) : undefined,
      elapsed: t.running
        ? Math.max(
            0,
            now - (Date.parse(`${(t.started_at || "").replace(" ", "T")}Z`) || now) / 1000,
          )
        : undefined,
    })),
    events,
  };
}

function json(res: http.ServerResponse, obj: unknown, status = 200): void {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json",
  ".ico": "image/x-icon",
  ".map": "application/json",
};

export function createServer(opts: PanelOptions): http.Server {
  const panelDist = path.join(path.dirname(fileURLToPath(import.meta.url)), "panel");
  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://localhost");
      if (url.pathname === "/api/state") {
        json(res, await buildState(opts));
        return;
      }
      if (url.pathname.startsWith("/api/task/")) {
        const code = decodeURIComponent(url.pathname.slice("/api/task/".length));
        json(res, {
          code,
          conversation: readJsonlTail(
            path.join(opts.dir, "tasks", code, "conversation.jsonl"),
            200,
          ),
        });
        return;
      }
      // 静态：React 构建产物
      let file = url.pathname === "/" ? "/index.html" : url.pathname;
      const full = path.join(panelDist, file);
      if (!full.startsWith(panelDist) || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
        file = "/index.html"; // SPA fallback
      }
      const target = path.join(panelDist, file);
      if (!fs.existsSync(target)) {
        res.writeHead(404).end("panel 未构建：先在 tsec_agent/ 下执行 pnpm build:panel");
        return;
      }
      res.writeHead(200, {
        "Content-Type": MIME[path.extname(target)] ?? "application/octet-stream",
      });
      res.end(fs.readFileSync(target));
    } catch (e) {
      json(res, { error: e instanceof Error ? `${e.name}: ${e.message}` : String(e) }, 500);
    }
  });
}

export function parsePanelArgs(argv: string[]): PanelOptions {
  let dir = "";
  let baseUrl = "";
  let port = Number(envValue("PANEL_PORT", "8791"));
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--dir") dir = argv[++i] ?? "";
    else if (argv[i] === "--url") baseUrl = argv[++i] ?? "";
    else if (argv[i] === "--port") port = Number(argv[++i]) || port;
  }
  if (!dir) {
    dir = findLatestRuns() ?? "";
  }
  if (!dir || !fs.existsSync(dir)) {
    console.error("未找到 runs/<TOKEN> 记录目录（用 --dir 指定，或先跑一次 run.sh）");
    process.exit(1);
  }
  if (!baseUrl) baseUrl = envBaseUrl();
  return { dir, baseUrl, port };
}

const isMain =
  process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isMain) {
  const opts = parsePanelArgs(process.argv.slice(2));
  createServer(opts).listen(opts.port, "127.0.0.1", () => {
    console.log(`panel: dir=${opts.dir}\n  → http://127.0.0.1:${opts.port}  (refresh 自动)`);
  });
}
