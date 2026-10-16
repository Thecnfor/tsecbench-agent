"""离线知识库 —— 自写 cheatsheet + stdlib 关键词倒排索引（零外部依赖）。

构建期：无（索引实时构建，文档量小）；运行期：只读。
CLI：python3 kb/index.py query "nginx 1.18 flask"  → JSON top-k 片段。
"""

from __future__ import annotations

import json
import math
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
# 题目描述是中文，只按 ASCII 分词会让纯中文描述零命中（实测：「文件类型校验」
# 「物流看板数据导入」都检索不到任何片段）。中文按**单字 + 相邻双字**入索引：
# 双字足以区分「注入/上传/越权」这类语义，又不至于像整句切分那样召回发散。
CJK_RE = re.compile(r"[㐀-鿿]+")
MAX_SNIPPET_CHARS = 1200


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 '## ' 标题切块，返回 [(标题, 正文)]。

    文档级前言（`# 标题` + 用法说明）**不进索引**：它词面太泛（服务/安全/协议
    这类通用词密集），会靠词频压过真正相关的节。没有 `##` 小节时才整篇兜底。
    """
    parts = re.split(r"^## ", text, flags=re.MULTILINE)
    out = []
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        if i == 0 and len(parts) > 1:
            continue  # 前言块，丢弃
        title, _, body = part.partition("\n")
        out.append((title.strip() or "(untitled)", body.strip()))
    return out


def _tokenize(text: str) -> list[str]:
    """ASCII 词 + 中文单字/双字。中英混排的题面两边都能召回。"""
    tokens = [t for t in WORD_RE.findall(text.lower()) if t not in STOPWORDS]
    for run in CJK_RE.findall(text):
        tokens.extend(run)  # 单字
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # 双字
    return tokens


class Index:
    def __init__(self, kb_dir: Path | None = None) -> None:
        self.kb_dir = kb_dir or HERE
        self.sections: list[tuple[str, str, str]] = []  # (doc, title, body)
        self.postings: dict[str, dict[int, float]] = {}  # token -> {section_idx: 权重}
        self._build()

    def _build(self) -> None:
        for md in sorted(self.kb_dir.glob("*.md")):
            for title, body in _split_sections(md.read_text(encoding="utf-8", errors="replace")):
                idx = len(self.sections)
                self.sections.append((md.stem, title, body))
                title_tokens = set(_tokenize(title))
                body_tokens = set(_tokenize(body))
                for token in title_tokens | body_tokens:
                    self.postings.setdefault(token, {})[idx] = 3.0 if token in title_tokens else 1.0

    def query(self, text: str, top_k: int = 3) -> list[dict]:
        """IDF 加权打分：标题 3 倍权重，**稀有词权重更高**。

        纯词频会被长片段刷分 —— 实测「格式校验」这类长节靠通用词
        （系统/功能/部署）压过真正相关的节。IDF 让 `pydash` 这种稀有词
        一票顶过十几个「系统」。
        """
        n = max(1, len(self.sections))
        scores: dict[int, float] = {}
        for token in _tokenize(text):
            postings = self.postings.get(token)
            if not postings:
                continue
            idf = math.log(1.0 + n / len(postings))
            for idx, weight in postings.items():
                scores[idx] = scores.get(idx, 0.0) + weight * idf
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for idx, score in ranked[:top_k]:
            doc, title, body = self.sections[idx]
            out.append(
                {
                    "doc": doc,
                    "section": title,
                    "score": round(score, 2),
                    "body": body[:MAX_SNIPPET_CHARS],
                }
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
