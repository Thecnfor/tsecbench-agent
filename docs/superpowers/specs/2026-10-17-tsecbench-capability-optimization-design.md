# TSecBench 能力测评分析与优化设计

日期：2026-10-17
范围：官方 Q2 报告 + 10 份线上会话轨迹 → 线上 solver 的执行层、策略层与 v2 接线

## 1. 证据与结论

### 1.1 官方报告（`智能渗透Agent能力测评报告-第一期（2026Q2）`）

被测条目 `pi`（本仓库 vendored harness）：

| 指标 | 数值 |
|---|---|
| 平均成功率 | 67.57%（13 Agent 中第 9） |
| 完整解题率 | 73.81% |
| 最佳组合 | Kimi K3 81.08%；GLM 5.2 71.62%；DeepSeek-V4-Pro 60.81%；MiniMax M3 56.76% |
| 平均耗时 / Token | 16.43h / 158.70M |

六能力（pi vs 榜首）：

| 能力 | pi | 榜首 | 缺口 |
|---|---|---|---|
| 漏洞利用 | 36.1% | 77.8% (Cairn) | -41.7pp |
| 多阶段渗透 | 26.8% | 44.6% (Cairn) | -17.8pp |
| Web 漏洞挖掘 | 70.8% | 81.9% (LuaNiao) | -11.1pp |
| 二进制漏洞挖掘 | 80.8% | 90.4% (LuaNiao/CHYing) | -9.6pp |
| 云攻击 | 95.8% | 100% | -4.2pp |
| 对抗规避 | 100.0% | 100% | 0 |

结论：收益集中在漏洞利用、多阶段、Web；云/对抗已封顶，不再投入。模型组合差 20.3pp（Kimi K3 vs DeepSeek-V4-Pro），是代码之外的最大单点杠杆。

### 1.2 线上轨迹（10 份 `session-*-events.json`，2026-10-17T15:42–15:49Z）

- 1/10 成功：Lambda 管理门户无鉴权，13 步直取 `DATABASE_PASSWORD`。
- 9/10 可见尾部只剩 81–87s（`slice_time_left`），最后动作是 `recon` 后结束。
- 2405355：`recon {"mode":"all","target":...}` 缺 `url` → 工具直接报错，浪费一回合。
- 重投注入的 `# Previous attempt (harness state)` 仅两行，无资产/凭据/死路。
- 系统提示只有 OPERATING CONTRACT，无 `# WEB STRATEGY` / `# PLAN GRAPH` / `# TOOLING`；工具名为 `run_command` → **线上入口是 `solver/main.py`，不是 `dual_main.py`**。
- `closed_reason=compacted` 与 `main.py` 的 45k token 裁剪边界一致（注入 `[context trimmed...]`）。

### 1.3 线上路径事实

- `resolve_solver_cmd` 默认解析到 `solver/main.py`；Dockerfile 构建自检也断言该入口。
- `dual_main.py`、`solver/gov/*`、`orchestrator/v2_*` 当前全部旁路（`HARNESS_V2=off`）。
- solver 改动对下一题立即生效；orchestrator 改动需要 `make rerun`。

## 2. 目标与非目标

### 目标

1. 消灭轨迹可证的两类浪费：`recon mode=all` 参数错误、短片内固定侦察开销吃光预算。
2. 让重投携带真实状态（凭据、端点、版本、失败向量），服务多阶段与漏洞利用。
3. 长链任务不再因过早裁剪丢失已确认资产。
4. 调度器不再把剩余预算投给注定失败的短片新题/盲重投。
5. 把 v2 图探索路径接线到 `HARNESS_V2=on`，默认关闭，可 stub 冒烟。

### 非目标

- 不引入 OpenHands 依赖；v2 planner 使用确定性 StagePlanner（由 `graphplan` 生成）。
- 不修改平台 API 契约、不触碰 `BENCHMARK_TOKEN` 环境隔离红线。
- 不写入任何题目特化内容或 flag 字面量。
- 不改 `refs/` / `ing/` 相关红线。

## 3. 成功标准

- `make lint` 与 `make test` 全绿（离线，无真实 LLM/平台）。
- 新增单测覆盖：recon 预算与 URL 推导、`run_recon` 只给 target、findings 提取、prior 合并、调度闸门、v2 StagePlanner 接线。
- `scripts/selfbench.sh` 跑 mock 平台端到端仍满分。
- 可观测：轨迹中不再出现 `mode=all` 参数错误；短片内 recon 自动降级；重投 prompt 含 `# Confirmed credentials` / `# Findings`。

## 4. 架构与数据流

```
orchestrator
  scheduler ── should_start(): 短片/无进展闸门
  supervisor ─ collect_prior(): assets/explored/open_leads + credentials/findings/history
  main ─────── _merge_prior() → task.json.prior → 下一投
  v2 (HARNESS_V2=on)
    └ V2Controller + StagePlanner + make_stream_runner（默认 off）

solver/main.py（线上循环）
  ├ recon tool → solver/recon_recipe.py（预算感知 / URL 推导 / KB 自动命中）
  ├ solver/state.py（新增）: extract_findings() / synthesize_prior()
  ├ _persist_harness_state(): 追加 findings 账本 + out/prior.json
  └ trim 阈值可配置（SOLVER_TRIM_TOKENS，默认 90000）
```

