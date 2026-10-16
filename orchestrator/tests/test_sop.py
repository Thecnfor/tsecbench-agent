"""EvoSOP 蒸馏/记账/检索/剪枝单测（harness-v2.md §4）。

合规硬闸是重点：flag 字面量存活 → fail-closed 拒写入；IP/host/URL/载荷统一
脱敏；零 LLM。生命周期 build → merge → evaluate → prune 全链路。
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator import sop


def make_session(ws: Path, lines: list[str]) -> None:
    out = ws / "out"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "session.jsonl").open("w", encoding="utf-8") as fh:
        for text in lines:
            event = json.dumps({"ts": 0.0, "kind": "pi_stdout", "text": text}, ensure_ascii=False)
            fh.write(event + "\n")


def test_distill_sanitizes_targets_and_urls(tmp_path):
    ws = tmp_path / "ws_c1"
    make_session(
        ws,
        [
            "$ curl http://10.0.0.8:8080/admin",
            "$ nmap -sV 10.0.0.8 -p 6379",
            "$ gobuster dir -u http://internal.lab.local:8080/ -w /lists/common.txt",
        ],
    )
    entry = sop.distill(ws, tmp_path)
    assert entry is not None
    body = (tmp_path / "sops" / entry["file"]).read_text(encoding="utf-8")
    assert "10.0.0.8" not in body and "internal.lab.local" not in body
    assert "<TARGET>" in body  # 裸 IP / host 打码
    assert "<URL>" in body  # URL 形态统一打码（含其中的 host:port）
    assert entry["wins"] == 1 and entry["uses"] == 0


def test_distill_refuses_flag_literals(tmp_path):
    ws2 = tmp_path / "ws_c3"
    make_session(ws2, ["$ echo FLAG{leak} && echo done"])
    entry = sop.distill(ws2, tmp_path)
    assert entry is None
    assert not ((tmp_path / "sops").exists() and list((tmp_path / "sops").glob("*.md")))


def test_distill_merge_by_fingerprint(tmp_path):
    ws = tmp_path / "ws_c4"
    make_session(ws, ["$ curl http://10.0.0.9:9000/api", "$ nmap -sV 10.0.0.9"])
    first = sop.distill(ws, tmp_path)
    assert first is not None
    (tmp_path / "sops" / str(first["file"])).unlink()  # 模拟文件被外部清走也能命中合并
    ws2 = tmp_path / "ws_c5"
    make_session(ws2, ["$ curl http://10.1.1.9:9000/api", "$ nmap -sV 10.1.1.9"])
    merged = sop.distill(ws2, tmp_path)
    assert merged is not None
    assert merged["wins"] == 2


def test_select_scores_by_overlap_and_wins(tmp_path):
    ws = tmp_path / "ws_c6"
    make_session(ws, ["$ curl http://10.0.0.9:9000/api", "$ nmap -sV 10.0.0.9"])
    entry = sop.distill(ws, tmp_path)
    assert entry is not None
    (tmp_path / "sops" / str(entry["file"])).write_text(
        "# SOP: nmap curl (distilled, de-identified)\n1. curl <TARGET>\n2. nmap -sV <TARGET>\n",
        encoding="utf-8",
    )
    picked = sop.select(tmp_path, "nginx api nmap curl recon", top_k=1)
    assert picked, "有命中竞品应返回正文"
    assert "nmap" in picked[0]


def test_prune_clears_low_winrate(tmp_path):
    ws = tmp_path / "ws_c7"
    make_session(ws, ["$ curl http://10.0.0.2/x"])
    entry = sop.distill(ws, tmp_path)
    assert entry is not None
    for _ in range(sop.PRUNE_USES + 1):  # uses 打到阈值之上，wins 停在 1
        sop.mark_used(tmp_path, [entry["slug"]])
    # 剪枝发生在下一次 distill 周期（§4.1 生命周期：evaluate → 下一轮 prune）
    ws2 = tmp_path / "ws_c9"
    make_session(ws2, ["$ nmap -sV 10.0.0.5 -p 22"])
    assert sop.distill(ws2, tmp_path) is not None
    idx = sop._read_index(tmp_path)
    assert entry["slug"] not in idx


def test_select_marks_used(tmp_path):
    ws = tmp_path / "ws_c8"
    make_session(ws, ["$ curl http://10.0.0.3/y"])
    entry = sop.distill(ws, tmp_path)
    assert entry is not None
    sop.select(tmp_path, "curl recon", top_k=1)
    index = sop._read_index(tmp_path)
    assert index[entry["slug"]]["uses"] == 1


def test_distill_empty_session_returns_none(tmp_path):
    ws = tmp_path / "ws_none"
    make_session(ws, ["plain output without commands"])
    assert sop.distill(ws, tmp_path) is None
    assert sop.distill(tmp_path / "missing", tmp_path) is None


class TestProgressiveDisclosure:
    """catalog/load（harness-v2-openhands.md §5）：目录注入 + 全文按需，读侧 fail-closed。

    fixture 对齐 distill 真实落盘格式：index.json 是 dict（key=slug，条目含
    file 字段），正文在 base/sops/{slug}.md。
    """

    @staticmethod
    def _make_store(tmp_path: Path, slug: str, body: str) -> Path:
        d = tmp_path / "sops"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.md").write_text(body, encoding="utf-8")
        (d / "index.json").write_text(
            json.dumps({slug: {"slug": slug, "file": f"{slug}.md", "wins": 1, "uses": 0}}),
            encoding="utf-8",
        )
        return tmp_path

    def test_catalog_and_load(self, tmp_path):
        base = self._make_store(
            tmp_path,
            "web-sqli",
            "# SOP: web-sqli (distilled, de-identified)\n\n1. curl <TARGET>\n2. sqlmap 批处理\n",
        )
        cat = sop.catalog(base)
        assert cat and cat[0][0] == "web-sqli"
        full = sop.load(base, "web-sqli")
        assert full is not None and "sqlmap" in full

    def test_load_rejects_flag_body(self, tmp_path):
        base = self._make_store(tmp_path, "dirty", "FLAG{leak}\n")
        assert sop.load(base, "dirty") is None
        assert sop.catalog(base) == []

    def test_load_missing_slug_none(self, tmp_path):
        assert sop.load(tmp_path, "ghost") is None
        assert sop.catalog(tmp_path) == []

    def test_catalog_summary_sanitized_and_truncated(self, tmp_path):
        long = "1. nmap -sV 10.0.0.9 -p " + "a" * 200
        base = self._make_store(tmp_path, "recon", f"{long}\n")
        (slug, summary) = sop.catalog(base)[0]
        assert slug == "recon"
        assert len(summary) <= 80
        assert "10.0.0.9" not in summary and "<TARGET>" in summary
