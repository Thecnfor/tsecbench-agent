"""Agent profiles —— 按题型注入策略提示包（对应 OpenHands agent-profiles service）。

分类是确定性关键词匹配，不花 LLM 调用；策略文本移植自 refs/CHYing-agent
(MIT, https://github.com/) 的多 agent 提示词体系，针对「单 pi headless 无
子 agent」的执行模型改写：delegation-rules 全部转为「自己直接做」的指令，
保留其攻击向量顺序 / 效率原则 / 停滞切换的核心内容。
"""

from __future__ import annotations

# 来源标注（THIRD_PARTY.md 同步登记）：refs/CHYing-agent/chying_agent/prompts/（MIT）
ATTRIBUTION = "# strategy pack adapted from CHYing-agent prompts (MIT)"

_CATEGORIES = ("web", "pwn", "reverse", "forensics", "misc")

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web": (
        "http",
        "https",
        "web",
        "url",
        "sqli",
        "xss",
        "ssrf",
        "cookie",
        "token",
        "api",
        "login",
        "upload",
        "nginx",
        "apache",
        "flask",
        "django",
        "express",
        "port 80",
        "port 443",
        ":80",
        ":443",
        ":8080",
        ":8000",
    ),
    "pwn": (
        "pwn",
        "binary",
        "stack",
        "heap",
        "overflow",
        "rop",
        "glibc",
        "shellcode",
        "nc ",
        "netcat",
        "elf",
        "canary",
        "format string",
    ),
    "reverse": (
        "reverse",
        "crackme",
        "decompile",
        "disassembl",
        "ghidra",
        "binary",
        "obfuscat",
        "packed",
        "upx",
        "keygen",
    ),
    "forensics": (
        "forensic",
        "pcap",
        "wireshark",
        "memory dump",
        "disk",
        "image file",
        "stegan",
        "exif",
        "wireshark",
        "volatility",
        "zip",
        "archive",
    ),
    "misc": ("misc", "crypto", "cipher", "rsa", "aes", "hash", "encoding", "qr"),
}

# 每类一份策略包（改写自 CHYing orchestrator_strategy / executor / reverse，
# MIT）。执行模型：pi headless + bash 工具，无子 agent、无浏览器专用通道。
_STRATEGY: dict[str, str] = {
    "web": """
# WEB STRATEGY
1. Recon first, always: fetch headers + body of / (curl -i), read HTML/JS comments,
   robots.txt, sitemap; list all endpoints, parameters, tech stack and version banners.
2. Version banners (server, X-Powered-By, JS framework strings) -> match known CVEs
   immediately; try public exploit patterns adapted to the target, do not guess blindly.
3. Standard vectors in order: injection points (sqli: quote/error/boolean/time),
   IDOR / access control on every object id, path traversal on file params, SSRF on
   url/fetch params, upload bypass (extension / content-type / magic bytes), SSTI on
   reflected input ({{7*7}}, ${7*7}), deserialization on cookies/base64 blobs.
4. Directory brute-force is a LAST resort, not the default: build a focused wordlist
   from the site's own links first; use ffuf/gobuster with -fc 404 and a timeout.
5. If a page returns near-empty HTML (<20 visible text nodes), suspect JS-rendered
   content: fetch its JS bundles and read them for hidden endpoints/secrets.
6. Stuck after 3 failed vectors on one attack surface -> switch surface, log what you
   tried as one line each, do not repeat a failed request shape.
""",
    "pwn": """
# PWN STRATEGY
1. Identify: `file`, `checksec --file=<bin>`; note libc version, protections.
2. Static read first: strings for hints; decompile main handlers (ghidra headless or
   objdump) and locate the input-reading function before touching the service.
3. Interact locally: `nc <host> <port>` or run the binary; map input format precisely
   (length, terminator, structure) with small python pwntools scripts.
4. Classic chain: overflow offset (cyclic pattern) -> leak (puts@got) -> ret2libc or
   ROP; if canary: leak via format string %p chain; if PIE: leak a .text pointer.
5. Keep payloads scripted (pwntools), never hand-typed; test locally before remote.
""",
    "reverse": """
# REVERSE STRATEGY
1. `file` + `strings -n 8` first — many crackmes leak the check logic or key format.
2. Identify packer (UPX etc.) and unpack before decompiling; ghidra headless
   (`analyzeHeadless`) for decompile, export the decompiled C of main + check funcs.
3. Understand the validation as a pure function: reproduce it in python, then invert
   or brute-force per character (flag format narrows the search hugely).
4. Dynamic: gdb with breakpoints on strcmp/memcmp/input handlers; watch what bytes
   the check actually compares.
""",
    "forensics": """
# FORENSICS STRATEGY
1. Identify the artifact: `file`, `binwalk`, `exiftool` before any tool run.
2. Archives: list first (`7z l`), try common passwords (flag-related words, empty,
   "infected"), check comments and extra fields.
3. Images: layers/stego in order — strings, zsteg/binwalk, LSB check, alpha channel,
   metadata; if pcap: follow TCP/HTTP streams, export objects, look at DNS anomalies.
4. Memory dumps: volatility plugin order pslist -> cmdline -> filescan -> dumpfiles.
""",
    "misc": """
# MISC STRATEGY
1. Read the description twice; identify the encoding/cipher family from alphabet and
   structure (base64/hex length ratios, repeated-block AES-ECB, RSA parameters).
2. Crypto: compute the obvious quantities (gcd of moduli, small e attack, Wiener,
   factorization for small n) before searching for exotic attacks.
3. QR/files/encodings: reconstruct deterministically (zbarimg, base64 -d chains),
   verify each decode step, never eyeball long transforms.
""",
}


def classify(text: str) -> str:
    """Deterministic keyword classification; returns the dominant category."""
    low = text.lower()
    scores = {cat: sum(1 for kw in kws if kw in low) for cat, kws in _KEYWORDS.items()}
    best = max(scores, key=lambda c: scores[c])
    return best if scores[best] > 0 else "misc"


def select(task: dict) -> tuple[str, str]:
    """Returns (category, strategy text). task is the already-loaded task.json dict."""
    text = " ".join(
        str(x)
        for x in (
            task.get("description") or "",
            *(task.get("targets") or []),
        )
    )
    cat = classify(text)
    return cat, _STRATEGY[cat].strip()


def available() -> tuple[str, ...]:
    return _CATEGORIES
