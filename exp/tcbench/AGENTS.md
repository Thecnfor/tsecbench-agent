# AGENTS.md — AI 协作者须知

本文件是给 AI agent（以及新加入的人类开发者）的操作手册。改代码前必读。

## 这个项目是什么

腾讯 TSec Benchmark 平台的**自动跑分 agent**：LLM 驱动、只带一个**异步 shell 工具**，
跑在 **Docker（Kali）** 容器里，端到端完成「VPN 接入 → 拉题 → 渗透 → 提交 flag → close 释放」。
**实现语言：Node 26 + TypeScript**（pnpm + vite + vitest；面板 React + Node 服务）——
运行时零第三方依赖（LLM 调用用原生 fetch），镜像只带 `pnpm build` 的纯 JS 产物。

**任务背景（重要）**：
- 平台要求**每个 bench 在 10 分钟内接入**，节奏上要「快启动、快出分、快释放」。
- 当前跑**官方本地跑分**（用官方 API）；后续可能切**内部本地跑分**（自建计分服务）。
  「本地」只是相对**正式比赛**而言（独立题集/环境/计分），**两者都跑在 Docker 里**。
  代码不区分这两种模式——差异只在 `BENCHMARK_BASE_URL` 和 `BENCHMARK_TOKEN`，
  **任何设计都不要把平台地址/ token 硬编码进业务逻辑**。

**运行模式（`AGENT_MODE`，auto 自动检测）**：
- **local 本地模式**：开发调试用。模型走公网官方 DeepSeek API（`https://api.deepseek.com/v1`，
  key 在 `.env`），冒烟时 VPN 预检不通过仅警告（靶场渗透本身仍需 VPN）。
- **hosted 托管模式**：最终交付。镜像 **≤3GB** 上传官方平台，**无公网**，
  模型只能走平台内网转写网关（`http://api.deepseek.com.tsecbench.gw/v1`），
  网关域名依赖 VPN 内网 DNS，VPN 预检**强制**（entrypoint exit 11）。
  托管形态**没有 run.sh、没有卷挂载**（交付 = `docker save | gzip` 导出的镜像）：平台只注入
  `BENCHMARK_TOKEN`/`BENCHMARK_BASE_URL`（注入值可能已是转写形态
  `http://xxx.tsecbench.gw`；可选 `VPN_CONFIG` 传 .ovpn 全文，entrypoint 落盘 0600 后照常拨）；
  entrypoint 自建 `/work`，镜像自带 agent 代码。**实测（2026-10-15）平台预检无需容器内拨
  VPN 即通过（平台网络层已打通），且平台不授予 NET_ADMIN**——锁网（NET_RESTRICT=1）白名单含
  **DNS**、平台 API、**模型网关** 三类；**iptables 无权限时打一条 WARNING 明确跳过，绝不打
  "egress restricted" 假成功日志**；关键域名解析不出时 **fail-open 不锁网**（打 FATAL-SAFE）。
  **模式误判防线**（托管镜像无 .env、无本地 key）：hosted 网关探测 HTTPError 也算可达、
  探测失败给两次机会、双探测都失败且无本地 key → 兜底判 hosted 由冒烟报错；
  key 为空时 `make_client` 干净退出码 2，不再抛 openai traceback。
  **托管网关鉴权（实测）**：本届转写网关**透传 Authorization 给上游 DeepSeek**（401 是
  DeepSeek 原生报文）→ 托管必须用**真实 DeepSeek key**（平台注入 `DEEPSEEK_API_KEY`）；
  `HOSTED_API_KEY` 回落链（显式 > DEEPSEEK_API_KEY > tsec-bench）+ **key 候选发现**
  （`agent.discover_gateway_key`：冒烟/401 共用，候选按优先级
  **匿名(剥 Authorization 头，httpx2 钩子绕 openai 3.x 空 key 构造限制) → 显式
  DEEPSEEK_API_KEY_HOSTED → BENCHMARK_TOKEN → 真实 DEEPSEEK_API_KEY(最后，烧自己额度)
  → current-config 兜底**；每个候选一行掩码日志(只显尾4位)+gateway_key_probe 事件；
  冒烟 probe_all 全探、热切换停首个命中且只切一次）。
