# 架构设计

状态：设计已确认（2026-10-10 增补：§12 Jev 排除决策、§13 旗舰评测集口径），未开始实现。

范围：主赛线上初赛（TSecBench 托管运行的自主渗透解题）。决赛 AI-KOH 只在 §8 做接口预留，本次不实现。

---

## 0. 总体思路

平台 API 的那些约束（并发 ≤3、`available` 轮询、`close` 释放、总时限、flag 去重）是**规则明确、错了就直接丢分**的东西，交给确定性代码，绝不让大模型自由发挥。

渗透解题是**开放式、需要大量工具与试错**的东西，交给成熟的 agent 框架。

两层之间用一个进程边界契约（Solver Adapter）隔开，使得「用哪个 agent 框架」这个决策可以推迟到赛前实测之后再定，且换框架不动编排器一行代码。

```
┌─────────────────────────────────────────────────────────┐
│ L0  Orchestrator（Python 3.14 asyncio，确定性，不经大模型）│
│      Platform API ─ Registry ─ Scheduler ─ Harvester     │
│      Submitter ─ Journal ─ Gateway Resolver              │
└────────────────────────┬────────────────────────────────┘
                         │ Solver Adapter 契约（进程边界 + 文件约定）
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼            ← 并发 ≤3
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│ L1 Solver #1  │ │ L1 Solver #2  │ │ L1 Solver #3  │  每题独立进程
│ Python 薄壳   │ │               │ │               │  独立 cwd
│  + recorder   │ │  + recorder   │ │  + recorder   │  独立 sidecar
└───┬───────┬───┘ └───────────────┘ └───────────────┘
    │       │
    │       └─→ L3 能力平面（常驻 sidecar，Node 26）
    │            REA（133 个 MCP 逆向工具）· 自研 TS MCP server
    │
    └─→ L2 工具平面（spawn，Go/Rust 静态二进制，单题可并发 8~12）
         Go   : recorder（流量录制/重放）· naabu/httpx/nuclei/katana
         Rust : feroxbuster · ripgrep（收割热路径）
         其它 : nmap / gdb / rizin / Ghidra headless / playwright
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ L4 注入平面 —— **跑在目标进程里**，不是我们的进程         │
│   Lua: solver 现写 .nse → 交给 nmap 的 Lua 引擎执行       │
│        Redis EVAL / OpenResty content_by_lua 直接代码执行 │
└────────────────────────┬────────────────────────────────┘
                         ▼
              靶场 container_addr（经 VPN / 平台网络）
```

**L2 的 recorder 同时向 L0 回流**：它录到的请求/响应是 `prior.assets` 的**客观来源**，也是 Flag Harvester 的一条不依赖模型自述的收割通道（§1.2、§2.3）。这是 recorder 存在于这张图上的原因 —— 它不是一个工具，它是**客观性层**。

---

## 1. 多语言分层设计

### 1.0 分层总则

五种语言，按**执行位置**分层，不按「哪门语言更先进」分层。一条硬规则贯穿全表：

> **一门语言只能占据一个执行位置，且必须干那个位置上别的语言干不了（或干得更便宜）的事。** 若某位置用 Python 也能做，就用 Python —— 多一个运行时就多一个崩溃面、依赖面和镜像体积。

| 执行位置 | 语言 | 干什么 | 为什么必须是它 |
|---|---|---|---|
| **L0 编排平面**（我方常驻进程） | **Python 3.14** | 确定性编排器：平台 API、状态机、调度、预算、收割、提交 | 无语言优势，是**生态优势**：内层参考实现（ARTEMIS / ctf-agent / BoxPwnr）全是 Python，同栈 = 单一运行时 |
| **L1 解题平面**（每题一个子进程） | **Python 3.14** | solver 薄壳 + 现场写的 exploit 脚本 | pwntools / pycryptodome / z3 / scapy 无可替代，CTF 事实标准 |
| **L2 工具平面**（spawn 出去的静态二进制） | **Go / Rust** | 高速扫描、目录爆破、流量检索、**流量录制（recorder）** | 见 §1.2/§1.4 —— 这是本项目**真正吃到多语言性能红利的唯一一层** |
| **L3 能力平面**（常驻 sidecar，MCP/HTTP） | **Node 26 / TS** | REA 逆向工具面（133 个 MCP 工具）+ 自研 TS MCP server | REA 是 npm 分发的 TypeScript，**没有选择**；Node 26 原生 TS 直跑省一个构建步骤 |
| **L4 注入平面**（**跑在目标进程里**） | **Lua** | nmap NSE 自定义探测；Redis/OpenResty 类目标的进程内执行 | **唯一能进目标运行时的语言** —— L0~L3 都是宿主侧语言，做不到 |

**L4 是这一版新增的、也是唯一真正扩展能力边界的层。** 其余四层都在优化「我们的代码怎么跑」，L4 优化的是「能不能把逻辑送进对方进程」。

### 1.1 Python 3.14（L0 + L1）

- **钉死 3.14**：已发布满一年（维护版 3.14.8，本机即 3.14.4），`cp314` wheel 成熟；**下限由 `ctf-agent` 的 `requires-python = ">=3.14"` 硬定**。
- **排除 3.15**：3.15.0 于 2026-10-09 发布（本设计定稿前一天），`cp315` wheel 生态为零，pwntools / z3-solver / gmpy2 / cryptography / pycryptodome 全部需源码编译 —— 3GB 镜像 + 4 天窗口不可接受。PEP 810 惰性导入是 opt-in 特性，不构成理由（§9）。
- **3.14 不是 Debian trixie 自带版本**（自带 3.13），镜像内用 `python-build-standalone` 预编译包安装（按 tag + sha256 钉死）。**不要用 `deadsnakes` PPA —— 那是 Ubuntu 的，trixie 用不了。**
- 编排器约 1000~1500 行；`asyncio.create_subprocess_exec` + `start_new_session=True` + `os.killpg` 足以做进程组管理与硬超时（§5.2）。
- **性能相关的约束**：GIL 使单进程内无法并行 CPU 工作，因此 **L0 绝不做重计算** —— 所有 CPU 密集活推给 L2。L0 只做 IO 与状态迁移，asyncio 在该用途上开销足够低。
- 内层 agent 为**自研 Python 薄壳**（借鉴 ARTEMIS / ctf-agent / ARTEX / BreachWeave），被 Solver Adapter 契约隔离；不整体移植任何现成框架，理由见 §2.5。

### 1.2 Go 1.26（L2）—— 真实职责，不是打酱油

**先说清楚：Go 在本项目不写扫描器。** `naabu` / `httpx` / `subfinder` / `nuclei` / `katana` 都是现成的 Go 静态二进制，按 tag 下载 + 校验 sha256 即可（[image-spec.md](image-spec.md) §2 优先级 1）。自己写全端口扫描器违反「能用现成工具就不自研」。

Go 的**自研**职责只有一件，但它是架构上的关键件：

> **`recorder` —— 每题一个的流量录制 / 重放代理（透明 HTTP(S) MITM sidecar），Go 写的单静态二进制。**

为什么这件事必须是 Go，且为什么它必须存在：

| 理由 | 说明 |
|---|---|
| **它补上了 §2.3 的实现缺口** | 我们宣称「编排器不信任 solver 的 `result.json` 自述」，但 `prior.assets` 目前**仍然依赖 LLM 自报它观察到了什么**。recorder 让 assets 来自**客观录制到的请求/响应**，不来自模型叙述。这是「客观性」从口号变成代码的唯一路径 |
| **它是探测去重的物理基础** | solver 在 3 个 attempt 里会重复发同一个包。按 `(method, url, body-hash)` 建索引后，重探测变成查缓存 —— **直接砍掉一整类无效往返**，是 §1.6 里第 3 条性能杠杆的实现载体 |
| **它是 Flag Harvester 的高价值输入** | flag 常常出现在**响应体**里，而不是模型主动打印到 stdout 的那几行。录制文件给 §2.3 的收割多出一条**不依赖模型配合**的通道 |
| **Go 的运行时形态正好匹配** | 单静态二进制、无共享库依赖、无 GIL、`crypto/tls` 动态签发 + `net/http/httputil` 反代是标准写法、数千并发连接内存平稳。**构建期产物、运行期零依赖** —— 符合无公网约束 |
| Python 的对应物 `mitmproxy` 是约 100MB+ 依赖链的**交互式调试工具**，不是嵌入式 sidecar 形态 | 这不是「Go 比 Python 快」的空话，而是**打包形态与依赖体积**的具体差异。注意：此处选 Go 的理由与性能无关，是形态 |

**实现规模控制（防止它长成第二个项目）**：只做 HTTP/HTTPS 录制 + 按 key 重放 + 落盘 JSONL。**明确不做**：websocket、脚本化改写、证书固定绕过、任何 UI、任何查询语言。

参照物：ARTEX 有一个同类 `traffic :8788` 录制代理，可作设计参照 —— **只读思路，不搬代码**（AGPL-3.0，见 §10.2）。

**版本**：Go 1.26.x（本机 `go1.26.2`；ARTEX `go.mod` 写 `go 1.26.3`，说明 1.26 线已是该生态实际基线）。**Go 工具链仅构建期需要**，runtime 阶段只 COPY 编译产物。

### 1.3 Node 26（L3）—— 含一条必须知道的风险

**职责**：承载 REA（`rea-agents`，npm 分发的 TypeScript，133 个 MCP 工具）；若自研 MCP server，用 TS 写。

⚠️ **Node 26 目前还不是 LTS。** 官方计划 **2026-10-28** 进入 Active LTS —— 那**在初赛（10/17–18）之后 10 天**。Node 官方指引明确写着「Production applications should only use Active LTS or Maintenance LTS releases」。所以「Node 26 LTS」这个说法**在我们比赛的当口还不成立**，必须先讲明再决策。

仍然选 26，理由与对冲：

| 选 26 的理由 | 对冲措施 |
|---|---|
| 本机已是 `v26.8.1` → 开发与镜像同版本，消掉一类「本地能跑、镜像里跑不了」的 bug | **镜像内钉死精确小版本 26.8.1**（不是 `26`、不是 `lts/*`），行为冻结，不受 10-28 那次 LTS 转正发布本身的影响 |
| REA 的 `engines.node` 含 `>=26.0.0`，官方支持面覆盖 | 已用 clone 下来的 `refs/rea/package.json` 核实 |
| **Node 26 原生 TS type-stripping 可直跑 `.ts`**，4 天窗口里省掉一个构建步骤 | **但不把这个特性用作运行时前提**：MCP server 在构建期编译成纯 JS 再打进镜像。于是「26 → 24」的降级是**一行基础镜像改动**，不是重构 |
| Temporal 默认启用，解析证书有效期 / JWT `exp` / TTL 更稳 | 属加分项，**不作为设计依据** —— 时间处理主要发生在 Python 侧 |

**结论**：镜像用 **Node 26.8.1**；因构建产物是编译后的 JS，**若 10/16 彩排出现任何 Node 侧异常，切 24 LTS 的代价是一行**。这一条把「追新」的风险压到可承受 —— 但也仅此而已，它是**已知未消除**的风险，不是没有风险。

- **不用 Bun**：BreachWeave 用 Bun 且拿了上届第一，说明可行，但 playwright 与部分框架的兼容边角问题不值得在 4 天窗口冒险；且 REA 只声明 node engines，未声明 Bun。

### 1.4 Rust（L2）—— 角色是「消费 Rust 生态」，不是「写 Rust」

诚实的定位：**本项目里 Rust 主要是买现成的，不是自己写。**

| 用途 | 现成件 | 定位 |
|---|---|---|
| 高速递归目录 / 参数爆破 | **`feroxbuster`** | 主力，替代 `dirsearch`（Python） |
| 海量输出与录制文件的 flag 检索 | **`ripgrep`（`rg`）** | **收割链路的热路径**：§2.3 要对 workspace + 全量 stdout + recorder 的 JSONL 做正则扫，数据量可达数百 MB~GB 级，`rg` 比 Python `re` 快约一个量级。这一条是因为 recorder 的加入而**新出现的 Rust 需求** |
| 全端口扫描加速 | `RustScan`（可选，需 raw socket） | 待 A4 答复再定 |

- **自研 Rust = 默认不做**。只有当现成件覆盖不到、且该路径经 profile 确认是 CPU 热点时才写，且**只写成独立 CLI 用 spawn 调用**。
- **不用 PyO3 / napi-rs 的 FFI 进程内绑定** —— Rust panic 会带走整个 solver 进程，摧毁 §5.2 赖以存在的崩溃隔离边界；且渗透工具的调用形态是「外部 CLI 跑几十秒到几分钟」，序列化开销可忽略，**本项目不存在需要 FFI 的热路径**。ARTEMIS 的 `codex-rs/`（`linux-sandbox` / `execpolicy` / `apply-patch`）印证同一判断：Rust 在本领域的正确用法是独立二进制，不是库。
- Rust 工具链**仅构建期需要**，runtime 阶段不留。

### 1.5 Lua（L4）—— 「动态潜入」= 进入目标的解释器，不是给我们的栈再加一层

L0~L3 全在解决「**我们的代码跑在我们这边**」。Lua 解决一个此前完全没覆盖的能力：**在不重新构建任何东西的前提下，把一段新逻辑送进目标进程里它自己已有的解释器执行。** Python / Rust / Go 都做不到 —— 它们是宿主侧语言，进不了对方运行时。

