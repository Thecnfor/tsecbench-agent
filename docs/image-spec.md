# 镜像规格

硬约束：上传的 `.tar` / `.tar.gz` / `.tgz` **最大 3GB**。托管环境**无公网**，所有工具、词库、模板、依赖必须在构建期离线打进镜像。

---

## 1. 基座：`debian:trixie-slim`

**不用 Kali。** 理由：

1. **构建不可复现**。Kali 是滚动发行版，比赛期间重建镜像可能装到不同版本的工具。赛前重建镜像是高概率动作，这个风险不能要。
2. **Kali 的唯一优势用不上**。它的价值在 `kali-linux-headless` / `kali-tools-*` 这类元包，而那套未压缩 10GB+，直接撞 3GB 上限。我们要精选安装，享受不到元包的好处。
3. **混挂 Kali 源到 Debian 会踩依赖地狱**（libc / libssl 版本差异），得不偿失。

`debian:trixie-slim` 的好处：压缩后约 30MB；glibc 够新，Go/Rust 静态二进制直接跑；apt 源稳定且版本可 pin；Python / Node 版本新。

## 2. 工具来源策略

按「体积 × 可复现性」排优先级：

| 优先级 | 渠道 | 适用工具 | 理由 |
|---|---|---|---|
| 1 | **官方 GitHub release 的静态二进制**（按 tag 下载，校验 sha256） | Go/Rust 系：nuclei、ffuf、httpx、gobuster、subfinder、dalfox、interactsh-client | 单个几 MB 到几十 MB，零共享库依赖，版本完全钉死，最省体积也最可复现 |
| 2 | **纯 Python 项目直接 clone / venv 安装**（按 commit 或 tag 钉死） | sqlmap（纯 Python，clone 即可跑）、dirsearch、ROPgadget、ropper、wafw00f、checksec | 不引入 apt 依赖链，体积可控 |
| 3 | **pip 装库**（锁 requirements 文件 + hash） | pwntools、pycryptodome、scapy、z3-solver、sympy、requests、gmpy2 | 解题脚本层刚需 |
| 4 | **apt（版本 pin）** | nmap、gdb、radare2/rizin、binwalk、john、hashcat、hydra、socat、netcat-openbsd、tcpdump、ruby（为 gem）、build-essential、binutils、patchelf、curl、wget、jq、unzip、xxd、file、git、openvpn、**lua5.4**、**redis-tools** | 只有这些确实需要系统级打包 |
| ✗ | **不装** | metasploit（2–3GB）、kali-tools 元包、完整 SecLists（1GB+）、SageMath（数 GB）、**ARTEX**（AGPL-3.0 + 强制 PostgreSQL + 原仓库已被 GitHub 下架，见 [architecture.md](architecture.md) §10.2）、**`refs/` 内任何参考实现**（合规红线，见 §6） | 体积不允许 / 许可与合规不允许 |

**Ghidra + JDK 21 由「不装」改为「装」**（2026-10-10 决策翻转）。理由与体积核算见 §3.2 与 §4。

**离线化要求**：任何运行时会联网拉资源的组件，必须在构建期把资源拉下来打进镜像：

- `nuclei-templates`：nuclei 默认运行时联网更新模板，必须在构建期下载并按官方离线用法放置，同时禁用其自动更新；
- **REA（`rea-agents`）**：绝不能在运行期走 `npx rea-agents ...` —— 托管环境无公网，`npx` 会尝试拉包并直接失败。必须构建期 `npm ci` 装进镜像、钉死版本、并把 `npx` 调用改为直接执行本地入口。同理其 `selfupdate` / 版本检查类逻辑必须关闭；
- **Ghidra**：构建期解压放置，禁用其启动时的更新检查；Ghidra 的 `analyzeHeadless` 首次运行会初始化项目目录，需在构建期预热一次以免占用比赛时间；
- **ARTEX 类带 `selfupdate` 的项目**：一律不装（除许可原因外，自更新逻辑在无公网环境里必然报错刷屏）；
- 词库：只装精选子集（见 §3）；
- pip / apt / npm / gem / go：只在构建期使用，运行期全部禁用；
- playwright 的 chromium：构建期 `playwright install --with-deps chromium`，运行期不再下载。

