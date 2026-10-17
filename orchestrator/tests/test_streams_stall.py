"""停滞看门狗：solver 活着但无输出时提前止损（不占槽等整片）。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from orchestrator.streams import run_solver_with_stream


def _rec() -> SimpleNamespace:
    return SimpleNamespace(submitted=set())


def _write_solver(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_stalled_solver_is_killed_early(tmp_path, monkeypatch):
    solver = _write_solver(
        tmp_path / "stall.py",
        "import time\nprint('started', flush=True)\ntime.sleep(30)\n",
    )
    events: list[dict] = []
    journal = SimpleNamespace(append=lambda event, **kw: events.append({"event": event, **kw}))
    started = time.monotonic()
    outcome, early_new, early_completed = asyncio.run(
        run_solver_with_stream(
            solver_cmd=[sys.executable, str(solver)],
            ws=tmp_path,
            deadline_epoch=time.time() + 30,
            slice_s=30,
            environ=None,
            client=None,
            journal=journal,
            unique_code="a-01",
            rec=_rec(),
            interval_s=0.15,
            stall_s=0.5,
        )
    )
    assert time.monotonic() - started < 10
    assert outcome.stalled is True
    assert outcome.exit_code == 124
    assert early_new == 0 and early_completed is False
    assert any(event["event"] == "solver_stalled" for event in events)


def test_normal_exit_is_not_stalled(tmp_path):
    solver = _write_solver(tmp_path / "ok.py", "print('done', flush=True)\n")
    journal = SimpleNamespace(append=lambda *args, **kw: None)
    outcome, _new, _completed = asyncio.run(
        run_solver_with_stream(
            solver_cmd=[sys.executable, str(solver)],
            ws=tmp_path,
            deadline_epoch=time.time() + 30,
            slice_s=30,
            environ=None,
            client=None,
            journal=journal,
            unique_code="a-01",
            rec=_rec(),
            interval_s=0.05,
            stall_s=5,
        )
    )
    assert outcome.stalled is False
    assert outcome.exit_code == 0
