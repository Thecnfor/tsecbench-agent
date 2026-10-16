# 运行手册

覆盖三件事：本地评测怎么跑、托管镜像怎么交付、测试怎么组织。当前 token 尚未生效，§2 的实弹部分待平台下发后执行；§4 的 mock 测试现在就能做。

---

## 1. 前置准备

本文档中的镜像名统一为 `tsecbench-agent:latest`（名称可自定，但需与上传时的 tag 一致）。

宿主机已确认具备：`docker` 29.8.2、`openvpn`、`nmap` 7.98、**`python3` 3.13.x**、**`node` v26.11.1**、**`go` 1.26.2**、`uv`。

> 三个运行时版本**与镜像内钉死的版本完全一致**（[image-spec.md](image-spec.md) §3.1）—— 这不是巧合而是刻意维持的：开发环境与镜像运行时同版本，能消掉一整类「本地能跑、镜像里跑不了」的 bug，这在只剩 4 天的窗口里值很多钱。**每次升级本机运行时都要同步检查镜像内的钉死版本**，否则这个优势立刻反转成隐蔽风险。
>
> `nmap` 自带 **612 个 `.nse`（Lua）**，这是 L4 注入平面「不新增运行时」的实证依据（[architecture.md](architecture.md) §1.5）。

需要补齐的宿主机工具（仅本地调试用，不进镜像）：`sqlmap`、`nuclei`、`ffuf`、`feroxbuster`、`ripgrep`、`gobuster`、`hydra`、`naabu`；若要跑 REA 的本地 smoke，还需 **JDK 21、Ghidra 12.1.x**（Node 已有）。

平台侧：每次开始测试，平台会下发一套全新的 `ing/` 配置（`env` + `task_*_vpn_config.ovpn`），token 与 VPN 均为一次性、与本次跑分任务绑定。

**构建上下文必须排除 `refs/`。** 在项目根建 `.dockerignore`，第一条就是 `refs/`。这只是第一道防线；真正的保证是构建后逐层扫描（[image-spec.md](image-spec.md) §6）—— `.dockerignore` 漏一行的后果是**参考实现被打包上传到托管平台**，而那里全程审计并部分公开，按平台作弊条款这会直接导致处置，不是扣分问题。

```
# .dockerignore
refs/
ing/
docs/
tmp
.git/
*.ovpn
.env
```

---

## 2. 本地评测模式

### 2.1 解析下发配置

`ing/env` 是**冒号分隔**，不是标准 dotenv 格式，不能直接 `source`：

```
BENCHMARK_TOKEN:<uuid>
BENCHMARK_BASE_URL:https://tsecbench.zc.tencent.com
```

（token 值不写入文档，以 `ing/env` 原件为准。）

⚠️ **切分隔符时只能在第一个冒号处切。** 这是本文件原本的一个错误示例：用 `awk -F: '{print $2}'` 取 `BENCHMARK_BASE_URL` 会得到 **`https`** —— 因为值本身含 `https://`。实测该文件第二行按 `:` 切出 **3 段**，第一行只有 2 段。照错误示例写进测试，得到的是一个「平台地址是 https」的诡异故障，比直接报错更难查。

```bash
# ✗ 错：值里含冒号时被切碎（实测得到 BENCHMARK_BASE_URL=https）
awk -F: '{print $1"="$2}' ing/env

# ✓ 对：归一化分隔符后交给 shell。已在当前这套 ing/env 上实测通过，
#   base_url 完整保留 https:// 不被切断
eval "$(sed -E 's/^([A-Za-z_][A-Za-z0-9_]*)[=:][[:space:]]*/\1=/' ing/env | tr -d '\r' | sed 's/^/export /')"
echo "$BENCHMARK_BASE_URL"   # → https://tsecbench.zc.tencent.com
```

⚠️ **上面这条 `eval` 只用于本地临时调试，不是正式实现。** 对不是自己写的文件做 `eval` 等于开一条代码执行路径（值里若含 `$(...)` 或反引号会被执行）。正式实现由 Ingest Resolver 在进程内解析，**绝不 eval、绝不经 shell**，见 [architecture.md](architecture.md) §6.5。

