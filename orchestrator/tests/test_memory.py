"""攻击图记忆层单测（harness-v2.md §6）。

关键契约：**triviumdb 不可用时必须完全降级**（空操作），编排器行为与没有它时
一致 —— 这是「任一环节失败即退回原路径」的落地，比功能本身更重要。
"""

from __future__ import annotations

from orchestrator.memory import AttackMemory

PRIOR = {
    "assets": ["10.0.0.1:8080"],
    "explored": ["curl -sS -D - http://10.0.0.1:8080/"],
    "open_leads": ["nginx/1.18.0", "?page= 参数疑似 LFI"],
}


def test_disabled_memory_is_noop(tmp_path):
    """enabled=False → 全空操作，不建文件、不抛异常。"""
    mem = AttackMemory(tmp_path / "x.tdb", enabled=False)
    assert mem.available is False
    assert mem.record("a-01", PRIOR) == 0
    assert mem.recall("a-01", "nginx") == []
    assert mem.unexplored("a-01") == []
    assert mem.stats() == {"enabled": False}
    mem.close()  # 不抛


def test_record_then_recall_and_unexplored(tmp_path):
    """写 prior 双图 → 属性信号能查到未探索线索；文本信号能召回相关节点。"""
    mem = AttackMemory(tmp_path / "attack.tdb")
    if not mem.available:
        return  # triviumdb 缺失时跳过（降级路径已由上一个用例覆盖）

    assert mem.record("a-01", PRIOR) == 4  # 1 asset + 1 explored + 2 leads

    leads = mem.unexplored("a-01")
    assert {x["key"].split("|")[-1] for x in leads} == {"nginx/1.18.0", "?page= 参数疑似 LFI"}

    hits = mem.recall("a-01", "nginx", top_k=3)
    assert any("nginx" in (h["key"] or "") for h in hits)

    # 跨题隔离：另一题查不到 a-01 的节点
    assert mem.unexplored("a-02") == []
    assert mem.recall("a-02", "nginx") == []

    stats = mem.stats()
    assert stats["enabled"] is True and stats["nodes"] >= 4
    mem.close()


def test_record_is_idempotent(tmp_path):
    """重复写同一份 prior 不产生重复节点（key 哈希定 ID + upsert）。"""
    mem = AttackMemory(tmp_path / "attack.tdb")
    if not mem.available:
        return
    mem.record("a-01", PRIOR)
    n1 = mem.stats()["nodes"]
    mem.record("a-01", PRIOR)
    assert mem.stats()["nodes"] == n1
    mem.close()
