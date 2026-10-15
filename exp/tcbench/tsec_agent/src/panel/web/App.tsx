import { useCallback, useEffect, useState } from "react";

interface TaskRow {
  code: string;
  duration: number;
  fmt_duration: string;
  segments: number;
  started_at?: string;
  closed_at?: string;
  target?: string;
  attempt?: number;
  flags?: string;
  outcome?: string;
  summary?: string;
  running: boolean;
  elapsed?: number;
  live?: { last_output: string; last_thinking: string; running_cmds: string[]; last_cmd: string };
}

interface PredictItem {
  code: string;
  state: string;
  delta: number;
  fmt_delta: string;
  fmt_pred: string;
  fmt_base: string;
  fmt_actual: string;
}

interface State {
  ts: string;
  dir: string;
  token_hint: string;
  meta: { mode: string; model: string; run: Record<string, any> };
  counts: { running: number; done: number; fail: number; paused: number };
  platform: { global: Record<string, number>; sel: Record<string, number> | null } | null;
  prediction: {
    total_pred: number;
    total_base: number;
    fmt_pred: string;
    fmt_base: string;
    fmt_delta: string;
    n_early: number;
    n_over: number;
    n_todo: number;
    items: PredictItem[];
  } | null;
  tasks: TaskRow[];
  events: Array<Record<string, any>>;
}

interface ConvMsg {
  seq: number;
  kind: string;
  ts: string;
  content?: any;
  output?: string | null;
  thinking?: string | null;
  tool_calls?: Array<{ id: string; name: string; arguments: string }>;
  result?: any;
  duration_ms?: number;
  kind_note?: string;
  error?: string;
}

const REFRESH_MS = 5000;

function fmtElapsed(sec: number | undefined): string {
  if (sec === undefined) return "";
  const s = Math.max(0, Math.trunc(sec));
  const h = Math.trunc(s / 3600);
  const m = Math.trunc((s % 3600) / 60);
  const ss = s % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`;
}

function short(text: string | undefined, width = 140): string {
  const t = (text || "").replace(/\s+/g, " ").trim();
  return t.length <= width ? t : `${t.slice(0, width - 1)}…`;
}

/** 单题详情抽屉：对话尾部（thinking/output/tool/result）。 */
function TaskDrawer({ code, onClose }: { code: string; onClose: () => void }) {
  const [msgs, setMsgs] = useState<ConvMsg[]>([]);
  const [err, setErr] = useState("");
  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/task/${encodeURIComponent(code)}`);
      const j = await r.json();
      setMsgs(j.conversation ?? []);
      setErr("");
    } catch (e) {
      setErr(String(e));
    }
  }, [code]);
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);
  return (
    <div className="drawer">
      <div className="drawer-head">
        <b>{code}</b> 对话尾部（3s 刷新）
        <button type="button" onClick={onClose}>
          关闭
        </button>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="drawer-body">
        {msgs.map((m) => (
          <div key={m.seq} className={`msg kind-${m.kind}`}>
            <div className="msg-head">
              <span className="kind">{m.kind}</span>
              <span className="ts">{m.ts}</span>
              <span className="dur">{m.duration_ms ?? 0}ms</span>
              {m.kind_note && <span className="note">{m.kind_note}</span>}
            </div>
            {typeof m.content === "string" && <pre>{short(m.content, 4000)}</pre>}
            {m.thinking && <pre className="thinking">🤔 {short(m.thinking, 2000)}</pre>}
            {m.output && <pre className="output">{m.output}</pre>}
            {m.tool_calls?.map((tc) => (
              <pre key={tc.id} className="toolcall">
                ▶ {tc.name} {short(tc.arguments, 600)}
              </pre>
            ))}
            {m.kind === "tool" && (
              <pre className="toolresult">
                {short(
                  typeof m.result === "string" ? m.result : JSON.stringify(m.result ?? null),
                  1500,
                )}
              </pre>
            )}
            {m.error && <pre className="err">{m.error}</pre>}
          </div>
        ))}
        {!msgs.length && <div className="empty">（无对话记录）</div>}
      </div>
    </div>
  );
}