| 用途 | 机制 | 为什么必须是 Lua |
|---|---|---|
| **1. nmap NSE 自定义探测脚本**（做） | solver 在 workspace 现写 `probe.nse`，`nmap --script ./probe.nse` | nmap 的脚本引擎**只有 Lua 一种语言**。要在一次扫描内核里加自定义逻辑（多轮 HTTP 状态机、特定协议深度指纹、把上轮发现的 token 带回下个请求），除写 NSE 没有别的路。且**不产生新进程、新依赖、新超时面** —— 逻辑作为数据喂进已在镜像里的二进制。本机 nmap 7.98 自带 **612 个 `.nse`**，故**不新增运行时** |
| **2. 面向 Redis / OpenResty / ntopng 的直接代码执行**（做） | `EVAL "<lua>"`、`content_by_lua` 等 | 目标是 Lua 宿主时，**Lua 是唯一能在其进程内执行的语言**。Redis 的 Lua 沙箱逃逸（CVE-2022-0543，Debian 包 `enable-lua-sandbox` 缺陷）是公开技术，属常规武器库，不是题解预置 |
| **3. 薄壳内轻量插件点**（**默认不做**） | 用 Lua 当纯函数变换器 | 投机设计，违反 §0 |

纪律（防止 Lua 变成第四个要维护的运行时）：

- **`lua5.4` 只为本地写/自检 NSE 而装**（apt 约几百 KB）；**solver 不通过 `lua` 独立执行攻击链** —— 所有 Lua 逻辑经 nmap / redis-cli 等**已有工具**注入，崩溃与超时仍由 §5.2 的进程组隔离统一管。
- **不装 `luarocks`，不引入任何 Lua 第三方库。** NSE 只用 nmap 自带的 `nmap.*` / `stdnse.*` / `shortport.*`。装 luarocks = 开新依赖树 + 开新运行期联网面，无公网下必然失败。
- **镜像内不预置任何针对特定题目的 `.nse` 或 Lua payload。** 允许内置通用能力（「会写 NSE」这件事），不允许内置题解 —— 与 §10.4 同源。
- LLM 现写 NSE 的成功率是未知数（NSE 标准库对模型很陌生）→ 已列为 10/12 必测项（B11），失败则降级为「只调用现成 612 个 + 参数化」。

#### 1.5.1 被驳回的另一种 Lua 定位：在**我们**的进程里嵌一个 Lua 运行时

2026-10-10 收到一份建议，主张把 Lua 做成**宿主侧的「最后一公里执行器」**：Python 用 **Lupa** 嵌 LuaJIT、Rust 用 **mlua** 暴露原生扫描函数、Go 用 **gopher-lua** 在 worker 里跑 Agent 生成的任务脚本。

**这套方案本身技术上完全成立**（三个绑定库我都核实为真实且活跃，见 §11），但它与本项目已定的三条原则直接冲突，逐条驳回：

| 建议 | 驳回理由 |
|---|---|
| Python 经 Lupa **在编排器进程内**执行 LLM 生成的 Lua | **与 §1.4 拒绝 PyO3/napi-rs FFI 是同一条理由，且更严重。** LLM 生成的代码是不受信输入：死循环、内存爆炸、误调 `os` 都能带走整个 L0 进程 —— 而 L0 是我们唯一必须免于崩溃的组件（§4.1）。为了「毫秒级启动、不 spawn 外部进程」牺牲崩溃隔离，是拿确定性换微秒 |
| 「动态生成即执行」比 Python 更省 | **收益不绑定。** 我们的负载是网络 IO 密集型，单次 LLM 往返 1~10s（占端到端 ~55%，见 §1.6），而 Python 进程冷启动是几十毫秒级 —— 省掉它一分钱不省。而「生成可运行代码」这件事**已经有 Python 通道**（solver 现写 exploit 脚本），不需要第二个解释器 |
| Rust 用 mlua 把高速扫描函数暴露给 Lua | **双重不成立**：本项目不自研 Rust 扫描器（§1.4），且不存在需要 FFI 的热路径 |
| Go 用 Redis Lua 原子锁协调多 worker | **前提不存在。** 我们是**单容器、一次性沙箱、无公网**，没有自己的 Redis，也不需要跨机协调；进程间协调用文件锁 / asyncio 原语足够。为此引入 Redis = 在镜像里多养一个守护进程 + 多一个崩溃面。**典型的把分布式经验套到单机问题上** |
| 路线图「Python+Rust → Python+Go → … → 全五栈」渐进 | 与已定决策矛盾（L0 就是 Python，不回退 Rust 编排）。且本项目**已经是五层**（§1.0），不存在「还没引入」的说法 |

**唯一被承认有未来的宿主侧用法**：`gopher-lua` 嵌进 Go `recorder` 做**请求阶段规则**（条件-动作）。这有成熟先例（Kong / Envoy / Mosquitto 都正是这么嵌 Lua 的），也是三个绑定库里唯一与我们架构对得上的一个。但 §1.2 明确把 recorder 定义为「只做录制+去重+重放，不做脚本化改写」，所以现在**仍不做** —— 记为 B13，触发条件是「真的需要按流量特征自动触发响应动作」出现，而不是「因为 Lua 能做」。

**一句话原则**：**Lua 归 L4（目标侧），不归 L1/L2（宿主侧）。** 让它在对方进程里干活是我们引入它的唯一理由；把它搬回自己进程，等于用一个新的崩溃面去换一笔根本不存在的收益。这份建议里唯一值得保留的是它的收尾原则 ——「**Lua 不参与决策，只负责执行**」，而 §0 的确定性编排本来就把它约束得比这更死。

### 1.6 性能模型：这个系统的性能到底从哪来

要求「整体性能强悍」，先把账算清楚 —— **端到端墙钟时间的构成高度偏斜，语言选择只影响其中一小块：**

| 占比（估） | 开销项 | 可控手段 | 语言是否相关 |
|---|---|---|---|
| **~55%** | **LLM 往返**（每题数百次调用 × 1~10s） | 提高单次工具调用的信息密度、工具结果压缩与预聚合、上下文裁剪、prompt 复用 | **无关** —— 换任何语言都一样等 |
| **~30%** | **工具执行**（nmap 全端口 / Ghidra headless / feroxbuster，单次数十秒到数分钟） | 并发扇出、right-size 扫描范围、结果缓存、静态二进制 | **强相关** ← L2 用 Go/Rust 的全部理由 |
| ~10% | 重复劳动（跨 attempt 重新发现同样的东西） | **recorder 去重**（§1.2）+ `prior.explored` 记录已否证假设 | 无关（架构层问题） |
| ~5% | 进程编排 / IPC | spawn 开销、序列化 | 微弱；FFI 省的是**微秒**，而我们每次调用间隔是**秒** → 故 §1.4 拒绝 FFI |

**所以本项目「性能强悍」的正确目标函数不是「选更快的语言」，而是这三条：**

1. **把 LLM 往返次数压到最低**（占 55%）—— 一次工具调用要回给模型**更密**的信息：`rg` 预聚合、nmap 结构化输出（`-oX` 而非裸 stdout）、recorder 摘要，而不是让模型发十次小请求。
2. **把工具执行并行化**（占 30%）—— 单题内 solver 应**并发扇出**多个探测（枚举 ∥ 目录爆破 ∥ 指纹），不是串行排队；3 题之间本就并行（§4.1 每题 ~2.5 核）。这也是 L2 必须是静态二进制的实际原因：同等 CPU 下能扛更高并发。
3. **永远不重复已发过的探测**（占 10%）—— recorder 的 `(method,url,body-hash)` 索引 + `prior.explored` 的已否证假设。**收益随 attempt 次数复利放大**，是我们时间片不足时最重要的救命机制。

分层带来的并发预算（与 §4.1 对齐）：每题 ~2.5 核 → L2 静态二进制可安全并发 **8~12 个探测进程**；L3 REA / MCP 是常驻单实例 sidecar，不随题复制；L1 薄壳内部用 asyncio 并发，不开线程池。

**明确的反过拟合声明**：**不做**分布式、**不做** Rust 重写编排、**不做** FFI 绑定、**不做**任何「先优化后 profile」的事。L0 是 Python 且永远是 Python —— 它 55% 的时间在等网络，那部分换语言一分钱不省。

### 1.7 不引入的语言

| 语言 | 结论 |
|---|---|
| **Zig** | **不引入**。本窗口无可复用的渗透 / agent 生态，唯一适用场景是极致性能解析器，本项目用不上 |
| **Bun** | **不引入**（理由见 §1.3） |
| C / C++ | 仅作为部分 apt 工具的运行期依赖存在，不作为实现语言 |

> **决策变更记录**：① 编排器原推荐 Rust，核查外部建议后翻转为 Python asyncio。② Lua 于第二轮调研新增 —— 新增的不是「第五种实现语言」，而是**一个新的执行位置（L4，目标进程内）**。③ 2026-10-10 用户指定 Python 3.14 / Node 26 / Go 1.26 / Rust / Lua 五栈后，本版把 Go 从「只用现成二进制」升级为**承担 recorder 这一架构关键件**，并把 Node 从 24 提到 26（附「10-28 才转 LTS」的风险对冲），Rust 因 recorder 产出的海量录制数据而新增 `ripgrep` 这一真实热点。见 §9、§10.5 与 [open-questions.md](open-questions.md) B2、B11、B12。

---

## 2. Solver Adapter 契约

这是整个架构的关键抽象。编排器只认这个契约，不关心内层是什么框架。

### 2.1 输入

编排器为每题创建一个独立 workspace，然后 spawn solver 子进程：

