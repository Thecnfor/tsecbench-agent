# TSec Benchmark Agent

用 Python + OpenAI 兼容 SDK 实现的自动跑分 agent，运行于 **Docker（Kali Linux）** 容器内：
- 模型：`deepseek-flash`（网关 `http://api.deepseek.com.tsecbench.gw/v1`）
- 工具：仅一个 **异步 shell**（后台执行 + job 轮询），无 MCP、无 subagent
- 容器内预装渗透工具：nmap、masscan、whatweb、sqlmap、gobuster、ffuf、nikto、dirb、hydra、netcat、python3(含 requests) 等
- VPN 由容器入口脚本在**容器网络命名空间**内连接（对宿主机网络零影响），agent 只做预检

## 运行（推荐，全自动）

```bash
cd /home/xsf/Projects/tcbench/docker
./run.sh            # 自动选最新的 task_*_vpn_config.ovpn
./run.sh /path/to/vpn.ovpn   # 指定 vpn 配置
# 观察：
newgrp docker -c "docker logs -f tsec-agent"
```

`run.sh` 流程：构建镜像（若无）→ 启动容器 → entrypoint 连 VPN → 等 `http://10.0.100.58`
返回 `status:"ok"`（失败即退出）→ `--smoke` 冒烟（模型网关/shell/预检）→ 正式跑分。

## 手动构建/调试

```bash
cd docker
newgrp docker -c "docker build -t tsec-agent:latest ."
# 只做冒烟测试：
newgrp docker -c "docker run --rm --cap-add NET_ADMIN --device /dev/net/tun \
  -v \$PWD/../tsec_agent:/agent -v \$PWD/../task_*_vpn_config.ovpn:/config/vpn.ovpn:ro \
  -v \$PWD/work:/work \
  -e BENCHMARK_TOKEN=70b15867-d393-4e75-9c16-65479a2a97ff \
  tsec-agent:latest python3 /agent/agent.py --smoke"
```

## 环境变量（均有内置默认值）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BENCHMARK_TOKEN` | 平台 token | `70b15867-d393-4e75-9c16-65479a2a97ff` |
| `BENCHMARK_BASE_URL` | 平台地址 | `https://tsecbench.zc.tencent.com` |
| `DEEPSEEK_BASE_URL` | 模型网关 | `http://api.deepseek.com.tsecbench.gw/v1` |
| `DEEPSEEK_API_KEY` | 网关鉴权 | `tsec-bench` |
| `AGENT_MODEL` | 模型名 | `deepseek-flash` |
| `AGENT_MAX_TURNS` | 最大 LLM 轮次 | 400 |
| `AGENT_DEADLINE_SEC` | 总时限秒（0=不限） | 0 |
| `VPN_CONF` | 容器内 vpn 配置路径 | `/config/vpn.ovpn` |

## 结构

- `agent.py` — 主循环：调模型 → 执行工具 → 回填结果，直到完成/超时
- `shell_tool.py` — 异步 shell：`run` 立即返回 job_id，`wait/status/kill/list` 轮询；输出截断 32KB；进程组超时自动杀
- `prompt.py` — 系统提示词（跑分流程、错误处理、**按题型的渗透方法建议**、输出约定）
- `config.py` — 配置
- `selftest.py` — 离线端到端自测（fake 模型）
- `../docker/Dockerfile` + `entrypoint.sh` + `run.sh` — 容器打包与启动

## 宿主机上的依赖（一次性）

- Docker daemon 正常运行；用户在 `docker` 组（已加）
- `/etc/docker/daemon.json` 配了国内 registry-mirrors（解决 Docker Hub 直连超时）

## 注意

- entrypoint 在 VPN 预检失败时直接退出（exit 11），不会带病跑分。
- 模型网关域名 `api.deepseek.com.tsecbench.gw` 依赖 VPN 内网 DNS，
  entrypoint 会尝试从 openvpn push 中提取 DNS 写入 `/etc/resolv.conf`。
