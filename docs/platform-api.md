# 平台 API 梳理与陷阱

来源：`../CHALLENGES_API.md`（官方接入文档）。本文件是面向实现的提炼，重点标注文档里容易被忽略、但写错就丢分的地方。

## 1. 认证

所有 `/openapi/v1/challenges/*` 接口通过 HTTP 请求头 `BENCHMARK_TOKEN` 鉴权（注意是**请求头名就叫 `BENCHMARK_TOKEN`**，不是 `Authorization: Bearer`）。

```http
GET /openapi/v1/challenges HTTP/1.1
Host: tsecbench.zc.tencent.com
BENCHMARK_TOKEN: <uuid>
```

token 无效或缺失 → `404 task_not_found`（不是 401/403，容易在错误处理里漏掉）。

## 2. 接口清单

| # | 方法 | 路径 | 作用 |
|---|---|---|---|
| 1 | `GET` | `/openapi/v1/challenges` | 题目列表 + 作答进度 + 容器状态 |
| 2 | `POST` | `/openapi/v1/challenges/start?unique_code=` | 启动题目容器 |
| 3 | `GET` | `/openapi/v1/challenges/hint?unique_code=` | 取提示（**扣分**） |
| 4 | `POST` | `/openapi/v1/challenges/submit` | 提交 flag（JSON body） |
| 5 | `POST` | `/openapi/v1/challenges/close?unique_code=` | 关闭容器释放资源 |

只有 submit 用 JSON body（`{unique_code, flag}`），其余全部走 query 参数。

## 3. 标准调用流程

```
GET  /challenges          获取题目列表
POST /challenges/start    启动目标题
  ↓ 轮询 GET /challenges 直到 container_status == available
（通过 VPN 访问 container_addr，渗透解题）
GET  /challenges/hint     可选，会扣分
POST /challenges/submit   提交 flag（多 flag 题需多次）
POST /challenges/close    释放容器
```

步骤 2–6 对每题重复。

## 4. 陷阱清单

按「踩中概率 × 后果严重度」排序。

### 4.1 `container_addr` 只在 `available` 时有值

文档明确：`container_addr` 仅当 `container_status == available` 时才有值，其余状态（`pending` / `stop_pending` / `stopped`）一律为空数组 `[]`。

**后果**：`POST /start` 返回的响应体里虽然带 `container_addr` 字段，但容器可能还在 `pending`。拿到 start 响应就冲过去访问，会连到一个还没起来的地址，然后 agent 会得出「目标不存在」的错误结论并放弃这道题。

**处理**：start 之后必须轮询 `GET /challenges`，等该题 `container_status == available` 且 `container_addr` 非空，才把地址交给 solver。轮询要有超时和退避。

### 4.2 大模型网关改写（写错 = 全程零分）

托管模式无公网，所有大模型调用必须改写：

1. 确认目标 API 在白名单内；
2. 原域名追加 `.tsecbench.gw` 后缀；
3. `https` 改为 `http`；
4. path / query 原样保留。

```
https://api.deepseek.com/v1  →  http://api.deepseek.com.tsecbench.gw/v1
https://api.moonshot.cn/v1   →  http://api.moonshot.cn.tsecbench.gw/v1
```

本项目使用 DeepSeek，改写后 base_url 为 `http://api.deepseek.com.tsecbench.gw/v1`。

**后果**：写错的表现是「模型调用全部超时或 DNS 解析失败」。托管沙箱无公网、无浏览器、一次性销毁，现场完全无法排查。

**处理**：改写逻辑做成纯函数，配 19 项白名单逐条断言的单元测试。见 [architecture.md](architecture.md) §6。

### 4.3 白名单（19 项）

