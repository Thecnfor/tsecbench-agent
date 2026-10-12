# AGENTS.md — AI 协作者须知

本文件是给 AI agent（以及新加入的人类开发者）的操作手册。改代码前必读。

## 这个项目是什么

腾讯 TSec Benchmark 平台的**自动跑分 agent**：LLM 驱动、只带一个**异步 shell 工具**，
跑在 **Docker（Kali）** 容器里，端到端完成「VPN 接入 → 拉题 → 渗透 → 提交 flag → close 释放」。

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
- **auto（默认）**：探测 local `/models` 可达 → local；否则探测 hosted → hosted；都失败回退 local 由冒烟报错。
  实现见 `config.py: detect_mode()`；命令行 `agent.py --mode local|hosted` 可覆盖。
- 凭证只放项目根 **`.env`**（`.gitignore` 排除，**严禁提交**）；优先级：真实环境变量 > `.env` > 内置默认。
- **注入分工**：hosted 托管模式下 `BENCHMARK_TOKEN`/`BENCHMARK_BASE_URL` 由**官方平台自动注入容器**
  （本地不配置、不传）；local 本地模式由 `run.sh --env-file .env` 注入——**不要在脚本里硬编码 token**。

## 架构不变量（不要轻易破坏）

1. **单工具设计**：agent 只有 `shell` 一个工具（异步 background 模式），**没有 MCP、没有 subagent**。
   这是评测方的设计要求，不要往里加 read/write/web 等工具。
2. **shell 是异步的**：`run` 立即返回 `job_id`，模型用 `wait/status/kill/list` 轮询。
   快速命令有 1.5s 同步窗口（`AGENT_SHELL_WAIT_MS`）。进程必须 `start_new_session`（独立进程组），
   kill 时整组杀，超时由 watcher 线程兜底。
3. **模型网关双模式**：local=官方 `https://api.deepseek.com/v1`（公网，key 在 `.env`），
   hosted=平台内网转写网关 `http://api.deepseek.com.tsecbench.gw/v1`（**依赖 VPN 内网 DNS，只在容器连上 VPN 后可解析**）。
   默认 `AGENT_MODE=auto` 自动探测切换，见架构不变量前的「运行模式」小节。
4. **VPN 只在容器内跑**：entrypoint 用 `openvpn` 在容器 netns 建隧道，
   需要 `--cap-add NET_ADMIN --device /dev/net/tun`。**永远不要在宿主机上连 VPN 或改宿主机路由/DNS**。
5. **预检是硬门槛**：`http://10.0.100.58` 返回 `status:"ok"` 才能进入跑分，
   否则 entrypoint exit 11、agent 直接报「VPN检测未通过,请检查靶场VPN网络配置」并停止。
   不允许用平台地址代替该预检。
6. **错误处置在提示词里**：`invalid_state`/`duplicate`/`task_not_found` 等平台错误码的处理规则
   写在 `prompt.py`，与平台规则一一对应，改动前对照 README「平台流程」章节。
7. **上下文按题隔离（方案 A）**：平台编排（选题/start/close/名额）由 `agent.py` 控制器 +
   `bench.py` 硬编码执行；模型只在**每题独立的 messages** 里解题（`solve_task`），
   题目做完上下文即弃。**不要改回单一大上下文**。失败题重试上限 2 次（attempts 字典）。

## 文件地图

| 路径 | 职责 | 什么时候改 |
| --- | --- | --- |
| `tsec_agent/agent.py` | 控制器主循环：选题 → start → `solve_task` 单题上下文 → 校验 → close；LLM 重试 | 加运行控制逻辑 |
| `tsec_agent/bench.py` | 平台 API 客户端（fetch/start/close/选题策略、名额释放） | 平台流程变化 |
| `tsec_agent/shell_tool.py` | 异步 shell：Job 生命周期、输出截断、进程组 kill | 调工具行为 |
| `tsec_agent/prompt.py` | 单题解题 system prompt + 任务卡模板（渗透方法、提交协议、结束约定） | 平台规则变化、提示词调优 |
| `tsec_agent/trace.py` | 追踪日志：思考/输出/工具/生命周期 → trace.jsonl + trace.log | 日志字段调整 |
| `tsec_agent/config.py` | 全部配置与默认值（环境变量可覆盖） | 加配置项 |
| `tsec_agent/selftest.py` | 离线端到端自测（fake 模型驱动工具循环） | 改动主循环后必须跑 |
| `docker/Dockerfile` | Kali + 渗透工具 + openvpn + agent venv | 加工具/依赖（**镜像 ≤3GB 硬约束**） |
| `docker/entrypoint.sh` | 容器入口：VPN → 预检 → DNS → 冒烟 → 跑分 | 接入流程变化 |
| `docker/run.sh` | 一键启动（选最新 vpn 配置、挂卷、环境变量） | 启动参数变化 |
| `README.md` | 面向人类的完整文档 | 任何对外可见的变化 |

## 常用操作

```bash
# 离线自测（改完代码先跑这个，不需要 VPN/网关）
cd tsec_agent && ../.venv/bin/python3 selftest.py

# 构建镜像
cd docker && newgrp docker -c "docker build -t tsec-agent:latest ."

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

- [ ] `selftest.py` 通过（离线，不依赖外部服务）
- [ ] `python3 -m py_compile` 通过
- [ ] `agent.py --smoke` 通过（本地模式不强求 VPN；hosted 模式必须全绿）
- [ ] 凭证只出现在 `.env`（已 gitignore），**没有硬编码进代码/文档/镜像层**
- [ ] 改了 Dockerfile/entrypoint → 重新 build + `--smoke` 验证
- [ ] **镜像体积 ≤3GB**：`docker images tsec-agent` 检查（当前 1.21GB；大体积包如 seclists 全量字典、metasploit 不要装）
- [ ] 改了平台相关逻辑 → 对照 README 平台流程与错误码
- [ ] 没有破坏上述架构不变量（单工具、容器 VPN、预检门槛、不硬编码平台差异）
- [ ] README/AGENTS.md 同步更新
