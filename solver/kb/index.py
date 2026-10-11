"""离线知识库 —— 自写 cheatsheet + stdlib 关键词倒排索引（零外部依赖）。

构建期：无（索引实时构建，文档量小）；运行期：只读。
CLI：python3 kb/index.py query "nginx 1.18 flask"  → JSON top-k 片段。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "be",
        "do",
        "does",
        "how",
        "what",
        "when",
        "your",
        "you",
        "it",
        "its",
        "as",
        "at",
        "by",
        "from",
        "into",
        "not",
        "use",
        "using",
        "this",
        "that",
        "if",
        "then",
        "else",
    ]
)
WORD_RE = re.compile(r"[a-z0-9_.\-]{2,}")
MAX_SNIPPET_CHARS = 1200


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 '## ' 标题切块，返回 [(标题, 正文)]；无标题则整文档一块。"""
    parts = re.split(r"^## ", text, flags=re.MULTILINE)
    out = []
    for part in parts:
        if not part.strip():
            continue
        title, _, body = part.partition("\n")
        out.append((title.strip() or "(untitled)", body.strip()))
    return out


def _tokenize(text: str) -> list[str]:
    return [t for t in WORD_RE.findall(text.lower()) if t not in STOPWORDS]


class Index:
    def __init__(self, kb_dir: Path | None = None) -> None:
        self.kb_dir = kb_dir or HERE
        self.sections: list[tuple[str, str, str]] = []  # (doc, title, body)
        self.inverted: dict[str, list[int]] = {}
        self._build()

    def _build(self) -> None:
        for md in sorted(self.kb_dir.glob("*.md")):
            for title, body in _split_sections(md.read_text(encoding="utf-8", errors="replace")):
                idx = len(self.sections)
                self.sections.append((md.stem, title, body))
                for token in set(_tokenize(title) * 3 + _tokenize(body)):
                    self.inverted.setdefault(token, []).append(idx)

    def query(self, text: str, top_k: int = 3) -> list[dict]:
        """关键词倒排打分（标题 3 倍权重在 _build 已体现）；返回 top-k 片段。"""
        scores: dict[int, int] = {}
        for token in _tokenize(text):
            for idx in self.inverted.get(token, []):
                scores[idx] = scores.get(idx, 0) + 1
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for idx, score in ranked[:top_k]:
            doc, title, body = self.sections[idx]
            out.append(
                {"doc": doc, "section": title, "score": score, "body": body[:MAX_SNIPPET_CHARS]}
            )
        return out


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "query":
        print("usage: index.py query <text> [top_k]", file=sys.stderr)
        return 2
    top_k = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    print(json.dumps(Index().query(sys.argv[2], top_k), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
