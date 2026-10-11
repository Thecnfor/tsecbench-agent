/**
 * L1.5 TS 手动工作流 solver（Mastra）—— 模型框架下的确定性节点图。
 *
 * 手动工作流：recon → triage → plan → exploit → harvest 全部是显式 createStep
 * 节点，节点间数据流经 zod schema 钉死；模型只在节点内部干活（plan 步仅 1 次
 * LLM 调用，exploit 步 spawn pi headless），任何节点失败降级不阻塞整图。
 * 契约与 dual_main.py 完全一致（out/flags/NNN.txt + result.json + session.jsonl，
 * SOLVER_DEADLINE_EPOCH 预算，SOLVER_PI_CMD 测试钩子）。
 *
 * 赛马：SOLVER_CMD 可指向本入口（node dist/index.js）或 solver/dual_main.py。
 * 版本钉在 toolchain.env（MASTRA_VERSION / ZOD_VERSION / PI_VERSION）。
 */
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import fs from "node:fs"
import path from "node:path"

import { Agent } from "@mastra/core/agent"
import { Mastra } from "@mastra/core"
import { createStep, createWorkflow } from "@mastra/core/workflows"
import { z } from "zod"

const execFileAsync = promisify(execFile)

const SOLVER_DIR = path.resolve(import.meta.dirname, "..") // solver/

const PI_PACKAGE = process.env.PI_PACKAGE ?? "@earendil-works/pi-coding-agent"
const PI_VERSION = process.env.PI_VERSION ?? "1.1.0"
const LLM_MODEL = process.env.LLM_MODEL ?? "deepseek-chat"

// ---- 契约状态 --------------------------------------------------------------
const taskPath = process.env.SOLVER_TASK ?? ""
if (!taskPath) {
  console.error("[ts-workflow] FATAL: SOLVER_TASK not set")
  process.exit(2)
}
const outDir = path.join(path.dirname(taskPath), "out")
const deadlineEpoch = process.env.SOLVER_DEADLINE_EPOCH
  ? Number(process.env.SOLVER_DEADLINE_EPOCH)
  : undefined
const budgetS = deadlineEpoch
  ? Math.max(60, Math.min(2400, deadlineEpoch - Date.now() / 1000))
  : 2400

function readTask(): Record<string, unknown> {
  try {
    return JSON.parse(fs.readFileSync(taskPath, "utf8")) as Record<string, unknown>
  } catch {
    return {}
  }
}

// ---- session 事件流（与 gov/session.py 同一落盘形状）------------------------
function logEvent(kind: string, fields: Record<string, unknown> = {}): void {
  try {
    fs.mkdirSync(outDir, { recursive: true })
    fs.appendFileSync(
      path.join(outDir, "session.jsonl"),
      JSON.stringify({ ts: Date.now() / 1000, kind, ...fields }) + "\n",
    )
  } catch {
    /* 日志失败不阻塞解题 */
  }
}

// ---- flag 检测（与 common.py 同源正则）--------------------------------------
const NAMED_FLAG_PATTERNS = [
  /(?<![a-zA-Z])FLAG\{[^}\n]{1,200}\}/,
  /(?<![a-zA-Z])HTB\{[^}\n]{1,200}\}/,
  /(?<![a-zA-Z])picoCTF\{[^}\n]{1,200}\}/,
  /(?<![a-zA-Z])THM\{[^}\n]{1,200}\}/,
  /(?<![a-zA-Z])CTF\{[^}\n]{1,200}\}/,
  /(?<![a-zA-Z])XBEN\{[^}\n]{1,200}\}/,
]
const GENERIC_FLAG_PATTERN = /(?<![A-Za-z0-9_])[A-Za-z0-9_]{2,24}\{[^}\n]{4,120}\}/

function findFlags(text: string, includeGeneric = false): string[] {
  const found: string[] = []
  const withGeneric = includeGeneric ? [...NAMED_FLAG_PATTERNS, GENERIC_FLAG_PATTERN] : NAMED_FLAG_PATTERNS
  for (const re of withGeneric) {
    for (const m of text.matchAll(new RegExp(re.source, "g"))) {
      if (!found.includes(m[0])) found.push(m[0])
    }
  }
  return found
}

