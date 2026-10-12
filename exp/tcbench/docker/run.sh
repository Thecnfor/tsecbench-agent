#!/bin/bash
# 启动 tsec-agent 容器（本地模式开发调试用；托管模式由官方平台自动注入变量，不走本脚本）：
#  - 凭证/模式开关统一读项目根 .env（gitignore 排除），不再硬编码
#  - 容器内跑 openvpn（NET_ADMIN + /dev/net/tun，只作用于容器网络命名空间，不改宿主机网络）
#  - 挂载 agent 代码与 vpn 配置，日志写入 /work（宿主机 ./work）
set -euo pipefail

cd "$(dirname "$0")"
PROJECT=/home/xsf/Projects/tcbench
ENV_FILE="$PROJECT/.env"
VPN_CONF="${1:-$(ls -t "$PROJECT"/task_*_vpn_config.ovpn | head -1)}"
echo "using vpn config: $VPN_CONF"
[ -f "$ENV_FILE" ] || { echo "FATAL: $ENV_FILE not found (BENCHMARK_TOKEN etc. live there)"; exit 1; }

mkdir -p work
[ -f work/openvpn.log ] || touch work/openvpn.log

newgrp docker -c "
docker rm -f tsec-agent 2>/dev/null || true
docker run -d \
  --name tsec-agent \
  --cap-add NET_ADMIN \
  --cap-add NET_RAW \
  --device /dev/net/tun \
  --env-file '$ENV_FILE' \
  -v '$PROJECT/tsec_agent:/agent' \
  -v '$VPN_CONF:/config/vpn.ovpn:ro' \
  -v '$PWD/work:/work' \
  tsec-agent:latest
"
echo "started. logs: docker logs -f tsec-agent  |  work/: ./work"
