#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 镜像导出 + 体积闸门 —— image-spec.md §7 / runbook.md §3.1 的可执行版本
#
#   scripts/export_image.sh [IMAGE_REF] [OUT.tar.gz]
#
# 本地和 CI 跑的是同一条管线、同一份阈值 —— 这是刻意的：口径漂移（本地 2.9G、
# CI 3.1G）在交付当天才发现，是这类项目最贵的失败模式。
#
# 四件事：
#   1. docker save 落成未压缩 tar —— A14 要的「解压后大小」就是这个数
#   2. 压成合法 .tar.gz（平台只收 .tar/.tar.gz/.tgz，见 image-spec.md §3 硬约束）
#   3. 两个口径都报：压缩后（硬闸门）+ 解压后（A14 未答复，先留数）
#   4. 超限非零退出，并按 §4.1 降级阶梯给出该砍哪几档
#
# 退出码：0 通过 · 1 超限 · 2 用法/环境错误
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

IMAGE_REF="${1:-tsecbench-agent:latest}"
OUT="${2:-agent.tar.gz}"

# 3GB 取十进制口径（3,000,000,000）。若平台实际按 GiB 判，这个更严的阈值只让我们
# 少用 221MB 余量；反过来（按 GiB 判、按 GiB 卡）则会出现「本地通过、上传被拒」。
LIMIT_BYTES="${LIMIT_BYTES:-3000000000}"
WARN_PCT="${WARN_PCT:-90}"

TAR="${OUT%.gz}.uncompressed.tar"
WARN_BYTES=$(( LIMIT_BYTES * WARN_PCT / 100 ))

# ── 输出：stdout 给人看，GITHUB_STEP_SUMMARY 给 CI 摘要 ──────────────────────
say()  { printf '%s\n' "$*"; }
note() { printf '%s\n' "$*"; [ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"; return 0; }

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || printf '%sB' "$1"; }

# ── 压缩器：在「合法 gzip」这个硬约束内挑压缩率最高的 ────────────────────────
# 平台只收 .tar/.tar.gz/.tgz，所以 zstd/xz 用不了 —— 它们的压缩率优势（20~50%）
# 在本项目里拿不到。剩下能动的只有 deflate 编码器的实现质量：
#   libdeflate -12  ≈ zopfli 的压缩率（比 gzip -9 小 ~4%），但快一个数量级，
#                     输出是标准 gzip 流，平台侧无感知
#   pigz -9 -b 1024 并行，压缩率≈gzip -9（块边界重置字典，差千分之几）
#   gzip -9         兜底
pick_compressor() {
  if command -v libdeflate-gzip >/dev/null 2>&1; then echo libdeflate
  elif command -v pigz >/dev/null 2>&1;          then echo pigz
  else                                                echo gzip
  fi
}

# ── §4.1 降级阶梯 ────────────────────────────────────────────────────────────
# 每行：<档位>|<压缩后省下 MB>|<砍什么>|<代价>|<必须同砍的档位，可空>
# 数字直接取自 image-spec.md §4.1 的表；改表就改这里，不要另写一份。
readonly TRIM_LADDER=(
  "1|120|build-essential / gcc 移出 runtime|现场无法编译 exploit|"
  "2|200|playwright + chromium|失去 JS 渲染与 DOM 交互，Web 题胜率下降|"
  "3|420|Ghidra + JDK 21|逆向题退化为 rizin 反汇编喂 LLM|"
  "4|180|REA|与 3 必须同砍，否则 Ghidra provider 空转|3"
  "5|40|nuclei-templates 只留 web/ 与 CVE 高频子集|漏洞覆盖变窄|"
)

print_full_ladder() {
  note "| 档 | 省下(压缩后) | 砍什么 | 代价 |"
  note "|---|---|---|---|"
  local row tier mb what cost _rest
  for row in "${TRIM_LADDER[@]}"; do
    IFS='|' read -r tier mb what cost _rest <<< "$row"
    note "| $tier | ${mb}MB | $what | $cost |"
  done
}

suggest_trim() {
  local over_bytes="$1"
  local over_mb=$(( (over_bytes + 1048575) / 1048576 ))

  note ""
  note "### 超限处置：需要砍掉约 $(human "$over_bytes")（压缩后）"
  note ""
  note "完整降级阶梯（image-spec.md §4.1，从先砍到后砍）："
  note ""
  print_full_ladder

  # TODO(owner) ──────────────────────────────────────────────────────────────
  # 上面只是把阶梯打出来，真正有用的是「按这个超限量，具体该砍哪几档」。
  # 这段逻辑归你 —— 它是 image-spec.md §4.1 那段散文的可执行化，也是本脚本
  # 唯一一处「砍到哪算够」的判断。
  #
  # 契约：读 $over_bytes，输出被选中档位的编号（空格分隔）到 stdout，例如 "1 2"。
  # 阶梯表里的数字是 MB，比较前自行换算（over_mb 上面已经算好了）。
  # 要处理的约束：
  #   · 阶梯按「分数损失 / 省下体积」排序，从 1 开始贪心累加即可；
  #   · 第 4 档 REA 与第 3 档 Ghidra 必须同砍 —— 选 4 就得带上 3，选 3 也建议带 4；
  #   · 第 1 档 build-essential 省得最少但代价「不小，尽量别砍」，
  #     是否值得为了 120MB 就牺牲现场编译能力，是你的判断；
  #   · 累加到省下的 MB ≥ over_mb 为止；全砍完仍不够就说明该改的是工具清单，
  #     不是阶梯 —— 这种情况要显式说出来，别静默返回空。
  #
  # 参考实现约 10 行。写完把下面的 fallback 删掉。
  # ──────────────────────────────────────────────────────────────────────────
  note ""
  note "（自动选档尚未实现 —— 见 scripts/export_image.sh 的 suggest_trim() TODO）"
}

# ── 前置检查 ────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { say "错误：找不到 docker"; exit 2; }
docker image inspect "$IMAGE_REF" >/dev/null 2>&1 \
  || { say "错误：镜像不存在：$IMAGE_REF（先 docker build，见 runbook.md §3.1）"; exit 2; }

COMPRESSOR="$(pick_compressor)"
case "$COMPRESSOR" in
  libdeflate) COMPRESS_CMD=(libdeflate-gzip -12 -c) ;;
  pigz)       COMPRESS_CMD=(pigz -9 -b 1024 -p "$(nproc)" -c) ;;
  gzip)       COMPRESS_CMD=(gzip -9 -c) ;;