- **auto（默认）**：三步按序——① **结构信号**：平台注入 `BENCHMARK_BASE_URL` host 以
  `.tsecbench.gw` 结尾（托管转写形态）→ 0 网络直判 hosted（本地原始 `https://` 不误触发）；
  ② 探测 local `/models` 可达（需本地 key）→ local，否则探测 hosted（两次机会）→ hosted；
  ③ 都失败时**无本地 key → hosted**（镜像内无 `.env`）、有 key → local 由冒烟报错。
  实现见 `src/config.ts: detectMode()`；命令行 `agent.js --mode local|hosted` 可覆盖。
  **entrypoint 兜底**：`NET_RESTRICT=auto` 判公网 AI API 不可达且未显式指定模式时，
  直接导出 `AGENT_MODE=hosted`（防止平台侧设了 DEEPSEEK_API_KEY 后探测抖动误选 local）。
  **转写规则**（`toGatewayUrl`，selftest 断言）：只转写 `https://`，http/IP 字面量原样，
  `.tsecbench.gw` 幂等；显式 `DEEPSEEK_BASE_URL_HOSTED` 过同一转换。
- 凭证只放项目根 **`.env`**（`.gitignore` 排除，**严禁提交**）；优先级：真实环境变量 > `.env` > 内置默认。
- **注入分工**：hosted 托管模式下 `BENCHMARK_TOKEN`/`BENCHMARK_BASE_URL` 由**官方平台自动注入容器**
  （本地不配置、不传）；local 本地模式由 `run.sh --env-file .env` 注入——**不要在脚本里硬编码 token**。

## 架构不变量（不要轻易破坏）

1. **单工具设计（2026-10-16 放宽）**：基础工具只有 `shell`（异步 background 模式），**无 MCP**。
   fork 多 Attempt 与 planner 统筹是**默认开启的控制器层能力**（`AGENT_FORK` 默认 1；
   `AGENT_PLANNER` 默认 1；`AGENT_BRANCH_CONCURRENCY` 默认 4），
   打开后工具集扩展为 `shell + fork + report + log + state_change`——这是与评测方对齐后的演进
   （单线调试可设 `AGENT_FORK=0`，行为回到旧单分支）。planner 是唯一统筹者，没有特权主分支；`AGENT_BRANCH_CONCURRENCY>1` 时多 Attempt turn 真并发（注入走 inbox）；
   read/write/web 等文件类工具仍禁止
   （shell 已覆盖且有审计价值）。设计契约见 `../docs/fork-planner-design.md`，
   缓存实测依据见 `../cache_exp/README.md`。
2. **shell 是异步的**：`run` 立即返回 `job_id`，模型用 `wait/wait_any/status/kill/list` 收结果。
   - **同轮多个 tool_call 并发派发**（方案 A）：一轮里多条 shell 同时起跑，结果仍按
     tool_calls 原序收割配对（`agent.ts` 两阶段：prepared 先派发、再按序 await + 落副作用）。
     **`submit_flag` 例外：按题串行**（`bench.ts` 的 per-unique_code 队列）——平台 submit 的
     `correct_flag_count`/`is_completed` 不是并发安全的，同时到达的两个提交会读到同一个旧
     计数并丢掉一次更新（平台停在 3/4、重交只得 duplicate，计数不再补回）。模型侧由 prompt
     提示「有依赖的命令并进同一条」。
   - `run` 可带 `label` 给 job 起语义名（如 `oa-brute`）：list/status/wait 显示，
     wait/status/kill 也可按 label 寻址（唯一同名才可，重名报错提示用 job_id）。
   - 快速命令有 1.5s 同步窗口（`AGENT_SHELL_WAIT_MS`）；`run` 可带 `wait_sec`（≤300s），
     **工具替模型决定何时返回**（命令结束 / 攒够 ≥2KB 新输出 / 到点，先到先返回）。
   - `wait` 同为**边沿触发**，不再干等；输出分页 `offset/lines`（每条带 `next_offset` 续读），
     `out_file` 直接落盘到 workdir（被杀不丢，可 `tail`）。
   - 上限：每题同时在跑 `AGENT_SHELL_MAX_JOBS`（8，满了拒绝新 `run` 并提示先收）；
     硬上限 `AGENT_SHELL_HARD_SEC`（1800s，到点整组 kill，0=永不杀）——**不做 300s 软提醒**。
   - 进程必须独立进程组（Node：`spawn detached:true`；Python 版是 `start_new_session`），kill 时整组杀，超时由 watcher 兜底。
   - **多模态**：`action=view` 把图片以 `image_url` part 作为**工具结果**返回（tool 消息
     的 content 可以是 parts 列表）；网关拒图时 `stripImages` 剥掉图片重试一次并把
     `CFG.vision_ok` 置 False，本会话后续 view 走文本模式（只有元数据）。对话重建时
     列表型 result 原样还原，**不能 JSON.stringify 成字符串**。
