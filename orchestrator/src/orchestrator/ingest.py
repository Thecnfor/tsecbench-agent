"""Ingest Resolver —— `ing/` 下发内容的形状归一化（architecture.md §6.5）。

每次测试平台都会下发一套新的 `ing/` 配置，文件名带可变任务 ID、键名会换代、
分隔符形态不定。本模块按**内容**分类，不做任何文件名硬编码。

硬规则（实现即契约）：
  - 归一化 `KEY=VALUE` / `KEY: VALUE` / `KEY:VALUE` / JSON，只在**第一个**分隔符处切
    （否则含 `https://` 的值会被切成 `https`，§7.1 #4）；
  - 缺 token / base_url → 硬失败 ValueError，绝不拿空 URL 发请求（§6.5 第 6 条）；
  - 优先级：显式环境变量 > ing/ 中发现的值 > 内置默认；
  - 产出脱敏 `ingest_report`（值不落报告，只记键名/长度，§7.1 #20）；
  - token 只留给 L0 返回值，绝不下传 solver（§6.5.1，白名单在 config.build_solver_env）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 默认探测序（§6.5 第 1 条）：INGEST_DIR 环境变量可覆盖
DEFAULT_INGEST_DIRS = ("/work/ing", "/ing", "./ing")

# 键名别名表（§6.5 第 4 条，应对键名换代：上届 Agent-Token / SERVER_HOST）
ALIASES: dict[str, tuple[str, ...]] = {
    "platform_token": ("BENCHMARK_TOKEN", "AGENT_TOKEN", "TOKEN"),
    "platform_base_url": ("BENCHMARK_BASE_URL", "SERVER_HOST", "BASE_URL"),
}

_KV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)[=:](.*)$")
_OVPN_BLOCK = re.compile(r"<(ca|cert|key)>", re.IGNORECASE)


# ---------------------------------------------------------------- 解析原语 ----
def parse_kv_line(line: str) -> tuple[str, str] | None:
    """解析一行键值。只在第一个 `=` 或 `:` 处切（§7.1 #4/#5 的核心纪律）。

    处理：剥 BOM/CRLF、跳空行与 `#` 注释、剥成对首尾引号、剥行尾空白。
    不是键值行返回 None。
    """
    line = line.lstrip("\ufeff").rstrip("\r\n").strip()
    if not line or line.startswith("#"):
        return None
    m = _KV_LINE.match(line)
    if m is None:
        return None
    key, value = m.group(1), m.group(2).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def parse_kv_text(text: str) -> tuple[dict[str, str], list[str]]:
    """解析多行键值文本。返回 (键值对, 警告列表)。

    同一键重复出现：后者优先 + 记警告，不静默覆盖（§7.1 #11）。
    """
    values: dict[str, str] = {}
    warnings: list[str] = []
    for raw in text.splitlines():
        parsed = parse_kv_line(raw)
        if parsed is None:
            continue
        key, value = parsed
        if key in values and values[key] != value:
            warnings.append(f"duplicate key overwritten: {key}")
        values[key] = value
    return values, warnings


def parse_json_config(text: str) -> dict[str, str] | None:
    """JSON 配置：递归展平成键值；解析失败返回 None（交给下一级分类）。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    def flatten(obj: object, prefix: str) -> dict[str, str]:
        out: dict[str, str] = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                out.update(flatten(v, f"{prefix}{k}." if prefix else f"{k}."))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                out.update(flatten(v, f"{prefix}{i}."))
        else:
            key = prefix.rstrip(".")
            out[key] = "" if obj is None else str(obj)
        return out

    return flatten(data, "")


def classify_content(text: str) -> str:
    """按内容分类（§6.5 第 2 条表格）。文件名只做辅助，这里完全不看文件名。

    返回 "ovpn" | "env" | "json" | "ca" | "unknown"。
    """
    stripped = text.lstrip("\ufeff").lstrip()
    if stripped.startswith("{") and parse_json_config(stripped) is not None:
        return "json"
    has_ovpn_blocks = bool(_OVPN_BLOCK.search(text))
    has_client_directive = bool(re.search(r"^\s*client\s*$", text, re.MULTILINE | re.IGNORECASE))
    if has_client_directive and has_ovpn_blocks:
        return "ovpn"
    if "-----BEGIN CERTIFICATE-----" in text and not has_ovpn_blocks:
        return "ca"
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines:
        kv_hits = sum(1 for ln in lines if _KV_LINE.match(ln.lstrip("\ufeff").strip()))
        if kv_hits / len(lines) >= 0.5:
            return "env"
    return "unknown"


# ---------------------------------------------------------------- 报告结构 ----
@dataclass(slots=True)
class IngestReport:
    """摄取报告（脱敏：任何值不进本结构，只记键名/长度/分类，§7.1 #20）。"""

    files: list[dict[str, object]] = field(default_factory=list)  # {path, kind, keys, action}
    warnings: list[str] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)  # §7.1 #13 保留未识别键名
    used_source: dict[str, str] = field(default_factory=dict)  # canonical → "env"|"ing"
    token_found: bool = False


@dataclass(slots=True)
class IngestResult:
    """resolve_ingest 的产物。token/base_url 只在这里（L0），绝不进 report。"""

    token: str
    base_url: str
    vpn_configs: list[str]  # ovpn 文件路径（确定性排序），供 §6.3 拨号
    report: IngestReport


