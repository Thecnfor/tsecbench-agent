# TSec Benchmark 自动跑分 Agent

面向腾讯 TSec Benchmark 平台的自动化渗透跑分系统：一个只带**异步 shell 工具**的 LLM agent，
运行在 **Docker（Kali Linux）** 容器内，自动完成「VPN 接入 → 拉题 → 渗透解题 → 提交 flag → 释放资源」全流程。

## 平台与运行模式

- **平台**：TSec Benchmark（`BENCHMARK_BASE_URL`，见下），所有接口携带 `BENCHMARK_TOKEN` 请求头认证。
- **接入时限（硬约束）**：平台要求每个 bench 在 **10 分钟内接入**。因此：
  - 每道题 `start` 拿到 `container_addr` 后必须**立即**开始访问渗透，不允许空等；
  - 题目容器是稀缺资源（同时最多 3 个 active），启动后尽快出结果、尽快 close；
  - 任务整体有总时限（超时后所有接口返回 `409 invalid_state`），agent 按「由易到难、先拿能拿的分」推进。
- **两种跑分模式**：
  1. **官方本地跑分（当前）**：使用官方提供的 API 与流程，agent 只负责解题与提交；
  2. **内部本地跑分（规划中）**：后续开发会考虑自建内部跑分服务，与正式比赛隔离。
  - 这里的「**本地**」只是与**正式比赛**相区别的概念（独立的题目集/环境/计分），
    **实际上两种模式都跑在 Docker 里**，代码结构上不区分——差异只体现在
    `BENCHMARK_BASE_URL` / `BENCHMARK_TOKEN` 两个环境变量与平台侧题集。

### 运行模式：本地 local vs 托管 hosted（`AGENT_MODE` 自动检测开关）

| | **本地模式 local** | **托管模式 hosted** |
|---|---|---|
| 用途 | 日常开发调试 | 最终交付 / 官方平台跑分 |
| 模型入口 | 官方 DeepSeek API（公网） | 平台内网转写网关（VPN 内） |
| 公网访问 | ✅ 可用 | ❌ 不可用，仅平台内网 |
| 交付形态 | 源码 + `.venv` 直接跑 | **≤3GB 镜像**上传官方平台 |
| 模型 | `deepseek-flash`（`AGENT_MODEL_LOCAL`） | `deepseek-v4.1-flash-expires-on-0910`（`AGENT_MODEL_HOSTED`） |
| 网关 | `https://api.deepseek.com/v1` | `http://api.deepseek.com.tsecbench.gw/v1` |
| VPN 预检 | 冒烟仅警告（靶场渗透仍需连） | **强制**，不过即退出 |

- **检测逻辑**（默认 `AGENT_MODE=auto`）：探测 local 网关 `/models` 可达 → local；
  否则探测 hosted 网关 → hosted；都不通回退 local 并由冒烟测试报错。
- **覆盖**：环境变量 `AGENT_MODE=local|hosted`，或命令行 `agent.py --mode hosted`。
- **托管模式转写网关（官方机制）**：托管环境中大模型 API 一律走转写网关，
  地址转换规则 `https://HOST[/path]` → `http://HOST.tsecbench.gw[/path]`（`config.py: to_gateway_url` 自动转换）。
  官方白名单供应商（19 家，均可按此规则转换）：
  `api.hunyuan.cloud.tencent.com`、`api.lkeap.cloud.tencent.com`、`tokenhub.tencentmaas.com`、
  `api.deepseek.com`、`dashscope.aliyuncs.com/compatible-mode`、`*.maas.aliyuncs.com`、
  `qianfan.baidubce.com`、`ark.cn-beijing.volces.com`、`open.bigmodel.cn`、`api.moonshot.cn`、
  `api.siliconflow.cn`、`spark-api-open.xf-yun.com`、`api.minimaxi.com`、`api.stepfun.com`、
  `api.lingyiwanwu.com`、`api.baichuan-ai.com`、`api.xiaomimimo.com`、`api.kimi.com`、`agent-awd.baidu.com`。
  **换供应商**：只改 `.env` 的 `DEEPSEEK_BASE_URL`（官方地址）+ `AGENT_MODEL`，hosted 地址自动转换。
