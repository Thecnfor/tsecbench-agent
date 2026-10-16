"""EVOSTER 在线修复 —— 失败片信号化的策略变异，而非直接放弃（harness-v2.md §5）。

EVOSTER 的「在线修复」对齐：失败发生即处置，不等整个轨迹结束才调整 ——
这里落成：纯函数把上一片的失败信号映射到有界的动作集合
（widen / retry_same / recompile），再按**锚定权重**选一；权重随命中
结果增减（update_weights，+25% / −20%），Argmax 遇平票走固定优先级，
全程确定性、可单测。

红线（§5.2）：MAX_ATTEMPTS 上限、finalize 保护一律不动 ——
本模块只在 attempt 预算内选择「换什么姿势重投」，不放宽任何闸门。
"""

from __future__ import annotations

from dataclasses import dataclass

from .registry import ChallengeRecord
from .supervisor import SolverOutcome

WIDEN_FACTOR = 1.5  # widen 动作对时间片的放大系数（最多一次，§5.1）
WEIGHT_UP = 1.25
WEIGHT_DOWN = 0.80
# 锚定权重初值：三类维修动作平权，随命中结果演化
BASE_WEIGHTS: dict[str, float] = {"retry_same": 1.0, "widen": 1.0, "recompile": 1.0}

# 平票时的固定优先序（确定性保证）
_PRIORITY = ("widen", "recompile", "retry_same")


@dataclass(slots=True)
class RepairPlan:
    action: str  # retry_same | widen | recompile
    next_slice_s: float | None = None  # widen 时给出下轮时间片
    note: str = ""  # 换面指令：经 task.json.repair 注入执行端（planner 提示）


def update_weights(weights: dict[str, float], action: str, success: bool) -> None:
    """锚定轨迹平衡：动作成功加 25%、失败减 20%。未知动作不进账。"""
    if action not in weights:
        return
    weights[action] *= WEIGHT_UP if success else WEIGHT_DOWN


def _pick(options: list[str], weights: dict[str, float], ranks: list[str]) -> str:
    """Argmax by weight（缺省按 BASE），平票走固定优先级 Prescott 序。"""
    best = max(weights.get(o, 1.0) for o in options)
    tied = [o for o in options if abs(weights.get(o, 1.0) - best) < 1e-9]
    for candidate in ranks:
        if candidate in tied:
            return candidate
    return tied[0]


def choose_repair(
    rec: ChallengeRecord,
    outcome: SolverOutcome,
    slice_s: float,
    *,
    total_remaining_s: float,
    slice_max_s: float,
    weights: dict[str, float],
    progress: bool = False,
) -> RepairPlan:
    """失败片 → 修复计划。只在 attempt 预算内转身，不碰任何硬上限。

    信号表（§5.1）：
      - timed_out（exit 124）→ widen（未用过且有预算）或 recompile（换面）
      - 其它非零退出        → retry_same（瞬态崩溃，计划本身没错）
      - 正常结束但零候选    → recompile（原 plan 的面已经探秃）
    """
    if outcome.exit_code == 124 and outcome.timed_out:
        # widen 只投给有进展信号的片（实测：零线索题 widen 后照样烧满整片，
        # 纯时间浪费；有 assets/线索的题才值得更多 runway）
        options: list[str] = ["recompile"]
        next_slice = None
        if not rec.repair_widened and progress:
            widened = min(slice_s * WIDEN_FACTOR, slice_max_s * 2.0)
            if widened <= total_remaining_s:
                options.insert(0, "widen")
                next_slice = widened
        action = _pick(options, weights, list(_PRIORITY))
        if action == "widen":
            return RepairPlan(
                action=action,
                next_slice_s=next_slice,
                note="time slice ended with no answer; the plan needs more runway "
                "or a decisive pivot after this attempt",
            )
        return RepairPlan(
            action=action,
            note="time slice ended with no answer; do not linger on the same "
            "attack surface — pivot to a different vector from prior leads",
        )
    if outcome.exit_code not in (None, 0):
        # 崩溃 / 被信号杀死：计划未走完，先原样重投（瞬态故障假设）
        return RepairPlan(action="retry_same", note=f"solver exited {outcome.exit_code}; retry")
    return RepairPlan(
        action="recompile",
        note="previous attempt finished cleanly with zero candidates; the "
        "primary surface is exhausted — focus on less-probed vectors "
        "(exploited leads, untried protocols, alternative entry points)",
    )
