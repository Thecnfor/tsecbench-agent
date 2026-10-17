/** 集中配置：双运行模式（local/hosted）+ 自动检测 + 平台凭证。
 *
 * 模式说明：
 * - local  本地模式：公网直连官方 DeepSeek API（开发调试用）
 * - hosted 托管模式：官方平台内网转写网关，无公网（最终交付形态，镜像 ≤3GB）
 * - auto   自动检测（默认）：先结构信号，再探测本地网关可达性，失败再试托管网关。
 *
 * 配置优先级：真实环境变量 > 项目根 .env > 内置默认值。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// --- 加载项目根 .env（不覆盖已存在的真实环境变量） ---
// 模块位于 tsec_agent/{src/,dist/} → 项目根 = 上两级；找不到时从模块目录逐级上找（最多 5 级）。
function loadDotenv(): void {
  let dir = path.dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 5 && dir !== path.dirname(dir); i++) {
    const p = path.join(dir, ".env");
    if (fs.existsSync(p)) {
      applyDotenv(p);
      return;
    }
    dir = path.dirname(dir);
  }
}

function applyDotenv(file: string): void {
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const s = line.trim();
    if (!s || s.startsWith("#") || !s.includes("=")) continue;
    const k = s.slice(0, s.indexOf("=")).trim();
    const v = s.slice(s.indexOf("=") + 1).trim();
    if (k && !(k in process.env)) process.env[k] = v;
  }
}

loadDotenv();

export function env(name: string, def: string): string {
  const v = process.env[name];
  return v !== undefined && v !== "" ? v : def;
}

// --- 托管模式转写网关通用转换 ---
// 托管环境所有大模型 API 走转写网关：https://HOST[/path] → http://HOST.tsecbench.gw[/path]。
export const GATEWAY_SUFFIX = ".tsecbench.gw";

/** https://api.deepseek.com/v1 → http://api.deepseek.com.tsecbench.gw/v1（幂等）
 *
 * 严格按官方转写规则：
 * - 只转写 **https://**；http:// 视为已显式给好的内网/转写地址原样保留；
 * - **IP 字面量不转写**；已是 .tsecbench.gw 的地址原样返回（防重复转写）。
 */
export function toGatewayUrl(baseUrl: string): string {
  const m = /^https:\/\/([^:/]+)(:\d+)?(\/.*)?$/.exec(baseUrl.trim());
  if (!m) return baseUrl;
  const host = m[1];
  const port = m[2] ?? "";
  const p = m[3] ?? "";
  if (host.endsWith(GATEWAY_SUFFIX)) return baseUrl;
  if (/^[0-9.]+$/.test(host)) return baseUrl; // IPv4 字面量
  return `http://${host}${GATEWAY_SUFFIX}${port}${p}`;
}

// --- 两套模型后端 ---
export const LOCAL_BASE_URL = env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1");
export const LOCAL_API_KEY = env("DEEPSEEK_API_KEY", "");
export const LOCAL_MODEL = env("AGENT_MODEL_LOCAL", "deepseek-flash");

// hosted：显式配置优先（https 形式按官方规则转写，http/IP 原样保留），否则由本地官方地址转换
export const HOSTED_BASE_URL = toGatewayUrl(env("DEEPSEEK_BASE_URL_HOSTED", LOCAL_BASE_URL));
// hosted key：显式 DEEPSEEK_API_KEY_HOSTED 优先；否则复用本地 DEEPSEEK_API_KEY；都没有才回退 "tsec-bench"。
// 实测 2026-10-15：本届转写网关把 Authorization **透传给上游 DeepSeek** → 托管必须用真实 DeepSeek key。
export const HOSTED_API_KEY = env("DEEPSEEK_API_KEY_HOSTED", LOCAL_API_KEY) || "tsec-bench";
// hosted 模型与本地同名（实测确认：托管也是 deepseek-flash，无 v4.1/expires 后缀）
export const HOSTED_MODEL = env("AGENT_MODEL_HOSTED", LOCAL_MODEL);

export const PROBE_TIMEOUT = Number(env("AGENT_PROBE_TIMEOUT", "8"));

/** GET {base}/models 探测网关可达性（2xx/4xx 都算可达，只看网络层）。
 *
 * HTTP 错误响应（4xx/5xx）也是"网络层可达"的证据——曾因未区分导致 hosted 转写网关
 * 对 /models 回 404/405 时被误判为不可达 → detect_mode 误判成 local → 冒烟必挂。
 */
