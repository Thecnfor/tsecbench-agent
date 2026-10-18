/** 卡塞信号（程序侧，零 LLM 成本）：planner 用它决定提醒/回收。
 *
 * 原planner LLM 已移除；判定逻辑保留在这里，供 controller 生成 planner 事件。
 */
export interface StallSignals {
  flagStall: boolean; // 题级主信号：长时间没 flag
  stall: boolean; // Attempt 距上次 log/flag 停滞超阈值
  repeat: boolean; // 近期同一命令 ≥3 次
  hardFail: boolean; // 近期反复连接/解析/命令缺失失败
  hallucSubmit: boolean; // 连续 correct:false
  count: number; // 分支级成立数（≥2 触发）
}

/** 五信号评估（flag 主时钟一票；其余任二）。纯函数可单测。 */
export function evaluateStall(input: {
  sinceCoreSec: number;
  stallSec: number;
  flagSinceSec: number;
  flagStallSec: number;
  recentCmds: string[];
  recentHardFails?: boolean[];
  wrongSubmits?: number;
}): StallSignals {
  const flagStall = input.flagSinceSec >= input.flagStallSec;
  const stall = input.sinceCoreSec >= input.stallSec;
  const counts = new Map<string, number>();
  for (const c of input.recentCmds) counts.set(c, (counts.get(c) ?? 0) + 1);
  let repeat = false;
  for (const n of counts.values()) if (n >= 3) repeat = true;
  const hardFail = (input.recentHardFails ?? []).slice(-8).filter(Boolean).length >= 3;
  const hallucSubmit = (input.wrongSubmits ?? 0) >= 2;
  const count = [stall, repeat, hardFail, hallucSubmit].filter(Boolean).length;
  return { flagStall, stall, repeat, hardFail, hallucSubmit, count };
}

/** 综合触发：flag 主时钟一票，或 Attempt 级四信号任二。 */
export function shouldIntervene(sig: StallSignals): boolean {
  return sig.flagStall || sig.count >= 2;
}

/** 卡塞 Attempt 最近消息摘要（给 planner/日志看结构，不塞全文）。 */
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

/** planner 提醒文本（程序生成，不调用 LLM；尾部追加保缓存）。 */
export function plannerNudgeText(args: {
  attempt: number;
  sinceCoreSec: number;
  signals: StallSignals;
  recentCmds: string[];
}): string {
  const flags: string[] = [];
  if (args.signals.stall) flags.push(`${Math.round(args.sinceCoreSec)}s 无 log/flag`);
  if (args.signals.repeat) flags.push("近期命令重复");
  if (args.signals.hardFail) flags.push("连接/解析类硬失败反复出现");
  if (args.signals.hallucSubmit) flags.push("连续错提交");
  if (args.signals.flagStall) flags.push("题目长时间无新 flag");
  const cmds = args.recentCmds.slice(-3).join("；");
  return (
    `[planner 提醒] Attempt ${args.attempt} 出现卡塞信号：${flags.join("、") || "进展停滞"}。` +
    `请先 log 一张当前证据卡，然后二选一：① 换方法/换层面/缩小目标继续；` +
    `② 若方向已死，直接 report(deadend, ...) 结束，不要重复已证伪的命令。` +
    (cmds ? `最近命令：${cmds}` : "")
  );
}
