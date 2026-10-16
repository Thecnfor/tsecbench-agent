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
| 交付形态 | 源码 + `pnpm build` 直接跑 | **≤3GB 镜像**上传官方平台 |
| 模型 | `deepseek-flash`（`AGENT_MODEL_LOCAL`） | `deepseek-flash`（`AGENT_MODEL_HOSTED`，与本地同名，无额外后缀） |
| 网关 | `https://api.deepseek.com/v1` | `http://api.deepseek.com.tsecbench.gw/v1` |
| VPN 预检 | 冒烟仅警告（靶场渗透仍需连） | **强制**，不过即退出 |

- **检测逻辑**（默认 `AGENT_MODE=auto`），三步按序：
  1. **结构信号（零网络、0.0s）**：平台注入的 `BENCHMARK_BASE_URL` host 以 `.tsecbench.gw`
     结尾（= 托管注入的转写形态，实测值 `http://tsecbench.zc.tencent.com.tsecbench.gw`）
     → 直接判 hosted；本地 `.env` 永远是原始 `https://` 形态，不会误触发；
  2. 探测 local 网关 `/models` 可达（需有本地 key）→ local；否则探测 hosted 网关
     （两次机会，容忍启动 DNS 抖动）→ hosted；
  3. 都不通时：**无本地 key → hosted**（托管镜像无 `.env`，local 必然跑不了），
     有本地 key → local 由冒烟报错。
- **entrypoint 兜底 hint**：`NET_RESTRICT=auto` 判定公网 AI API 不可达时（= hosted 环境），
  未显式指定 `AGENT_MODE` 就直接导出 `AGENT_MODE=hosted`——防止在平台侧设了
  `DEEPSEEK_API_KEY` 的情况下探测抖动误选 local（实测 exit 12）。
  显式 `AGENT_MODE=local|hosted` 与命令行 `agent.js --mode` 始终优先。
- **覆盖**：环境变量 `AGENT_MODE=local|hosted`，或命令行 `agent.js --mode hosted`。
- **托管模式转写网关（官方机制）**：托管环境中大模型 API 一律走转写网关，
  地址转换规则 `https://HOST[/path]` → `http://HOST.tsecbench.gw[/path]`（`src/config.ts: toGatewayUrl` 自动转换）。
  转写严格按官方规则收紧（selftest 有断言）：**只转写 `https://`**（显式给的 `http://`
  内网/转写地址原样保留）、**IP 字面量不拼后缀**（`10.0.0.5.tsecbench.gw` 解析不了）、
  已是 `.tsecbench.gw` 的幂等保留；显式 `DEEPSEEK_BASE_URL_HOSTED` 也过同一转换
  （https 形式自动转写，http/IP 原样）。转写地址同时用于**冒烟与正式调用**（hosted 实测
  `model gateway OK` 走的就是转写形态）。
- **托管网关鉴权（实测 2026-10-15）**：本届转写网关把 `Authorization` **透传给上游 DeepSeek**
  ——401 报文是 DeepSeek 原生格式（`Your api key: ****ench is invalid`）。即托管模式必须使用
  **真实 DeepSeek key**（平台环境变量 `DEEPSEEK_API_KEY` 注入；镜像内无 `.env`），
  上届约定的内置 `tsec-bench` 会被上游 401。
