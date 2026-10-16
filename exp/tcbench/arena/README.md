# arena —— 内部本地跑分题场（题场 API + docker 真靶机）

官方 Challenges API 的**自建计分服务**：线格式（URL/头/错误码/响应字段）严格对齐
`CHALLENGES_API.md`（官方原文），`tsec_agent` **零改动**接入——只改
`BENCHMARK_BASE_URL` / `BENCHMARK_TOKEN` 两个环境变量。

与根仓库 `mock/`（Python，纯内存假状态，服务 orchestrator 回归）不同：
**arena 的 start/close 真的创建/删除 docker 容器靶机**，flag 真打、计分真算。

```
tsec-agent（bench.ts 5 个端点）
   │  HTTP + BENCHMARK_TOKEN 头（线格式 = 官方 §5）
   ▼
arena  server.ts（路由/鉴权/422）→ state.ts（状态机+计分+持久化）→ docker.ts（驱动）
   │  docker run -d / rm -f（label tcbench.arena=1）
   ▼
靶机容器（宿主机发布端口 9100-9199，active 上限 3）
```

## 快速开始

```bash
cd arena
pnpm install
pnpm check        # typecheck + 22 个 vitest 用例（离线，FakeDriver）
pnpm dev          # 起服务：127.0.0.1:8899，真 docker 驱动，启动时预热构建靶机镜像
```

agent 侧接入（`.env`，local 模式）：

```bash
BENCHMARK_BASE_URL=http://127.0.0.1:8899   # agent 在容器里时用 http://172.17.0.1:8899
BENCHMARK_TOKEN=arena-dev-token            # 与 ARENA_TOKEN 一致
```

> local 模式不锁网（NET_RESTRICT=auto 判定公网 AI API 可达），agent 直连靶机端口没问题。
> `NET_RESTRICT=1` 的白名单只有 DNS/平台域名/模型网关，靶机会被 DROP——内部本地跑分约定用 local 模式。

## API（与官方逐字段一致）

```
GET  /openapi/v1/challenges                      题列表与进度
POST /openapi/v1/challenges/start?unique_code=X  启动靶机（active 上限 ARENA_MAX_ACTIVE，默认 3）
GET  /openapi/v1/challenges/hint?unique_code=X   提示（按 hint_cost_ratio 扣分；通关后 409）
POST /openapi/v1/challenges/submit               {"unique_code","flag"} → correct/awarded/cumulative_score/...
POST /openapi/v1/challenges/close?unique_code=X  删容器释放名额
```

守卫次序与语义要点：

| 顺序 | 守卫 | 响应 |
| --- | --- | --- |
| 1 | token 不符 | 404 `task_not_found`（官方语义：不是 401/403） |
| 2 | 任务总时限到点（`ARENA_TOTAL_LIMIT_S`） | 409 `invalid_state`，message 含 `finished` → agent 判终态即停 |
| 3 | flag 长度 1~4096 / 坏 JSON / 缺字段 | 422，FastAPI 形状 `detail:[{loc,msg,type}]` |
| 4 | start 时 active ≥ 上限 | 409 `invalid_state` |
| 5 | 重复正确提交 → 409 `duplicate`；已通关取 hint → 409；docker 失败 → 503 `resource_unavailable` | |

官方未定义处的决策（与根仓库 `mock/` 同口径）：start/close 幂等；
`container_addr` 仅 `available` 时在 list 暴露（start 响应即时给地址）；
就绪探测失败 → 回收容器回 `stopped`（agent 视为启动失败可重试）。

## 题包 schema（`challenges/<目录>/challenge.json`）

```json
{
  "unique_code": "web_sqli_01",        // 缺省 = 目录名
  "description": "……",                 // 可选
  "difficulty": "easy", "level": 1,    // 缺省 easy / 1
  "total_score": 100,                  // 缺省 100；每 flag 均分，余数给最后一个
  "hint": "……",                        // 可选
  "hint_cost_ratio": 0.2,              // 取 hint 后每 flag 扣分比例，缺省 0.2
  "flags": ["FLAG{...}"],              // 计分真值（唯一），flag_count = len
  "image": "my-target:latest",         // 可选；缺省则用同目录 Dockerfile 现场 build
  "container_ports": [8080],           // 容器内端口 → 发布到宿主机 9100-9199 池
  "flag_inject": "env",                // env = docker run -e FLAG_0.. 注入；none = 烤在镜像里
  "ready_path": "/"                    // 可选：TCP 通后再 HTTP 探这个路径
}
```

