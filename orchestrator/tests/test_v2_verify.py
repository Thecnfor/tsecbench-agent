"""Task 5 brief：独立验证闸门测试（无 LLM、离线可跑）。"""

from pathlib import Path

from orchestrator.v2_verify import verify_claim


def _ws(tmp_path: Path, flag: str | None) -> Path:
    out = tmp_path / "out" / "flags"
    out.mkdir(parents=True)
    if flag:
        (out / "1.txt").write_text(flag, encoding="utf-8")
    return tmp_path


class TestVerify:
    def test_no_claim(self, tmp_path):
        r = verify_claim(_ws(tmp_path, None), runner=lambda: True)
        assert r.claimed == [] and not r.passed

    def test_real_flag_confirmed(self, tmp_path):
        r = verify_claim(_ws(tmp_path, "flag{cafe_babe_42}"), runner=lambda: True)
        assert r.claimed == ["flag{cafe_babe_42}"]
        assert r.confirmed == r.claimed and r.passed

    def test_liveness_false_fails(self, tmp_path):
        r = verify_claim(_ws(tmp_path, "flag{aabb}"), runner=lambda: False)
        assert not r.liveness and not r.passed
        assert r.claimed  # 声称仍在，但不通过

    def test_shell_roundtrip_default_runner(self, tmp_path):
        # 真实 sh 往返（离线可跑）
        r = verify_claim(_ws(tmp_path, None))
        assert r.liveness is True