## 3. 工具清单

按解题域组织。清单是**目标集**，构建时逐项验证可用性与体积，超预算则按「域优先级」裁剪。

### 侦察与扫描
`nmap`（apt）、`masscan`（apt，需 raw socket）、**`naabu`** / `httpx` / `subfinder` / `katana`（Go 静态二进制）、`nuclei` + 离线模板
可选：`RustScan`（Rust 二进制，全端口扫描显著快于 nmap，但仍依赖 nmap 做服务识别；需 raw socket，见 [open-questions.md](open-questions.md) A4）

**nmap 的 NSE 脚本必须完整打进镜像**（`/usr/share/nmap/scripts/`，本机实测 **612 个 `.nse`**，均为 Lua）。这是 Nmap 自带的，apt 装完就有 —— 要做的是**确认多阶段构建没把它当文档裁掉**（`--no-install-recommends` 不会删，但「删 `/usr/share/doc`」这类清理步骤要确认没误伤 `/usr/share/nmap`）。构建后自检：`ls /usr/share/nmap/scripts/*.nse | wc -l` 必须 >500，且 `nmap --script-help=http-headers` 能出说明。

### Lua 注入层（2026-10-10 新增）

`lua5.4`（apt，约几百 KB）+ `nmap-scripting-guide` 无需额外安装。定位与纪律见 [architecture.md](architecture.md) §1：

- **`lua` 解释器只为本地写/自检 `.nse` 脚本而存在**，solver 不通过 `lua` 独立执行攻击链 —— 所有 Lua 逻辑经由 nmap / redis-cli 等**已有工具**注入进目标，崩溃与超时仍走 §5.2 的进程组隔离；
- **不装 `luarocks`，不引入任何 Lua 第三方库。** NSE 只用 nmap 自带的 `nmap.*` / `stdnse.*` / `shortport.*`。装 luarocks = 开一个新依赖树 + 一个新的运行期联网面，无公网环境下必然失败；
- solver 现写的 `.nse` 落在该题 workspace，随 workspace 清理；**镜像内不预置任何针对性 NSE 脚本**（平台作弊条款禁止内置针对题目的解题方法，见 §6）。允许内置的是「NSE 标准库参考文档」这类通用能力资料，不允许的是「打某道题的成品脚本」。
- Redis 相关的 Lua 能力依赖 `redis-cli`（apt，约 1MB）—— 一并装入。若目标出现 Redis，`EVAL` 与已公开的沙箱逃逸（CVE-2022-0543）属通用武器库知识，不是题解预置。

### Web
`sqlmap`（clone）、`ffuf` / `gobuster`（Go 静态二进制）、`dirsearch` / `wafw00f`（pip）、`nikto`（apt/git）、`dalfox`（Go 静态二进制）、`curl`、`socat`、`playwright + chromium`（JS 渲染 / DOM XSS / 会话交互，已确认要装）

### 爆破与口令
`hydra`（apt）、`john`（apt）、`hashcat`（apt；注意托管环境**无 GPU**，只能 CPU 模式，算力有限，不作为主力）

**高速暴力枚举是 Rust 的主战场**（2026-10-10 决策）：

- **首选 `feroxbuster`**（Rust，官方 release 提供静态二进制，按 tag 下载 + 校验 sha256）。递归目录/文件爆破，支持自动递归、扩展名过滤、并发调节，性能远优于 `dirsearch`（Python）且与 `ffuf`（Go）同级。**现成的就直接用，不自研。**
- `ffuf` 保留，用于需要模板化 fuzz（参数、header、POST body）的场景 —— feroxbuster 专注路径爆破，两者不重叠。
- `RustScan` 作为可选的全端口扫描加速（见「侦察与扫描」）。
- **只有当上述现成工具都覆盖不到时**，才自研 Rust 二进制，且必须以**独立 CLI + spawn** 形式集成，不做 PyO3/napi-rs 的 FFI 进程内绑定（理由见 [architecture.md](architecture.md) §1）。

