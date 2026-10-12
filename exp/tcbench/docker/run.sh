#!/bin/bash
# 启动 tsec-agent 容器（本地模式开发调试用；托管模式由官方平台自动注入变量，不走本脚本）：
#  - 凭证/模式开关统一读项目根 .env（gitignore 排除），不硬编码
#  - bench 隔离：只挂载当前 token 的 bench 目录 runs/<TOKEN>:/work —— 容器内只有当前
#    bench 的记录/任务文件，其他 bench 不在挂载内（天然"bench 内可读、bench 外不可读"）。
#    旧 bench 用 archive_runs.sh 归档到 docker 目录外。
#  - 容器内跑 openvpn（NET_ADMIN + /dev/net/tun，只作用于容器网络命名空间，不改宿主机网络）
set -euo pipefail

cd "$(dirname "$0")"
PROJECT=/home/xsf/Projects/tcbench
ENV_FILE="$PROJECT/.env"
VPN_CONF="${1:-$(ls -t "$PROJECT"/task_*_vpn_config.ovpn | head -1)}"

[ -f "$ENV_FILE" ] || { echo "FATAL: $ENV_FILE not found"; exit 1; }
TOKEN=$(grep '^BENCHMARK_TOKEN=' "$ENV_FILE" | cut -d= -f2)
[ -n "$TOKEN" ] || { echo "FATAL: BENCHMARK_TOKEN empty in .env"; exit 1; }

BENCH_DIR="$PWD/runs/$TOKEN"
mkdir -p "$BENCH_DIR"
echo "bench dir: $BENCH_DIR (mounted as /work)"
echo "using vpn config: $VPN_CONF"

[ -f work/openvpn.log ] 2>/dev/null || touch "$BENCH_DIR/openvpn.log" 2>/dev/null || true

CONTAINER="tsec-agent-$(echo "$TOKEN" | cut -c1-8)"

newgrp docker -c "
docker rm -f '$CONTAINER' 2>/dev/null || true
docker run -d \
  --name '$CONTAINER' \
  --cap-add NET_ADMIN \
  --cap-add NET_RAW \
  --device /dev/net/tun \
  --env-file '$ENV_FILE' \
  -v '$PROJECT/tsec_agent:/agent' \
  -v '$VPN_CONF:/config/vpn.ovpn:ro' \
  -v '$BENCH_DIR:/work' \
  tsec-agent:latest
"
echo "started. logs: docker logs -f $CONTAINER"
echo "records:  $BENCH_DIR"
echo "archive:  ./archive_runs.sh $TOKEN   (归档到 docker 目录外)"
