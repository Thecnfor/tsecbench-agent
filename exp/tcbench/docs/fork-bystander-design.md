# Fork 多分支 + 旁观者 设计（tsec_agent）

状态：**已实现**（2026-10-16，`AGENT_FORK`/`AGENT_BYSTANDER` 默认 off；105 个 vitest 全绿，
`pnpm check` 通过）。实现映射：§1→branch_tools.ts/agent.ts 工具分发、§2→agent.ts `branchCwd`
+prompt.ts FORK_PROMPT_EXTRA、§3→branch.ts `prune`、§4→branch.ts `reserveFork` 护栏、
§5→bystander.ts + agent.ts `maybeBystander`、§6→records.ts `rebuildBranches`/corelog.ts。
缓存红线全部落实：分支指令 append 不动 system、广播/旁观者/剪枝摘要一律尾部追加、
轮转单飞天然串行（分支首请求并发竞争已被实测否决）。面板：LiveView 带分支名册与 黑板 尾部。

## 总纲（协作思想，行为改动先对齐这里）

- **界由 fork 划定，不轻越**：分支的范畴/任务只由 fork 指令规定；旁观者、广播、轻推等
  一切注入**不得给已分工的分支改派范畴外任务**——旁观者输入必须带「各分支分工」、
  系统提示有对应硬约束（实测教训：旁观者曾让已分工的分支 1「停手转去做主分支的活」，
  方向被弃用 + 重复劳动）；执行侧同理「不要轻易跨界，范畴外的发现走 log/上行广播」。
  换方向 = 主分支 fork 新分支，不是改道别人。
- **广播拓扑是星型（单向上行），不是全连网**：非主分支只发主分支（branch 0），
  横向目标自动改投主分支、由它统筹转发，子分支之间不直连；主分支 → 任意分支的下行
  保留。信息共享的主通道是 log 黑板，broadcast 只做「需要对方知道/协调」的点对点。

## 0. 依据（cache_exp 实测，deepseek-flash）

| 结论 | 数据 | 对本设计的约束 |
|---|---|---|
| 交错轮转免费 | a1b1a2a3b2 逐请求命中率 = 顺序执行（热身 95%） | 调度器不为缓存妥协 |
| 跨分支共享前缀命中 | 分支首请求 99.2%（miss 仅 180 tok） | 共享段只冷启动一次 |
| 改 system 伤首跳 | 21.8% → 下一跳自愈 94.9% | 分支指令只 append，不动 system |
| 尾部插入近免费 | 中途近尾插入 92.5% | 广播/合并/旁观者一律尾部追加 |
| 并发首发双冷启动 | a1‖b1 双 0% | 新分支首请求排队串行 |

## 1. 工具面（放宽单工具不变量，AGENTS.md 同步更新）

每个分支工具集一致（schema 稳定 → 前缀缓存稳定）：

- **`shell`**（现有）：cwd = 本分支命名空间；绝对路径可读任意处
- **`fork(direction: str)`**：**任意活跃分支可调**（含分支再分叉；护栏见 §4，树的深度
  自然有界）。controller 复制**调用方当前尾部** messages 为新分支 + 追加方向指令
  （含命名空间/广播规则/父分支）。护栏命中时**返回拒绝原因**（不静默）：
  活跃 >`AGENT_MAX_BRANCHES`(3) / 总数 ≥`AGENT_FORK_MAX`(5) / 冷却
  <`AGENT_FORK_COOLDOWN_SEC`(180s)。新分支记录 `parent`（面板树视图/审计）；
  首请求排队（并发首发护栏）
- **`log(note: str)`**：落 `log.jsonl` `{ts, branch, seq, note}`。旁观者与面板的
  唯一抽象层；不注入其他分支；controller 天然携带 branch，无文本解析
- **flag 事件（controller + 模型混合权威）**：flag 的稳定 ID = submit 响应的
  **`matched_flag_index`**——flag **内容**会因靶机重启/重置而变，**索引不变**
  （`1..total_flag_count`）。分工：**模型**在 submit 后立即 `log` 记
  `flag idx=<matched_flag_index>`（模型能从 curl 响应直接看到该字段）；
  **controller** 以 watcher 快照的 `correct_flag_count` 跳变为权威触发
  `{kind:"flag", branch, flag_index, correct_flag_count, total_flag_count}` 落账
  （模型漏报则 index 记 `?`，计数仍准确）。用途：① 哪个分支拿到第几个 flag
  （贡献度/剪枝决策）② 重启续跑后按 index 对账「还差 idx 2,4」而不比对内容
  ③ 进度统一表示 `2/5 (idx:1,3)`。`409 duplicate` 视为「该 index 已存在」不计新进展
