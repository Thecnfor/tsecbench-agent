/** 产物指纹：有界扫描 + 跨 Attempt 去重。 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  collectNewArtifacts,
  looksLikeFileCommand,
  looksLikeSharedWrite,
} from "../src/artifacts.js";
import { type BranchCfg, BranchManager } from "../src/branch.js";
import type { ChatMessage } from "../src/types.js";

const CFG: BranchCfg = {
  enabled: true,
  maxBranches: 3,
  maxForks: 5,
  forkCooldownSec: 180,
  pruneSec: 600,
  plannerNudgeMax: 2,
  plannerNudgeCooldownSec: 400,
};

const trunk: ChatMessage[] = [
  { role: "system", content: "sys" },
  { role: "user", content: "card" },
];

describe("artifacts", () => {
  it("有界扫描新文件并算 sha256，跳过旧文件/记录文件", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "art_"));
    const old = path.join(dir, "old.bin");
    fs.writeFileSync(old, "old");
    const past = (Date.now() - 60_000) / 1000;
    fs.utimesSync(old, past, past);
    fs.writeFileSync(path.join(dir, "a.bin"), "same");
    fs.writeFileSync(path.join(dir, "conversation.jsonl"), "noise");
    const arts = collectNewArtifacts(dir, Date.now() / 1000 - 2);
    expect(arts.some((a) => a.path === "a.bin")).toBe(true);
    expect(arts.some((a) => a.path === "old.bin")).toBe(false);
    expect(arts.some((a) => a.path === "conversation.jsonl")).toBe(false);
    expect(arts[0].hash).toMatch(/^[0-9a-f]{64}$/);
  });

  it("命令分类：文件类命令扫描，纯只读命令不扫描", () => {
    expect(looksLikeFileCommand("curl -o validator http://x/download")).toBe(true);
    expect(looksLikeFileCommand("nmap -sT 10.0.0.1 -oN ports.log")).toBe(true);
    expect(looksLikeFileCommand("echo hi")).toBe(false);
    expect(looksLikeFileCommand("cat /etc/passwd")).toBe(false);
    expect(looksLikeSharedWrite("systemctl restart nginx")).toBe(true);
    expect(looksLikeSharedWrite("redis-cli -h 10.0.0.1 set k v")).toBe(true);
    expect(looksLikeSharedWrite("curl -o validator http://x/download")).toBe(false);
    expect(looksLikeSharedWrite("cat /etc/passwd")).toBe(false);
  });

  it("跨 Attempt 同 sha256 产物返回重复来源", () => {
    const bm = new BranchManager(CFG, trunk, 1000);
    bm.materializeFork(1, 0, "A", 2, 1001, (id) => `/w/${id}`);
    bm.materializeFork(2, 0, "B", 2, 1002, (id) => `/w/${id}`);
    const dup = bm.recordArtifacts(2, [{ path: "branches/2/validator", hash: "abc" }]);
    expect(dup).toEqual([]); // 首个
    const dup2 = bm.recordArtifacts(1, [{ path: "branches/1/validator", hash: "abc" }]);
    expect(dup2).toEqual([{ branch: 2, path: "branches/2/validator", hash: "abc" }]);

    const bm2 = new BranchManager(CFG, trunk, 1000);
    bm2.materializeFork(1, 0, "A", 2, 1001, (id) => `/w/${id}`);
    bm2.restoreArtifacts([{ branch: 7, hash: "oldhash", path: "old/validator" }]);
    expect(bm2.recordArtifacts(1, [{ path: "new/validator", hash: "oldhash" }])).toEqual([
      { branch: 7, path: "old/validator", hash: "oldhash" },
    ]);
  });
});