export function App() {
  const [st, setSt] = useState<State | null>(null);
  const [err, setErr] = useState("");
  const [openTask, setOpenTask] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(true);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/state");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = (await r.json()) as State;
      setSt(j);
      setErr("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  if (!st) {
    return (
      <div className="page">
        <h1>TSec Bench 面板</h1>
        {err ? <div className="err">加载失败：{err}（面板服务未起？）</div> : <div>加载中…</div>}
      </div>
    );
  }

  const g = st.platform?.global;
  const s = st.platform?.sel;
  const running = st.tasks.filter((t) => t.running);
  const done = st.tasks
    .filter((t) => t.outcome === "pass")
    .sort((a, b) => (b.closed_at ?? "").localeCompare(a.closed_at ?? ""));
  const failed = st.tasks
    .filter((t) => t.outcome === "fail")
    .sort((a, b) => (b.closed_at ?? "").localeCompare(a.closed_at ?? ""));
  const paused = st.tasks
    .filter((t) => t.outcome === "paused")
    .sort((a, b) => (b.closed_at ?? "").localeCompare(a.closed_at ?? ""));
  const p = st.prediction;

  return (
    <div className="page">
      <header>
        <div className="title">
          <h1>TSec Bench</h1>
          <span className="muted">
            bench={st.token_hint}… {st.meta.mode}/{st.meta.model} · run@
            {st.meta.run.started_at ?? "?"} · now {st.ts}
          </span>
        </div>
        <div className="stats">
          {st.platform ? (
            s ? (
              <>
                <Stat
                  label={`平台(选题 ${s.total}/${g?.total ?? 0})`}
                  value={`${s.completed}/${s.total}`}
                  sub={`flags ${s.flags}/${s.max_flags} · 总分 ${s.score}`}
                />
                <Stat
                  label="全局"
                  value={`${g?.completed}/${g?.total}`}
                  sub={`flags ${g?.flags}/${g?.max_flags} · 总分 ${g?.score}`}
                />
              </>
            ) : (
              <Stat
                label="平台"
                value={`${g?.completed}/${g?.total}`}
                sub={`flags ${g?.flags}/${g?.max_flags} · 总分 ${g?.score}`}
              />
            )
          ) : (
            <Stat
              label="平台（离线）"
              value={`本地通关 ${st.counts.done}`}
              sub={`记录 ${st.tasks.length} 题`}
            />
          )}
          <Stat
            label="本地"
            value={`▶${st.counts.running} ✔${st.counts.done} ✘${st.counts.fail}`}
            sub={st.counts.paused ? `∥${st.counts.paused} 暂停` : ""}
          />
        </div>
        <div className="muted dir">{st.dir}</div>
      </header>

      {err && <div className="err">刷新失败：{err}</div>}

      {p && (
        <section className="card prediction">
          <div className="row">
            <b>预测 {p.fmt_pred}</b> vs 历史 <b>{p.fmt_base}</b>{" "}
            <span className={p.fmt_delta.startsWith("-") ? "good" : "bad"}>{p.fmt_delta}</span>
            <span className="muted">
              &nbsp;✔提前 {p.n_early} · ▲超时 {p.n_over} · ⏳未开始 {p.n_todo}
            </span>
          </div>
          <div className="pred-items">
            {p.items
              .filter((i) => Math.abs(i.delta) >= 1)
              .slice(0, 20)
              .map((i) => (
                <span
                  key={i.code}
                  className={`pred-item ${i.state} ${i.delta > 0 ? "over" : "under"}`}
                >
                  {i.state === "done"
                    ? "✔"
                    : i.state === "run"
                      ? "▶"
                      : i.state === "fail"
                        ? "✘"
                        : "⏳"}
                  {i.code} {i.fmt_delta}
                  <span className="muted">
                    {" "}
                    ({i.fmt_actual}/史{i.fmt_base})
                  </span>
                </span>
              ))}
          </div>
        </section>
      )}

      <section>
        <h2>
          进行中 <span className="count">{running.length}</span>
        </h2>
        {running.length === 0 && <div className="empty">无</div>}
        {running.map((t) => (
          <button
            type="button"
            key={t.code}
            className="card task running"
            onClick={() => setOpenTask(t.code)}
          >
            <div className="row">
              <b>[{t.code}]</b> ▶ {fmtElapsed(t.elapsed)}{" "}
              <span className="muted">att={t.attempt ?? "?"}</span>{" "}
              <span className="muted">{t.target ?? "?"}</span>{" "}
              <span className="muted">
                段×{t.segments} 累计 {t.fmt_duration}
              </span>
              <span className="flags">{t.flags}</span>
            </div>
            {t.live && (
              <div className="live">
                {t.live.running_cmds[0] ? (
                  <div className="cmd">↳ {short(t.live.running_cmds[0])}</div>
                ) : t.live.last_cmd ? (
                  <div className="cmd muted">↳ {short(t.live.last_cmd)}</div>
                ) : t.live.last_output ? (
                  <div className="cmd muted">↳ {short(t.live.last_output)}</div>
                ) : t.live.last_thinking ? (
                  <div className="cmd muted">↳ {short(t.live.last_thinking)}</div>
                ) : null}
                {t.live.running_cmds.length > 1 && (
                  <div className="muted">
                    另 {t.live.running_cmds.length - 1} 个在跑：
                    {t.live.running_cmds.slice(1).join(" | ")}
                  </div>
                )}
              </div>
            )}
          </button>
        ))}
      </section>

      {paused.length > 0 && (
        <section>
          <h2>
            ∥ 待续跑 <span className="count">{paused.length}</span>
          </h2>
          <div className="chips">
            {paused.map((t) => (
              <button
                type="button"
                key={t.code}
                className="chip paused"
                title={t.summary}
                onClick={() => setOpenTask(t.code)}
              >
                {t.code}({t.flags} {t.fmt_duration})
              </button>
            ))}
          </div>
        </section>
      )}

      <section>
        <h2>
          ✘ 失败 <span className="count">{failed.length}</span>
        </h2>
        <div className="chips">
          {failed.map((t) => (
            <button
              type="button"
              key={t.code}
              className="chip fail"
              title={t.summary}
              onClick={() => setOpenTask(t.code)}
            >
              {t.code}({t.flags} {t.fmt_duration}
              {(t.attempt ?? 1) > 1 ? " 重试" : ""})
            </button>
          ))}
          {!failed.length && <span className="empty">无</span>}
        </div>
      </section>

      <section>
        <h2>
          <button type="button" className="heading-toggle" onClick={() => setShowDone(!showDone)}>
            ✔ 通关 <span className="count">{done.length}</span>
            <span className="muted"> {showDone ? "（点击收起）" : "（点击展开）"}</span>
          </button>
        </h2>
        {showDone && (
          <div className="chips">
            {done.map((t) => (
              <button
                type="button"
                key={t.code}
                className="chip pass"
                onClick={() => setOpenTask(t.code)}
              >
                {t.code}({t.flags} {t.fmt_duration})
              </button>
            ))}
            {!done.length && <span className="empty">无</span>}
          </div>
        )}
      </section>

      <section>
        <h2>最近事件</h2>
        <div className="events">
          {st.events.map((e) => (
            <div key={`${e.ts}-${e.type}-${JSON.stringify(e)}`} className="event">
              <span className="ts">{e.ts}</span> <b>{e.type}</b>{" "}
              <span className="muted">
                {short(
                  JSON.stringify(
                    Object.fromEntries(
                      Object.entries(e).filter(([k]) => k !== "ts" && k !== "type"),
                    ),
                  ),
                  200,
                )}
              </span>
            </div>
          ))}
          {!st.events.length && <div className="empty">无</div>}
        </div>
      </section>

      {openTask && <TaskDrawer code={openTask} onClose={() => setOpenTask(null)} />}
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