**`ing/` 里有什么是不能假设的** —— 文件名带任务 ID（`task_gDhBwRoIg0Lj_vpn_config.ovpn`），每次评测下发一套；分隔符、键名、文件数量、甚至「有没有 VPN 文件」都可能变。托管模式下**根本没有 `ing/`**（凭据由平台注入环境变量）。因此解析层必须「按内容识别 + 缺什么都能跑」，设计见 [architecture.md](architecture.md) §6.5。

```bash
export BENCHMARK_TOKEN=$(awk -F: '/^BENCHMARK_TOKEN/{print $2}' ing/env)
export BENCHMARK_BASE_URL=$(awk -F: '/^BENCHMARK_BASE_URL/{print $2}' ing/env)
```

### 2.2 拨 VPN

```bash
sudo openvpn --config ing/task_*_vpn_config.ovpn
```

配置文件里没有任何 `route` / `redirect-gateway`，路由由服务端 push，连上后靶场网段会自动出现。验证：拨号成功后对 `GET /challenges` 返回的 `container_addr` 做 TCP 连通性测试。

### 2.3 容器网络（最容易踩的坑）

**VPN 拨在宿主机上，容器默认 bridge 网络走不到靶场 IP。** 容器内的流量经 NAT 出宿主机时，源地址被改写、回程路由走不到 tun 接口，表现为「宿主机能连通靶场，容器内超时」。

两种解法：

| 方案 | 命令 | 取舍 |
|---|---|---|
| **A. host 网络（本地评测推荐）** | `docker run --network host ...` | 最简单，容器直接共享宿主机路由表，VPN 通了就通。缺点：无网络隔离，但本地评测不需要 |
| B. 容器内自拨 VPN | `docker run --cap-add NET_ADMIN --device /dev/net/tun -e VPN_CONFIG="$(cat ing/task_*.ovpn)" ...` | 更接近托管形态，能顺带验证 §6.3 的双模探测逻辑。缺点：需要特权，且宿主机与容器会各持一条 VPN 会话（注意平台是否限制并发连接数） |

本地评测先用 A 把功能跑通，再用 B 单独验证 VPN 双模探测分支。

### 2.4 启动

```bash
docker run --rm -it --network host \
  -e BENCHMARK_TOKEN \
  -e BENCHMARK_BASE_URL \
  -e LLM_API_KEY \
  -e GATEWAY_MODE=off \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LOG_LEVEL=debug \
  tsecbench-agent:latest
```

本地模式 `GATEWAY_MODE=off`：直连真实 `https://api.deepseek.com/v1`，不做 `.tsecbench.gw` 改写。

#### 快速调试循环（宿主机直跑，比容器快得多）

容器镜像的 runtime 阶段目前**尚未 COPY `orchestrator/`**、ENTRYPOINT 还是骨架，所以「在容器里跑官方测试」暂不成立。调参/调代码走宿主机直跑：

```bash
make check     # 只读连通自检（零副作用）
make run       # 前台起编排器（Ctrl-C 停）
make rerun     # 停掉在跑的编排器 + solver，后台重启（日志 /tmp/tsec-run.log）
```

**代码生效时机（关键）**：
- **改 `solver/` 下的代码实时生效** —— 每题都是新起的子进程，直接读仓库源码；改完下一个时间片就是新逻辑，**不需要重启**。
- **改 `orchestrator/` 需要 `make rerun`** —— 进程启动即把代码载入内存。重启走崩溃恢复（journal + 平台对账续跑），**已解出的题不会丢**，代价只是杀掉在跑的题。

**观察点**：`tail -f workspaces/journal.jsonl`（事件流：started / solver_done / repair / solved / abandoned）；每题完整轨迹在 `workspaces/workspaces/<code>_attempt<N>/stdout.log`。

