# Harness v2 —— 五层升级（Staircase Streaming / ReActNet / EvoSOP / TriviumDB / EVOSTER）

> 2026-10-16 立项，分支 `feat/harness-v2`，`main` 不动。本文件是行为契约，
> 代码注释按 § 号引用本文；与本文冲突以代码为准（README.md 通用约定同源）。
> 五个机制来源（Staircase Streaming / ReActNet / EvoSOP / TriviumDB /
> EVOSTER）均为架构借鉴，**不构成本仓库既有硬约束的任何豁免**（§1）。

## 0. 动机、边界与映射总表

初赛前夜（2026-10-17 开赛）只做**加法升级**：不动 `docker/Dockerfile`、
不动 toolchain 版本真值源、不动并发/submit 的既有经济学；全部新层
确定性实现、离线可测、任一环节失败即退回原路径。五层与仓库落点：

| 层 | 机制来源 | 仓库落点 | 目标语义 | 状态 |
|---|---|---|---|---|
| L1 | Staircase Streaming（流式半成品驱动下游） | `solver/gov/stream.py` + `orchestrator/streams.py` | TTFT = flag 产出 → 提交的延迟；下游不等上游整批完成 | 已落地 |
| L2 | ReActNet（优化拓扑 → 图工程） | `solver/gov/graphplan.py` | planner 自由文本 → 显式 stage 图 + 逐跳 why/how 指令 | 已落地 |
| L3 | EvoSOP（SOP 生命周期） | `orchestrator/sop.py` | 成功轨迹 → 脱敏 SOP → 记账检索 → 剪枝 | 已落地 |
| L4 | GraphRAG（TriviumDB / TSNG 三信号） | 接驳位（见 §6） | prior 双图 JSON → 单引擎 `.tdb` | 阻塞，P0 只留接口 |
| L5 | EVOSTER（在线修复 + 锚定轨迹平衡） | `orchestrator/repair.py` | 失败片信号化 → 有界动作 + 权重演化，不直接放弃 | 已落地 |

不变式（对本文件全篇生效）：BENCHMARK_TOKEN 绝不进 solver 环境
（architecture.md §6.5.1）；`refs/` 不进镜像不进库；镜像与 prompt 不含
flag 字面量与题目特化内容（architecture.md §10.4 / image-spec.md §6）。

## 1. 边界、不变式与实施策略

不变式（对本文件全篇生效）：BENCHMARK_TOKEN 绝不进 solver 环境
（architecture.md §6.5.1）；`refs/` 不进镜像不进库；镜像与 prompt 不含
flag 字面量与题目特化内容（architecture.md §10.4 / image-spec.md §6）。

## 2. L1 Staircase Streaming

### 2.1 solver 侧：逐行流式泵

`solver/gov/stream.py::stream_pi(proc, on_line, budget_s, on_expire)` 替代
dual_main 原先的 `proc.communicate(timeout)` 整批等待。行到即处理：事件行
进 `gov.session`、候选行实时 `report_candidate()`（out/flags/NNN.txt 实时
可见）、每 `FLUSH_EVERY=40` 行增量预写一次 `out/prior.json` —— solver
未退出时，编排器/复盘已能读到「半成品 prior」。任何单行处理失败只丢该行
的派生效果，绝不中断流路；EOF 靠 killpg 后管道闭合自然收束，另有
`HARD_CAP_S` 死等兜底，收集循环永不挂死。超时语义与原 `communicate`
等价：`budget_s + 30` 到点杀整进程组。

### 2.2 节拍早收割与增量预写

- solver 侧节拍：每消费 `FLUSH_EVERY` 行预写一次 prior（dual_main 闭包）；
- 编排器侧节拍：`orchestrator/streams.py::run_solver_with_stream` 以
  `STREAM_INTERVAL_S=45` 扫 workspace，新候选（不在 `rec.submitted` 的
  harvest 产物）立即 `submit_candidates`，记 `early_submit` 事件；
  `completed` 信号同步带出。
