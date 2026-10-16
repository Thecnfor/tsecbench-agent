import pytest
from orchestrator.v2_state import (  # noqa: F401  状态别名一并导入，校验导出面
    DEAD_END,
    EXPLORED,
    EXPLORING,
    UNEXPLORED,
    GraphController,
    IllegalTransition,
    NodeState,
    SlotFullError,
)

assert tuple(NodeState) == (UNEXPLORED, EXPLORING, EXPLORED, DEAD_END)


class TestTransitions:
    def test_seed_and_explore_path(self):
        gc = GraphController(max_parallel=2, node_timeout_s=900)
        gc.add_node("root")
        assert gc.unexplored() == ["root"]
        gc.begin("root")
        gc.finish("root", explored=True)
        assert gc.free_slots == 2

    def test_dead_end_is_terminal(self):
        gc = GraphController()
        gc.add_node("a")
        gc.begin("a")
        gc.timeout_node("a")
        with pytest.raises(IllegalTransition):
            gc.begin("a")

    def test_exploring_requires_unexplored(self):
        gc = GraphController()
        with pytest.raises(IllegalTransition):
            gc.begin("ghost")

    def test_double_begin_rejected(self):
        gc = GraphController()
        gc.add_node("a")
        gc.begin("a")
        with pytest.raises(IllegalTransition):
            gc.begin("a")


class TestConcurrency:
    def test_slots_exhausted(self):
        gc = GraphController(max_parallel=3)
        for k in ("a", "b", "c"):
            gc.add_node(k)
            gc.begin(k)
        assert gc.free_slots == 0
        gc.add_node("d")
        with pytest.raises(SlotFullError):
            gc.begin("d")

    def test_finish_releases_slot(self):
        gc = GraphController(max_parallel=1)
        gc.add_node("a")
        gc.begin("a")
        gc.add_node("b")
        with pytest.raises(SlotFullError):
            gc.begin("b")
        gc.finish("a", explored=False)  # 非超时失败也释放槽
        gc.begin("b")
        assert gc.free_slots == 0


class TestTimeoutFuse:
    def test_expired_and_force_dead_end(self):
        gc = GraphController(max_parallel=2, node_timeout_s=10)
        gc.add_node("a")
        gc.begin("a")
        gc.add_node("b")
        gc.begin("b")
        stale = gc.expired(now=_clock() + 11)
        assert sorted(stale) == ["a", "b"]
        for k in stale:
            gc.timeout_node(k)
        assert gc.free_slots == 2
        assert gc.snapshot()["a"]["state"] is DEAD_END


def _clock() -> float:
    import time

    return time.monotonic()