**将来要在容器里跑同一套源码**（验证镜像行为又不想每次 rebuild）：把工作区 bind mount 进去，容器内看到的就是宿主机当前源码 —— 挂载点对齐 Dockerfile 的 COPY 目标（`solver/ → /opt/tsecbench/solver/`）：

```bash
docker run --rm -it --network host \
  -v "$PWD/solver:/opt/tsecbench/solver" \
  -v "$PWD/orchestrator:/opt/tsecbench/orchestrator" \
  -e BENCHMARK_TOKEN -e BENCHMARK_BASE_URL -e LLM_API_KEY \
  -e GATEWAY_MODE=off -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  tsecbench-agent:latest
```

（镜像与 prompt 的红线不变：`ing/` / `refs/` 绝不进镜像，挂载时也只挂源码目录。）

### 2.5 观察点

- 编排器是否正确列出题目、排序、遵守并发 ≤3；
- start 之后是否**等 `container_status == available`** 才把地址交给 solver；
- 每题结束后是否 close；
- 多 flag 题是否在 `correct_flag_count < flag_count` 时继续而非提前 close；
- flag 收割是否抓到散落在 stdout 里的候选；
- 平台页面上的得分与编排器日志中的 `awarded` / `cumulative_score` 是否一致。

---

## 3. 托管模式交付

### 3.1 构建与导出

```bash
docker build -t tsecbench-agent:latest .
# 构建流程内含泄漏检查，命中即失败（见 image-spec.md §6）
docker save tsecbench-agent:latest | gzip > agent.tar.gz
ls -lh agent.tar.gz     # 压缩后必须 ≤ 3GB

# 同时记下解压后大小 —— A14（3GB 指压缩还是解压）尚未有答复，两个口径都要有数
ls -lh agent.tar.gz                                     # 压缩后
docker rmi tsecbench-agent:latest 2>/dev/null; \
  gunzip -c agent.tar.gz | tar -tf - >/dev/null; \
  docker load -i agent.tar.gz; \
  docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' tsecbench-agent
```

当前预算压缩后约 1.88GB、解压约 4.7~5.6GB。**若主办方答复 3GB 指解压后大小，必须按 [image-spec.md](image-spec.md) §4.1 的降级阶梯砍第 3+4 步（Ghidra + REA，省约 600MB）后重测。** 不要等到上传当天才发现。

### 3.2 平台页面操作

1. 「切换 Agent」下拉：经确认平台**不做任何框架限制**，镜像内容完全自定义，只要能在官方机器上跑起来即可。选择项只影响平台的展示/归类。
2. 上传 `agent.tar.gz`（支持 `.tar` / `.tar.gz` / `.tgz`）。
3. 配置运行时环境变量（见 3.3）。
4. 创建任务，平台自动生成并注入 `BENCHMARK_TOKEN` 与 `BENCHMARK_BASE_URL`。
5. 容器启动后自行开始解题，无需人工干预。

平台明确要求：**务必先完成本地模式评测（Agent 已接入平台 API），再进行托管模式评测。**

### 3.3 运行时环境变量清单

