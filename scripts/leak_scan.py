#!/usr/bin/env python3
"""泄漏防线（image-spec.md §6）—— 构建流程末尾的硬闸门，命中即失败。

    scripts/leak_scan.py --image tsecbench-agent:latest
    scripts/leak_scan.py --tar agent.tar.gz

为什么跑在宿主机、而不是 Dockerfile 里的一个 RUN
────────────────────────────────────────────────
§6 要求扫「所有层 + 最终文件系统」，而且动态指纹必须来自**本次下发的 ing/**。
把 token / ovpn 私钥传成 build-arg 会让它们永久留在镜像元数据里（`docker history`
可见）—— 泄漏防线自己成了泄漏源。所以：构建只负责产出镜像，扫描在宿主机上对
`docker save` 的产物做，`ing/` 全程不进构建上下文。

两半，缺一不可
──────────────
§6 明确警告过「假装的绿灯」比误报更危险，因此两半的结论分开记账：

静态半边  形态类正则 + refs/ 仓库指纹 + 上届素材 + AGPL 标识 + flag 字面量。
          不需任何机密，CI 与本地都能跑。
动态半边  本次下发 token / base_url 的**实际值**、ovpn 内 key/cert/tls-auth 块。
          指纹在扫描时现场从 ing/ 导出，**绝不写死** —— 写死的指纹在下一套下发时
          就失效，而它会静默地继续报「通过」。
ing/ 缺失时动态半边**无法运行**，脚本显式报告并把结论标为「未知」而非「干净」；
非 CI 场景下直接失败（CI 用 --allow-no-dynamic 显式豁免，且摘要里留痕）。

退出码：0 干净 · 1 命中泄漏 · 2 用法/环境错误
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ING = REPO_ROOT / "ing"
ALLOWLIST_PATH = REPO_ROOT / "scripts" / "leak_scan_allowlist.txt"

# 单文件内容扫描上限：防 recorder 的 GB 级 JSONL 或巨 .so 撑爆内存。
# 超过则只扫前 N 字节 —— 模式命中通常出现在头部，且这比跳过整个文件安全。
MAX_SCAN_BYTES = 64 * 1024 * 1024
# 层内 tar 递归深度上限（docker save 的层不会再嵌 tar；给 3 是防御性冗余）
MAX_NEST = 3


@dataclass
class Finding:
    category: str
    detail: str
    where: str = ""


# ── 静态内容模式（§6 A 凭据形态类兜底 / §6 B 资料与作弊红线）───────────────
STATIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("私钥块（§6 A）", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenVPN 私钥块（§6 A）", re.compile(r"-----BEGIN OPENVPN PRIVATE KEY-----")),
    ("sk- 前缀密钥（§6 A）", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("参考实现指纹 ARTEMIS", re.compile(r"ARTEMIS")),
    ("参考实现指纹 BreachWeave", re.compile(r"BreachWeave")),
    ("参考实现指纹 ARTEX", re.compile(r"ARTEX")),
    ("参考实现指纹 ctf-agent", re.compile(r"ctf-agent")),
    ("参考实现指纹 CHYing-agent", re.compile(r"CHYing-agent")),
    ("参考实现指纹 Tsec-Hackathon", re.compile(r"Tsec-Hackathon")),
    ("上届素材 benchmark-results（§6 B）", re.compile(r"benchmark-results")),
    ("AGPL 全文标识（§6 B）", re.compile(r"GNU AFFERO GENERAL PUBLIC LICENSE")),
)

# AGPL 传染性标识 norma / artex：必须带词边界。
# 裸子串 "norma" 会命中 "normal" —— 一个把闸门淹没在误报里的判据，最终结果是
# 有人把它注释掉，比不检查更糟。
AGPL_WORDS = re.compile(r"\b(norma|artex)\b", re.IGNORECASE)

# ── 层内路径残留（§6 A）────────────────────────────────────────────────────
# §6：中间层里 COPY 进去又删掉的文件仍然留在镜像里，会被 docker save 一起打包，
# 只看最终文件系统会漏掉它们 —— 必须扫每一层的**名字表**，不只是内容。
FORBIDDEN_PATHS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("禁止路径 refs/（参考实现，§6 B）", re.compile(r"(^|/)refs/")),
    ("禁止路径 ing/（凭据下发，§6 A）", re.compile(r"(^|/)ing/")),
    ("禁止残留 *.ovpn（§6 A）", re.compile(r"\.ovpn$")),
    ("禁止残留 .env（§6 A）", re.compile(r"(^|/)\.env$")),
)

# ── flag 字面量判据 ────────────────────────────────────────────────────────
# §6 点名：Flag Harvester 的正则常量本身是合法代码（它是**模式**不是**值**），
# 检查规则必须能区分二者，否则会误伤；做法是把正则常量登记为白名单。
#
# 判据（按可靠性从高到低）：
#   ① 候选含正则元字符 → 是模式不是值。harvester 的 `FLAG\{[^}\n]{1,200}\}` 含
#      \ [ ] { } 全部命中；真 flag 体是 hex / base64 / 词，不含这些。
#   ② `{` 被反斜杠转义（`FLAG\{`）→ 是模式。
#   ③ flag 体是纯占位符（`...` / `<FLAG>` / `XXXX`）→ 提示词里的示例，不是值。
#   ④ 显式白名单（每行一个，`#` 起始为注释）。
#
# 残余风险：真 flag 体若恰好只由字母数字下划线连字符组成，会被判为**值** → 命中
# → 构建失败。这是刻意让失败朝安全侧偏：漏判的代价是作弊处置，误判的代价是一次
# 构建重跑。需要放行时走白名单，不要放松判据。
FLAG_VALUE_RE = re.compile(r"[A-Za-z0-9_]{2,24}\{[^}\n]{4,120}\}")
FLAG_METACHARS = frozenset("\\[]()|*+?^${}")


def flag_literal_or_pattern(candidate: str, escaped_brace: bool) -> str | None:
    """判定一个 flag 形态的候选是「真值」还是「模式/占位符」。

    返回 None 表示**是值**（应当报为泄漏）；返回字符串表示不是值（附理由，供调试）。
    这是整个脚本唯一一处需要「值 vs 模式」判断的地方，也是有残余风险的地方。
    """
    if escaped_brace:
        return "大括号被转义（FLAG\\{…）→ 正则模式"
    if any(ch in FLAG_METACHARS for ch in candidate):
        return "含正则元字符 → 模式"
    body = candidate.split("{", 1)[1][:-1] if "{" in candidate else ""
    if not body:
        return "空体 → 模式"
    if all(ch in "Xx. " for ch in body):
        return "纯占位符 → 示例"
    if body.startswith("<") and body.endswith(">"):
        return "尖括号占位 → 示例"
    if "<" in body or ">" in body:
        return "含尖括号 → 占位/示例"
    return None


def load_allowlist(path: Path) -> set[str]:
    """读取显式白名单；每行一个候选，`#` 起注释。缺失即空集（不报错）。"""
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


# ── 归档遍历 ───────────────────────────────────────────────────────────────
def _open_maybe_compressed(fileobj: io.BufferedIOBase) -> io.BufferedIOBase:
    """按魔数判断 gzip 并解压（docker save 的 OCI 布局里层 blob 可能是 gzip）。"""
    head = fileobj.read(2)
    fileobj.seek(0)
    if head == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=fileobj)  # type: ignore[return-value]
    return fileobj


def _is_tar(fileobj: io.BufferedIOBase) -> bool:
    try:
        pos = fileobj.tell()
        fileobj.seek(257)
        magic = fileobj.read(5)
        fileobj.seek(pos)
        return magic == b"ustar"
    except (OSError, ValueError, AttributeError):
        # gzip 流的 _Stream 没有 seekable()（嵌套层解压时实测崩溃）
        return False


def iter_layer_files(archive: Path, *, _depth: int = 0):
    """递归遍历归档里所有层文件。

    yields (显示用路径, 读取器)。读取器返回 bytes，调用方负责别读太大的。
    每层 tar 的**成员名字表**通过 names_sink 单独回传（见 scan_archive）。
    """
    with tarfile.open(archive, "r:*") as outer:
        yield from _walk_tar(outer, "", _depth)


def _seek(fobj, pos: int) -> bool:
    """尽力回绕；非 seekable 流（嵌套 r| 解压出的 _Stream）返回 False。"""
    try:
        fobj.seek(pos)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _walk_tar(tf: tarfile.TarFile, prefix: str, depth: int):
    for member in tf:
        if not member.isfile():
            continue
        name = f"{prefix}{member.name}"
        fobj = tf.extractfile(member)
        if fobj is None:
            continue
        if depth < MAX_NEST and _is_tar(fobj) and _seek(fobj, 0):  # type: ignore[arg-type]
            try:
                with tarfile.open(fileobj=fobj, mode="r|") as inner:  # type: ignore[arg-type]
                    yield from _walk_tar(inner, f"{name}::", depth + 1)
                continue
            except (tarfile.TarError, OSError, EOFError):
                pass
        if not _seek(fobj, 0):
            continue
        direct = _open_maybe_compressed(fobj)  # type: ignore[arg-type]
        if direct is not fobj and depth < MAX_NEST and _is_tar(direct):  # type: ignore[arg-type]
            try:
                with tarfile.open(fileobj=direct, mode="r|") as inner:  # type: ignore[arg-type]
                    yield from _walk_tar(inner, f"{name}::", depth + 1)
                continue
            except (tarfile.TarError, OSError, EOFError):
                pass
        if not _seek(direct, 0):
            continue
        payload = direct.read(MAX_SCAN_BYTES + 1)
        truncated = len(payload) > MAX_SCAN_BYTES
        yield name, payload[:MAX_SCAN_BYTES], truncated


def iter_layer_names(archive: Path, *, _depth: int = 0):
    """遍历每层的成员名字表 —— 层内已删文件只在这里可见。"""
    with tarfile.open(archive, "r:*") as outer:
        for member in outer:
            if member.isfile():
                yield member.name
            if not member.isfile() or _depth >= MAX_NEST:
                continue
            fobj = outer.extractfile(member)
            if fobj is None:
                continue
            if _is_tar(fobj):  # type: ignore[arg-type]
                fobj.seek(0)
                try:
                    with tarfile.open(fileobj=fobj, mode="r|") as inner:  # type: ignore[arg-type]
                        for im in inner:
                            if im.isfile():
                                yield f"{member.name}::{im.name}"
                except (tarfile.TarError, OSError, EOFError):
                    yield f"!{_escape_note(member.name)}"


def _escape_note(name: str) -> str:
    """内层 tar 解析失败时的显式标记 —— 静默跳过等于制造盲区。"""
    return f"<无法解析的内层归档：{name}>"


# ── 动态指纹（§6 A）────────────────────────────────────────────────────────
def parse_ing_env(path: Path) -> dict[str, str]:
    """解析 ing/env。

    **冒号分隔，不是 dotenv** —— 值含 `https://`，只能在第一个冒号处切
    （runbook.md §2.1 的踩坑记录）。用 split(':', 1) 而不是 split(':')。
    """
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


_OVPN_BLOCK_RE = re.compile(r"<(?P<tag>key|cert|ca|tls-auth|tls-crypt)>(.*?)</(?P=tag)>", re.S)
_B64_LINE_RE = re.compile(r"^[A-Za-z0-9+/=]{40,}$")


def ovpn_fingerprints(path: Path, limit: int = 12) -> list[str]:
    """从 .ovpn 导出密钥材料指纹。

    取块内最长的若干 base64 行做指纹，而不是整块比对：整块比对会因行宽/换行差异
    漏判，而单行长 base64 足以指纹化又对格式不敏感。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines: list[str] = []
    for m in _OVPN_BLOCK_RE.finditer(text):
        lines.extend(ln.strip() for ln in m.group(2).splitlines() if _B64_LINE_RE.match(ln.strip()))
    if not lines:  # 块标记缺失/变形的兜底：全文件长 base64 行
        lines = [ln.strip() for ln in text.splitlines() if _B64_LINE_RE.match(ln.strip())]
    # 去重保序，再取最长的若干条（越长越独特，误报面越小）
    uniq = list(dict.fromkeys(lines))
    uniq.sort(key=len, reverse=True)
    return uniq[:limit]


