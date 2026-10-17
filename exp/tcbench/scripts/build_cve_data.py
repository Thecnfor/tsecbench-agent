#!/usr/bin/env python3
"""把 NVD 年度镜像（fkie-cad/nvd-json-data-feeds 的 .json.xz）与 CISA KEV 压成 KB 用的紧凑数据。

产出（放 KB/B-漏洞与利用/03-cve-data/）：
  nvd-2023-highcrit.jsonl   每行一条：{id,year,sev,cvss,prods,desc}
  kev.jsonl                 每行一条：{id,vendor,prods,added,due,notes}

只保留 high/critical 且发布年 ≥2023 的条目；KEV 全量（它本身只有 ~1.7k 条且都已在野利用）。
字段刻意精简：用途是"版本/产品 → CVE 候选"的检索，不做完整漏洞档案。
"""
from __future__ import annotations

import argparse
import json
import lzma
import re
import sys
from pathlib import Path

YEAR_MIN = 2023


def nvd_severity(vuln: dict) -> tuple[str, float, str] | None:
    """只认 CVSS v3.x / v4 的 (severity, score, vector)。
    v2 不收：v2 没有 critical 档，7 分即 HIGH，会把'高危'灌水。"""
    metrics = vuln.get("metrics", {})
    ordered = [metrics[k] for k in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30")
               if k in metrics]
    for m in ordered:
        for item in (m if isinstance(m, list) else [m]):
            data = item.get("cvssData", {})
            sev = (data.get("baseSeverity") or "").lower()
            if sev in {"high", "critical"}:
                return sev, float(data.get("baseScore", 0)), data.get("vectorString", "")
    return None


def products(vuln: dict, limit: int = 6) -> list[str]:
    out = []
    # fkie-cad 镜像带简化的 affected 段（vendor/product 明确），优先用
    for block in vuln.get("affected", []):
        for ad in block.get("affectedData", []):
            v, p2 = ad.get("vendor", ""), ad.get("product", "")
            if p2:
                out.append(f"{v}:{p2}".strip(":"))
    if out:
        return list(dict.fromkeys(out))[:limit]
    for c in vuln.get("configurations", []):
        for n in c.get("nodes", []):
            for cm in n.get("cpeMatch", []):
                m = re.match(r"cpe:2\.3:[^:]*:([^:]+):([^:]+)", cm.get("criteria", "") or "")
                if m:
                    vendor, prod = m.group(1), m.group(2)
                    if prod != "*":
                        out.append(f"{vendor}:{prod}")
    return list(dict.fromkeys(out))[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xz-dir", required=True, help="含 CVE-20xx.json.xz 的目录")
    ap.add_argument("--kev", required=True, help="KEV 原始 json")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--desc-chars", type=int, default=220)
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    n = 0
    with open(out / "nvd-2023-highcrit.jsonl", "w", encoding="utf-8") as f:
        for xz in sorted(Path(a.xz_dir).glob("CVE-*.json.xz")):
            year = int(xz.name.split(".")[0].split("-")[1])
            if year < YEAR_MIN:
                continue
            with lzma.open(xz, "rt", encoding="utf-8", errors="replace") as g:
                data = json.load(g)
            for vuln in (data.get("vulnerabilities") or data.get("cve_items") or []):
                cve = vuln.get("cve", vuln)
                cid = cve.get("id", "")
                if not cid.startswith("CVE-"):
                    continue
                s = nvd_severity(cve)
                if not s:
                    continue
                sev, score, vector = s
                desc = ""
                for d in cve.get("descriptions", []):
                    if d.get("lang") == "en":
                        desc = d.get("value", "")[: a.desc_chars]
                f.write(json.dumps({"id": cid, "year": year, "sev": sev, "cvss": score,
                                    "vector": vector, "prods": products(cve), "desc": desc},
                                   ensure_ascii=False) + "\n")
                n += 1

    kev = json.load(open(a.kev, encoding="utf-8"))
    m = 0
    with open(out / "kev.jsonl", "w", encoding="utf-8") as f:
        for v in kev.get("vulnerabilities", []):
            f.write(json.dumps({"id": v.get("cveID"), "vendor": v.get("vendorProject"),
                                "prods": [v.get("product", "")], "added": v.get("dateAdded"),
                                "due": v.get("dueDate"), "notes": v.get("shortDescription", "")[:200]},
                               ensure_ascii=False) + "\n")
            m += 1
    size = sum(p.stat().st_size for p in out.glob("*.jsonl")) / 1048576
    print(f"NVD high/critical ≥{YEAR_MIN}: {n} 条 | KEV: {m} 条 | 合计体积 {size:.1f} MB → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
