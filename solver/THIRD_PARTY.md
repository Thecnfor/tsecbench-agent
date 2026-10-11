# Third-party sources ledger

本仓库从 refs/ 抄用/改写的素材清单（refs/ 本身 gitignore，抄用内容进入仓库代码区）。
用户已授权抄用（含 AGPL / 无 LICENSE 项）；对外分发派生作品时注意 AGPL 义务。

| 来源（refs/） | 许可 | 抄用位置 | 方式 |
|---|---|---|---|
| CHYing-agent `chying_agent/prompts/`（orchestrator_strategy / executor / reverse 等） | MIT | [solver/gov/profiles.py](../solver/gov/profiles.py) | 改写（去多 agent delegation，转单执行模型策略包，AT attribution 常量） |
| BreachWeave `.claude/skills/pi-mono-framework/` 文档 | Apache-2.0 | [solver/pi_ext/recon.ts](../solver/pi_ext/recon.ts) | API 依据（registerTool / TypeBox / extensions 加载路径），未拷代码 |
| BoxPwnr `src/boxpwnr/prompts/generic_prompt.yaml`（INTEGRITY RULES） | AGPL-3.0 | main.py 既有契约提示词思路（反作弊条款） | 思路参照 |
| aide-for-pentest `resources/patterns/`（explore_plan_execute 等） | 无 LICENSE | [solver/gov/profiles.py](../solver/gov/profiles.py) 策略顺序 | 思路参照 |
| ARTEMIS `supervisor/`（triage / context_manager） | Apache-2.0 | solver/gov/（session/prior 的 service 划分） | 思路参照 |
| OpenHands `src/api/`（conversation / event-stream / agent-profiles services）、`docs/ACP_AGENTS.md` | MIT | solver/gov/ 治理薄版结构、dual_main 动态提示词注入位 | 架构参照 |

自创（无外部来源）：solver/kb/*.md cheatsheet、solver/recon_recipe.py、solver/gov/prior.py、solver/kb/index.py。
