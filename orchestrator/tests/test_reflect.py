"""Observer-lite：解析/脱敏/触发条件与 prior 注入。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from orchestrator.supervisor import collect_prior

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

import reflect  # noqa: E402


def test_parse_normalizes_and_redacts_flag():
    obs = reflect.parse(
        json.dumps(
            {
                "hypothesis": "upload bypass leads to RCE",
                "lock": "tests upload validation; flag likely in /flag",
                "confirmed": ["tried /admin -> 403"],
                "angles": ["upload extension bypass", "flag{should_not_leak}"],
                "tension": ["a", "b", "c"],
            }
        )
    )
    assert obs is not None
    assert obs["lock"].startswith("tests upload")
    assert len(obs["angles"]) == 2
    assert "flag{" not in json.dumps(obs)
    assert len(obs["tension"]) == 2


def test_parse_rejects_empty_or_invalid():
    assert reflect.parse("") is None
    assert reflect.parse("not json") is None
    assert reflect.parse('{"hypothesis": "x"}') is None


def test_should_reflect_limits():
    assert reflect.should_reflect(reflects=0, remaining_s=None) is True
    assert reflect.should_reflect(reflects=2, remaining_s=500, max_reflects=3) is True
    assert reflect.should_reflect(reflects=3, remaining_s=500, max_reflects=3) is False
    assert reflect.should_reflect(reflects=0, remaining_s=10, min_remaining_s=90) is False


def test_render_contains_sections():
    text = reflect.render(
        {"hypothesis": "h", "lock": "l", "confirmed": ["c"], "angles": ["a"], "tension": ["t"]}
    )
    for section in ("HYPOTHESIS", "CONFIRMED", "LOCK", "ANGLES", "TENSION"):
        assert section in text


def test_collect_prior_reads_observer(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "observer.json").write_text(
        json.dumps({"revision": 7, "lock": "l", "angles": ["a"], "extra": "drop"}),
        encoding="utf-8",
    )
    prior = collect_prior(tmp_path)
    assert prior["observer"]["revision"] == 7
    assert prior["observer"]["lock"] == "l"
    assert "extra" not in prior["observer"]
