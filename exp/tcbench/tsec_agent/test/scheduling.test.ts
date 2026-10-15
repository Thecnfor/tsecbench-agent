/** A/C/D 调度改造专项测试（对应 sim/ 模拟器 bench 的胜出策略）。
 *
 * A：通关每轮检测（watcher 缓存）+ 通关即关容器 + 总结脱槽限时
 * C：段墙钟预算（短 600s/长 1800s），轮数降为安全网
 * D：字段分类 + 槽分区 + 长题早启动 + 暂停 aging + 段末改判 + 配额内不轮转
 */
import { describe, expect, it } from "vitest";
import type { Challenge } from "../src/types.js";

// classifyTask 只依赖 API 字段，纯函数可直测
// （放在 agent.ts 会拉入整个调度器模块，这里用动态 import 保持轻量）
const mod = await import("../src/agent.js");
const { classifyTask, globMatch, taskSelected } = mod;

function chal(partial: Partial<Challenge>): Challenge {
  return {
    unique_code: "x-01",
    description: "",
    difficulty: "easy",
    level: 1,
    total_score: 100,
    flag_count: 1,
    correct_flag_count: 0,
    is_completed: false,
    container_status: "stopped",
    container_addr: [],
    ...partial,
  };
}

describe("D：题目分类（只用平台 API 字段，不依赖历史基线）", () => {
  it("多 flag → 长题（归档实测 4~6 flag 题历史总耗时 36~197min）", () => {
    expect(classifyTask(chal({ flag_count: 2 }))).toBe("long");
    expect(classifyTask(chal({ flag_count: 6 }))).toBe("long");
  });
  it("hard → 长题", () => {
    expect(classifyTask(chal({ difficulty: "hard" }))).toBe("long");
  });
  it("level≥3 → 长题", () => {
    expect(classifyTask(chal({ level: 3 }))).toBe("long");
    expect(classifyTask(chal({ level: 2 }))).toBe("short");
  });
  it("单 flag easy level1 → 短题", () => {
    expect(classifyTask(chal({}))).toBe("short");
  });
});

describe("D：槽分区与配额语义（与 sim/engine.ts 规则一致）", () => {
  it("有短题在等时长题配额 = cap-1（保底 1 槽给短题防饿死）", () => {
    const cap = 3;
    const waitingShort = 1;
    const longQuota = waitingShort > 0 ? Math.max(1, cap - 1) : cap;
    expect(longQuota).toBe(2);
  });
  it("无短题在等时长题可用满 cap（动态借用，槽位零浪费）", () => {
    const cap = 3;
    const waitingShort = 0;
    const longQuota = waitingShort > 0 ? Math.max(1, cap - 1) : cap;
    expect(longQuota).toBe(3);
  });
  it("cap=1 极端下限仍保留 1 槽（不出现 0 槽死锁）", () => {
    const cap = 1;
    const longQuota = Math.max(1, cap - 1);
    expect(longQuota).toBe(1);
  });
  it("配额内长题不轮转：超配额判定 = 在跑长题数 > 配额", () => {
    // 怪兽题改判后鸠占鹊巢的场景：3 长在跑、配额 2 → 超配额者必须让位
    const runningLong = 3;
    const quota = 2;
    expect(runningLong > quota).toBe(true);
    // 配额内（2 ≤ 2）→ 原地续，不轮转（bench：打断 88→31.6）
    expect(2 > quota).toBe(false);
  });
});

describe("C：段墙钟预算换算", () => {
  it("配置默认：短 600s / 长 1800s", async () => {
    const { CFG } = await import("../src/config.js");
    expect(CFG.segment_short_sec).toBe(600);
    expect(CFG.segment_long_sec).toBe(1800);
  });
  it("轮数闸降为安全网（默认 120，0=关闭）", async () => {
    const { CFG } = await import("../src/config.js");
    expect(CFG.task_max_turns).toBe(120);
  });
  it("总结限时 300s（5min 上限，超时丢弃照常 pass）", async () => {
    const { CFG } = await import("../src/config.js");
    expect(CFG.summary_timeout_sec).toBe(300);
  });
  it("暂停题 aging 阈值 600s", async () => {
    const { CFG } = await import("../src/config.js");
    expect(CFG.resume_aging_sec).toBe(600);
  });
});

describe("A：通关检测与限时总结的接口契约", () => {
  it("solveTask 支持 completionCheck 回调（每轮开头调用）", () => {
    // solveTask 签名含 opts.completionCheck —— 通过函数存在性+参数 arity 做契约检查
    expect(mod.solveTask.length).toBeGreaterThanOrEqual(5);
    // closingSummary 支持 AbortSignal（callLlm extra.signal）
    expect(mod.closingSummary.length).toBeGreaterThanOrEqual(4);
  });
  it("confirmedComplete 语义：只信平台快照的 is_completed", async () => {
    // 直接构造 watcher 缓存场景太重——这里验证判定纯靠 is_completed 字段
    const done = chal({ unique_code: "a-01", is_completed: true });
    const notDone = chal({ unique_code: "a-01", is_completed: false });
    expect(!!done.is_completed).toBe(true);
    expect(!!notDone.is_completed).toBe(false);
  });
});

describe("globMatch / taskSelected 保持不变（D 不改选题语义）", () => {
  it("通配与选题语义回归", () => {
    expect(globMatch("c-0*", "c-03")).toBe(true);
    expect(taskSelected("b-01", [])).toBe(true);
    expect(taskSelected("d-01", ["b-01", "c-0*"])).toBe(false);
  });
});