- **key 候选发现（`src/llm.ts: discoverGatewayKey`）**：冒烟（hosted）与运行中 401 都走同一套
  **候选清单逐个探测**，每个候选一行**掩码**结果日志（只显示尾 4 位，绝不输出完整 key，
  `BENCHMARK_TOKEN` 同样掩码）+ `events.jsonl: gateway_key_probe`。候选按优先级：
  1. `anonymous` —— **匿名（不带 Authorization 头）**：fetch client 直接省略该头
     （wire 级等价于"无凭据请求"）；
     部分部署网关不透传鉴权、自带平台配额，匿名即可用（实测命中即选中，省自己的 key 额度）；
  2. `env:DEEPSEEK_API_KEY_HOSTED` —— 显式配置的托管 key；
  3. `env:BENCHMARK_TOKEN` —— 平台注入凭据，个别部署网关也认；
  4. `env:DEEPSEEK_API_KEY` —— **真实 key（最后试：用它=烧自己的额度）**；
  5. `current-config` —— 当前已配置 key（如内置 tsec-bench）兜底。
  冒烟用 `probe_all=True` 把所有候选探一遍（完整诊断报告）但按优先级选第一个可用；
  401 热切换停在第一个命中（省请求），且**只热切换一次**防死循环。
  实测三形态：passthrough 网关逐候选 `rejected(401)` 直到真实 key `OK`；
  匿名可用网关 `anonymous -> OK` 直接选中；全拒时 `ALL N candidates failed` 明确报错。
  官方白名单供应商（19 家，均可按此规则转换）：
  `api.hunyuan.cloud.tencent.com`、`api.lkeap.cloud.tencent.com`、`tokenhub.tencentmaas.com`、
  `api.deepseek.com`、`dashscope.aliyuncs.com/compatible-mode`、`*.maas.aliyuncs.com`、
  `qianfan.baidubce.com`、`ark.cn-beijing.volces.com`、`open.bigmodel.cn`、`api.moonshot.cn`、
  `api.siliconflow.cn`、`spark-api-open.xf-yun.com`、`api.minimaxi.com`、`api.stepfun.com`、
  `api.lingyiwanwu.com`、`api.baichuan-ai.com`、`api.xiaomimimo.com`、`api.kimi.com`、`agent-awd.baidu.com`。
  **换供应商**：只改 `.env` 的 `DEEPSEEK_BASE_URL`（官方地址）+ `AGENT_MODEL`，hosted 地址自动转换。
- **出站防火墙（docker 层面限网）**：`NET_RESTRICT`（entrypoint，默认 auto：
  探测公网 AI API 可达=local 不限网，不可达=hosted 启用）。启用后容器 netns 内 iptables
  出站白名单：`lo` 全通、`tun0`（VPN 隧道，覆盖全部转写网关与靶场）全通、**DNS
  （resolv.conf 里的 nameserver，UDP/TCP 53——锁死它等于全容器解析瘫痪，实测教训）**、
  VPN 服务器端口（保重连）、平台 API 域名 IP、**模型转写网关域名 IP**；**其余出站 DROP**
  （IPv6 一并禁）。只作用于容器网络命名空间。**两层失败安全（fail-open）**：
  ① 平台 API 或模型网关域名解析不出 IP 时**不锁网**并打 `FATAL-SAFE`——白名单缺了关键
  域名还 DROP 全部出站，等于自己造出"容器起得来但题库/模型全不通"；
  ② **平台不授予 NET_ADMIN**（实测：iptables 报 `Permission denied`）时容器内根本锁不了网
  → 打一条明确 WARNING 跳过，**绝不打"egress restricted"假成功日志**，限网交由平台网络层。
- **凭证**：全部写在项目根 `.env`（`.gitignore` 排除、不入库）。
  优先级：真实环境变量 > `.env` > 内置默认。
- **变量注入分工**：
  - **hosted 托管模式**：`BENCHMARK_TOKEN` / `BENCHMARK_BASE_URL` 由**官方平台自动注入容器**，本地不配置、不传；
    容器无 run.sh、无卷挂载——entrypoint 自建 `/work`，镜像内已含 agent 代码与知识库；
    若平台经 `VPN_CONFIG` 环境变量下发完整 `.ovpn` 内容，entrypoint 会落盘为 0600 临时文件并照常拨 VPN。
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
│    4. node /agent/dist/agent.js --smoke 冒烟 → 通过后正式运行 agent                  │
│                                                                   │
│  agent.js（主循环，Node/TS 实现）                                  │
│    model: deepseek-flash                                             │
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
- **多模态读图**：`{"action":"view","path":"..."}` 把图片本体作为工具结果返回给模型
  （截图/验证码/隐写图直接看懂；文件头解析尺寸、超限自动压缩、网关拒图自动降级为
  元数据文本，见 `AGENT_VISION`）。
- **工具参数容错**：模型偶发把 shell 工具参数序列化坏（`action` 混入 XML 参数片段或整段命令、
  JSON 值漏引号）→ `shell_tool.parseToolArguments` 直接修复后照常执行，结果带
  `[参数已自动修复：…]` 前缀、原样参数记在 `events.jsonl` 的 `tool_arg_repaired`
  里——不再整轮作废（上一轮 c-03 因此白扔 14 轮）。

### 分段解题（每题独立上下文 + 暂停/续跑 + A/C/D 调度）

