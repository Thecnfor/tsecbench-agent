# TSec Benchmark Agent（Node/TS 版）

用 **Node 26 + TypeScript**（pnpm + vite + vitest）实现的自动跑分 agent，运行于 **Docker（Kali Linux）** 容器内：
- 模型：`deepseek-flash`（OpenAI 兼容网关；local=官方公网 API，hosted=平台内网转写网关，自动探测切换）
- 工具：仅一个 **异步 shell**（后台执行 + job 轮询 + 多模态读图），无 MCP、无 subagent
- 运行时**零第三方依赖**：LLM 调用用原生 `fetch`（无 openai SDK），镜像里只带 `pnpm build` 产出的纯 JS（不带 node_modules）
- 观察面板：**React + Node 服务**（读 `runs/<TOKEN>/` 记录 + 平台 API，浏览器访问）

## 目录模型（排查"面板没数据"先看这里）

```
docker/runs/<TOKEN>/          ← bench 记录根（= 容器内 /work，agent 的 RECORDS_DIR）
├── meta.json                 # bench 元信息 + runs[]（每次运行起止/结果）
├── events.jsonl              # run 级事件
├── tasks.jsonl               # 题目生命周期（task_start/task_close）
└── tasks/<code>/             # 每题目录：conversation.jsonl 与该题 shell 工作目录【同居一处】
docker/work/                  # 遗留目录，当前 run.sh 已不用
```

- **agent 写哪**（`records.ts: pickRecordsDir`）：显式 `RECORDS_DIR` > 容器内 `/work`
  （run.sh 把 `runs/<TOKEN>` 挂载为 `/work`；hosted 形态 entrypoint 自建）> 宿主机回退 cwd。
- **面板读哪**（`panel/data.ts: findLatestRuns`）：从模块/cwd 向上找 `docker/runs`，
  候选里只认有 `meta.json`/`tasks.jsonl` 的目录，按**记录文件 mtime** 选最新
  （不看目录 mtime——run.sh 先 mkdir 再跑容器，空目录 mtime 反而最新会抢跑）。
- `--dir` 两种形态都行：`--dir runs/<TOKEN>` 或 `--dir runs/`（自动挑最新 bench）。
- 目录为空时面板给出诊断（缺 meta.json/tasks.jsonl 或刚启动还没写 tasks.jsonl），不干显示 0。
- 每题工作目录基址派生自 `RECORDS_DIR`（`tasks/<code>/`），保证 conversation 与 shell
  产物永远不因 `RECORDS_DIR` 改指而分家；`TASK_WORKDIR` 可整体覆盖。

## 命令

```bash
pnpm install          # 首次
pnpm build            # 构建 agent（dist/agent.js、dist/panel-server.js）+ 面板（dist/panel/）
pnpm test             # 离线自测（50 个用例，不依赖 VPN/网关/平台）
pnpm check            # 提交前自查：tsc --noEmit + biome check + pnpm test
pnpm typecheck        # 仅类型检查
pnpm lint             # biome 检查（配置 biome.jsonc）
pnpm fmt              # biome 自动格式化 + 安全修复
pnpm serve            # 起观察面板 → http://127.0.0.1:8791（自动找最新 runs/<TOKEN>）
pnpm serve -- --dir ../docker/runs/<TOKEN> --port 8791 --url https://...
pnpm dev              # vite dev server（5173，/api 代理到 8791）
node dist/agent.js --smoke               # 冒烟：网关连通 + shell 工具 + VPN 预检 + 平台题库
node dist/agent.js --mode local --task b-0*   # 只解指定题（调试）
```

镜像构建：**先 `pnpm build`，再** `cd ../docker && docker build -f Dockerfile -t tsec-agent:latest ..`
（镜像只 COPY `dist/` 产物；`node_modules`/`src`/`test` 不进镜像；Dockerfile 构建阶段
`node --check dist/agent.js` 硬验证，dist 缺失 = 构建失败。交付 `docker save` 导出的
就是这份 dist——改完代码忘记 `pnpm build` 会 save 出旧产物）。构建后端依赖见下节。
启动与观察见 `../docker/run.sh` 与 `../README.md`。

## 技术选型

| 领域 | 选择 | 理由 / 备选 |
| --- | --- | --- |
| 运行时 | **Node 26 + TypeScript**（strict） | 交付镜像里 Kali apt 的 nodejs 即可运行；`AbortSignal.any` 等新 API 加了降级保护（实测容器内 Node v24.21.0 无问题） |
| shell 工具 | **Node 内置 `node:child_process`（spawn）** | 无第三方：`detached: true` 得独立进程组、`process.kill(-pid)` 整组 kill、stdio 直写文件做输出落盘与截断；每题独立 cwd 用内置 `node:async_hooks` 的 AsyncLocalStorage（替代 Python 的 thread-local） |
| LLM 调用 | **原生 `fetch`**（无 openai SDK） | 网关是 OpenAI 兼容协议，一个 POST 就够；省掉 SDK 意味着**镜像运行时零第三方依赖**（不带 node_modules）；匿名候选 = 直接不带 Authorization 头（比 Python 版 httpx 钩子剥头更干净）；401 热切换/重试策略在 `llm.ts` 自实现 |
| 平台 API | 原生 `fetch` + `AbortSignal.timeout` | 同上，无 axios/undici 显式依赖 |
| 记录持久化 | `node:fs`（appendFileSync JSONL + 原子 rename 写 meta） | 与 Python 版产物格式逐字节兼容，panel/续跑重建共用 |
| 归档基线读取 | 系统 `tar`（`execFileSync`） | Node 无内置 tar.gz；容器/宿主机都有 GNU tar，不引 node-tar |
| 压图 / OCR | 系统 `python3 -c PIL` / `tesseract` 子进程 | 与 Python 版一致（镜像已带 python3-pil 与 tesseract-ocr），不引 sharp/tesseract.js（体积与镜像约束） |
| 前端 | **React 19 + Vite** | 只在构建期出现在 devDependencies；产物是静态文件，由 Node 服务托管 |
| 面板服务 | Node 内置 `node:http` | 只读记录 + 转发平台 API，无 express |
| 测试 | **Vitest** | 与 Vite 共用配置；`test/setup.ts` 在模块图加载前隔离宿主 `.env`；e2e 用 fake client + `vi.mock` 平台层，全离线 |
| 格式化/Lint | **Biome**（`biome.jsonc`） | 单二进制、快；formatter + linter 一体；lineWidth 100 对齐仓库 Python 侧 ruff 口径；`noExplicitAny`/`noNonNullAssertion` 关闭（JSONL 载荷松散类型，类型安全由 strict tsc 把关） |
| 包管理 | **pnpm** | 硬盘去重；`pnpm-workspace.yaml` 里 `allowBuilds: esbuild`（只放行 esbuild 的 postinstall） |