- **出站防火墙（docker 层面限网）**：`NET_RESTRICT`（entrypoint，默认 auto：
  探测公网 AI API 可达=local 不限网，不可达=hosted 启用）。启用后容器 netns 内 iptables
  出站白名单：`lo` 全通、`tun0`（VPN 隧道，覆盖全部转写网关与靶场）全通、VPN 服务器端口
  （保重连）、平台 API 域名 IP；**其余出站 DROP**（IPv6 一并禁）。只作用于容器网络命名空间。
- **凭证**：全部写在项目根 `.env`（`.gitignore` 排除、不入库）。
  优先级：真实环境变量 > `.env` > 内置默认。
- **变量注入分工**：
  - **hosted 托管模式**：`BENCHMARK_TOKEN` / `BENCHMARK_BASE_URL` 由**官方平台自动注入容器**，本地不配置、不传；
  - **local 本地模式**：`run.sh --env-file .env` 把项目根 `.env` 注入容器（token 只在 `.env` 维护一处）。
- 两模式代码零差异，只切网关/模型/key——托管模式下系统不能访问公网，
  模型调用走平台内网转写网关；本地模式直接走官方 API。

## 架构

```
┌─ docker run --cap-add NET_ADMIN --device /dev/net/tun ───────────┐
│  Kali 容器 (tsec-agent:latest)                                    │
│                                                                   │
│  entrypoint.sh                                                    │
│    1. 容器内启动 openvpn（连 task_*_vpn_config.ovpn）              │
│    2. VPN 联通预检 http://10.0.100.58 == status:"ok"（失败退出）   │
│    3. 从 openvpn push 提取内网 DNS → 解析模型网关域名              │
│    4. agent.py --smoke 冒烟 → 通过后正式运行 agent.py              │
│                                                                   │
│  agent.py（主循环）                                               │
│    model: deepseek-v4.1-flash-expires-on-0910                     │
│    gateway: http://api.deepseek.com.tsecbench.gw/v1（OpenAI 兼容）│
│    tools: 仅 shell（异步 background），无 MCP、无 subagent         │
│      └─ run: 后台启动返回 job_id（1.5s 快速窗口，可带 wait_sec）     │
│         wait / wait_any / status / kill / list：边沿等待+分页读        │
│         （offset/lines），输出可 out_file 落盘，1800s 硬上限才 kill   │
│                                                                   │
│  预装工具：nmap masscan whatweb sqlmap gobuster ffuf nikto dirb   │
│            hydra netcat dig tcpdump python3(含 requests) ...      │
│  挂载：/agent（代码） /config/vpn.ovpn /work（日志）              │
└───────────────────────────────────────────────────────────────────┘
   VPN 隧道只存在于容器网络命名空间 → 宿主机网络零影响
```

- **VPN 只在容器内**：宿主机不需要 openvpn、不需要 root 改路由；断线/重连都不影响宿主机。
- **网络隔离**：容器 `NET_ADMIN + tun` 仅作用于自身 netns；宿主机路由表、DNS 均不被修改。
- **工具参数容错**：模型偶发把 shell 工具参数序列化坏（`action` 混入 XML 参数片段或整段命令、
  JSON 值漏引号）→ `shell_tool.parse_tool_arguments` 直接修复后照常执行，结果带
  `[参数已自动修复：…]` 前缀、原样参数记在 `events.jsonl` 的 `tool_arg_repaired`
  里——不再整轮作废（上一轮 c-03 因此白扔 14 轮）。

### 分段解题（每题独立上下文 + 暂停/续跑）

- 每题每次 **`AGENT_TASK_MAX_TURNS`（默认 60）轮为一段**：到点**不判死**，而是
  **关闭题目释放名额**（`task_close{outcome:"paused"}`）→ 上下文原样保留（`resume_meta`，
  可从 `conversation.jsonl` 重建）→ 排到队尾，**等没有新题可派时再 `start` 续跑**。
- 续跑会**重启靶机**（拿到新 target），并在上下文末尾追加一条续跑通知：
  告知「靶机已重启、靶机内状态重置、workdir 文件仍在、已提交的 flag 不丢、从上次进度继续」。
- **只有三种情况才终止本题**：① 上下文超出模型窗口（`context_length_exceeded`，
  立即停、不重试）；② 连续 `AGENT_GIVEUP_MAX` 段「没通关就输出总结」；③ 真失败
  （LLM 致命错/VPN 超时/时限）累计 `AGENT_MAX_FAILURES` 次。