```
https://api.hunyuan.cloud.tencent.com/*
https://api.lkeap.cloud.tencent.com/*
https://tokenhub.tencentmaas.com/*
https://api.deepseek.com/*
https://dashscope.aliyuncs.com/compatible-mode/*
https://*.maas.aliyuncs.com/compatible-mode/*
https://qianfan.baidubce.com/*
https://ark.cn-beijing.volces.com/*
https://open.bigmodel.cn/*
https://api.moonshot.cn/*
https://api.siliconflow.cn/*
https://spark-api-open.xf-yun.com/*
https://api.minimaxi.com/*
https://api.stepfun.com/*
https://api.lingyiwanwu.com/*
https://api.baichuan-ai.com/*
https://api.xiaomimimo.com/*
https://api.kimi.com/*
https://agent-awd.baidu.com/*
```

白名单外的域名即使加了 `.tsecbench.gw` 也走不通。任何工具在运行时联网拉取资源（nuclei 模板更新、pip install、apt update、词库下载、模型权重下载）在托管模式下都会失败 —— 这些东西必须在构建期全部离线打进镜像。见 [image-spec.md](image-spec.md)。

`https://agent-awd.baidu.com/*` 这一项对应决赛 AI-KOH（Attack-With-Defense）赛制，初赛用不到。

### 4.4 并发上限 3，且 `close` 是选手的责任

同一时间最多启动 3 道题。超限时 `POST /start` 返回 `409 invalid_state`。

**后果**：解完题忘记 `close`，活跃计数不下降，第 4 题永远起不来，Agent 会卡死在「start 一直 409」的循环里，把剩余时间全烧光。

**处理**：
- 编排器维护活跃容器集合，`close` 是状态机的必经终态，不是可选清理；
- 以 `GET /challenges` 返回的 `container_status` 为**真值来源**，本地计数只作优化；
- 收到 start 的 409 时，先做一次全局对账（清掉「本地记为活跃但平台已 stopped」的僵尸条目），再决定是否关掉优先级最低的题腾位置；
- 收尾阶段（总时限剩最后 N 分钟）强制 close 全部容器。

### 4.5 `409 invalid_state` 有三种完全不同的含义

同一个 `409 invalid_state` 错误码，在三类场景下触发：

- **任务级**：跑分任务已超时过期或被手动停止 → 所有接口都会返回，此时应全局停机；
- **额度级**：仅 `POST /start` 返回，含义是活跃实例数已达上限 → 此时应腾位置重试；
- **题目级**：仅 `GET /hint` 返回，含义是**该题已通关**，不允许再看提示 → 此时应标记 `is_completed`，与该题全局状态无关（见 4.7）。

**后果**：如果不区分，会在任务已结束后继续无意义地重试；或者在只是并发满的时候误判为「比赛结束了」而提前放弃；最坏的一种是 —— 对已通关题误取 hint 收到 409，被判成任务级终止，于是**在还剩很多题的时候全局停机，把剩余分数全部丢掉**。

**处理**：按接口分派，不是按错误码分派。只有 `POST /start` 的 409 走「腾位置重试」分支，只有 `GET /hint` 的 409 走「标记该题已完成」分支，**其余接口的 409 才视为任务级终止**。

### 4.6 一题多 flag

`flag_count` 可能大于 1，需要多次 submit。submit 响应里的 `correct_flag_count` / `total_flag_count` / `matched_flag_index` 是进度真值。

**后果**：解出第一个 flag 就 `close`，直接丢掉该题剩余分数。

**处理**：`correct_flag_count < total_flag_count` 时不 close，把「还差几个、已中哪个 index」回灌给 solver 继续。

### 4.7 hint 扣分且不可逆

`GET /hint` 之后，该题后续 flag 得分按 `hint_cost_radio` 比例扣减。已通关（所有 flag 已正确提交）的题不能再取 hint，会返回 `409 invalid_state`（即 4.5 的题目级）。

**后果**：随手取 hint 会把一道满分题打折；对已通关题取 hint 会收到一个看起来像「任务结束」的 409，如果按 4.5 误判就会全局停机。

**处理**：hint 由独立的 Hint Broker 决策，触发条件是「首轮时间片耗尽 + 零 flag + 扣分比例可接受」；对 `is_completed == true` 的题禁止调用 hint 接口。

**三个连带的文档缺口**（都不是我可以自行填补的）：

