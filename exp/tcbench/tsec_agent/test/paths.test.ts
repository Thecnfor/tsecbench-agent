/** 路径解析回归测试：记录根目录选择 + 面板目录发现。
 *
 * 背景 bug（用户报告"跑起任务却说没任务没有监控"）：
 * 1. 重写时 defaultRecordsDir 兜底改成了 cwd——容器里 cwd=/agent（源码挂载点），
 *    记录全写进源码目录，挂载的 runs/<TOKEN> 是空的 → 面板无数据；
 * 2. 面板 findLatestRuns 的模块相对路径只在源码布局正确，dist/ 布局算错；
 *    且按目录 mtime 排序会选中 run.sh 刚建的空目录。
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { isBenchDir, recordsMtime, resolveRunsDir } from "../src/panel/data.js";
import { pickRecordsDir } from "../src/records.js";

let tmp = "";
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "paths_"));
});
afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

function mkBenchDir(
  parent: string,
  name: string,
  opts: { meta?: boolean; tasks?: boolean; recent?: boolean },
): string {
  const d = path.join(parent, name);
  fs.mkdirSync(d, { recursive: true });
  if (opts.meta) fs.writeFileSync(path.join(d, "meta.json"), "{}");
  if (opts.tasks) fs.writeFileSync(path.join(d, "tasks.jsonl"), "");
  if (opts.recent) {
    // 把记录文件 mtime 推到未来，模拟"正在被写"
    const f = path.join(d, "tasks.jsonl");
    const future = new Date(Date.now() + 60_000);
    fs.utimesSync(f, future, future);
  }
  return d;
}

describe("pickRecordsDir：记录根目录优先级（修复 bug 1）", () => {
  it("显式 RECORDS_DIR 且目录存在 → 用它", () => {
    expect(pickRecordsDir(tmp, true)).toBe(tmp);
  });
  it("/work 存在（容器内挂载点）→ 优先于 cwd", () => {
    // workExists=true 模拟容器：即使没设 RECORDS_DIR 也必须落 /work，不能落 cwd
    expect(pickRecordsDir(undefined, true)).toBe("/work");
  });
  it("/work 不存在（宿主机开发）→ 回退 cwd", () => {
    expect(pickRecordsDir(undefined, false)).toBe(process.cwd());
  });
  it("RECORDS_DIR 指向不存在的目录 → 回退 /work（容器）", () => {
    expect(pickRecordsDir("/nonexistent-xyz", true)).toBe("/work");
  });
  it("RECORDS_DIR 存在 → 即使 /work 也在，仍用显式值", () => {
    expect(pickRecordsDir(tmp, true)).toBe(tmp);
  });
});

describe("isBenchDir / recordsMtime：bench 目录识别", () => {
  it("有 meta.json → 是 bench 目录", () => {
    const d = mkBenchDir(tmp, "t1", { meta: true });
    expect(isBenchDir(d)).toBe(true);
  });
  it("有 tasks.jsonl → 是 bench 目录", () => {
    const d = mkBenchDir(tmp, "t2", { tasks: true });
    expect(isBenchDir(d)).toBe(true);
  });
  it("两者都缺（run.sh 刚 mkdir 的空目录）→ 不是 bench 目录", () => {
    const d = mkBenchDir(tmp, "t3", {});
    expect(isBenchDir(d)).toBe(false);
  });
  it("不存在的路径 → 不是 bench 目录", () => {
    expect(isBenchDir(path.join(tmp, "nope"))).toBe(false);
  });
  it("recordsMtime 反映记录文件写入（未来 mtime 最大）", () => {
    const older = mkBenchDir(tmp, "old", { tasks: true });
    const newer = mkBenchDir(tmp, "new", { tasks: true, recent: true });
    expect(recordsMtime(newer)).toBeGreaterThan(recordsMtime(older));
  });
});

describe("resolveRunsDir：--dir 接受 runs/ 或 runs/<TOKEN>", () => {
  it("直接给 <TOKEN> 目录 → 原样返回", () => {
    const d = mkBenchDir(tmp, "abc123", { meta: true, tasks: true });
    expect(resolveRunsDir(d)).toBe(d);
  });
  it("给 runs/ 父目录 → 挑记录最新的 bench（不是目录 mtime 最新的空目录）", () => {
    // 先建一个"真" bench，再建一个更新的空目录（模拟 run.sh mkdir 行为）
    const real = mkBenchDir(tmp, "real-token", { meta: true, tasks: true });
    const empty = mkBenchDir(tmp, "empty-token", {}); // run.sh 刚建、还没写记录
    expect(resolveRunsDir(tmp)).toBe(real);
    expect(resolveRunsDir(tmp)).not.toBe(empty);
  });
  it("多个真 bench → 挑记录文件最新的", () => {
    const older = mkBenchDir(tmp, "older", { meta: true, tasks: true });
    const newer = mkBenchDir(tmp, "newer", { meta: true, tasks: true, recent: true });
    expect(resolveRunsDir(tmp)).toBe(newer);
    expect(resolveRunsDir(tmp)).not.toBe(older);
  });
  it("父目录里没有任何 bench → null", () => {
    mkBenchDir(tmp, "empty", {});
    expect(resolveRunsDir(tmp)).toBeNull();
  });
  it("不存在的目录 → null", () => {
    expect(resolveRunsDir(path.join(tmp, "ghost"))).toBeNull();
  });
});

describe("目录模型文档化（防止再次混淆）", () => {
  it("bench 目录里 tasks/<code>/ 放 conversation 与 shell 工作目录（同居一处）", () => {
    const bench = mkBenchDir(tmp, "tok", { meta: true, tasks: true });
    const taskDir = path.join(bench, "tasks", "a-01");
    fs.mkdirSync(taskDir, { recursive: true });
    fs.writeFileSync(path.join(taskDir, "conversation.jsonl"), "");
    fs.writeFileSync(path.join(taskDir, "nmap.log"), "");
    // 同一目录既是记录也是工作目录——这是设计，不是 bug
    expect(fs.readdirSync(taskDir).sort()).toEqual(["conversation.jsonl", "nmap.log"]);
    expect(isBenchDir(bench)).toBe(true);
    // 子目录本身不是 bench 目录
    expect(isBenchDir(taskDir)).toBe(false);
  });
});
