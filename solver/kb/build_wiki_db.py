#!/usr/bin/env python3
"""构建知识库 FTS 数据库（构建期一次性；运行期零索引开销）。

输入：若干语料根目录（hacktricks/ctf-wiki/payloadsallthethings/Skills 等）
输出：<kb_dir>/unified.db —— SQLite FTS5（tokens 列存英中混合分词结果）

用法：python3 solver/kb/build_wiki_db.py /tmp/kb-src ... --out solver/kb/unified.db

规矩：
  - 只收 .md/.txt；跳过 AppleDouble（._*）、>256KB 巨文件、隐藏目录
  - flag 字面量一律 <FLAG> 化（红线：镜像不含 flag 字面量）
  - 每节正文上限 4000 字符（检索命中后注入 solver 的片段另有 1200 截断）
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from index import _split_sections, _tokenize  # 同一套分词，查询/建库对称

FLAG_RE = re.compile(r"(?:flag|FLAG|ctf|CTF|HTB|htb|[A-Za-z0-9_]{2,24})\{[^}\n]{4,120}\}")
# 宽松探测：任何 flag-ish 前导 + 花括号/下划线形态 —— 命中即整节丢弃（宁缺毋滥）
FLAG_ISH_RE = re.compile(r"(?:flag|FLAG|HTB|htb|ctf|CTF)[{_]")
MAX_FILE_BYTES = 256 * 1024
MAX_BODY_CHARS = 4000


def iter_docs(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if name.startswith("."):
            continue
        if p.suffix.lower() not in (".md", ".txt"):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--out", default="solver/kb/unified.db")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE secs(doc TEXT, title TEXT, body TEXT)")
    db.execute("CREATE VIRTUAL TABLE ft USING fts5(tokens)")
    db.execute("CREATE TABLE rowmap(rid INTEGER PRIMARY KEY, sec INTEGER)")

    n_docs = n_secs = 0
    for root_s in args.roots:
        root = Path(root_s)
        if not root.is_dir():
            continue
        for doc in iter_docs(root):
            n_docs += 1
            try:
                text = doc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text = FLAG_RE.sub("<FLAG>", text)
            rel = str(doc.relative_to(root))[:300]
            for title, body in _split_sections(text):
                body = body[:MAX_BODY_CHARS]
                if len(body) < 40:
                    continue
                if FLAG_ISH_RE.search(title + "\n" + body):
                    continue  # fail-closed：疑似 flag 字面量整节不入库
                cur = db.execute(
                    "INSERT INTO secs(doc,title,body) VALUES(?,?,?)", (rel, title[:200], body)
                )
                db.execute(
                    "INSERT INTO rowmap(rid,sec) VALUES(?,?)", (cur.lastrowid, cur.lastrowid)
                )
                toks = " ".join(_tokenize(title + "\n" + body))
                db.execute("INSERT INTO ft(rowid,tokens) VALUES(?,?)", (cur.lastrowid, toks))
                n_secs += 1
            if n_docs % 2000 == 0:
                db.commit()
                print(f"... {n_docs} docs, {n_secs} sections", file=sys.stderr)
    db.commit()
    db.execute("INSERT INTO ft(ft) VALUES('optimize')")
    db.commit()
    db.close()
    size_mb = out.stat().st_size / 1e6
    print(f"done: {n_docs} docs, {n_secs} sections, {size_mb:.0f} MB -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
