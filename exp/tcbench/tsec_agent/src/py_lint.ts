/** 生成的 python 脚本自动 lint（shell run 出口，`[py-lint]` 前缀附在结果尾）。
 *
 * 两级链（run2 枚举复盘驱动：五历史错误里静态可查的全抓 + 空输出痛点）：
 *   ① ruff（镜像内预装，Dockerfile pip 层）：F821 未定义名 / F841 算了没用 /
 *      E722 裸 except / E711,E712 比较陷阱 / B006 可变默认参数 / F401 未用导入
 *   ② stdlib ast 兜底（宿主无 ruff 也能跑，零第三方）：语法/缩进、恒假分支
 *      （if False/if 0/三元死臂——历史错误「死代码」run2 复现 3 处）、
 *      恒空 except（静默吞错=「没命中」其实是「炸了没看见」）、
 *      **无输出语句**（run2 大量 out=0B 空跑——算完没 print，跑900s交白卷）
 *
 * black 是 formatter 不查隐患且会改写模型脚本（看到的≠执行的），明确不用。
 * 触发面：命令含 python3 + heredoc 脚本（模型写脚本的固定形态，arena 实测 168 次）。
 * 安全边界：工具缺失/提取失败/任何异常 → 静默跳过，lint 永不阻断执行。 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const HEREDOC_START = /<<-?\s*['"]?([A-Za-z_]\w*)['"]?\s*(?:;|$|\n)/;

/** 从命令中提取第一个 heredoc 脚本体（需有闭合定界行才返回）。 */
export function extractHeredocScript(cmd: string): string | null {
  const m = HEREDOC_START.exec(cmd);
  if (!m) return null;
  const delim = m[1];
  const bodyStart = cmd.indexOf("\n", m.index);
  if (bodyStart < 0) return null;
  const rest = cmd.slice(bodyStart + 1);
  const close = new RegExp(`\\n[ \\t]*${delim}[ \\t]*(\\n|$)`);
  const c = close.exec(rest);
  if (!c) return null; // 未闭合 → 不猜、不 lint
  return rest.slice(0, c.index);
}

/** ruff 精选规则（bug 级优先，不收 style 噪音）。 */
const RUFF_SELECT = "F821,F841,E722,E711,E712,B006,F401";

/** ruff concise 行 → [py-lint] 文案（纯函数，可离线单测）。 */
export function parseRuffLines(lines: string[]): string[] {
  const out: string[] = [];
  for (const raw of lines) {
    // 形如: /tmp/x.py:12:5: F821 undefined name `k4`
    const m = /^[^:]+:(\d+):\d+:\s+([A-Z]\d+)\s+(.+)$/.exec(raw.trim());
    if (!m) continue;
    const [, ln, code, msg] = m;
    const hint =
      code === "F821"
        ? "——运行时 NameError，脚本会炸"
        : code === "E722"
          ? "——裸 except 静默吞错"
          : "";
    out.push(`[py-lint] ruff ${code} L${ln}: ${msg}${hint}`);
  }
  return out;
}

function runRuff(file: string): string[] | null {
  const r = spawnSync(
    "ruff",
    [
      "check",
      "--no-cache",
      "--no-summary",
      "--select",
      RUFF_SELECT,
      "--output-format",
      "concise",
      file,
    ],
    {
      timeout: 5000,
      encoding: "utf8",
      windowsHide: true,
    },
  );
  if (r.error) return null; // ruff 不在（宿主/未装）→ ast 兜底
  if (r.status !== 0 && r.status !== 1) return null; // 用法错等异常 → 不猜
  return (r.stdout || "").split("\n").filter((l) => l.trim());
}

/** stdlib ast 单次调用：语法 + 恒假 + 恒空 except + 无输出。输出标记行。 */
const PY_AST = `
import ast, sys
src = open(sys.argv[1], encoding="utf8").read()
try:
    tree = ast.parse(src)
except SyntaxError as e:
    print(f"SYNTAX_ERROR L{e.lineno}: {e.msg}")
    sys.exit(0)
class V(ast.NodeVisitor):
    def _false(self, t): return isinstance(t, ast.Constant) and t.value is False
    def visit_If(self, n):
        if self._false(n.test): dead.append(("IF", n.lineno))
        self.generic_visit(n)
    def visit_While(self, n):
        if self._false(n.test): dead.append(("WHILE", n.lineno))
        self.generic_visit(n)
    def visit_IfExp(self, n):
        if self._false(n.test): dead.append(("IFEXP(恒选 else 臂)", n.lineno))
        self.generic_visit(n)
    def visit_ExceptHandler(self, n):
        if len(n.body) == 1 and isinstance(n.body[0], ast.Pass):
            swallowed.append(n.lineno)
        self.generic_visit(n)
dead, swallowed = [], []
V().visit(tree)
for kind, ln in dead: print(f"DEAD {kind} L{ln}")
for ln in swallowed: print(f"SILENT_EXCEPT L{ln}")
has_out = any(
    (isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "print")
    or (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr in ("write", "writelines")
        and isinstance(c.func.value, ast.Attribute) and getattr(c.func.value, "attr", "") in ("stdout", "stderr"))
    or (isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "open"
        and len(c.args) >= 2 and isinstance(c.args[1], ast.Constant)
        and isinstance(c.args[1].value, str) and set(c.args[1].value) & set("wa+"))
    for c in ast.walk(tree)
)
if not has_out: print("NO_OUTPUT")
`;

