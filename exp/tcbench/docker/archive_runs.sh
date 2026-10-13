#!/bin/bash
# 归档 bench 记录到 runs/ 之外（默认 docker/archives/，项目内但在挂载范围外、gitignore 排除），
# 使容器未来挂载范围永远读不到；归档成功后源目录删除，归档包存留自行管理
# （ARCHIVE_DIR 可改目标目录）。
#
# root 残留兜底：容器内 root 产物（openvpn 的 0600 日志、root 属主的 tasks/ 目录）宿主机
# 用户读不动也删不掉；run.sh 已预建 openvpn.log 规避，这里在打包/删除失败时再用
# tsec-agent 镜像以 root 把属主归位成当前用户后重试（历史残留目录同样可归档）。
#
# 用法：
#   ./archive_runs.sh              # 归档 runs/ 下全部 bench
#   ./archive_runs.sh <TOKEN>...   # 归档指定 token 的 bench
set -euo pipefail

cd "$(dirname "$0")"
RUNS="$PWD/runs"
DEST="${ARCHIVE_DIR:-$PWD/archives}"
mkdir -p "$DEST"

IMAGE="${ARCHIVE_IMAGE:-tsec-agent:latest}"

# docker 组变更不回溯到已有会话（见 AGENTS.md），统一走 newgrp 调用
docker_sh() { newgrp docker -c "$1"; }

# 用镜像以 root 把目录属主归位成当前用户（tar 读 / rm 删 恢复可用）
chown_as_user() {
    local dir="$1"
    docker_sh "docker run --rm --entrypoint /bin/bash \
        --env HOST_UID=$(id -u) --env HOST_GID=$(id -g) \
        -v '$dir:/work' '$IMAGE' -c 'chown -R \$HOST_UID:\$HOST_GID /work'"
}

if [ ! -d "$RUNS" ] || [ -z "$(ls -A "$RUNS" 2>/dev/null)" ]; then
    echo "nothing to archive: $RUNS is empty"
    exit 0
fi

archive_one() {
    local token="$1"
    local src="$RUNS/$token"
    [ -d "$src" ] || { echo "skip (not found): ${token:0:8}..."; return 0; }
    # 运行中的容器还挂着该目录 → 拒绝归档
    local cid
    cid=$(docker_sh "docker ps -q --filter 'volume=$src'" 2>/dev/null || true)
    if [ -n "$cid" ]; then
        echo "REFUSED: bench ${token:0:8}... is in use by container $cid (stop it first)"
        return 0
    fi
    local ts out err
    ts=$(date +%Y%m%d-%H%M%S)
    out="$DEST/${token}_${ts}.tar.gz"
    err=$(mktemp)
    if tar -czf "$out" -C "$RUNS" "$token" 2>"$err"; then
        :
    else
        # 打包失败：多半是容器内 root 产物读不动 → docker 以 root 归位属主后重试
        echo "note: 直接打包失败（root 属主/0600 文件），改用 docker 归位属主: ${token:0:8}..."
        if ! chown_as_user "$src" || ! tar -czf "$out" -C "$RUNS" "$token" 2>"$err"; then
            echo "FAILED: 打包失败: ${token:0:8}..."
            sed 's/^/    /' "$err"
            echo "    可手动处理: sudo chown -R $(id -u):$(id -g) '$src' 后重跑归档"
            rm -f "$err" "$out"
            return 1
        fi
    fi
    rm -f "$err"
    # 归档已含全部内容，删除源目录；root 属主子目录删不动时同样归位后重试
    if ! rm -rf "$src" 2>/dev/null; then
        if ! chown_as_user "$src" || ! rm -rf "$src"; then
            echo "FAILED: 已打包但源目录删除失败: $src"
            echo "    归档包完好: $out"
            echo "    可手动处理: sudo rm -rf '$src'"
            return 1
        fi
    fi
    echo "archived: ${token:0:8}... -> $out (source removed)"
}

fail=0
if [ $# -gt 0 ]; then
    for t in "$@"; do archive_one "$t" || fail=1; done
else
    for d in "$RUNS"/*/; do
        [ -d "$d" ] || continue
        archive_one "$(basename "$d")" || fail=1
    done
fi

echo "archives in $DEST (数量/存留由你自行管理):"
ls -lh "$DEST" 2>/dev/null | tail -n +2 || true
if [ "$fail" != 0 ]; then
    echo "有 bench 归档失败（原因见上方提示）"
    exit 1
fi
