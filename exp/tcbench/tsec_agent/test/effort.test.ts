/** 思考档位分档（reasoning_effort）：短题 low / 长题默认档，环境变量开关。
 * 覆盖：档位选择（taskClass）、callLlm 透传、400 降级（hosted 网关不认参数时
 * 永久去掉重试，主对话绝不因档位挂掉）、config 默认值。 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { solveTask } from "../src/agent.js";
import { CFG } from "../src/config.js";
import { callLlm, LlmHttpError, resetEffortState } from "../src/llm.js";
import { initRecords, rec, resetRecords, setRecordsDir } from "../src/records.js";
import type { LlmClient } from "../src/types.js";

const SNAP = { ...CFG };

describe("思考档位分档", () => {
  it("config 默认：短题 low、长题空（=不发=模型默认档）", () => {
    expect(CFG.reasoning_effort_short).toBe("low");
    expect(CFG.reasoning_effort_long).toBe("");
  });

  it("solveTask 按 taskClass 选档：短题带 low、长题不带", async () => {
    Object.assign(CFG, SNAP);
    CFG.model = "deepseek-flash";
    CFG.task_max_turns = 1;
    const recdir = fs.mkdtempSync(path.join(os.tmpdir(), "effort_"));
    setRecordsDir(recdir);
    resetRecords();
    initRecords("local", "deepseek-flash");

    const seen: Array<string | undefined> = [];
    const mkClient = (): LlmClient => ({
      async create(opts) {
        seen.push(opts.reasoning_effort);
        return { choices: [{ message: { role: "assistant", content: "完成" } }] };
      },
    });
    const conv1 = rec().conversation("t-e1");
    await solveTask(mkClient(), "t-e1", "卡", 600, conv1, { taskClass: "short" });
    const conv2 = rec().conversation("t-e2");
    await solveTask(mkClient(), "t-e2", "卡", 600, conv2, { taskClass: "long" });
    expect(seen[0]).toBe("low"); // 短题
    expect(seen[1]).toBeUndefined(); // 长题：不发=模型默认档

    // 两档都置空 = 特性关闭（现状行为）
    Object.assign(CFG, SNAP);
    CFG.model = "deepseek-flash";
    CFG.task_max_turns = 1;
    CFG.reasoning_effort_short = "";
    const conv3 = rec().conversation("t-e3");
    await solveTask(mkClient(), "t-e3", "卡", 600, conv3, { taskClass: "short" });
    expect(seen[2]).toBeUndefined();
  });

  it("网关 400 拒绝 reasoning_effort → 永久降级去掉参数重试，主对话不挂", async () => {
    resetEffortState();
    let calls = 0;
    const client: LlmClient = {
      async create(opts) {
        calls++;
        if (calls === 1) {
          expect(opts.reasoning_effort).toBe("low");
          throw new LlmHttpError(
            "HTTP 400: unknown parameter: reasoning_effort",
            400,
            '{"error":{"message":"unknown parameter: reasoning_effort"}}',
          );
        }
        // 第二次：参数已去掉
        expect(opts.reasoning_effort).toBeUndefined();
        return { choices: [{ message: { role: "assistant", content: "ok" } }] };
      },
    };
    const resp = await callLlm(client, [{ role: "user", content: "hi" }], false, { effort: "low" });
    expect(resp.choices[0].message.content).toBe("ok");
    expect(calls).toBe(2);
    resetEffortState(); // 不污染其他用例
  });
});