### 二进制 / Pwn
`pwntools`（pip）、`gdb` + `pwndbg`（apt + git）、`ROPgadget` / `ropper`（pip）、`one_gadget`（gem，需 ruby）、`checksec`、`patchelf`、`binutils`、`gcc` / `build-essential`（现场编译 exploit）

### 逆向
`radare2` / `rizin`（apt）、`binwalk`（apt/pip）、`objdump` / `readelf`、`file`、`xxd`

**`Ghidra 12.1.x` + `JDK 21`（本轮决策：装）** —— 反编译产出的伪 C 比反汇编易读得多，直接决定逆向题的胜率；且 REA 的 Ghidra provider 需要它。版本选择理由见 §3.2。

### MCP 工具层（本轮新增）

把渗透/逆向工具封装为 MCP server，供内层 agent 统一调用。**这是内部工具集成协议，与平台 API 无关**（平台是 REST，见 [architecture.md](architecture.md) §9.2）。已 clone 到 `../refs/` 供评估：

| 项目 | 提供 | 取舍 |
|---|---|---|
| **REA**（`morluto/rea`，MIT，npm `rea-agents`） | **133 个 MCP 工具** + 89 个 CLI 命令；覆盖原生二进制（Hopper/Ghidra/IDA）、JS/Electron、.NET、APK（JADX）、固件（binwalk/unblob）、网站、EVM 字节码、崩溃记录、进程行为。**证据驱动**：每条结论附证据与置信度，查不到明确标注未知 | **装**。这是逆向题的最大杠杆，且 MIT 许可干净。**必须钉死版本**（见 §3.2） |
| **hexstrike-ai**（`0x4m4/hexstrike-ai`） | `hexstrike_server.py`（HTTP 后端，~500 个函数）+ `hexstrike_mcp.py`（MCP 前端，~310 个工具定义）；150+ 渗透工具编排 | **装**。server/MCP 两层拆分的设计值得直接借鉴 —— 工具实现与 MCP 暴露解耦 |
| **mcp-security-hub**（`FuzzingLabs`） | Nmap、Ghidra、Nuclei、SQLMap、Hashcat 等的 MCP 封装 | **评估**，与上两者有重叠，优先取无重叠部分 |
| **mcp-for-security**（`cyproxio`） | SQLMap、FFUF、NMAP、Masscan 的 MCP server 合集 | **评估**，同上 |

MCP 层的取舍原则：**能用 CLI 直接 spawn 的就不套 MCP**。MCP 的价值在于给 LLM 一个统一、带 schema 的工具面（减少 prompt 里的工具说明体积、提高 function calling 命中率），不在于进程管理。对于 nmap 这种一条命令就能跑完的工具，直接 spawn 更简单可靠；对于 REA 这种有 133 个工具、需要维护调查上下文状态的，MCP 才是正确形态。

### Crypto
`pycryptodome`、`sympy`、`z3-solver`、`gmpy2`（pip）；SageMath **不装**（体积数 GB）。

### 网络 / 协议
`socat`、`netcat-openbsd`、`tcpdump`、`scapy`（pip）、`openvpn`（≥2.5，VPN 双模用）、`redis-tools`（Lua `EVAL` 注入用）

### 流量录制（Go 自研，L2/L0 之间的客观性层）
**`recorder`** —— 本项目唯一自研的 Go 二进制，每题一个 sidecar，HTTP(S) 透明录制 + 按 `(method,url,body-hash)` 去重重放。职责、选型理由与实现规模边界见 [architecture.md](architecture.md) §1.2。

- 构建：builder 阶段 `CGO_ENABLED=0 go build` → 单静态二进制（约 15~25MB）COPY 进 runtime，**容器内不留 Go 工具链**；
- 运行：监听 `127.0.0.1:<每题独立端口>`，**不得监听 `0.0.0.0`**；CA 证书构建期生成并挂载，运行期不联网；
- 落盘：`<workspace>/out/traffic/*.jsonl`，随 workspace 清理，受 §4 的磁盘配额约束（**solver 可能录到几百 MB，需要在 Supervisor 里设录制的体积上限与降级策略：超限后只存 header + body 摘要，不存全文**）。

