"""60s 试探闸门的进展判据（轨迹证据：27 次续片 / 0 次停止）。

explored / session 行数/资产里回显的目标地址都不是进展：任何一次命令都会
产生它们，导致闸门恒开。只有新端点、凭据、版本指纹或候选 flag 才算。
"""

from __future__ import annotations

import json

from orchestrator.main import _slice_has_progress

TARGET = "10.0.0.5:8080"


def _workspace(tmp_path, *, prior, targets=(TARGET,), session_lines=0, flags=()):
    ws = tmp_path / "ws"
    (ws / "out" / "flags").mkdir(parents=True)
    (ws / "task.json").write_text(json.dumps({"targets": list(targets)}), encoding="utf-8")
    (ws / "out" / "prior.json").write_text(json.dumps(prior), encoding="utf-8")
    if session_lines:
        (ws / "out" / "session.jsonl").write_text("{}\n" * session_lines, encoding="utf-8")
    for name in flags:
        (ws / "out" / "flags" / name).write_text("FLAG{demo}", encoding="utf-8")
    return ws


def test_explored_commands_and_session_lines_are_not_progress(tmp_path):
    ws = _workspace(
        tmp_path,
        prior={"explored": ["curl http://10.0.0.5:8080/admin.php"], "assets": [TARGET]},
        session_lines=40,
    )
    assert _slice_has_progress(ws) is False


def test_asset_on_target_host_is_not_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"assets": [TARGET, "10.0.0.5"]})
    assert _slice_has_progress(ws) is False


def test_asset_on_new_host_is_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"assets": ["10.0.0.6:6379"]})
    assert _slice_has_progress(ws) is True


def test_new_port_on_target_host_is_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"assets": [TARGET, "10.0.0.5:22"]})
    assert _slice_has_progress(ws) is True


def test_credentials_are_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"credentials": ["admin:hunter2"]})
    assert _slice_has_progress(ws) is True


def test_version_finding_is_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"findings": ["nginx/1.18.0"]})
    assert _slice_has_progress(ws) is True


def test_candidate_flag_is_progress(tmp_path):
    ws = _workspace(tmp_path, prior={}, flags=("001.txt",))
    assert _slice_has_progress(ws) is True


def test_open_lead_on_new_host_is_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"open_leads": ["http://10.0.0.7/flag"]})
    assert _slice_has_progress(ws) is True


def test_open_lead_on_target_host_is_not_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"open_leads": [f"http://{TARGET}/admin"]})
    assert _slice_has_progress(ws) is False


def test_missing_prior_and_flags_dir_is_not_progress(tmp_path):
    ws = tmp_path / "bare"
    ws.mkdir()
    assert _slice_has_progress(ws) is False


def test_host_only_target_any_port_is_not_progress(tmp_path):
    ws = _workspace(tmp_path, prior={"assets": ["10.0.0.5:80"]}, targets=("10.0.0.5",))
    assert _slice_has_progress(ws) is False