- **段预算以墙钟为主**（`AGENT_TASK_SEGMENT_SHORT_SEC`=600s / `LONG_SEC`=1800s，按题目
  字段分类取值）：到点**不判死**，而是**关闭题目释放名额**（`task_close{outcome:"paused"}`）
  → 上下文原样保留（`resumeMeta`，可从 `conversation.jsonl` 重建）→ 之后 `start` 续跑。
  轮数 `AGENT_TASK_MAX_TURNS`（默认 120，0=关）只是安全网——实测段墙钟 p50=4.7min、
  p90=28.9min、max=158min，同样 60 轮墙钟差 30 倍（耗时由工具等待主导），轮数当主闸不合适。
- **通关即关容器（A）**：每轮开头查 watcher 快照缓存（20s 刷新，零额外 API），平台确认
  满 flag 立刻收束 → **立即关容器释放槽位**（不等模型自己收尾——实测通关后白挂 22 分钟）
  → 复盘总结脱槽跑（不占并发名额），限时 `AGENT_SUMMARY_TIMEOUT_SEC`（默认 300s），
  超时丢弃总结照常 pass。
- **段到点处置**：段末零新 flag 的短题**动态改判长题**（治字段失真）；有短题在等时长题配额
  = `cap-1`（保底 1 槽给短题）；**配额内长题不轮转**（bench：轮走后只会被另一道长题补回
  同槽，短题一无所获，纯重启浪费——打断 88→31.6 完成持平）；超配额长题必须让位；
  短题有等待者时照旧关题轮转。**无人等名额时不关题直接续一段**（`segment_continue`，
  短题适用，省掉靶机重置代价）。
- **调度排序（D）**：长题早启动（开局就顶上去跑，估算降序）、短题最短优先、暂停题 aging
  （等 `AGENT_RESUME_AGING_SEC`=600s 提权，防 fresh 无限插队——实测曾有题暂停后等 118 分钟）。
  分类只用 API 字段（flag_count/difficulty/level），**不依赖历史基线**（正式比赛题目必变）。
  策略由 `sim/` 离散事件模拟器 bench 验证（多场景×多策略×百种子，含防饿死性质测试）。
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
冒烟覆盖：模型网关连通、shell 工具、VPN 预检（hosted 强制）、**平台题库可达**
（hosted 下 `BENCHMARK_TOKEN` 缺失或平台 API 连不上都算失败；拿到业务错误说明网络可达，放行）。

### 手动调试