1. **`hint_cost_radio` 在响应里读不到**。文档说比例「由题目配置决定」，但 `GET /challenges` 的字段表里没有这个字段 —— 取 hint 前无法知道扣多少。现按 **10%** 设默认值，依据是**上届两处口径一致**（第二届 HTTP 文档「扣减10%」、MCP 文档「扣除该题总分的 10%」）。注意这是上届规则，本届若显著更高，此默认值必须改（[open-questions.md](open-questions.md) A10）。
2. **本届响应没有 `hint_viewed`**，上届有。意味着**「这题是否已取过 hint」在平台侧读不回来**，崩溃恢复后无法对账，可能重复取、重复扣分。只能靠 State Journal 同步 fsync 兜底（A10b）。
3. 扣分是「从奖励分数中扣减」还是「从该题总分扣减」，本届措辞（「按 `hint_cost_radio` 比例扣减该题得分」）与上届（「从奖励分数中扣减10%」）表述不同，实际影响是否只影响后续未得的 flag、还是追溯已得的 flag，**不明确**。这直接改变 Hint Broker 在第 2 个 flag 之前 vs 之后取 hint 的价值判断。

### 4.8 submit 幂等，重复提交报 `duplicate`

同一 `flag_index` 重复正确提交 → `409 duplicate`，不会重复加分。

**处理**：`duplicate` 不算失败，标记该候选已入袋，继续下一个。

### 4.9 flag 长度约束

`flag` 字段长度必须在 1~4096，否则 FastAPI 返回 `422`。

422 的响应体结构与业务异常**完全不同**（框架生成的 `{"detail": [...]}` 数组，而非 `{"code","message","detail"}` 对象）。错误处理里的 JSON 解析要能同时吃下两种结构，否则一个 422 会让解析器崩掉。

**处理**：正则收割出的候选先做长度过滤，再提交。

### 4.10 总时限之后一切作废

跑分任务有总时限，超时后所有接口返回 `invalid_state`。

**后果**：没有收尾保护的话，最后一个 solver 可能还在跑，而平台已经关门，收割到的 flag 一个也提交不上去。

**处理**：编排器持有全局 deadline，剩余时间低于阈值时停止启动新题、杀掉在跑的 solver、强制提交所有已收割候选、强制 close 所有容器。

### 4.11 可能有关卡门，但文档里既没有 403 也没有 `current_level`

本届响应有 `level` 字段，但**没有 `current_level`，错误码总表里也没有 403**。而**上届明确有关卡解锁机制**：响应含 `current_level`，`403 = 比赛尚未开始 / 尚未解锁对应关卡`，提交正确答案后 `message` 末尾追加 `【您已成功解锁新的关卡】`。上届参赛队 CHYing-agent 的复盘也印证卡关是真实机制（共 54 个 flag，解出 30 个，「卡在了第三关」）。

**后果**：若本届沿用而我们不处理，Phase 1 会把 3 个并发额度全部占在高关卡题上反复撞 403，白烧预算与限频配额，且每次撞墙都要走一遍 start→失败→close 的完整往返。

**处理**：编排器**先写好 403 分支**（移入 `LOCKED` 集合，不占额度、不需 close，等解锁信号后重新入队，见 [architecture.md](architecture.md) §3.1）。本届若无关卡门，这段代码永不触发，成本为零；反之若等到比赛中才发现，已经没有修复窗口。已列入 [open-questions.md](open-questions.md) A19。

### 4.12 限频：本届只字未提，上届是每秒 3 次

上届 API 文档专设「频率限制」一节：**每队每秒最多 3 次接口调用，所有接口共享计数，基于 token 识别身份，超出返回 HTTP 429**，并建议调用间加 `time.sleep(0.5)`。**本届文档对此完全没提。**

**后果**：3 题并发时，三个 solver 的收割结果可能同时触发 submit，叠加 Registry 的对账轮询，瞬时 QPS 很容易超过 3。若本届沿用上届限频而没写在文档里，表现是**大量莫名的 429**，而我们的错误表里没有 429 → 会被当成未知错误重试，越重试越撞。

