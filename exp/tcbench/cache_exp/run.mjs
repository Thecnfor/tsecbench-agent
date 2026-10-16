#!/usr/bin/env node
/** DeepSeek 前缀缓存 × fork 交叉对话 实验（零依赖，独立于 tsec_agent）。
 *
 * 研究问题：多 agent fork 模式下，两个分支交叉推进（a1 b1 a2 a3 b2）时，
 * DeepSeek 硬盘前缀缓存的命中率相对单线推进掉多少？哪些操作会毁掉缓存？
 *
 * 方法：合成消息（system + 共享上下文 + 分支指令 + 若干轮），不结合解题；
 * 每个请求读返回 usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens。
 *
 * 条件（每条件 2 重复，salt 隔离防跨条件缓存污染）：
 *   A_single       单线顺序推进 6 轮（基线）
 *   B_two_seq      fork 后 A 分支 3 轮跑完，再跑 B 分支 3 轮（fork 代价，无交叉）
 *   C_two_xchg     fork 后交叉推进 a1 b1 a2 a3 b2（核心问题）
 *   D_xchg_sysB    同 C，但 B 分支改写 system（风险①：第 0 token 起分叉）
 *   E_xchg_merge   同 C 前三步，然后中途把 B 的发现插入 A 历史继续（风险②：改写前缀）
 *   F_concurrent   a1 与 b1 同时发出（风险③：缓存秒级构建，并发首发竞争共享前缀）
 *
 * 条件过滤：EXP_CONDS=E,F 只跑指定条件（补跑用）。
 *
 * 预算护栏：请求上限 + prompt token 硬上限，超限即停（.env 真实 key，极其保守）。
 * 结果：results/raw-<ts>.jsonl（逐请求）+ results/summary-<ts>.md（汇总）。
 */
import { mkdirSync, readFileSync, writeFileSync, appendFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

// ------------------------------------------------------------------ config --
function loadEnvFile(path) {
  const out = {};
  try {
    for (const line of readFileSync(path, "utf8").split("\n")) {
      const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$/);
      if (m) out[m[1]] = m[2].trim();
    }
  } catch {
    /* .env 可缺省，全靠环境变量 */
  }
  return out;
}

const fileEnv = loadEnvFile(join(HERE, "..", ".env"));
const pick = (k) => process.env[k] || fileEnv[k] || "";

const API_KEY = pick("DEEPSEEK_API_KEY");
const BASE_URL = (pick("DEEPSEEK_BASE_URL") || "https://api.deepseek.com/v1").replace(/\/+$/, "");
const MODEL = pick("AGENT_MODEL_LOCAL") || "deepseek-flash";

const CFG = {
  reps: Number(process.env.EXP_REPS || 2),
  settleSec: 3, // 请求间隔（缓存构建秒级，模拟真实轮次间隔）
  gapBetweenCondsSec: 4,
  maxRequests: 70, // 请求总数硬上限
  maxPromptTokens: 500_000, // prompt token 硬上限（预算护栏）
  maxTokens: 16, // 输出极短，completion 成本忽略不计
  sysTokens: 500, // 合成 system 大小（≈token 数，按 2 token/行估算）
  sharedTokens: 1800, // 共享上下文大小
  dirTokens: 30, // 分支指令
  turnTokens: 110, // 每轮 user 追加
};

const PRICE = { hit: 0.003, miss: 0.15, out: 0.6 }; // $/Mtok，off-peak 仅作参考展示

// ------------------------------------------------------------------- utils --
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 确定性合成填充文本：~2 token/行，内容不重要，长度与稳定性重要。 */
function filler(tokens, salt) {
  const lines = [];
  for (let i = 0; i < Math.ceil(tokens / 2); i++) {
    lines.push(`tick ${salt}-${i} status=ok payload=${((i * 2654435761) >>> 0).toString(16)}`);
  }
  return lines.join("\n");
}