- 返回 `(outcome, early_new, early_completed)` 并入 `_pipeline` 终局/多轮
  判定；片尾收割、提交路径原样保留，同一真值（`rec.submitted`）零重复计；
- 末段：solver 自然退出后仍有一次全量收割补扫，候选零漏（与原契约同位）。

### 2.3 语义红线

早收交后**不杀在跑 solver**（完成收尾仍走原状态机）；重试触发条件
（完整时间片 + 零 flag）不受本地早交影响 —— `correct_flag_count`
是平台对账真值；超时杀进程组纪律不变（killpg，不留孤儿）。
（注：hint 链路已整体删除，见 architecture.md §4.4；本层的早收割不与之耦合。）

## 3. L2 ReActNet 图工程

`solver/gov/graphplan.py`：把 `(task, planner 要点, prior 双图)` 确定性
编译为 `{category, nodes, edges}`，全程零 LLM —— planner 输出只作为节点
指令词料，失败时整图按题型默认拓扑降级。

- **§3.1 节点目录**：stage = recon / probe / exploit / verify，按题型播种；
  未知识别题型回落 misc。recon 节点携带 `confirmed_assets`，prior 有资产时
  指令自动改为「从已确认资产续扫、不重复侦察」。
- **§3.2 图即指令**：每条边携带 `when / why / how`（何时进入、为何需要、
  上一跳产出如何变成下一跳输入）；exploit 节点 `directives` 收纳 planner
  前三条要点。`render()` 产物注入 APPEND_SYSTEM。
- **§3.3 审计产物**：编译结果落 `out/plan_graph.json`，编排器与 debug-ui
  观测台读取；失败不阻塞解题（与 prior.write 同防御位）。

## 4. L3 EvoSOP（技能演化）

`orchestrator/sop.py`，库目录 `<workspace_root>/sops/`（运行期产物：不进
镜像、不入仓库、不出题工作区，天然满足「一次性沙箱」约束）。

### 4.1 生命周期

build → merge → evaluate → prune：

- **build**：SOLVED / 本轮有命中（`early_new or report.new_matches`）时
  `_pipeline` 调 `distill(ws, base)` —— 从 `out/session.jsonl` 提取 `$ `
  命令行（与 prior 合成同源正则）；尺寸：`MAX_STEPS=6` 条、每条
  `MAX_STEP_CHARS=160`（§4.2 预算）。
- **merge**：步骤序列指纹去重；重复命中 = `wins+1`，不重复建条。
- **evaluate**：`select()` 注入消费即 `uses+1`（wins 由成功蒸馏轮累计）。
- **prune**：`uses>=4 且 wins<=1` 出清；库容量超限（>24）踢低胜率旧条；
  正文文件丢失的条目一并出库。剪枝发生在下一次 `distill` 周期落盘。

### 4.2 内容规范与合规硬闸（fail-closed）

平台禁止「针对题目的历史答题记忆」→ SOP 只存**通用**步骤残片：

1. 统一脱敏：URL → `<URL>`、裸 IP / host[:port] → `<TARGET>`、长
   hex/base64 载荷 → `<PAYLOAD>`；
2. 任何命名 flag 模式（`FLAG{...}` / `HTB{...}` …，与 harvester 同组）
   在条目内存活 → **整条拒绝写入**；检索侧对历史脏条目同样过闸；
3. 零 LLM：提取、去重、打分全为正则 + 集合运算，胜态是布尔命中计数。

### 4.3 检索与注入

`supervisor.prepare_workspace` 建 workspace 时 `select()` 打分
（token 重叠 ×2 + wins），top-2 正文进 `task.json.sops`；dual_main 注入
APPEND_SYSTEM，标头写明「generic，按当前题型退化改写」。旧 solver 忽略
新 task 字段，向后兼容。

## 5. L5 EVOSTER（在线修复）

`orchestrator/repair.py::choose_repair`：失败片的信号 → 有界动作集合 →
锚定权重 argmax；平票走固定优先序 widen → recompile → retry_same。

### 5.1 信号表与权重