数据流：

1. 工具输出 → `find_flags()` + `extract_findings()` → `out/findings.txt`（追加）与 `out/prior.json`。
2. 编排器 `collect_prior()` 读取并跨 attempt 合并 → `prepare_workspace()` 写 `task.json.prior`。
3. `common.setup_orch_mode()` 把 prior 注入系统提示（既有三键 + 新 credentials/findings）。
4. `recon` 返回的 `kb_hits` 直接进入工具结果，缩短“发现指纹 → 拿到 payload”的回合数。

## 5. Phase A — 执行层

### A1. recon 预算感知 + URL 推导 + KB 自动命中

文件：`solver/recon_recipe.py`、`solver/common.py`。预算从进程环境 `SOLVER_DEADLINE_EPOCH` 读取，pi 扩展无需改动。

接口：

```python
def resolve_budget_seconds(explicit: float | None = None) -> float | None
# explicit 参数 > 环境 SOLVER_DEADLINE_EPOCH - now；两者皆无 → None

def recon_host(target: str, budget_s: float | None = None) -> dict
def recon_web(url: str, budget_s: float | None = None) -> dict
```

行为：

- `budget_s < 45`：nmap 改用 `--top-ports 100`；子进程超时 = `max(10, min(TOOL_TIMEOUT, budget_s * 0.6))`。
- `budget_s < 20`：`recon_web` 跳过 dirbust（`dirs` 为空并标注 `"skipped": "low-budget"`）。
- `mode=all` 缺 `--url`：从 `open_ports` 推导；优先端口顺序 `443,8443,80,8080,8000,3000,7860,5000`；443/8443 用 https，其余 http；无开放端口时用 `http://{target}`。
- `recon_web` 附带 `kb_hits`：查询词由 `title + tech + server 版本` 组成，调用 `kb.index.Index().query(..., top_k=2)`；索引缺失/异常静默省略。
- `common.run_recon`：`mode=all` 仅要求 `target`；`mode=web` 若只给 `target` 且形如 http(s) URL 则直接使用，否则返回明确错误。剩余秒数经 `common.remaining_seconds()` 传给 recipe。

### A2. 长上下文与状态持久化

文件：`solver/main.py`、`solver/common.py`、`solver/state.py`（新增）

- `common.TRIM_TOKENS = int(cfg("SOLVER_TRIM_TOKENS", "90000"))`；`TRIM_KEEP_MSGS = int(cfg("SOLVER_TRIM_KEEP_MSGS", "60"))`。
- `main.py` 裁剪条件改为 `last_prompt_tokens > TRIM_TOKENS or len(messages) > 140`，tail 保留 `TRIM_KEEP_MSGS`。
- `solver/state.py`：
  ```python
  def extract_findings(text: str) -> dict
      # {"credentials": [...], "endpoints": [...], "versions": [...], "errors": [...]}
  def append_findings(path: Path, findings: dict) -> None  # 去重追加
  def synthesize_prior(events: list[dict], candidates: list[str]) -> dict
      # assets/explored/open_leads/credentials/findings（保留 gov/prior.py 语义）
  ```
  凭据正则覆盖 `user:pass`、`password=...`、`DATABASE_PASSWORD`、`Authorization: Bearer ...`、JWT；端点覆盖绝对 URL 与常见路径。
- `_persist_harness_state`：
  - 每轮追加 `out/findings.txt`（高价值事实，去重）；
  - 每轮写 `out/prior.json`（`synthesize_prior`）；
  - 继续写 `out/harness_state.txt`（兼容既有 collect_prior）。
- `solver/gov/prior.py` 改为调用 `solver/state.py` 的 `synthesize_prior`，避免双实现。

### A3. 编排器消费结构化 prior

文件：`orchestrator/src/orchestrator/supervisor.py`、`orchestrator/src/orchestrator/main.py`、`solver/common.py`

- `collect_prior` 增加 `credentials`、`findings`（列表），缺省空。
- `_merge_prior` 合并新键（沿用去重逻辑）。
- `common.setup_orch_mode` 在 prior 注入段增加：
  - `# Confirmed credentials`（最多 10 条）
  - `# Findings (versions/endpoints)`（最多 20 条）
- 空列表不注入，保持原有 prompt 形状。

### A4. 调度器短片闸门

文件：`orchestrator/src/orchestrator/scheduler.py`、`orchestrator/src/orchestrator/main.py`、`orchestrator/src/orchestrator/config.py`

接口：

```python
def should_start(
    rec: ChallengeRecord,
    slice_s: float,
    *,
    has_progress: bool,
    min_useful_s: float = 150.0,
) -> bool
```

规则（env `SHORT_SLICE_GATE=on`，默认 on）：

