/** 面板数据层（对应 panel.py）：load_tasks 分段耗时累加 / 基线预测 / 选题过滤 / UTC 解析。 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  fmtDelta,
  fmtDur,
  globMatch,
  loadTasks,
  platStats,
  predictVsBaseline,
  taskFilterOf,
  utcEpoch,
} from "../src/panel/data.js";

let dir = "";
beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "panel_data_"));
});
afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

function writeJsonl(file: string, rows: unknown[]): void {
  fs.writeFileSync(file, `${rows.map((r) => JSON.stringify(r)).join("\n")}\n`);
}

describe("load_tasks（分段解题多段 close 耗时累加）", () => {
  it("start/close 配对、duration 累加、paused 也算段", () => {
    writeJsonl(path.join(dir, "tasks.jsonl"), [
      {
        phase: "task_start",
        code: "a-01",
        started_at: "2026-10-14 08:00:00",
        target: "10.0.0.1",
        attempt: 1,
      },
      {
        phase: "task_close",
        code: "a-01",
        closed_at: "2026-10-14 08:10:00",
        duration_sec: 600,
        outcome: "paused",
        flags: "0/2",
      },
      {
        phase: "task_start",
        code: "a-01",
        started_at: "2026-10-14 08:20:00",
        target: "10.0.0.2",
        attempt: 2,
        resume: true,
      },
      {
        phase: "task_close",
        code: "a-01",
        closed_at: "2026-10-14 08:30:00",
        duration_sec: 300,
        outcome: "pass",
        flags: "2/2",
      },
    ]);
    const t = loadTasks(dir)["a-01"];
    expect(t.segments).toBe(2);
    expect(t.duration).toBe(900); // 累加而不是覆盖
    expect(t.outcome).toBe("pass");
    expect(t.running).toBe(false);
    expect(t.target).toBe("10.0.0.2");
  });

  it("未 close 的段 running=true", () => {
    writeJsonl(path.join(dir, "tasks.jsonl"), [
      { phase: "task_start", code: "b-01", started_at: "2026-10-14 08:00:00", attempt: 1 },
    ]);
    expect(loadTasks(dir)["b-01"].running).toBe(true);
  });
});

describe("时间与预测", () => {
  it("utc_epoch 按 UTC 解析（容器无时区）", () => {
    const e = utcEpoch("2026-10-14 08:00:00");
    expect(e).not.toBeNull();
    expect(e).toBe(Date.UTC(2026, 9, 14, 8, 0, 0) / 1000);
    expect(utcEpoch("bad")).toBeNull();
    expect(utcEpoch("")).toBeNull();
  });

  it("预测规则：在跑取 max(历史,已耗时)、通关扣减、未开始=历史", () => {
    const now = Date.UTC(2026, 9, 14, 9, 0, 0) / 1000; // 09:00 UTC
    const tasks = {
      "run-1": {
        code: "run-1",
        duration: 600,
        segments: 1,
        running: true,
        started_at: "2026-10-14 08:30:00", // 已跑 1800s，累计 600+1800=2400
      },
      "done-1": { code: "done-1", duration: 100, segments: 1, running: false, outcome: "pass" },
      "new-1": { code: "new-1", duration: 0, segments: 0, running: false },
    };
    const base = { "run-1": 1000, "done-1": 500, "old-1": 300 };
    const p = predictVsBaseline(tasks, base, [], now)!;
    expect(p).not.toBeNull();
    const byCode = Object.fromEntries(p.items.map((i) => [i.code, i]));
    expect(byCode["run-1"].state).toBe("run");
    expect(byCode["run-1"].actual).toBe(2400);
    expect(byCode["run-1"].pred).toBe(2400); // max(1000, 2400)
    expect(byCode["run-1"].delta).toBe(1400);
    expect(byCode["done-1"].state).toBe("done");
    expect(byCode["done-1"].pred).toBe(100); // 提前完成直接扣
    expect(byCode["done-1"].delta).toBe(-400);
    // 未跑过且不在选题范围的历史码不补进来
    expect(byCode["old-1"]).toBeUndefined();
    expect(p.total_base).toBe(1500);
    expect(p.total_pred).toBe(2500);
  });

  it("显式选题时未开始的题按基线计入（todo）", () => {
    const now = Date.UTC(2026, 9, 14, 9, 0, 0) / 1000;
    const p = predictVsBaseline({}, { "x-01": 400 }, ["x-*"], now)!;
    expect(p.items[0].state).toBe("todo");
    expect(p.items[0].pred).toBe(400);
  });
});

describe("选题过滤与统计", () => {
  it("globMatch 与 agent 同规则", () => {
    expect(globMatch("c-0*", "c-03")).toBe(true);
    expect(globMatch("c-0*", "c-33")).toBe(false);
    expect(globMatch("c-0?", "c-03")).toBe(true);
    expect(globMatch("c-0?", "c-033")).toBe(false);
    expect(globMatch("b-01", "d-01")).toBe(false);
  });

  it("platStats 选题模式只统计命中的题", () => {
    const items = [
      {
        unique_code: "a-01",
        is_completed: true,
        correct_flag_count: 2,
        flag_count: 2,
        total_score: 100,
      },
      {
        unique_code: "b-01",
        is_completed: false,
        correct_flag_count: 1,
        flag_count: 3,
        total_score: 200,
      },
    ] as any;
    expect(platStats(items, [])).toEqual({
      total: 2,
      completed: 1,
      flags: 3,
      max_flags: 5,
      score: 100,
    });
    expect(platStats(items, ["a-*"])).toEqual({
      total: 1,
      completed: 1,
      flags: 2,
      max_flags: 2,
      score: 100,
    });
  });

  it("task_filter_of 优先 meta.runs[-1].task_filter", () => {
    expect(taskFilterOf({ runs: [{ task_filter: ["c-02", "b-01"] }] })).toEqual(["c-02", "b-01"]);
    // 空 → 回退环境/.env（与 panel.py env_value 同：向上找 .env，空串算缺省）
    // 宿主 .env 设了 AGENT_TASKS 时这里会拿到它——只断言回退路径不炸、类型正确
    const fb = taskFilterOf({ runs: [{}] });
    expect(Array.isArray(fb)).toBe(true);
    expect(taskFilterOf({})).toEqual(fb);
  });
});

describe("展示格式", () => {
  it("fmtDur / fmtDelta", () => {
    expect(fmtDur(65)).toBe("1:05");
    expect(fmtDur(3725)).toBe("1:02:05");
    expect(fmtDelta(-65)).toBe("-1:05");
    expect(fmtDelta(65)).toBe("+1:05");
  });
});
