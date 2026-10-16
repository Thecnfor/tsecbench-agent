/** 状态机单测：计分余数、hint 扣分、持久化回灌、驱动失败映射、对账接管。 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { FakeDriver } from "../src/fake.js";
import { ArenaError, ArenaState, perFlagScore } from "../src/state.js";
import type { ChallengeSpec } from "../src/types.js";

function spec(overrides: Partial<ChallengeSpec> = {}): ChallengeSpec {
  return {
    dir: "/tmp/arena-spec",
    unique_code: "unit_01",
    description: null,
    difficulty: "easy",
    level: 1,
    total_score: 100,
    hint: null,
    hint_cost_ratio: 0.5,
    flags: ["F1", "F2", "F3"],
    image: "fake-image",
    container_ports: [80],
    flag_inject: "env",
    ready_path: null,
    ...overrides,
  };
}

function packs(...specs: ChallengeSpec[]): Map<string, ChallengeSpec> {
  return new Map(specs.map((s) => [s.unique_code, s]));
}

function testCfg(stateFile: string, extra: Record<string, string> = {}) {
  return loadConfig({ ARENA_STATE_FILE: stateFile, ARENA_PORT: "0", ...extra });
}

describe("计分", () => {
  it("每 flag 均分 + 余数给最后一个（sum === total_score）", () => {
    const s = spec({ total_score: 100, flags: ["a", "b", "c"] });
    expect([0, 1, 2].map((i) => perFlagScore(s, i))).toEqual([33, 33, 34]);
    const s2 = spec({ total_score: 100, flags: ["a", "b"] });
    expect([0, 1].map((i) => perFlagScore(s2, i))).toEqual([50, 50]);
  });

  it("多 flag 逐次提交：awarded 累计、hint 扣分作用于每个 flag", async () => {
    const dir = mkdtempSync(join(tmpdir(), "arena-unit-"));
    try {
      const driver = new FakeDriver({ readyDelayMs: 1 });
      const state = new ArenaState(packs(spec()), driver, testCfg(join(dir, "state.json")));
      await state.start("unit_01");

      expect(state.submit("unit_01", "F2")).toMatchObject({
        correct: true,
        awarded: 33,
        cumulative_score: 33,
        correct_flag_count: 1,
        total_flag_count: 3,
        matched_flag_index: 1,
      });
      // 错 flag：不改变任何进度
      expect(state.submit("unit_01", "nope")).toMatchObject({ correct: false, awarded: 0, correct_flag_count: 1 });

      state.hint("unit_01"); // hint_cost_ratio=0.5 → 33→17、34→17
      expect(state.submit("unit_01", "F1")).toMatchObject({ awarded: 17, cumulative_score: 50 });
      expect(state.submit("unit_01", "F3")).toMatchObject({ awarded: 17, cumulative_score: 67 });
      expect(state.list()[0]).toMatchObject({ correct_flag_count: 3, is_completed: true });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("持久化", () => {
  it("进度与任务时限跨进程保留；duplicate 判定恢复", async () => {
    const dir = mkdtempSync(join(tmpdir(), "arena-persist-"));
    const file = join(dir, "state.json");
    try {
      const cfg1 = testCfg(file, { ARENA_TOTAL_LIMIT_S: "3600" });
      const s1 = new ArenaState(packs(spec()), new FakeDriver({ readyDelayMs: 1 }), cfg1);
      await s1.start("unit_01");
      s1.submit("unit_01", "F1");
      s1.hint("unit_01");
      expect(s1.deadlineAt).not.toBeNull();
      expect((s1.deadlineAt ?? 0) - s1.taskStartedAt).toBe(3_600_000);

      // “重启”：新进程读同一 state.json
      const s2 = new ArenaState(packs(spec()), new FakeDriver({ readyDelayMs: 1 }), cfg1);
      expect(s2.taskStartedAt).toBe(s1.taskStartedAt); // 任务起点不因重启而重置
      expect(s2.deadlineAt).toBe(s1.deadlineAt); // 任务窗口不因重启而续命
      const row = s2.list()[0];
      expect(row).toMatchObject({ correct_flag_count: 1, is_completed: false });
      expect(() => s2.submit("unit_01", "F1")).toThrowError(ArenaError);
      expect(s2.submit("unit_01", "F2")).toMatchObject({ correct: true, cumulative_score: 50 }); // hint 状态也保留（round(33*0.5)=17，33+17）
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("驱动故障映射", () => {
  it("docker run 失败 → 503 resource_unavailable，状态回 stopped", async () => {
    const dir = mkdtempSync(join(tmpdir(), "arena-fail-"));
    try {
      const driver = new FakeDriver();
      driver.failRun = new Error("docker daemon down");
      const state = new ArenaState(packs(spec()), driver, testCfg(join(dir, "state.json")));
      await expect(state.start("unit_01")).rejects.toMatchObject({
        http: 503,
        code: "resource_unavailable",
      });
      expect(state.list()[0].container_status).toBe("stopped");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("对账", () => {
  it("托管容器接管后恢复 available；未知 code 容器被清理", async () => {
    const dir = mkdtempSync(join(tmpdir(), "arena-recon-"));
    try {
      const driver = new FakeDriver({ readyDelayMs: 1 });
      driver.managed.set("unit_01", [9123]);
      driver.managed.set("orphan", [9144]);
      const state = new ArenaState(packs(spec()), driver, testCfg(join(dir, "state.json")));
      await state.reconcile();
      await new Promise((r) => setTimeout(r, 30));
      expect(state.list()[0]).toMatchObject({
        container_status: "available",
        container_addr: ["127.0.0.1:9123"],
      });
      expect(driver.managed.has("orphan")).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
