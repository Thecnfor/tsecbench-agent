#!/usr/bin/env python3
"""Live harness 观测台 + 一键接入（stdlib 单文件，无第三方依赖）。

启动：uv run python debug-ui/live.py [--port 7879]  → http://127.0.0.1:7879/

能力：
  观测  — orchestrator 存活 / VPN 状态 / 通关数 / 每题模型档、轮次、错误、静默秒数
  对话  — 点击任一任务查看 stdout 轨迹尾部（CoT + 工具调用，自动刷新）
  图谱  — prior.json 的 assets / open_leads / explored 渲染为攻击面图（canvas）
  日志  — 单题 stdout 与全量 journal 一键下载
  接入  — 粘贴 BENCHMARK_TOKEN / BASE_URL / ovpn 正文（或上传文件）+ sudo 密码，
          一键写 ing/ 并拉起 openvpn（本工具只监听 127.0.0.1）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
WS_ROOT = REPO / "workspaces"
JOURNAL = WS_ROOT / "journal.jsonl"
ING = REPO / "ing"
LOG_DIR = WS_ROOT / "ui-logs"
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MODEL_RE = re.compile(r"model=([a-z0-9_.-]+)")
OVPN_NAME = "task_live_vpn_config.ovpn"


# ---------------------------------------------------------------- 数据采集 ----
def _tail_bytes(path: Path, n_lines: int, block: int = 256 * 1024) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - block))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in data.splitlines() if line.strip()][-n_lines:]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def journal_events(limit: int | None = None) -> list[dict]:
    evs = []
    try:
        with JOURNAL.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return evs[-limit:] if limit else evs


def scoreboard(evs: list[dict]) -> dict:
    solved = sorted({e["unique_code"] for e in evs if e.get("event") == "solved"})
    abandoned = sorted(
        {e["unique_code"] for e in evs if e.get("event") == "abandoned"} - set(solved)
    )
    return {
        "solved": solved,
        "solved_count": len(solved),
        "abandoned": abandoned,
        "model_upgraded_events": sum(1 for e in evs if e.get("event") == "model_upgraded"),
        "early_submit_events": sum(1 for e in evs if e.get("event") == "early_submit"),
        "total_events": len(evs),
    }


def vpn_status() -> dict:
    try:
        out = subprocess.run(
            ["ip", "-br", "a"], capture_output=True, text=True, timeout=3
        ).stdout
        route = subprocess.run(
            ["ip", "route"], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"up": False, "iface": "", "ip": "", "subnet_route": False}
    iface, ip, subnet = "", "", False
    for line in out.splitlines():
        m = re.match(r"(tun\d+)\s+\S+\s+([0-9.]+)/", line)
        if m:
            iface, ip = m.group(1), m.group(2)
            break
    subnet = bool(re.search(r"10\.0\.128\.0/18", route))
    return {"up": bool(iface), "iface": iface, "ip": ip, "subnet_route": subnet}


def orchestrator_alive() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "python -m orchestrator"], capture_output=True, timeout=3
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def task_dir(name: str) -> Path | None:
    if not TASK_NAME_RE.match(name or ""):
        return None
    p = WS_ROOT / "workspaces" / name
    return p if p.is_dir() else None


def running_tasks(limit: int = 10) -> list[dict]:
    base = WS_ROOT / "workspaces"
    if not base.is_dir():
        return []
    dirs = [d for d in base.iterdir() if d.is_dir() and "_attempt" in d.name]

    def mtime(d: Path) -> float:
        log = d / "stdout.log"
        return log.stat().st_mtime if log.exists() else 0.0

    dirs.sort(key=mtime, reverse=True)
    tasks = []
    for d in dirs[: limit * 2]:
        log = d / "stdout.log"
        if not log.exists() or time.time() - mtime(d) > 900:
            continue
        lines = _tail_bytes(log, 400)
        model = ""
        for line in lines[:3]:
            m = MODEL_RE.search(line)
            if m:
                model = m.group(1)
                break
        last = ""
        for line in reversed(lines):
            if not line.startswith("[solver] finished"):
                last = line[:280]
                break
        flags_dir = d / "out" / "flags"
        prior = _read_json(d / "out" / "prior.json")
        tasks.append(
            {
                "name": d.name,
                "model": model,
                "age_s": round(time.time() - mtime(d)),
                "turns": sum(1 for line in lines if "assistant (turn" in line),
                "llm_errors": sum(1 for line in lines if "LLM call failed" in line),
                "flags": len(list(flags_dir.iterdir())) if flags_dir.is_dir() else 0,
                "last": last,
                "prior": {
                    "assets": prior.get("assets", []),
                    "explored": prior.get("explored", []),
                    "open_leads": prior.get("open_leads", []),
                },
            }
        )
        if len(tasks) >= limit:
            break
    return tasks


def snapshot() -> dict:
    evs = journal_events()
    return {
        "now": time.strftime("%H:%M:%S"),
        "orchestrator_alive": orchestrator_alive(),
        "vpn": vpn_status(),
        "score": scoreboard(evs),
        "tasks": running_tasks(),
        "journal": evs[-80:],
    }


# ---------------------------------------------------------------- 一键接入 ----
def _sudo(pw: str, argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["sudo", "-S", *argv],
            input=pw + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr)[-2000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"sudo failed: {exc}"


def connect(token: str, base_url: str, ovpn: str, sudo_pw: str) -> dict:
    if not token or not base_url or "--" not in ovpn:
        return {"ok": False, "msg": "token / base_url / ovpn 均不能为空"}
    ING.mkdir(exist_ok=True)
    (ING / "env").write_text(
        f"BENCHMARK_TOKEN:{token}\nBENCHMARK_BASE_URL:{base_url}\n", encoding="utf-8"
    )
    ovpn_path = ING / OVPN_NAME
    ovpn_path.write_text(ovpn, encoding="utf-8")
    os.chmod(ING / "env", 0o600)
    os.chmod(ovpn_path, 0o600)
    rc, _ = _sudo(sudo_pw, ["pkill", "-f", "openvpn --config"])
    _sudo(sudo_pw, ["pkill", "-9", "openvpn"])
    time.sleep(2)
    rc, out = _sudo(
        sudo_pw,
        ["openvpn", "--config", str(ovpn_path), "--daemon", "--log", "/tmp/openvpn-ui.log"],
    )
    if rc != 0:
        return {"ok": False, "msg": f"openvpn 启动失败: {out[-300:]}"}
    for _ in range(12):
        time.sleep(2)
        st = vpn_status()
        if st["up"]:
            return {"ok": True, "msg": f"已连接 {st['iface']} {st['ip']}", **st}
    return {"ok": False, "msg": "VPN 已拉起但 24s 内未出现 tun 接口（看 /tmp/openvpn-ui.log）"}


# ---------------------------------------------------------------- HTTP 层 ----
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path == "/api/live":
            self._send(200, json.dumps(snapshot(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if url.path == "/api/task/log":
            name = parse_qs(url.query).get("name", [""])[0]
            d = task_dir(name)
            if d is None:
                self._send(404, b"no such task", "text/plain")
                return
            tail = parse_qs(url.query).get("tail", ["300"])[0]
            body = "\n".join(_tail_bytes(d / "stdout.log", int(tail))).encode()
            self._send(200, body, "text/plain; charset=utf-8")
            return
        if url.path == "/api/download/task":
            name = parse_qs(url.query).get("name", [""])[0]
            d = task_dir(name)
            if d is None:
                self._send(404, b"no such task", "text/plain")
                return
            self._send(200, (d / "stdout.log").read_bytes(), "text/plain",
                       {"Content-Disposition": f'attachment; filename="{name}-stdout.log"'})
            return
        if url.path == "/api/download/journal":
            self._send(200, JOURNAL.read_bytes(), "application/jsonl",
                       {"Content-Disposition": 'attachment; filename="journal.jsonl"'})
            return
        if url.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if urlparse(self.path).path != "/api/connect":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, b"bad json", "text/plain")
            return
        result = connect(
            str(payload.get("token", "")).strip(),
            str(payload.get("base_url", "")).strip(),
            str(payload.get("ovpn", "")),
            str(payload.get("sudo_password", "")),
        )
        self._send(200, json.dumps(result, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def log_message(self, *args) -> None:
        pass


PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>Harness Live</title>
<style>
 :root{--bg:#0b0e14;--panel:#11161f;--line:#1f2733;--fg:#d5dbe5;--dim:#7d8798;
       --blue:#5aa9ff;--green:#3fb950;--orange:#f0883e;--purple:#bc8cff;--red:#f85149}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
   font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;display:flex;min-height:100vh}
 aside{width:330px;flex:none;background:var(--panel);border-right:1px solid var(--line);padding:16px}
 main{flex:1;padding:16px;min-width:0}
 h1{font-size:15px;margin:0 0 12px;color:var(--blue);letter-spacing:.3px}
 h2{font-size:12px;color:var(--dim);margin:18px 0 8px;font-weight:400;text-transform:uppercase}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:14px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
 .card .v{font-size:20px;margin-top:2px}.card .k{color:var(--dim);font-size:11px}
 .ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--orange)}
 input,textarea,button{width:100%;background:#0b1017;border:1px solid var(--line);color:var(--fg);
   border-radius:6px;padding:7px 9px;font:inherit;margin-bottom:8px}
 textarea{height:110px;resize:vertical}
 button{background:#1b2a41;border-color:#2b4a6f;cursor:pointer}
 button:hover{background:#22344f} button:disabled{opacity:.5;cursor:wait}
 .tabs{display:flex;gap:6px;margin-bottom:12px}
 .tab{padding:6px 14px;border:1px solid var(--line);border-radius:7px 7px 0 0;cursor:pointer;color:var(--dim)}
 .tab.on{color:var(--fg);background:var(--panel);border-bottom-color:var(--panel)}
 table{border-collapse:collapse;width:100%}
 th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
 th{color:var(--dim);font-weight:400;font-size:11px}
 tr.task{cursor:pointer} tr.task:hover td{background:#141b26}
 tr.sel td{background:#172235}
 td.last{color:var(--dim);max-width:640px;overflow:hidden;text-overflow:ellipsis}
 #drawer{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:12px}
 #log{max-height:46vh;overflow:auto;background:#070b10;border:1px solid var(--line);
      border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-all;font-size:12px}
 #log .turn{color:var(--blue)} #log .asst{color:var(--fg)} #log .err{color:var(--red)}
 #log .flag{color:var(--green);font-weight:600}
 #jrn{max-height:62vh;overflow:auto;background:#070b10;border:1px solid var(--line);
      border-radius:6px;padding:8px} #jrn div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #jrn b{color:var(--fg)} .ev-solved{color:var(--green)} .ev-abandoned{color:var(--orange)}
 .ev-model_upgraded{color:var(--purple)} .ev-early_submit{color:var(--blue)}
 canvas{width:100%;height:340px;background:#070b10;border:1px solid var(--line);border-radius:6px}
 .row{display:flex;gap:8px}.row>*{flex:1}
 .hint{color:var(--dim);font-size:11px;margin:-2px 0 8px}
 .meta{color:var(--dim);font-size:12px;margin-bottom:8px}
</style></head><body>
<aside>
 <h1>TSecBench Harness</h1>
 <h2>接入</h2>
 <input id=token placeholder="BENCHMARK_TOKEN">
 <input id=base placeholder="BENCHMARK_BASE_URL" value="https://tsecbench.zc.tencent.com">
 <textarea id=ovpn placeholder="粘贴 .ovpn 正文（或选择文件）"></textarea>
 <div class=hint><input type=file id=file accept=".ovpn" style="margin:0"></div>
 <input id=sudopw type=password placeholder="sudo 密码（仅用于启动 openvpn）">
 <button id=conn>一键连接 VPN + 写入 ing/</button>
 <div id=connmsg class=hint></div>
 <h2>状态</h2>
 <div class=card><div class=k>VPN</div><div class=v id=vpn>—</div></div>
</aside>
<main>
 <div class=cards>
  <div class=card><div class=k>orchestrator</div><div class="v" id=orch>—</div></div>
  <div class=card><div class=k>已通关</div><div class=v id=sc>0</div></div>
  <div class=card><div class=k>reasoner 升档</div><div class=v id=up>0</div></div>
  <div class=card><div class=k>early_submit</div><div class=v id=es>0</div></div>
  <div class=card><div class=k>放弃</div><div class=v id=ab>0</div></div>
  <div class=card><div class=k>时钟</div><div class=v id=now>—</div></div>
 </div>
 <div class=tabs>
  <div class="tab on" data-t=tasks>任务</div>
  <div class=tab data-t=graph>图谱</div>
  <div class=tab data-t=journal>Journal</div>
 </div>
 <section id=sec-tasks>
  <table><thead><tr><th>任务</th><th>模型</th><th>轮次</th><th>错误</th><th>flags</th>
   <th>静默(s)</th><th>最后动作</th></tr></thead><tbody id=tb></tbody></table>
  <div id=drawer style="display:none">
   <div class=meta id=dmeta></div>
   <div class=row style="margin-bottom:8px">
    <button id=dl>下载该题 stdout</button>
    <button id=dlj>下载 journal</button>
   </div>
   <canvas id=cv></canvas>
   <div class=hint style="margin-top:6px">攻击面图：实心=已确认资产，空心=待探索线索，连线=同题上下文</div>
   <h2 style="margin-top:12px">对话轨迹（尾部自动刷新）</h2>
   <div id=log></div>
  </div>
 </section>
 <section id=sec-journal style="display:none">
  <div class=row style="margin-bottom:8px"><button id=dlj2>下载全量 journal.jsonl</button></div>
  <div id=jrn></div>
 </section>
</main>
<script>
const $=s=>document.querySelector(s);
let sel=null, tab="tasks", last=null;
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
 tab=t.dataset.t;document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x===t));
 $("#sec-tasks").style.display=tab==="tasks"?"":"none";
 $("#sec-journal").style.display=tab==="journal"?"":"none";
 if(tab==="graph"&&sel)openTask(sel);
});
$("#file").onchange=async e=>{const f=e.target.files[0];if(f)$("#ovpn").value=await f.text()};
$("#conn").onclick=async()=>{
 const b=$("#conn");b.disabled=true;$("#connmsg").textContent="连接中…";
 try{
  const r=await (await fetch("/api/connect",{method:"POST",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({token:$("#token").value.trim(),base_url:$("#base").value.trim(),
    ovpn:$("#ovpn").value,sudo_password:$("#sudopw").value})})).json();
  $("#connmsg").textContent=(r.ok?"✓ ":"✗ ")+r.msg;
 }catch(e){$("#connmsg").textContent="✗ 请求失败"}
 b.disabled=false;tick();
};
async function openTask(name){
 sel=name;$("#drawer").style.display="";$("#dmeta").textContent="任务 "+name;
 $("#dl").onclick=()=>location.href="/api/download/task?name="+encodeURIComponent(name);
 drawGraph();await loadLog();
}
function drawGraph(){
 const t=(last||{}).tasks?.find(x=>x.name===sel); const cv=$("#cv");
 const dpr=window.devicePixelRatio||1; const w=cv.clientWidth,h=cv.clientHeight;
 cv.width=w*dpr;cv.height=h*dpr;const ctx=cv.getContext("2d");ctx.scale(dpr,dpr);
 ctx.clearRect(0,0,w,h);
 if(!t){ctx.fillStyle="#7d8798";ctx.font="12px monospace";ctx.fillText("暂无 prior.json 数据",12,24);return}
 const assets=t.prior.assets.slice(0,14), leads=t.prior.open_leads.slice(0,14);
 const cx=w/2,cy=h/2,ra=Math.min(w,h)*0.32;
 const pos={};
 assets.forEach((a,i)=>{const ang=i/Math.max(1,assets.length)*Math.PI*2-Math.PI/2;
  const x=assets.length===1?cx:cx+Math.cos(ang)*ra,y=cy+Math.sin(ang)*ra;pos["a"+i]=[x,y];
  ctx.beginPath();ctx.arc(x,y,10,0,7);ctx.fillStyle="#1f6feb";ctx.fill();
  ctx.fillStyle="#d5dbe5";ctx.font="11px monospace";ctx.fillText(String(a).slice(0,40),x+14,y+4)});
 leads.forEach((l,i)=>{const ang=(i+0.5)/Math.max(1,leads.length)*Math.PI*2-Math.PI/2;
  const x=cx+Math.cos(ang)*(ra*0.55),y=cy+Math.sin(ang)*(ra*0.55);
  if(!assets.length){ctx.strokeStyle="#3d4757";ctx.strokeRect(x-6,y-6,12,12)}
  ctx.strokeStyle="#bc8cff";ctx.strokeRect(x-6,y-6,12,12);
  ctx.fillStyle="#7d8798";ctx.font="11px monospace";ctx.fillText(String(l).slice(0,40),x+12,y+4)});
 if(!assets.length&&!leads.length){
  ctx.fillStyle="#7d8798";ctx.font="12px monospace";ctx.fillText("攻击面尚未发现（prior 为空）",12,24)}
}
function logLine(line){
 const d=el("div","");
 if(line.startsWith("[solver] turn")||line.startsWith("[solver] assistant")) d.className="asst";
 if(line.includes("LLM call failed")) d.className="err";
 if(/flag\{/.test(line)) d.className="flag";
 if(line.startsWith("[solver] assistant")) d.className="turn";
 d.textContent=line;return d;
}
async function loadLog(){
 if(!sel)return;
 try{
  const txt=await (await fetch("/api/task/log?name="+encodeURIComponent(sel)+"&tail=400")).text();
  const box=$("#log");box.replaceChildren();
  for(const line of txt.split("\n").slice(-400)) box.append(logLine(line));
  box.scrollTop=box.scrollHeight;
 }catch(e){}
}
function vpnBadge(v){
 const e=$("#vpn");
 if(!v) {e.textContent="—";return}
 if(v.up&&v.subnet_route){e.textContent="up "+v.iface+" "+v.ip;e.className="v ok"}
 else if(v.up){e.textContent="up "+v.iface+"（缺子网路由）";e.className="v warn"}
 else {e.textContent="down";e.className="v bad"}
}
async function tick(){
 try{
  const d=await (await fetch("/api/live")).json();last=d;
  $("#now").textContent=d.now;
  $("#orch").textContent=d.orchestrator_alive?"alive":"DEAD";
  $("#orch").className="v "+(d.orchestrator_alive?"ok":"bad");
  $("#sc").textContent=d.score.solved_count;
  $("#up").textContent=d.score.model_upgraded_events;
  $("#es").textContent=d.score.early_submit_events;
  $("#ab").textContent=d.score.abandoned.length;
  vpnBadge(d.vpn);
  const tb=$("#tb");tb.replaceChildren();
  if(!d.tasks.length){const tr=el("tr"),td=el("td","",'无活跃任务');td.colSpan=7;tr.append(td);tb.append(tr)}
  for(const t of d.tasks){const tr=el("tr","task"+(t.name===sel?" sel":""));
   for(const v of [t.name,t.model,t.turns,t.llm_errors,t.flags,t.age_s])tr.append(el("td","",String(v)));
   tr.append(el("td","last",t.last));tr.onclick=()=>openTask(t.name);tb.append(tr)}
  const jrn=$("#jrn");jrn.replaceChildren();
  for(const e of d.journal.slice().reverse()){const div=el("div","ev-"+(e.event||""));
   div.append(document.createTextNode(new Date(e.ts*1000).toLocaleTimeString()+" "));
   div.append(el("b","",e.event));
   div.append(document.createTextNode(" "+(e.unique_code||"")+" "+(e.action||"")+" "+
     (e.at||"")+" "+(e.model||"")+" "+(e.duration_s!==undefined?e.duration_s+"s":"")));
   jrn.append(div)}
  if(sel){await loadLog();drawGraph()}
 }catch(e){$("#orch").textContent="UI-LOST";$("#orch").className="v bad"}
}
$("#dlj").onclick=$("#dlj2").onclick=()=>location.href="/api/download/journal";
tick();setInterval(tick,2000);
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7879)
    args = ap.parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"harness live: http://127.0.0.1:{args.port}/", file=sys.stderr)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
