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
3. **止损判断**：如果同一思路已消耗大量轮次，明确说"这条路放弃，改攻 X"——
   X 必须是**当前题目内**的另一个目标/层面/方法。

**职责边界（硬约束）**：你只针对**当前题目**给题内方向与止损建议。
**严禁建议换题、跳过/放弃当前题、优先做其他题、调用或探测平台接口**——
选题与调度是编排控制器的职责，执行者被任务卡锁定在单题上、没有换题能力，
此类建议是无效幻觉（run5 实测 12 条注入里 8 条陷在换题循环里空耗）。
「止损」永远指**题内换方法/换层面/换子目标**，不是换目标题目。
4. **幻觉与错误前提核对**：对照各卡与进度找矛盾——声称拿到的证据在别处被自己证伪、
   连续提交都 correct:false 仍坚持、反复连接同一个不存在的目标（Connection refused 刷屏）。
   指出"你可能在臆测/打错目标"，并要求回到有工具结果支撑的事实。

要求：
- 只输出**一段简短中文**（≤120 字），直接给指引，不要客套、不要复述日志、不要输出命令块。
- 指引要具体到"下一步该往哪个方向想"，但**不要**替它写具体命令/payload。
- 你只读运行时派生的核心日志——不要猜测题目答案，不要编造日志里没有的事实。`;

export interface StallSignals {
  flagStall: boolean; // ⓪ 题级主信号：长时间没 flag（以 flag 为核心的介入锚）
  stall: boolean; // ① 分支距上次 core_log/flag 停滞超阈值
  repeat: boolean; // ② 近期同一命令 ≥3 次
  hardFail: boolean; // ③ 近期反复连接/解析/命令缺失失败=对幻觉目标空转
  hallucSubmit: boolean; // ④ 连续 correct:false=幻觉/错误提交
  count: number; // 成立数（≥2 触发）
}

/** 五信号评估（任二成立）。④⑤为幻觉/无用功的事实维度（零 LLM 成本）。纯函数可单测。
 * budget 语义（2026-01-16 修正）：段过半 + **距上次核心事件也超半段**——
 * 旧条件「段内零核心事件」会被段首的一张卡永久作废（arena 实测：08:00 记首卡后
 * 旁观者整段失明 15+ 分钟，nudge 额度同时耗尽）。 */
export function evaluateStall(input: {
  sinceCoreSec: number; // 分支距上次 core_log/flag 的秒数
  stallSec: number; // 分支停滞阈值（短题/长题分档）
  flagSinceSec: number; // 题级：距上一个新 flag 的秒数
  flagStallSec: number; // flag 主时钟阈值（短题/长题分档）
  recentCmds: string[]; // 分支近期命令（已归一化）
  recentHardFails?: boolean[]; // 近期工具结果硬失败标记（滚动，信号③）
  wrongSubmits?: number; // 连续 correct:false 次数（信号④）
}): StallSignals {
  // ⓪ flag 主时钟（题级）：黑板一直在记卡≠真进展——run3 盲区（记卡型空转永不触发停滞）
  const flagStall = input.flagSinceSec >= input.flagStallSec;
  const stall = input.sinceCoreSec >= input.stallSec;
  const counts = new Map<string, number>();
  for (const c of input.recentCmds) counts.set(c, (counts.get(c) ?? 0) + 1);
  let repeat = false;
  for (const n of counts.values()) if (n >= 3) repeat = true;
  // ③ 近8次连接/解析/命令缺失类硬失败≥3（幻觉端口/错误前提空转）；④ 连续错提交≥2
  const hardFail = (input.recentHardFails ?? []).slice(-8).filter(Boolean).length >= 3;
  const hallucSubmit = (input.wrongSubmits ?? 0) >= 2;
  // count 只计分支级四信号（任二）；flagStall 一票独立（长时间没 flag 就介入——
  // 否则「记卡不停但没 flag」型空转永远凑不齐二信号）
  const count = [stall, repeat, hardFail, hallucSubmit].filter(Boolean).length;
  return { flagStall, stall, repeat, hardFail, hallucSubmit, count };
}

/** 综合触发判定：flag 主时钟一票 或 分支级四信号任二。 */
export function bystanderShouldFire(sig: StallSignals): boolean {
  return sig.flagStall || sig.count >= 2;
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
    `- 触发信号：无flag${args.signals.flagStall ? "✓" : "✗"} 分支停滞${args.signals.stall ? "✓" : "✗"} ` +
    `重复${args.signals.repeat ? "✓" : "✗"} 连接空转${args.signals.hardFail ? "✓" : "✗"} ` +
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
  parts.push(
    "请给出一段≤120字的全局指引。\n" +
      "**末尾锚定：只准是当前题目内的下一步（换方法/换层面/换子目标）；" +
      "任何换题、做其他题、动平台接口的建议都视为越权，不要输出。**",
  );
  return parts.join("\n");
}

/** 越权建议检测（注入前过滤）：换题/其他题/题名形态——保守命中即让 LLM 重写一次。
 * run5 实测 8/12 注入含此类（「转攻 web_sqli_01」「转向该 SQLi 分支」等）。 */
export function isOverreachingAdvice(advice: string): boolean {
  return /(换题|做别的题|其他题|别的题|未动的.{0,8}[题挑]|转攻\s+[A-Za-z]|转攻.{0,8}(未动|别的|其他)|转向该|转\s+[A-Za-z][A-Za-z0-9_-]+|停止.{0,6}本题|放弃本题|平台\s*(api|接口|端口).{0,10}(调用|交互|提交))/i.test(
    advice,
  );
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