| 变量 | 必填 | 示例值 | 说明 |
|---|---|---|---|
| `BENCHMARK_TOKEN` | 平台注入 | — | **不要手填**，平台自动生成注入 |
| `BENCHMARK_BASE_URL` | 平台注入 | — | **不要手填** |
| `LLM_API_KEY` | ✅ | DeepSeek key | 平台要求敏感配置一律走环境变量 |
| `GATEWAY_MODE` | ✅ | `on` | 托管模式必须 `on`，否则模型调用全部失败 |
| `LLM_BASE_URL` | ✅ | `https://api.deepseek.com/v1` | 填**未改写**的原始形态，由 Gateway Rewriter 按 `GATEWAY_MODE` 改写 |
| `LLM_MODEL` | 可选 | `deepseek-chat` | |
| `TOTAL_TIME_LIMIT` | 可选 | 秒数 | 若平台不下发总时限，必须手填一个保守值，否则收尾保护无法触发（见 open-questions） |
| `SOLVER_FRAMEWORK` | 可选 | `shell` | 已定为自研薄壳，默认 `shell`；保留该变量只是为了万一要用现成 CLI 包一层（§2.5） |
| `CONCURRENCY_LIMIT` | 可选 | `3` | **不得超过平台硬上限 3** |
| `API_MAX_QPS` | 可选 | `3` | 全局令牌桶速率（上届限频值，本届未提但默认自我限速，见 platform-api §4.12） |
| `TRAFFIC_MAX_MB` | 可选 | `512` | 单题 recorder 录制体积上限；超限降级为「只存 header + body 摘要，不存全文」，防 workspace 撑爆磁盘 |
| `RESERVE_SECONDS` | 可选 | `600` | 收尾保护预留时间 |
| `LOG_LEVEL` | 可选 | `info` | |
| `VPN_CONFIG` | ❌ 见下 | — | 仅当 §6.3 探测到需要容器内自拨时才用 |

### 3.4 关于 `VPN_CONFIG` 的注意

`VPN_CONFIG` 的内容是完整的 `.ovpn`，**内含客户端私钥**。平台页面说明「托管运行期间全程产生数据将被审计和部分公开」，环境变量是否会在任务配置中被展示/公开并不明确。

因此：

- 托管模式**默认不注入** `VPN_CONFIG` —— 因为推定平台侧已在网络层打通靶场可达性（且托管无公网，容器自拨公网 VPN 端点大概率也拨不出去）；
- 只有在实测确认托管沙箱需要我们自拨、且平台确认环境变量不会公开时，才注入；
- 即使注入，也使用平台当次下发的一次性配置（本身随任务失效），不使用长期凭证。

这是一个待确认项，见 [open-questions.md](open-questions.md)。

---

## 4. 测试组织

### 4.1 mock server（当前最高优先级可交付物）

自建一个 mock 实现 5 个接口，用于在没有真 token 的情况下验证编排器的全部逻辑。必须能模拟：

**本届文档明确要求的行为**

- `GET /challenges` 的完整状态时序：`stopped → pending →（N 秒后）available`，且 `container_addr` 只在 `available` 时非空；
- 活跃容器计数与 `POST /start` 的 `409 invalid_state`（第 4 题时起）；
- 全部错误码：`task_not_found` / `challenge_not_found` / `invalid_state` / `duplicate` / `resource_unavailable` / `internal_error` / `422`；
- 一题多 flag：`flag_count > 1`，逐次 submit 递增 `correct_flag_count`；
- hint 扣分与「已通关题取 hint 返回 409」——**本项目不调用 hint**（platform-api §4.7），
  mock 保留该路由**作绊线**：编排器整轮跑下来若命中它，即视为回归（见 §4.2 用例）；
- **响应里不提供 `hint_cost_radio`，也不提供 `hint_viewed`** —— 照本届文档实现，保持 mock 与真平台同形；
- 任务级超时：到点后所有接口一律返回 `invalid_state`；
- 故障注入：慢响应、连接重置、500、503、返回体畸形。

**本届文档未写、但按上届规则需要防御的行为**（做成可开关的 mock feature，便于验证分支正确又不干扰主流程）

- `429` 限频（开关：`MOCK_RATE_LIMIT=3`）—— 验证全局令牌桶确实把出站 QPS 压在阈值内，且收到 429 后退避而非误判；
- `403` 关卡未解锁（开关：`MOCK_LEVEL_GATE=on`，配 `current_level` 与解锁提示）—— 验证 `LOCKED` 状态不占并发额度、不重复 start、解锁信号后能重新入队；
- `POST /start` 幂等性未知：重复 start 同一题返回什么。

编排器通过 `BENCHMARK_BASE_URL` 指向 mock，无需任何代码改动即可切换 —— 这也顺带验证了配置解析与 API 客户端的正确性。

