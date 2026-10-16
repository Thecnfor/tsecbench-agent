# 第三届腾讯云智能攻防挑战赛 · 主赛线上初赛 Agent

> 本目录是当前唯一的设计与运行文档来源。代码尚未开始实现。

## 赛事定位

| 项 | 内容 |
|---|---|
| 赛事 | 第三届腾讯云智能攻防挑战赛（原腾讯云黑客松智能渗透挑战赛） |
| 官网 | https://zc.tencent.com/hackathon |
| 我们要打的阶段 | **主赛线上初赛** —— TSecBench 平台托管运行，全新题集，自主渗透 |
| 平台 | https://tsecbench.zc.tencent.com |
| 报名截止 | 2026-10-15 00:00 |
| 线上初赛 | 2026-10-17 ~ 10-18 |
| 线下决赛 | 2026-10-30（AI-KOH「王座协议」多智能体同场攻防，**不在本次镜像范围内**） |

线上初赛从全部参赛队伍中选拔 10 支战队进入线下决赛。决赛才是「争夺据点、攻守易位、AI 对抗 AI」的赛制；初赛是纯粹的自主渗透解题。

## 交付物

Docker镜像(限制3G)

## 运行环境约束

| 约束 | 值 | 影响 |
|---|---|---|
| 资源 | 8 核 16G | 并发 3 题 → 每题约 2.5 核 / 5GB，必须做进程级资源限制 |
| 公网 | **完全禁止** | 所有大模型调用必须走平台网关；所有工具与词库必须离线打包 |
| 靶场可达性 | 需 VPN（本地模式由我们拨；托管模式推定平台侧已打通） | 见 [open-questions.md](open-questions.md) |
| 沙箱 | 独立、一次性、结束即销毁 | 不能依赖任何持久化状态 |
| 审计 | 全程网络流量与大模型对话记录被审计并部分公开 | 日志必须干净；镜像内不得含任何密钥 |
| 禁止事项 | Agent 不可内置针对题目的历史答题记忆或解题方法 | 不得硬编码 flag、题目答案或题解逻辑，违者按作弊处理 |

## 关键决策摘要