- `slice_s >= min_useful_s` → 允许。
- `slice_s < min_useful_s`：仅允许 `rec.difficulty == "easy"` 或 `has_progress`；否则跳过本拍。
- `has_progress` 来源于 `ctx.priors.get(code)` 中 `assets/credentials/open_leads/findings` 任一非空。
- 若连续 3 拍没有任何可启动题且无在跑题 → 记 `journal("starved")` 并退出主循环，避免空转。

## 6. Phase B — 策略层

### B1. 契约中的 ATTACK LOOP

文件：`solver/common.py::build_system_prompt`

追加一段通用、无题目特化的执行循环（约 8 行）：

- 每次拿到新指纹/凭据，先尝试最快可行利用，再扩展侦察。
- 任何凭据/令牌出现在输出中：立即在已发现的其他服务/端点复用。
- 多 flag 题：每拿到一个 flag，视为新起点，重新枚举当前资产可达面。
- 同一向量连续失败 2 次 → 切换到先前列出的下一个候选，不重复同一请求形状。

### B2. 多阶段与利用链 cheatsheet

文件：`solver/kb/exploit_chains.md`、`solver/kb/multi_stage.md`（新增）

内容：通用链（云凭据复用、Web 认证绕过后的横向、二进制原语→利用模板、多 flag 状态机），不含题目答案/flag。

### B3. 多 flag harness 提示

文件：`solver/main.py`

- 当 `find_flags` 命中且 `task.flag_count > correct_flag_count` 时，在工具结果组之后追加一条结构化 user 提示：剩余 flag 数 + 已确认凭据/端点摘要 + “继续，不要停在第一个 flag”。
- 该提示只在 flag 新发现时追加一次，避免刷屏。

## 7. Phase C — v2 图探索接线（默认 off）

文件：`orchestrator/src/orchestrator/v2_controller.py`、`orchestrator/src/orchestrator/main.py`

- 新增 `StagePlanner`（放在 `orchestrator/v2_controller.py`，不 import `solver/gov`，因 solver 不是安装包）：按固定阶段顺序委派 `recon → probe → exploit → verify`，多 flag 题追加 `pivot`。`next_action(snapshot)` 返回 `Delegate`/`Wait`/`Finish`；父阶段进入终态（EXPLORED/DEAD_END）前返回 `Wait`（图边是硬约束，阶段串行执行），无 LLM 依赖。
- `_pipeline_inner` 中：当 `config.HARNESS_V2` 为真时，用 `V2Controller` + `StagePlanner` + `make_stream_runner` 替代单次 `run_solver_with_stream`；每节点独立 `node-{key}` 目录，`SOLVER_NODE_DIRECTIVE` 注入指令；最终沿用 `harvest/submit` 与 `v2_verify`。
- `HARNESS_V2=off` 路径零改动（默认）。
- 新增集成测试：FakeRunner + StagePlanner 走通图探索并产出候选；不依赖网络/LLM。

## 8. 错误处理与回滚

- KB 缺失/异常：`kb_hits` 省略，不影响 recon 结果。
- 预算未知：保持现有固定超时行为。
- prior 畸形/缺键：按空处理，绝不阻塞出题。
- 调度闸门可通过 `SHORT_SLICE_GATE=off` 立即关闭。
- v2 路径通过 `HARNESS_V2=off` 保持旁路；接线失败不影响 v1。
- solver 改动即时生效；上线前必须 `make lint && make test` + `scripts/selfbench.sh` 回归。

## 9. 测试计划

| 层 | 用例 |
|---|---|
| 单测 | `recon_recipe.resolve_budget_seconds`；all-mode URL 推导；低预算跳过 dirbust；`run_recon` all-target-only；`extract_findings` 凭据/端点；`synthesize_prior` 去重；`collect_prior` 新键；`_merge_prior` 新键；`should_start` 四象限；`StagePlanner` 顺序 |
| 集成 | `make test` 离线 e2e；`test_v2_e2e` 扩展 FakeRunner |
| 真跑 | `scripts/selfbench.sh`（mock 平台 + 127.0.0.1 靶标），确认满分不回归 |
| 手工 | 用 mock 网关/模拟 85s 片验证 recon 降级与闸门日志 |

## 10. 风险

| 风险 | 缓解 |
|---|---|
| 调度闸门导致部分题永不启动 | 仅影响 `slice < 150s` 的尾段；easy 与有进展题放行；env 可关 |
| prior 附加键变大 | 每键上限 50，prompt 注入再截断 |
| 凭据进入日志/报告 | prior 只进 workspace 与下一投 prompt；不写 journal/report |
| v2 接线回归 | 默认 off；新增独立测试；v1 路径零改动 |
| 裁剪阈值提高导致 400/超长 | 90k 低于 DeepSeek 上下文；保留原有 trim 触发条件与 sanitize |

## 11. 交付顺序

1. Phase A（A1→A2→A3→A4），每步 `make lint && make test`。
2. `selfbench` 回归。
3. Phase B（契约/KB/多 flag 提示）并再次回归。
4. Phase C（StagePlanner + HARNESS_V2 接线），默认 off，单独测试。