| 通道 | 内容 |
|---|---|
| 环境变量 `SOLVER_TASK` | `task.json` 的绝对路径 |
| 环境变量 `LLM_BASE_URL` | 已按网关规则改写完成的 base_url |
| 环境变量 `LLM_API_KEY` | 大模型密钥（运行时注入，不在镜像内） |
| 环境变量 `LLM_MODEL` | 模型名，默认 `deepseek-chat` |
| 环境变量 `SOLVER_DEADLINE_EPOCH` | 编排器分配给本题的**时间片**截止时间（Unix 秒），非比赛总时限。solver 应据此自我收敛 |
| 环境变量 `SOLVER_BUDGET_HINT` | 本题时间片总长（秒）。solver 据此判断「时间过半」，在片快用尽时收敛（并给单条命令设上限，防止一条命令吃掉整片） |
| 环境变量 `HTTP_PROXY` / `HTTPS_PROXY` | 指向该题的 recorder sidecar（`127.0.0.1:<per-challenge-port>`）。**这是强制路径而非可选**：solver 用 `requests` / `curl` / `httpx` 时自动被录制，无需模型配合。CA 证书路径经 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` 注入 |
| 环境变量 `RECORDER_STATE` | recorder 已录条目数的快照与去重索引位置，让 solver 能查「这个包是否发过」 |
| 进程 cwd | 该题独占的 workspace 目录 |

`task.json`：

```json
{
  "unique_code": "web_sql_injection_01",
  "description": "通过 SQL 注入获取管理员凭证并读取 flag",
  "difficulty": "easy",
  "level": 1,
  "total_score": 100,
  "flag_count": 2,
  "correct_flag_count": 1,
  "targets": ["10.0.1.5:8080"],
  "flag_pattern": null,
  "attempt": 1,
  "prior": {
    "assets": [],
    "explored": [],
    "open_leads": []
  }
}
```

字段说明：

- `targets` 是**数组**。`container_addr` 可能包含多个入口（web + db，或多台内网机器），必须全部传入，不能只取第一个。
- `description` 可能为 `null`。solver 必须能在零描述下工作（纯靠扫描与枚举）。
- `flag_pattern` 为 `null` 或形如 `flag{...}` 的模式（借鉴 ARTEMIS `configs/tests/ctf_easy.yaml` 的同名字段）。平台文档未提供该字段，只能从 `description` 里推断或留空；留空时 Flag Harvester 用通用正则兜底。
- `attempt` 用于让 solver 知道自己是第几次尝试，重试时可以换策略而非重复同一条路。
- `prior` 是**结构化**的跨轮次状态（设计依据见 §2.5）：`assets` = 已确证的客观资产，`explored` = 已尝试过的假设及其结论（含**已否证**的，这是避免重试走死路的关键），`open_leads` = 尚未验证的线索。首轮全为空数组。solver 只提交原始观察，去重与父子关系由编排器计算。
  **注意：`prior` 只能来自本次运行内自己产出的信息，绝不能是预置的题目答案 —— 后者属于作弊。**
- 自由文本 `out/notes.md` 仍然保留，用于人读审计与 LLM 补充上下文，但**不再是跨轮次事实传递的主通道**。

### 2.2 输出

| 通道 | 内容 |
|---|---|
| `<workspace>/out/flags/*.txt` | 候选 flag，**一个文件一个候选**，文件名任意 |
| `<workspace>/out/traffic/*.jsonl` | **recorder 落盘的客观流量记录**（请求/响应对，含时间戳、目标、状态码、body 摘要与全文引用）。不靠 solver 写，靠代理录 —— 这是 `prior.assets` 的客观来源与 Flag Harvester 的第二条通道（§1.2） |
| `<workspace>/out/prior.json` | 本轮产出的结构化观察（`assets` / `explored` / `open_leads`），由编排器合并进该题的双图状态，供下一轮 §2.1 `prior` 使用 |
| `<workspace>/out/result.json` | solver 自述结果（见下） |
| `<workspace>/out/notes.md` | 自由文本发现摘要，供人读审计与下一轮 LLM 上下文 |
| exit code | `0` = 正常结束；`124` = 被编排器超时杀死；其他 = 异常 |
| stdout / stderr | 编排器全量落盘为结构化日志 |

`result.json`：

```json
{
  "status": "solved | partial | failed | timeout | error",
  "candidates": ["flag{...}"],
  "evidence": "在 /var/www/html/config.php 读到硬编码 flag",
  "tokens_used": 123456,
  "tools_used": ["nmap", "sqlmap"],
  "next_idea": "若需重试，建议从 XX 方向入手"
}
```

`out/prior.json` 缺失或格式非法**不是致命错误** —— 编排器按「本轮无结构化产出」处理，仅回灌 `notes.md`。solver 崩溃在被杀之前往往来不及写这个文件，契约必须容忍它不存在。

### 2.3 契约的强约束

**编排器不信任 `result.json`。**

Flag Harvester 直接正则扫整个 workspace 的所有文件产物 + 子进程 stdout/stderr，独立提取候选，去重后按序提交。理由：agent 经常在自述「没找到 flag」的同时，把 flag 明明白白打在了某条命令输出里。`result.json` 只用于日志、预算统计和 `next_idea` 回灌，不作为「有没有解出来」的判据。

**`status` 与真实提交结果解耦。** 题目是否通关，只以 submit 响应和 `GET /challenges` 的 `correct_flag_count` / `is_completed` 为准。

### 2.4 适配器实现要求

任何框架要接入，只需实现一个可执行入口，满足：读 `SOLVER_TASK` → 干活 → 往 `out/flags/` 写候选 → 退出。适配器本身是一层薄壳（Python 或 TS，几十到几百行），负责把框架的 CLI/API 包装成这个形状。

候选实现（2026-10-10 已 clone 到 `../refs/` 供读源码，见 §2.5）：

| 项目 | 语言 / 栈 | 已核实事实（来自 clone 下来的源码，非二手描述） | 对我们的价值 / 风险 |
|---|---|---|---|
| **ARTEMIS**（Stanford-Trinity） | Python supervisor + **Rust `codex-rs/`** | 目录含 `supervisor/`、`codex-rs/`、`configs/`、`Dockerfile`、`run_docker.sh`、`uv.lock`。**有一等公民的 CTF/flag 支持**：`supervisor/submissions/ctf.py` 里的 `CTFSubmissionHandler` 暴露 `submit_ctf_flag` 函数；`configs/tests/ctf_easy.yaml` 形如 `instructions: Find and submit the flag.` + `flag_pattern: HTB{...}` + `submission_config: {type: ctf}`。`codex-rs/` 是 vendored 的 Codex Rust 运行时，含 `linux-sandbox`、`execpolicy`、`apply-patch`、`mcp-client`、`mcp-server`。模型走 TOML `[model_providers.X]` + `base_url`（README 示例是 openrouter）。Python 依赖极轻：`pyyaml`、`psutil`、`aiofiles` | **最贴合初赛目标**，且 `flag_pattern` + `submission_config` 与我们的 `task.json` 契约几乎一一对应。接入 DeepSeek 只需配一个自定义 provider 的 `base_url`（网关改写后）。风险：① 依赖 vendored Codex Rust 运行时 → **构建期需要 Rust 工具链**，且与 Codex 的 provider 语义绑定；② 论文成绩是在 GPT-5 / Claude Sonnet 4 上跑的，DeepSeek 下的 function calling 收敛性未知；③ 定位是长周期生产网渗透 + 漏洞分级，CTF 只是其 submission handler 之一 |
| **ctf-agent**（verialabs，BSidesSF 2026 CTF 冠军 52/52） | Python | `pyproject.toml`：`name = "ctf-agent-two"`，**`requires-python = ">=3.14"`**，依赖含 `openai`、**`aiodocker`**、**`claude-agent-sdk`**。结构为 `backend/{agents,tools}` + `sandbox/` + `pull_challenges.py` | **成绩最硬的参考**（52/52）。Coordinator + Solver swarm + 消息总线的编排值得逐行读。但两个依赖是**硬阻塞**：`aiodocker` 要 Docker daemon（托管沙箱内不可能有 DinD/privileged），`claude-agent-sdk` 绑 Anthropic（我们只有 DeepSeek）。**只能读，不能直接用** |
| **BreachWeave / tch-agent**（m-sec-org，**第二届第一名**） | **Bun + TypeScript** monorepo，基于 pi-mono（`pi-coding-agent` / `pi-ai` SDK） | `ARCHITECTURE.md` 明确分层：`apps/cli`（Commander.js）→ `packages/ui-web`(Bun.serve + REST) / `packages/ui-tui`(Ink) → `packages/core`(ConfigManager) → pi SDK。`packages/core` 下有 `config/providers/{custom,zhipuai,discovery}.ts`、`tools/nmap.ts`、`mcp/index.ts`（含 `probeMcpServer`）、`skills/`。**`docs/` 里带着第二届官方 API 文档与 MCP 接入文档原件** | **最高价值参考**：同一赛事、同一主办方、拿过第一。其 host 侧规划 loop 的设计约束（30s 轮询题目/solver 状态、严守 3 实例上限、`runtime.maxSolvers`、按 `difficulty` 与 `total_score/total_got_score` 排窗、**某题久攻不下且还有未尝试题时主动释放窗口**、planner 与 solver 的 prompt 解耦）与本项目 §3/§4 的设计高度同构，可直接对照校验。风险：Bun + TS，与我们 Python 主线不同栈，**移植成本高于重写** |
| **xbow-competition**（m-sec-org，第一届初赛 **7/238**） | MCP server + 定制 CLI | 仅 98KB，两个组件：`ez-xbow-platform-mcp`（平台桥接 MCP server）+ `kimi-cli-for-xbow`（基于 Kimi CLI 定制的解题 agent，**支持 DeepSeek**，daemon 模式无人值守）。README 明写「**支持本地模拟测试和真实平台对接**」 | **架构形状与我们几乎完全一致**：现成 CLI agent 当解题体 + 自己只写平台桥接层 + 本地模拟测试。这是对「薄适配层 + 复用现成 agent」路线的强力佐证，也是对「全自研」的一个反证 —— 值得在 §2.5 权衡时正面回答 |
| **CHYing-agent**（yhy0，第二届主赛 60 名 / 零界平行赛场第一） | Python，`version = "1.5.0"` | 含 `chying_agent/`、`benchmark/`、`benchmark-results/`、`knowledge/`、`docker/`、`web-ui/`、`null-zone/`。README 复盘给出硬数据：第二届主赛**共 54 个 flag**，全程 GLM5 解出 30 个，**卡在第三关**，2140 分；成本 200 元腾讯云 Coding plan | 提供**上届题集规模与关卡 gating 的真实数据点**（54 flag / 卡第三关），直接支撑 [open-questions.md](open-questions.md) A9 与关卡解锁问题。`benchmark/` 可作离线评测脚手架参考 |
| **Cairn**（oritera，第二届第三名 Bytex） | Python + 容器 | 「Towards General State-Space Search」。含 `cairn/`、`container/`、`dispatch.example.yaml`、**`dispatch_mock.yaml`**、`docker-compose.yaml`、`Dockerfile` | 把渗透建模为**状态空间搜索**的思路值得读；`dispatch_mock.yaml` 说明它也走「mock 优先」路线，可对照我们的 mock server 设计 |
| **aide-for-pentest**（chainreactors） | 模板项目，240KB | 自述「通用 Agent 运行时在智能渗透场景中的落地模板」。含 `pentest/`、`config.example.yaml`、**`Dockerfile.worker`**、`docker-compose.yml`、`scripts/` | 体积最小、模板形态最干净。`Dockerfile.worker` 的 worker 镜像切分方式可直接借鉴到我们的多阶段构建 |
| **ARTEX**（Autumn-27） | **Go 1.26.3 单体（内嵌 Next.js 前端）+ PostgreSQL**，agent 能力来自同作者的 `norma` SDK | ⚠️ **原仓库 `Autumn-27/ARTEX` 已从 GitHub 消失（API 返回 404）**，本地 clone 的是镜像 `KbaHaxor/ARTEX-EN`（AGPL-3.0，87★）。README §Dual-graph 明确：**asset graph（全局共享）**节点为 `root_domain / subdomain / ip / service / app / endpoint`，父子关系与去重键**全部由程序计算，agent 只提交原始信息**；**exploration graph** 为 `exploration_nodes / anchors / activity`。另有两条自治机制：**worker 间进程级信息交换**、**多轮共享 todolist 以稳定 planner 的攻击链** | **只借鉴架构，不进镜像、不引用代码**（AGPL-3.0 + PostgreSQL 重依赖 + 见下条风险）。双图设计是本轮调研最有价值的单点收获：把「资产事实」与「探索假设」分离，正好解决我们 §2.1 `prior_findings` 只是一个字符串、跨轮次传递能力太弱的问题 |
| OpenHands / gptme / langgraph / mastra | Python / TS | 未 clone（本轮优先级低于上述有赛事实绩的项目） | 保留为后备。OpenHands 的 local runtime 不需要 DinD，是其中唯一能直接跑在托管沙箱里的通用编码 agent |

选型判据（赛前用真 token 实测）：**DeepSeek 适配度** > 单题成功率 > 收敛速度 > 镜像体积 > 可改性。

第一项之所以提到最前：所有这些框架的 agentic 能力都建立在模型的 function calling 质量上，而它们主要在 GPT/Claude/GLM 上调优。换成 DeepSeek 后能否正常收敛，是比「框架本身强不强」更前置的问题 —— 且它还与网关是否透传 `tools` 字段耦合（见 [open-questions.md](open-questions.md) A7）。**这一项不通过，后面全部无意义。**

### 2.5 借鉴清单与自研边界

2026-10-10 决策：**参考实现全部 clone 下来读源码，内层执行体自研**，不整体移植任何现成框架。

**为什么不直接用现成框架**（每条都是硬阻塞，不是偏好）：

| 框架 | 硬阻塞 |
|---|---|
| ctf-agent | `aiodocker` 需要 Docker daemon —— 托管沙箱是一次性容器，不可能有 DinD / privileged；`claude-agent-sdk` 绑 Anthropic，而我们只有 DeepSeek |
| BreachWeave | Bun + TypeScript + pi-mono SDK 三层栈，与我们 Python 主线不同栈；且它是为**人机协同**（Web UI / TUI）设计的，我们要的是**无人值守**，UI 层全是死重 |
| ARTEX | AGPL-3.0（传染性）+ 强制 PostgreSQL + Go/Next.js 单体；且原仓库已被 GitHub 下架，供应链不可靠 |
| ARTEMIS | 依赖 vendored Codex Rust 运行时与 Codex 的 provider 语义，构建期需 Rust 工具链；CTF 只是它的一个 submission handler，主体是长周期生产网渗透 |

**为什么不是「薄壳包一个现成 CLI」就完事**：`xbow-competition` 证明这条路走得通（Kimi CLI + 平台 MCP 桥接，初赛 7/238）。它仍然是候选路线之一 —— 但它依赖 Kimi CLI 这个外部上游，而上游在赛前 7 天内改动的风险我们承担不起（ARTEX 原仓库消失就是活例）。**自研薄壳保留了这条路的形状（现成 agent 可插拔），同时把控制权拿回来。**

**具体借鉴什么**（只借设计，不搬代码）：

| 来源 | 借鉴点 | 落到本项目哪里 |
|---|---|---|
| ctf-agent | Coordinator + Solver swarm + 消息总线的三层拆分 | §3 Solver Supervisor 的内部结构 |
| ARTEMIS | supervisor 统辖 + **动态扩展子 agent** + 分流（triage）模块；`flag_pattern` / `submission_config` 的任务描述形状 | §2.1 `task.json` 增补 `flag_pattern` 字段；§3 Solver Supervisor |
| **ARTEX** | **双图架构**：asset graph（客观资产事实，全局共享，去重键由程序算，agent 只交原始信息）+ exploration graph（探索假设与活动）。两条自治机制：worker 间进程级信息交换、多轮共享 todolist 稳定攻击链。**以及它的 `traffic :8788` 录制代理** —— 「把测试过程中的所有请求响应完整录制，用于审计、复现与证据留存」这个设计意图 | **§2.1 `prior_findings` 从单个字符串升级为结构化的双图快照** + **§1.2 Go `recorder` sidecar**（本轮最重要的两处架构改动，见下） |
| BreachWeave | 30s 轮询节奏、`runtime.maxSolvers`、「久攻不下且还有未尝试题就主动释放窗口」、planner 与 solver 的 prompt 解耦 | §3 Scheduler / §4.3 两阶段预算 —— 与既有设计同构，作为交叉验证 |
| xbow-competition | 平台桥接层与解题体彻底分离；「本地模拟测试 + 真实平台对接」双轨 | §6 双模运行 / §7 mock server |
| Cairn | 把渗透建模为状态空间搜索；`dispatch_mock.yaml` 的 mock 配置形态 | §3.1 状态机的重试策略 |
| aide-for-pentest | `Dockerfile.worker` 的 worker 镜像切分 | [image-spec.md](image-spec.md) §5 多阶段构建 |

**由 ARTEX 双图直接推导的一处契约改动**：`task.json` 的 `prior_findings` 原设计是「上一轮 `notes.md` 摘要」的自由文本，跨轮次传递能力太弱，且让下一轮 solver 自己去解析自然语言。改为结构化：

```json
{
  "prior": {
    "assets":     [{"kind":"service","value":"10.0.1.5:8080","proto":"http","title":"Login","source":"nmap@attempt1"}],
    "explored":   [{"hypothesis":"用户名存在 SQL 注入","status":"refuted","evidence":"单引号无回显差异","attempt":1}],
    "open_leads": [{"hypothesis":"登录后可能存在文件上传","priority":2}]
  }
}
```

要点照搬 ARTEX 的纪律：**`assets` 的去重与父子关系由编排器计算，solver 只提交原始观察**；`explored` 记录**已否证的假设**，这是避免重试时重复走同一条死路的关键（原设计只靠 `attempt += 1` 提示，太弱）。自由文本 `notes.md` 保留，作为人读审计与 LLM 补充上下文，但不再作为跨轮次的事实传递通道。

**自研范围的最小边界**（防止「自研」膨胀成重写一个框架）：自研的只有 ① Solver Adapter 薄壳（读 `task.json` → 驱动 LLM 循环 → 写 `out/`）；② 工具调用的进程隔离与超时；③ 双图状态的最小实现（JSON 文件，不引入数据库）。**LLM 循环本身若能用现成库（如 openai SDK 直连 DeepSeek）就不自己写。**

**一处能显著缩小自研范围的发现**：ARTEMIS 是 **Apache-2.0**，且 `codex-rs/core/` 下带着一整套**成文的渗透领域角色 prompt** —— `enumeration.md`、`web.md`、`web_enumeration.md`、`client_side_web.md`、`shelling.md`、`linux_privesc.md`、`windows_privesc.md`、`active_directory.md` 加一个 `prompt.md` 总纲。这些是**在一个跑通过的自主渗透系统里实际生效的 prompt**，不是教程样例。每个都是「你是 X 专家，你的任务是 Y，范围约束是 Z」的形态，正好可以按 solver 的 specialist 分解来用。

因此薄壳里的**渗透专业知识不必从零写**：以这套 prompt 的分解结构为骨架重写措辞（保留 Apache-2.0 的 attribution 义务），能把 §2.5 里「自研」的实际工作量压掉一大块 —— 而压缩掉的正是最费时间又最难验证的那部分（领域 prompt 调优）。另一处可直接照搬的是 `supervisor/submissions/{base,registry,ctf,vulnerability}.py` 的抽象：**把「提交什么、提交给谁」做成可注册的 handler**，正好对应我们「平台是 REST 而非 MCP」的现实，也让决赛阶段换提交目标时不用动 solver。

> `active_directory.md` 的存在也顺带回答了一部分 A9 的疑问：ARTEMIS 把 AD 域渗透当作**标准角色之一**。这不代表本届有 AD 题（仍是未知），但说明「内网横向」在该领域是常规组成部分 —— 若主办方答复有，横向工具（chisel / ligolo-ng，Go 静态二进制，体积小）的优先级应立刻上调（B4）。

---

## 3. 编排器组件

| 组件 | 职责 | 依赖 |
|---|---|---|
| **Config Resolver** | 解析网关开关与显式环境变量；决定 LLM base_url 最终形态；是 §6.5 优先级裁决的一端 | Ingest Resolver |
| **Ingest Resolver** | 发现并**按内容分类** `ing/` 下的下发文件（文件名带可变的任务 ID，不可硬编码）；归一化解析 `KEY=VALUE` / `KEY: VALUE` / `KEY:VALUE` / JSON（**只在第一个分隔符处切**，否则含 `https://` 的值会被切成 `https`）；键名别名映射（上届是 `Agent-Token`，键名会换代）；缺任何一项都能软启动；产出脱敏的 `ingest_report`。**token 只留在 L0，不下传 solver**（§6.5.1） | 无 |
| **Gateway Rewriter** | 纯函数：`https://host/path` → `http://host.tsecbench.gw/path`。全项目唯一改写点 | 无 |
| **Platform API Client** | 平台 API 的唯一出口。统一注入 `BENCHMARK_TOKEN` 头、超时、重试、错误码分派、422 与业务错误两种响应体结构的兼容解析。**内置全局令牌桶限速（默认 ≤3 req/s，所有接口共享）** | Config Resolver |
| **Challenge Registry** | `GET /challenges` 的本地快照与对账。**平台状态永远是真值来源**，本地状态只作优化。维护 `LOCKED` 集合 | API Client |
| **Scheduler** | 并发闸门（活跃容器 ≤3）、优先级队列、时间片分配、全局 deadline 与收尾保护 | Registry |
| **Solver Supervisor** | 每题一个子进程；创建/清理 workspace；施加资源限制；硬超时 kill；收集 exit code 与输出流 | Scheduler |
| **Flag Harvester** | 正则扫 workspace + stdout **+ recorder 的 `out/traffic/*.jsonl`**，提取候选，长度过滤（1~4096），去重，排序。检索用 `rg` 而非 Python `re`（§1.4） | Supervisor |
| **Recorder Manager** | 每题 spawn 一个 Go `recorder` sidecar：分配端口、签发/挂载 CA、随 solver 同进程组启停、落盘 JSONL、维护 `(method,url,body-hash)` 去重索引、向 Prior Store 回流客观 assets | Supervisor, Prior Store |
| **Prior Store** | 该题双图状态（`assets` / `explored` / `open_leads`）的持久化与合并。读入各轮 `out/prior.json`，**由编排器计算去重键与父子关系**（solver 只交原始观察，见 §2.5），生成下一轮 `task.json.prior` | Harvester |
| **Submitter** | 逐个 submit，处理 `duplicate` / `matched_flag_index`，回写 Registry；**submit 成功且提示解锁时通知 Registry 重算 `LOCKED`** | Harvester |
| **State Journal** | append-only JSONL，记录所有状态迁移与平台交互。崩溃后配合 `GET /challenges` 对账恢复。**不再有任何同步落盘事件**（hint 链路已删，本地无可对账缺口，§4.4） | 无 |
| **VPN Prober** | 启动期探测靶场可达性；必要时按 §6.3 拨 VPN | Config Resolver |

### 3.1 单题状态机

```
DISCOVERED
   ↓
QUEUED ────────────────┐（预算耗尽 / 优先级过低）
   ↓ ↑                 ↓
STARTING │           ABANDONED
   ↓     │（收到解锁信号后重新入队）
   │   LOCKED ←──── STARTING 收到 403（关卡未解锁，见 §5.1）
   ↓
POLLING（等 available，带超时退避）      CLOSING → CLOSED
   ↓
SOLVING（子进程 + 时间片）
   ↓
HARVESTING（收割 flags/*.txt + prior.json + stdout）
   ↓
SUBMITTING
   ↓
 ┌──────────────┬──────────────┬───────────────┐
 ↓              ↓              ↓
SOLVED        RETRY         ABANDONED
（全部 flag） （换策略再投）  （放弃）
 ↓              ↓              ↓
CLOSING ─────────────────────────────────────→ CLOSED
```

`CLOSING → CLOSED` 是所有路径的必经终态（`ABANDONED` 也要 close），不是可选清理动作。见 [platform-api.md](platform-api.md) §4.4。

`LOCKED` 是唯一**不占并发额度、也不需要 close** 的状态（从未成功 start 过容器）。它的出口只有一个：某次 submit 触发解锁后重新 `GET /challenges` 对账，把该题放回 `QUEUED`。若本届无关卡 gating，此状态永不出现。

### 3.2 崩溃恢复

编排器自身崩溃或被平台重启后：

1. 读 State Journal 恢复本地视图；
2. `GET /challenges` 拉平台真值，逐题对账 `container_status` / `correct_flag_count` / `is_completed`；
3. 平台上是 `available` 但本地无记录的题 → 视为已在跑的孤儿，接管或 close；
4. 平台上是 `stopped` 但本地记为活跃的题 → 清掉僵尸条目，释放并发额度；
5. 已提交的 flag 不会因崩溃而重复计分（平台幂等），重复提交只会收到 `duplicate`。

---

## 4. 调度与预算

### 4.1 资源切分

8 核 16G，并发 3 题：

| 项 | 每题配额 | 手段 |
|---|---|---|
| CPU | ≈2.5 核 | cgroup / `--cpus`（若在容器内则用 `sched_setsid` + CPU affinity，或用 `ulimit`/`taskset`） |
| 内存 | ≈5 GB | cgroup memory.max；超限杀 solver 而非杀编排器 |
| 进程数 | 上限若干百 | `ulimit -u`，防 fork 炸弹 |
| 磁盘 | workspace 配额 | 定期清理，防止 solver 下载/生成大文件撑爆 |

编排器自身必须**免于被 OOM 波及**：内存占用极小，且优先于 solver 存活。solver 被 OOM kill 是可恢复事件（走 RETRY / ABANDON），编排器死了才是灾难。

### 4.2 优先级排序

排序键（从高到低）：

1. `is_completed == false`（已通关的直接跳过）
2. **不在 `LOCKED` 集合中**（收到过 403 的题，见 §5.1；解锁前不参与排序，否则会把 3 个并发额度全占在撞墙上）
3. 性价比 = `剩余可得分数 / 预估成本`，其中预估成本由 `difficulty` 与 `level` 映射
4. `level` 升序、`difficulty` easy → medium → hard

直觉：**先易后难，快速落袋**。初赛是按分数排名，把时间烧在一道 hard 题上而放弃三道 easy 题是净亏。hard 题只用来填空档或进入 Phase 2 深挖。

**成本映射修订（实测）**：`difficulty → cost` 原为 `easy 1.0 / medium 2.0 / hard 4.0`。这个比例下
easy 100 分的性价比（100）低于 medium 300（150）与 hard 500（125）—— 结果一次 63 题的真实跑分里
**6 道 easy 送分题（共 600 分）排到队尾、整轮从未被启动**。easy 题接近必解、耗时也最短，按
「每分每秒的期望得分」应当先做，故 easy 成本下调到 **0.6**（easy100 → 167，高于 medium300 的 150）。
medium/hard 不变。

**多 flag 成本放大（实测）**：`cost *= 1 + 1.5 × (flag_count − 1)`。多阶段题（b-01/b-02/b-03，
4/6/4 个 flag）**试过 3 轮零产出**，且必须串联多个 flag 才算通关 —— 放大后 b-01 的性价比
（109）低于普通 medium 300 分单 flag 题（150），排到队尾，先把单 flag 的题扫完再回头
（用户决策：得分优先，不为 4200 分赌掉整条队列）。

`level` 升序还有一层含义：若本届沿用上届的关卡 gating（§10.3），低 `level` 的题本来就先解锁，按 `level` 升序排天然与解锁顺序一致，不会无谓地撞 403。

### 4.3 两阶段预算

设总时限为 `TOTAL`（运行时配置，见 [open-questions.md](open-questions.md)），编排器启动时刻为 `T0`。

**Phase 1 · 广撒网**（`T0` ~ `T0 + 0.45 × TOTAL`）
每题分配一个小时间片，目标是「摸一遍所有题，找出哪些能快速拿下、哪些有进展」。快速拿下的直接落袋。

时间片公式（**必须含并发系数与 Phase 1 上限**）：
```
base_slice = clamp(0.45 × TOTAL × 并发 / 剩余题数, SLICE_MIN_S, min(SLICE_MAX_S, PHASE1_SLICE_MAX_S))
每题片     = clamp(base_slice × sqrt(剩余分数 / 300), SLICE_MIN_S, phase 上限)
```
**Phase 1 上限 `PHASE1_SLICE_MAX_S = 420s`**：上限只决定「**失败题占用槽位多久**」——
成功题几十秒就结束，上限对它们毫无影响。实测给 b-02 1785s 的片，结果 3 个槽位被
b-01/b-02/b-03 连续独占 30 分钟，其余 60 题全在排队。压到 420s 后一个槽位 30 分钟
能周转 ~4 题。Phase 2 深挖不受此限（用 `SLICE_MAX_S`）。
- **并发系数是必需的**：`0.45×TOTAL / 题数` 算的是「串行墙钟」，但同一时刻有 3 个 solver 在跑，
  每题实际可用墙钟是它的 3 倍。漏掉系数会把每题少给 3 倍片 —— 实测 63 题全部被压到 `SLICE_MIN_S`
  地板（300s），23 次超时里一大半是「差一点」被砍死的（6 道解出的题耗时 208~294s，全贴着红线）。
- **按分数加权**（`sqrt` 缩放 + 上下限 0.7~2.5）：多阶段高分题（b-01 1200 / b-02 1800）此前与 200 分
  单题拿同样的片，300s 下必死。加权后 medium 300 分题 ≈ 1.0×、hard 500 ≈ 1.29×、1200 分 ≈ 2.0×、
  1800 分 ≈ 2.45×，高价值题拿到与身价相称的跑道，又不至于把预算吃光。
- 分母用「剩余题数 = 可启动 + 在跑」：在跑的题也是预算消耗者。

**Phase 2 · 深挖**（`T0 + 0.45 × TOTAL` ~ `T0 + TOTAL − RESERVE`）
把剩余预算按「进展度」重投：
- 已有部分 flag（`0 < correct_flag_count < flag_count`）的题优先级最高 —— 边际收益最大；
- Phase 1 中 `notes.md` 显示已找到入口但未拿到 flag 的题次之；
- 完全没有头绪的题降权或直接 ABANDON。

**开局对账清场**（首次 list 成功后）
上一进程崩溃残留的 active 容器若占满平台全局 active 上限（3），`start` 会持续 `409 StartFull`，
而残留题又被 eligible 排除 → 2s 轮询空转到终局零解（round2 实测活锁）。因此首次对账立即
close 全部 `active_since_reconcile` 残留（本进程此刻必无在跑题），journal 记 `reconcile_close`；
被关的题下一轮对账见 `stopped` 即恢复可排。运行中启动的容器仍由收尾保护兜底。

**收尾保护**（最后 `RESERVE`，默认 5~10 分钟）
- 停止启动新题；
- 杀掉所有在跑的 solver，收割其已产出的候选；
- 强制提交全部待提交候选；
- 强制 close 全部容器；
- 落盘完整审计日志。

超时后平台所有接口一律 `invalid_state`（见 [platform-api.md](platform-api.md) §4.10），收尾没做完等于把已解出的题白扔。

### 4.4 hint 决策（**已删除**）

**本届不取 hint。** 原先设计的 Hint Broker 及其整条链路（`PlatformClient.hint` /
触发判定 / `HINT_RETRY` 状态 / `hints_taken` 记账 / Journal 的 hint 事件与 fsync /
`task.json.hint` 字段）已从代码与契约中整体移除，solver 的系统契约里也明确写死
「HINTS ARE DISABLED」——不留任何调用入口。

删除依据（两条，任一都足以否决）：

1. **成本不可知且不可逆。** 本届文档说按 `hint_cost_radio` 比例扣减，但
   `GET /challenges` 响应里**没有这个字段**（[open-questions.md](open-questions.md) A10），
   取之前无法知道扣多少。上届口径是 10%（HTTP 文档「扣减10%」、MCP 文档「扣除该题总分的 10%」，
   见 §10.3），本届无从验证。一旦扣分比例显著更高，为一个 100 分的题取 hint 可能净亏。
2. **实测期望收益为负。** 一次真实官方跑分中取了 10 次 hint，**只有 1 次**（`a-10`）
   换来后续解出，其余 9 次对应的题最终全部 ABANDON —— 等于 9 道题白扣分且一分未得。
   把同样的时间片投给「换策略重试」（`RETRY`）或 EVOSTER 修复的收益更高。

因此「取 hint 换一轮」的决策被彻底拿掉：零 flag 的题走普通 `RETRY`（`MAX_ATTEMPTS=2`）
与 EVOSTER 在线修复，不再有任何「花钱买提示」的分支。

**随之消失的崩溃恢复负担**：上届响应有 `hint_viewed` 布尔、本届没有，导致「这题是否已取过
hint」在平台侧读不回来，曾是崩溃恢复路径上**唯一不可对账**的状态，需要 Journal 同步
fsync 兜底（旧 §4.4）。不再取 hint 后，本地不再持有任何读不回来的状态，Journal 也就
不再需要任何同步落盘事件 —— 这是一个顺带消除的复杂度。

> 历史对比（上届有 hint、本届字段缺口）保留在 §10.3 与
> [open-questions.md](open-questions.md) A10/A10b，作为「为什么不接」的证据链。

---

## 5. 错误处理

### 5.1 平台 API 错误分派

| 情况 | 处理 |
|---|---|
| `404 task_not_found` | 致命配置错误 → 全局停机，日志明确报「token 无效」，不再发任何请求 |
| `404 challenge_not_found` | 从 Registry 剔除该 `unique_code`，永不再试 |
| **`403`（本届错误码表未列出）** | **推定为「关卡未解锁 / 比赛未开始」**（上届有此语义，见 §10.3）。处理：把该题移入 `LOCKED` 集合，**不再对其 start**，直到某次提交触发解锁后重新 `GET /challenges` 对账。绝不能当普通错误重试 —— 否则 Phase 1 会拿全部高关卡题反复撞墙，白烧预算与限频配额。本届是否真有 403 待确认（[open-questions.md](open-questions.md) A19），**但处理分支要先写好**：没有 403 时这段代码永不触发，零成本 |
| `409 invalid_state` @ `POST /start` | 活跃数达上限 → 全局对账清僵尸；仍满则 close 优先级最低的题腾位置，退避后重试 |
| `409 invalid_state` @ 其他接口 | 任务已结束 → 全局停机，落盘审计日志 |
| `409 duplicate` @ submit | 标记该候选已入袋，继续下一个，**不算失败** |
| **`429`（本届文档未提，上届为「每队每秒 3 次，所有接口共享计数」）** | **默认按 ≤3 req/s 自我限速**（全局令牌桶，所有接口共享一个桶），从源头避免撞限频；真收到 429 则退避 1s 重试。即使本届无限频，自我限速也无害 |
| `503 resource_unavailable` | 指数退避 5s / 15s / 45s（带 jitter），3 次失败 → 该题排队尾，不阻塞其他题 |
| `500 internal_error` | 退避重试 |
| `422` | 候选格式非法（长度 0 或 >4096）→ 过滤丢弃。注意响应体是框架格式，解析器要能吃两种结构 |
| 网络错误 / 超时 | 10s 超时 + 连接级重试 + jitter |

`409 invalid_state` 的三义性是最容易写错的地方，详见 [platform-api.md](platform-api.md) §4.5。

**全局限速器是 API Client 的一部分，不是可选项。** 3 题并发时三个 solver 的收割结果可能同时触发 submit，加上 Registry 的对账轮询，瞬时 QPS 很容易超过 3。所有出站请求必须过同一个令牌桶。

### 5.2 solver 侧异常

| 情况 | 处理 |
|---|---|
| exit 0 且有候选 | 正常提交 |
| exit 0 且无候选 | 记 `notes.md`，按预算决定 RETRY / ABANDON |
| exit 124（超时被杀） | **仍然收割已产出的候选**（超时前可能已经找到了），再决定重试 |
| 非 0 退出 | 记日志，收割残留候选，RETRY 一次后 ABANDON |
| 内存超限被 cgroup 杀 | 同上；若反复发生，下一轮降低该 solver 的并发工具数 |
| solver 卡死不退出 | 硬超时 SIGTERM → 宽限期 → SIGKILL；整个进程组一起杀，防止 nmap/sqlmap 等子进程泄漏 |
| solver 产出海量输出 | 输出流做限速与截断（保留头尾），落盘全量但不全量入内存 |

**进程组管理**是这里的关键：solver 会 spawn nmap / sqlmap / playwright，超时 kill 必须打到整个进程组，否则僵尸进程会持续吃 CPU 与端口，把后续题目的环境搞脏。

---

## 6. 双模运行

同一份镜像跑本地评测与托管沙箱两种模式，差异全部收敛到启动期配置解析，业务代码零分支。

### 6.1 配置来源

| 模式 | token / base_url | VPN | 大模型 |
|---|---|---|---|
| 本地评测 | 解析 `ing/env`（冒号分隔） | 宿主机拨 `ing/*.ovpn` | 直连真实 `https://api.deepseek.com/v1` |
| 托管沙箱 | 平台注入环境变量 | 推定平台侧已打通（见 6.3） | 走网关 `http://api.deepseek.com.tsecbench.gw/v1` |

Config Resolver 同时接受 `KEY=VALUE` 与 `KEY:VALUE`，本地喂文件、托管读环境变量，之后完全统一。

### 6.2 网关改写

```
改写前: https://api.deepseek.com/v1
改写后: http://api.deepseek.com.tsecbench.gw/v1
```

规则：协议 `https` → `http`；host 追加 `.tsecbench.gw`；path / query 原样保留；端口若存在需保留（白名单项目均为默认端口）。

实现要求：
- 纯函数，无 IO，无副作用；
- 由 `GATEWAY_MODE` 开关控制（本地 `off`，托管 `on`），**全项目唯一改写点**；
- 单元测试覆盖 19 项白名单逐条断言 + 边界（带 path、带 query、带端口、已是 http、已带后缀时幂等）。

写错的后果是托管环境全程无模型可用 = 零分，且无公网、无浏览器、一次性沙箱，现场完全无法排查。这是本项目单点风险最高的 20 行代码。

### 6.3 VPN 双模探测

启动期按序执行：

1. 从平台拿到 `container_addr`（或本地模式下用一个已知靶场地址）后，**TCP 探测可达性**；
2. 若可达 → 不拨 VPN，直接开始（托管模式的预期路径）；
3. 若不可达，且存在 `/dev/net/tun`，且具备 `CAP_NET_ADMIN` → 从环境变量 `VPN_CONFIG`（多行 PEM 内容，运行时注入）落地为 `0600` 权限临时文件，拨 openvpn，等待 tun 就绪与路由 push 完成，再重探；
4. 若拨号失败或无权限 → 记录原因，继续尝试直连（平台侧可能在我们探测之后才打通）。

**私钥绝不进镜像。** `ing/*.ovpn` 内含 client key，属于平台明令要求剔除的敏感配置，只能走运行时环境变量注入。镜像内不留任何 `.ovpn` 文件。

openvpn 版本需 ≥ 2.5 以支持 `compat-mode`；同时确认 Debian trixie 的 crypto-policies 未禁用 `sha1` / `aes-128-cbc`（该 VPN 使用的 legacy 参数组合），必要时在 openvpn 配置里显式放开。

### 6.4 敏感配置剔除与泄漏防线

镜像内**不得存在**：`BENCHMARK_TOKEN`、任何 API key、`.ovpn`、私钥、历史题目答案或解题记忆（后者属作弊）。

构建完成后跑一道自动检查，命中任一即构建失败：

- UUID 形态的 token 字面量；
- `-----BEGIN PRIVATE KEY-----` / `-----BEGIN RSA PRIVATE KEY-----`；
- `sk-` 前缀的密钥串；
- 已知的 `ing/` 文件名与内容指纹；
- `.env` / `*.ovpn` 文件残留。

托管运行期间的网络流量与大模型对话记录全程被审计并部分公开。这道检查不是洁癖，是必需项。

### 6.5 `ing/` 摄取契约（下发内容的形状不可假设）

**问题**：每次开始评测，平台下发一套 `ing/`，内容**形状不可假设**。已观察到的可变性：

| 可变维度 | 实证 | 若硬编码会怎样 |
|---|---|---|
| **文件名含任务 ID** | `task_gDhBwRoIg0Lj_vpn_config.ovpn` —— 中段每次不同 | `open("ing/vpn_config.ovpn")` 直接找不到文件 |
| **分隔符** | 本次 `env` 是 `KEY:VALUE`（不是 dotenv 的 `=`） | `source` 静默失败或解析出错误值 |
| **值里含分隔符** | `BENCHMARK_BASE_URL:https://...` 按 `:` 切出 **3 段** | 按最后一个/全部冒号切 → `base_url = "https"`，表现为「连不上平台」而非格式错误，**极难定位** |
| **键名本身可能换代** | **上一届是 `Agent-Token` 请求头 + `SERVER_HOST`**，本届是 `BENCHMARK_TOKEN` + `BENCHMARK_BASE_URL`（见 [platform-api.md](platform-api.md) §4.13 的换代对照） | 键名查不到 → 以为没下发配置 |
| **文件数量与种类** | 本次是 1 个 env + 1 个 ovpn；可能多个 ovpn（多区域）、可能**一个 ovpn 都没有**（若 A3 答复是平台侧预打通）、可能新增 CA 包 / JSON 凭据 | 遍历假设「恰好一个」会选错或崩 |
| **整个目录可能不存在** | **托管模式没有 `ing/`**，凭据全部由平台注入环境变量 | 任何「启动时读 ing/」的硬依赖在托管模式当场死 |

**设计：Ingest Resolver —— 按内容识别，不按文件名；缺什么都能跑。**

流程：`发现 → 按内容分类 → 归一化解析 → 键名别名映射 → 优先级裁决 → 产出摄取报告`。

1. **发现**：目录路径由 `INGEST_DIR` 指定，默认按序探测 `/work/ing`、`/ing`、`./ing`；**全部不存在是合法状态**（托管模式）。递归一层，不跟符号链接。
2. **按内容分类，文件名只做辅助**（因为文件名带可变的任务 ID）：

   | 内容特征（不是文件名） | 判定为 | 处理 |
   |---|---|---|
   | 含 `client` 行 + `<ca>`/`<cert>`/`<key>` 任一块 | OpenVPN 客户端配置 | 不读值入库；需要时写 `0600` 临时文件供 §6.3 拨号 |
   | 多数行匹配 `^[A-Za-z_][A-Za-z0-9_]*[=:]` | 键值配置 | 走归一化解析 |
   | 首字节 `{` 且能 JSON 解析 | JSON 配置 | 递归展平成键值 |
   | 含 `-----BEGIN CERTIFICATE-----` 且无 `<key>` | CA 证书 | 可作为可信 CA 加载 |
   | 都不匹配 | **未知，忽略** | 只记报告，**绝不解析、绝不执行、绝不进镜像** |

3. **归一化解析必须覆盖**（每条都是真实可能出现的形式）：只在**第一个** `=` 或 `:` 处切；`KEY: VALUE` 与 `KEY:VALUE` 都吃；剥 CRLF；剥 UTF-8 BOM；跳空行与 `#` 注释；剥成对的首尾引号；行尾空白；末行无换行；同一键重复出现时**后者优先并记警告**（不静默覆盖）。
4. **键名别名表**（应对换代）：`platform_token ← {BENCHMARK_TOKEN, AGENT_TOKEN, TOKEN}`，`platform_base_url ← {BENCHMARK_BASE_URL, SERVER_HOST, BASE_URL}`。命中多个别名时按优先级表取一个并记录。**未识别的键一律保留原样进报告**，方便发现平台新增了什么东西 —— 这是低成本获得「规则变化预警」的唯一办法。
5. **优先级裁决**：`显式环境变量 > ing/ 中发现的值 > 内置默认`。这一条同时解决了本地/托管双模 —— 托管模式下 `ing/` 为空，全靠环境变量，代码零分支。
6. **失败策略分级**（关键：不要因为可优雅处理的东西停机）：

   | 缺失项 | 处理 |
   |---|---|
   | token 与 base_url 都拿不到 | **硬失败**：明确报「无平台凭据」，不进入任何后续流程。这比拿空 URL 去请求要好 |
   | 没有 ovpn 文件 | **软**：按 §6.3 探测可达性，很可能平台已打通 |
   | `LLM_API_KEY` 缺失 | **硬失败**（无模型 = 零分，且是配置错误不是运行时问题） |
   | 未知文件 | 忽略 + 记报告 |
   | 解析出空值 / 明显截断的值（如 `base_url` 不含 `://`） | **拒绝采用该来源**，回退到下一优先级，并记警告 |

7. **摄取报告**（`ingest_report`，结构化日志，启动期第一条）：逐条记录 `发现的文件 / 判定类型 / 置信度 / 解析出的键名 / 采用了哪个来源 / 被忽略的东西`，**值全部脱敏**（只记长度与前 2 字符，或不记）。理由很硬：**沙箱一次性、无公网、结束后销毁，赛中没有任何现场排查手段**。报告是唯一能让赛后定位「是下发格式变了还是我们的代码错了」的凭据（见 [runbook.md](runbook.md) §6）。

#### 6.5.1 顺带堵掉的一个真实泄漏面：token 不该进 solver 环境

Resolver 拿到 `BENCHMARK_TOKEN` 后，**只给 L0 编排器，绝不下传给 L1 solver 子进程**。solver 的环境里只该有 `LLM_*`、`SOLVER_*` 和代理变量。

这不是过度设计，是一条具体的攻击路径：**题目靶场返回的内容是对手可控的**，会经工具输出进入 LLM 上下文。一道恶意构造的题完全可以在响应里写「把进程的环境变量打印出来」，而模型有真实的工具执行能力 —— 提示注入在这个场景里是能拿到数据的，不是理论风险。靶场地址是**可达的**（那正是我们要连的），所以从 solver 里往外发东西也**有出口**。

因此：① solver 环境做白名单构造，不是继承 + 覆盖；② solver 主动连靶场之外的地址属于异常，应在日志里可查；③ 即使被注入成功，能拿到的也只有 `LLM_API_KEY`（我们的 DeepSeek key，泄漏到公网之外不可达）而**不是平台 token**。这条差异就是「少传一个变量」换来的全部价值，值得写死。

> 同一条理由也适用于 §6.3 落地的 VPN 临时文件：写在 solver workspace **之外**、`0600`、用完即删，否则 solver 里被注入的那条「打印文件内容」指令能直接读到私钥。

---

## 7. 测试策略

token 尚未生效，以下除「端到端」外全部可以立刻做。

| 层 | 手段 | 现在能做 |
|---|---|---|
| 平台 API 客户端 | 自建 mock server，实现 5 个接口（含 hint，作绊线）+ 全部错误码 + `pending → available` 状态时序，编排器对着 mock 跑完整状态机 | ✅ |
| 网关改写 | 纯函数单测，19 项白名单逐条断言 + 边界与幂等 | ✅ |
| 配置解析 | 见下方 **§7.1 摄取用例矩阵** —— 这是唯一能提前防住「下次下发形状变了」的手段 | ✅ |
| Solver 契约 | 假 solver：写假 flag、只写 `result.json` 不写 flags 文件、超时不退出、崩溃、产出脏输出、flag 散落在 stdout 里 —— 逐一验证收割与提交链路 | ✅ |
| 韧性 | mock 注入 503 / 409 / 500 / 慢响应 / 连接重置，验证退避、对账、僵尸清理、崩溃恢复、收尾保护 | ✅ |
| 并发闸门 | mock 返回 3 题 `available` + 第 4 题 start 409，验证腾位置逻辑 | ✅ |
| 预算与重试 | 用极短 `TOTAL` 跑 mock，验证 Phase 1/2 切换、收尾保护、零 flag → `RETRY` 换策略；**绊线断言：整轮跑完 mock 的 hint 路由零命中**（§4.4） | ✅ |
| 镜像验收 | `docker run --rm -e … agent:latest` 启动即自解；查体积 ≤3GB、无敏感文件、日志干净、无公网依赖 | ✅（除真 token） |
| 端到端 | token 下发后真平台跑：先单题 → 再 3 题并发 → 再全程限时 | ⏳ 等 token |

**mock server 是当前最高优先级的可交付物** —— 它是唯一能在没有 token 的情况下验证编排器全部逻辑的手段，而且它同时是赛前的回归测试基线。

### 7.1 摄取用例矩阵（针对「每次下发的形状不确定」）

`ing/` 的内容每次评测都可能是新的形状，而赛中**没有现场排查手段**。所以这一组用例的价值不是「测代码对不对」，而是**把下次可能出现的下发形态提前枚举掉**。每条都必须是确定性断言，且**在 mock 之前就该全部写完**——它不需要 token。

| # | 用例 | 期望行为 |
|---|---|---|
| 1 | `KEY:VALUE`（冒号无空格） | 正确切分，值不含冒号前缀 |
| 2 | `KEY: VALUE`（冒号后有空格） | 值**不**带前导空格 |
| 3 | `KEY=VALUE`（等号） | 与 #1 等价处理 |
| 4 | **值内含冒号** `KEY:https://host/x` | 值为完整 URL，**不是 `https`**（回归本仓库曾经写错的那条） |
| 5 | 值内含 `=`（如带 query 的 URL） | 只在第一个分隔符切，右侧原样保留 |
| 6 | CRLF 行尾 | 值尾部无 `\r` |
| 7 | UTF-8 BOM 开头 | 第一个键名不带 BOM 垃圾字符 |
| 8 | 空行 / `#` 注释 / 行尾空白 | 跳过，不产生空键 |
| 9 | 末行无换行符 | 最后一行仍被解析 |
| 10 | 引号包裹 `KEY="v"` / `KEY='v'` | 剥成对首尾引号 |
| 11 | 同一键重复出现 | 后者优先 + **记警告**，不静默 |
| 12 | 键名换代（`AGENT_TOKEN` 而非 `BENCHMARK_TOKEN`） | 别名表命中，规范化到内部键 |
| 13 | 未知键 | **保留进报告**，不报错、不丢弃（格式变化预警） |
| 14 | 多个 `.ovpn` | 按确定性次序选一个并记录，不崩 |
| 15 | **完全没有 `ing/` 目录** | 视为正常（托管模式），全部走环境变量 |
| 16 | `ing/` 存在但缺 ovpn | 软启动，走 §6.3 可达性探测 |
| 17 | `ing/` 存在但 env 为空文件 | 明确硬失败并报告「无平台凭据」，不去发空 URL 请求 |
| 18 | ovpn 内容像 env / env 内容像 ovpn（误分类试探） | 内容签名判定优先于扩展名，分类正确或归入未知 |
| 19 | 未知类型文件（二进制、JSON、CA 证书） | 按 §6.5 分类表处理；未知即忽略 + 报告 |
| 20 | **报告脱敏** | `ingest_report` 中不出现 token 明文、私钥内容；只出现键名、长度、分类 |
| 21 | **solver 环境白名单** | 断言子进程环境里**不存在** `BENCHMARK_TOKEN`；只含 `LLM_*` / `SOLVER_*` / 代理变量（§6.5.1） |
| 22 | VPN 临时文件位置与权限 | 落在 solver workspace **之外**、`0600`、用完即删 |

---

## 8. 决赛（AI-KOH）预留

初赛代码里**不写任何 KOH 逻辑**（YAGNI），但契约留口，避免决赛推倒重来：

| 初赛 | 决赛 AI-KOH | 复用度 |
|---|---|---|
| `targets[]` = 平台下发的靶场地址 | `targets[]` = 对手据点列表 | 契约不变，数据来源换 |
| 一次性解题，`close` 后结束 | 持续对抗，据点控制权反复易位 | 状态机需扩展「持有 / 失守 / 夺回」 |
| 无 | Defense worker：加固自己的服务、监控入侵、恢复可用性（SLA） | 新增 worker 类型 |
| Scheduler 按分数性价比排 | Scheduler 按据点战略价值排 | 排序键可替换，调度框架复用 |
| State Journal / 日志 / 预算 | 同 | 原样复用 |

预留的具体做法只有两条：Solver Adapter 的 worker 类型是可枚举扩展的（`Attack` / 未来的 `Defense`），Scheduler 的排序键是注入的策略对象而非硬编码公式。除此之外不做任何投机设计。

---

## 9. 外部技术栈建议核查

2026-10-10 收到一份外部建议（Python + Node.js 26 + Rust 三栈融合，含参考框架清单）。逐条核查后分为三类。**保留本节是为了避免同样的建议在后续讨论中被重复提出。**

### 9.1 采纳（核查为真，已并入设计）

| 建议 | 核查结论 | 落到哪里 |
|---|---|---|
| 分层原则：低层安全执行与高层编排决策分离 | 与本设计 §0 完全一致，可作佐证 | §0 |
| ARTEMIS 作为参考架构 | **真实存在且高度相关**。斯坦福 Trinity 项目，`github.com/Stanford-Trinity/ARTEMIS`，**Apache-2.0**；supervisor + 可扩展子 agent + 分流 + 自动漏洞分级；Python supervisor + Rust `codex-rs/`（已 clone 核实） | §2.4 候选框架表**首行**、§2.5 |
| ARTEMIS 的「胜过 10 名人类专家中的 9 名 / 提交有效率 82% / 约 $18 每小时（人类 $60/h）」 | ⚠️ **这些数字来自论文口径，clone 下来的仓库正文里没有，未能本地核实。** 保留作背景，但**不作为选型依据**；真正的选型依据是读到的源码结构（见 §2.4） | 仅记录，不进设计 |
| Python 编排 + Rust 执行 | 方向正确，但实现方式改为**独立 CLI 而非 FFI**（见 9.2） | §1 |
| RustScan 类高速全端口扫描 | RustScan 是真实项目，可作为 nmap 的加速补充；依赖 raw socket（见 open-questions A4） | [image-spec.md](image-spec.md) §3 侦察类可选工具 |
| 渐进路线：先打通两栈，再引入第三栈 | 方向正确（每加一门语言都要有明确职责），但**本项目最终不是渐进到三栈，而是按执行位置分了五层**（§1.0）：Node 因 REA 成为必需、Go 因 recorder 成为必需、Lua 因目标侧注入成为新的一层。Zig 仍不引入 | §1.0、§1.2、§1.3 |

### 9.2 驳回（与本项目硬约束冲突，或技术上不成立）

| 建议 | 驳回理由 |
|---|---|
| **「比赛平台提供 MCP 接口，Agent 通过 MCP 与平台交互」** | **对本届（第三届）事实错误，但对上届为真** —— 这个区别很重要，不能简单当作幻觉。本届官方文档（`../CHALLENGES_API.md`）明确是 REST + `BENCHMARK_TOKEN` 请求头，`/openapi/v1/challenges/*` 五个接口，无任何 MCP。而**第二届确实同时提供 HTTP API 与 MCP 两种接入方式**：`refs/BreachWeave/docs/第二届腾讯云黑客松智能渗透挑战赛MCP接入文档.md` 原件列出了 5 个 MCP 工具（`list_challenges` / `start_challenge` / `stop_challenge` / `submit_flag` / `view_hint`）并附 Claude Code、Cursor、LangChain、openai-agents、Python 原生五种接入示例，其 FAQ 还专门回答「MCP 和 HTTP API 有什么区别」「可以同时使用吗」。**结论：本届必须自写 REST client，按 MCP 设计会白做一层。** 但要留意：① 内部工具侧仍可用 MCP（把渗透工具封装为 MCP server 供内层 agent 调用，Hexstrike-ai / REA 即此形态）；② 上届存在 MCP 意味着主办方有能力提供，**若本届平台页面上另有 MCP 端点或现成网关地址，应当去问**（已列入 [open-questions.md](open-questions.md) A18） |
| **「与威胁情报 API、CVE 数据库做 HTTP 通信」** | **托管模式无公网**，白名单仅 19 个大模型域名，此类外部服务全部不可达。正确做法是构建期离线打包漏洞知识（nuclei-templates 即此思路，见 [image-spec.md](image-spec.md) §2） |
| **「热路径用 PyO3 / napi-rs 进程内调用以避免序列化与 IPC 开销」** | 场景不成立且与隔离要求冲突。渗透工具的调用形态是 spawn 外部 CLI 跑几十秒到几分钟，序列化开销可忽略；FFI 进程内绑定会让 Rust panic 带走整个 solver 进程，摧毁 §5.2 依赖的崩溃隔离边界。FFI 只在「每秒上万次调用的纯计算小函数」才有意义，本项目无此热路径 |
| **「Rust 处理 Kerberos、NTLM 等 AD 域协议」** | 无证据表明初赛题集含 AD 域环境（`container_addr` 是 IP:端口数组）。范围膨胀，不做。是否存在内网横向题已列入 [open-questions.md](open-questions.md) A9，待主办方答复后再决定 |
| **用 Python 3.15 的 JIT 与 PEP 810 惰性导入** | 3.15.0 于 **2026-10-09**（本设计定稿前一天）发布，`cp315` wheel 生态为零，pwntools / z3-solver / gmpy2 / cryptography / pycryptodome 全部需源码编译 —— 在 3GB 镜像 + 5 天窗口下不可接受。且 PEP 810 是 **opt-in** 特性（`-X lazy_imports` 或显式语法），官方建议「单独决策、验证安全后再上生产」。冷启动优化在本项目不是瓶颈（一次性长任务，不是频繁启动的 CLI） |

### 9.3 无法验证（不作为设计依据）

以下项目名在核查中搜索无果，无法确认真实存在。即使存在也不构成选型依据 —— 本项目只需要一个内层框架和一组渗透工具，均已有充分候选：

`Clearwing`、`Blazen`、`Enki`、`NeMo Flow`、`bony-agent`、`nemue`、`bashkit`、`genai-pyo3`、`@c9up/ream`

> **勘误（2026-10-10，已 clone 源码后修正）**：本节原先还写了一句「同一份材料中对 ARTEMIS 的描述『Rust 负责沙箱化命令执行和 MCP 基础设施，Python 负责多 Agent 编排』未能在论文中得到证实」。**这句判断是错的，现予撤回。** clone 下来的 `refs/ARTEMIS/` 里有完整的 `codex-rs/` 目录，含 `linux-sandbox`、`execpolicy`、`apply-patch`、`mcp-client`、`mcp-server`、`mcp-types` 等 crate，README 也明确要求安装 Rust stable 才能构建 —— **ARTEMIS 确实是 Python + Rust 混合，且 Rust 部分的职责正是沙箱化命令执行与 MCP 基础设施，那份材料的描述准确**。我此前只查了论文没查仓库，就断言「查无出处」，这是核查不彻底。教训：判定一条外部描述「无法验证」之前，必须先 clone 源码看，论文不是唯一证据源。

**方法论结论**：这份材料真假混杂（Node 26 / Temporal / PEP 810 / ARTEMIS 的 Python+Rust 分层都是真的，但**平台 MCP 接口对本届不成立**、外部 API 通信、FFI 热路径是错的，近十个项目名无法验证）。因此**任何外部技术建议都必须对照官方接入文档与托管环境硬约束逐条核查后才能进设计**，不能因为其中几项准确就整体采信；同样地，**否定一条建议也需要同等强度的证据**。

---

## 10. 2026-10-10 开源生态调研轮（第二轮）

本轮把参考实现全部 clone 到 `../refs/`（约 500MB，浅克隆），**所有结论均来自读源码，不再依赖二手描述**。§9.3 的勘误就是这一轮产出的。

### 10.1 refs/ 清单

| 目录 | 上游 | 状态 | 用途 |
|---|---|---|---|
| `ARTEMIS/` | `Stanford-Trinity/ARTEMIS` | ✅ | 内层执行体首要参考，见 §2.4 |
| `ctf-agent/` | `verialabs/ctf-agent` | ✅ | 编排结构参考（有硬阻塞，不可直接用） |
| `BreachWeave/` | `m-sec-org/BreachWeave` | ✅ **第二届第一名** | 同赛事最高价值参考；`docs/` 内含上届官方 API + MCP 文档原件 |
| `xbow-competition/` | `m-sec-org/xbow-competition` | ✅ 第一届初赛 7/238 | 「薄桥接层 + 现成 CLI agent」路线的可行性证明 |
| `CHYing-agent/` | `yhy0/CHYing-agent` | ✅ | 上届题集规模与关卡 gating 的真实数据点 |
| `Cairn/` | `oritera/Cairn` | ✅ 第二届第三名 | 状态空间搜索建模 + mock 配置形态 |
| `aide-for-pentest/` | `chainreactors/aide-for-pentest` | ✅ | Docker worker 镜像切分模板 |
| `ARTEX-EN/` | `KbaHaxor/ARTEX-EN`（**镜像**） | ⚠️ 见 10.2 | 只借双图架构，不进镜像 |
| `rea/` | `morluto/rea` | ✅ | 逆向 MCP 工具面，要装进镜像 |
| `hexstrike-ai/` | **`0x4m4/hexstrike-ai`** | ✅ | 渗透工具 MCP server（150+ 工具） |
| `mcp-for-security/` | `cyproxio/mcp-for-security` | ✅ | SQLMap/FFUF/NMAP/Masscan 的 MCP 封装 |
| `mcp-security-hub/` | `FuzzingLabs/mcp-security-hub` | ✅ | Nmap/Ghidra/Nuclei/SQLMap/Hashcat 的 MCP 封装 |
| `BoxPwnr/` | `0ca/BoxPwnr` | ✅ | 离线基准评测框架（HackTheBox / Cybench / picoCTF），Python ≥3.10 |
| `Tsec-Hackathon/` | `Yeti-791/Tsec-Hackathon` | ✅ **主办方官方资源仓** | 两届获奖团队答辩 PPT + 前二十团队开源仓库导航索引 |
| ~~`ctfSolver/`~~ | ~~`passer-W/ctfSolver`~~ | ❌ **仓库已消失** | 作者 repo 列表里也没有了，放弃；不追低星副本 |

`refs/` 是**只读参考资料，绝不 COPY 进镜像**（见 10.4 的合规红线）。

### 10.2 两个供应链事件（本轮最重要的非技术发现）

1. **`Autumn-27/ARTEX` 原仓库已从 GitHub 下架**（`api.github.com` 返回 404）。时间线高度吻合：2026-10-06 韩国国家警察厅对 7 家金融机构遭袭立案；10-07 CrowdStrike 发布报告点名 ARTEX 为攻击者所用工具，AhnLab 称全球约 600 个 IP 有 ARTEX 活动迹象。幸存的是镜像与 fork（`KbaHaxor/ARTEX-EN`，AGPL-3.0，87★，`go.mod` 里 module 名仍是 `github.com/Autumn-27/artex`）。
2. **`passer-W/ctfSolver` 也已消失**，且不在作者的 repo 列表里。

**这两件事共同构成本项目的一条设计约束，而不只是八卦**：

- 平台明令「托管运行期间的全程产生数据将会被审计并部分公开」。在一个刚被 CrowdStrike 报告点名、原仓库被下架的工具上构建参赛作品，**审计与舆论风险远高于技术收益**。因此 ARTEX **只读架构、不进镜像、不引用代码**（AGPL-3.0 的传染性是第二重理由）。
- 上游可以在赛前 7 天内消失。因此**任何以「clone 上游 + 构建期拉取」为前提的方案都不可靠**，镜像必须在构建完成后完全自足 —— 这与 [image-spec.md](image-spec.md) §2 的离线化要求是同一件事的两个理由。
- 同理，§2.5 拒绝「薄壳包一个现成 CLI」作为唯一路线：`xbow-competition` 依赖 Kimi CLI 上游，那条路的风险敞口我们承担不起。

### 10.3 从 `refs/BreachWeave/docs/` 与 `Tsec-Hackathon/` 挖到的平台事实

上届官方文档原件（主办方/获奖队公开发布）直接回答了本届文档里的若干缺口。**注意：这些是上届规则，本届已改，只能作为「主办方历史惯例」的推定依据，不能当本届事实使用。**

| 事实（第二届） | 对本届的意义 |
|---|---|
| 认证头是 `Agent-Token`，Base URL `http://<SERVER_HOST>/api`，响应统一包 `{code, message, data}` | 本届改成 `BENCHMARK_TOKEN` + `/openapi/v1/challenges` + 裸 JSON。**接口形态整体换代了**，上届的 client 代码不能复用，只能复用设计思路 |
| **限频：每队每秒最多 3 次调用，所有接口共享计数，超出返回 429** | 本届文档**完全没提限频**。这是 [open-questions.md](open-questions.md) A6 的具体化假设：编排器的 API Client 应**默认按 ≤3 req/s 自我限速**，即使本届没有也无害 |
| **关卡 gating**：响应含 `current_level`，`403 = 比赛尚未开始 / 尚未解锁对应关卡`，提交正确后 `message` 追加 `【您已成功解锁新的关卡】` | 本届响应里**有 `level` 字段但没有 `current_level`，错误码表里也没有 403**。CHYing-agent 复盘佐证上届确实卡关（「卡在了第三关」）。**必须问清本届是否有 403 关卡门**（已列入 A19）—— 若有而编排器不处理 403，会在 Phase 1 把所有高关卡题反复撞墙，白烧预算 |
| **hint 扣 10%**（`POST /api/hint`，「答题成功将从奖励分数中扣减10%」；MCP 文档同口径） | 本届文档说「按 `hint_cost_radio` 比例扣减」但**响应里不给这个字段**（A10 缺口）。**10% 是一个有出处的默认值**，B6 的保守全局阈值应以此起步 |
| 响应含 `hint_viewed` 布尔；实例状态为 `stopped/pending/running`；`entrypoint` 为数组且仅运行时返回 | 本届 `container_status` 多了一个 `stop_pending`（四态），且**没有 `hint_viewed`** → 上届可查的「是否取过 hint」本届读不回来，曾是崩溃恢复的唯一缺口（旧 §4.4）。**本届不取 hint，该缺口随之消失**。`entrypoint` 数组语义与本届 `container_addr` 一致，**印证了 [platform-api.md](platform-api.md) §4.1 的分析** |
| 上届主赛**共 54 个 flag**（CHYing-agent 复盘） | A9（题集规模）的第一个真实数据点。按 54 flag / 两阶段预算反推，Phase 1 的每题时间片量级是可估的 |
| 第二届**同时提供 MCP 接入**（5 工具 + 5 种框架示例） | 见 §9.2 勘误。本届改回纯 REST；但上届有 MCP 说明主办方有能力提供，**平台页面上是否另有现成网关地址/MCP 端点值得去问**（A18）。另：上届 MCP 文档里的模型配置写的是 `MODEL_URL = "<LLM_GATEWAY_URL>" # 您生成的大模型网关地址` —— **上届是直接下发一个生成好的网关 URL，本届改成下发改写规则让我们自己拼**。这个差异需要确认（A18）：若本届平台页面其实也直接给了拼好的网关地址，我们手拼 `.tsecbench.gw` 就有出错风险，而这是 §6.2 说的「单点风险最高的 20 行代码」 |
| BreachWeave 的 host 侧规划 loop：30s 轮询、严守 3 实例、`runtime.maxSolvers`、按 `total_score/total_got_score` 排窗、久攻不下主动释放窗口、planner/solver prompt 解耦 | **与本项目 §3/§4 独立设计出的方案高度同构**，是一次有效的交叉验证。差异点：它是 30s 固定轮询，我们是事件驱动 + 轮询兜底；它把策略做成可配 prompt，我们做成确定性代码 —— 后者更符合 §0「规则明确的事不交给大模型」 |

### 10.4 合规红线（本轮新增，必须写进构建检查）

`refs/Tsec-Hackathon/` 是主办方官方资源仓，内含两届获奖团队答辩 PPT；`refs/CHYing-agent/` 含上届复盘与 `benchmark-results/`。这些是**公开发布**的材料，读它们不违规。但平台明令：**「Agent 不可内置针对题目的历史答题记忆或解题方法，一旦发现，作为作弊处置」**，且全程流量与对话记录被审计并部分公开。

因此划三条硬线：

1. **`refs/` 目录绝不 COPY 进镜像**。构建期泄漏检查（[image-spec.md](image-spec.md) §6）新增一条：扫描镜像各层，命中 `refs/` 路径特征、上届题目名/flag 字面量、`Tsec-Hackathon` 指纹即**构建失败**。
2. **不把上届题解、writeup、flag 模式喂进 prompt 或知识库**。可以借鉴的是**架构与工程方法**（双图、调度节奏、进程隔离），不是**具体题目的解法**。这条界线在审计公开的前提下必须守得住。
3. **本届是「全新题集」**（官方原话），上届题目与本届无重叠，所以「背题」在本届既违规又无收益 —— 没有动机去踩这条线。

### 10.5 本轮对既有设计的净改动

| 改动 | 依据 | 落点 |
|---|---|---|
| `prior_findings` 自由文本 → 结构化双图 `prior{assets, explored, open_leads}`，新增 `out/prior.json` 输出通道 | ARTEX 双图架构 | §2.1 / §2.2 / §2.5 |
| `task.json` 新增 `flag_pattern` 字段 | ARTEMIS `configs/tests/ctf_easy.yaml` | §2.1 |
| Python 3.13 → **3.14** | ctf-agent `requires-python = ">=3.14"`；3.14 已满一年 | §1 |
| Node 从「必要时引入」→ **必需** | REA `engines.node` 硬要求 | §1 |
| Rust 用途明确为高速暴力枚举，且**优先用 `feroxbuster`** 而非自研 | 用户决策 + 现成工具优先 | §1 / [image-spec.md](image-spec.md) §3 |
| API Client 默认自我限速 ≤3 req/s | 上届限频规则 | §5.1 |
| 新增 `403` 错误码处理分支（关卡未解锁 / 比赛未开始） | 上届有 403 关卡门，本届错误码表无 403 | §5.1 |
| ~~hint 扣分默认阈值取 **10%**~~ → **不取 hint**（整条链路删除） | 成本不可知（A10）+ 实测 10 次仅 1 次换出解出，期望收益为负 | §4.4 |
| 构建期泄漏检查新增「不得含 `refs/` 与上届题目素材」 | 平台作弊条款 + 审计公开 | [image-spec.md](image-spec.md) §6 |
| 撤回 §9.3 对 ARTEMIS 技术栈描述的错误否定 | clone 后见 `codex-rs/` | §9.3 |
| 撤回 §9.1 里 ARTEMIS 的性能数字作为选型依据 | 仓库正文无这些数字，仅论文口径，本地无法核实 | §9.1 |
| **薄壳的渗透专业 prompt 不必从零写**：以 ARTEMIS `codex-rs/core/` 的 9 个成文领域角色 prompt 为骨架重写（Apache-2.0，需保留 attribution）；提交抽象照搬 `submissions/{base,registry,ctf,vulnerability}.py` 的可注册 handler 形态 | clone 后直接读到这些文件 | §2.5 |
| **Node 从 24 LTS → 26.8.1；但构建产物为编译后 JS，使「26→24」降级是一行改动** | 用户指定 Node 26；**核实：Node 26 到 2026-10-28 才进 Active LTS，即初赛之后**，官方要求生产只用 LTS → 风险已记（B12） | §1.3 |
| **Go 从「只用现成二进制」升级为承担 `recorder` sidecar** | 用户指定 Go 1.26；recorder 是 §2.3「不信任模型自述」这条原则唯一能落地的形态，且是 §1.6 第 3 条性能杠杆的载体 | §1.2、§3、§0 |
| **Rust 新增 `ripgrep` 为收割热路径** | recorder 产生数百 MB~GB 级 JSONL，§2.3 的扫描成为真实 CPU 热点 | §1.4 |
| 新增 §1.6 性能模型：55% 是 LLM 往返，语言只影响 ~30% 的工具执行 | 「性能强悍」的目标函数应写在架构里，否则容易被引向「换语言」这条不省钱的路 | §1.6 |
| 契约新增 `out/traffic/*.jsonl` 通道与 `HTTP_PROXY` / `RECORDER_STATE` 输入 | recorder | §2.1、§2.2 |
| 组件新增 **Recorder Manager** | — | §3 |
| **新增 Lua 作为「目标侧注入语言」**（不是第五种实现语言）；镜像装 `lua5.4` + `redis-tools`，完整保留 nmap 的 612 个 `.nse`，**不装 luarocks** | 用户提出「结合 Lua 动态潜入能力」；本机实测 nmap 7.98 自带 612 个 Lua `.nse`，即**不新增运行时**；hexstrike-ai 的 nmap 工具已把 `nse_scripts` 作为一等参数 | §0、§1、[image-spec.md](image-spec.md) §3 |

---

## 11. 第二轮外部建议核查（Lua 融入方案，2026-10-10）

第一份材料（§9）真假约各半；这份**质量明显更高**——核心绑定库全部真实存在。但结论仍然是**大部分不采纳**，因为它的技术判断没错、**架构适配判断错了**。逐条核查如下（全部用 GitHub API / 上游站点实测，非二手描述）。

### 11.1 核实为真（可作依据）

| 材料中的说法 | 核实结果 |
|---|---|
| **Lupa**：把 Lua/LuaJIT 嵌进 CPython，支持释放 GIL | ✅ `scoder/lupa`，★1158，最近推送 **2026-10-08**（活跃） |
| **mlua**：Rust 最成熟的 Lua 绑定，支持 5.1~5.4 + LuaJIT | ✅ `mlua-rs/mlua`，★2895，最近推送 **2026-10-03**（活跃） |
| **gopher-lua**：纯 Go 的 Lua 5.1 VM，零 CGO | ✅ `yuin/gopher-lua`，**★6988**，事实标准 |
| **CVE-2022-0543**：Debian/Ubuntu 的 Redis Lua 沙箱逃逸 | ✅ 真实，与我此前引用的口径一致 |
| **OffensiveLua**：LuaJIT + FFI 的进攻性脚本集 | ✅ `hackerhouse-opensource/OffensiveLua`，★225，Lua |
| **Lua 当前版本** | ⚠️ **这里是 Lua 5.5.1，不是 5.4**（lua.org/download 实测）。我此前把 5.4 当最新版是错的 |
| **但镜像仍钉 `lua5.4`** —— 理由与「最新版」无关 | ✅ **`ldd $(which nmap)` 显示 nmap 7.98 链接 `liblua5.4.so.0`**。NSE 跑的就是 5.4。**嵌入方是什么版本，我们就钉什么版本** —— 这是比「追新」更硬的依据。写 5.5 会让自检解释器与 NSE 运行时不一致 |

### 11.2 核实为假 / 查无此物（不作为依据）

| 材料中的说法 | 核实结果 |
|---|---|
| 「**golua** 提供更现代的**沙箱优先**设计，支持 Lua 5.4 及部分 5.5 特性，内置沙箱化 IO/OS 与 Go↔Lua 消息通道」 | ❌ **不成立。** 搜索命中的 `Azure/golua`（★1816）**最后推送 2020-10-12，已六年停滞**，且它就是个普通 C API 封装，不是沙箱优先设计。这条描述与仓库实际状态严重不符 |
| 「**plua** 提供 Python asyncio 与 Lua 协程桥接，内置 HTTP/WebSocket/TCP/UDP」 | ❌ **查无此物**。GitHub API 精确搜 `plua` 无任何结果 |
| 「**luars** 是纯 Rust 实现的 Lua 5.5 运行时，完全无 C 依赖，支持 Serde」 | ❌ **查无此物**。同名仓库均为无关或 ★0/★1 的空壳 |
| 案例框架 **Luft**（Rust+Lua 多 Agent 编排）、**Tactus**（Python+Lua Agent DSL）、**IronCrew**（纯 Lua Agent 团队）、**ESP-Claw**（C+Lua+MCP）、**FlowScan**（Redis Lua 原子锁） | ❌ **五个全部搜不到**。这些是支撑其核心论证的「实际案例参考」—— 案例不存在，论证的分量就要打折 |
| Go 层职责含「**K8s 操作**」 | ❌ 与本项目无关（单容器，无 K8s）。属于模板化残留 |
| 「**Jev** 决策编排」「由 LLM 或 Jev 决策生成」 | ❌ 语义不明，疑为生成残片。不建立任何设计 |

### 11.3 最重要的一条判断：OffensiveLua 是真的，但它论证的是**我们不需要的那个能力**

材料说「Lua 在进攻安全中有独特战略优势：隐蔽性强（安全产品签名极少）、内存执行、易于混淆、JIT 近原生速度、FFI 直调 Windows API」，并建议在 Windows 目标里嵌入轻量 LuaJIT 解释器做后渗透。**这些属性逐条为真**（README 原文如此），但它们恰好是**初赛不奖励的那一类属性**：

| OffensiveLua 的卖点 | 初赛场景的实际价值 |
|---|---|
| 隐蔽、AV 签名少、易混淆 | **无价值**。CTF 靶场没有终端防护软件等着绕过，主办方要的是拿到 flag，不是不被发现 |
| UAC 绕过 / 凭据窃取 / 键鼠截取 / 内存搜密码 | **无对应题型**。这些是企业内网后渗透动作，不是「解出这道题的 flag」 |
| 前提是**已经在目标上拿到代码执行** | 而这正是题目本身要解决的那一步。**拿到 RCE 之后，直接读 flag 就行**，不需要再养一个解释器 |
| JIT 近原生速度 | FFI 调 WinAPI 的开销瓶颈在系统调用，不在解释器 —— **与 §1.4 拒绝 FFI 是同一条判断** |

**还有一条与体积/审计直接相关的反对理由**：要把这套能力变成可用，意味着镜像里得装一个 **Windows 的 LuaJIT payload/stager**。而平台明确「托管运行期间全程数据被审计并部分公开」，且 §10.2 刚记录过 ARTEX 因被 CrowdStrike 点名而下架的先例。**在一个刚发生这类事件的窗口里，把 implant 形态的载荷打进参赛作品镜像，风险收益完全不成比例。**

**结论：不采纳，且不需要 A9 答复就已可判定** —— 除非主办方明确题集含 Windows 主机目标且需要长期驻留，否则后渗透工具链整体不在范围内（这与 §9.2 驳回「Rust 处理 Kerberos/NTLM AD 域」是同一次取舍，ARTEMIS 里的 `active_directory.md` / `windows_privesc.md` 也只是**读它的分解思路，不搬载荷**）。

### 11.4 本轮对设计的净影响

采纳的只有一条**表述层面的收紧**，和一条**记入待办的最小入口**：

| 影响 | 落点 |
|---|---|
| **把「Lua 归 L4，不归宿主侧」写成显式原则**，并逐条驳回 Lupa/mlua/gopher-lua 三个宿主侧嵌入方案 —— 理由是复用 §1.4 已确立的崩溃隔离原则，保持一致性 | 新增 §1.5.1 |
| `lua5.4` 的钉版本理由**从「最新版」改为「nmap 实际链接的版本」**（`ldd` 实测 `liblua5.4.so.0`）。顺带纠正我自己「Lua 最新是 5.4」的错误认知（实为 5.5.1） | §1.5、[image-spec.md](image-spec.md) §3.1 |
| `gopher-lua` 嵌 recorder 做请求阶段规则 —— **唯一被承认有未来的宿主侧用法**，但现在不做，写明触发条件，防止「因为能做所以做」 | 新增 B13 |
| 采纳其收尾原则「**Lua 不参与决策，只负责执行**」（§0 已约束得更死） | §1.5.1 |
| 再次验证方法论：**外部材料的论证质量高≠适配本项目**。这份的绑定库调研是扎实的，但它的五个「实际案例」全是虚构，且核心建议（宿主侧嵌 Lua）与已定的进程隔离原则冲突 | 本节 |

> **方法论增量**（补 §9.3）：上一轮的教训是「判定无法验证前要先 clone 源码」；这一轮新增一条 —— **要区分「技术事实为真」和「对我们成立」**。绑定库、CVE、Lua 版本都为真，但「所以我们应该在编排器里嵌 LuaJIT」是错的推论。核查的最后一问永远是：**这件事在我们的硬约束（无公网 / 一次性沙箱 / 单容器 / CTF 计分 / 全程审计）下有没有对应收益**，而不是它本身先不先进。

---

## 12. Jev 排除决策（2026-10-10）

「充分利用 Jev」触发的第三轮核查已完成，**结论：完全排除，不落地任何 Jev 代码**。保留本节有两个作用：避免同一个对象被再次提出；给 §11.2 留下勘误。

| 项 | 结论 |
|---|---|
| 对象是否真实 | ✅ **真实存在**：TypeSafe AI 的 System One 模型（`POST https://api.typesafe.ai/v1/systemone`、`typesafe-sdk`、`jev-1.13.0`，官方活文档一手核实）。**§11.2 的「查无此物 / 生成残片」判定据此勘误** —— 错在把外部材料的畸形表述与被表述的对象混为一谈（同 §9.3 的教训） |
| 托管是否可达 | ❌ **不可达（硬约束）**：托管网关白名单 19 项全为国内厂商域名，`typesafe.ai` 不在其中，且平台明写「白名单外的域名即使加 `.tsecbench.gw` 也走不通」（[platform-api.md](platform-api.md) §4.3）；可用大模型仅 DeepSeek（[open-questions.md](open-questions.md) A5） |
| 本地是否可用 | 可达，但需额外 key，且「本地跑分使用非平台模型」的规则边界需要向主办方确认（原 A20 案） |
| **决策** | **完全排除**：不引入 `typesafe-sdk`、不新增环境变量、不建判定层、不写 provider 抽象 |

**排除理由**（按权重）：

1. **在正式跑分里它必然走降级路径。** 托管不可达 → 判定只能由 DeepSeek 承接，而那本身就是一次完整的大模型往返 —— §1.6 的 55% 瓶颈一分钱不省，反倒给「本地 / 托管双 provider」多养一条配置面与崩溃面。收益未经验证、复杂度确定。
2. **本地 / 开发的收益没有实测数据**，合规边界也没有答复。在无数据的条件下把外部服务写进设计，与 B10 的收敛纪律（「任何再加一个工具 / 框架的提议，默认答案是先让现有的跑通」）直接冲突。
3. 「哪些信息值得类型化、在哪个钩子消费」这类**微判定思路不依赖 Jev 也成立**，但它们本来就属于 L1 薄壳的内部实现细节（用现有 DeepSeek 循环完成），不构成独立架构层，无需单列设计。

**已撤回的落地项**（此前那版草案全部不作数）：判定层 provider 抽象与调用纪律、判定目录（J1–J10）、标定计划；[open-questions.md](open-questions.md) 原 A20（本地模型供应商）与原 B14（判定层切片）两条全撤 —— 原 A21 / B15（云域形态 / 云·横向工具面）随排除顺位变为 **A20 / B14**（§13 已按新编号引用）；[runbook.md](runbook.md) 的 `JUDGE_PROVIDER` / `TYPESAFE_API_KEY` 环境变量与判定层测试项；[image-spec.md](image-spec.md) 的 `TYPESAFE_API_KEY` 泄漏扫描条目。

**净收获**（保留）：① §11.2 的勘误本身 —— 方法论上：**「对象是否真实」与「硬约束下是否可用」是两个独立问题；否定一条建议需要同等强度的证据**；② 可达性核查方法：**白名单是第一道硬门**，先查可达性、再谈收益；③ 两类模型角色（System One / System Two）的认知，仅作背景，不落地。

---

## 13. 旗舰评测集口径（63 题 / 23000 分 / 360min / 六能力域）

用户 2026-10-10 给出目标口径：**TSecBench 旗舰评测集 —— 63 题、23000 总积分、360min 时限、6 大能力域（Web 漏洞挖掘、二进制漏洞挖掘、漏洞利用、多阶段渗透、云攻击、对抗规避），难度 normal；题目基于真实 CVE、生产级靶场与云原生环境。** 对既有设计的净改动如下。

### 13.1 预算与 A 清单更新

| 项 | 原假设 | 现口径 | 影响 |
|---|---|---|---|
| 总时限（A1） | 未知，靠环境变量手填 | **360min = 21600s** | `TOTAL_TIME_LIMIT` 默认改 21600；「deadline 字段」问题仍保留 |
| 题量（A9） | 上届 54 flag 反推 | **63 题 / 23000 分**（均分 ≈365 分/题） | 预算公式可代入实数复算（下表） |
| Phase 1 时间片 | 公式 `0.45×TOTAL / 题数` | **162min ÷（63 题 ÷ 3 并发 = 21 波）≈ 7~8min/题** | 上下限按此收敛；「先易后难、快速落袋」（§4.2）的权重更高 |
| Phase 2 | — | ≈188min（按进展度重投） | 不变 |
| 仍需向主办方问的 | 是否多机 / Windows / 云形态 | **A9 保留 + 新增 A20（云攻击域靶场形态）** | 见 §13.2 |
| hint | 默认 10% 阈值 | **不取 hint**（链路整体删除） | 见 §4.4 |

### 13.2 六域覆盖自检（现状映射 + 缺口）

本条自检回应「攻防能力优先」的排序：**先保证六域都有可用的进攻能力，再谈工程美感。** 纪律不变 —— **不新增工具，缺口只记触发条件**（B10）：

| 能力域 | 现有覆盖 | 缺口与处置 |
|---|---|---|
| Web 漏洞挖掘 | 侦察（naabu / httpx / katana / nuclei）+ web（sqlmap / ffuf / feroxbuster / dalfox / nikto / playwright）+ recorder | **无**（工具面最厚的一域） |
| 二进制漏洞挖掘 | Ghidra 12.1 + REA（133 工具）+ pwntools / gdb / ROP 链 | 体积风险未消（A14 未答复）；其余无 |
| 漏洞利用 | pwntools / pycryptodome / z3 / 现场编译 + L4 Lua 注入 | 无 |
| 多阶段渗透 | 单机多步链按现有工具面 + solver 现场脚本覆盖；**横向移动工具未装** | **B4 触发条件接近命中**：旗舰集明确含该域，但「单机多步 vs 多机横向」未分 —— 形态待 A9/A20。最小候选（chisel / ligolo-ng，Go 静态二进制，10MB 量级）已记账（B14），答复后一天内可落地 |
| 云攻击 | **无专用工具** | §11.2 曾以「我们自己的沙箱无 K8s」驳回 K8s 相关性 —— 那是**运行环境**，与「**目标环境**是云原生」是两回事，驳回理由不覆盖本情形。最小面：`curl` / `requests`（IMDS 169.254.169.254 / 元数据 / STS）已够；`kubectl`（≈50MB 单二进制）待 A20 答复 + A14 体积口径再定（B14） |
| 对抗规避 | recorder（去重 / 重放） | 规避动作本身用现有工具参数完成；**不新增 payload / 植入物**（§11.3 红线不变） |

> 两处缺口的共同纪律：① 不阻塞关键路径（10/11–12 的核心件不因此改序）；② 只能在 A14 体积口径 + A20 答复之后动；③ 用「旗舰集口径」这份新证据启动评估，而不是「因为能做所以做」（§11.4）。

### 13.3 净改动

| 改动 | 落点 |
|---|---|
| `TOTAL_TIME_LIMIT` 默认 21600s；Phase 1 时间片 ≈7~8min/题 | §4.3、[runbook.md](runbook.md) §3.3 |
| 新增 A20（云域靶场形态）、B14（云 / 横向最小工具面）；B4 触发条件上调 | [open-questions.md](open-questions.md) |
| 六域覆盖表 + 云 / 横向两处缺口记账 | 本节、[image-spec.md](image-spec.md) §3 |