export async function reachable(baseUrl: string): Promise<boolean> {
  const url = `${baseUrl.replace(/\/+$/, "")}/models`;
  try {
    const resp = await fetch(url, {
      method: "GET",
      signal: AbortSignal.timeout(PROBE_TIMEOUT * 1000),
    });
    return resp.status >= 200 && resp.status < 500;
  } catch {
    return false;
  }
}

/** 零成本结构信号（不发网络请求）：平台注入的 BENCHMARK_BASE_URL 是否已是转写形态。 */
export function benchLooksHosted(): boolean {
  const url = env("BENCHMARK_BASE_URL", "");
  const m = /^[a-z]+:\/\/([^:/]+)/i.exec(url.trim());
  return !!m && m[1].endsWith(GATEWAY_SUFFIX);
}

/** 返回最终生效模式：'local' 或 'hosted'。 */
export async function detectMode(): Promise<string> {
  const mode = env("AGENT_MODE", "auto").toLowerCase();
  if (mode === "local" || mode === "hosted") return mode;
  // 1) 结构信号优先：平台注入转写形态 bench URL = 托管环境实锤，直接判 hosted（零网络）
  if (benchLooksHosted()) return "hosted";
  // 2) auto：先 local（公网），后 hosted（内网）；hosted 探测给两次机会（启动初期 DNS 抖动）
  if (LOCAL_API_KEY && (await reachable(LOCAL_BASE_URL))) return "local";
  if ((await reachable(HOSTED_BASE_URL)) || (await reachable(HOSTED_BASE_URL))) return "hosted";
  // 3) 兜底：托管镜像不带 .env → 没有本地 key，local 必然跑不了 → 判 hosted 由冒烟报错
  if (!LOCAL_API_KEY) return "hosted";
  return "local";
}

export function backendFor(mode: string): [string, string, string] {
  if (mode === "hosted") return [HOSTED_BASE_URL, HOSTED_API_KEY, HOSTED_MODEL];
  return [LOCAL_BASE_URL, LOCAL_API_KEY, LOCAL_MODEL];
}

export interface Config {
  mode: string;
  benchmark_token: string;
  benchmark_base_url: string;
  vpn_health_url: string;
  max_turns: number;
  // 分段解题的【安全网】闸：段主闸是墙钟时间（segment_*_sec），轮数只防极端空转（0=关闭）。
  // 为什么不用轮数当主闸：实测段墙钟 p50=4.7min、p90=28.9min、max=158min——同样 60 轮
  // 墙钟差 30 倍，因为耗时由工具等待（nmap/爆破的 wait_sec）主导，与轮数无关。
  task_max_turns: number;
  // 段墙钟预算（主闸）：短题 600s / 长题 1800s（归档实测短段 p50=3.2min、长段 p50=19min p90=45min，
  // 档位落在 p75~p90，检查点不至于太碎增加重启成本，也不至于太粗失控）。
  segment_short_sec: number;
  segment_long_sec: number;
  // 通关后 AI 复盘的时间上限（秒）：容器此时已关闭、槽位已释放，总结脱槽跑不占并发名额；
  // 超时丢弃总结照常记 pass——总结失败绝不能拖累通关，也不能让 agent 无限烧 token。
  summary_timeout_sec: number;
  /** 上下文压缩阈值（字符估算 ≈ messages 总长；超过即压缩交接。0=关） */
  compact_chars: number;
  // 暂停题续跑 aging（秒）：排队超过此时长的暂停题提权，防止 fresh 无限插队
  //（实测曾有题暂停后等 118 分钟才续跑）。
  resume_aging_sec: number;
  // 平台 max-active 硬上限（平台文档值 3）：并发上限取 min(AGENT_MAX_PARALLEL, 本值)。
  // 教训：本地意愿(5) 虚高时首轮长题配额按 cap-1=4 发 start，平台只放行 3 个 →
  // b-01/b-02/b-03 三长占满全部槽，短题一个没轮到（用户实测）。
  platform_capacity: number;
  pause_max: number;
  giveup_max: number;
  max_failures: number;
  tasks: string;
  deadline_sec: number;
  request_timeout: number;
  api_max_retries: number;
  llm_retry_mode: string;
  llm_retry_max_delay: number;
  shell_default_wait_ms: number;
  shell_max_output: number;
  shell_hard_sec: number;
  shell_max_jobs: number;
  vision: string;
  vision_max_kb: number;
  vision_ok: boolean | null;
  // fork 多分支（默认关）
  fork_enabled: boolean;
  max_branches: number;
  fork_max: number;
  fork_cooldown_sec: number;
  branch_prune_sec: number;
  broadcast_max: number;
  // 旁观者（默认关）
  bystander_enabled: boolean;
  bystander_max: number;
  bystander_cooldown_sec: number;
  stall_short_sec: number;
  stall_long_sec: number;
  flag_stall_short_sec: number;
  flag_stall_long_sec: number;
  // core_log 轮级轻推（执行层兜底）
  core_log_nudge_rounds: number;
  core_log_nudge_max: number;
  // 思考档位分档（空=不发=模型默认档）
  reasoning_effort_short: string;
  reasoning_effort_long: string;
  extra_env: Record<string, string>;
  llm_base_url: string;
  llm_api_key: string;
  model: string;
}

