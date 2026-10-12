#!/bin/bash
# 归档 bench 记录到 docker 目录之外（默认 ~/tsec_bench_runs/），使容器未来挂载范围永远读不到。
# 归档后源目录删除；归档包由用户自行管理数量与存留。
#
# 用法：
#   ./archive_runs.sh              # 归档 runs/ 下全部 bench
#   ./archive_runs.sh <TOKEN>...   # 归档指定 token 的 bench
set -euo pipefail

cd "$(dirname "$0")"
RUNS="$PWD/runs"
DEST="${ARCHIVE_DIR:-$HOME/tsec_bench_runs}"
mkdir -p "$DEST"

if [ ! -d "$RUNS" ] || [ -z "$(ls -A "$RUNS" 2>/dev/null)" ]; then
    echo "nothing to archive: $RUNS is empty"
    exit 0
fi

archive_one() {
    local token="$1"
    local src="$RUNS/$token"
    [ -d "$src" ] || { echo "skip (not found): ${token:0:8}..."; return; }
    # 运行中的容器还挂着该目录 → 拒绝归档
    local cid
    cid=$(docker ps -q --filter "volume=$src" 2>/dev/null || true)
    if [ -n "$cid" ]; then
        echo "REFUSED: bench ${token:0:8}... is in use by container $cid (stop it first)"
        return
    fi
    local ts out
    ts=$(date +%Y%m%d-%H%M%S)
    out="$DEST/${token}_${ts}.tar.gz"
    tar -czf "$out" -C "$RUNS" "$token"
    rm -rf "$src"
    echo "archived: ${token:0:8}... -> $out (source removed)"
}

if [ $# -gt 0 ]; then
    for t in "$@"; do archive_one "$t"; done
else
    for d in "$RUNS"/*/; do
        [ -d "$d" ] || continue
        archive_one "$(basename "$d")"
    done
fi

echo "archives in $DEST (数量/存留由你自行管理):"
ls -lh "$DEST" 2>/dev/null | tail -n +2 || true
