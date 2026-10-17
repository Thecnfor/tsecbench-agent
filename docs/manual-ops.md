# 手动运维手册（评测全流程）

> 面向人手动操作；CI/自动化场景同样适用。凭据红线见 AGENTS.md：
> `ing/`（token + VPN 私钥）绝不 commit、绝不进镜像。

## 0. 每次平台新下发后：放凭据

两个文件，格式严格：

```
# ing/env —— 冒号分隔，不是 dotenv 的 = 号
BENCHMARK_TOKEN:<新 token>
BENCHMARK_BASE_URL:https://tsecbench.zc.tencent.com

# VPN 配置原样放进 ing/
cp ~/Downloads/task_XXXX_vpn_config.ovpn ing/
chmod 600 ing/env ing/*.ovpn
```

一键替代：打开 debug-ui（见 §4），左侧填入 token / base_url / ovpn 正文
（或选择文件）/ sudo 密码，点「一键连接」（等价于上面 + §1 的 openvpn 拉起）。

## 1. 起 VPN 并**验证数据面**（不可跳过）

```bash
sudo openvpn --config ing/task_XXXX_vpn_config.ovpn --daemon
sleep 10
ip -br a | grep tun                 # 应出现 tun0 + 10.254.x.x
ip route | grep 10.0.128.0          # 应有 10.0.128.0/18 via tun0
ping -c1 -w2 10.254.0.1 \
  && nc -zv <目标任务IP> <端口>      # ★ 数据面验证：ping 不通就不要进入下一步
```

2026-10-17 实测过的故障形态：控制面全绿（TLS、认证、路由推送都成功），
但隧道内 `ping 10.254.0.1` 零回包 —— 属于官方侧数据面故障，客户端重连无效，
只能等待或要求平台重新下发。

## 2. 接入自检（只读，零副作用）

```bash
make check
```

输出应为 `平台 API 连通 OK 题目总数=63 ...`。

## 3. 起评测

```bash
make run          # 前台；Ctrl-C 停止
```

默认已生效的优化（无需开关）：
- 分级模型：新题/非 hard 用 deepseek-chat 快扫，hard 或失败重投升档 deepseek-reasoner
- TCP 就绪预检（TARGET_READY_TIMEOUT_S=120，冷启容器先等再放 solver）
- 失败题 priority 降权（新题优先吃并发槽）
- KB 注入（solver/kb/*.md 实战条目）+ EvoSOP 蒸馏
- pi 悬空 tool_calls 消毒补丁（.pi-vendor 重装后需重打，见 solver/THIRD_PARTY.md）

## 4. 观测台

```bash
uv run python debug-ui/live.py --port 7879     # 若未在跑
# → http://127.0.0.1:7879/
```

任务表（模型档/轮次/LLM 错误/flags/静默秒数）、点题看对话轨迹与攻击面图、
journal 事件流、单题/全量日志下载。

## 5. 停机

```bash
pkill -f "python -m orchestrator"
sudo pkill openvpn
```

## 6. 排障速查

| 现象 | 处置 |
|---|---|
| `AUTH_FAILED: Exceeded number of clients` | 等 30-60s 让服务端回收旧会话；确认没有第二个 openvpn 进程 |
| tun0 在但目标任务不通 | 先 `ping 10.254.0.1`；不通 = 官方数据面故障，等待/重下发 |
| 编排器存活但零事件 | debug-ui 看「静默(s)」；journal 查 `pipeline_error` / `list_failed` |
| 平台域名解析抽风 | `/etc/hosts` 钉 `106.53.137.227 tsecbench.zc.tencent.com`；官方换 IP 时更新 |
| solver 400 `tool_calls must be followed` | 确认 .pi-vendor 两处消毒补丁在位（THIRD_PARTY.md） |
