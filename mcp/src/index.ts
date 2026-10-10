/**
 * L3 自研 MCP server 骨架（architecture.md §1.3 / image-spec.md §5）。
 *
 * 决策约束：
 *  - Node 26.8.1 钉死（toolchain.env）；构建期编译成纯 JS（tsc → dist/），
 *    不把 Node 26 的原生 TS type-stripping 当运行前提 —— 保住「切 24 LTS 只改一行」的退路。
 *  - B9（open-questions.md）：能用 CLI spawn 的就不套 MCP。本 server 只承载
 *    「工具数量多到需要 schema 化管理、且需维护跨调用状态」的能力面；工具清单待实测后定。
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const server = new McpServer({ name: "tsecbench-mcp", version: "0.0.1" });

// TODO(B9 实测后填充): 工具面与 REA（133 工具）的分工边界。
// 占位 ping 工具仅用于验证 stdio 握手与编译链。
server.registerTool(
  "ping",
  {
    description: "scaffold health check — remove once real tools land",
    inputSchema: {},
  },
  async () => ({ content: [{ type: "text", text: "pong" }] }),
);

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[tsecbench-mcp] ready (stdio)");