> mock 的接口形态**必须严格按本届文档实现**（裸 JSON、`unique_code`、`BENCHMARK_TOKEN` 头），不要照抄上届的 `{code,message,data}` 包装。上届文档只用来参考「可能有什么隐藏规则」，不用来定接口形状。见 [platform-api.md](platform-api.md) §4.13。

### 4.2 测试矩阵

| 用例 | 验证点 |
|---|---|
| 单题完整流程 | list → start → 轮询 available → solver → 收割 → submit → close |
| 3 题并发 | 并发闸门不超过 3，资源限制生效 |
| 第 4 题 start 409 | 对账清僵尸 / 腾位置逻辑 |
| 多 flag 题 | 不提前 close，结构化 `prior` 跨轮回灌 |
| 全部错误码 | 逐个注入，验证分派与退避 |
| **出站 QPS** | **全局令牌桶：3 题同时收割触发批量 submit 时，实测 QPS ≤3；`MOCK_RATE_LIMIT=3` 下无 429** |
| **429 注入** | **退避重试，且不被误判为任务级终止** |
| **403 / `LOCKED`** | **`MOCK_LEVEL_GATE=on` 时高关卡题被移入 `LOCKED`，不占额度、不发重复 start；解锁后重新入队** |
| **双图 `prior` 合并** | **两轮 solver 各写 `out/prior.json`，验证去重键由编排器算、`explored` 里已否证的假设在第二轮 `task.json` 中可见** |
| **`prior.json` 缺失/畸形** | **solver 被杀前来不及写 → 编排器降级为只回灌 `notes.md`，不崩** |
| solver 超时 | 124 kill 整个进程组，仍收割已产出候选 |
| solver 崩溃 / 脏输出 | flag 散落在 stdout 也能被抓到 |
| flag 只在 stdout | 验证「不信任 result.json」 |
| 超长 / 空 flag 候选 | 长度过滤，不触发 422 |
| 编排器崩溃重启 | Journal + `GET /challenges` 对账恢复，不重复计分 |
| **绊线：整轮不碰 hint** | 零 flag + 超时 + 预算充足的旧 Hint Broker 触发场景下，mock 的 hint 路由零命中（`state.hint_viewed` 为空）|
| 极短 `TOTAL` | Phase 1/2 切换、收尾保护、强制 close 与提交 |
| 零 flag 重试 | 不取 hint，靠 `RETRY`（换策略第二轮）解出；`task.json` 不含 `hint` 字段 |
| 网关改写 | 19 项白名单逐条断言 + 边界 + 幂等 |
| 配置解析 | **完整跑 [architecture.md](architecture.md) §7.1 的 22 条摄取用例矩阵**（含「值内含冒号不能被切成 https」「无 ing/ 目录视为正常托管模式」「solver 环境不得含 BENCHMARK_TOKEN」）|
| VPN 双模 | 可达时不拨；不可达且有 tun+NET_ADMIN 时拨；拨失败时降级继续 |
| **离线解题基准** | **用 `refs/BoxPwnr` 的题目集（HackTheBox / picoCTF 等）在无 token 条件下测自研薄壳的单题收敛率** —— 这是 B1 自研路线唯一能在 10/12 前拿到的量化证据 |
| **工具面自检** | **镜像内 ghidra headless / REA（本地路径，非 `npx`）/ feroxbuster / nuclei 离线模板各跑一次 smoke，确认无运行期联网行为；`ss -ltnp` 确认 REA bridge 未监听 `0.0.0.0`** |
| **NSE 完整性** | `ls /usr/share/nmap/scripts/*.nse \| wc -l` > 500（多阶段构建与清理步骤不得误删 `/usr/share/nmap`）；`nmap --script-help=http-headers` 有输出；`nmap -sC -p80 <本地靶>` 能跑通默认脚本集 |
| **LLM 写 NSE 的可行性（B11）** | **让薄壳在 DeepSeek 下现写一个最小 NSE（如「取 `/flag` 并在响应含 `flag{` 时报告」），统计首次运行成功率。** 若写不出可运行脚本 → 按 B11 把 Lua 用途①降级为「只调用现成 612 个脚本 + 参数化」，**并据此重排 solver 的扫描策略**，不要赛中才发现 |
| **Lua 无第三方库** | `luarocks` 不存在；镜像内除 nmap 自带 NSE 标准库外无其它 Lua 模块；确认没有运行期 `require` 外部包的路径 |
| **recorder 录制完整性** | solver 经 `HTTP_PROXY` 发出的请求 100% 落到 `out/traffic/*.jsonl`；绕过代理的裸 socket 连接**应录不到但要在日志里显式告警**（不能假装录到了） |
| **recorder 去重有效性** | 同一 `(method,url,body-hash)` 在第 2 个 attempt 再发时命中缓存，**实测 LLM 往返次数下降** —— 这是 §1.6 第 3 条性能杠杆唯一可量化的验证点 |
| **recorder 磁盘上限** | 打到 `TRAFFIC_MAX_MB` 后降级为 header+摘要，不 OOM、不撑爆 workspace，且收割仍能读到 body 摘要里的 flag |
| **recorder 只监听 loopback** | `ss -ltnp` 确认每题 sidecar 只绑 `127.0.0.1`，绝不 `0.0.0.0`（托管全程流量审计，意外对外监听会被记录在案） |
| **CA 私钥不在镜像** | 泄漏扫描能抓到构建期 CA 私钥；运行期私钥为每题临时生成且随 workspace 销毁 |
| **收割走 traffic 通道** | 构造「flag 只出现在某条响应体、且 solver 完全没打印到 stdout」的用例 → 验证不依赖模型自述也能收割到 |
| 镜像验收 | 体积 ≤3GB（**并记录解压后实际大小**，为 A14 答复做准备）、启动即自解、无敏感文件、无公网依赖、日志脱敏 |
| **泄漏检查：参考资料类** | **镜像各层不含 `refs/` 路径指纹、上届答辩 PPT、上届题目名/flag 字面量；同时验证 Flag Harvester 的提取正则本身不会被误报为「硬编码 flag」** |