- 除这三种情况外**题目一直续跑到通关或平台终态**；暂停/续跑不消耗失败次数。

## 快速开始

```bash
cd /home/xsf/Projects/tcbench/docker
./run.sh                              # 自动选最新 task_*_vpn_config.ovpn，构建+启动
./run.sh /path/to/vpn.ovpn            # 指定 vpn 配置
newgrp docker -c "docker logs -f tsec-agent"   # 观察全流程
```

启动后 entrypoint 自动依次完成：VPN 连接 → 联通预检 → DNS 修复 → 冒烟测试 → 正式跑分。
任一环节失败容器即退出（exit code 见下表），不会带病跑分。

### 手动调试

```bash
cd docker
newgrp docker -c "docker build -t tsec-agent:latest ."

# 只跑冒烟（验证 VPN / 网关 / shell 工具）
newgrp docker -c "docker run --rm --cap-add NET_ADMIN --device /dev/net/tun \
  -v \$PWD/../tsec_agent:/agent \
  -v \$PWD/../task_*_vpn_config.ovpn:/config/vpn.ovpn:ro \
  -v \$PWD/work:/work \
  tsec-agent:latest python3 /opt/agent-venv/bin/python3 /agent/agent.py --smoke"
```

### 宿主机一次性准备（已完成）

- 用户加入 `docker` 组：`usermod -aG docker xsf`
- `/etc/docker/daemon.json` 配置国内 registry-mirrors（Docker Hub 直连超时的绕行）
- Kali 基础镜像内的 `mirrors.neusoft.edu.cn` 源已切换为 `http.kali.org`（见 Dockerfile）

## 环境变量与 `.env`

> **限网原则**：托管/正式环境只允许 AI API + 靶场 VPN 出站，公网全 ban。
> 工具与字典全部**按领域预装进镜像**（Web/内网/爆破/逆向/取证/隧道/数据库 + exploitdb/seclists/rockyou），
> 不放任何题目特化资源（题目每次都变）；详见 `docker/Dockerfile` 的领域分组注释。

凭证统一放项目根 `.env`（`.gitignore` 已排除，**不入库**）。优先级：真实环境变量 > `.env` > 内置默认。

| 变量 | 说明 | 默认值 / `.env` |
| --- | --- | --- |
| `AGENT_MODE` | 运行模式 `auto`/`local`/`hosted`（auto=自动检测） | `auto` |
| `BENCHMARK_TOKEN` | 平台认证 token（每个 bench 任务一个） | `.env` 中维护 |
| `BENCHMARK_BASE_URL` | 平台 API 地址 | `https://tsecbench.zc.tencent.com` |
| `DEEPSEEK_BASE_URL` | **本地模式**模型网关（OpenAI 兼容） | `https://api.deepseek.com/v1` |
| `DEEPSEEK_API_KEY` | 本地模式官方 DeepSeek key | `.env` 中维护 |
| `AGENT_MODEL_LOCAL` | 本地模式模型 | `deepseek-flash` |
| `DEEPSEEK_BASE_URL_HOSTED` | **托管模式**内网转写网关 | `http://api.deepseek.com.tsecbench.gw/v1` |
| `DEEPSEEK_API_KEY_HOSTED` | 托管模式网关鉴权 | `tsec-bench` |
| `AGENT_MODEL_HOSTED` | 托管模式模型 | `deepseek-v4.1-flash-expires-on-0910` |
| `AGENT_PROBE_TIMEOUT` | auto 模式探测超时（秒） | `8` |
| `VPN_GUARD` | 运行时 VPN 守卫 `auto`/`1`/`0`（auto=hosted 开、local 关） | `auto` |
| `VPN_PROBE_INTERVAL` | VPN 探测间隔秒 | `20` |
| `AGENT_VPN_WAIT_MAX` | VPN 挂起时 worker 最长等待秒（超时弃题） | `600` |
| `AGENT_MAX_TURNS` | 最大 LLM 轮次 | `400` |
| `AGENT_TASK_MAX_TURNS` | 每段轮次预算（到点暂停、保留上下文，见「分段解题」） | `60` |
| `AGENT_TASKS` | 只解部分题目（测试用）：逗号/空白分隔，支持 `*`/`?` 通配，空=全部；`--task`/`AGENT_TASK_FILTER` 优先 | 空 |
| `AGENT_PAUSE_MAX` | 每题最多暂停续跑次数（`0`=不限，一直续跑直到通关） | `0` |
| `AGENT_GIVEUP_MAX` | 连续「未通关就输出总结」的段数上限（`0`=不限；达上限判 fail） | `3` |
| `AGENT_MAX_FAILURES` | 真失败（LLM 致命错/上下文超限/VPN 放弃）放弃上限（≥1） | `2` |
| `AGENT_DEADLINE_SEC` | 任务总时限秒（0=不限） | `0` |
| `AGENT_SHELL_WAIT_MS` | shell 快速同步窗口 | `1500` |
| `AGENT_SHELL_HARD_SEC` | 后台命令硬上限秒（到点整组 kill，输出已落盘；0=永不杀） | `1800` |
| `AGENT_SHELL_MAX_JOBS` | 同时在跑的后台 job 上限（满了拒绝新 `run`） | `8` |
| `VPN_CONF` | 容器内 vpn 配置路径 | `/config/vpn.ovpn` |
| `VPN_WAIT_SEC` | VPN 预检最长等待（hosted 强制） | `90` |