# ---------------------------------------------------------------- 主流程 ----
def _discover_dirs(root: str | None, environ: dict[str, str]) -> list[Path]:
    """发现 ing/ 目录。优先级：显式参数 > INGEST_DIR > 默认探测序。
    全部不存在是合法状态（托管模式，§7.1 #15）。"""
    candidates: list[str] = []
    if root:
        candidates.append(root)
    elif env_dir := environ.get("INGEST_DIR"):
        candidates.append(env_dir)
    else:
        candidates.extend(DEFAULT_INGEST_DIRS)
    return [Path(c) for c in candidates if Path(c).is_dir()]


def _canonicalize(kv: dict[str, str], report: IngestReport) -> dict[str, str]:
    """键名别名映射 → 内部规范键；未识别键保留进 report（§7.1 #12/#13）。"""
    canonical: dict[str, str] = {}
    reverse = {alias: c for c, aliases in ALIASES.items() for alias in aliases}
    for key, value in kv.items():
        c = reverse.get(key)
        if c is not None:
            if c in canonical and canonical[c] != value:
                report.warnings.append(f"alias collision for {c}: {key} overrides earlier alias")
            canonical[c] = value
        else:
            report.unknown_keys.append(key)
    return canonical


def _merge_priorities(
    from_env: dict[str, str], from_ing: dict[str, str], report: IngestReport
) -> dict[str, str]:
    """优先级裁决：显式环境变量 > ing/ 发现值（§6.5 第 5 条）。"""
    merged = dict(from_ing)
    for key, value in from_env.items():
        if key in from_ing and from_ing[key] != value:
            report.warnings.append(f"{key}: env overrides ing value")
        merged[key] = value
        report.used_source[key] = "env"
    for key in from_ing:
        report.used_source.setdefault(key, "ing")
    return merged


def _reject_bad_base_url(
    kv: dict[str, str], environ: dict[str, str], report: IngestReport
) -> dict[str, str]:
    """明显截断/格式非法的值拒绝采用，回退下一优先级（§6.5 第 6 条）。

    典型：base_url 不含 `://`（回归「值内含冒号被切碎成 https」的历史错误）。
    """
    key = "platform_base_url"
    if key in kv and key not in environ and "://" not in kv[key]:
        report.warnings.append(
            f"{key}: rejected malformed value (len={len(kv[key])}), falling back"
        )
        kv = {k: v for k, v in kv.items() if k != key}
    return kv


def resolve_ingest(root: str | None = None, environ: dict[str, str] | None = None) -> IngestResult:
    """扫描 ing/ 并归一化解析。缺 token / base_url → 硬失败 ValueError。"""
    environ = dict(os.environ if environ is None else environ)
    report = IngestReport()

    # 1. 显式环境变量（最高优先级，同样走别名映射 —— 托管模式全靠这条路）
    env_canonical = _canonicalize(
        {k: v for k, v in environ.items() if k in {a for al in ALIASES.values() for a in al}},
        report,
    )

    # 2. ing/ 目录（本地模式）。不存在是合法状态。
    ing_kv: dict[str, str] = {}
    vpn_configs: list[str] = []
    for d in _discover_dirs(root, environ):
        for path in sorted(d.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                report.warnings.append(f"{path}: unreadable ({type(exc).__name__})")
                continue
            kind = classify_content(text)
            entry: dict[str, object] = {"path": str(path), "kind": kind}
            if kind in ("env", "json"):
                if kind == "env":
                    values, warns = parse_kv_text(text)
                    report.warnings.extend(f"{path.name}: {w}" for w in warns)
                else:
                    values = parse_json_config(text) or {}
                for k, v in values.items():
                    if k in ing_kv and ing_kv[k] != v:
                        report.warnings.append(
                            f"cross-file duplicate: {k} overridden by {path.name}"
                        )
                    ing_kv[k] = v
                entry["keys"] = sorted(values)
                entry["action"] = "parsed"
            elif kind == "ovpn":
                vpn_configs.append(str(path))
                entry["action"] = "vpn-config"  # 值不读入任何内存结构
            else:  # ca / unknown：忽略 + 记报告（绝不解析、绝不执行）
                entry["action"] = "ignored"
            report.files.append(entry)
        if vpn_configs or ing_kv:
            break  # 命中第一个有效目录即止，避免多目录重复摄取
    vpn_configs.sort()  # 多个 ovpn：确定性次序（§7.1 #14）

    ing_canonical = _canonicalize(ing_kv, report)

    # 3. 合并（env > ing），并对 ing 来源做明显坏值拒绝
    merged = _merge_priorities(env_canonical, ing_canonical, report)
    merged = _reject_bad_base_url(merged, env_canonical, report)

    # 4. 硬失败分级（§6.5 第 6 条）：无凭据比拿空 URL 请求好得多
    missing = [k for k in ("platform_token", "platform_base_url") if k not in merged]
    if missing:
        names = {
            "platform_token": "BENCHMARK_TOKEN",
            "platform_base_url": "BENCHMARK_BASE_URL",
        }
        raise ValueError(
            "no platform credentials: missing "
            + ", ".join(names[k] for k in missing)
            + " (checked env and ing/); refusing to start"
        )

    report.token_found = True
    return IngestResult(
        token=merged["platform_token"],
        base_url=merged["platform_base_url"],
        vpn_configs=vpn_configs,
        report=report,
    )