- **`broadcast(note: str, to?: "all"|"trunk"|ids)`**：目标分支 messages **尾部追加**
  `user` 消息（`[分支N广播] …`），下一轮边界生效。限频
  `AGENT_BROADCAST_MAX`(8/段/分支) + 近似重复去重（归一化后编辑距离）。
  **拓扑夹取**：`fromId ≠ 0` 时横向目标（活着的兄弟）一律改投 `0`（返回文案注明
  「子分支仅可上行主分支；横向协同由主分支转发」），无效 id 照旧过滤拒绝；
  `fromId = 0` 的下行广播保持任意目标

## 2. Shell 命名空间

- 每题已有独立 workdir（`taskWorkdir(code)` = `/work/tasks/<code>`，宿主对应
  `runs/<benchid>/tasks/<题目id>`；`/work` 是 `runs/<TOKEN>` 卷挂载）。
  **trunk cwd = 题目 workdir 不变**；分支 cwd = `<workdir>/branches/<N>`
  （同容器；分支产物天然落在该题记录树下）
- 跨命名空间**读任意**（绝对路径；约定 broadcast 中通报产物路径，如
  `<workdir>/branches/2/scan.log`）
- **写靠提示词约定，不强拦截**：① 产物只写本分支命名空间 ② 命名空间外与容器级操作
  （重启服务/监听共享端口/iptables/改系统文件）**默认只读**，要动先 broadcast 协调
- shell job 元数据带 branch；进程组 kill/硬上限等现有语义不变

## 3. 主分支与合并

- **trunk（branch 0）职责特殊而非历史特殊**：默认解题线、永不被剪枝、合并汇入方、
  flag 收束以 trunk 进度为准（flag 本身仍由 controller 全局收割）；
  **fork 权不限 trunk**（护栏兜底，见 §4）
- **不存在历史合并**：各分支 messages 永不互插中段（缓存红线）。合并 = 发现层：
  ① broadcast 星型互通（子→主上行、主→任意下行，横向经主中转）② 剪枝/分支自停时
  controller 把该分支 黑板 尾部摘要 append 进 trunk
- 分支自停条件：拿满 flag（→ 全停收束）/ 主动放弃 / 被剪枝

## 4. 防无止境分支（护栏，全部 env 可调）

| 护栏 | 默认 | 说明 |
|---|---|---|
| `AGENT_MAX_BRANCHES` | 3 | 活跃分支上限（含 trunk） |
| `AGENT_FORK_MAX` | 5 | 每题 fork 总数硬上限（含分支再分叉；树大小有界） |
| `AGENT_FORK_COOLDOWN_SEC` | 180 | fork 冷却（对所有分支生效） |
| 预算共享 | 固定 | **fork 不增预算**：全分支共享段墙钟与轮数，分支越多每线越薄 |
| `AGENT_BRANCH_PRUNE_SEC` | 1200 | 分支 黑板/flag 双停滞 → 自动剪枝 + 摘要注入 trunk（**B 方案 2026-10-16**：曾为 600=与长题停滞阈值同值，剪枝先执行令分支旁观者窗口为0；1200 使 900s 触发后仍有 300s 救治窗） |

> 分支可再分叉：深度不单独设限——活跃上限 3 + 总数上限 5 + 冷却 + 预算共享
> 四道闸叠加后树天然有界（最坏 3 层：trunk→b1→b2 且总数 5 截断）。

## 5. 旁观者

- **触发（2026-10-16 重构：以 flag 为核心的两层判定）**：
  **⓪ flag 主时钟一票触发**：题级距上个新 flag idx ≥ `AGENT_FLAG_STALL_SHORT_SEC`(400)
  / `LONG`(900)——「长时间没 flag 就介入」；抓 run3 暴露的「黑板记卡不停 ≠ 真进展」
  盲区（旧停滞信号对记卡型空转免疫）。**其余四信号任二**：分支停滞（300/600s）、
  同命令≥3 重复、连接类硬失败≥3（对幻觉目标空转）、连续 correct:false≥2（幻觉提交）
  ——后两者零 LLM 成本纯结果解析；旁观者 prompt 同步做卡片间矛盾交叉核对
  （被自己证伪的证据/refused 刷屏/连续错提交仍坚持 → 点名质疑臆测）。
  （原「预算」信号退役：与 flag 主时钟同为题级时间窗且后者更贴终局。）
