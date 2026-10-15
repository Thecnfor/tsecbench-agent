/** 工具参数序列化容错（形状取自真实跑分里的损坏样本）——对应 selftest.py 同段断言。 */
import { describe, expect, it } from "vitest";
import { dispatchShell, parseToolArguments, repairToolArgs } from "../src/shell_tool.js";

const Q = '"';
const NL = "\n";
const TAGO = `<parameter name=${Q}`;

describe("工具参数序列化容错", () => {
  it("1) action 混入 XML 参数片段（run + command 在里面），其余参数保留", () => {
    const xmlAction =
      "run" +
      Q +
      ">" +
      NL +
      TAGO +
      "command" +
      Q +
      ">" +
      "cd /work/tasks/x; ffuf -u http://t/FUZZ -w big.txt";
    const [args, fix] = parseToolArguments(JSON.stringify({ action: xmlAction, timeout_sec: 60 }));
    expect(args).toBeTruthy();
    expect(args!.action).toBe("run");
    expect(String(args!.command)).toMatch(/^cd \/work\/tasks\/x; ffuf/);
    expect(args!.timeout_sec).toBe(60);
    expect(fix).toBeTruthy();
  });

  it("2) 同类损坏但动作是 wait（job_id 在片段里）", () => {
    const xmlWait = `wait${Q}>${NL}${TAGO}job_id${Q}>a5daca2b`;
    const [args] = parseToolArguments(JSON.stringify({ action: xmlWait, timeout_sec: 120 }));
    expect(args!.action).toBe("wait");
    expect(args!.job_id).toBe("a5daca2b");
  });

  it("3) action 写成别的工具名，但 command 正常", () => {
    const [args, fix] = parseToolArguments(
      JSON.stringify({ action: "shell", command: "cat /tmp/x" }),
    );
    expect(args!.action).toBe("run");
    expect(args!.command).toBe("cat /tmp/x");
    expect(fix).toBeTruthy();
  });

  it("4) action 整段是命令（没有 command 键），wait_ms 要保留", () => {
    const [args, fix] = parseToolArguments(
      JSON.stringify({ action: "cd /work; nmap -p- 10.0.0.1", wait_ms: 8000 }),
    );
    expect(args!.action).toBe("run");
    expect(args!.command).toBe("cd /work; nmap -p- 10.0.0.1");
    expect(args!.wait_ms).toBe(8000);
    expect(fix).toBeTruthy();
  });

  it("5) JSON 值没加引号（解析直接失败）", () => {
    const bare =
      "{" +
      Q +
      "action" +
      Q +
      ": " +
      Q +
      "wait" +
      Q +
      ", " +
      Q +
      "job_id" +
      Q +
      ": 829f40d3, " +
      Q +
      "timeout_sec" +
      Q +
      ": 90}";
    const [args] = parseToolArguments(bare);
    expect(args!.action).toBe("wait");
    expect(args!.job_id).toBe("829f40d3");
  });

  it("6) 正常参数不应被改动（不产生修复说明）", () => {
    const [args, fix] = parseToolArguments(JSON.stringify({ action: "run", command: "echo ok" }));
    expect(args!.command).toBe("echo ok");
    expect(fix).toBeNull();
  });

  it("7) 无任何可恢复参数 → 原样返回，dispatch 给出可操作的报错", async () => {
    const [args, fix] = parseToolArguments(JSON.stringify({ action: "shell" }));
    expect(args!.action).toBe("shell");
    expect(fix).toBeNull();
    const err = await dispatchShell({ ...args! });
    expect(String(err)).toMatch(/^error: unknown action/);
    expect(String(err)).toContain("action");
  });

  it("repairToolArgs：action 为 null + 有 command → 原样返回（与 Python 行为一致，由 dispatch 报错）", () => {
    // Python: action=None → 本地改成 'run' 后命中 KNOWN_ACTIONS → 返回原 dict（action 仍是 None）
    const [args, fix] = repairToolArgs({ action: null, command: "echo hi" });
    expect(args.action).toBeNull();
    expect(fix).toBeNull();
  });
});
