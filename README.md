# TSecBench 自主渗透 Agent · 设计与调研文档

第三届腾讯云智能攻防挑战赛 · 主赛线上初赛（TSecBench 平台托管运行）参赛 Agent 的**设计与调研文档**。

**当前状态：设计已定稿，尚未开始实现。** 全部架构决策、调研证据与取舍理由都在 `docs/` 里。

→ 从这里开始读：**[docs/README.md](docs/README.md)**（赛事定位、运行约束、关键决策摘要、文档索引）

## 文档

| 文件 | 内容 |
|---|---|
| [docs/README.md](docs/README.md) | 入口：赛事定位、托管环境约束、关键决策摘要表、原始资料清单 |
| [docs/architecture.md](docs/architecture.md) | 架构主文档：五栈按执行位置分层（L0~L4）、性能模型、Solver Adapter 契约、组件与状态机、调度与预算、错误处理、双模运行、测试策略、决赛预留，以及两轮外部建议的逐条核查记录 |
| [docs/platform-api.md](docs/platform-api.md) | 答题 API 梳理与**陷阱清单**（含与上一届的规则差异对比） |
| [docs/image-spec.md](docs/image-spec.md) | 镜像规格：基座选择、工具来源策略、运行时版本钉死、体积预算与降级阶梯、构建规范、泄漏防线 |
| [docs/runbook.md](docs/runbook.md) | 运行手册：本地评测、托管交付、mock server 规格与测试矩阵 |
| [docs/open-questions.md](docs/open-questions.md) | 待向主办方确认的问题（A1–A19）、内部悬置决策（B1–B13）、决策截止线 |

## 本仓库刻意没有包含什么

这一点比仓库里有什么更重要：

| 排除项 | 原因 |
|---|---|
| `ing/`（`env` + `*.ovpn`） | 内含 `BENCHMARK_TOKEN` 实际值与 **OpenVPN 客户端私钥**。平台要求敏感配置一律以环境变量注入；文档中相关取值均已改为占位符 |
| `refs/`（14 个第三方参考仓库，约 467MB） | 见 [`refs.lock`](refs.lock)。**其中 `aide-for-pentest` / `Tsec-Hackathon` / `xbow-competition` 没有 LICENSE 文件，即默认保留所有权利，无权再分发**；`ARTEX-EN` / `BoxPwnr` / `Cairn` 为 AGPL-3.0（传染性）。`refs.lock` 记录了每个仓库的 URL + commit + 许可证 + 实际借用了什么，按它重新 clone 即可复现全部调研结论 |

## 合规声明

平台规则明确：托管运行期间产生的网络流量与大模型对话记录将被审计并部分公开；**Agent 不可内置针对题目的历史答题记忆或解题方法，违者按作弊处置**。

据此，本仓库的设计约束是：

- 镜像内不含任何凭据、私钥、`.ovpn` 文件，构建流程末尾有一道**逐层泄漏扫描**（覆盖所有镜像层，因为 `docker save` 会打包中间层里被删除的文件）；
- 镜像内不含任何题目答案、flag 字面量或针对性题解 —— **允许内置通用能力，不允许内置题解**；
- 上一届赛事素材（获奖答辩、题解、flag 模式）只用于**学习架构与工程方法**，不进入镜像、不喂进 prompt。