加一道题 = 建目录 + 写 `challenge.json` + 写 `Dockerfile`（或预构建镜像），重启服务即可。
flag 注入（`flag_inject: env`）时镜像里**不含任何 flag 字面量**，判分真值只在
`challenge.json`；靶机服务自己从 `FLAG_0..FLAG_n` / `FLAG_COUNT` 决定 flag 放哪。

内置示例两道（`challenges/`）：

- `web_sqli_01/`：stdlib-only Python 登录页，SQL 注入（`' OR 1=1-- `）拿管理员会话后
  `/admin` 出 flag——验证「起靶机→打靶→提交→close」全链路（flag 走 env 注入）；
- `f2-05/`：逆向题（hard/500 分，复刻自存档中的真题形态）——静态说明页 + `/download`
  下发 `validator` ELF（与存档 md5 一致），flag 是逆向产物（`flag_inject: none`），
  **容器/镜像/网页里都不含 flag 字符串**。

## 配置（ARENA_* 环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ARENA_BIND` / `ARENA_PORT` | `127.0.0.1` / `8899` | 监听地址（0 = 系统分配，测试用） |
| `ARENA_TOKEN` | `arena-dev-token` | 鉴权头值 |
| `ARENA_ADVERTISE_HOST` | `127.0.0.1` | 返回给客户端的宿主机地址；agent 在容器里时设 docker0 网关（如 `172.17.0.1`） |
| `ARENA_PORT_MIN` / `MAX` | `9100` / `9199` | 靶机宿主机发布端口池 |
| `ARENA_MAX_ACTIVE` | `3` | 并发闸门（= 官方 active 上限） |
| `ARENA_TOTAL_LIMIT_S` | `0`（不限） | 任务总时限；到点全部接口 409 终态 |
| `ARENA_CHALLENGES_DIR` | `<包根>/challenges` | 题包目录 |
| `ARENA_STATE_FILE` | `<包根>/state.json` | 进度持久化（gitignore 排除） |
| `ARENA_READY_TIMEOUT_S` / `INTERVAL_MS` | `60` / `400` | 就绪探测 |
| `ARENA_DRIVER` | `cli` | `fake` = 内存桩（演示/无 docker） |

## 持久化与重启对账

- `state.json`（原子写）：每题已收 flag、hint 状态、累计分、任务起始/截止时间——
  arena 重启**不重置任务窗口、不丢进度**；容器状态不落盘。
- 容器全部带 label `tcbench.arena=1, tcbench.code=<code>`；启动时 `reconcile()`：
  托管容器接管（重探就绪），无主容器清掉。
- 靶机镜像在服务启动时后台预热构建（`prebuild`），首次 start 不用等 build。

## 测试

```bash
pnpm check   # tsc --noEmit + vitest
```

| 文件 | 覆盖 |
| --- | --- |
| `test/contract.test.ts` | 起真 HTTP server + FakeDriver（`test/fixtures/` 题包，与 `challenges/` 解耦）：鉴权/字段全集/状态时序/计分/重复/422/幂等/闸门/终态/对账/hint 扣分 |
| `test/state.test.ts` | 计分余数、持久化回灌、驱动失败 → 503、对账接管 |
| `test/catalog.test.ts` | 题包 fail-fast（重复 flags/缺镜像缺 Dockerfile/坏字段） |

真实 docker 冒烟（需要 docker 组）：`pnpm dev` 起服务后按示例题手动
`start → SQLi 拿 flag → submit → close`，或看下方「加题指南」的验证脚本思路。

## 红线

- `arena/challenges/` 含 flag 字面量（题目答案）：**绝不进 agent 交付镜像**——
  `exp/tcbench/.dockerignore` 已加 `arena/`（构建上下文排除），别删。
- arena 只绑 loopback（默认），token 只是开发期弱防线，不要暴露到非受信网络。