```bash
cd docker
# 构建上下文 = 项目根（凭证/运行记录由根 .dockerignore 排除；知识库与 entrypoint 进镜像）
newgrp docker -c "docker build -f Dockerfile -t tsec-agent:latest .."

# 只跑冒烟（验证 VPN / 网关 / shell 工具）
newgrp docker -c "docker run --rm --cap-add NET_ADMIN --device /dev/net/tun \
  -v \$PWD/../tsec_agent:/agent \
  -v \$PWD/../task_*_vpn_config.ovpn:/config/vpn.ovpn:ro \
  -v \$PWD/work:/work \
  tsec-agent:latest node /agent/dist/agent.js --smoke"
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
| `DEEPSEEK_API_KEY_HOSTED` | **托管模式**网关鉴权（可不设） | 回落链：显式值 > `DEEPSEEK_API_KEY`（**真实 key，推荐**）> `tsec-bench`（上届约定，本届会被 401） |
| `AGENT_MODEL_HOSTED` | 托管模式模型 | `deepseek-flash`（与本地同名，无额外后缀） |
| `AGENT_PROBE_TIMEOUT` | auto 模式探测超时（秒） | `8` |
| `VPN_GUARD` | 运行时 VPN 守卫 `auto`/`1`/`0`（auto=hosted 开、local 关） | `auto` |
| `VPN_PROBE_INTERVAL` | VPN 探测间隔秒 | `20` |
| `AGENT_VPN_WAIT_MAX` | VPN 挂起时 worker 最长等待秒（超时弃题） | `600` |
| `AGENT_MAX_TURNS` | 最大 LLM 轮次 | `400` |
| `AGENT_TASK_SEGMENT_SHORT_SEC` | 短题段墙钟预算（主闸；到点暂停、保留上下文） | `600` |
| `AGENT_TASK_SEGMENT_LONG_SEC` | 长题段墙钟预算（多 flag/hard/level≥3） | `1800` |
| `AGENT_TASK_MAX_TURNS` | 段轮数【安全网】（0=关；墙钟才是主闸） | `120` |
| `AGENT_SUMMARY_TIMEOUT_SEC` | 通关复盘限时（容器已关、脱槽跑；超时丢弃照常 pass） | `300` |
| `AGENT_RESUME_AGING_SEC` | 暂停题排队提权阈值秒（防 fresh 无限插队） | `600` |
| `AGENT_TASKS` | 只解部分题目（测试用）：逗号/空白分隔，支持 `*`/`?` 通配，空=全部；`--task`/`AGENT_TASK_FILTER` 优先 | 空 |
| `AGENT_PAUSE_MAX` | 每题最多暂停续跑次数（`0`=不限，一直续跑直到通关） | `0` |
| `AGENT_GIVEUP_MAX` | 连续「未通关就输出总结」的段数上限（`0`=不限；达上限判 fail） | `3` |
| `AGENT_MAX_FAILURES` | 真失败（LLM 致命错/上下文超限/VPN 放弃）放弃上限（≥1） | `2` |
| `AGENT_DEADLINE_SEC` | 任务总时限秒（0=不限） | `0` |
| `AGENT_SHELL_WAIT_MS` | shell 快速同步窗口 | `1500` |
| `AGENT_SHELL_HARD_SEC` | 后台命令硬上限秒（到点整组 kill，输出已落盘；0=永不杀） | `1800` |
| `AGENT_SHELL_MAX_JOBS` | 同时在跑的后台 job 上限（满了拒绝新 `run`） | `8` |
| `AGENT_FORK` | fork 多分支（`1`/`on` 开：分支工具 fork/core_log/broadcast + 交错轮转；缓存实测交错零代价） | `0` |
| `AGENT_BYSTANDER` | 旁观者（`1`/`on` 开：卡塞组合信号触发独立短上下文插嘴；可单独于 fork 开） | `0` |
| `AGENT_MAX_BRANCHES` | 活跃分支上限（含 trunk） | `3` |
| `AGENT_FORK_MAX` | 每题 fork 总数硬上限（含分支再分叉） | `5` |
| `AGENT_FORK_COOLDOWN_SEC` | fork 冷却秒 | `180` |
| `AGENT_BRANCH_PRUNE_SEC` | 分支 corelog/flag 双停滞剪枝阈值秒 | `600` |
| `AGENT_BROADCAST_MAX` | 每段每分支广播上限 | `8` |
| `AGENT_BYSTANDER_MAX` | 每段每分支旁观者注入上限 | `2` |
| `AGENT_BYSTANDER_COOLDOWN_SEC` | 旁观者冷却秒 | `400` |
| `AGENT_STALL_SHORT_SEC` / `AGENT_STALL_LONG_SEC` | 停滞信号阈值（短题/长题分档） | `300` / `600` |
| `AGENT_VISION` | 多模态读图 `auto`/`on`/`off`（auto=网关拒图时本会话降级为文本，只给元数据） | `auto` |
| `AGENT_VISION_MAX_KB` | `action=view` 单图 data URL 上限，超出先自动压缩 | `4096` |
| `VPN_CONF` | 容器内 vpn 配置路径 | `/config/vpn.ovpn` |
| `VPN_WAIT_SEC` | VPN 预检最长等待（hosted 强制） | `90` |

> 切换「内部本地跑分」时只需改 `BENCHMARK_BASE_URL` / `BENCHMARK_TOKEN`，代码零改动。
> 手动指定模式：`agent.js --mode local|hosted` 或 `AGENT_MODE=hosted`。

## bench 记录持久化（src/records.ts，替代旧 trace 双文件）

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
     │    每条含 ts/seq/turn/tool_call_id/**branch**（fork 多分支，0=trunk）
     │    + **成对起止时间戳（供甘特图）**：
     │    started_at/ended_at（epoch 秒·毫秒精度）+ duration_ms（自动算）——
     │    assistant=本次 LLM 调用起止、tool=工具执行起止、user/note=写入同刻；
     │    **同时是分段解题续跑时重建上下文的唯一来源**（`records.rebuildMessages`
     │    trunk / `rebuildBranches` 分支——分支按 fork_seq 截断父快照）
     │    **通关后同上下文追加复盘总结**
     │    （思路/工具困难/优化建议，assistant 条目带 kind_note:"closing_summary"）
     ├── corelog.jsonl                # fork/旁观者核心日志（AGENT_FORK/AGENT_BYSTANDER 开时）：
     │    {ts,seq,branch,kind,note?}，kind ∈ core|broadcast|bystander|flag|prune|fork；
     │    **flag 事件以 matched_flag_index 为稳定 ID**（flag 内容随靶机重启变、索引不变，
     │    续跑按 index 对账「还差 idx 2,4」）；旁观者输入按分支分组读此文件
     ├── branches/<N>/                # fork 分支命名空间（分支专属 cwd，shell 默认写这里；
     │                                #  读任意路径允许，写只写自己目录——提示词约定）
     └── <AI 解题产物>
```