/** ast 检查 → [py-lint] 文案（ruff 缺席时的兜底；恒假/吞错/无输出 ruff 不全包）。 */
export function astLint(file: string): string[] {
  const r = spawnSync("python3", ["-c", PY_AST, file], {
    timeout: 3000,
    encoding: "utf8",
    windowsHide: true,
  });
  if (r.error || r.status !== 0) return [];
  const notes: string[] = [];
  for (const raw of (r.stdout || "").split("\n")) {
    const l = raw.trim();
    if (l.startsWith("SYNTAX_ERROR")) {
      notes.push(
        `[py-lint] 语法错误 ${l.slice("SYNTAX_ERROR ".length)}——脚本会直接失败，先修缩进/语法`,
      );
    } else if (l.startsWith("DEAD ")) {
      const m = /^DEAD (\S+)\s+L(\d+)/.exec(l);
      if (m)
        notes.push(`[py-lint] ⚠ L${m[2]} 恒假分支（${m[1]}）——不可达=枚举覆盖盲区，删掉或启用它`);
    } else if (l.startsWith("SILENT_EXCEPT")) {
      const m = /L(\d+)/.exec(l);
      if (m)
        notes.push(
          `[py-lint] ⚠ L${m[1]} 恒空 except:pass——异常被静默吞掉，中途失败会"看起来没命中"，打印 e 再继续`,
        );
    } else if (l === "NO_OUTPUT") {
      notes.push(
        "[py-lint] ⚠ 脚本没有任何输出语句——算完结果你也看不到（返回 0B 空跑），记得 print 结果或写文件",
      );
    }
  }
  return notes;
}

/** 对 heredoc 脚本跑两级 lint，返回附加文本（null=不附加）。 */
export function lintPyScript(src: string): string | null {
  if (!src.trim()) return null;
  let tmp = "";
  try {
    tmp = path.join(
      os.tmpdir(),
      `pylint_${process.pid}_${Date.now()}_${Math.floor(Math.random() * 1e6)}.py`,
    );
    fs.writeFileSync(tmp, src);
    const notes: string[] = [];
    const ruff = runRuff(tmp); // 先 ruff（镜像内有）
    notes.push(...astLint(tmp)); // ast 恒查（恒假/吞错/无输出是 ruff 不全包的场景规则）
    if (ruff) notes.push(...parseRuffLines(ruff));
    if (!notes.length) {
      notes.push(
        ruff ? "[py-lint] 检查通过（ruff + ast）" : "[py-lint] 检查通过（ast；ruff 未安装）",
      );
    }
    return `\n${notes.join("\n")}`;
  } catch {
    return null;
  } finally {
    if (tmp) {
      try {
        fs.unlinkSync(tmp);
      } catch {
        /* ignore */
      }
    }
  }
}

/** shell run 出口：命令携带 heredoc 且判定是 python 脚本时附加 lint。
 * python 判定三条件（漏检教训：`cat > x.py <<EOF` 写跑分离时命令不含 python3）：
 * 命令含 python3 / heredoc 目标是 .py 文件 / 脚本体内容像 python（import/def/print 开头）。 */
export function attachPyLint<T>(command: string, out: T): T {
  if (typeof out !== "string") return out;
  const script = extractHeredocScript(command);
  if (script === null) return out;
  const looksPy =
    /python3?\b/.test(command) ||
    />\s*\S+\.py\s*<<-?\s*['"]?\w/.test(command) ||
    /^\s*(import\s|from\s+\w+\s+import|def\s|print\()/m.test(script);
  if (!looksPy) return out;
  const note = lintPyScript(script);
  return note ? (`${out}${note}` as T) : out;
}
