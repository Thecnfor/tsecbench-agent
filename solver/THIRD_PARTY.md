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

## 本地补丁（.pi-vendor 重装后需重打，2026-10-17）
pi-ai dist/api/openai-completions.js convertMessages() 尾部：新增"悬空 tool_calls 消毒"——
assistant 消息带 tool_calls 但下一条不是 tool 结果时（连接中断的残缺轮次），剥离该消息并
注入 user 提示 "[system] previous tool call was interrupted"。否则 deepseek 400
"must be followed by tool messages" 死循环烧掉整个时间片（实测 3/12 片）。
补丁位置 2（同日）：pi-coding-agent dist/bundle/chunks/openai-completions-WWQW7S3V.js 的
convertMessages return 前同款消毒（bundle 内联版才是实际生效路径；pi-ai 包版本是死代码路径）。
补丁 v3（2026-10-17 06:2x）：消毒逻辑升级为"合成缺失 tool 结果"——逐 call_id 校验，
缺失则注入 {role:"tool",tool_call_id,content:"[system] tool call was interrupted..."}。
仅剥消息不够：deepseek 对多 tool_calls 部分覆盖同样 400（c-01 实测）。两处文件均已打。
补丁 v4（07:2x）：消毒升级为共享 __tsecSanitize——向前扫描到下一个 assistant/user 收集
已答 call_id，两侧 id 做 "|" 前缀归一化（OpenAI Responses 格式），缺失即合成 tool 结果。
（v3 仅查紧邻，d-04 实测仍 400 的变体根因。）

## 知识库语料（solver/kb/unified.db，2026-10-17）
由 `security-knowledge-base-2026-10-14.tar.gz`（hacktricks-zh、ctf-wiki、
payloadsallthethings、nuclei-templates、awesome-poc 等）与 `Skills.7z` 抽取生成，
构建脚本 `solver/kb/build_wiki_db.py`。仅收 .md/.txt；AppleDouble、>256KB 文件、
隐藏目录跳过；flag 形态内容 fail-closed 整节丢弃。
来源许可：hacktricks (AGPL-3.0)、ctf-wiki (CC BY-SA)、payloadsallthethings (MIT)、
nuclei-templates (MIT)、awesome-poc (MIT)、Skills 包内部各自声明。本项目仅作
离线检索用途，不分发修改版；如评审要求可整体移除（删除 unified.db 即回退到
自研 9 篇 cheatsheet）。