### 词库（精选，非完整 SecLists）
常见目录/文件名字典（约 20MB 量级）、`rockyou.txt` 前 100 万行（约 15MB 量级）、常见弱口令小表。**完整 SecLists 约 1GB+，不装。**

### 通用
`git`、`curl`、`wget`、`jq`、`unzip`、`tmux`、`python3-venv`、**Node 26.8.1**、**`ripgrep`**

## 3.1 运行时版本钉死

| 运行时 | 版本 | 理由 |
|---|---|---|
| **Python** | **3.14**（当前维护版 3.14.8） | **不是 trixie 自带版本（自带 3.13），需额外安装** —— 但这是硬下限：`ctf-agent` 的 `pyproject.toml` 写死 `requires-python = ">=3.14"`，且 3.14 已发布满一年、`cp314` wheel 生态成熟。**3.15 排除**：3.15.0 于 2026-10-09 发布（本设计定稿前一天），`cp315` wheel 为零，pwntools / z3-solver / gmpy2 / cryptography / pycryptodome 全部需源码编译。安装方式优先 `python-build-standalone` 的预编译 tarball（按 release tag + sha256 钉死），避免在镜像里编译 CPython。详见 [architecture.md](architecture.md) §1 与 §9.2 |
| **Node** | **26.8.1（精确小版本钉死）** | 用户指定 Node 26。REA 的 `engines.node = "^22.19.0 \|\| ^24.11.0 \|\| >=26.0.0"` 覆盖 26 ✓；本机即 `v26.8.1`，开发与镜像同版本 ✓。<br>⚠️ **Node 26 到 2026-10-28 才进 Active LTS，即初赛（10/17–18）之后**，官方要求「生产只用 Active/Maintenance LTS」→ 这是**已知未消除**的风险。<br>**对冲**：MCP server 在构建期编译为纯 JS，不依赖 Node 26 的原生 TS type-stripping 作为运行前提 → 若 10/16 彩排出现 Node 侧异常，切 24 LTS 只是**一行基础镜像改动**。详见 [architecture.md](architecture.md) §1.3 |
| **Go** | **1.26.x，仅构建期** | 自研 `recorder` 流量录制 sidecar 与 `naabu` / `httpx` / `subfinder` / `nuclei` / `katana` 等现成工具都是 Go。本机 `go1.26.2`，ARTEX `go.mod` 基线 `go 1.26.3`。**runtime 阶段只 COPY 静态二进制**（`CGO_ENABLED=0`），不留 Go 工具链 |
| **ripgrep** | 按 release tag 下载静态二进制 + sha256 | Rust。收割链路热路径：扫 workspace + stdout + recorder 的 JSONL（可达数百 MB~GB 级），比 Python `re` 快约一个量级 |
| **JDK** | **21（64 位完整 JDK，含 compiler 模块）** | REA 的 Ghidra provider 与 JADX（APK 分析）都要求完整 JDK 而非 JRE；REA 文档明确「Ghidra 12.1.4 + JDK 21」为其验证组合 |
| **Ghidra** | **12.1.x（对齐 12.1.4）** | REA 声明支持 Ghidra 12.1.x，并在 12.1.4 上做过 process/project cleanup 验证。**跟着 REA 的版本走，不自行选更新版** —— REA 不会替你下载 Ghidra，路径由配置告知，版本不匹配会导致 provider 静默不可用 |
| **REA** | **钉死 `rea-agents` 具体版本（clone 时为 6.3.0）** | ⚠️ **版本churn 极快**：5.0.0（10-07，133 工具）→ 6.1.0 / 6.2.0（10-09）→ 6.3.0。几天一个大版本。**必须钉死 + npm lockfile + 构建期离线打包**，绝不写 `^6` 这类范围。赛前若需升级，必须重跑 REA 的工具面自检 |
| **feroxbuster** | 按 GitHub release tag 下载静态二进制 + 校验 sha256 | Rust 系高速目录爆破首选，见 §3 |
| **Lua** | **5.4**（`lua5.4`，apt） | **钉 5.4 的理由是「nmap 实际链接的版本」，不是「最新版」** —— 实测 `ldd $(which nmap)` 显示 nmap 7.98 链接 `liblua5.4.so.0`，NSE 跑的就是 5.4（Lua 官方当前已是 **5.5.1**，若钉 5.5 会让自检解释器与 NSE 运行时不一致）。仅用于本地写/自检 NSE 脚本；**不装 luarocks、不引入 Lua 第三方库**。真正的执行载体是 nmap 内嵌的 Lua 引擎（随 apt 的 nmap 一起来，612 个 `.nse`）。体积可忽略，见 §3「Lua 注入层」。**不在我方任何进程里嵌入 Lua 运行时**（Lupa/mlua/gopher-lua 三个宿主侧方案均已驳回，理由见 [architecture.md](architecture.md) §1.5.1） |
| Rust | stable，**仅用于构建期** | 若引入自研高速枚举器，编译为静态二进制后 COPY 进 runtime 阶段，容器内不需要 Rust 工具链。注：ARTEMIS 若要构建，其 `codex-rs/` 也需要构建期 Rust |
| openvpn | **≥ 2.5** | 靶场 VPN 使用 `compat-mode 2.4.8` + `aes-128-cbc` + `sha1` 的 legacy 参数组合，需确认版本支持 `compat-mode`，且 Debian trixie 的 crypto-policies 未禁用 sha1 / aes-128-cbc |

