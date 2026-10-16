/** submit_flag toolcall：flag 提交的唯一通道（防代码爆破 + 结构化记录）。
 *
 * 设计（两新动作之一）：
 * - **防代码爆破**：提交走 toolcall 而非 shell——LLM 每次只能提交一个 flag、
 *   无法 for 循环批量穷举提交；shell 出口另做 `curl …challenges/submit` 拦截
 *   （isSubmitCurl）双保险，模型改走工具。
 * - **结构化记录**：controller 拿到平台原始响应（matched_flag_index 等）直接喂
 *   flag 账本/flag 主时钟/corelog flag 卡——不再依赖 shell 输出正则（保留兜底）。
 * - unique_code 由 controller 填（模型只给 flag 值，防跨题误提交）。 */

import type { BranchManager } from "./branch.js";
import type { CorelogWriter, FlagLedger } from "./corelog.js";

export const SUBMIT_FLAG_TOOL_SCHEMA = {
  type: "function",
  function: {
    name: "submit_flag",
    description:
      "Submit a flag captured from the TARGET (the ONLY submission channel — never " +
      "curl the submit endpoint from shell: scripted/bulk submissions are blocked). " +
      "Pass only the flag string read verbatim from the target; unique_code is filled " +
      "automatically for the current task. Returns the platform response with " +
      "correct / correct_flag_count / total_flag_count / matched_flag_index — " +
      "matched_flag_index is the stable flag id (index stays fixed across restarts).",
    parameters: {
      type: "object",
      properties: {
        flag: {
          type: "string",
          description: "The flag string exactly as read from the target system.",
        },
      },
      required: ["flag"],
    },
  },
};

/** shell 命令是否在 curl 提交接口（执行层拦截：提交只走 submit_flag）。 */
export function isSubmitCurl(command: string): boolean {
  return /curl\b[^|;&]*challenges\/submit/i.test(command);
}

export interface SubmitOutcome {
  result: string; // 给模型的工具结果文本
  newFlag: boolean; // 平台确认的新 flag（账本/时钟/卡的触发器）
  correctCount: number;
  totalCount: number;
  flagIndex: number | null;
}

/** 平台响应 → 工具结果 + 账本集成。caller 负责 bm.recordFlag/corelog（因为要 now/conv） */
export function integrateSubmit(resp: any, ledger: FlagLedger, branchId: number): SubmitOutcome {
  const correct = resp?.correct === true;
  const idx =
    typeof resp?.matched_flag_index === "number" ? (resp.matched_flag_index as number) : null;
  const correctCount = Number(resp?.correct_flag_count ?? ledger.count());
  const totalCount = Number(resp?.total_flag_count ?? 0);
  if (totalCount) ledger.setTotal(totalCount);
  const newFlag = correct && idx !== null && ledger.add(idx, branchId);
  return {
    result: JSON.stringify(resp),
    newFlag,
    correctCount,
    totalCount,
    flagIndex: idx,
  };
}

/** flag 成功后的总结轮指示（附加在 submit_flag 结果尾）——两版按题型分流：
 * 多 flag：总结用于压缩（原文→总结，累积链）并继续；
 * 单 flag：同样产出总结，但不执行替换（无新内容要接），**该总结即收尾**。 */
export const FLAG_SUMMARY_HINT_MULTI =
  "\n【系统】该 flag 已确认。请输出此 flag 的探索总结（≤120 字：走了哪条路径/用什么手法/踩了什么坑），只输出这段总结文字、不调用工具——系统会用它压缩此前的探索上下文并让你继续下一个 flag。";
export const FLAG_SUMMARY_HINT_SINGLE =
  "\n【系统】全部 flag 已确认。请输出本题的探索总结（≤120 字：走了哪条路径/用什么手法/踩了什么坑），只输出这段总结文字、不调用工具——它将作为本题收尾。";
