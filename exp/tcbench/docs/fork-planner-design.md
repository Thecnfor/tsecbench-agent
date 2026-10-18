# Fork 多 Attempt + Planner 设计（tsec_agent）

状态：**已实现**（2026-10-16，`AGENT_FORK` 默认 on；`AGENT_BRANCH_CONCURRENCY` 默认 4；`AGENT_PLANNER` 在 fork 开启时默认 on；
198 个 vitest 全绿，
`pnpm check` 通过）。实现映射：§1→branch_tools.ts/agent.ts 工具分发、§2→agent.ts `branchCwd`
+prompt.ts FORK_PROMPT_EXTRA、§3→branch.ts `prune`、§4→branch.ts `reserveFork` 护栏、
§5→stall.ts + planner.ts + agent.ts `maybePlannerStall`、§6→records.ts `rebuildBranches`/corelog.ts。
缓存红线全部落实：分支指令 append 不动 system、广播/planner/剪枝摘要一律尾部追加、
轮转单飞天然串行（分支首请求并发竞争已被实测否决）。面板：LiveView 带分支名册与 黑板 尾部。

## 总纲（协作思想，行为改动先对齐这里）

- **界由 fork 划定，不轻越**：分支的范畴/任务只由 fork 指令规定；planner、广播、轻推等
  一切注入**不得给已分工的分支改派范畴外任务**——planner输入必须带「各分支分工」、
  系统提示有对应硬约束（实测教训：planner曾让已分工的 Attempt 1「停手转去做别人的活」，
  方向被弃用 + 重复劳动）；执行侧同理「不要轻易跨界，范畴外的发现走 log/通知相关 Attempt」。
  换方向 = planner 从 checkpoint 新开 Attempt，不是改道别人。
- **所有 Attempt 对等，没有特权主分支**：没有 broadcast 工具；共享信息统一走 `log`，
  写黑板并送 planner，由 planner 决定是否提醒/转告。

## 0. 依据（cache_exp 实测，deepseek-flash）

| 结论 | 数据 | 对本设计的约束 |
|---|---|---|
| 交错轮转免费 | a1b1a2a3b2 逐请求命中率 = 顺序执行（热身 95%） | 调度器不为缓存妥协 |
| 跨分支共享前缀命中 | 分支首请求 99.2%（miss 仅 180 tok） | 共享段只冷启动一次 |
| 改 system 伤首跳 | 21.8% → 下一跳自愈 94.9% | 分支指令只 append，不动 system |
| 尾部插入近免费 | 中途近尾插入 92.5% | 广播/合并/planner一律尾部追加 |
| 并发首发双冷启动 | a1‖b1 双 0% | 新分支首请求排队串行 |

## 1. 工具面（放宽单工具不变量，AGENTS.md 同步更新）

每个分支工具集一致（schema 稳定 → 前缀缓存稳定）：

- **`shell`**（现有）：cwd = 本分支命名空间；绝对路径可读任意处
- **`fork(direction: str)`**：**任意活跃分支可调**（含分支再分叉；护栏见 §4，树的深度
  自然有界）。controller 复制**调用方当前尾部** messages 为新分支 + 追加方向指令
  （含命名空间/广播规则/fork 源）。护栏命中时**返回拒绝原因**（不静默）：
  活跃 >`AGENT_MAX_BRANCHES`(5) / 总数 ≥`AGENT_FORK_MAX`(8) / 冷却
  <`AGENT_FORK_COOLDOWN_SEC`(180s)。新分支记录 `parent`（面板树视图/审计）；
  首请求排队（并发首发护栏）
- **`log(note: str)`**：落 `log.jsonl` `{ts, branch, seq, note}`。planner与面板的
  唯一抽象层；不注入其他分支；controller 天然携带 branch，无文本解析