3. **模型网关双模式**：local=官方 `https://api.deepseek.com/v1`（公网，key 在 `.env`），
   hosted=平台内网转写网关 `http://api.deepseek.com.tsecbench.gw/v1`（**依赖 VPN 内网 DNS，只在容器连上 VPN 后可解析**）。
   默认 `AGENT_MODE=auto` 自动探测切换，见架构不变量前的「运行模式」小节。
4. **VPN 只在容器内跑**：entrypoint 用 `openvpn` 在容器 netns 建隧道，
   需要 `--cap-add NET_ADMIN --device /dev/net/tun`。**永远不要在宿主机上连 VPN 或改宿主机路由/DNS**。
5. **预检是硬门槛**：`http://10.0.100.58` 返回 `status:"ok"` 才能进入跑分，
   否则 entrypoint exit 11、agent 直接报「VPN检测未通过,请检查靶场VPN网络配置」并停止。
   不允许用平台地址代替该预检。
6. **错误处置在提示词里**：`invalid_state`/`duplicate`/`task_not_found` 等平台错误码的处理规则
   写在 `prompt.ts`，与平台规则一一对应，改动前对照 README「平台流程」章节。
7. **上下文按题隔离（方案 A）+ 分段解题**：平台编排（选题/start/close/名额）由 `src/agent.ts` 控制器 +
   `src/bench.ts` 硬编码执行；模型只在**每题独立的 messages** 里解题（`solveTask`）。
   **不要改回单一大上下文**。
   - **每段预算以墙钟为主（`AGENT_TASK_SEGMENT_SHORT_SEC`=600s / `LONG_SEC`=1800s，按
     `classifyTask` 的字段分类取值）：段到点不判死**——`task_close{outcome:"paused"}`
     关题释放名额 → 上下文保留（`resumeMeta`；需要时由 `records.rebuildMessages` 从
     conversation.jsonl 重建）→ 之后 `start` 续跑（重启靶机），
     续跑在上下文末尾追加 `prompt.RESUME_NOTE`（新 target / 靶机内状态已重置 / workdir 保留 /
     已提交 flag 不丢 / 从上次进度继续）；`startChallenge` 后还会在所有 recovery 消息之后
     追加 `prompt.TARGET_NOTE`，用新 target 覆盖旧 mutation 文本里的历史地址；
     **本地全齐但平台复核仍未通关**（计数未同步）时追加 `prompt.COMPLETION_DESYNC_NOTE`
     （重交已有 flag、不要重做侦察/打新入口）。
     轮数 `AGENT_TASK_MAX_TURNS`（默认 120，0=关）
     只是安全网——实测段墙钟 p50=4.7min、p90=28.9min、max=158min，同样 60 轮墙钟差 30 倍
     （耗时由工具等待主导），所以墙钟当主闸、轮数防极端空转。
   - **通关即关容器（A）**：`solveTask` 每轮开头查 watcher 快照缓存（20s 刷新，零额外 API），
     平台确认满 flag 立刻收束 → **立即 `close` 释放槽位**（不等模型自己想起来收尾——b-02
     实测通关后白挂 22 分钟）；**多 flag 全齐由 submit_flag 响应确认**（
     correct_flag_count==total，或账本全部 index 已确认——平台计数滞后时账本兜底）
     → 回合末立即 done，不等 watcher 缓存/模型尾轮
     （衔接优化：槽位即刻归还调度器）；**pass 前再做平台复核**：本地全齐时先等
     `AGENT_FLAG_VERIFY_DELAY_SEC`（默认 5s）让平台计数落账，再在
     `AGENT_FLAG_VERIFY_TIMEOUT_SEC`（默认 20s）窗口内轮询 `GET /challenges`，
     只有 `is_completed=true` 才判通关；窗口耗尽仍为 false 记
     `completion_unconfirmed` 事件（本地账本与平台计数不一致）；复盘总结脱槽跑
     （纯 LLM 调用不占并发名额），限时 `AGENT_SUMMARY_TIMEOUT_SEC`（默认 300s），
     超时丢弃总结照常记 pass。
   - **段到点处置（保留 segment_continue 收尾语义 + D 配额规则）**：
     ① 段末零新 flag 的短题**动态改判长题**（治"字段说 easy 实际 200min"）；
     ② 有短题在等时长题配额 = `cap-1`（保底 1 槽给短题防饿死）——配额**按在跑长题扣减**
     （`orderCandidates` 前排长题名额 = quota − runningLong；实测 bug：只按排序快照给名额，
     已在跑的 2 长不计入 → 短题通关释放的槽被第三长抢走、三长占满 3 槽，sim 引擎本来就是
     按 `longRunning() < longQuota` 活配额判定，生产侧曾与之漂移）；
     ③ **配额内长题不轮转**——原地续（bench 实测：把配额长题轮走后调度器立刻用另一道长题
     补回同槽，等待的短题一无所获，纯重启浪费；打断 88→31.6 完成持平）；
     ④ 超配额长题（怪兽题改判后鸠占鹊巢）段到点必须让位；短题有等待者时照旧关题轮转。
   - **调度排序（D）**：长题早启动（开局就顶上去跑，不能最后才启动，估算降序），
     短题最短优先，暂停题 aging（`AGENT_RESUME_AGING_SEC` 默认 600s，等最久的先续——
     防 fresh 无限插队，实测曾有题暂停后等 118 分钟）。分类只用 API 字段
     （flag_count/difficulty/level），**不依赖历史基线**（正式比赛题目必变）。
     策略选择由 `sim/` 离散事件模拟器 bench 验证（74 测试含 `test/sim.test.ts`）。
   - **无人等名额就不关题（段到点 / giveup 都适用）**：若快照里**没有别的题在等
     这个并发名额**（无新题、也无待续跑的题——典型即"收尾只剩几题且都在跑"）：
     ① 段到点：不 `close`、不 `start`，**同一容器、同一上下文直接续一段**
     （事件 `segment_continue`）；
     ② giveup（所有 Attempt 已 park）：同样不 `close`、不 `start`，重新打开初始 Attempt、
     注入 `prompt.GIVEUP_NOTE` 原地续一段（事件 `giveup_continue`）。无题等槽时默认
     `AGENT_NO_WAITER_GIVEUP_MAX=0`（不限次数，最后几题不主动放弃）；有题等槽时才用
     `AGENT_GIVEUP_MAX=3` 收束。上下文和 target 都不变，省掉容器重启代价。
     有人等名额才照旧关题让位（长题另有配额规则，见上）。快照拿不到时保守按原行为关题。
     实测单段最长 158min 平台未超时砍题。
   - **上下文压缩（通用交接文档，2026-10-17 取代逐 flag 总结）**：
     **触发 = 单个 Attempt 的实测 `usage.prompt_tokens` ≥ `AGENT_COMPACT_TOKENS`（默认 400k）**；
     找到 flag（多 flag 未全齐）仍可在回合末强制压缩（信息密度最高）。
     压缩成功后 token 计数清零（真实 usage 重建口径），失败退避 3 轮。
     **压缩范围优先选 fork 断点前缀**：候选 = 各 Attempt 的 `checkpointMessages` 长度 +
     当前完整上下文；按**覆盖的存活 Attempt 数**优先、同覆盖取更长前缀。只替换当前
     Attempt 的前缀，保留它 fork 后的 tail；其他 Attempt 不立刻改，等自己到阈值时
     复用同一前缀的交接文档（同一 solveTask 内 `sharedCompacts` 缓存；续跑按同样逻辑重算）。
     没有可共享前缀时退化为整段压缩。
     元调用 LLM 生成 **交接文档**（①已确认事实**原值保留**②产物与在途：workdir 文件清单 +
     未完成的后台 shell 说明（**生命周期不归压缩管**，job 照跑）③已证伪死路
     ④当前进度⑤flag 进度，≤1500 字）；运行时替换为 [system, 任务卡, 文档, 保留 tail]。
     conv 记 `kind_note:"context_compact"` 锚 + `compact_tail` 保留段；分支重建遇到
     `context_compact` 锚时只取 system/任务卡头 + 摘要 + tail，不再拼完整父前缀
     （防摘要与父前缀重复）；黑板同步一张【压缩交接】卡；压缩是元调用不占轮次。
     **单 flag 收尾总结轮保留**（submit 后 ≤120 字总结即收尾）。
   - **自动黑板（2026-10-17，治"提示词暗示不可靠"）**：模型每轮的**进展叙述**
     （assistant content，不含 thinking/tool；≥8 字）由控制器**自动落黑板卡**
     （≤400 字/轮）——b-02 实测 123 次 shell 仅 1 张实质卡，靠提示词自觉记黑板
     不可行；记卡即核心事件（停滞时钟刷新，黑板确实在长不算隐形空转）。
     log 提示相应减负（撤"立刻记×3/频率/回收威胁"轰炸，保留"写凭据/证伪/flag idx
     + 判断带置信"）；手动 log 仍用于证据卡与 flag idx 记账。
   - **只有三种终止**：上下文超模型窗口（`context_overflow`，立即停不重试）、
     有题等槽时连续 `AGENT_GIVEUP_MAX`（默认 3）段「未通关就输出总结」、真失败累计
     `AGENT_MAX_FAILURES`（默认 2）次；其余情况一律续跑到通关或平台终态。
     真失败/启动失败计入 `failures` 字典；`attempts` 只用于 attempt 编号与启动失败计数。
   - 暂停、续跑**不消耗**失败次数。