> 切换「内部本地跑分」时只需改 `BENCHMARK_BASE_URL` / `BENCHMARK_TOKEN`，代码零改动。
> 手动指定模式：`agent.py --mode local|hosted` 或 `AGENT_MODE=hosted`。

## bench 记录持久化（records.py，替代旧 trace 双文件）

```
docker/runs/<TOKEN>/                  # 唯一键 = token（无时间戳；同 token 重启则 append）
├── meta.json                         # mode/model、runs[]（每次运行 started_at/finished_at/结果）
├── events.jsonl                      # run 级事件：llm_retry、platform_poll_error、final_summary…
├── tasks.jsonl                       # 题目生命周期：task_start{started_at,target,attempt,resume}
│                                      / task_close{closed_at,duration_sec,outcome,flags}
│                                      # outcome ∈ pass | fail | paused（paused=本段到点，稍后续跑）
├── openvpn.log
└── tasks/<code>/
     ├── conversation.jsonl           # 完整 LLM 对话，**全量不裁剪**：
     │    system/user 任务卡、assistant{thinking,output,tool_calls}、tool{result}
     │    每条含 ts/seq/turn/tool_call_id + **成对起止时间戳（供甘特图）**：
     │    started_at/ended_at（epoch 秒·毫秒精度）+ duration_ms（自动算）——
     │    assistant=本次 LLM 调用起止、tool=工具执行起止、user/note=写入同刻；
     │    **同时是分段解题续跑时重建上下文的唯一来源**（`records.rebuild_messages`）
     │    **通关后同上下文追加复盘总结**
     │    （思路/工具困难/优化建议，assistant 条目带 kind_note:"closing_summary"）
     └── <AI 解题产物>
```

- **隔离**：`run.sh` 只挂 `runs/<TOKEN>:/work` → 容器内只有当前 bench；
  其他 bench 不在挂载内（bench 内可读、bench 外不可读），不做 shell 拦截。
- **归档**：`docker/archive_runs.sh [TOKEN...]` 把记录打包到 docker 目录外
  （默认 `~/tsec_bench_runs/<TOKEN>_<ts>.tar.gz`）并删除源目录；归档包数量/存留自行管理。
  运行中的 bench 会拒绝归档。
- **反作弊**：任务不跨 bench 共享（题目/代号每轮不同）；`.gitignore` 排除 `docker/runs/`。
- 查看对话：`jq . docker/runs/<TOKEN>/tasks/e1-01/conversation.jsonl`

### 本地观察面板（panel.py）

```bash
python3 tsec_agent/panel.py                 # 自动发现最新 runs/<TOKEN>，5s 刷新
python3 tsec_agent/panel.py --dir docker/runs/<TOKEN> --interval 3
python3 tsec_agent/panel.py --once          # 只渲染一帧（脚本/测试）
```

单屏 ANSI 面板展示：
- 全局：平台通关数/flag 数/总分（平台 API 不可达时**离线回退**本地记录统计）
- 每题：`▶ RUNNING 已跑时长 / attempt / target`、`✔ PASS(flags/耗时)`、`✘ FAIL`
- 进行中题目实时视角：**进行中的 shell 调用**（assistant 已发 tool_call 但未回结果 = 在跑，
  如 `wait job_id=xx timeout_sec=30`、`run: sqlmap ...`）、最近一次模型输出/思考