// ---- 确定性 triage（与 gov/profiles.py 同源关键词）---------------------------
const KEYWORDS: Record<string, string[]> = {
  web: ["http", "https", "web", "url", "sqli", "xss", "ssrf", "cookie", "token", "api",
    "login", "upload", "nginx", "apache", "flask", "django", "express", ":80", ":443", ":8080"],
  pwn: ["pwn", "binary", "stack", "heap", "overflow", "rop", "glibc", "shellcode",
    "nc ", "netcat", "elf", "canary", "format string"],
  reverse: ["reverse", "crackme", "decompile", "disassembl", "ghidra", "obfuscat", "packed"],
  forensics: ["forensic", "pcap", "wireshark", "memory dump", "stegan", "exif", "volatility"],
  misc: ["misc", "crypto", "cipher", "rsa", "aes", "hash", "encoding", "qr"],
}

function classify(text: string): string {
  const low = text.toLowerCase()
  let best = "misc"
  let bestScore = 0
  for (const [cat, kws] of Object.entries(KEYWORDS)) {
    const score = kws.filter((kw) => low.includes(kw)).length
    if (score > bestScore) {
      best = cat
      bestScore = score
    }
  }
  return best
}

const STRATEGY: Record<string, string> = {
  web: "Headers+body of / first; banner versions -> CVE match; vectors in order: sqli, IDOR, traversal, SSRF, upload, SSTI; dirbust last with focused wordlist; near-empty HTML -> read JS bundles.",
  pwn: "file+checksec -> static read of input handler -> map input format locally -> overflow offset/leak/ret2libc chain, scripted with pwntools.",
  reverse: "file+strings -> unpack -> decompile main/check (ghidra headless) -> reproduce check in python and invert/brute-force per char.",
  forensics: "file/binwalk/exiftool first; archives list+weak passwords; images zsteg/steghide; pcap export objects + stream conv.",
  misc: "Identify encoding/cipher family from alphabet; compute obvious quantities (gcd, small-e, chi-square XOR) before exotic attacks.",
}

// ---- 共享上下文（只读；可变数据一律走节点间 schema 流）------------------------
const task = readTask()
const targets = ((task.targets as string[] | undefined) ?? []).join(", ")
const taskText = [String(task.description ?? ""), String(task.hint ?? ""), targets].join(" ")
logEvent("meta", { solver: "ts-workflow", model: LLM_MODEL })

// step 1：确定性侦察（recon_recipe.py 预聚合 JSON，§1.6）
const reconStep = createStep({
  id: "recon",
  inputSchema: z.object({ targets: z.string() }),
  outputSchema: z.object({ recon: z.string() }),
  execute: async ({ inputData }) => {
    const target = inputData.targets.split(",")[0]?.trim()
    if (!target) return { recon: "" }
    try {
      const { stdout } = await execFileAsync(
        "python3",
        [path.join(SOLVER_DIR, "recon_recipe.py"), "host", "--target", target],
        { timeout: 150_000, maxBuffer: 8 * 1024 * 1024 },
      )
      logEvent("recon", { chars: stdout.length })
      return { recon: stdout.slice(0, 6000) }
    } catch (e) {
      logEvent("recon", { error: String(e).slice(0, 200) })
      return { recon: "" }
    }
  },
})

// step 2：确定性 triage（分类 + kb 检索，零 LLM）
const triageStep = createStep({
  id: "triage",
  inputSchema: z.object({ recon: z.string() }),
  outputSchema: z.object({ category: z.string(), knowledge: z.string() }),
  execute: async ({ inputData }) => {
    const category = classify(taskText + " " + inputData.recon)
    logEvent("profile", { category })
    let knowledge = ""
    try {
      const { stdout } = await execFileAsync(
        "python3",
        [path.join(SOLVER_DIR, "kb", "index.py"), "query", taskText, "2"],
        { timeout: 30_000 },
      )
      const hits = JSON.parse(stdout) as Array<{ doc: string; section: string; body: string }>
      knowledge = hits.map((h) => `## KB: ${h.doc}/${h.section}\n${h.body}`).join("\n\n")
      logEvent("kb", { hits: hits.length })
    } catch {
      /* kb 缺失降级 */
    }
    return { category, knowledge }
  },
})