function sysMsg(salt, extra = "") {
  return { role: "system", content: `[exp-salt ${salt}]\n你是实验助手。\n${filler(CFG.sysTokens, `${salt}-sys`)}${extra}` };
}
function sharedMsg(salt) {
  return { role: "user", content: `[shared-context ${salt}]\n${filler(CFG.sharedTokens, `${salt}-sh`)}` };
}
const dirA = { role: "user", content: "[directive] 你是分支 A，只输出 OK。" };
const dirB = { role: "user", content: "[directive] 你是分支 B，只输出 OK。" };
function turnMsg(tag, i, salt) {
  return { role: "user", content: `[turn ${tag}-${i} ${salt}]\n${filler(CFG.turnTokens, `${salt}-${tag}${i}`)}` };
}
const mergeMsg = (salt) => ({
  role: "user",
  content: `[merge] 分支 B 的发现已并入：${filler(60, `${salt}-mg`)}`,
});

// ---------------------------------------------------------------- LLM call --
const budget = { requests: 0, prompt: 0, completion: 0, hit: 0, miss: 0 };
let thinkingParamOk = true; // thinking 参数被拒时全局降级

async function callLLM(messages) {
  if (budget.requests >= CFG.maxRequests) throw new Error(`请求上限 ${CFG.maxRequests} 已到，停止`);
  if (budget.prompt >= CFG.maxPromptTokens) throw new Error(`prompt token 上限 ${CFG.maxPromptTokens} 已到，停止`);
  const body = {
    model: MODEL,
    messages,
    temperature: 0,
    max_tokens: CFG.maxTokens,
    ...(thinkingParamOk ? { thinking: { type: "disabled" } } : {}),
  };
  let lastErr = "";
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const t0 = Date.now();
      const resp = await fetch(`${BASE_URL}/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${API_KEY}` },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(90_000),
      });
      const text = await resp.text();
      if (!resp.ok) {
        if (resp.status === 400 && text.includes("thinking") && thinkingParamOk) {
          thinkingParamOk = false; // 网关不认 thinking 参数 → 去掉重试
          lastErr = `HTTP 400 thinking 参数被拒，已降级去掉`;
          continue;
        }
        lastErr = `HTTP ${resp.status}: ${text.slice(0, 300)}`;
        if ([429, 500, 502, 503].includes(resp.status)) {
          await sleep(attempt * 5000);
          continue;
        }
        throw new Error(lastErr);
      }
      const data = JSON.parse(text);
      const u = data.usage || {};
      budget.requests++;
      budget.prompt += u.prompt_tokens || 0;
      budget.completion += u.completion_tokens || 0;
      budget.hit += u.prompt_cache_hit_tokens || 0;
      budget.miss += u.prompt_cache_miss_tokens || 0;
      return {
        content: data.choices?.[0]?.message?.content ?? "",
        usage: u,
        latencyMs: Date.now() - t0,
      };
    } catch (e) {
      lastErr = e.message;
      if (String(e.message).includes("上限")) throw e;
      await sleep(attempt * 5000);
    }
  }
  throw new Error(`LLM 调用失败（重试 3 次）: ${lastErr}`);
}

// ------------------------------------------------------------- recording --
const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
const resultsDir = join(HERE, "results");
mkdirSync(resultsDir, { recursive: true });
const rawPath = join(resultsDir, `raw-${ts}.jsonl`);
const records = [];

async function step(msgs, cond, rep, label) {
  const r = await callLLM(msgs);
  const u = r.usage || {};
  const rec = {
    cond,
    rep,
    label,
    prompt_tokens: u.prompt_tokens ?? null,
    hit: u.prompt_cache_hit_tokens ?? null,
    miss: u.prompt_cache_miss_tokens ?? null,
    completion_tokens: u.completion_tokens ?? null,
    latency_ms: r.latencyMs,
  };
  records.push(rec);
  appendFileSync(rawPath, JSON.stringify(rec) + "\n");
  const rate = rec.hit != null && rec.hit + rec.miss > 0 ? ((rec.hit / (rec.hit + rec.miss)) * 100).toFixed(1) + "%" : "-";
  console.log(
    `  [${cond} r${rep}] ${label.padEnd(8)} prompt=${String(rec.prompt_tokens).padStart(6)} ` +
      `hit=${String(rec.hit).padStart(6)} miss=${String(rec.miss).padStart(6)} hit%=${rate.padStart(6)} ` +
      `${(r.latencyMs / 1000).toFixed(1)}s`,
  );
  msgs.push({ role: "assistant", content: r.content || "OK" });
  await sleep((CFG.settleSec + (budget.requests % 3)) * 1000);
  return rec;
}

