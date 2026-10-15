/** key 候选发现 + 掩码 + 题目选择 —— 对应 selftest.py 同段（离线部分）。 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { selectTasks, taskSelected } from "../src/agent.js";
import { CFG } from "../src/config.js";
import { keyCandidates, keyMask, resetAuthState } from "../src/llm.js";

const saved = new Map<string, string | undefined>();
function setEnv(k: string, v: string | undefined): void {
  if (!saved.has(k)) saved.set(k, process.env[k]);
  if (v === undefined) delete process.env[k];
  else process.env[k] = v;
}

describe("题目选择解析与通配（AGENT_TASKS/--task）", () => {
  it("select_tasks 逗号/空白分隔", () => {
    expect(selectTasks("")).toEqual([]);
    expect(selectTasks("b-01,b-02  c-03")).toEqual(["b-01", "b-02", "c-03"]);
  });
  it("task_selected 空选择 = 全部，通配命中/越界", () => {
    expect(taskSelected("b-01", [])).toBe(true);
    expect(taskSelected("c-03", ["b-01", "c-0*"])).toBe(true);
    expect(taskSelected("e1-05", ["e1-05"])).toBe(true);
    expect(taskSelected("d-01", ["b-01", "c-0*"])).toBe(false);
  });
});

describe("key 候选清单（匿名优先/掩码/exclude）", () => {
  const oldKey = CFG.llm_api_key;
  const oldMode = CFG.mode;
  beforeEach(() => {
    saved.clear();
    resetAuthState();
  });
  afterEach(() => {
    for (const [k, v] of saved) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    CFG.llm_api_key = oldKey;
    CFG.mode = oldMode;
  });

  it("匿名在前、真实 key 在后、当前 key 兜底；标签只带尾4位掩码", () => {
    CFG.mode = "hosted";
    CFG.llm_api_key = "tsec-bench";
    setEnv("DEEPSEEK_API_KEY", "real-key-1234");

    const cands = keyCandidates();
    expect(cands[0]).toEqual(["anonymous", ""]);
    const labels = cands.map(([l]) => l);
    expect(labels.some((l) => l.includes("DEEPSEEK_API_KEY"))).toBe(true);
    // 当前 key（tsec-bench）应出现在清单里
    expect(cands.map(([, k]) => k)).toContain("tsec-bench");
    // 标签不泄露完整 key
    expect(labels.every((l) => !l.includes("real-key-1234") && !l.includes("tsec-bench"))).toBe(
      true,
    );
  });

  it("独一无二的当前 key 以 current-config 兜底在最后", () => {
    CFG.mode = "hosted";
    setEnv("DEEPSEEK_API_KEY", "real-key-1234");
    CFG.llm_api_key = "unique-current-key-xyz";
    const c3 = keyCandidates();
    expect(c3[c3.length - 1]).toEqual(["current-config(****-xyz)", "unique-current-key-xyz"]);
  });

  it("exclude：401 热切换时跳过刚被拒的 key", () => {
    CFG.llm_api_key = "tsec-bench";
    const c2 = keyCandidates(new Set(["tsec-bench"]));
    expect(c2.every(([, k]) => k !== "tsec-bench")).toBe(true);
  });

  it("key 掩码只显示尾4位", () => {
    expect(keyMask("real-key-1234")).toBe("****1234");
    expect(keyMask("")).toBe("(none)");
  });
});
