"""ReActNet 图工程 —— 把策略编译成显式通信图（harness-v2.md §3）。

对齐 ReActNet 的核心思想：**优化拓扑 → 图工程**。planner 的自由文本要点
不再整段塞进 APPEND_SYSTEM，而是被编译成一组 stage 节点 + 带 why/how 语义
的有向边，逐跳携带自然语言指令。图本身可离线确定性编译（零 LLM）：LLM
的 planner 输出只作为节点内的指令词料，失败时整图降级为按题型播种的默认
拓扑。产物双份：渲染文本进 APPEND_SYSTEM（执行端消费），
out/plan_graph.json 落盘（编排器/观测台审计，§3.3）。
"""

from __future__ import annotations

import json
from pathlib import Path

# 每个题型的默认拓扑：stage 节点按顺序对齐执行位（§3.1 节点目录）。
_STAGES: dict[str, list[dict]] = {
    "web": [
        {"id": "recon", "job": "fingerprint the target surface (recon tool / headers / tech)"},
        {"id": "probe", "job": "verify each open lead against known web weakness classes"},
        {"id": "exploit", "job": "materialize the working exploit path for the strongest probe"},
        {"id": "verify", "job": "harvest and confirm the flag exactly as discovered"},
    ],
    "pwn": [
        {"id": "recon", "job": "inspect binary protections and entry surface"},
        {"id": "probe", "job": "find the memory-corruption primitive"},
        {"id": "exploit", "job": "weaponize primitive into shell/code execution"},
        {"id": "verify", "job": "read the flag from the target and confirm format"},
    ],
    "reverse": [
        {"id": "recon", "job": "identify file type, packer, reachable logic"},
        {"id": "probe", "job": "trace the guarded branch and extract the decision secret"},
        {"id": "exploit", "job": "replay the recovered key/answer to unlock the flag"},
        {"id": "verify", "job": "confirm the flag by rerunning the unlocked path"},
    ],
    "forensics": [
        {"id": "recon", "job": "classify the artifact and its embedded formats"},
        {"id": "probe", "job": "extract every stego/packaging layer until byte-tail level"},
        {"id": "exploit", "job": "decode the recovered payload that carries the flag"},
        {"id": "verify", "job": "re-derive flag with a second method before reporting"},
    ],
    "misc": [
        {"id": "recon", "job": "understand the challenge framing and interface"},
        {"id": "probe", "job": "attack the known leads breadth-first"},
        {"id": "exploit", "job": "converge on the strongest successful method"},
        {"id": "verify", "job": "confirm the flag and only then stop"},
    ],
}
_MAYBE_CATEGORY = "misc"

MAX_NODE_CHARS = 220


def _clip(text: str, limit: int = MAX_NODE_CHARS) -> str:
    return text.strip()[:limit]


def compile(task: dict, strategy: str, category: str, prior: dict | None) -> dict:
    """把 (task, planner 要点, prior 双图) 编成通信图；全部确定性，零 LLM。

    edges 语义（ReActNet 对齐）：每条边携带 when（进入条件）、why（动机）、
    how（怎么把上一跳的产出变成下一跳的输入）。未知题型回落 misc 拓扑。
    """
    key = category if category in _STAGES else ""
    stages = _STAGES.get(key) or _STAGES[_MAYBE_CATEGORY]
    assets = (prior or {}).get("assets") or []
    explored = (prior or {}).get("explored") or []
    leads = (prior or {}).get("open_leads") or []
    bullets = [b.strip() for b in strategy.replace("\n", "; ").split(";") if b.strip()]

    nodes = []
    for index, stage in enumerate(stages):
        node = {
            "id": stage["id"],
            "order": index,
            "instruction": _clip(strategy) if index == 1 else _clip(stage["job"]),
        }
        if stage["id"] == "recon":
            node["state"] = {
                "confirmed_assets": len(assets),
                "already_explored": len(explored),
            }
            if assets:
                node["instruction"] = _clip(
                    f"resume from confirmed assets ({len(assets)}); skip fresh recon for them"
                )
        if stage["id"] == "probe":
            node["open_leads"] = len(leads)
        if stage["id"] == "exploit":
            node["directives"] = [_clip(b, 160) for b in bullets[:3]]
        nodes.append(node)

    edges = []
    for a, b in zip(nodes, nodes[1:], strict=False):
        edges.append(
            {
                "from": a["id"],
                "to": b["id"],
                "when": f"{a['id']} produces a stable observation",
                "why": f"{b['id']} cannot start without that observation",
                "how": (
                    f"carry the {a['id']} result (targets/port/banner finding) as the input "
                    f"list for {b['id']}; skip steps already in explored[{len(explored)}]"
                ),
            }
        )
    return {"category": key or _MAYBE_CATEGORY, "nodes": nodes, "edges": edges}


def render(plan: dict) -> str:
    """渲染成注入文本：图即指令 —— 每跳会说何时/为何/如何（§3.2）。"""
    lines = ["# PLAN GRAPH (compiled communication graph — follow hop by hop)"]
    for edge in plan["edges"]:
        lines.append(
            f"- {edge['from']} -> {edge['to']} | when: {edge['when']} | "
            f"why: {edge['why']} | how: {edge['how']}"
        )
    for node in plan["nodes"]:
        if node.get("directives"):
            lines.append(f"- stage {node['id']} directives: " + " | ".join(node["directives"]))
    return "\n".join(lines)


def dump(out_dir: Path, plan: dict) -> None:
    """审计产物：图落盘给编排器/观测台读；失败不阻塞解题。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "plan_graph.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
