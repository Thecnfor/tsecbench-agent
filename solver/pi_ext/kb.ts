/**
 * pi 扩展：离线安全知识库检索（wiki/skill 语料，渐进披露）。
 *
 * 语料：solver/kb/unified.db（FTS5；hacktricks-zh / ctf-wiki /
 * payloadsallthethings / nuclei-templates / awesome-poc / Skills 包）。
 * 工具内部调 solver/kb/index.py query，返回 top-k 片段 JSON。
 * 用途：N-day 复现、Payload 注入模板、SOP 检查清单——遇到卡点先查库，
 * 不要凭空猜 exploit 细节。
 */
import { Type } from "@sinclair/typebox"

export default function (pi: {
  registerTool: (tool: Record<string, unknown>) => void
  exec: (cmd: string, args: string[]) => Promise<{ stdout: string; stderr: string }>
}) {
  pi.registerTool({
    name: "kb",
    label: "kb",
    description:
      "Search the offline security knowledge base (hacktricks/cf-wiki/payloadsallthethings/" +
      "nuclei/awesome-poc + agent skills) by keywords. Returns top sections with full text " +
      "(payloads, exploit steps, checklists). USE THIS for N-day exploits, CVE payloads and " +
      "SOP checklists instead of guessing. Query in Chinese or English.",
    parameters: Type.Object({
      query: Type.String({ description: "keywords, e.g. 'HugeGraph gremlin RCE' or 'sql 注入 登录 绕过'" }),
      top_k: Type.Optional(Type.Number({ description: "default 3" })),
    }),
    async execute(
      _toolCallId: string,
      params: { query: string; top_k?: number },
    ) {
      const k = String(params.top_k ?? 3)
      try {
        const proc = await pi.exec("/usr/bin/env", [
          "python3",
          "__KB_QUERY__",
          "query",
          params.query,
          k,
        ])
        const text = (proc.stdout || proc.stderr || "").trim() || "[]"
        return { content: [{ type: "text", text: text.slice(0, 16000) }], details: {} }
      } catch (e) {
        return { content: [{ type: "text", text: `kb error: ${String(e)}` }], details: {} }
      }
    },
  })
}
