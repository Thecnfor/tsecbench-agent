#!/usr/bin/env python3
"""kb 检索引擎回归评测（stdlib-only）。

两套评测，同时报告：
1) 无偏抽样（治"越调越像某道题"）：从 INDEX.tsv 随机抽 N 条标题当查询，
   期望命中同一文件——抽样与种子固定，改动前后可比。
2) 人工样例（kb_gold.tsv）：领域风格样例，检查典型场景不回退。

用法：
  python3 kb_eval.py --index /tmp/opencode/kbindex [--n 600] [--gold .../kb_gold.tsv]
退出码恒为 0（它是测量仪不是断言器）；拿最后一行数字做前后对比即可。
"""
from __future__ import annotations

import argparse
import random
import re
import sqlite3
import sys
from pathlib import Path

CJK_RUN = re.compile(r"[一-鿿㐀-䶿]+|[^一-鿿㐀-䶿]+")
CJK = re.compile(r"[一-鿿㐀-䶿]+")

# 与 kb 查询端同构：中文二元切分 + 多词 OR
def to_match(q: str) -> str:
    toks: list[str] = []
    for run in CJK_RUN.findall(q):
        if not CJK.fullmatch(run):
            toks.extend(run.split())
        elif len(run) <= 2:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return " OR ".join(f'"{t}"' for t in toks if len(t) >= 2) or '""'


def topk(conn, query: str, kind: str | None, k: int = 5) -> list[str]:
    sql = ("SELECT d.path FROM fts f JOIN docs d ON d.id=f.docid WHERE fts MATCH ? ")
    ps: list = [to_match(query)]
    if kind:
        sql += "AND d.kind=? "
        ps.append(kind)
    elif kind is not None:            # 空串=与 kb find 默认一致：排除参考型数据
        sql += "AND d.kind NOT IN ('template','cve','kev') "
    sql += "ORDER BY bm25(fts) LIMIT ?"
    ps.append(k)
    try:
        return [r[0] for r in conn.execute(sql, ps)]
    except sqlite3.OperationalError:
        return []


def eval_auto(conn, idx: Path, n: int, seed: int) -> tuple[int, int, int]:
    rows = [l.rstrip("\n").split("\t") for l in open(idx / "INDEX.tsv", encoding="utf-8")]
    rows = [r for r in rows if len(r) == 4 and 3 <= len(r[3]) <= 60
            and "http" not in r[3] and len(r[3].split()) <= 12]
    random.seed(seed)
    sample = random.sample(rows, min(n, len(rows)))
    h1 = h5 = 0
    for path, _line, _level, title in sample:
        hits = topk(conn, title, "")
        h1 += bool(hits) and hits[0] == path
        h5 += path in hits
    return len(sample), h1, h5


def eval_gold(conn, gold_path: Path, verbose: bool) -> tuple[int, int, int]:
    if not gold_path.exists():
        return 0, 0, 0
    h1 = h5 = 0
    n = 0
    for ln in open(gold_path, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        query, rx = parts[0], parts[1]
        kind = parts[2] if len(parts) > 2 and parts[2] else ""
        n += 1
        hits = topk(conn, query, kind)
        ok1 = bool(hits) and re.search(rx, hits[0], re.I)
        ok5 = any(re.search(rx, p, re.I) for p in hits)
        h1 += bool(ok1)
        h5 += bool(ok5)
        if verbose and not ok5:
            print(f"  MISS {query}  →  {hits[:2] or '—'}")
    return n, h1, h5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=__import__("os").environ.get("KB_INDEX", "/opt/security-knowledge-base/.index"))
    ap.add_argument("--n", type=int, default=600, help="无偏抽样条数")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--gold", default=str(Path(__file__).parent / "kb_gold.tsv"))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    idx = Path(a.index)
    if not (idx / "kb.sqlite").exists():
        print(f"索引不存在: {idx}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{idx / 'kb.sqlite'}?mode=ro", uri=True)

    na, a1, a5 = eval_auto(conn, idx, a.n, a.seed)
    ng, g1, g5 = eval_gold(conn, Path(a.gold), a.verbose)
    print(f"无偏抽样 {na} 条：hit@1 {a1}/{na}={a1/na:.0%}  hit@5 {a5}/{na}={a5/na:.0%}")
    if ng:
        print(f"人工样例 {ng} 条：hit@1 {g1}/{ng}={g1/ng:.0%}  hit@5 {g5}/{ng}={g5/ng:.0%}")
    print(f"SUMMARY auto@1={a1/na:.3f} auto@5={a5/na:.3f} gold@1={g1/ng:.3f} gold@5={g5/ng:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