8. **bench 记录与隔离（src/records.ts）**：
   - 记录目录唯一键 = **token**（`docker/runs/<TOKEN>/`，无时间戳；同 token 重启 append）；
   - **隔离靠挂载边界**：`run.sh` 只挂当前 token 目录为 `/work`——容器内只见当前 bench，
     其他 bench 不在挂载内；**不做 shell 拦截**；旧 bench 由 `docker/archive_runs.sh`
     归档到项目内 `docker/archives/`（挂载范围外、gitignore 排除，存留用户自行管理），
     归档后容器挂载内彻底不可读；
     容器内 root 产物（openvpn 强制 0600 的日志、root 属主的 `tasks/` 目录）会让宿主机
     tar/rm 失败：`run.sh` 启动前预建宿主机属主的 `openvpn.log`（openvpn 只写内容不改属主），
     归档脚本在失败时用 `tsec-agent` 镜像以 root 归位属主后重试（docker 组走 newgrp，
     同时修好"运行中容器占用检查"），docker 不可用则报错并保留源目录；
   - **对话忠诚不裁剪**：发送给模型的 system/user、模型的 thinking/output/tool_calls、
     工具 result 全部原样落 `conversation.jsonl`（含 ts/seq/turn/tool_call_id/**branch**），
     **禁止任何 _clip/截断逻辑进对话记录**；
     **每条消息带成对起止时间戳**（`started_at`/`ended_at` epoch 秒·毫秒精度 +
     `duration_ms` 自动计算；assistant=LLM 调用起止、tool=工具执行起止，
     埋点在 `solve_task`/`closing_summary`）——直接可画甘特图（llm/tool 泳道）；
   - **fork 分支记录（2026-10-16 增）**：conversation.jsonl 单流追加 + `branch` 字段
     （0=初始 Attempt，缺省视为 0 兼容旧记录）；fork/spawn 写 `kind_note:"fork_directive"`
     （含 parent_branch/fork_seq/fork_label）；**`log.jsonl` 独立文件**记
     core/planner/flag/prune/fork/result/mutation/artifact 核心事件，**flag 以
     `matched_flag_index` 为稳定 ID**（内容随靶机重启变、索引不变）；
     暂停续跑时初始 Attempt 用 `rebuildMessages`（branch=0 过滤）、后续 Attempt 用 `rebuildBranches`
     （fork 源 **fork_seq 快照截断** + 分支自身记录；fork 后 fork 源的轮次不得进入子 Attempt）；
   - 题目生命周期：`tasks.jsonl` 成对记 `task_start{started_at,target,attempt,resume}` /
     `task_close{closed_at,duration_sec,outcome,flags}`（`outcome` ∈ `pass`/`fail`/`paused`）；
   - **通关复盘**：题目全部 flag 提交后，**同一上下文直接追加**一条复盘请求
     （`src/prompt.ts: CLOSING_SUMMARY_PROMPT`：解题思路 / 工具使用困难 / 工具集优化建议），
     由 deepseek 输出；复盘作为**普通 user/assistant 消息**原样写入同一 conversation.jsonl
     （带 `kind_note: "closing_summary"` 标记，`task_close.summary` 同步存全文），
     未通关的题不复盘；复盘完丢弃 messages（销毁该题 agent 上下文）；
   - **平台终态立即停止**：`src/bench.ts` 把 `invalid_state + "finished"` 分类为
     `TaskFinishedError`（评测截止终态）→ `src/agent.ts: platformEnded()` 置全局 `STOP` 标志：
     调度器/worker（solveTask 每轮检测）/watcher/callLlm 重试全部收敛退出，
     join 所有 worker 后尽力 close 其靶场容器，写 `run_finish{reason:"platform_ended"}`
     正常退出（exit 0，容器随之停止）。**禁止把终态当普通网络错误无限重试**；
   - **VPN 运行时守卫**（`VPN_GUARD`，auto=hosted 开/local 关，或 1/0 显式）：调度器周期
     （默认 20s）探测预检地址，**连续 3 次失败** → 置 `VPN_DOWN`：暂停派发新题
     （**不消耗 attempts**；平台 API 走公网，snapshot/终态检测/释放照常）；worker 的
     `solve_task` 检测到后**挂起等待**（不烧轮次预算，conv 记 suspend/resume note，
     上限 `AGENT_VPN_WAIT_MAX` 默认 600s 超时才弃题）；探测恢复 → 自动清除、原地续跑。
     **判弱点：绝不因 VPN 中断把题库烧成假 fail**；
   - **任务不跨 bench 共享**：不同 bench 题目/代号不同，记录互不共享（防跨 bench 作弊）；
     token 明文不落盘（meta 只存 hint，目录名本身即 token 属挂载语义）。

## 文件地图

| 路径 | 职责 | 什么时候改 |
| --- | --- | --- |
| `tsec_agent/src/agent.ts` | 控制器主循环：选题 → start → `solveTask` 单题上下文 → 校验 → close；LLM 重试、并行调度、`--smoke` | 加运行控制逻辑 |
| `tsec_agent/src/bench.ts` | 平台 API 客户端（fetch/start/close/选题策略、名额释放） | 平台流程变化 |
| `tsec_agent/src/shell.ts` + `shell_tool.ts` | 异步 shell：Job 生命周期、输出截断、进程组 kill；每题独立 ShellManager（job 表跨题完全隔离）+ job 归属 Attempt（list/wait/status/kill 隔离，每 Attempt 配额 `AGENT_SHELL_MAX_JOBS_PER_BRANCH`）；`wait_sec`/`wait_any` 边沿等待、`offset/lines` 分页、`out_file` 落盘、job 上限与 1800s 硬上限；`parseToolArguments` 工具参数容错（坏参数自动修复）；`view` 多模态读图 | 调工具行为、参数解析 |
| `tsec_agent/src/llm.ts` | OpenAI 兼容 fetch client、重试策略、key 候选发现与 401 热切换 | 网关协议/鉴权变化 |
| `tsec_agent/src/prompt.ts` | 单题解题 system prompt + 任务卡模板（渗透方法、提交协议、结束约定） | 平台规则变化、提示词调优 |
| `tsec_agent/src/records.ts` | bench 级持久化：`runs/<TOKEN>/` 下 meta.json/events.jsonl/tasks.jsonl/tasks/<code>/conversation.jsonl（+log.jsonl）；`rebuildMessages`/`rebuildBranches` 从对话重建续跑上下文（分支按 fork_seq 截断） | 记录字段调整、续跑上下文 |
| `tsec_agent/src/branch.ts` | Attempt/Scope 执行面：状态/护栏/轮转/广播（尾部追加+限频+去重；**Attempt 对等，无特权主分支**）/report 结果卡/`spawnFromCheckpoint` 回溯 | 分支策略调整 |
| `tsec_agent/src/branch_tools.ts` | fork/report/log/state_change 四工具 schema 与分发（fork 两阶段：reserve 护栏→轮末 materialize；report 收尾 + result card；log 同时送 planner） | 工具契约变化 |
| `tsec_agent/src/planner.ts` | 唯一统筹者：消费 report/stall/log 事件，启发式决定提醒/回收/checkpoint spawn；不跑 shell/不改 messages | planner 策略调整 |
| `tsec_agent/src/planner_state.ts` | `tasks/<code>/planner.json`：scopes/attempts/findings/mutations/recovery 结构化快照（tmp+rename） | planner 持久化字段 |
| `tsec_agent/src/corelog.ts` | log.jsonl 读写 + flag 索引账本（matched_flag_index 为稳定 ID）+ Attempt 分组视图/planner 事件源 | 黑板日志格式 |
| `tsec_agent/src/stall.ts` | 卡塞信号（flag 主时钟 + 四信号任二）+ planner 提醒模板；由 controller 事件化交给 planner，无独立 observer LLM | 卡塞判定/提醒文本 |
| `tsec_agent/src/artifacts.ts` | shell 后新文件的有界扫描 + sha256；跨 Attempt/跨段产物去重提示；共享写命令启发式提醒 | 产物去重/扫描阈值 |
| `tsec_agent/src/panel/` | 本地观察面板（React 前端 `web/` + Node 服务 `server.ts` + 数据层 `data.ts`；只读记录+平台 API；RUNNING/PASS/FAIL、进行中 shell、模型输出、flag 数——有选题时按选题范围统计，取 `meta.runs[-1].task_filter`，空=全局；**按 `docker/archives/` 归档做实时耗时预测对比**：提前完成即扣、超时逐帧累加） | 面板展示项 |
| `tsec_agent/src/config.ts` | 全部配置与默认值（环境变量可覆盖）；含 fork/planner 开关 `AGENT_FORK`/`AGENT_PLANNER`、`AGENT_BRANCH_CONCURRENCY`/`AGENT_MAX_BRANCHES`/`AGENT_FORK_MAX` 及护栏/提醒阈值 | 加配置项 |
| `tsec_agent/test/` | vitest 自测（`e2e.test.ts` 离线端到端：fake 模型驱动工具循环 + 记录断言） | 改动主循环后必须跑 |
| `arena/` | 内部本地跑分题场：官方 Challenges API 线格式兼容的计分服务 + **docker 真靶机驱动**（题包在 `arena/challenges/`，含 flag 字面量——`.dockerignore` 已排除 `arena/`，**红线别删**）；行为契约见 `arena/README.md`，接入只改 `BENCHMARK_BASE_URL`/`BENCHMARK_TOKEN` | 内部本地跑分、加题 |
| `docker/Dockerfile` | Kali + 渗透工具 + openvpn + 纯 JS agent 产物（`tsec_agent/dist/`，构建前先 `pnpm build`）+ 离线知识库（构建上下文 = 项目根，根 `.dockerignore` 排除凭证/记录/node_modules） | 加工具/依赖（**镜像 ≤3GB 是压缩后口径**） |
| `docker/entrypoint.sh` | 容器入口：VPN → 预检 → DNS → 冒烟 → 跑分 | 接入流程变化 |
| `docker/run.sh` | 一键启动（选最新 vpn 配置、挂卷、环境变量） | 启动参数变化 |
| `docker/archive_runs.sh` | 归档记录到 `docker/archives/`（容器 root 残留用镜像归位属主兜底） | 归档行为变化 |
| `security-knowledge-base/` | 离线安全知识库（16 源：CVE POC 文章 + nuclei 模板 + hacktricks 等，196MB）；进镜像 `/opt/security-knowledge-base`，**gitignore 排除不入库** | 更新知识库 / 检索规则（写在 prompt） |
| `README.md` | 面向人类的完整文档 | 任何对外可见的变化 |

## 常用操作

```bash
# 离线自测（改完代码先跑这个，不需要 VPN/网关；193 个 vitest 用例）
cd tsec_agent && pnpm install && pnpm test

# 一次性检查：类型检查 + biome lint + 全部测试（提交前自查）
cd tsec_agent && pnpm check

# 格式化（biome；配置在 biome.jsonc，lineWidth 100 对齐仓库 ruff 口径）
cd tsec_agent && pnpm fmt       # 自动修复
cd tsec_agent && pnpm lint      # 只检查

# 类型检查 + 构建 agent 与面板（镜像里只带 dist/ 产物，构建镜像前必须先跑——
# 镜像构建阶段会 `node --check dist/agent.js` 硬验证，dist 缺失直接构建失败）
cd tsec_agent && pnpm typecheck && pnpm build

# 观察面板（React；读最新 runs/<TOKEN>，浏览器打开 127.0.0.1:8791）
cd tsec_agent && pnpm serve

# 内部本地跑分题场（官方 API 线格式 + docker 真靶机；agent 侧把
# BENCHMARK_BASE_URL 指到 http://<宿主机>:8899、BENCHMARK_TOKEN 对上 ARENA_TOKEN 即可）
cd arena && pnpm install && pnpm check   # typecheck + 22 用例（离线，FakeDriver）
cd arena && pnpm dev                     # 起 127.0.0.1:8899（真 docker 驱动，预热构建靶机镜像）

# 构建镜像（上下文 = 项目根，`-f Dockerfile` 指向本目录的 Dockerfile；根 .dockerignore 排除凭证/记录）
cd docker && newgrp docker -c "docker build -f Dockerfile -t tsec-agent:latest .."

# 导出交付镜像（平台托管模式：docker save 的内容 = 镜像内 /agent/dist 纯 JS 产物，
# 构建前务必先 pnpm build，否则 save 出去的是旧 dist）
cd docker && newgrp docker -c "docker save tsec-agent:latest | gzip -1 -c > tsec-agent.tar.gz"

# 启动 + 观察
cd docker && ./run.sh
newgrp docker -c "docker logs -f tsec-agent"

# 容器内手动调试
newgrp docker -c "docker exec -it tsec-agent bash"
```

注意：宿主机用户 `xsf` 已在 `docker` 组，但**新开的 shell 需要 `newgrp docker -c "..."`** 才能生效
（组变更不回溯到已有会话）。

## 平台 API 速查（全部带 `BENCHMARK_TOKEN` 请求头）

```
GET  {BASE}/openapi/v1/challenges                      题列表与进度
POST {BASE}/openapi/v1/challenges/start?unique_code=X  启动容器（active 上限 3，10min 内接入）
GET  {BASE}/openapi/v1/challenges/hint?unique_code=X   提示（看后扣分，通关后 409）
POST {BASE}/openapi/v1/challenges/submit               {"unique_code","flag"} → correct/awarded/...
POST {BASE}/openapi/v1/challenges/close?unique_code=X  释放名额（通关/放弃后必须调）
```

错误码：`task_not_found`(token 错，停) / `challenge_not_found`(跳过) /
`invalid_state`(超时结束→停，max active→close 再 start) / `duplicate`(幂等，跳过) /
`resource_unavailable`(重试后换题) / `internal_error`(重试一次)。

## 环境依赖（宿主机一次性准备，已完成，勿重复）

- Docker daemon + 用户进 `docker` 组
- `/etc/docker/daemon.json` 国内 registry-mirrors（Docker Hub 直连超时）
- Dockerfile 内已把 Kali apt 源固定为 TUNA/USTC/阿里云
  （官方源 302 到 `mirrors.neusoft.edu.cn`，它只有 IPv6，容器内解析失败——这是坑，别改回去）
- VPN 配置文件 `task_<id>_vpn_config.ovpn` 每个 bench 任务重新下发，`run.sh` 自动选最新

## 改动检查清单

- [ ] `pnpm check` 通过（`tsc --noEmit` + `biome check` + `pnpm test`，离线不依赖外部服务；含 `e2e.test.ts` 端到端）
- [ ] 改了代码 → **`pnpm build` 后再 build 镜像**（镜像只 COPY `tsec_agent/`，`dist/` 是 gitignore 的构建产物；
      Dockerfile 里 `node --check /agent/dist/agent.js` 会硬验证，dist 缺失构建直接失败，不会产出坏镜像）
- [ ] `node dist/agent.js --smoke` 通过（本地模式不强求 VPN；hosted 模式必须全绿）
- [ ] 凭证只出现在 `.env`（已 gitignore），**没有硬编码进代码/文档/镜像层**
- [ ] 改了 Dockerfile/entrypoint → 重新 build + `--smoke` 验证
- [ ] **镜像体积 ≤3GB——口径是 push 上传后的压缩体积**，不是 `docker images` 的原始数字：
  `docker save tsec-agent:latest | gzip -1 -c | wc -c` 实测。
  **当前（2026-10-16）：原始 10.1GB / 压缩 2.71GB**（比值约 3.7×，余量约 290MB）。
  已按要求装入：`metasploit-framework`、`hashcat`（无 GPU=CPU/pocl，只做离线爆破）、`zaproxy`（`-cmd` 无头）、
  `nuclei`、`responder`、`netexec` + BloodHound 采集器（`bloodhound-python`/`bloodhound-ce-python`/`sharphound`，
  **不装 neo4j 服务端**）、`aircrack-ng`、`volatility3==2.28.2`（pip，命令 `vol`）、`ghidra`
  （headless 入口 `analyzeHeadless` 已链到 PATH）、`security-knowledge-base`、
  JRE/python2/ssh/php/gcc/make/node/go/telnet；
  **2026-10-17 补齐**：`tesseract-ocr`+`eng`（验证码取字）、`feroxbuster`、`busybox-static`、
  seclists 保留 `Discovery/DNS`（子域字典）、`kadimus`+`nomore403`（源码构建，LFI/403 绕过）、
  `/opt/portable/{mysql,redis-cli,socat}`（自包含可搬运客户端：二进制+libs+loader，靶机缺客户端时 scp 上去 `run.sh` 即跑）；
  **版本口径**：基底按 digest 钉死、pip 精确钉版本、apt 跟 rolling 最新，全量快照在镜像内 `/opt/toolchain.lock`
  （硬钉某包照它写 `pkg=ver`）；**新增安装层自己清理 doc/man/locale**（清理层在前，之后的层会自带一批），
  且**删除只在 builder 阶段有意义**（最终阶段删 = 白花一层 whiteout，基底字节仍在 save 里）；
  读图走多模态（模型自己看图），`tesseract` 仅作验证码取字的 shell 命令（eng 训练包）；
  `seclists` 全量字典、`oracle-instantclient`、`nuclei-templates` 全量仍不要装
- [ ] **限网运行**：容器只允许 AI API + 靶场 VPN 出站，公网全 ban。
  新依赖必须**预装进镜像**（apt/pip/字典），不要依赖运行时联网安装；
  新的外联需求先加到 Dockerfile 再改提示词
- [ ] **只放通用资源，不放题目特化资源**：题目每次都变，源码仓库/PoC/特定 CVE 材料
  一律不进镜像（上轮用过的仓库不代表下轮有用）；只保留通用库（exploitdb/searchsploit、
  seclists、rockyou、dirb）和跨题常用工具
- [ ] 改了平台相关逻辑 → 对照 README 平台流程与错误码
- [ ] 没有破坏上述架构不变量（单工具、容器 VPN、预检门槛、不硬编码平台差异）
- [ ] README/AGENTS.md 同步更新