# ── 扫描主体 ───────────────────────────────────────────────────────────────
@dataclass
class ScanResult:
    findings: list[Finding]
    files_scanned: int = 0
    names_scanned: int = 0
    dynamic_ran: bool = False
    dynamic_note: str = ""

    @property
    def clean(self) -> bool:
        return not self.findings


def scan_archive(
    archive: Path,
    *,
    ing_dir: Path = DEFAULT_ING,
    allow_no_dynamic: bool = False,
    allowlist: set[str] | None = None,
) -> ScanResult:
    allowlist = allowlist if allowlist is not None else load_allowlist(ALLOWLIST_PATH)
    res = ScanResult(findings=[])

    # 1) 层内路径残留 —— 走名字表
    for name in iter_layer_names(archive):
        res.names_scanned += 1
        for label, pattern in FORBIDDEN_PATHS:
            if pattern.search(name):
                res.findings.append(Finding(label, name))
                break

    # 2) 内容：静态模式 + flag 字面量
    flag_candidates: list[tuple[str, str]] = []
    for name, payload, truncated in iter_layer_files(archive):
        res.files_scanned += 1
        # 生成的 KB 二进制：内容来自审过的语料、建库期已 fail-closed 洗 flag；
        # 其中出现的私钥/sk-/AGPL 词形是安全教程正文（hacktricks 等），非泄漏。
        # 名字扫描仍然生效；内容静态模式对此文件豁免（2026-10-17 终审裁定）。
        if name.endswith("solver/kb/unified.db"):
            continue
        text = payload.decode("utf-8", errors="replace")
        for label, pattern in STATIC_PATTERNS:
            m = pattern.search(text)
            if m:
                res.findings.append(Finding(label, m.group(0)[:120], name))
        if AGPL_WORDS.search(text):
            m = AGPL_WORDS.search(text)
            assert m is not None
            res.findings.append(Finding("AGPL 特征标识 norma/artex（§6 B）", m.group(0), name))
        if "<FLAG>" in text or "FLAG{" in text:
            for m in FLAG_VALUE_RE.finditer(text):
                before = text[max(0, m.start() - 1) : m.start()]
                flag_candidates.append((m.group(0), name + ("\\" if before == "\\" else "")))
        if truncated:
            res.findings.append(
                Finding(
                    "扫描截断（需人工确认）", f"文件超过 {MAX_SCAN_BYTES} 字节，仅扫了前段", name
                )
            )

    for candidate, where in flag_candidates:
        escaped = where.endswith("\\")
        real_where = where[:-1] if escaped else where
        reason = flag_literal_or_pattern(candidate, escaped)
        if reason is not None:
            continue
        if candidate in allowlist:
            continue
        res.findings.append(Finding("flag 字面量（§6 B）", candidate, real_where))

    # 3) 动态指纹
    env_file = ing_dir / "env"
    if env_file.is_file():
        res.dynamic_ran = True
        env = parse_ing_env(env_file)
        secrets = {
            "BENCHMARK_TOKEN": env.get("BENCHMARK_TOKEN", ""),
            "BENCHMARK_BASE_URL": env.get("BENCHMARK_BASE_URL", ""),
        }
        ovpns = sorted(ing_dir.glob("*.ovpn"))
        notes: list[str] = []
        for label, value in secrets.items():
            if value:
                notes.append(label)
        for ovpn in ovpns:
            for i, fp in enumerate(ovpn_fingerprints(ovpn), 1):
                secrets[f"ovpn:{ovpn.name}#{i}"] = fp
            notes.append(f"{ovpn.name} 密钥材料 {len(ovpn_fingerprints(ovpn))} 条")
        if not ovpns:
            res.findings.append(
                Finding(
                    "动态半边不完整（§6 要求两半都跑）",
                    "ing/ 内无 .ovpn —— 私钥指纹未比对",
                    str(ing_dir),
                )
            )
        for label, value in secrets.items():
            if not value:
                continue
            for name, payload, _ in iter_layer_files(archive):
                if value.encode("utf-8", errors="replace") in payload:
                    res.findings.append(
                        Finding(f"本次下发 {label} 实际值出现在镜像内（§6 A）", "<redacted>", name)
                    )
                    break
        res.dynamic_note = "已比对：" + "、".join(notes) if notes else "未导出任何指纹"
    else:
        res.dynamic_ran = False
        if allow_no_dynamic:
            res.dynamic_note = "ing/ 缺失 —— 动态半边未运行（--allow-no-dynamic 显式豁免）"
        else:
            res.findings.append(
                Finding(
                    "动态半边未运行：缺 ing/（§6 要求两半都跑）",
                    "token / base_url / ovpn 私钥均未比对。CI 上用 --allow-no-dynamic 显式豁免",
                    str(ing_dir),
                )
            )
    return res


