"""Event log (corresponding to OpenHands conversation/event-stream service, simplified version).

Structured append of all observable events within a single-question lifecycle (pi stdout,
planning decisions, candidate flags, process meta) into out/session.jsonl — used for
two purposes: ① postmortem / score tracking; ② deterministic synthesis of out/prior.json.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Session:
    """JSONL event log. All writes are append + flush; corruption never crashes the solver."""

    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / "session.jsonl"
        self._fh = None

    def _ensure(self):
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        return self._fh

    def log(self, kind: str, **fields) -> None:
        event = {"ts": round(time.time(), 3), "kind": kind, **fields}
        try:
            fh = self._ensure()
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
        except OSError:
            pass  # Logging failure never blocks problem solving

    def lines(self) -> list[dict]:
        """Read back all events (used for prior synthesis; corrupted lines are skipped)."""
        out: list[dict] = []
        try:
            for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return out

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
