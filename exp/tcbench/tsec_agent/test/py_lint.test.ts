/** py 脚本自动 lint（[py-lint] 挂 shell 出口）——run2 枚举复盘的行为层防线。
 * 宿主无 ruff → ast 兜底路径天然被测；ruff 解析用纯函数单测。 */
import { describe, expect, it } from "vitest";
import {
  attachPyLint,
  extractHeredocScript,
  lintPyScript,
  parseRuffLines,
} from "../src/py_lint.js";
import { dispatchShell } from "../src/shell_tool.js";

describe("heredoc 提取", () => {
  it("PYEOF/EOF 闭合正常提取；未闭合返回 null；位运算不误提", () => {
    expect(extractHeredocScript("cat > a.py <<'PYEOF'\nprint(1)\nPYEOF\npython3 a.py")).toBe(
      "print(1)",
    );
    expect(extractHeredocScript("python3 - <<'EOF'\nx = 1\nEOF")).toBe("x = 1");
    expect(extractHeredocScript("python3 - <<'EOF'\nprint(1)")).toBeNull(); // 未闭合
    expect(extractHeredocScript('python3 -c "print(1 << 4)"')).toBeNull(); // 位运算无 heredoc
    expect(extractHeredocScript("echo hi")).toBeNull();
  });
});

describe("ast 兜底四规则（宿主无 ruff 时的主路径）", () => {
  it("死代码：if False / 三元死臂 → 恒假警告", () => {
    const note = lintPyScript("x = 0\nif False:\n    x = 1\ny = 1 if False else 2\nprint(x, y)\n");
    expect(note).toContain("[py-lint]");
    expect(note).toContain("恒假");
    expect(note).toContain("L2");
  });

  it("恒空 except:pass → 静默吞错警告", () => {
    const note = lintPyScript(
      "try:\n    risky()\nexcept:\n    pass\nprint('done')\n".replace("risky()", "1/0"),
    );
    expect(note).toContain("恒空 except");
  });

  it("无任何输出语句 → 空跑警告（run2 out=0B 痛点）", () => {
    const note = lintPyScript("a = 1\nb = a + 1\nhits = [i for i in range(10)]\n");
    expect(note).toContain("没有任何输出语句");
    expect(note).toContain("0B 空跑");
  });

  it("语法错误 → 报行号并止于语法（不报后续规则）", () => {
    const note = lintPyScript("def broken(:\n    pass\n");
    expect(note).toContain("语法错误");
    expect(note).toMatch(/L\d/);
    expect(note).not.toContain("恒假");
  });

  it("干净脚本 → 通过（标注降级链）", () => {
    const note = lintPyScript("def f(x):\n    return x + 1\nprint(f(1))\n");
    expect(note).toContain("检查通过");
  });
});

describe("ruff 解析与挂点", () => {
  it("parseRuffLines 解析 concise 行并加注 F821 提示", () => {
    const out = parseRuffLines([
      "/tmp/x.py:12:5: F821 undefined name `k4`",
      "/tmp/x.py:3:1: F401 `os` imported but unused",
      "garbage line",
    ]);
    expect(out.length).toBe(2);
    expect(out[0]).toContain("ruff F821 L12");
    expect(out[0]).toContain("NameError");
    expect(out[1]).toContain("ruff F401 L3");
  });

  it("attachPyLint：python heredoc 才附加，非 python/无 heredoc 不附加", () => {
    const py = attachPyLint("cat > a.py <<'EOF'\nif False:\n    pass\nprint(1)\nEOF", "RESULT");
    expect(String(py)).toContain("RESULT");
    expect(String(py)).toContain("[py-lint]");
    expect(attachPyLint("echo hello", "RESULT")).toBe("RESULT");
    expect(attachPyLint("python3 -c 'print(1)'", "RESULT")).toBe("RESULT"); // 无 heredoc
    // shell 脚本 heredoc 不误判为 python（内容特征：sh 以 #!/bin/sh、echo 开头）
    expect(attachPyLint("cat > a.sh <<'EOF'\n#!/bin/sh\necho hi\nEOF", "RESULT")).toBe("RESULT");
  });

  it("shell run 集成：工具结果尾带 [py-lint]", async () => {
    const out = String(
      await dispatchShell({
        action: "run",
        command:
          "cat > /tmp/pylint_it.py <<'PYEOF'\nif False:\n    x = 1\nprint('ok')\nPYEOF\npython3 /tmp/pylint_it.py",
        wait_ms: 8000,
      }),
    );
    expect(out).toContain("[py-lint]");
    expect(out).toContain("恒假");
  });
});
