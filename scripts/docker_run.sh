#!/usr/bin/env bash
# 本地托管仿真：在 Docker 内跑编排器，容器内拨 VPN，默认拒绝公网。
# 用法：make docker-run            （默认 NETGUARD=on, VPN_MODE=vpn）
#       VPN_MODE=none make docker-run  （无靶场时自检网络）
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f ing/env ] || { echo "missing ing/env" >&2; exit 1; }
set -a
[ -f solver/.env ] && . solver/.env
set +a
[ -n "${LLM_API_KEY:-}" ] || { echo "LLM_API_KEY missing" >&2; exit 1; }

# ing/env 是冒号分隔，只能在第一个冒号切
while IFS= read -r line; do
    case "$line" in
        BENCHMARK_TOKEN*|BENCHMARK_BASE_URL*)
            key=${line%%:*}
            value=${line#*:}
            export "$key=$value"
            ;;
    esac
done < ing/env

OVPN=$(ls ing/*.ovpn 2>/dev/null | head -1 || true)
VPN_MODE=${VPN_MODE:-vpn}
if [ "$VPN_MODE" = "vpn" ] && [ -z "$OVPN" ]; then
    echo "no ing/*.ovpn for VPN_MODE=vpn (use VPN_MODE=none for a network self-test)" >&2
    exit 1
fi

if ip link show tun0 >/dev/null 2>&1; then
    echo "WARN: host tun0 is up; stop the host VPN before using VPN_MODE=vpn" >&2
fi

# 宿主侧 LLM 网关（容器内只允许访问它，不允许直连公网）
if ! (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ':8080 '; then
    echo "[docker-run] starting host LLM gateway on 0.0.0.0:8080"
    GATEWAY_BIND=0.0.0.0 GATEWAY_PORT=8080 nohup python3 scripts/mock_gateway.py \
        >/tmp/tsec-gateway.log 2>&1 &
    sleep 1
fi

mkdir -p workspaces-docker
docker rm -f tsec-local >/dev/null 2>&1 || true

args=(
    --rm --name tsec-local
    --cap-add NET_ADMIN
    --device /dev/net/tun
    --add-host api.deepseek.com.tsecbench.gw:host-gateway
    -e BENCHMARK_TOKEN -e BENCHMARK_BASE_URL
    -e LLM_API_KEY -e "LLM_MODEL=${LLM_MODEL:-deepseek-chat}"
    -e GATEWAY_MODE=on
    -e LLM_BASE_URL=https://api.deepseek.com:8080
    -e "LLM_GATEWAY_PORT=8080"
    -e "VPN_MODE=$VPN_MODE"
    -e "NETGUARD=${NETGUARD:-on}"
    -e "CONCURRENCY_LIMIT=${CONCURRENCY_LIMIT:-3}"
    -e "TOTAL_TIME_LIMIT=${TOTAL_TIME_LIMIT:-21600}"
    -e "SLICE_MIN_S=${SLICE_MIN_S:-300}"
    -e "SLICE_MAX_S=${SLICE_MAX_S:-1800}"
    -v "$PWD/scripts/docker_entrypoint.sh:/entrypoint.sh:ro"
    -v "$PWD/workspaces-docker:/workspaces"
    --entrypoint /bin/sh
)
if [ "$VPN_MODE" = "vpn" ]; then
    args+=( -e "VPN_CONFIG=$(cat "$OVPN")" )
elif [ "$VPN_MODE" = "host" ]; then
    # 容器内 VPN 不可用时的兜底：共享宿主网卡（NETGUARD 无法执行，需人工确认）
    echo "WARN: VPN_MODE=host uses --network host and disables netguard" >&2
    args+=( --network host -e NETGUARD=off )
    args=( "${args[@]/NETGUARD=on/NETGUARD=off}" )
fi

exec docker run "${args[@]}" tsec-agent:latest /entrypoint.sh
