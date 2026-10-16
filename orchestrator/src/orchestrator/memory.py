"""攻击图记忆层 —— TriviumDB 支撑的跨轮上下文（harness-v2.md §6）。

**定位**：prior.json 契约（architecture.md §2.5）不变；本模块是**增强读通道**，
不是新的耦合点。TriviumDB 缺失、导入失败或 `MEMORY_TDB=off` 时全部退化为空操作，
编排器行为与没有它时完全一致 —— 这是「任一环节失败即退回原路径」的落地。

**三信号检索（TSNG）**，实测在 triviumdb 0.8.8 上逐条验证过：

  - **文本**：`index_text(id, text)` + `build_text_index()` → `search_hybrid(alpha=0)`
  - **属性**：`indexed_lookup({"challenge": code})` / `payload_filter=` / `tql`
  - **图**：`neighbors` / `reachable` / `search_hybrid(expand_depth=N)`

向量信号暂缺（本项目没有 embedding 来源）；`dim` 保留位，将来接上 embedding
即可启用，其余三路已足够支撑「在同一题的攻击图邻域内做语义检索」。

**单写者**：写侧收敛到编排器一处（`record`），执行体只产出 prior.json。
跨进程写 `.tdb` 是排他锁（多读单写），编排器独占写权 → 天然无争用。
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path

try:  # 可选依赖：缺失即整体降级，绝不让 import 失败拖垮编排器
    import triviumdb
except Exception:  # pragma: no cover - 环境相关
    triviumdb = None  # type: ignore[assignment]

# 节点状态机（对应决赛架构的 UNEXPLORED → EXPLORING → EXPLORED/DEAD_END）
STATE_UNEXPLORED = "UNEXPLORED"
STATE_EXPLORING = "EXPLORING"
STATE_EXPLORED = "EXPLORED"
STATE_DEAD_END = "DEAD_END"

_KIND_ASSET = "asset"
_KIND_LEAD = "lead"
_KIND_EXPLORED = "explored"


def _node_id(key: str) -> int:
    """key → 稳定 64 位正整数 ID（TriviumDB 的 ID 必须是整数）。

    用 blake2b 摘要而非自增计数器：确定性、跨进程一致、不需要额外的分配表。
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1  # 抹掉符号位


class AttackMemory:
    """一局跑分共享的攻击图记忆。`enabled=False` 时所有方法都是空操作。"""

    def __init__(self, path: Path, *, enabled: bool = True, dim: int = 64) -> None:
        self.path = path
        self._dim = dim
        # 无 embedding 来源 → 全部节点写零向量；向量信号留待接上 embedding 再启用。
        self._zero = [0.0] * dim
        self._db = None
        self._indexed = False
        if not enabled or triviumdb is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = triviumdb.TriviumDB(str(path), dim=dim, load_text_index=True)
        except Exception:
            self._db = None

    @property
    def available(self) -> bool:
        return self._db is not None

    # ---- 写侧（编排器独占）----------------------------------------------------
    def record(self, code: str, prior: dict) -> int:
        """把该题的 prior 双图写进攻击图。返回新写入的节点数（0 = 降级）。"""
        if self._db is None:
            return 0
        written = 0
        groups = (
            (_KIND_ASSET, prior.get("assets") or [], STATE_EXPLORED),
            (_KIND_LEAD, prior.get("open_leads") or [], STATE_UNEXPLORED),
            (_KIND_EXPLORED, prior.get("explored") or [], STATE_DEAD_END),
        )
        try:
            for kind, items, state in groups:
                for item in items:
                    text = item if isinstance(item, str) else str(item)
                    if not text.strip():
                        continue
                    key = f"{code}|{kind}|{text[:200]}"
                    nid = _node_id(key)
                    self._db.upsert_with_id(
                        nid,
                        self._zero,
                        {
                            "key": key,
                            "challenge": code,
                            "kind": kind,
                            "state": state,
                            "text": text[:500],
                        },
                    )
                    self._db.index_text(nid, text[:500])
                    written += 1
            if written and not self._indexed:
                self._db.create_index("challenge")
                self._db.create_index("state")
                self._db.build_text_index()
                self._indexed = True
            self._db.flush()
        except Exception:
            return 0
        return written

    def mark(self, code: str, kind: str, text: str, state: str) -> None:
        """更新某个已知节点的状态（EXPLORING / EXPLORED / DEAD_END）。"""
        if self._db is None:
            return
        key = f"{code}|{kind}|{text[:200]}"
        with contextlib.suppress(Exception):
            self._db.patch_payload(_node_id(key), {"state": state})

    # ---- 读侧（三信号）--------------------------------------------------------
    def recall(self, code: str, query_text: str, *, top_k: int = 5) -> list[dict]:
        """TSNG 检索：在本题的图邻域内按文本信号召回相关资产/线索。"""
        if self._db is None or not self._indexed:
            return []
        try:
            hits = self._db.search_hybrid(
                self._zero,
                query_text,
                top_k=top_k,
                hybrid_alpha=0.0,  # 纯文本权重（无 embedding 来源）
                min_score=0.0,
                expand_depth=1,  # 图信号：沿边扩散一跳
                payload_filter={"challenge": code},
            )
        except Exception:
            return []
        return [
            {"key": h.payload.get("key"), "kind": h.payload.get("kind"), "score": round(h.score, 3)}
            for h in hits
        ]

    def unexplored(self, code: str | None = None) -> list[dict]:
        """还没探过的节点（属性信号）。`code=None` 时跨全部题目。"""
        if self._db is None:
            return []
        try:
            filt = {"state": STATE_UNEXPLORED}
            if code:
                filt["challenge"] = code
            ids = self._db.indexed_lookup(filt, max_results=200)
        except Exception:
            return []
        out = []
        for nid in ids:
            try:
                payload = self._db.get_payload(nid)
            except Exception:
                continue
            out.append({"key": payload.get("key"), "challenge": payload.get("challenge")})
        return out

    def stats(self) -> dict:
        if self._db is None:
            return {"enabled": False}
        try:
            return {"enabled": True, "nodes": self._db.node_count(), **self._db.graph_stats()}
        except Exception:
            return {"enabled": True}

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.flush()
                self._db.close()
            except Exception:
                pass
            self._db = None