- **隔离**：`run.sh` 只挂 `runs/<TOKEN>:/work` → 容器内只有当前 bench；
  其他 bench 不在挂载内（bench 内可读、bench 外不可读），不做 shell 拦截。
- **归档**：`docker/archive_runs.sh [TOKEN...]` 把记录打包到项目内 `docker/archives/`
  （默认 `<TOKEN>_<ts>.tar.gz`；在挂载范围外、gitignore 排除，容器永远读不到）并删除源目录；
  归档包存留自行管理，`ARCHIVE_DIR` 可改目标目录。
  运行中的 bench 会拒绝归档。
  容器内 root 产物（openvpn 强制 0600 的日志、root 属主的 `tasks/` 目录）宿主机用户读不动也删不掉：
  `run.sh` 启动前预建宿主机属主的 `openvpn.log` 规避（实测 openvpn 只写内容、不改属主/权限）；
  归档脚本在打包/删除失败时自动用 `tsec-agent` 镜像以 root 把属主归位后重试
  （需 docker 权限，历史残留目录同样可归档；docker 不可用则明确报错并保留源目录）。
- **反作弊**：任务不跨 bench 共享（题目/代号每轮不同）；`.gitignore` 排除 `docker/runs/`。
- 查看对话：`jq . docker/runs/<TOKEN>/tasks/e1-01/conversation.jsonl`

### 本地观察面板（React + Node 服务）

```bash
cd tsec_agent && pnpm build           # 首次/改完面板代码
cd tsec_agent && pnpm serve           # 自动发现最新 runs/<TOKEN>，5s 刷新 → http://127.0.0.1:8791
pnpm serve -- --dir ../docker/runs/<TOKEN> --port 8791 --url https://...
pnpm dev                             # vite dev server（5173，/api 代理到 8791）
```

浏览器面板（深色单页）展示：
- 全局：平台通关数/flag 数/总分（平台 API 不可达时**离线回退**本地记录统计）；
  **有选题时主数字只统计选中的题**：`平台(选题 9/63): 通关 1/9 | flags 3/20 | 总分 300
  [全局: …]`——选题模式取 `meta.runs[-1].task_filter`（agent 落盘），旧记录回退读环境/.env
- 每题：`▶ RUNNING 已跑时长 / attempt / target`、`✔ PASS(flags/耗时)`、`✘ FAIL`
- 进行中题目实时视角：**进行中的 shell 调用**（assistant 已发 tool_call 但未回结果 = 在跑，
  如 `wait job_id=xx timeout_sec=30`、`run: sqlmap ...`）、最近一次模型输出/思考
- 最近事件：llm_retry、parallel_cap、task_error 等
- **实时耗时预测 vs 历史归档**（有 `docker/archives/*.tar.gz` 才显示，固定两行）：
  ```
  预测: 8:45:51 vs 历史 8:27:36 Δ+18:14 | ✔提前1 ▲超时2 ⏳未开始1
  变化: ✔c-02 -3:07:24 ▲f2-05 +59:24(3:01:23/史2:01:58) ▲b-02 +2:26:14(5:33:20/史3:07:05)
  ```
  规则：默认每题与历史**等长**；**提前完成直接扣掉省下的时间**；还在跑的取
  `max(历史, 已耗时)`，超出部分**每次刷新实时累加**；显式选中但还没开始的题按历史计入
  （没设选题时不补历史题号，避免下个题集题号不同导致虚增）。
  基线 = 归档包里 `tasks.jsonl` 的分段耗时之和（同题以**最新归档**为准），只解包一次并缓存。

数据源全部**只读**持久化记录 + 平台 API（token=目录名），不碰 agent 运行时；无第三方依赖。

## 平台流程（agent 提示词内置）

