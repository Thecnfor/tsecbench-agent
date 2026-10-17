#!/usr/bin/env python3
"""离线知识库索引构建器（stdlib-only）。

产出三件套（默认写到 <KB>/.index/）：
  kb.sqlite  FTS5(trigram) 全文索引，按标题切块，kind 分流
  INDEX.tsv  全部 markdown 标题倒排（路径\t行号\t层级\t标题）
  CONST.tsv  代码/模板里的十六进制常数倒排（常数\t出现次数\t路径:行号，取首次）

设计取舍：
- 不分词（无 jieba），FTS5 用 trigram tokenizer，中英文子串都能命中；
- atlas-data 这类纯数据 dump 不进全文层，只抽 ATT&CK 技术号进签名层；
- nuclei 模板单独 kind=template，检索时可过滤，避免污染教程排序。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

TEXT_EXT = {".md", ".yaml", ".yml", ".py", ".txt"}
SKIP_DIR = {".git", "node_modules", "__pycache__", "images", "figure", "figures", "dist"}
CHUNK_LIMIT = 3000                  # 单块正文上限（字符）
INSERT_BATCH = 400

HEX_RE = re.compile(r"(?<![0-9a-zA-Z])(?:0[xX])?([0-9a-fA-F]{8,16})(?![0-9a-fA-F])")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
CJK_RUN_RE = re.compile(r"[一-鿿㐀-䶿]+|[^一-鿿㐀-䶿]+")
CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")


def seg(text: str) -> str:
    """中文按二元切分（bigram），其余原样——配合 unicode61 分词器实现子串级召回。
    trigram 分词器要求 token ≥3 字符，两字中文词（逆向/注入/隧道）会静默失配，故弃用。"""
    out = []
    for run in CJK_RUN_RE.findall(text):
        if not CJK_RE.fullmatch(run):            # 非中文原样
            out.append(run)
        elif len(run) <= 2:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return " ".join(out)


def classify(rel: Path) -> str:
    s = str(rel)
    if "02-nuclei-templates" in s:
        return "template"
    if "01-awesome-poc" in s:
        return "poc"
    if rel.suffix in {".md", ""}:
        return "tutorial"
    if rel.suffix in {".py", ".txt"}:
        return "code"
    return "doc"


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in TEXT_EXT:
                yield p
            elif not p.suffix:
                try:
                    if p.stat().st_size >= 200_000:
                        continue
                    head = p.open("rb").read(4096)
                except OSError:                      # 断链符号链接等
                    continue
                if b"\0" not in head:
                    yield p


def chunk_md(text: str):
    """按标题切块；小节合并到 CHUNK_LIMIT；返回 (heading, start_line, body)。
    heading 是完整父级链（用于检索与定位），层级用真实级别号比较，不能用标题长度。"""
    lines = text.splitlines()
    sections, cur, cur_line = [], [], 1
    heading, stack = "", []            # stack: [(level, title), ...]
    for i, ln in enumerate(lines, 1):
        m = HEADING_RE.match(ln)
        if m:
            if cur:
                sections.append((heading or (lines[0][:80] if lines else ""), cur_line,
                                 "\n".join(cur)))
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading, cur, cur_line = " > ".join(t for _, t in stack), [], i
        else:
            cur.append(ln)
    if cur:
        sections.append((heading, cur_line, "\n".join(cur)))

    merged = []
    for h, ln, body in sections:
        body = body.strip()
        if not body:
            continue
        if merged and len(merged[-1][2]) + len(body) < CHUNK_LIMIT:
            ph, pln, pb = merged[-1]
            merged[-1] = (ph, pln, pb + "\n" + body)
        elif len(body) > CHUNK_LIMIT:  # 超长无标题块硬切
            for j in range(0, len(body), CHUNK_LIMIT):
                merged.append((h, ln + j // 2, body[j:j + CHUNK_LIMIT]))
        else:
            merged.append((h, ln, body))
    return merged


def chunk_yaml(text: str, limit: int = CHUNK_LIMIT):
    """非模板 YAML（如 GTFOBins 的 _data/functions.yml）按顶层键切块，
    否则长数据文件被整体截断，函数名级别的查询永远命中不了。"""
    chunks, key, buf, start = [], "", [], 1
    for i, ln in enumerate(text.splitlines(), 1):
        if re.match(r"^[A-Za-z0-9_./\- ]+:\s*$", ln):          # 顶层键
            if buf:
                chunks.append((key, start, "\n".join(buf)))
            key, buf, start = ln[:-1].strip(), [ln], i
        else:
            buf.append(ln)
            if sum(len(x) + 1 for x in buf) > limit:
                chunks.append((key, start, "\n".join(buf)))
                buf, start = [], i
    if buf:
        chunks.append((key, start, "\n".join(buf)))
    return chunks


def build(kb_root: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "kb.sqlite"
    if db_path.exists():
        db_path.unlink()

    t0 = time.time()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, "
                 "heading TEXT, startline INTEGER, body TEXT)")
    conn.execute("CREATE VIRTUAL TABLE fts USING fts5(body, docid UNINDEXED, "
                 "tokenize='unicode61')")
    conn.execute("CREATE VIRTUAL TABLE fts_cve USING fts5(body, docid UNINDEXED, "
                 "tokenize='unicode61')")

    const_hits: dict[str, list[str]] = defaultdict(list)
    const_count: Counter = Counter()
    headings: list[tuple[str, int, int, str]] = []
    stats: Counter = Counter()
    n_files = 0
    for path in iter_files(kb_root):
        rel = path.relative_to(kb_root)
        n_files += 1
        kind = classify(rel)
        rels = str(rel)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # 常数抽取（签名层）
        for m in HEX_RE.finditer(text):
            val = m.group(1).lower()
            if len(val) % 2:                            # 奇数长度多半是误报
                continue
            const_count[val] += 1
            if len(const_hits[val]) < 3:
                line = text.count("\n", 0, m.start()) + 1
                const_hits[val].append(f"{rels}:{line}")

        # 标题倒排（md / 无扩展名条目）
        if rel.suffix in {".md", ""}:
            for i, ln in enumerate(text.splitlines(), 1):
                hm = HEADING_RE.match(ln)
                if hm:
                    headings.append((rels, i, len(hm.group(1)), hm.group(2).strip().replace("\t", " ")))

        # 全文切块入库
        if kind == "template":
            # 模板只索引 front-matter 的 info 段（id/名称/严重度/标签/描述）：
            # 原始 http 请求块既是排序噪声，也是索引体积大头
            info = re.search(r"(?ms)^info:\s*(.*?)(?:^\w[\w-]*:|\Z)", text)
            body = info.group(1)[:1200] if info else text[:600]
            chunks = [("", 1, body)]
        elif rel.suffix in {".md", ""}:
            chunks = chunk_md(text)
        elif rel.suffix in {".yml", ".yaml"}:
            chunks = chunk_yaml(text)
        else:
            chunks = [((rel.stem), 1, text[:CHUNK_LIMIT * 2])]
        for h, ln, body in chunks:
            body = (f"{h}\n{body}" if h else body).strip()
            if not body:
                continue
            conn.execute("INSERT INTO docs(path,kind,heading,startline,body) VALUES (?,?,?,?,?)",
                         (rels, kind, h, ln, body))
            did = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO fts(body, docid) VALUES (?,?)",
                         (seg(f"{rels}\n{body}"), did))
            stats[kind] += 1

    conn.execute("CREATE INDEX idx_docs_path ON docs(path)")
    conn.commit()

    # CVE/KEV 结构化数据（build_cve_data.py 产出）：进 kind=cve / kind=kev 通道，
    # 供"产品/版本 → CVE 候选"与 KEV 优先级检索
    cve_dir = kb_root / "B-漏洞与利用" / "03-cve-data"
    import json as _json
    for jl in sorted(cve_dir.glob("*.jsonl")) if cve_dir.is_dir() else []:
        rels = str(jl.relative_to(kb_root))
        with open(jl, encoding="utf-8") as fh:
            for i, ln in enumerate(fh, 1):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = _json.loads(ln)
                except ValueError:
                    continue
                rid = rec.get("id") or ""
                if not rid.startswith("CVE-"):
                    continue
                if rec.get("sev"):                     # NVD：high/critical
                    body = (f"{rid} {rec.get('sev')} cvss {rec.get('cvss')} "
                            f"{rec.get('year')} {' '.join(rec.get('prods', []))} "
                            f"{rec.get('desc', '')}")
                    kind, heading = "cve", f"{rec.get('sev')} {rec.get('cvss')} {rid}"
                else:                                  # KEV：在野利用
                    body = (f"{rid} KEV known-exploited {rec.get('vendor', '')} "
                            f"{':'.join(rec.get('prods', []))} added {rec.get('added', '')} "
                            f"due {rec.get('due', '')} {rec.get('notes', '')}")
                    kind, heading = "kev", f"KEV {rid}"
                conn.execute("INSERT INTO docs(path,kind,heading,startline,body) "
                             "VALUES (?,?,?,?,?)", (rels, kind, heading, i, body))
                did = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute("INSERT INTO fts_cve(body, docid) VALUES (?,?)",
                             (seg(body), did))
                stats[kind] += 1

    # INDEX.tsv
    with open(out_dir / "INDEX.tsv", "w", encoding="utf-8") as f:
        for rels, ln, level, title in headings:
            f.write(f"{rels}\t{ln}\t{level}\t{title}\n")

    # CONST.tsv（按出现次数排序，过滤只出现一次的噪声）
    with open(out_dir / "CONST.tsv", "w", encoding="utf-8") as f:
        for val, cnt in const_count.most_common():
            if cnt < 2 and not (val.startswith("9e3779b9") or len(val) >= 8):
                continue
            f.write(f"{val}\t{cnt}\t{const_hits[val][0]}\n")

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    dt = time.time() - t0
    size = (out_dir / "kb.sqlite").stat().st_size / 1048576
    print(f"文件 {n_files} 个 | 块 {sum(stats.values())} | "
          + " ".join(f"{k}={v}" for k, v in stats.items()))
    print(f"kb.sqlite {size:.1f} MB | 耗时 {dt:.1f}s | "
          f"INDEX.tsv {len(headings)} 行 | CONST.tsv {len(const_count)} 常数")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True, help="知识库根目录")
    ap.add_argument("--out", default=None, help="输出目录（默认 <kb>/.index）")
    args = ap.parse_args()
    kb = Path(args.kb).resolve()
    out = Path(args.out).resolve() if args.out else kb / ".index"
    if not (kb / "A-基础与技巧").exists():
        print(f"错误：{kb} 不像知识库根目录（缺 A-基础与技巧/）", file=sys.stderr)
        return 2
    build(kb, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
