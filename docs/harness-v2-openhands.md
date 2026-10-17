# Harness v2 —— OpenHands 图推理大脑 + Pi 一次性执行 + TriviumDB 攻击图

> 2026-10-16 立项，分支 `feat/harness-v2`。本文件是 v2 控制面重写的**行为契约**，
> 代码注释按 § 号引用本文；与本文冲突以代码为准。前作 `harness-v2.md`
> （L1–L5 五层加法升级）继续生效，v2 是在其上叠加重写控制面，不是替代。

## 0. 定调与决策记录

- **范围**：全力推 v2 重写（放弃初赛零风险铺垫路线）。初赛仍以
  `HARNESS_V2=off`（默认）跑现有单代理主路径；v2 并行开发，绿了再切。
- **选型决策**（2026-10-16 用户拍板）：
  1. OpenHands 1.11.0 **真跑** Agent 作为 planner（非自写轻控制面）；
  2. Pi 执行面以**每节点一次性子进程**形态被委派（复用 dual_main 切片 /
     killpg / stream.py 机制），无常驻服务；
  3. TriviumDB **唯一 Writer = OpenHands 所在的 Python 编排层**（桥接层代写），
     Pi 与复盘只读（与早期「Pi 写」方案相反，此处以本文件为准）；
  4. Python 3.13（OpenHands 硬约束，前作 cca32d0 已降级）。

## 1. 不变式（对全篇生效）

- BENCHMARK_TOKEN 绝不进 solver/planner 环境（architecture.md §6.5.1）；
- `refs/` 不进镜像不进库；镜像与 prompt 不含 flag 字面量与题目特化内容；
- 运行期零公网：LLM API 是唯一非靶端点；TriviumDB 二进制绑定必须
  vendored（image-spec.md §3 版本纪律）；
- `CONCURRENCY_LIMIT ≤ 3` 启动即校验；MAX_ATTEMPTS / finalize 收尾保护不动；
- `prior.json` 双图 JSON 通道契约零改动（architecture.md §2.5）——
  .tdb 是增强层，不是新耦合点。

## 2. §A 进程拓扑与控制权

```
orchestrator (Python, 顶层守护)
 ├─ v2_controller   确定性状态机，并发/熔断/预算的唯一切实点
 │   └─ OpenHands planner（openhands-ai 1.11.0，独立 venv）
 │        只输出 AgentDelegateAction / AgentFinishAction
 │        bash/execute 类 Action 在桥接层一律拒绝（fail-closed）
 │        .tdb 只读
 ├─ Pi 子进程（每节点一次 dual_main，复用切片/killpg/stream.py）
 │        .tdb 只读 + prior.json 写（契约不变）
 └─ 独立验证器（确定性，无 LLM）
```

- 桥接层 `orchestrator/v2_bridge.py`：捕获 `AgentDelegateAction` 入队状态机；
  捕获 `CmdRunAction` / `IPythonRunCellAction` 一律回
  `REJECTED: planner 不执行命令，请委派`（可配置宽限次数，超限熔断 planner）。
- 阶梯流式：Pi 流式半成品（前作 L1 stream.py）作为 Observation 回灌
  planner —— 不等节点跑完，planner 即可起下一个原点。

## 3. §B 攻击图状态机 + TriviumDB

- 节点状态枚举：`UNEXPLORED → EXPLORING → EXPLORED / DEAD_END`；
  非法转移 raise（EXPLORING 只能来自 UNEXPLORED；DEAD_END 终态不可逆）。
- 熔断：单节点 `NODE_TIMEOUT_S`（默认 900s，env 可覆盖）到点 killpg +
  强制 DEAD_END + 释放并发槽。
- TriviumDB：桥接层是唯一 Writer（planner 决策经桥接层
  `insert_with_id` + `link` 落 .tdb）；Pi / 复盘走 ReadOnly
  （`ReadOnlyError` 已 POC 验证，前作 §6.2）。
- Node napi 绑定（B15）不在 v2 主线，决赛前窗口再实证。

## 4. §C 独立验证闸门

- 模型/执行体**不得自宣成功**：Pi 声称 flag 后，验证器在 workspace 内
  确定性复核（正则扫 flag 形态 + `echo $RANDOM` shell 往返确认执行面存活），
  通过才进 submit 路径；不通过则节点标 DEAD_END，失败细节回灌 planner。
- 平台对账真值仍是 `rec.submitted` / `correct_flag_count`；闸门只是
  「声称 ≠ 通过」的确定性复核层。

## 5. §D SOP 渐进式披露

- 前作 L3 零 LLM SOP 库升级为两级注入：planner System Prompt 只给
  目录（名称 + 一行摘要）；OpenHands 只读工具 `get_sop(name)` 调用时
  才注入全文。脱敏 / flag 形态 fail-closed 闸门（前作 §4.2）原样复用。

## 6. §E 测试、开关与回滚

- 单测：状态机（3 并发同时 EXPLORING → 15min 到点全 DEAD_END 且槽位归零、
  非法转移 raise）；桥接层（CmdRun 必拒、委派必入队）；.tdb 单写多读；
  验证器（假 flag 拒绝、真形态通过）。
- e2e：stub planner（直接吐 AgentDelegateAction 的确定性假 planner）
  跑全链路，不依赖真 LLM key；真 OpenHands 冒烟单独验证（隐式网络 /
  config 目录行为首次实机跑）。
- 回滚：`HARNESS_V2` 特性开关（默认 off）；off = 现有单代理主路径原样；
  v2 代码全部旁路，各模块互相独立可单独下线。

## 7. 未决

- B15：TriviumDB Node 绑定实证（决赛前窗口）。
- B17：OpenHands 1.11.0 离线镜像行为实证（config 目录、隐式探活、内存占用）
  —— v2 冒烟的先决项，不绿不切真跑。

## 8. 状态（2026-10-17）

stub e2e 全绿（test_v2_*）；v2 模块默认旁路（HARNESS_V2=off），交付镜像
按已验证主路径构建。OpenHandsPlanner 真跑冒烟 = B17，未绿前
HARNESS_V2 不在正式镜像开 on。