1. `GET /openapi/v1/challenges` — 拉题列表与进度（优先 `is_completed=false`，由易到难）
2. `POST .../challenges/start?unique_code=` — 启动容器拿 `container_addr`（**10 分钟内接入**，active 上限 3）
3. 渗透解题拿 flag（`flag_count>1` 需逐个拿）
4. `GET .../challenges/hint` — 可选，查看后该题得分按比例扣减
5. `POST .../challenges/submit` — 提交 flag（`409 duplicate` 幂等跳过）
6. `POST .../challenges/close` — 通关/放弃后**必须**释放名额
7. 循环直到全部通关或任务超时

错误码处置、分题型渗透方法、输出约定全部在 `tsec_agent/src/prompt.ts` 的系统提示词里。
**平台返回 `invalid_state + already finished`（评测截止）= 终态**：agent 立即停掉所有
worker、清理其容器并正常退出（`run_finish: platform_ended`），绝不重试。

## 退出码（entrypoint）

| code | 含义 |
| --- | --- |
| 0 | 正常完成 |
| 10 | openvpn 启动失败 |
| 11 | VPN 联通预检失败（VPN检测未通过,请检查靶场VPN网络配置） |
| 12 | 冒烟测试失败（模型网关 / shell / 预检） |

agent.js 自身：`0` 正常收尾 / `2` 模型网关不可达 / `4` 控制器异常。

## 目录结构

> 镜像体积硬约束：**≤ 3 GB，口径是 push 上传后的压缩体积**（原始体积不是判据，实测比值约 3.7×）。
> 实测（2026-10-16）：`docker images` **原始 10.1 GB**；压缩
> `docker save tsec-agent:latest | gzip -1 -c | wc -c` = **2.71 GB** ✓（余量约 290 MB）
> 工具面（按领域覆盖、同类去重，每类留代表）：`metasploit-framework`、
> `hashcat`（容器无 GPU → CPU/pocl，只做离线爆破）、`zaproxy`（`-cmd -quickurl …` 无头 Web 漏扫）、
> `nuclei`、`responder`、`netexec` + BloodHound 采集器（`bloodhound-python` /
> `bloodhound-ce-python` / `sharphound`，**不装 neo4j 服务端**）、`aircrack-ng`、
> `volatility3`（pip 钉 `==2.28.2`，命令 `vol`）、`ghidra` 12.1.4（headless 入口 `analyzeHeadless`，
> 已链到 PATH）、`security-knowledge-base` 离线知识库，以及 JRE/python2/ssh/php/gcc/make/node/go/telnet。
> 版本口径：基底 `kalilinux/kali-rolling` 按 **digest 钉死**（升级 = 换两处 FROM 里的 sha256）、
> pip 精确钉版本、apt 跟随 rolling 最新；当次解析到的全量版本快照在镜像内 **`/opt/toolchain.lock`**，
> 要硬钉某个包照它写 `pkg=ver`。瘦身（doc/man/locale、`/usr/lib/jvm/*/jmods` ~340M、
> ghidra Extensions）**只能在 builder 阶段删**——最终阶段删只加 whiteout 层，`docker save` 里基底字节照旧。
> 仍不要引入：seclists 全量字典、oracle-instantclient、nuclei-templates 全量（GB 级，知识库里已有精选）。