function num(name: string, def: string): number {
  return Number(env(name, def));
}

export const CFG: Config = {
  // --- 运行模式（惰性解析，默认 auto 探测） ---
  mode: "",

  // --- TSec Benchmark 平台 ---
  benchmark_token: env("BENCHMARK_TOKEN", ""),
  benchmark_base_url: env("BENCHMARK_BASE_URL", "https://tsecbench.zc.tencent.com"),
  vpn_health_url: env("VPN_HEALTH_URL", "http://10.0.100.58"),

  // --- 运行控制 ---
  max_turns: num("AGENT_MAX_TURNS", "400"),
  // 分段解题：段主闸是墙钟时间（见下），轮数只是安全网（0=关闭）。
  task_max_turns: num("AGENT_TASK_MAX_TURNS", "120"),
  segment_short_sec: num("AGENT_TASK_SEGMENT_SHORT_SEC", "600"),
  segment_long_sec: num("AGENT_TASK_SEGMENT_LONG_SEC", "1800"),
  summary_timeout_sec: num("AGENT_SUMMARY_TIMEOUT_SEC", "300"),
  compact_chars: num("AGENT_COMPACT_CHARS", "120000"), // ≈30k tok（128k 窗口留足余量）
  resume_aging_sec: num("AGENT_RESUME_AGING_SEC", "600"),
  platform_capacity: num("AGENT_PLATFORM_CAPACITY", "3"),
  pause_max: num("AGENT_PAUSE_MAX", "0"), // 每题最多暂停续跑次数，0=不限
  giveup_max: num("AGENT_GIVEUP_MAX", "3"), // 连续"未通关就收尾"上限，0=不限
  max_failures: num("AGENT_MAX_FAILURES", "2"), // 真失败放弃上限（≥1，0 按 1）
  // 只解部分题目（测试用）：逗号/空白分隔，支持 * ? 通配；空=全部。
  tasks: env("AGENT_TASKS", ""),
  deadline_sec: num("AGENT_DEADLINE_SEC", "0"),
  request_timeout: num("AGENT_REQUEST_TIMEOUT", "300"),
  api_max_retries: num("AGENT_API_RETRIES", "5"),
  // LLM 重试模式：forever=网络类错误无限退避重试直到任务时限；finite=按次数后放弃。
  llm_retry_mode: env("AGENT_LLM_RETRY_MODE", "forever").toLowerCase(),
  llm_retry_max_delay: num("AGENT_LLM_RETRY_MAX_DELAY", "60"),

  // shell 工具
  shell_default_wait_ms: num("AGENT_SHELL_WAIT_MS", "1500"),
  // 单条工具结果进上下文的字符上限：8192（≈2k token）。曾为 32768——历史归档实测
  // tool.result 占对话字节 29-40%，是上下文膨胀（f2-05 实测 88-143×）与 thinking
  // 成本（占单题成本 37-69%）的第二大源头；截断头尾保留 + 结果自带日志路径，
  // 全量可 tail（out_file/分页兜底既有机制）。ANSI 转义实测为 0（管道非 TTY 禁色），无需剥离。
  shell_max_output: num("AGENT_SHELL_MAX_OUTPUT", "8192"),
  // 后台命令硬上限：到点整组 kill（有 out_file 兜底，杀掉不丢数据）；0=永不杀
  shell_hard_sec: num("AGENT_SHELL_HARD_SEC", "1800"),
  // 同时在跑的后台 job 上限（满了拒绝新 run）
  shell_max_jobs: num("AGENT_SHELL_MAX_JOBS", "8"),

  // --- 多模态（shell action=view 读图）---
  // auto=先按图片发，网关 400 拒图时本会话降级为文本（只有元数据）
  vision: env("AGENT_VISION", "auto").toLowerCase(),
  vision_max_kb: num("AGENT_VISION_MAX_KB", "4096"),
  vision_ok: null,

  // --- fork 多分支（../docs/fork-bystander-design.md §7，默认全关） ---
  fork_enabled: ["1", "on", "true"].includes(env("AGENT_FORK", "0").toLowerCase()),
  max_branches: num("AGENT_MAX_BRANCHES", "3"), // 存活分支上限（含 trunk）
  fork_max: num("AGENT_FORK_MAX", "5"), // 每题 fork 总数硬上限
  fork_cooldown_sec: num("AGENT_FORK_COOLDOWN_SEC", "180"),
  // 1200（B 方案）：长题停滞 600s+二信号 900s 触发旁观者后仍留 300s 救治窗，
  // 救不活才剪——两阈值曾同为 600（剪枝先执行=分支旁观者窗口为 0，靠巧合而非设计）
  branch_prune_sec: num("AGENT_BRANCH_PRUNE_SEC", "1200"), // corelog/flag 双停滞剪枝
  broadcast_max: num("AGENT_BROADCAST_MAX", "8"), // 每段每分支广播上限

  // --- 旁观者（默认关；依赖 core_log 工具，可单独于 fork 打开） ---
  bystander_enabled: ["1", "on", "true"].includes(env("AGENT_BYSTANDER", "0").toLowerCase()),
  bystander_max: num("AGENT_BYSTANDER_MAX", "2"), // 每段每分支旁观者上限
  bystander_cooldown_sec: num("AGENT_BYSTANDER_COOLDOWN_SEC", "400"),
  // 组合信号（任二成立触发）的分档阈值：停滞时长按题目分类取值
  stall_short_sec: num("AGENT_STALL_SHORT_SEC", "300"),
  stall_long_sec: num("AGENT_STALL_LONG_SEC", "600"),
  // flag 主时钟（题级，旁观者核心信号）：长时间没 flag 就介入
  flag_stall_short_sec: num("AGENT_FLAG_STALL_SHORT_SEC", "400"),
  flag_stall_long_sec: num("AGENT_FLAG_STALL_LONG_SEC", "900"),
  // core_log 执行层兜底（轮级轻推，零 LLM 调用）：连续 N 轮无 core_log →
  // 在该轮最后一个工具结果尾部附提醒；每段每分支最多 MAX 次，之后交给旁观者。
  core_log_nudge_rounds: num("AGENT_CORE_LOG_NUDGE_ROUNDS", "3"),
  core_log_nudge_max: num("AGENT_CORE_LOG_NUDGE_MAX", "2"),

  // 思考档位分档（顶层 reasoning_effort；空=不发=模型默认档，空即特性开关）：
  // 短题（classifyTask：单 flag 且非 hard 且 level<3）默认 low 省 thinking——
  // thinking 实测占单题成本 37-69%（跨 4 run 归档）；长题不发（保持模型默认档不降质）。
  reasoning_effort_short: env("AGENT_REASONING_EFFORT_SHORT", "low"),
  reasoning_effort_long: env("AGENT_REASONING_EFFORT_LONG", ""),

  // 追加到 shell 子进程环境的变量
  extra_env: {
    BENCHMARK_TOKEN: env("BENCHMARK_TOKEN", ""),
    BENCHMARK_BASE_URL: env("BENCHMARK_BASE_URL", "https://tsecbench.zc.tencent.com"),
    NO_PROXY: "127.0.0.1",
    no_proxy: "127.0.0.1",
  },

  // 模型后端（resolve 后填充）
  llm_base_url: "",
  llm_api_key: "",
  model: "",
};

/** 主入口（CLI 用；行为与 Python detect_mode 一致，含网络探测与兜底）。 */
export async function resolveAsync(): Promise<Config> {
  CFG.mode = await detectMode();
  const [b, k, m] = backendFor(CFG.mode);
  CFG.llm_base_url = b;
  CFG.llm_api_key = k;
  CFG.model = m;
  return CFG;
}