- **`report(status, summary, evidence?, artifacts?, next?)`**：结束当前 Attempt。
  `done`=方向完成/目标达成；`deadend`=已证伪或穷尽；`blocked`=外部阻塞。
  结果卡写入 `log.jsonl`（kind=result），planner 读事件并负责后续决策；
  本 Attempt 立即 park，不参与后续轮转。Scope 聚合：任一 done=satisfied、
  全 deadend=refuted、有 blocked=unresolved。分支换方向由 planner 从 checkpoint
  新开 Attempt，不允许分支自己改派范畴。
- **`state_change(action, recovery, status?, id?, scope?)`**：共享靶机写操作账本。
  `status=done` 记录 mutation id 与恢复方法（目标重启后由 planner 选一个 owner 恢复）；
  `status=recovered` 标记完成；`scope:"local"` 只是 workdir 写，不进入恢复队列。
  详见 §11。
- **flag 事件（controller + 模型混合权威）**：flag 的稳定 ID = submit 响应的
  **`matched_flag_index`**——flag **内容**会因靶机重启/重置而变，**索引不变**
  （`1..total_flag_count`）。分工：**模型**在 submit 后立即 `log` 记
  `flag idx=<matched_flag_index>`（模型能从 curl 响应直接看到该字段）；
  **controller** 以 watcher 快照的 `correct_flag_count` 跳变为权威触发
  `{kind:"flag", branch, flag_index, correct_flag_count, total_flag_count}` 落账
  （模型漏报则 index 记 `?`，计数仍准确）。用途：① 哪个分支拿到第几个 flag
  （贡献度/剪枝决策）② 重启续跑后按 index 对账「还差 idx 2,4」而不比对内容
  ③ 进度统一表示 `2/5 (idx:1,3)`。`409 duplicate` 视为「该 index 已存在」不计新进展
- **`log(note: str)`**：共享黑板的唯一写入通道。写 `log.jsonl` 并生成 planner `LogEvent`；
  planner 可据此提醒/转告/更新持久化状态。没有 broadcast 工具，不允许 Attempt 直接群发。
  planner 关闭时仍写黑板，由面板/审计读取。

## 2. Shell 命名空间

- 每题已有独立 workdir（`taskWorkdir(code)` = `/work/tasks/<code>`，宿主对应
  `runs/<benchid>/tasks/<题目id>`；`/work` 是 `runs/<TOKEN>` 卷挂载）。
  **初始 Attempt 0 cwd = 题目 workdir 不变**；后续 Attempt cwd = `<workdir>/branches/<N>`
  （同容器；分支产物天然落在该题记录树下）
- 跨命名空间**读任意**（绝对路径；约定 log 中通报产物路径，如
  `<workdir>/branches/2/scan.log`）
- **写靠提示词约定，不强拦截**：① 产物只写本分支命名空间 ② 命名空间外与容器级操作
  （重启服务/监听共享端口/iptables/改系统文件）**默认只读**，要动先 log 告知 planner 协调
- 每题独立 `ShellManager`（job 表/寻址/配额跨题隔离）；shell job 再归属 Attempt，list/wait/status/kill 默认只看本 Attempt；每 Attempt 有
  `AGENT_SHELL_MAX_JOBS_PER_BRANCH` 配额，避免一个 Attempt 占满本题 job 池；
  进程组 kill/硬上限等现有语义不变

## 3. 协调与合并

- **没有特权主分支**：Attempt 0 只是初始 Attempt，和后续 Attempt 对等；
  统筹者是 **planner**（controller 内的决策层），不是某条对话线。
  planner 读共享黑板/结果卡，决定从 checkpoint spawn、回收、关闭 Scope。
- **不存在历史合并**：各 Attempt messages 永不互插中段（缓存红线）。合并 = 发现层：
  ① log 写黑板并送 planner ② 结果卡写 log，由 planner 决策
  ③ Attempt 结束/剪枝时写黑板，由 planner/相关 scope 增量读取
- Attempt 自停条件：report(done/deadend) / 被回收 / 拿满 flag（全停收束）

## 4. 防无止境分支（护栏，全部 env 可调）

