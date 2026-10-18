"""Flag Harvester + Submitter 单测（§2.3 不信任 result.json / §5.1 duplicate）。"""

from __future__ import annotations

import asyncio
import threading

import pytest
from mock_server.server import make_server
from mock_server.state import ChallengeSpec
from orchestrator.api import FlagRejected, PlatformClient
from orchestrator.harvester import SubmitReport, extract_flags, harvest, submit_candidates
from orchestrator.registry import ChallengeRecord

TOKEN = "mock-token"


@pytest.fixture
def factory():
    servers: list = []

    def make(specs=None, env=None):
        server, _state = make_server(specs, env=env, token=TOKEN)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


# ---- 收割 ----
def test_extract_named_and_generic_order():
    text = "noise FLAG{aaa} HTB{bbb} Flag{abc-123} FLAG{aaa} end"
    assert extract_flags(text) == ["FLAG{aaa}", "HTB{bbb}", "Flag{abc-123}"]


def test_extract_rejects_css_and_js_generic():
    text = (
        "body{color:#fff;background:#000} "
        "h1{border-right:1px solid rgba(255,255,255,.3)} "
        "let{children:t} try{r=a,n=b} FLAG{ok}"
    )
    assert extract_flags(text) == ["FLAG{ok}"]


def test_harvest_scans_all_files_not_just_result(tmp_path):
    """flag 散落在 stdout.log / notes.md / flags/*.txt —— 全部被抓到并去重。"""
    ws = tmp_path / "ws"
    (ws / "out" / "flags").mkdir(parents=True)
    (ws / "out" / "traffic").mkdir(parents=True)
    (ws / "out" / "flags" / "000.txt").write_text("FLAG{from_file}\n", encoding="utf-8")
    (ws / "stdout.log").write_text("... scanned ... FLAG{from_stdout} ...", encoding="utf-8")
    (ws / "notes.md").write_text("evidence: HTB{in_notes}", encoding="utf-8")
    # result.json 自述失败但 flag 在里面 —— 不信任其 status，照样扫出
    (ws / "out" / "result.json").write_text(
        '{"status": "failed", "candidates": []}', encoding="utf-8"
    )
    (ws / "out" / "traffic" / "01.jsonl").write_text(
        '{"req": "GET /", "body": "FLAG{in_traffic}"}\n', encoding="utf-8"
    )
    flags = harvest(ws)
    # 顺序 = 文件名字典序 × 文件内出现序（notes.md < out/* < stdout.log）
    assert flags == ["HTB{in_notes}", "FLAG{from_file}", "FLAG{in_traffic}", "FLAG{from_stdout}"]


def test_harvest_length_filter(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    too_long = "FLAG{" + "a" * 4096 + "}"
    ok = "FLAG{ok}"
    (ws / "f.txt").write_text(f"{ok} {too_long}", encoding="utf-8")
    assert harvest(ws) == [ok]


def test_harvest_empty_workspace(tmp_path):
    assert harvest(tmp_path / "nonexistent") == []


# ---- 提交 ----
def client(base: str) -> PlatformClient:
    return PlatformClient(base, TOKEN, qps=100.0, jitter=0.0)


class RejectingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, unique_code: str, flag: str) -> dict:
        self.calls += 1
        raise FlagRejected("422 rejected")


class NoMatchClient:
    """平台对错误 flag 返回 200 + matched_flag_index=null（不是 422）。"""

    async def submit(self, unique_code: str, flag: str) -> dict:
        return {
            "correct_flag_count": 0,
            "total_flag_count": 4,
            "matched_flag_index": None,
        }


def test_200_without_match_counts_rejected_and_budget():
    rec = ChallengeRecord(unique_code="n1", flag_count=4, total_score=1200)
    report = asyncio.run(submit_candidates(NoMatchClient(), rec, ["flag{one}", "flag{two}"]))
    assert report.rejected == 2
    assert report.new_matches == 0
    assert rec.rejected_candidates == 2
    assert rec.reject_budget == 1
    assert rec.correct_flag_count == 0


def test_rejected_candidate_is_not_resubmitted():
    rec = ChallengeRecord(unique_code="r1")
    rejecting = RejectingClient()
    report = asyncio.run(submit_candidates(rejecting, rec, ["body{color:red}"]))
    assert report.rejected == 1
    assert rec.rejected_candidates == 1
    assert "body{color:red}" in rec.submitted
    assert [f for f in ["body{color:red}"] if f not in rec.submitted] == []


def test_submit_multi_flag_progress_and_complete(factory):
    base = factory([ChallengeSpec("m1", flags=["FLAG{a}", "FLAG{b}"])])
    rec = ChallengeRecord(unique_code="m1", flag_count=2)
    c = client(base)
    asyncio.run(c.start("m1"))
    report = asyncio.run(submit_candidates(c, rec, ["FLAG{wrong}", "FLAG{a}", "FLAG{b}"]))
    assert report.new_matches == 2
    assert report.completed is True
    assert rec.matched_indices == {0, 1}


def test_submit_duplicate_not_failure(factory):
    base = factory([ChallengeSpec("d1", flags=["FLAG{dup}"])])
    rec = ChallengeRecord(unique_code="d1", flag_count=1)
    c = client(base)
    asyncio.run(c.start("d1"))
    asyncio.run(c.submit("d1", "FLAG{dup}"))  # 平台已有
    report = asyncio.run(submit_candidates(c, rec, ["FLAG{dup}"]))
    assert report.duplicates == 1
    assert report.new_matches == 0


def test_submit_stops_on_task_ended(factory):
    base = factory([ChallengeSpec("c1", flags=["FLAG{a}"])], env={"MOCK_TOTAL_LIMIT_S": "0.2"})
    rec = ChallengeRecord(unique_code="c1", flag_count=1)
    c = client(base)
    asyncio.run(c.start("c1"))
    import time

    time.sleep(0.35)
    report = asyncio.run(submit_candidates(c, rec, ["FLAG{a}"]))
    assert report.task_ended is True


def test_submit_report_type():
    r = SubmitReport()
    assert r.new_matches == 0 and r.completed is False


def test_extract_rejects_code_identifier_prefixes():
    text = "JSAPI_Event_Abandon_{83e44378-6c8c-4be3-b3e5-29d6dfde1d55} collect{it.name}"
    assert extract_flags(text) == []


def test_extract_rejects_placeholder_flags():
    text = "FLAG{...} FLAG{<flag>} FLAG{xxx} FLAG{[^} flag{ce38f9c5-2635-4c27-8209-ee21e32559f0}"
    assert extract_flags(text) == ["flag{ce38f9c5-2635-4c27-8209-ee21e32559f0}"]
