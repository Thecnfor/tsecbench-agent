/** 旁观者（bystander）：卡塞判定（组合信号）+ 输入构建 + 插嘴指令。
 *
 * 设计契约见 ../docs/fork-bystander-design.md §5：
 * - 触发 = 三信号任二成立（停滞时长 / 重复命令 / 段预算过半仍零核心事件）；
 * - 输入 = corelog 按分支分组 + flag 索引账本 + 卡塞分支近况摘要（不给全量历史，
 *   逼出抽象全局视角）；
 * - 本体 = 同款模型换 system（hosted 已验证可用），独立短上下文，与主对话缓存互不干扰；
 * - 输出 = 尾部追加 user 消息（append-only，缓存红线），限频在 BranchManager.allowBystander。
 */
import { buildBystanderLogView, type CorelogEntry, type FlagLedger } from "./corelog.js";

export const BYSTANDER_SYSTEM = `你是渗透任务的**旁观者（observer）**——另一个视角的资深渗透专家，
不参与执行，只负责在执行分支卡住时给出**更抽象、更全局**的指引。

你会看到：各分支的核心日志（按分支分组）、flag 索引进度、以及卡塞分支的近况。
你的价值在于**跳出执行细节**：

1. **换层**：卡在某个注入点/爆破上？提示往协议层、认证层、配置泄露、历史 CVE、
   信任链（token/session/SSRF）这些**更抽象的层面**想，而不是同一层换参数。
2. **全局调度**：日志里哪些方向重复了？哪些已确认走不通该放弃？不同分支的日志
   之间有没有可以组合起来的线索（A 分支的入口 + B 分支的凭据）？
3. **止损判断**：如果同一思路已消耗大量轮次，明确说"这条路放弃，改攻 X"。
4. **幻觉与错误前提核对**：对照各卡与进度找矛盾——声称拿到的证据在别处被自己证伪、
   连续提交都 correct:false 仍坚持、反复连接同一个不存在的目标（Connection refused 刷屏）。
   指出"你可能在臆测/打错目标"，并要求回到有工具结果支撑的事实。

要求：
- 只输出**一段简短中文**（≤120 字），直接给指引，不要客套、不要复述日志、不要输出命令块。
- 指引要具体到"下一步该往哪个方向想"，但**不要**替它写具体命令/payload。
- 你只读运行时派生的核心日志——不要猜测题目答案，不要编造日志里没有的事实。`;

export interface StallSignals {
  stall: boolean; // ① 距上次 core_log/flag 停滞超阈值
  repeat: boolean; // ② 近期同一命令 ≥3 次
  budget: boolean; // ③ 段过半且超半段无核心事件
  hardFail: boolean; // ④ 近期反复连接/解析/命令缺失失败=对幻觉目标空转
  hallucSubmit: boolean; // ⑤ 连续 correct:false=幻觉/错误提交
  count: number; // 成立数（≥2 触发）
}

/** 五信号评估（任二成立）。④⑤为幻觉/无用功的事实维度（零 LLM 成本）。纯函数可单测。
 * budget 语义（2026-01-16 修正）：段过半 + **距上次核心事件也超半段**——
 * 旧条件「段内零核心事件」会被段首的一张卡永久作废（arena 实测：08:00 记首卡后
 * 旁观者整段失明 15+ 分钟，nudge 额度同时耗尽）。 */