| 护栏 | 默认 | 说明 |
|---|---|---|
| `AGENT_MAX_BRANCHES` | 5 | 活跃 Attempt 上限（含初始 Attempt 0） |
| `AGENT_FORK_MAX` | 8 | 每题 fork 总数硬上限（含分支再分叉；树大小有界） |
| `AGENT_FORK_COOLDOWN_SEC` | 180 | fork 冷却（对所有分支生效） |
| 预算共享 | 固定 | **fork 不增预算**：全分支共享段墙钟与轮数，分支越多每线越薄 |
| `AGENT_PLANNER_WEIGHT_COOLDOWN_SEC` | 120 | 同一 Attempt 的 planner 权重调整冷却 |
| `AGENT_BRANCH_PRUNE_SEC` | 1200 | Attempt 黑板/flag 双停滞 → 回收 + 摘要写黑板（**B 方案 2026-10-16**：曾为 600=与长题停滞阈值同值，剪枝先执行令planner窗口为0；1200 使 900s 触发后仍有 300s 救治窗） |

> 分支可再分叉：深度不单独设限——活跃上限 3 + 总数上限 5 + 冷却 + 预算共享
> 四道闸叠加后树天然有界（最坏 3 层：Attempt 0→1→2 且总数 5 截断）。

## 5. Planner 卡塞介入

- **程序检测信号**（零 LLM 成本）：`stall.ts:evaluateStall` 计算
  ⓪ flag 主时钟（短题 400s / 长题 900s，一票触发）＋ Attempt 级四信号任二：
  停滞（300/600s）、同命令≥3、连接类硬失败≥3、连续错提交≥2。
- **事件化**：`agent.ts:maybePlannerStall` 选最久无核心事件的 active Attempt，
  生成 `StallEvent` 交给 planner；planner 不轮询、不跑 shell。
- **planner 动作**（启发式）：
  - 有提醒额度 → `remind`：尾部追加 `[planner 提醒] …`，要求先 log 证据，再换方法或
    `report(deadend)`；
  - 额度用完仍卡 → `retire` 该 Attempt（含初始 Attempt 0；timer prune 仍以 0 为兜底锚点）；
  - `flag` 进展事件 → `set_weight(attempt, 2)`：让出 flag 的 Attempt 获得更多调度份额；
    controller 做同值 no-op + `AGENT_PLANNER_WEIGHT_COOLDOWN_SEC`(120) 防反复调整；
  - `done + next` 且题目仍有未拿 flag → `next` 进入 planner 待定队列（持久化到
    `planner.json`），有空槽/分支让位时由 controller spawn；`done` 只表示方向完成，
    不表示整题完成；duplicate flag 也不等于方向完成；
  - 提醒有每 Attempt 上限 `AGENT_PLANNER_NUDGE_MAX`(2) + 冷却
    `AGENT_PLANNER_NUDGE_COOLDOWN_SEC`(400)；冷却中不重复提醒也不因此关闭。
- **事件优先级**：`report > stall > flag/log > coverage`；report 是结构性变化，不能
  被周期性的 stall/coverage 事件压在 FIFO 后面。
- **覆盖检查**：多 flag / 长题每轮检查 active Attempt 与 scope 数；不足目标（按 flag 数，
  最多 4 条）时 planner 发 `coverage` 事件并 `remind`：按 fork 分类（时间/空间/假设/
  探索/产出与权限）检查覆盖是否完全，有独立方向就补 fork；同样受提醒额度/冷却限制。
- **时钟跨段持久**：flag/停滞时钟锚在题起点 + 黑板最后事件，solveTask 启动时恢复。
- **不再有独立 observer LLM**：提醒文本由程序模板生成；planner 只做决策，
  不读全量对话、不改 messages、不做跨题建议。

## 6. 记录与续跑