- **目标（可介入任何分支）**：存在停滞分支 → 救**卡最死的**（lastCoreTs 最老）；
  仅 flag 时钟触发（分支在干活但没 flag，如记卡型空转）→ 盯**最新鲜推进线**
  （lastCoreTs 最新）；无可选 → trunk
- **时钟必须跨段持久（2026-10-16 run4 实测修复）**：flag/停滞时钟锚在
  **题起点（TASK_START_TS，跨段不重置）+ 黑板 最后事件**，solveTask 启动时恢复——
  旧实现锚在 BranchManager 构造（每段归零），而段墙钟 = min(段上限, 剩余时限/6)
  会被短限时压到 ~192s（arena 2400s 实测），900s flag 主时钟与 600s 停滞在段内
  **数学上永不可达**（run4 旁观者全程静默的真相）。诊断日志：`flag-stall ARMED` /
  `bystander SKIPPED` 各每段一条，定位时钟与限频问题。
- **输入构建（处理交错，按分支分组）**：**各分支分工**（id→方向名册，见总纲「不轻越」）
  → 分支 A 黑板（时序）→ 分支 B 黑板（时序）
  → 全局进度（**flag 索引账本** `2/5 (idx:1,3)` + 耗时/分支数）→ 卡塞分支最近 10 条消息摘要
- **分工尊重（2026-10-17 实测修正）**：非主分支有专属 fork 方向时，指引必须**服务该方向**
  （方向内更细的角度/工具/验证），**不得弃用方向改派别的任务**、不得让它转做其他分支
  已负责的工作；方向已死 → 就地最小证伪 + log 记结论待回收，换方向是 fork 的事
  （实测：旁观者曾连续让已分工分支改做主分支的枚举活）。
- **本体**：同款 deepseek-flash 换 system（抽象全局视角；hosted 已验证可用）；
  独立短上下文，与主对话缓存互不干扰
- **输出**：尾部追加 `[旁观者] …` user 消息；`AGENT_BYSTANDER_MAX`(2/段/分支) +
  冷却 `AGENT_BYSTANDER_COOLDOWN_SEC`(400)；旁观者调用计入段预算

## 6. 记录与续跑

- `conversation.jsonl`：单流追加 + **`branch` 字段**（对话忠实落盘不变量保持）
- `log.jsonl`：新增，独立文件，
  `{ts, branch, seq, kind: core|broadcast|bystander|flag, note?, flag_index?,
  correct_flag_count?, total_flag_count?}`——flag 以 **index 为 ID**（内容随靶机重启变，索引不变）
- `rebuildMessages` 按 branch 重建多份 messages；tasks.jsonl 生命周期不变
- panel：分支泳道（甘特图 llm/tool 泳道按 branch 分色）、黑板 视图、旁观者事件标记

## 7. 开关与默认

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_FORK` | 0 | fork 工具与分支调度 |
| `AGENT_BYSTANDER` | 0 | 旁观者（依赖 log 工具，可单独开） |
| 其余 | 上表 | 全部可 env 覆盖 |

初赛镜像保持两开关默认 off；决赛（多智能体赛制）开启。

## 8. 合规

- 旁观者只读运行时派生的 黑板，不引入任何历史题解/题目特化内容
- 广播与旁观者消息全部落 conversation.jsonl/log.jsonl，审计友好
- 不改动平台流程、flag 收割、VPN/预检等既有不变量

## 9. arena 实测修正（2026-10-16 首跑）

**现象**：f2-05 跑 8 分钟 28 轮、反复使用 VM_KEY07（146 处），却 0 log / 0 fork
（同 run 的 web_sqli_01 正常调用 log → 工具链无 bug，tools 每轮随请求下发）；
旁观者未触发 = 停滞阈值 600s 而 run 494s 即停，三信号均未成立（按设计）。

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
  分工：B = 轮级分钟级轻推；旁观者 = 时间/行为信号的重量级插嘴。
- 观察留档：repeat 信号对「同前缀不同参数」的命令不敏感（8 条 `cd…; echo "` 系
  内容不同，合理不算重复）；长题零 黑板 的实际介入时点 = max(第4轮轻推, 600s+二信号)。