// ------------------------------------------------------------- conditions --
async function condA(salt, rep) {
  // 单线顺序 6 轮：基线命中率曲线
  const msgs = [sysMsg(salt), sharedMsg(salt)];
  for (let i = 1; i <= 6; i++) {
    if (i > 1) msgs.push(turnMsg("main", i, salt));
    await step(msgs, "A_single", rep, `t${i}`);
  }
}

async function condB(salt, rep) {
  // fork 后顺序：A 3 轮 → B 3 轮（fork 本身的代价，无交叉）
  const fork = [sysMsg(salt), sharedMsg(salt)];
  const A = [...fork, dirA];
  await step(A, "B_two_seq", rep, "a1");
  A.push(turnMsg("a", 2, salt));
  await step(A, "B_two_seq", rep, "a2");
  A.push(turnMsg("a", 3, salt));
  await step(A, "B_two_seq", rep, "a3");
  const B = [...fork, dirB];
  await step(B, "B_two_seq", rep, "b1");
  B.push(turnMsg("b", 2, salt));
  await step(B, "B_two_seq", rep, "b2");
  B.push(turnMsg("b", 3, salt));
  await step(B, "B_two_seq", rep, "b3");
}

async function condC(salt, rep) {
  // 核心问题：交叉推进 a1 b1 a2 a3 b2
  const fork = [sysMsg(salt), sharedMsg(salt)];
  const A = [...fork, dirA];
  const B = [...fork, dirB];
  await step(A, "C_two_xchg", rep, "a1");
  await step(B, "C_two_xchg", rep, "b1");
  A.push(turnMsg("a", 2, salt));
  await step(A, "C_two_xchg", rep, "a2");
  A.push(turnMsg("a", 3, salt));
  await step(A, "C_two_xchg", rep, "a3");
  B.push(turnMsg("b", 2, salt));
  await step(B, "C_two_xchg", rep, "b2");
}

async function condD(salt, rep) {
  // 风险①：B 分支改写 system（分叉点在第 0 token）
  const A = [sysMsg(salt), sharedMsg(salt), dirA];
  const B = [sysMsg(salt, "\n[extra] B 分支专属系统指令。"), sharedMsg(salt), dirB];
  await step(A, "D_xchg_sysB", rep, "a1");
  await step(B, "D_xchg_sysB", rep, "b1");
  A.push(turnMsg("a", 2, salt));
  await step(A, "D_xchg_sysB", rep, "a2");
  A.push(turnMsg("a", 3, salt));
  await step(A, "D_xchg_sysB", rep, "a3");
  B.push(turnMsg("b", 2, salt));
  await step(B, "D_xchg_sysB", rep, "b2");
}

async function condE(salt, rep) {
  // 风险②：交叉三步后，把 B 的发现插入 A 历史中段，再继续 A（改写前缀）
  const fork = [sysMsg(salt), sharedMsg(salt)];
  const A = [...fork, dirA];
  const B = [...fork, dirB];
  await step(A, "E_xchg_merge", rep, "a1");
  await step(B, "E_xchg_merge", rep, "b1");
  A.push(turnMsg("a", 2, salt));
  await step(A, "E_xchg_merge", rep, "a2");
  // merge：A 的历史被改写（a2 的位置被 merge 消息顶掉，a2/a3 内容后移）
  const M = [...fork, dirA, { role: "assistant", content: "OK" }, mergeMsg(salt), turnMsg("a", 3, salt)];
  await step(M, "E_xchg_merge", rep, "m1");
  M.push(turnMsg("a", 4, salt));
  await step(M, "E_xchg_merge", rep, "m2");
}

async function condF(salt, rep) {
  // 风险③：a1 与 b1 同时发出——缓存秒级构建，先到者写、后到者可能读不到共享前缀
  const fork = [sysMsg(salt), sharedMsg(salt)];
  const A = [...fork, dirA];
  const B = [...fork, dirB];
  await Promise.all([step(A, "F_concurrent", rep, "a1||b1"), step(B, "F_concurrent", rep, "b1||a1")]);
  A.push(turnMsg("a", 2, salt));
  await step(A, "F_concurrent", rep, "a2");
  B.push(turnMsg("b", 2, salt));
  await step(B, "F_concurrent", rep, "b2");
}