export function evaluateStall(input: {
  sinceCoreSec: number; // 距上次 core_log/flag 的秒数
  stallSec: number; // 阈值（短题/长题分档）
  recentCmds: string[]; // 分支近期命令（已归一化）
  segElapsedSec: number;
  segBudgetSec: number;
  recentHardFails?: boolean[]; // 近期工具结果硬失败标记（滚动，信号④）
  wrongSubmits?: number; // 连续 correct:false 次数（信号⑤）
}): StallSignals {
  const stall = input.sinceCoreSec >= input.stallSec;
  const counts = new Map<string, number>();
  for (const c of input.recentCmds) counts.set(c, (counts.get(c) ?? 0) + 1);
  let repeat = false;
  for (const n of counts.values()) if (n >= 3) repeat = true;
  const half = input.segBudgetSec * 0.5;
  const budget =
    input.segBudgetSec > 0 && input.segElapsedSec >= half && input.sinceCoreSec >= half;
  // ④ 近8次连接/解析/命令缺失类硬失败≥3（幻觉端口/错误前提空转）；⑤ 连续错提交≥2
  const hardFail = (input.recentHardFails ?? []).slice(-8).filter(Boolean).length >= 3;
  const hallucSubmit = (input.wrongSubmits ?? 0) >= 2;
  const count = [stall, repeat, budget, hardFail, hallucSubmit].filter(Boolean).length;
  return { stall, repeat, budget, hardFail, hallucSubmit, count };
}

/** 旁观者输入（corelog 分组视图 + flag 账本 + 卡塞分支近况）。 */
export function buildBystanderInput(args: {
  entries: CorelogEntry[];
  branchId: number;
  branchLabel?: string;
  branchNames: Map<number, string>;
  ledger: FlagLedger | null;
  recentDigest: string; // 卡塞分支最近 N 条消息摘要
  recentCmds: string[];
  segElapsedSec: number;
  signals: StallSignals;
}): string {
  const parts: string[] = [];
  const prog = args.ledger ? args.ledger.progress() : "未知";
  const head =
    `# 任务态势\n- flag 进度：${prog}\n` +
    `- 段已用：${Math.round(args.segElapsedSec)}s；卡塞对象：` +
    `${args.branchId === 0 ? "主分支" : `分支 ${args.branchId}`}` +
    `${args.branchLabel ? `（${args.branchLabel}）` : ""}\n` +
    `- 触发信号：停滞${args.signals.stall ? "✓" : "✗"} 重复${args.signals.repeat ? "✓" : "✗"} ` +
    `预算${args.signals.budget ? "✓" : "✗"} 连接空转${args.signals.hardFail ? "✓" : "✗"} ` +
    `错提交${args.signals.hallucSubmit ? "✓" : "✗"}\n`;
  parts.push(head);
  parts.push(
    `# 各分支核心日志（按分支分组）\n${buildBystanderLogView(args.entries, { branches: args.branchNames }) || "（暂无）"}\n`,
  );
  if (args.recentCmds.length) {
    parts.push(
      `# 卡塞分支近期命令\n${args.recentCmds
        .slice(-8)
        .map((c) => `- ${c}`)
        .join("\n")}\n`,
    );
  }
  parts.push(`# 卡塞分支最近对话摘要\n${args.recentDigest || "（无）"}\n`);
  parts.push("请给出一段≤120字的全局指引。");
  return parts.join("\n");
}

/** 卡塞分支最近消息摘要（N 条，截断），给旁观者看结构不看全文。 */
export function recentDigest(
  messages: Array<{ role: string; content?: unknown; tool_calls?: unknown }>,
  n = 10,
): string {
  const tail = messages.slice(-n);
  const lines: string[] = [];
  for (const m of tail) {
    let text = "";
    if (typeof m.content === "string") text = m.content;
    else if (Array.isArray(m.content)) {
      text = m.content
        .map((p) =>
          p && typeof p === "object" && "text" in p
            ? String((p as { text: unknown }).text ?? "")
            : "",
        )
        .join(" ");
    }
    if (m.tool_calls) {
      const ids = (m.tool_calls as Array<{ function?: { name?: string } }>).map(
        (t) => t.function?.name ?? "?",
      );
      text = `[tool_calls: ${ids.join(",")}] ${text}`;
    }
    text = text.replace(/\s+/g, " ").trim().slice(0, 220);
    lines.push(`- ${m.role}: ${text || "(空)"}`);
  }
  return lines.join("\n");
}

/** 插嘴注入消息（尾部追加用，append-only 保缓存）。 */
export function bystanderMessage(advice: string): string {
  return `[旁观者提示] ${advice.trim()}`;
}