- `conversation.jsonl`：单流追加 + **`branch` 字段**（对话忠实落盘不变量保持）
- `log.jsonl`：新增，独立文件，
  `{ts, branch, seq, kind: core|planner|flag|mutation|artifact, note?, flag_index?,
  correct_flag_count?, total_flag_count?}`——flag 以 **index 为 ID**（内容随靶机重启变，索引不变）
- `rebuildMessages` 按 branch 重建多份 messages；tasks.jsonl 生命周期不变
- panel：Attempt 树（parent/scopeId/depth/status，按缩进展示）、分支泳道（甘特图 llm/tool 按 branch 分色）、黑板视图、planner 事件标记

## 7. 开关与默认

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_FORK` | 1 | fork 工具与多 Attempt 调度（0=单线） |
| `AGENT_PLANNER` | 1 | planner 统筹（fork 开启时生效；0=关闭自动提醒/spawn） |
| `AGENT_PLANNER_NUDGE_MAX` | 2 | 每段每 Attempt 的 planner 提醒上限 |
| `AGENT_PLANNER_NUDGE_COOLDOWN_SEC` | 400 | planner 提醒冷却 |
| `AGENT_SHELL_MAX_JOBS_PER_BRANCH` | 4 | 每 Attempt 后台 job 上限（每题仍 8） |
| 其余 | 上表 | 全部可 env 覆盖 |

默认即多 Attempt；需要单线排查时设 `AGENT_FORK=0`。

## 8. 合规

- planner 只读运行时派生的黑板与结果卡，不引入任何历史题解/题目特化内容
- 广播与 planner 提醒全部落 conversation.jsonl/log.jsonl，审计友好
- 不改动平台流程、flag 收割、VPN/预检等既有不变量

## 9. arena 实测修正（2026-10-16 首跑）

**现象**：f2-05 跑 8 分钟 28 轮、反复使用 VM_KEY07（146 处），却 0 log / 0 fork
（同 run 的 web_sqli_01 正常调用 log → 工具链无 bug，tools 每轮随请求下发）；
planner未触发 = 停滞阈值 600s 而 run 494s 即停，三信号均未成立（按设计）。

**结论与修正**：
- **A. prompt 触发判据具体化**（prompt.ts）：log 从抽象「关键发现」改为
  **黑板（blackboard）证据卡**语汇——证据类（凭据/密钥/中间产物如 VM_KEY07、
  端口版本、2xx 命中、校验常量）与判断类（路径证伪/新假设/阶段切换）逐项列举，
  频率「每 ≤5 轮或换方向至少一张」，后果「黑板空白=停滞回收」；
  fork 加**渗透流程时机判据**（长活挂后台 × 开第二条攻击线，典型如
  web 路径爆破 × 本地二进制逆向并行）。
- **B. log 轮级轻推**（agent.ts，执行层兜底，零 LLM 调用）：分支连续
  `AGENT_CORE_LOG_NUDGE_ROUNDS`（默认 3）轮无 log/flag → 在该轮**最后一个
  工具结果尾部**附提醒（append-only 保缓存、conv 忠实落盘）；每段每分支最多
  `AGENT_CORE_LOG_NUDGE_MAX`（默认 2）次、注入后归零给 3 轮响应期；
  `multiOn` 闸保证只在 log 工具在场时注入（常规模式无该工具会诱导 unknown tool）。
  分工：B = 轮级分钟级轻推；planner = 时间/行为信号的重量级插嘴。
- 观察留档：repeat 信号对「同前缀不同参数」的命令不敏感（8 条 `cd…; echo "` 系
  内容不同，合理不算重复）；长题零 黑板 的实际介入时点 = max(第4轮轻推, 600s+二信号)。

## 10. Attempt / Scope / report（P0）

- **术语**：`Attempt` = 一条独立 session/执行线（原 BranchState）；`Scope` = 一组共享
  同一前文的 Attempt；`Checkpoint` = fork 时刻的回合边界前缀（`parent + forkSeq` 即其
  序列化）。不再用 parent/child 表达所有权；`parent` 只表示上下文血缘。
