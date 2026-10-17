#!/usr/bin/env bash
# 清理上一 run 的 solver 临时产物。只删已知文件名，不碰 /tmp 其他内容。
# 用法：新 run 前执行（make run 会自动调用）。
set -u
for name in cj cj2 cj3 cjA cj_admin cj_docpass admin2.txt admin.txt \
            notes.md notes2.md notes_final.md sas sas.txt g.py x o id_t \
            c001 src_download.txt src_upload.txt src_auth.txt src_config.txt; do
  rm -f "/tmp/$name" 2>/dev/null || true
done
rm -rf /tmp/gradio 2>/dev/null || true
echo "cleaned solver /tmp artifacts"