- 最近事件：llm_retry、parallel_cap、task_error 等

数据源全部**只读**持久化记录 + 平台 API（token=目录名），不碰 agent 运行时；无第三方依赖。

## 平台流程（agent 提示词内置）

1. `GET /openapi/v1/challenges` — 拉题列表与进度（优先 `is_completed=false`，由易到难）
2. `POST .../challenges/start?unique_code=` — 启动容器拿 `container_addr`（**10 分钟内接入**，active 上限 3）
3. 渗透解题拿 flag（`flag_count>1` 需逐个拿）
4. `GET .../challenges/hint` — 可选，查看后该题得分按比例扣减
5. `POST .../challenges/submit` — 提交 flag（`409 duplicate` 幂等跳过）
6. `POST .../challenges/close` — 通关/放弃后**必须**释放名额
7. 循环直到全部通关或任务超时

错误码处置、分题型渗透方法、输出约定全部在 `tsec_agent/prompt.py` 的系统提示词里。
**平台返回 `invalid_state + already finished`（评测截止）= 终态**：agent 立即停掉所有
worker、清理其容器并正常退出（`run_finish: platform_ended`），绝不重试。

## 退出码（entrypoint）

| code | 含义 |
| --- | --- |
| 0 | 正常完成 |
| 10 | openvpn 启动失败 |
| 11 | VPN 联通预检失败（VPN检测未通过,请检查靶场VPN网络配置） |
| 12 | 冒烟测试失败（模型网关 / shell / 预检） |

agent.py 自身：`0` 正常收尾 / `2` 模型网关不可达 / `3` 达到最大轮次。

## 目录结构

> 镜像体积硬约束：**≤ 3GB**（当前 `tsec-agent:latest` 约 1.21GB）。
> 构建后用 `newgrp docker -c "docker images tsec-agent"` 验证；
> 不要引入 seclists 全量字典、metasploit 等大体积组件。

```
tcbench/
├── README.md                      # 本文档
├── AGENTS.md                      # AI 协作者须知（架构不变量、改代码前必读）
├── task_<id>_vpn_config.ovpn      # 每个 bench 任务下发的 VPN 配置（以最新为准）
├── tsec_agent/                    # agent 代码（挂载进容器）
│   ├── agent.py                   # 主循环：LLM 调用 → 工具分发 → 结果回填
│   ├── shell_tool.py              # 异步 shell（job 管理、截断、超时、进程组 kill）
│   ├── prompt.py                  # 系统提示词（跑分流程/错误处理/渗透指引）
│   ├── config.py                  # 配置（全部环境变量可覆盖）
│   ├── selftest.py                # 离线端到端自测（fake 模型驱动工具循环）
│   └── README.md                  # 快速使用说明
├── docker/
│   ├── Dockerfile                 # Kali + 渗透工具 + openvpn + agent venv
│   ├── entrypoint.sh              # VPN → 预检 → DNS → 冒烟 → 跑分
│   ├── run.sh                     # 一键启动
│   └── work/                      # openvpn.log、agent.log 等运行产物
└── .venv/                         # 宿主机开发用 python 环境（selftest）
```

## 测试

```bash
cd tsec_agent
../.venv/bin/python3 selftest.py        # 离线自测（不需要 VPN / 网关），必须 PASS
../.venv/bin/python3 agent.py --smoke   # 需要 VPN 已连 + 网关可达
```

## 故障排查

| 现象 | 排查 |
| --- | --- |
| 容器 exit 11 | `docker/work/openvpn.log` 看握手/路由；宿主机 `curl http://10.0.100.58` |
| 网关域名解析失败 | entrypoint 会从 openvpn push 提取 DNS；检查日志 `gateway DNS resolved` |
| `docker pull` 超时 | registry-mirrors（见上）；或手动 `docker pull docker.m.daocloud.io/<image>` 再 tag |
| 平台 404 task_not_found | `BENCHMARK_TOKEN` 失效/未注入 |
| 平台 409 invalid_state 持续 | 任务已超时结束 → 停止并报告；若是 max active → close 一题再 start |
| 重复提交 | `409 duplicate` 属正常幂等，跳过 |
