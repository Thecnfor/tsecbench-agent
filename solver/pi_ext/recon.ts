/**
 * pi 扩展：确定性侦察工具（Phase 3，.trae/documents/dual-kernel-solver-plan.md）。
 *
 * 由 dual_main.py 生成到 <agentDir>/extensions/recon.ts（jiti 加载 TS，
 * TypeBox 为 virtual module，文档依据 refs/BreachWeave/.claude/skills/
 * pi-mono-framework/references/pi-coding-agent.md）。工具内部调
 * solver/recon_recipe.py，返回预聚合 JSON —— 替代模型手拼长命令，
 * 落实 architecture.md §1.6「单次调用信息密度」杠杆。
 *
 * API 钉在 @earendil-works/pi-coding-agent@1.1.0（toolchain.env PI_VERSION）；
 * registerTool/execute 签名与 TypeBox 用法已对照 pi-mono-framework 文档核实。
 */
import { Type } from "@sinclair/typebox"

interface ReconResult {
  content: Array<{ type: string; text: string }>
  details: Record<string, unknown>
}

export default function (pi: {
  registerTool: (tool: Record<string, unknown>) => void
  exec: (cmd: string, args: string[]) => Promise<{ stdout: string; stderr: string }>
}) {
  pi.registerTool({
    name: "recon",
    label: "recon",
    description:
      "Deterministic recon recipe: nmap top-ports + service versions (mode=host), " +
      "HTTP headers/title/tech-fingerprint/dir-bust (mode=web), or both (mode=all). " +
      "Returns pre-aggregated JSON. PREFER THIS over hand-typing nmap/gobuster.",
    parameters: Type.Object({
      mode: Type.Union([Type.Literal("host"), Type.Literal("web"), Type.Literal("all")], {
        description: "host = port/service scan; web = http recon; all = both",
      }),
      target: Type.Optional(
        Type.String({ description: "host or ip for host/all modes, e.g. 10.0.0.1" }),
      ),
      url: Type.Optional(
        Type.String({ description: "absolute http URL for web/all modes, e.g. http://10.0.0.1:8080" }),
      ),
    }),
    async execute(_toolCallId: string, params: { mode: string; target?: string; url?: string }): Promise<ReconResult> {
      const argv = ["/usr/bin/env", "python3", "__RECON_RECIPE__", params.mode]
      if (params.mode === "web") {
        if (!params.url) {
          return err("--url is required for mode=web")
        }
        argv.push("--url", params.url)
      } else {
        if (!params.target) {
          return err("--target is required for mode=host/all")
        }
        argv.push("--target", params.target)
        if (params.url) argv.push("--url", params.url)
      }
      try {
        // jiti 加载的扩展里 pi.exec 由运行时注入（pi-mono-framework 文档 §Git Checkpoint 同款用法）
        const proc = await pi.exec(argv[0], [...argv.slice(1)])
        const text = (proc.stdout || proc.stderr || "").trim() || "{}"
        return { content: [{ type: "text", text: text.slice(0, 12000) }], details: {} }
      } catch (e) {
        return err(String(e))
      }
    },
  })
}

function err(message: string): ReconResult {
  return {
    content: [{ type: "text", text: JSON.stringify({ error: message }) }],
    details: {},
  }
}