- **工具面**：fork 模式下新增 `report`，与 fork/log/state_change 一并下发。`report` 是
  Attempt 的唯一合法收尾动作；调用后该 Attempt `status` 置 done/deadend/blocked，
  `active=false`，结果卡写入 `log.jsonl`（kind=result），planner 读事件、相关 scope 读增量。
- **结果卡**：`{status, summary, evidence[], artifacts[], next[], at}`；只合并知识，
  不拼接对话历史。planner/相关 scope 可据此决定从 checkpoint 开新 Attempt、关闭 Scope
  或合并信息。
- **Planner（唯一统筹者）**：`src/planner.ts` 的启发式消费 report 事件；
  deadend 且 result card 给了 `next` 时，回收 source 并从它的 `checkpointMessages`
  spawn 兄弟（不复制失败尾部）；同一 scope 的 Attempt 共享 checkpoint/scopeId。
  动作由 controller 校验执行，planner 不跑 shell、不改 messages。
- **命令去重**：`BranchManager.commandSeenElsewhere` 跨 Attempt 检查最近命令；
  命中时在 shell 工具结果尾部提示，减少并行重复劳动。
- **Scope 状态**：任一成员 done → satisfied；所有成员 deadend/pruned → refuted；
  存在 blocked → unresolved；否则 open。`refreshScope` 在 report/prune/restore 时刷新。
- **持久化**：`BranchLite` 带 `scopeId/status/active/result`；续段恢复保留已结束
  Attempt 的状态（不再把所有分支都无条件激活）。剪枝分支仍按原逻辑不进入 `branches`
  （其事实保留在 log.jsonl；全量树持久化留待后续）。
- **兼容**：fork 指令仍以 `【分支 N · 方向】` 开头（测试/面板/旧记录依赖该前缀），
  正文说明其 Attempt/session 语义；report 结果卡用 `[Attempt N 已结束 · status]`。

## 11. 重启重建（write ledger + recovery owner）

- **记录共享写**：`state_change(action, recovery, status?, id?, scope?)` 把靶机写操作
  写入 `log.jsonl`（kind=mutation，status=pending）并返回 mutation id；`scope:"local"`
  只算 workdir 写，不进入恢复队列。
- **续跑规划**：`pendingMutations` 读取每个 mutation 的最新状态；`selectRecoveryOwner`
  只选**一个** active Attempt 当 recovery owner（优先共享写最多的 scope，其次 id 最小，
  无匹配则初始 Attempt 0），避免多个子 Attempt 同时写共享靶机。
- **告知全部**：所有 Attempt 收到 `RECOVERY_NOTICE`——重新下载/重新确认数据，
  不要假设旧会话/服务/文件还在；共享写只由 owner 执行，其他人只读。
- **只给 owner，一次一项**：`RECOVERY_ASSIGNMENT` 追加到 owner，只列下一项
  mutation id/action/recovery；完成后调用 `state_change(status:"recovered", id:"...")`，
  controller 再选 owner 下发下一项（可换人）。
- **结果广播**：恢复完成后，controller 把 `[重建完成]` 尾部追加给该 scope 及其后代
  scope 的 Attempt；其他 scope 不被打扰。

## 12. 产物去重

- `artifacts.ts` 在 shell 命令后做**有界扫描**（深度 4 / 400 条目 / 单文件 8MB / 每次最多 5 个），
  只对可能写文件的命令触发；记录文件（conversation/log/meta/events/tasks）不参与。
- 新文件按 sha256 进入 `BranchManager` 产物索引；命中其他 Attempt 的同哈希文件时，
  在工具结果尾部追加 `[产物去重] ...`，提示复用已有分析而不是重复下载/逆向。
- 产物事件写 `log.jsonl`（kind=artifact，含 hash/path/size）；续跑时从这些记录恢复内存索引，跨段也能去重。
- 共享写启发式：`looksLikeSharedWrite` 命中 systemctl/service/redis-cli/mysql/scp 等命令时，
  在 shell 结果尾部提醒调用 `state_change`；只提醒不拦截，避免误伤正常利用。