// step 3：规划（全程唯一一次 LLM 调用，Mastra Agent；失败/超时跳过）
const planStep = createStep({
  id: "plan",
  inputSchema: z.object({ category: z.string(), knowledge: z.string() }),
  outputSchema: z.object({ strategy: z.string(), knowledge: z.string() }),
  execute: async ({ inputData }) => {
    let strategy = STRATEGY[inputData.category] ?? STRATEGY.misc!
    const apiKey = process.env.LLM_API_KEY ?? process.env.DEEPSEEK_API_KEY
    if (!apiKey || (deadlineEpoch !== undefined && deadlineEpoch - Date.now() / 1000 < 90)) {
      return { strategy, knowledge: inputData.knowledge }
    }
    try {
      const agent = new Agent({
        id: "planner",
        name: "planner",
        instructions: "Reply with JSON only.",
        model: {
          url: process.env.LLM_BASE_URL ?? "https://api.deepseek.com/v1",
          id: `deepseek/${LLM_MODEL}`,
          apiKey,
        },
      })
      const res = await agent.generate(
        `Pick 3-5 concrete first-step strategy bullets for this ${inputData.category} CTF task. Terse.\n` +
          taskText.slice(0, 2000),
      )
      const text = res.text?.trim() ?? ""
      if (text) {
        strategy += `\n# PLANNER\n${text.slice(0, 2000)}`
        logEvent("plan", { chars: text.length })
      }
    } catch (e) {
      logEvent("plan", { skipped: String(e).slice(0, 200) })
    }
    return { strategy, knowledge: inputData.knowledge }
  },
})

// step 4：exploit（spawn pi headless；agent dir 进程内生成）
async function runPi(prompt: string): Promise<string> {
  const agentDir = fs.mkdtempSync(path.join("/tmp", "tswf-agent-"))
  const baseUrl = (process.env.LLM_BASE_URL ?? "https://api.deepseek.com/v1").replace(/\/$/, "")
  fs.writeFileSync(
    path.join(agentDir, "models.json"),
    JSON.stringify({
      providers: {
        deepseek: {
          baseUrl: baseUrl.endsWith("/v1") ? baseUrl : `${baseUrl}/v1`,
          api: "openai-completions",
          apiKey: "$LLM_API_KEY",
          models: [{ id: LLM_MODEL }],
        },
      },
    }),
  )
  const append =
    "# EXECUTION CONTRACT\n" +
    "- You act through bash on a LEAN host (nmap, gobuster, curl, python3 installed).\n" +
    "- A `recon` tool exists (nmap top-ports + HTTP fingerprint + dir-bust, JSON out); prefer it.\n" +
    "- Non-interactive only; explicit timeouts; NEVER search the internet for writeups.\n" +
    "- Output the verified flag exactly (e.g. FLAG{...}) as your final answer and stop.\n" +
    (strategyInput.strategy ? `\n${strategyInput.strategy}\n` : "") +
    (strategyInput.knowledge ? `\n# KNOWLEDGE\n${strategyInput.knowledge}\n` : "")
  fs.writeFileSync(path.join(agentDir, "APPEND_SYSTEM.md"), append)
  // 注入快照（debug-ui 观测台展示「动态提示词注入」用；失败不影响解题）
  try {
    fs.mkdirSync(outDir, { recursive: true })
    fs.writeFileSync(path.join(outDir, "append_system.md"), append)
  } catch {
    /* 快照失败降级 */
  }
  // 复用 pi_ext 扩展与 kb skills（与 dual_main.py 同源）
  try {
    fs.mkdirSync(path.join(agentDir, "extensions"), { recursive: true })
    fs.copyFileSync(
      path.join(SOLVER_DIR, "pi_ext", "recon.ts"),
      path.join(agentDir, "extensions", "recon.ts"),
    )
    fs.mkdirSync(path.join(agentDir, "skills"), { recursive: true })
    for (const md of fs.readdirSync(path.join(SOLVER_DIR, "kb"))) {
      if (!md.endsWith(".md")) continue
      const stem = md.replace(/\.md$/, "")
      fs.mkdirSync(path.join(agentDir, "skills", stem), { recursive: true })
      fs.copyFileSync(
        path.join(SOLVER_DIR, "kb", md),
        path.join(agentDir, "skills", stem, "SKILL.md"),
      )
    }
  } catch {
    /* 扩展/skills 部署失败降级为裸 pi */
  }

  const override = (process.env.SOLVER_PI_CMD ?? "").trim()
  const argv = override
    ? [...override.split(" ").filter(Boolean), prompt]
    : [
        ...(process.env.PI_BIN ? [process.env.PI_BIN] : ["npx", "-y", `${PI_PACKAGE}@${PI_VERSION}`]),
        "-p", "--approve", "--no-session", "--no-context-files", "--no-mcp",
        "--model", `deepseek/${LLM_MODEL}`, "--", prompt,
      ]
  const { spawn } = await import("node:child_process")
  return await new Promise<string>((resolve) => {
    const child = spawn(argv[0]!, argv.slice(1), {
      env: { ...process.env, PI_CODING_AGENT_DIR: agentDir },
      stdio: ["ignore", "pipe", "pipe"],
    })
    let out = ""
    const timer = setTimeout(() => child.kill("SIGKILL"), budgetS * 1000)
    child.stdout.on("data", (d) => (out += d))
    child.stderr.on("data", (d) => (out += d))
    child.on("close", () => {
      clearTimeout(timer)
      resolve(out)
    })
    child.on("error", () => {
      clearTimeout(timer)
      resolve(out)
    })
  })
}