### 4.3 端到端（等真 token）

顺序不可颠倒：

1. **单题**：`CONCURRENCY_LIMIT=1`，跑一道 easy，验证全链路与平台计分一致；
2. **3 题并发**：验证闸门、资源限制、close 时序；
3. **全程限时**：完整跑一次任务，观察预算分配与收尾保护；
4. **托管上传**：本地全绿之后才上传，避免浪费托管评测次数。

---

## 5. 赛中处置速查

| 现象 | 首要怀疑 | 动作 |
|---|---|---|
| 所有模型调用超时 / DNS 失败 | `GATEWAY_MODE` 未设 `on`，或 base_url 改写错误 | 检查环境变量；托管模式无法现场修，只能重新上传镜像 |
| start 一直 409 | 有容器没 close，或本地计数与平台漂移 | 看 Registry 对账日志；必要时手动 close 低优先级题 |
| solver 报连不上靶场 | VPN 未通 / 未等 `available` 就冲 | 检查 `container_status` 时序与 VPN 探测日志 |
| 分数低于预期 | 多 flag 题提前 close / 收尾未提交 | 查 Journal 里的 `awarded`、`correct_flag_count` 轨迹 |
| 接口全部 `invalid_state` | 任务已超时结束 | 已无法挽回；复盘预算分配与收尾保护是否触发过晚 |
| 容器被 OOM | solver 内存超配额 | 降低单 solver 并发工具数，或降 `CONCURRENCY_LIMIT` |

---

## 6. 赛后复盘

沙箱一次性销毁、平台不留存程序，因此**日志是唯一资产**：

- 全程 JSON lines 日志必须在容器存活期间持续输出到 stdout（平台会采集审计）；
- 记录每题的时间片消耗、token 消耗、工具调用序列、候选 flag 与 submit 结果；
- 脱敏：token 与 API key 不得出现在日志中；
- 赛后按「未解出题目的失败模式」归类，作为决赛（AI-KOH）与下一次的输入。