| 信号 | 动作 | 语义 |
|---|---|---|
| timed_out（首次，预算够） | widen | 下轮时间片 ×1.5（封顶 `slice_max_s×2`、不超剩余总预算）；全程只许一次 |
| timed_out（已用过 / 预算不够） | recompile | 换攻击面重投 |
| 其它非零退出 | retry_same | 瞬态崩溃原计划重投 |
| 正常结束零候选 | recompile | 原面探秃 → 指令进 `task.json.repair.note`（APPEND_SYSTEM 的 REPAIR FOCUS 段） |

锚定轨迹平衡的工程化收敛：命中页 `_pipeline` 对 `last_repair` 动作
`update_weights(+25%)`；无负反馈（避免不对称震荡）；重启丢失从 BASE 平
权重重新演化（`RunContext.repair_weights`）。

### 5.2 红线

`MAX_ATTEMPTS`、finalize 收尾保护一律不动 —— 本层只在既有
attempt 预算内选择「换什么姿势重投」，不放宽任何闸门；widen 切片覆盖
用一次即清（`rec.next_slice_s`），`repair_widened` 防重复放大。

## 6. L4 GraphRAG（TriviumDB）—— 现状与分阶段接入

**结论先行**：TriviumDB（Apache-2.0，Rust 内核）选型论证成立，但**本周不
引入镜像**；两处硬阻塞见 §6.2。已采纳的架构决策（§6.1）：写入侧收敛到
执行体单点、编排器只读 —— 同一 `.tdb` 跨进程写是排他锁（多读单写），
按题并发=3 时以每题分片持有写权（attempt 串行，天然单写者）。

### 6.1 目标形态

- `.tdb` 节点 = 双图 prior 的超集：assets / explored / open_leads 之外
  增语义稠密向量与时间维；写入发生在执行体收尾（与今日 prior.json 同位）；
- 检索 = TSNG 三信号（向量 q + 属性过滤 + N 跳图邻域），编排器跨轮合并
  prior 用它替代 `_merge_prior` 的朴素去重样本集；
- prior.json 契约（architecture.md §2.5）进程间保持不变 —— TriviumDB 是
  增强读通道，不是新的耦合点。

### 6.2 两个硬阻塞

1. **Python 绑定 `requires-python <3.13`，本仓库钉死 3.14**（toolchain.env
   唯一真值源；`ctf-agent` 硬限 `>=3.14`）。uv 直接拒绝安装 0.8.x 全系，
   编排器（Python 侧）无论 writer 还是 reader 都装不上。可用的是
   napi-rs Node 绑定 `triviumdb@0.8.8`，但本地与构建机均无可实证的
   node 环境 —— API 形状未验证前写不出诚实的契约测试。
2. 运行期零公网：二进制绑定必须钉死版本 + sha256 进 pi-vendor 式 vendored
   阶段（image-spec.md §3 版本纪律），而 `docker/Dockerfile` 本周冻结。

### 6.3 处置

接驳位已留：双图 JSON 通道零改动；写侧接口收敛在 `gov/prior.write` 单点
（未来 `.tdb` 写入者就是这一个函数的旁路）；编排器读取仍在
`collect_prior` / `_merge_prior`。升级动作（初赛后的非赛期窗口）：
在能启动 node 的环境实证 npm 绑定 API → 以 `MEMORY_TDB=on` 特性开关灰度
→ e2e 假执行体全链路通过后再进 Dockerfile。

## 7. 测试矩阵与回滚

- `make test`：188 passed + 1 skipped（本层新增 test_repair / test_sop /
  test_streams / test_graphplan；e2e mock 全链路仍绿 —— 节拍早收割不破坏
  原切片时序断言）；
- `make lint`（ruff check + format）全绿；
- 回滚：本分支整体 revert 即回 main 语义；各新层都是旁路 + 默认保守参数，
  单层下线 = 删去对应调用点（streams / repair / sop / graphplan 互相独立）。

## 8. 未决（open-questions.md 补编）

- B15：TriviumDB Node 绑定 API 实证与 0.8.x 行为冻结策略（§6.2）；
- B16：SOP 库跨 run 持久化的合规边界（当前限定 run 内，§4.1）。