## 结构（src/）

| 文件 | 职责 |
| --- | --- |
| `agent.ts` | 控制器主循环：选题 → start → 单题独立上下文解题 → 校验 → close；并行调度、分段解题、VPN 守卫、平台终态收敛、`--smoke` |
| `shell.ts` | 异步 shell：Job 生命周期、进程组 kill、输出截断与分页、`wait_sec` 边沿等待、`out_file` 落盘、job 上限、`view` 多模态读图（含 PIL 压图/OCR 降级） |
| `shell_tool.ts` | shell 工具 schema、参数容错修复（`parseToolArguments`）、动作路由 |
| `llm.ts` | OpenAI 兼容 fetch client、重试策略、key 候选发现与 401 热切换（匿名优先，标签只带尾 4 位掩码） |
| `bench.ts` | 平台 API 客户端（fetch/start/close/选题策略/名额释放），错误码分类（含 `TaskFinishedError` 终态） |
| `records.ts` | bench 级持久化：`meta.json`/`events.jsonl`/`tasks.jsonl`/`tasks/<code>/conversation.jsonl`；`rebuildMessages` 续跑重建 |
| `config.ts` | 全部配置与默认值（环境变量 + 项目根 `.env`）、local/hosted 模式检测、转写规则 `toGatewayUrl` |
| `prompt.ts` | 单题 system prompt、任务卡模板、续跑/拉回/复盘通知 |
| `panel/` | `data.ts` 记录读取与统计（含历史归档基线预测）；`server.ts` Node 面板服务；`web/` React 面板 |
| `types.ts` | 共享类型契约 |
| `sim/` | 调度策略离散事件模拟器（纯内存）：`tasks.ts` 场景生成（真实配比/easy慢题/全短/全长/fuzz）、`engine.ts` 世界推进、`policies.ts` 策略消融、`run.ts` bench——**A/C/D 策略选择的依据**，回归时可再跑 |

测试在 `test/`（vitest，`test/setup.ts` 隔离宿主 `.env` 泄漏）；`e2e.test.ts` 是
`selftest.py` 的等价移植（假模型 + 内存平台，全离线跑通「分段解题 + 记录断言」）；
`scheduling.test.ts` 是 A/C/D 调度改造的契约测试；`sim.test.ts` 是模拟器 bench
（含"100 种子短题零饿死"性质断言，`pnpm bench` 单独跑）。

## 环境变量（均有内置默认值）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BENCHMARK_TOKEN` | 平台 token（本地模式在项目根 `.env`，托管由平台注入） | — |
| `BENCHMARK_BASE_URL` | 平台地址 | `https://tsecbench.zc.tencent.com` |
| `DEEPSEEK_BASE_URL` | 本地模型网关（官方） | `https://api.deepseek.com/v1` |
| `DEEPSEEK_API_KEY` | 网关鉴权 | — |
| `AGENT_MODE` | `auto`（默认）/`local`/`hosted` | `auto` |
| `AGENT_TASK_SEGMENT_SHORT_SEC` | 短题段墙钟预算（主闸；到点暂停保留上下文） | 600 |
| `AGENT_TASK_SEGMENT_LONG_SEC` | 长题段墙钟预算（多 flag ∨ hard ∨ level≥3） | 1800 |
| `AGENT_TASK_MAX_TURNS` | 段轮数【安全网】（0=关；墙钟才是主闸，轮数防极端空转） | 120 |
| `AGENT_SUMMARY_TIMEOUT_SEC` | 通关复盘限时（容器已关、脱槽跑；超时丢弃照常 pass） | 300 |
| `AGENT_RESUME_AGING_SEC` | 暂停题排队提权阈值（秒，防 fresh 无限插队） | 600 |
| `AGENT_MAX_PARALLEL` | 并行解题数（平台 max-active 会自动收缩） | 5 |
| `AGENT_DEADLINE_SEC` | 总时限秒（0=不限） | 0 |
| `VPN_GUARD` | VPN 运行时守卫：`auto`（hosted 开/local 关）/1/0 | `auto` |
| `PANEL_PORT` | 面板端口 | 8791 |

（其余配置项见 `src/config.ts`，全部可用环境变量覆盖。）

## 不变量（改代码前必读 `../AGENTS.md`）

- 单工具设计（只有 `shell`）、shell 异步边沿等待、每题独立上下文 + 分段解题、
  对话忠实落盘不裁剪、平台终态立即停止、VPN 只在容器内跑、预检是硬门槛。
- `--smoke` 与 `pnpm test` 是每次改动后的必跑检查。