| 决策 | 结论 | 详见 |
|---|---|---|
| 架构路线 | **混合**：自研确定性编排层 + 自研薄壳作解题执行体 | [architecture.md](architecture.md) |
| **五栈分层** | **按执行位置分层，不按「哪个语言更先进」分**：L0/L1 **Python 3.13**（编排 + 解题）· L2 **Go 1.26 + Rust**（静态二进制工具面）· L3 **Node 26**（MCP / REA 能力面）· **L4 Lua（跑在目标进程里的注入面）** | [architecture.md](architecture.md) §1.0 |
| 编排器语言 / 解题脚本 | **Python 3.13 + asyncio** / **Python 3.13**（原推荐 Rust，2026-10-10 翻转） | [architecture.md](architecture.md) §1.1 |
| Python 版本 | **钉死 3.13**（trixie 自带，**不再需要 standalone 包**）。2026-10-16 从 3.14 降级：`openhands-ai` 全系 `requires_python <3.14`，作为库装不进来；代价是弃用 PEP 758 无括号 except（全仓 12 处已改写）。3.15 发布仅一天，`cp315` wheel 为零 | [architecture.md](architecture.md) §1.1、§10.5 |
| **Go 1.26** | **不只用现成件（naabu/httpx/nuclei/katana），还承担唯一自研二进制 `recorder`** —— 每题一个流量录制/重放 sidecar。它是 §2.3「不信任模型自述」**唯一能落地的形态**：`prior.assets` 来自客观录制而非 LLM 自报，并给 Flag Harvester 多一条通道、支撑探测去重。选 Go 的理由是**打包形态**（单静态二进制、运行期零依赖），不是「比 Python 快」 | [architecture.md](architecture.md) §1.2 |
| **Node 运行时** | **26.8.1，精确小版本钉死**；非 Bun。承载 REA + 自研 TS MCP server。<br>⚠️ **Node 26 到 2026-10-28 才进 Active LTS，即初赛之后** —— 官方要求生产只用 LTS，这是**已知未消除**的风险。对冲：MCP server 构建期编译成纯 JS，不把原生 TS 直跑当运行前提 → **切 24 LTS 只是一行改动** | [architecture.md](architecture.md) §1.3、[open-questions.md](open-questions.md) B12 |
| **Rust** | **角色是「消费 Rust 生态」不是「写 Rust」**：`feroxbuster`（高速爆破）+ **`ripgrep`（收割热路径 —— 因 recorder 产出数百 MB~GB 级 JSONL 而新增的真实需求）**。自研默认不做；**绝不做 PyO3/napi-rs FFI**（摧毁崩溃隔离，且无热路径） | [architecture.md](architecture.md) §1.4 |
| **性能模型** | **端到端约 55% 是 LLM 往返、30% 工具执行、10% 重复劳动、5% 编排/IPC —— 语言只影响那 30%。**「性能强悍」的正确目标函数是三条架构手段：①提高单次工具调用信息密度以压往返 ②单题内并发扇出探测 ③recorder 去重永不重复发包。**不做**分布式 / Rust 重写编排 / FFI | [architecture.md](architecture.md) §1.6 |
| Zig / Bun | **都不引入** | [architecture.md](architecture.md) §1.7 |
| **Lua（L4）** | **不是「第五种实现语言」，是新的执行位置** —— 唯一能把逻辑送进**对方进程**的语言：nmap NSE 引擎（本机实测自带 **612 个 Lua `.nse`**）、Redis `EVAL`、OpenResty 都内嵌 Lua。**不新增运行时**；`lua5.4`（钉它是因为实测 `ldd` 显示 nmap 链接 `liblua5.4.so.0`，**不是**因为它是最新版 —— Lua 官方已是 5.5.1）仅为本地自检，**不装 luarocks、无 Lua 第三方库**；solver 不独立跑 `lua`，一律经 nmap/redis-cli 注入 | [architecture.md](architecture.md) §1.5、[image-spec.md](image-spec.md) §3 |
| **内层执行体** | **自研薄壳**（原「择一现成框架」已废）。参考实现全部 clone 到 `refs/` 读源码后借鉴：ARTEMIS 的 supervisor/子 agent/分流、ctf-agent 的 Coordinator+Solver swarm、**ARTEX 的双图架构**、BreachWeave 的窗口释放策略。**不整体移植任何一个**（每条都有硬阻塞） | [architecture.md](architecture.md) §2.4、§2.5 |
| **跨轮次状态** | `prior_findings` 自由文本 → **结构化双图 `prior{assets, explored, open_leads}`**，新增 `out/prior.json` 与 `out/traffic/*.jsonl` 通道；`explored` 必须记录**已否证的假设** | [architecture.md](architecture.md) §2.1、§2.2、§2.5 |
| 基座镜像 | **`debian:trixie-slim`**，不用 Kali | [image-spec.md](image-spec.md) |
| **Ghidra + JDK** | **装**（原「先不装」已翻转）。Ghidra 12.1.x + JDK 21 完整版 | [image-spec.md](image-spec.md) §3.2 |
| **REA**（`morluto/rea`） | **装**，133 个逆向 MCP 工具，MIT 许可。**版本必须钉死**（clone 时 6.3.0，五天从 5.0.0 走到 6.3.0）；运行期禁 `npx` | [image-spec.md](image-spec.md) §3、§3.1 |
| MCP 工具层 | REA 必装；hexstrike-ai 评估装；mcp-security-hub / mcp-for-security 评估。**原则：能用 CLI spawn 的就不套 MCP** | [image-spec.md](image-spec.md) §3 |
| playwright + chromium | **装** | [image-spec.md](image-spec.md) |
| metasploit | **不装** | [image-spec.md](image-spec.md) |
| **ARTEX** | **只读架构，不进镜像、不引用代码** —— AGPL-3.0 + 强制 PostgreSQL + **原仓库已从 GitHub 下架**（CrowdStrike 报告点名后的时间线高度吻合） | [architecture.md](architecture.md) §10.2 |
| 体积 | **压缩后 ~1.88GB**（原 1.41GB）。**头号风险：3GB 若指解压后则直接超标**，已备好降级阶梯 | [image-spec.md](image-spec.md) §4、§4.1 |
| API 限速 | **默认自我限速 ≤3 req/s**（上届规则，本届未提但必须防）；新增 `403` 关卡门与 `429` 的防御性分支 | [platform-api.md](platform-api.md) §4.11–4.13 |
| hint | **不取 hint**（链路整体删除）—— 成本不可知（`hint_cost_radio` 读不到）+ 实测 10 次仅 1 次换出解出 | [platform-api.md](platform-api.md) §4.7 |
| **合规红线** | `refs/` 与两届获奖队素材**绝不进镜像**，也不把上届题解喂进 prompt —— 平台按作弊处置且全程审计 | [architecture.md](architecture.md) §10.4、[image-spec.md](image-spec.md) §6 |
| 大模型 | **DeepSeek**（`api.deepseek.com`，OpenAI 兼容 `/v1`） | [platform-api.md](platform-api.md) §4 |
| **Jev（10-10 决策：完全排除）** | **不落地任何 Jev 代码**：核查结论是「产品真实（勘误 §11.2）但托管不可达 + 本地收益未验证」，用户决策**完全排除** —— 判定层不建、零新增依赖、零新增环境变量；相关落地项（原 A20、原 B14、runbook/image-spec 的 Jev 条目）已全部撤回 | [architecture.md](architecture.md) §12 |
| **旗舰评测集口径（10-10）** | **63 题 / 23000 分 / 360min / 六能力域（Web·二进制·利用·多阶段·云·对抗规避）/ normal**；`TOTAL_TIME_LIMIT` 默认 21600s、Phase 1 ≈7~8min/题；六域覆盖自检与两处缺口（横向/云）见 §13.2 | [architecture.md](architecture.md) §13 |