**处理**：API Client 内置**全局令牌桶，默认 ≤3 req/s，所有接口共享一个桶**。即使本届无限频也无害（我们的调用量本来就很低）。真收到 429 则退避 1s 重试。列入 A6。

### 4.13 接口形态在两届之间整体换代过，不要因为「看起来一样」就假设行为一样

这是本节最该记住的一条。上届与本届的同一套功能，**认证头、路径、响应包装、状态机、错误码全都变了**：

| | 第二届 | 第三届（本届） |
|---|---|---|
| 认证头 | `Agent-Token` | `BENCHMARK_TOKEN` |
| Base | `http://<SERVER_HOST>/api` | `/openapi/v1/challenges` |
| 响应包装 | `{code, message, data}`，`code=0` 成功 | **裸 JSON**，业务异常才是 `{code,message,detail}` |
| 题目主键 | `code`（如 `BwAMWQASB1ROWFA`） | `unique_code`（如 `web_sql_injection_01`） |
| 实例状态 | `stopped` / `pending` / `running` | `stopped` / `pending` / **`available`** / **`stop_pending`** |
| 入口地址字段 | `entrypoint`（array\|null） | `container_addr`（array，非 available 时为 `[]`） |
| hint | `POST /api/hint`，固定扣 10% | `GET /openapi/v1/challenges/hint`，按比例 `hint_cost_radio` 且**读不到** |
| 限频 | 3 req/s → 429 | 未提及 |
| 关卡门 | 有（`current_level` / 403） | 有 `level`，但无 `current_level` / 403 |
| 接入方式 | HTTP API **或 MCP**（二选一） | 仅 HTTP API |

**结论**：上届的任何客户端代码、错误处理表、MCP 配置**都不能直接搬到本届**。可以复用的只有**调度思路与踩坑经验**。反过来，凡本文档中标注为「上届如此」的行为，在本届都属于**未验证假设**，必须在本地 mock server 里按本届文档实现，并按这些假设准备好被打破的空间（见 [architecture.md](architecture.md) §7）。

## 5. 响应结构

### 5.1 `GET /challenges` 元素字段

| 字段 | 类型 | 用途 |
|---|---|---|
| `unique_code` | string | 题目唯一标识，后续所有接口用它 |
| `description` | string \| null | 题目描述，**可能为空**，solver 要能在无描述下工作 |
| `difficulty` | string | easy / medium / hard，用于优先级排序 |
| `level` | int | 关卡，用于优先级排序 |
| `total_score` | int | 满分，用于「性价比」排序 |
| `flag_count` | int | flag 总数 |
| `correct_flag_count` | int | 已正确提交数 |
| `is_completed` | bool | 是否全部通关 |
| `container_status` | string | `pending` / `available` / `stop_pending` / `stopped` |
| `container_addr` | array[string] | 靶场直连地址（IP:端口），**可能多个**，仅 `available` 时非空 |

`container_addr` 是数组 —— 一题可能有多个入口（例如一个 web 服务 + 一个数据库端口，或多台内网机器）。solver 契约里必须传数组，不能只取第一个。

### 5.2 统一业务错误格式

```json
{ "code": "error_code", "message": "人类可读描述", "detail": {} }
```

### 5.3 错误码总表

| HTTP | code | 触发场景 | 处理 |
|---|---|---|---|
| 404 | `task_not_found` | token 无效/缺失/任务不存在 | 致命，停机并报错（配置问题） |
| 404 | `challenge_not_found` | `unique_code` 不在本任务题集内 | 从本地题库剔除，永不再试 |
| 409 | `invalid_state` | 任务已结束 **或** start 时活跃数达上限 **或** 对已通关题取 hint | 见 4.5、4.7 |
| 409 | `duplicate` | 同 flag_index 重复正确提交 | 标记已入袋，继续 |
| 503 | `resource_unavailable` | 靶场实例未就绪或已耗尽 | 指数退避 5s/15s/45s，3 次失败排队尾 |
| 500 | `internal_error` | 平台内部错误 | 退避重试 |
| 422 | （框架级） | 参数校验失败，如 flag 长度非法 | 过滤候选；注意响应体结构不同 |
| **403** | **文档未列出** | **推定为「关卡未解锁 / 比赛未开始」（上届语义）** | **移入 `LOCKED`，不 start、不占额度；解锁后重新入队。见 4.11。零成本防御性分支** |
| **429** | **文档未列出** | **推定为限频（上届为 3 req/s，所有接口共享计数）** | **全局令牌桶从源头避免；真收到则退避 1s 重试。见 4.12** |