## 13. 并发 Attempt turn

- `AGENT_BRANCH_CONCURRENCY`（默认 4，最大 4）控制同时 inflight 的 Attempt turn 数；
  1 = 旧单飞轮转，>1 = 真正并发 LLM/tool turn。
- scheduler 用 `pickExcept(inflight)` / `ranked` 选 runnable Attempt，`Promise.race` 收割
  完成的 turn；`pruneStalled`/`planner 卡塞` 跳过 inflight，避免把正在跑的分支回收。
- **有 planner 自定义权重时**改用 `pickWeighted`：smooth weighted round-robin，每个正权重
  Attempt 都有保底机会；`weight` 默认 1，planner 可设 1–4，同值/冷却中不重复调整。
- 没有自定义权重时沿用 `ranked` 的优先级 + `rr` 轮转（不是 id 顺序）：高 id 新 fork
  不会被低 id 永久饿死。
- 每个 turn 在独立 `AsyncLocalStorage` 分支上下文里执行（shell job 归属不串味）。
- **并发注入邮箱**：branch 正忙于 LLM/tool 时，广播/结果卡/重建任务先入 `inbox`，
  下一轮开始再并入 messages；conv 注入也排队到本回合 tool 结果落盘后写，
  避免 user 消息插进 `assistant(tool_calls) → tool` 中间。
- 首请求冷启动由 `warmed` + `coldBusy` 保护：同一时刻只启动一个 cold Attempt；后续 turn 可重叠。
- **fork 优先调度**：`materializeFork`/`spawnFromCheckpoint` 给父 Attempt 1 票、新 Attempt 2 票；
  `pickExcept` 优先消费 boost，新 fork 下一轮先于父/其他 Attempt 获得调度。

## 14. Planner 持久化

- `tasks/<code>/planner.json`：结构化快照，独立于 append-only 的 `log.jsonl`。
  内容：`scopes`、`attempts`（parent/scopeId/status/result）、最近 `findings`、
  `mutations`、`recovery {owner, pending[]}`、`updated_at`。
- 每次 planner 动作 / 恢复 flush / 收段时原子写（tmp + rename）；
  重启后可直接查看“树 + 知识 + 待恢复”，面板/审计也可读。
- `log.jsonl` 仍是原始事实流；`planner.json` 是 planner 的结构化视图，
  二者不互相替代。

## 15. 上下文压缩（按 Attempt / 共享 fork 前缀）

- **触发**：每个 Attempt 独立看 `usage.prompt_tokens`，达到 `AGENT_COMPACT_TOKENS`
  （默认 400k）时在下一次 turn 开头压缩；找到 flag（多 flag 未全齐）仍可回合末强制压缩。
  压缩是元调用，不占 turn；失败退避 3 轮。
- **范围**：候选边界 = 各 Attempt 的 `checkpointMessages` 长度（fork 断点）+ 当前完整上下文。
  按**共享该前缀的存活 Attempt 数**优先，同覆盖取更长前缀；没有可共享前缀时退化为整段压缩。
- **只改触发 Attempt**：替换其共享前缀为交接文档，保留 fork 后的 tail；其他 Attempt
  不立即替换，等各自到阈值时复用同前缀的摘要（同一 solveTask 内 `sharedCompacts`；
  续跑按同样逻辑重算）。
- **持久化**：conv 写 `context_compact` 锚，随后把保留 tail 重放为 `compact_tail` 记录；
  重建时锚之后只保留这些 tail。分支含压缩锚时重建 = system/任务卡头 + 摘要 + tail，
  不再拼接完整父前缀，避免与摘要重复。
- **黑板**：压缩文档同步写 `【压缩交接】` 卡；事件 `context_compacted` 记录
  `prefix_len` / `coverage` / `reused` / before-after 字符数。