## 文档索引

| 文件 | 内容 |
|---|---|
| [platform-api.md](platform-api.md) | 答题 API 梳理、调用流程、错误码、以及文档里容易踩的陷阱（**§4.11–4.13 是与上届的规则差异**：关卡门、限频、接口整体换代）。摄取契约与下发形状不确定性见 [architecture.md](architecture.md) §6.5 与 §7.1 |
| [architecture.md](architecture.md) | 完整架构设计：语言分工、Solver Adapter 契约（含双图 `prior`）、借鉴清单与自研边界、组件、状态机、预算策略、错误处理、双模运行、测试策略、决赛预留。**§1 是五栈按执行位置的分层设计（L0~L4）+ 性能模型；§9/§11 是两轮外部技术栈建议的逐条核查（含一处已撤回的错误判断）；§10 是开源生态调研轮：refs/ 清单、两个供应链事件、上届官方文档挖到的平台事实、合规红线；**§12 Jev 排除决策（含 §11.2 勘误）；§13 旗舰评测集口径（63/23000/360min/六域）** |
| [image-spec.md](image-spec.md) | 基座选择理由、工具来源策略、工具清单（含 MCP 工具层）、运行时版本钉死、Ghidra+REA 决策依据、体积预算与降级阶梯、构建规范、密钥与参考资料泄漏防线 |
| [runbook.md](runbook.md) | 本地评测怎么跑、托管镜像怎么交付、测试矩阵 |
| [harness-v2.md](harness-v2.md) | 五层升级契约（Staircase 流式 / ReActNet 图工程 / EvoSOP / TriviumDB / EVOSTER 在线修复）：分支 `feat/harness-v2`，初赛前只加法、不动 Dockerfile 与 toolchain，含 TriviumDB 阻塞分析 |
| [open-questions.md](open-questions.md) | 需要向主办方确认的问题清单（A1–A20），按是否阻塞排序；内部悬置决策（B1–B14）；决策截止线 |

## 参考实现（`../refs/`，只读，**严禁进镜像**）

2026-10-10 clone 了 14 个仓库（约 500MB）用于读源码借鉴架构。完整清单与逐个的取舍结论见 [architecture.md](architecture.md) §10.1，其中价值最高的三个：

| 仓库 | 为什么值得读 |
|---|---|
| `BreachWeave/` | **第二届第一名**，同主办方。其 `docs/` 里带着**上届官方 API 与 MCP 接入文档原件** —— 这是本届多个文档缺口的唯一交叉验证来源 |
| `Tsec-Hackathon/` | **主办方官方资源仓**：两届获奖团队答辩 PPT + 前二十团队开源仓库导航索引（本项目一半的参考仓库是从这里定位到准确 URL 的） |
| `ARTEMIS/` | 内层执行体的首要参考。`supervisor/submissions/ctf.py` + `configs/tests/ctf_easy.yaml` 是现成的 CTF flag 提交抽象设计 |

> **纪律**：`refs/` 只借架构，不搬代码、不进镜像、不把上届题解喂进 prompt。构建期泄漏检查会验证这一点（[image-spec.md](image-spec.md) §6）。