## 3.2 Ghidra + REA 的决策依据与代价

**决策翻转记录**：Ghidra 原定「先不装，留体积余量再补」。2026-10-10 改为**装**，触发因素是 REA —— 单独装 Ghidra 只是多一个 GUI 反编译器（在无人值守容器里价值有限，只能跑 headless analyzeHeadless），而 **REA 把 Ghidra 的反编译输出变成了 LLM 可直接调用、带证据链的工具面**，两者合起来才构成逆向题的完整能力。

代价与缓解：

| 代价 | 缓解 |
|---|---|
| 体积 +300~400MB（Ghidra + JDK 21，压缩后） | §4 重算后仍在预算内 |
| Node 从可选变必需，多一个运行时 | Node 26.8.1 压缩后约 30MB，且 §3 的 MCP 层本来也需要它 |
| REA 版本 churn 快，赛前可能已不是 6.3.0 | 钉死版本 + 离线打包；**不接受运行期 `npx` 拉取**（托管环境无公网，运行期拉取必然失败） |
| Ghidra headless 分析单个二进制可能耗时数分钟 | 纳入 solver 的时间片预算；REA 的调查上下文可跨调用复用，避免重复分析 |
| REA 的进程捕获功能默认禁用（需一堆 `REA_PROCESS_CAPTURE_ENABLED=` 类环境变量开启） | **保持禁用**。动态捕获会引入不可控的宿主交互，且我们的目标是 CTF 靶场二进制，静态分析 + gdb 已足够 |

**REA 的 Ghidra bridge 走本地私有 socket 且需鉴权** —— 在一次性沙箱里这是安全的，但要确认它不监听 `0.0.0.0`。托管运行全程流量被审计，任何意外监听端口都会在审计记录里留下痕迹。构建后需实测 `ss -ltnp` 确认监听面。

## 4. 体积预算

估算值（**压缩后**），构建时以实际测量为准并回填。2026-10-10 因 Ghidra + JDK + REA 入镜像而重算：

| 层 | 压缩后估算 |
|---|---|
| `debian:trixie-slim` 基座 | ~30 MB |
| apt 系统工具 + build-essential + **Python 3.14（standalone tarball）** + Node 26.8.1 + lua5.4 + redis-tools | ~300 MB |
| **JDK 21（headless）** | ~120 MB |
| **Ghidra 12.1.x** | ~300 MB |
| Go/Rust 静态工具（nuclei/naabu/httpx/katana/ffuf/gobuster/subfinder/dalfox/**feroxbuster**/**ripgrep**）+ **自研 `recorder`** | ~130 MB |
| nuclei-templates 离线包 | ~60 MB |
| Python 安全库（pwntools / pycryptodome / scapy / z3 / sympy …，z3 占大头） | ~180 MB |
| sqlmap + dirsearch 等 clone 项目 | ~40 MB |
| gdb + pwndbg + rizin + binwalk | ~120 MB |
| 精选词库 | ~40 MB |
| playwright + chromium | ~200 MB |
| **REA（`rea-agents` + node_modules，含原生 Node-API 模块）** | ~180 MB |
| **hexstrike-ai + MCP 工具层** | ~60 MB |
| **内层 solver（自研薄壳 + openai SDK + 依赖）** | ~120 MB |
| 编排器（Python，复用同一运行时） | ~1 MB |
| **合计** | **~1.88 GB** |