esac

say "镜像   ：$IMAGE_REF"
say "输出   ：$OUT"
say "压缩器 ：$COMPRESSOR"
say "阈值   ：$(human "$LIMIT_BYTES")（警告线 $(human "$WARN_BYTES")）"
say ""

# ── 1/3 导出未压缩 tar ──────────────────────────────────────────────────────
say "── 1/3 docker save ────────────────────────────────────────────────"
docker save -o "$TAR" "$IMAGE_REF"
TAR_BYTES=$(stat -c%s "$TAR")
say "解压后（未压缩 tar）：$(human "$TAR_BYTES")  [$TAR_BYTES bytes]"

# ── 2/3 压缩 ────────────────────────────────────────────────────────────────
say ""
say "── 2/3 压缩（$COMPRESSOR）────────────────────────────────────────────"
"${COMPRESS_CMD[@]}" < "$TAR" > "$OUT"
OUT_BYTES=$(stat -c%s "$OUT")
say "压缩后：$(human "$OUT_BYTES")  [$OUT_BYTES bytes]"

RATIO=$(awk -v a="$TAR_BYTES" -v b="$OUT_BYTES" 'BEGIN{ if(b>0) printf "%.2f", a/b; else print "0" }')
say "压缩比：${RATIO}×"

# ── 3/3 逐层体积排行（超限时告诉你砍哪儿）───────────────────────────────────
say ""
say "── 3/3 逐层体积排行（Top 15）─────────────────────────────────────"
docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' "$IMAGE_REF" \
  | sort -h -r | head -15 | cut -c1-140 || true

# ── 闸门 ────────────────────────────────────────────────────────────────────
say ""
note "## 镜像体积闸门"
note ""
note "| 口径 | 实测 | 阈值 | 说明 |"
note "|---|---|---|---|"
note "| 压缩后 tar.gz（**硬闸门**） | $(human "$OUT_BYTES") | ≤ $(human "$LIMIT_BYTES") | 平台上传硬约束（image-spec.md §3） |"
note "| 解压后 | $(human "$TAR_BYTES") | — | A14 未答复，两个口径都要有数（runbook.md §3.1） |"
note ""
note "压缩比 ${RATIO}× · 压缩器 \`$COMPRESSOR\`"

if [ "$OUT_BYTES" -gt "$LIMIT_BYTES" ]; then
  OVER=$(( OUT_BYTES - LIMIT_BYTES ))
  say ""
  say "✗ 超限：$(human "$OUT_BYTES") > $(human "$LIMIT_BYTES")，超出 $(human "$OVER")"
  suggest_trim "$OVER"
  note ""
  note "**结论：超限，构建失败。**"
  rm -f "$TAR"
  exit 1
fi

if [ "$OUT_BYTES" -gt "$WARN_BYTES" ]; then
  say ""
  say "⚠ 已用掉 $(( OUT_BYTES * 100 / LIMIT_BYTES ))% 配额，余量 $(human $(( LIMIT_BYTES - OUT_BYTES )))"
  note ""
  note "⚠ 余量仅 $(human $(( LIMIT_BYTES - OUT_BYTES ))) —— 工具清单再增项就会破线。"
fi

say ""
say "✓ 通过：$(human "$OUT_BYTES") / $(human "$LIMIT_BYTES")（余量 $(human $(( LIMIT_BYTES - OUT_BYTES )))）"
note ""
note "**结论：通过**，余量 $(human $(( LIMIT_BYTES - OUT_BYTES )))。"

rm -f "$TAR"
exit 0