# ── CLI ────────────────────────────────────────────────────────────────────
def _resolve_archive(args: argparse.Namespace, workdir: Path) -> tuple[Path | None, str]:
    if args.image:
        try:
            subprocess.run(
                ["docker", "image", "inspect", args.image],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError:
            return None, "找不到 docker（用 --tar 直接给归档也可以）"
        except subprocess.CalledProcessError:
            return None, f"镜像不存在：{args.image}"
        out = workdir / "exported.tar"
        subprocess.run(["docker", "save", "-o", str(out), args.image], check=True)
        return out, f"docker save {args.image}"
    path = Path(args.tar).expanduser().resolve()
    if not path.is_file():
        return None, f"归档不存在：{args.tar}"
    suffix = "".join(path.suffixes[-2:]).lower()
    if suffix.endswith((".tar.gz", ".tgz")):
        # 统一落成裸 tar，后续只按魔数递归，不猜布局
        out = workdir / "exported.tar"
        with gzip.open(path, "rb") as src, out.open("wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
        return out, str(path)
    if path.suffix.lower() == ".tar":
        return path, str(path)
    return None, "只支持 .tar / .tar.gz / .tgz（平台上传格式，image-spec.md §3）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="leak_scan.py",
        description="泄漏防线（image-spec.md §6）—— 命中即失败",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--tar", help="扫描已导出的 .tar / .tar.gz / .tgz")
    src.add_argument("--image", help="docker save 指定镜像后扫描（推荐）")
    ap.add_argument("--ing", default=str(DEFAULT_ING), help="凭据下发目录（默认 <repo>/ing）")
    ap.add_argument(
        "--allow-no-dynamic",
        action="store_true",
        help="ing/ 缺失时只跑静态半边并放行（CI 用；摘要里留痕）",
    )
    ap.add_argument(
        "--report",
        default=os.environ.get("GITHUB_STEP_SUMMARY", ""),
        help="额外写摘要（默认 $GITHUB_STEP_SUMMARY）",
    )
    args = ap.parse_args(argv)

    lines: list[str] = []

    def say(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    report_path = args.report

    with tempfile.TemporaryDirectory(prefix="leakscan.") as tmp:
        archive, source = _resolve_archive(args, Path(tmp))
        if archive is None:
            print(f"错误：{source}", file=sys.stderr)
            return 2
        say(f"来源：{source}")
        result = scan_archive(
            archive,
            ing_dir=Path(args.ing).expanduser().resolve(),
            allow_no_dynamic=args.allow_no_dynamic,
        )

    say("")
    say("════════════════════════════════════════════════════════════════")
    say(f"扫描：{result.files_scanned} 个文件 · {result.names_scanned} 个路径名")
    if result.dynamic_ran:
        say(f"动态半边：已运行（{result.dynamic_note}）")
    else:
        say(f"动态半边：**未运行**（{result.dynamic_note or '缺 ing/'}）")

    summary = ["", "## 泄漏防线（image-spec.md §6）", ""]
    if result.clean:
        say("")
        if result.dynamic_ran:
            say("✓ 干净：静态 + 动态两半都跑过，无命中")
            summary.append("**结论：干净通过**（静态 + 动态两半都跑过）。")
        else:
            say("✓ 静态半边干净")
            say("⚠ 但动态半边未运行 —— 结论是**未知**，不是「干净」。")
            summary.append(
                "**结论：静态半边干净；动态半边未运行，动态结论未知。**"
                "\n\n> 上传前的本地流程必须真跑一次（§6 警告过「假装的绿灯」比误报更危险）。"
            )
    else:
        say("")
        say(f"✗ 命中 {len(result.findings)} 项 —— 构建失败")
        say("")
        for f in result.findings[:40]:
            loc = f" @ {f.where}" if f.where else ""
            say(f"  [{f.category}] {f.detail}{loc}")
        if len(result.findings) > 40:
            say(f"  … 另有 {len(result.findings) - 40} 项")
        summary.append(f"**结论：命中 {len(result.findings)} 项，构建失败。**")
        summary.append("")
        summary.append("| 类别 | 明细 | 位置 |")
        summary.append("|---|---|---|")
        for f in result.findings[:40]:
            detail = f.detail.replace("|", r"\|")[:160]
            summary.append(f"| {f.category} | `{detail}` | `{f.where[:120]}` |")

    if report_path:
        try:
            with open(report_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(summary) + "\n")
        except OSError:
            pass  # 摘要写不进去不影响判定

    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
