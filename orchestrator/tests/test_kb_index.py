"""离线 KB 检索：预建 FTS 库 + 本地 curated cheatsheet 必须都能命中。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def load_index():
    sys.path.insert(0, str(SOLVER_DIR / "kb"))
    sys.modules.pop("index", None)
    return __import__("index")


def test_local_cheatsheets_are_searchable_even_with_prebuilt_db(tmp_path):
    index_mod = load_index()
    db = sqlite3.connect(tmp_path / "unified.db")
    db.execute("CREATE TABLE secs(doc TEXT, title TEXT, body TEXT)")
    db.execute("CREATE VIRTUAL TABLE ft USING fts5(tokens)")
    db.execute("CREATE TABLE rowmap(rid INTEGER PRIMARY KEY, sec INTEGER)")
    db.commit()
    db.close()
    (tmp_path / "multi_stage.md").write_text(
        "# Multi\n\n## credential reuse lateral movement\n"
        "Reuse confirmed credentials on every discovered service.\n",
        encoding="utf-8",
    )
    hits = index_mod.Index(kb_dir=tmp_path).query("credential reuse lateral", top_k=3)
    assert hits
    assert hits[0]["doc"] == "multi_stage"