对比上一版预算（~1.41GB）净增 ~470MB，主要来自 Ghidra(300) + JDK(120) + REA(180) + MCP 层(60) + recorder(20) + Node 26/lua/redis(+10)，被「内层从 OpenHands 量级 400MB 降为自研薄壳 120MB」抵消了一部分。

### 4.1 3GB 上限的口径是当前的头号体积风险

**若 3GB 指上传文件（压缩后 tar.gz）**：余量约 1.12GB，安全。

**若 3GB 指解压后的镜像大小**：**当前预算直接超标。** 压缩比通常在 2.5~3× 之间，1.88GB 压缩 ≈ 4.7~5.6GB 解压。这会把 [open-questions.md](open-questions.md) A14 从 🟡 提到 🔴 —— **在主办方答复之前，镜像必须按「解压后 ≤3GB」这个更严的口径准备一份可裁剪方案。**

裁剪优先级（从先砍到后砍，按「分数损失 / 省下体积」排序）：

| 顺序 | 砍什么 | 省下 | 代价 |
|---|---|---|---|
| 1 | `build-essential` / `gcc` 移出 runtime 阶段（只在 builder 用） | ~120 MB | 现场无法编译 exploit —— 代价不小，尽量别砍 |
| 2 | playwright + chromium | ~200 MB | 失去 JS 渲染与 DOM 交互能力，Web 题胜率下降 |
| 3 | Ghidra + JDK | ~420 MB | 逆向题退化为 rizin 反汇编喂 LLM，胜率明显下降 |
| 4 | REA | ~180 MB | 与 3 联动砍，否则 REA 的 Ghidra provider 空转 |
| 5 | nuclei-templates 只留 web/ 与 CVE 高频子集 | ~40 MB | 漏洞覆盖变窄 |

**注意 3 与 4 必须一起砍**（REA 的核心价值就在 Ghidra bridge），合计省 ~600MB，是最有效的单步降级。

体积控制手段：多阶段构建（builder 阶段装编译工具链，runtime 阶段只 COPY 产物）；`apt` 用 `--no-install-recommends` 并清理 `/var/lib/apt/lists`；pip 用 `--no-cache-dir`；npm 清理 cache（`npm ci --omit=dev` + 删 `~/.npm`）；Ghidra 删掉用不到的 `docs/`、`GPL/`、多余 processor 模块（**保留 x86 / ARM / MIPS / PowerPC，删其余**）；删除 man / locale / `/usr/share/doc`。

## 5. 构建规范