// ------------------------------------------------------------------ report --
function summarize() {
  const groups = new Map();
  for (const r of records) {
    const k = `${r.cond} r${r.rep}`;
    const g = groups.get(k) || { cond: r.cond, rep: r.rep, reqs: 0, prompt: 0, hit: 0, miss: 0, out: 0 };
    g.reqs++;
    g.prompt += r.prompt_tokens || 0;
    g.hit += r.hit || 0;
    g.miss += r.miss || 0;
    g.out += r.completion_tokens || 0;
    groups.set(k, g);
  }
  const cost = ((budget.hit / 1e6) * PRICE.hit + (budget.miss / 1e6) * PRICE.miss + (budget.completion / 1e6) * PRICE.out).toFixed(4);

  let md = `# DeepSeek 缓存实验汇总（${ts}）\n\n`;
  md += `- 模型 \`${MODEL}\`，base \`${BASE_URL}\`，temperature 0，输出 ≤${CFG.maxTokens} tok\n`;
  md += `- 间隔 ${CFG.settleSec}s（缓存构建秒级），每条件 ${CFG.reps} 重复，salt 隔离\n`;
  md += `- 总请求 ${budget.requests}，prompt ${budget.prompt} tok（hit ${budget.hit} / miss ${budget.miss}），` +
    `completion ${budget.completion} tok\n`;
  md += `- 估算成本 ≈ $${cost}（off-peak 价目）\n\n`;
  md += `| 条件 | rep | 请求数 | Σprompt | Σhit | Σmiss | 命中率 |\n|---|---|---|---|---|---|---|\n`;
  for (const g of groups.values()) {
    const rate = g.hit + g.miss > 0 ? ((g.hit / (g.hit + g.miss)) * 100).toFixed(1) + "%" : "-";
    md += `| ${g.cond} | r${g.rep} | ${g.reqs} | ${g.prompt} | ${g.hit} | ${g.miss} | ${rate} |\n`;
  }
  md += `\n## 逐请求明细\n\n| 条件 | rep | 轮 | prompt | hit | miss | hit% | 延迟 |\n|---|---|---|---|---|---|---|---|\n`;
  for (const r of records) {
    const rate = r.hit != null && r.hit + r.miss > 0 ? ((r.hit / (r.hit + r.miss)) * 100).toFixed(1) + "%" : "-";
    md += `| ${r.cond} | r${r.rep} | ${r.label} | ${r.prompt_tokens} | ${r.hit} | ${r.miss} | ${rate} | ${(r.latency_ms / 1000).toFixed(1)}s |\n`;
  }
  const summaryPath = join(resultsDir, `summary-${ts}.md`);
  writeFileSync(summaryPath, md);
  console.log(`\n${md}`);
  console.log(`raw: ${rawPath}\nsummary: ${summaryPath}`);
}

// -------------------------------------------------------------------- main --
async function main() {
  if (!API_KEY) {
    console.error("FATAL: DEEPSEEK_API_KEY 未设置（exp/tcbench/.env 或环境变量）");
    process.exit(2);
  }
  console.log(`[cache-exp] model=${MODEL} base=${BASE_URL} reps=${CFG.reps} ` +
    `cap: ${CFG.maxRequests} reqs / ${CFG.maxPromptTokens} prompt-toks`);
  const conds = [
    ["A", condA],
    ["B", condB],
    ["C", condC],
    ["D", condD],
    ["E", condE],
    ["F", condF],
  ];
  const only = (process.env.EXP_CONDS || "").split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
  const plan = only.length ? conds.filter(([name]) => only.includes(name)) : conds;
  if (only.length && plan.length !== only.length) {
    console.error(`FATAL: EXP_CONDS=${only.join(",")} 有未知条件名`);
    process.exit(2);
  }
  try {
    for (let rep = 1; rep <= CFG.reps; rep++) {
      for (const [name, fn] of plan) {
        console.log(`\n== ${name} rep${rep} ==`);
        await fn(`${name.toLowerCase()}-r${rep}`, rep);
        await sleep(CFG.gapBetweenCondsSec * 1000);
      }
    }
  } catch (e) {
    console.error(`\n[提前终止] ${e.message}`);
  }
  summarize();
}

main();