```
tcbench/
├── README.md                      # 本文档
├── AGENTS.md                      # AI 协作者须知（架构不变量、改代码前必读）
├── task_<id>_vpn_config.ovpn      # 每个 bench 任务下发的 VPN 配置（以最新为准）
├── tsec_agent/                    # agent 代码（Node/TS；pnpm build 产出 dist/ 进镜像）
│   ├── src/agent.ts               # 主循环：LLM 调用 → 工具分发 → 结果回填
│   ├── src/shell.ts               # 异步 shell（job 管理、截断、超时、进程组 kill、读图）
│   ├── src/shell_tool.ts          # shell 工具 schema / 参数容错 / 动作路由
│   ├── src/llm.ts                 # OpenAI 兼容 fetch client、重试、key 发现与热切换
│   ├── src/bench.ts               # 平台 API 客户端
│   ├── src/prompt.ts              # 系统提示词（跑分流程/错误处理/渗透指引）
│   ├── src/records.ts             # bench 记录持久化 + 续跑上下文重建
│   ├── src/config.ts              # 配置（全部环境变量可覆盖）
│   ├── src/panel/                 # React 面板（web/ 前端 + server.ts + data.ts）
│   ├── test/                      # vitest 自测（e2e.test.ts 离线端到端）
│   └── README.md                  # 快速使用说明
├── arena/                         # 内部本地跑分题场（自建计分服务 + docker 真靶机）
│   ├── src/                       # server（路由/鉴权）/ state（状态机+计分+持久化）/ docker（驱动）
│   ├── challenges/<code>/         # 题包：challenge.json + Dockerfile（含 flag 字面量；
│   │                              #   .dockerignore 已排除 arena/，绝不进交付镜像）
│   ├── test/                      # vitest 契约+单测（22 用例，离线，FakeDriver）
│   └── README.md                  # 题场设计、题包 schema、ARENA_* 配置
├── docker/
│   ├── Dockerfile                 # Kali + 渗透工具 + openvpn + 纯 JS agent 产物
│   ├── entrypoint.sh              # VPN → 预检 → DNS → 冒烟 → 跑分
│   ├── run.sh                     # 一键启动（挂 runs/<TOKEN>:/work，预建宿主机属主的 openvpn.log）
│   ├── archive_runs.sh            # 归档记录到 archives/（root 残留用镜像归位属主兜底）
│   ├── runs/<TOKEN>/              # bench 记录：meta.json、events/tasks.jsonl、openvpn.log、tasks/
│   └── archives/                  # 归档包 <TOKEN>_<ts>.tar.gz（挂载范围外、gitignore 排除）
├── security-knowledge-base/        # 离线安全知识库（16 源 / 196MB；gitignore 不入库，进镜像 /opt/）
│   ├── A-基础与技巧/               # hacktricks、ctf-wiki、payloadsallthethings…（按主题，全文搜）
│   └── B-漏洞与利用/               # awesome-poc 中文 POC 文章 + nuclei 模板
│                                   #   （`nuclei-templates/cves.json` = CVE→模板路径 精确索引）
└── tsec_agent/node_modules/        # pnpm 依赖（gitignore 不入库；镜像不带，运行时零第三方依赖）
```

## 内部本地跑分题场（arena/）

不依赖官方下发 token 的自建计分服务：API 线格式与官方 Challenges API 逐字段一致，
`start`/`close` 真的创建/删除 docker 靶机容器（active 上限 3、发布端口 9100-9199、
label 对账），flag 运行时注入、计分/扣分/重复/终态全语义。agent 接入零改动：

```bash
cd arena && pnpm install && pnpm dev      # 127.0.0.1:8899，启动时预热构建靶机镜像
# tsec_agent 侧 .env（local 模式）：BENCHMARK_BASE_URL=http://<宿主机>:8899、BENCHMARK_TOKEN=arena-dev-token
```

加题 = `arena/challenges/<code>/` 下写 `challenge.json` + `Dockerfile`。内置两道示例：
`web_sqli_01`（SQL 注入，flag env 注入）与 `f2-05`（逆向题，从 bench 存档复刻的真题形态：
静态页 + `/download` 下发 validator ELF，flag 是逆向产物不进容器）。红线：题包含 flag 字面量，
`.dockerignore` 已排除 `arena/`，**别把它加回构建上下文**。细节见 `arena/README.md`。

## 测试

```bash
cd tsec_agent
pnpm install && pnpm test         # 离线自测（50 个 vitest 用例，不需要 VPN / 网关），必须全绿
node dist/agent.js --smoke        # 需要 VPN 已连 + 网关可达（先 pnpm build）
```

## 故障排查

| 现象 | 排查 |
| --- | --- |
| 容器 exit 11 | `docker/runs/<TOKEN>/openvpn.log` 看握手/路由；宿主机 `curl http://10.0.100.58` |
| 归档报 `tar: ... 权限不够` | 容器内 root 产物（openvpn 0600 日志等）；`./archive_runs.sh` 会自动用镜像以 root 归位属主重试，docker 不可用时按提示 `sudo chown -R $(id -u):$(id -g) docker/runs/<TOKEN>` 后重跑 |
| 网关域名解析失败 | entrypoint 会从 openvpn push 提取 DNS；检查日志 `gateway DNS resolved` |
| `docker pull` 超时 | registry-mirrors（见上）；或手动 `docker pull docker.m.daocloud.io/<image>` 再 tag |
| 平台 404 task_not_found | `BENCHMARK_TOKEN` 失效/未注入 |
| 平台 409 invalid_state 持续 | 任务已超时结束 → 停止并报告；若是 max active → close 一题再 start |
| 重复提交 | `409 duplicate` 属正常幂等，跳过 |