- **多阶段**：`go-builder`（Go 1.26，`CGO_ENABLED=0` 编译 `recorder`）→ `rust-builder`（Rust stable，仅当自研枚举器时才需要）→ `node-builder`（Node 26.8.1，把自研 TS MCP server **编译成纯 JS**，使运行期不依赖原生 TS 直跑 → 保留「26 切 24 只改一行」的退路）→ `runtime`（只有运行所需的二进制与解释器，**不含任何语言工具链**）。
- **recorder 的 CA**：构建期生成 CA 密钥对并把**公钥/证书**打进镜像（供 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` 挂载）。**CA 私钥必须是每题启动时随机生成的临时私钥，不能是构建期那份** —— 固定 CA 私钥进镜像等于给审计留下一个可长期信任我方流量的凭据，且它一旦被公开记录在审计材料里就是真实的密钥泄漏。构建期只放证书用于本地自检，运行期临时签发、随 workspace 销毁。
- **版本全钉死**：静态二进制按 tag 下载并校验 sha256；apt 包 pin 版本；pip 用带 hash 的 lock 文件；npm 用 lockfile；gem 钉版本。工具版本记录在一份构建清单文件中，重建产物必须逐字节可复现。
- **入口**：`ENTRYPOINT` 启动即自动开始解题。平台明确要求「容器启动后将自行开始解题」，不能有任何等待人工输入的步骤。
- **非 root 运行**：编排器与 solver 尽量以非 root 用户跑；但部分工具（raw socket 的 masscan、openvpn 拨号）需要特权，按需在镜像里赋予 `cap_net_admin` / `cap_net_raw`（通过文件 capability 而非整体 root），并在文档中记录 —— 托管沙箱是否允许这些 capability 是待确认项。
- **日志**：stdout/stderr 结构化输出（JSON lines），便于赛后复盘与应对审计。日志中必须脱敏 token 与 key。
- **无持久化依赖**：沙箱一次性销毁，任何状态只在本次运行内有意义。

## 6. 泄漏防线

构建流程末尾插入一道自动检查，命中任一模式即**构建失败**：

**A. 凭据与私钥**

- UUID 形态的 token 字面量（当前手上的 token 已知，可作精确指纹）；
- `-----BEGIN PRIVATE KEY-----` / `-----BEGIN RSA PRIVATE KEY-----` / `-----BEGIN OPENVPN PRIVATE KEY-----`；
- `sk-` 前缀的密钥串；
- `BENCHMARK_TOKEN` 的实际取值；
- `*.ovpn` 文件残留、`ing/` 目录残留、`.env` 文件残留；
- OpenVPN tls-auth 静态密钥块的指纹；
- **recorder 的构建期 CA 私钥**（新增检查项，见 §5）—— 镜像里允许出现 CA **证书/公钥**，绝不允许出现对应**私钥**。扫描 `-----BEGIN CERTIFICATE-----` 邻近是否存在 `BEGIN PRIVATE KEY`，并对构建期 CA 的私钥做精确指纹比对。

**B. 参考资料与作弊红线（2026-10-10 新增）**

平台明令：**「Agent 不可内置针对题目的历史答题记忆或解题方法，一旦发现，作为作弊处置」**，且托管全程的网络流量与大模型对话记录被审计并部分公开。因此：

- **`refs/` 目录的任何内容**（14 个 clone 下来的参考实现，约 500MB）—— 扫描各层是否出现 `refs/` 路径特征、以及 `ARTEMIS` / `BreachWeave` / `ARTEX` / `ctf-agent` / `CHYing-agent` / `Tsec-Hackathon` 等仓库的目录指纹；
- **上届赛事素材** —— 获奖团队答辩 PPT（`.pdf` / `.pptx`）、上届题目名、上届 flag 字面量、`benchmark-results/` 内容；
- **任何硬编码的 flag 字面量或题解逻辑** —— 正则扫 `flag\{[^}]+\}`、`HTB\{[^}]+\}`、`CTF\{[^}]+\}` 等形态。注意区分：**Flag Harvester 的提取正则本身是合法代码**（它是模式，不是字面量），检查规则要能区分「匹配 flag 的正则表达式」与「一个具体的 flag 值」，否则会误伤。做法是把 harvester 的正则常量登记为白名单。
- **AGPL-3.0 代码** —— `ARTEX` 是 AGPL-3.0，其代码片段若被复制进镜像会触发传染性许可义务。扫描 `norma` / `artex` 的特征标识。

`refs/` 的定位是**只读研究资料，仅供人读源码借鉴架构**，构建上下文中必须通过 `.dockerignore` 显式排除，且 CI 检查独立验证它确实没进任何一层。详见 [architecture.md](architecture.md) §10.4。

检查范围覆盖**所有镜像层**与最终文件系统（`docker save` 后逐层扫，或 `docker history` + 导出 rootfs 扫描），而不只是最终层 —— 中间层里 COPY 进去又删掉的文件仍然留在镜像里，会被 `docker save` 一起打包上传。

## 7. 导出与上传

```
docker build -t tsecbench-agent:latest .
# 泄漏检查（构建流程的一部分）
docker save tsecbench-agent:latest | gzip > agent.tar.gz
ls -lh agent.tar.gz        # 必须 ≤ 3GB
```

上传后在平台页面配置运行时环境变量，见 [runbook.md](runbook.md) §3。