> 后两行**不是本届文档的事实**，而是「上届有、本届未提但不能因此当它不存在」的防御性处理。两条都写成默认不触发的分支：本届若无此机制，代码路径永不执行。理由见 4.13 —— 接口在两届之间整体换代过，文档没写不等于没有。

## 6. 双模配置

### 6.1 本地评测模式

平台每次开始测试会下发一套 `ing/`：

```
ing/env                              BENCHMARK_TOKEN:<uuid>
                                     BENCHMARK_BASE_URL:https://tsecbench.zc.tencent.com
ing/task_*_vpn_config.ovpn           靶场 SSLVPN 客户端配置（含私钥）
```

**`ing/env` 是冒号分隔，不是标准 `KEY=VALUE`**，不能被 shell `source`，也不能被常见 dotenv 库直接解析。配置解析器必须同时支持 `KEY=VALUE` 与 `KEY:VALUE` 两种分隔符。

`BENCHMARK_BASE_URL` 在本地模式是 `https://tsecbench.zc.tencent.com`（真 HTTPS，不经网关）。

### 6.2 VPN 配置形态

> **本文档所在仓库是公开的**，因此 VPN 端点 IP/端口、token 值一律以占位符表示，具体取值只存在于 `ing/` 原件（已被 `.gitignore` 排除）。 VPN 端点虽是每次任务下发的一次性配置，但公开索引后会长期留存，不值得为可读性让步。

`ing/task_*_vpn_config.ovpn` 的关键指令：

```
client
dev tun
proto udp
remote <VPN_HOST> <VPN_PORT>
cipher aes-128-cbc
auth sha1
data-ciphers aes-128-cbc
compat-mode 2.4.8
key-direction 1
remote-cert-tls server
<tls-auth> ... </tls-auth>
<ca>       ... </ca>
<cert>     ... </cert>
<key>      ... </key>       ← 客户端私钥，敏感
```

- 证书型认证，**没有用户名/密码**，也没有 `auth-user-pass`；
- 内嵌 CA、客户端证书、客户端私钥、tls-auth 静态密钥；
- 配置文件里**没有任何 `route` / `redirect-gateway`**，路由完全由服务端 push 决定 → 连上之后靶场网段会自动出现，不需要我们手写路由；
- `compat-mode 2.4.8` + `aes-128-cbc` + `sha1`：老式参数组合，容器内的 openvpn 版本必须支持 `ignore-unknown-option` 与 `compat-mode`，即 **openvpn ≥ 2.5**（2.4 也认 `compat-mode`，但 2.6/2.7 对 legacy 算法的默认策略更严，需确认未被 crypto-policies 禁掉 sha1/aes-128-cbc）；
- 远端是**公网地址**（`<VPN_HOST>:<VPN_PORT>/udp`，值以 `ing/` 原件为准）。

**关键推论**：托管模式明确禁止公网访问，因此容器内自己拨这个 VPN 大概率拨不出去。托管模式极可能由平台在网络层已打通靶场可达性。这仍是待确认项，见 [open-questions.md](open-questions.md)，设计上按双模处理（见 [architecture.md](architecture.md) §6.3）。

### 6.3 托管模式

平台在任务创建后自动生成并注入：

| 变量 | 来源 |
|---|---|
| `BENCHMARK_TOKEN` | 平台自动注入，**不需要我们填** |
| `BENCHMARK_BASE_URL` | 平台自动注入，**不需要我们填** |

需要我们自己在平台「运行时环境变量」里配置的：大模型 API key、网关开关、时间预算、可选的 VPN 配置等。见 [runbook.md](runbook.md) §3。

平台支持「从 `.env` 导入」批量添加变量。
