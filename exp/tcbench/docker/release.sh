#!/bin/bash
# 平台交付镜像一键脚本：检查+编译 dist → 构建镜像 → 导出 gzip → ≤3GB 压缩口径校验。
#
# 固化 AGENTS 两条纪律（人肉步骤容易漏）：
#   1. **先 pnpm build 再 build 镜像**——dist 是 gitignore 构建产物，忘了编译就 save
#      会导出旧 dist（Dockerfile 的 node --check 只验证"存在"，验不出"旧"）；
#   2. **交付体积 ≤3GB 是 push 上传的压缩口径**（docker save | gzip -1 后实测），
#      不是 docker images 的原始数字——本脚本导出后自动测量、超限即失败。
#
# 用法：
#   ./release.sh            # 全量：pnpm check（typecheck+biome+全部测试）+ 编译 + 构建 + 导出
#   ./release.sh --fast     # 跳过 pnpm check（已自查过的快速重导）
#   RELEASE_OUT=/path/x.tar.gz ./release.sh   # 自定义导出路径
#
# 产物：docker/tsec-agent.tar.gz（*.tar.gz 已 gitignore，不入库）
set -euo pipefail

cd "$(dirname "$0")" # docker/
PROJECT="$(cd .. && pwd)" # 项目根 = exp/tcbench（构建上下文；其 .dockerignore 排除凭证/记录）
AGENT="$PROJECT/tsec_agent"
SELF="$PWD/$(basename "$0")"
OUT="${RELEASE_OUT:-$PWD/tsec-agent.tar.gz}"
LIMIT_BYTES=3000000000 # ≤3GB：按 3×10^9 字节保守判定（push 上传口径）

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

# docker 组自举：宿主新开 shell 需 newgrp 才有权限（AGENTS 环境注意）；
# 重入一次为限，防 newgrp 后仍无权限时死循环。
if ! docker info >/dev/null 2>&1; then
  command -v newgrp >/dev/null || { echo "FATAL: 无 docker 权限且找不到 newgrp"; exit 1; }
  [ "${RELEASE_DOCKER_RETRIED:-0}" = "1" ] && { echo "FATAL: newgrp 后 docker 仍不可用"; exit 1; }
  exec newgrp docker -c "RELEASE_DOCKER_RETRIED=1 bash '$SELF' $*"
fi

# 导出中途失败（磁盘满/管道断）不留半截文件冒充交付物
trap '[ $? -ne 0 ] && rm -f "$OUT"' EXIT

echo "==> [1/4] 检查 + 编译 dist（先 pnpm build 再 build 镜像）"
if [ "$FAST" -eq 0 ]; then
  (cd "$AGENT" && pnpm check) # typecheck + biome + 全部测试；带病不交付
else
  echo "    (--fast 跳过 pnpm check)"
fi
(cd "$AGENT" && pnpm build)

echo "==> [2/4] 构建镜像（上下文=$(basename "$PROJECT")/，层缓存；镜像内 node --check 硬验证 dist）"
# 预警：记录残留会被 COPY 进镜像（Dockerfile 尾部有 rm 清理，但根治在宿主侧）
if [ -d "$AGENT/tasks" ]; then
  echo "    ⚠ 警告: $AGENT/tasks 存在（历史记录残留会被 COPY——Dockerfile 会清，建议宿主先删）"
fi
docker build -f Dockerfile -t tsec-agent:latest "$PROJECT/."

echo "==> [3/4] 导出交付镜像（docker save | gzip -1；10GB 级原始 → 1~3 分钟）"
rm -f "$OUT"
docker save tsec-agent:latest | gzip -1 -c > "$OUT"

echo "==> [4/4] 体积校验（≤3GB = push 上传压缩口径）"
BYTES=$(wc -c < "$OUT")
GB=$(awk "BEGIN{printf \"%.2f\", $BYTES/1000000000}")
GIB=$(awk "BEGIN{printf \"%.2f\", $BYTES/1073741824}")
RAW=$(docker image inspect tsec-agent:latest --format '{{.Size}}')
RAW_GB=$(awk "BEGIN{printf \"%.2f\", $RAW/1000000000}")
GIT_REV=$(git -C "$PROJECT" rev-parse --short HEAD 2>/dev/null || echo "nogit")
echo "    commit:  ${GIT_REV}"
echo "    原始镜像: ${RAW_GB} GB（10^9 口径）"
echo "    交付压缩: ${GB} GB / ${GIB} GiB（gzip -1）　限 ${LIMIT_BYTES} 字节"
if [ "$BYTES" -gt "$LIMIT_BYTES" ]; then
  echo "❌ 超限 ${GB} GB > 3.00 GB —— 不可交付，按 image-spec.md §4 降阶梯瘦身（seclists/裁剪/去大件）后重跑"
  rm -f "$OUT"
  exit 1
fi
echo "✅ 交付就绪: $OUT（${GB} GB）→ 平台上传"