// exploit 步要用 plan 的输出组装 APPEND_SYSTEM.md；schema 流 + 闭包双通道
let strategyInput = { strategy: "", knowledge: "" }

const exploitStep = createStep({
  id: "exploit",
  inputSchema: z.object({ strategy: z.string(), knowledge: z.string() }),
  outputSchema: z.object({ piOutput: z.string() }),
  execute: async ({ inputData }) => {
    strategyInput = { strategy: inputData.strategy, knowledge: inputData.knowledge }
    const prompt =
      "Solve the pentest challenge described in your instructions now. " +
      (targets ? `Target: ${targets}. ` : "") +
      "Work autonomously; when you have verified the flag, output it exactly (e.g. FLAG{...}) " +
      "as your final answer and stop."
    const out = await runPi(prompt)
    for (const line of out.split("\n")) {
      if (line.trim()) logEvent("pi_stdout", { text: line.slice(0, 500) })
    }
    return { piOutput: out }
  },
})

// step 5：收割（落盘契约，§2.2）
const harvestStep = createStep({
  id: "harvest",
  inputSchema: z.object({ piOutput: z.string() }),
  outputSchema: z.object({ candidates: z.array(z.string()) }),
  execute: async ({ inputData }) => {
    const candidates = findFlags(inputData.piOutput, true)
    fs.mkdirSync(path.join(outDir, "flags"), { recursive: true })
    candidates.forEach((flag, i) => {
      fs.writeFileSync(path.join(outDir, "flags", `${String(i).padStart(3, "0")}.txt`), flag + "\n")
      process.stdout.write(flag + "\n")
      logEvent("candidate", { flag })
    })
    const result = {
      status: candidates.length > 0 ? "solved" : "failed",
      candidates,
      evidence: "",
      tokens_used: 0,
      tools_used: [] as string[],
      next_idea: "",
    }
    fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify(result, null, 2))
    logEvent("done", { candidates: candidates.length })
    return { candidates }
  },
})

// ---- 组图 + 注册 + 启动 -------------------------------------------------------
const solveWorkflow = createWorkflow({
  id: "ts-solve",
  inputSchema: z.object({ targets: z.string() }),
  outputSchema: z.object({ candidates: z.array(z.string()) }),
})
  .then(reconStep)
  .then(triageStep)
  .then(planStep)
  .then(exploitStep)
  .then(harvestStep)
  .commit()

const mastra = new Mastra({ workflows: { solve: solveWorkflow } })

async function main(): Promise<number> {
  const wf = mastra.getWorkflow("solve")
  const run = await wf.createRun()
  const result = await run.start({ inputData: { targets } })
  const candidates =
    result.status === "success" ? ((result.result?.candidates as string[] | undefined) ?? []) : []
  console.log(`[ts-workflow] finished status=${result.status} candidates=${JSON.stringify(candidates)}`)
  return 0
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    console.error(`[ts-workflow] FATAL: ${e instanceof Error ? e.message : String(e)}`)
    process.exit(0) // 契约：异常也不以非零退出，harvest 已尽力落盘
  })
