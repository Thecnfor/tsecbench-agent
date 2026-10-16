/** 共享类型契约：题包、docker 驱动接口、API 响应形状。
 *
 * 线格式严格对齐 CHALLENGES_API.md §5（官方原文）：arena 的存在意义就是让
 * tsec_agent 的 bench.ts 零改动从官方平台切过来。submit 响应必须带全字段
 * correct/awarded/cumulative_score——prompt.ts 依赖 `correct:true`（根仓库 mock/ 缺这几个字段，勿照抄）。
 */

/** GET /openapi/v1/challenges 的 container_status 取值（官方 §5.1） */
export type ContainerStatus = "stopped" | "pending" | "available" | "stop_pending";

/** challenge.json 解析后的题包（缺省值已填充、校验已通过） */
export interface ChallengeSpec {
  /** 题目目录（绝对路径）：docker build 用 */
  dir: string;
  unique_code: string;
  description: string | null;
  difficulty: string;
  level: number;
  total_score: number;
  hint: string | null;
  /** 取 hint 后每 flag 得分扣减比例（官方 hint_cost_radio 的本地语义），[0,1] */
  hint_cost_ratio: number;
  /** 计分真值；flag_count = flags.length；绝不经 API 暴露、不进 agent 镜像（.dockerignore 排除 arena/） */
  flags: string[];
  /** docker 镜像名；null = 用同目录 Dockerfile 现场 build（tag: tcbench-target/<code>:latest） */
  image: string | null;
  /** 容器内端口；arena 为每个分配一个宿主机端口（池 ARENA_PORT_MIN..MAX） */
  container_ports: number[];
  /** env = docker run -e FLAG_0.. 注入；none = flag 烤在镜像里（题包作者自保证与计分真值一致） */
  flag_inject: "env" | "none";
  /** 可选：TCP 通后追加 HTTP GET 探测的路径（收到任何响应即视为就绪） */
  ready_path: string | null;
}

/** docker 驱动句柄 */
export interface DriverHandle {
  code: string;
  /** 容器名（CliDriver 用它 docker rm -f） */
  name: string;
  /** 与 spec.container_ports 对齐的宿主机端口 */
  host_ports: number[];
}

/** docker 驱动抽象：CliDriver 真跑容器；FakeDriver 内存桩（测试与无 docker 演示） */
export interface Driver {
  run(spec: ChallengeSpec, hostPorts: number[]): Promise<DriverHandle>;
  waitReady(spec: ChallengeSpec, h: DriverHandle): Promise<void>;
  rm(h: DriverHandle): Promise<void>;
  /** 重启对账：label tcbench.arena=1 的容器 → unique_code → 宿主机端口列表 */
  listManaged(): Promise<Map<string, number[]>>;
  /** 启动时预热构建（失败只记日志，不阻塞服务） */
  prebuild?(spec: ChallengeSpec): Promise<void>;
}

/** GET /openapi/v1/challenges 元素（官方 §5.1） */
export interface ChallengeView {
  unique_code: string;
  description: string | null;
  difficulty: string;
  level: number;
  total_score: number;
  flag_count: number;
  correct_flag_count: number;
  is_completed: boolean;
  container_status: ContainerStatus;
  /** 仅 available 时非空（官方硬语义），值形如 <ARENA_ADVERTISE_HOST>:<宿主机端口> */
  container_addr: string[];
}

/** POST /submit 成功响应（官方 §5.4 全字段） */
export interface SubmitResult {
  correct: boolean;
  awarded: number;
  cumulative_score: number;
  correct_flag_count: number;
  total_flag_count: number;
  matched_flag_index: number | null;
}
