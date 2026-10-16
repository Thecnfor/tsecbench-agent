/** 共享类型契约：全部模块（agent/bench/records/shell/panel）统一用这套形状。 */

/** OpenAI 兼容 tool call（与平台网关 wire 格式一致） */
export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

/** 多模态 content part（shell action=view 的返回与对话重建用） */
export type ContentPart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string; detail: string } };

/** 发送给模型 / 从记录重建的消息 */
export interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content?: string | ContentPart[] | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

/** chat completion 响应（只取用到的字段，形状对齐 openai SDK） */
export interface ChatResponse {
  choices: Array<{
    message: {
      role: string;
      content?: string | null;
      tool_calls?: ToolCall[];
      reasoning_content?: string | null;
    };
  }>;
  usage?: Record<string, unknown> | null;
}

/** LLM 客户端抽象：agent 只依赖它，测试注入 fake（对应 Python 的 fake client） */
export interface LlmClient {
  create(opts: {
    model: string;
    messages: ChatMessage[];
    tools?: unknown[];
    tool_choice?: string;
    temperature?: number;
    max_tokens?: number;
    /** DeepSeek 思考控制：轻调用（旁观者/复盘/冒烟）传 {type:"disabled"} 省 completion */
    thinking?: { type: "disabled" } | { type: "enabled"; [k: string]: unknown };
    /** 思考档位（顶层字段 low|medium|high）：按题目分档（短题 low / 长题默认档） */
    reasoning_effort?: string;
    signal?: AbortSignal;
  }): Promise<ChatResponse>;
}

/** 平台题目对象（GET /openapi/v1/challenges 元素） */
export interface Challenge {
  unique_code: string;
  description: string;
  difficulty: string;
  level?: number;
  total_score: number;
  flag_count: number;
  correct_flag_count: number;
  is_completed: boolean;
  container_status: string;
  container_addr?: string[] | string;
}

/** conversation.jsonl 单条记录（写入端全量、读取端容忍缺字段） */
export interface ConvRecord {
  ts: string;
  seq: number;
  code: string;
  kind: "system" | "user" | "assistant" | "tool" | "note" | "error";
  branch?: number; // fork 多分支：0=trunk（缺省视为 0，兼容旧记录）
  started_at: number;
  ended_at: number;
  duration_ms: number;
  content?: string | ContentPart[];
  output?: string | null;
  thinking?: string | null;
  tool_calls?: Array<{ id: string; name: string; arguments: string }>;
  tool_call_id?: string;
  tool?: string;
  arguments?: unknown;
  result?: unknown;
  kind_note?: string;
  parent_branch?: number; // kind_note=fork_directive 时的父分支
  fork_seq?: number; // fork 时刻父分支的 conv seq（重建分界）
  error?: string;
  usage?: unknown;
  turn?: string;
  [k: string]: unknown;
}

/** tasks.jsonl 单条记录 */
export interface TaskRecord {
  ts: string;
  phase: "task_start" | "task_close";
  code: string;
  started_at?: string;
  closed_at?: string;
  target?: string;
  attempt?: number;
  resume?: boolean;
  parallel?: number;
  flags?: string;
  duration_sec?: number;
  outcome?: "pass" | "fail" | "paused";
  summary?: string;
  [k: string]: unknown;
}
