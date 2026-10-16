/** 题包校验测试：缺省值、fail-fast 规则（坏题包必须在启动时就报出来）。 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadCatalog, loadPack } from "../src/catalog.js";

function packDir(raw: object | null, withDockerfile = false): string {
  const dir = mkdtempSync(join(tmpdir(), "arena-pack-"));
  if (raw !== null) writeFileSync(join(dir, "challenge.json"), JSON.stringify(raw));
  if (withDockerfile) writeFileSync(join(dir, "Dockerfile"), "FROM scratch\n");
  return dir;
}

describe("loadPack", () => {
  it("缺省值填充（code=目录名、difficulty/level/total_score/flag_inject）", () => {
    const dir = packDir(
      { flags: ["F"], container_ports: [80], image: "img" },
      false,
    );
    const spec = loadPack(dir, "my_dir_code");
    expect(spec).toMatchObject({
      unique_code: "my_dir_code",
      difficulty: "easy",
      level: 1,
      total_score: 100,
      hint_cost_ratio: 0.2,
      flag_inject: "env",
      image: "img",
    });
    rmSync(dir, { recursive: true, force: true });
  });

  it("fail-fast：重复 flags / 无 image 无 Dockerfile / 坏 flag_inject / 空 flags", () => {
    const cases: Array<object> = [
      { flags: ["F", "F"], container_ports: [80], image: "img" },
      { flags: ["F"], container_ports: [80] }, // 无 image 且无 Dockerfile
      { flags: ["F"], container_ports: [80], image: "img", flag_inject: "bake" },
      { flags: [], container_ports: [80], image: "img" },
      { flags: ["F"], container_ports: [], image: "img" },
      { flags: ["F"], container_ports: [80], image: "img", hint_cost_ratio: 1.5 },
    ];
    for (const raw of cases) {
      const dir = packDir(raw);
      expect(() => loadPack(dir, "x"), JSON.stringify(raw)).toThrowError(/非法/);
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("无 image 但有 Dockerfile：通过（现场 build）", () => {
    const dir = packDir({ flags: ["F"], container_ports: [80] }, true);
    expect(loadPack(dir, "built_in").image).toBeNull();
    rmSync(dir, { recursive: true, force: true });
  });
});

describe("loadCatalog", () => {
  it("目录扫描 + unique_code 重复检测", () => {
    const root = mkdtempSync(join(tmpdir(), "arena-catalog-"));
    const write = (name: string, code: string) => {
      const dir = join(root, name);
      mkdirSync(dir, { recursive: true });
      writeFileSync(
        join(dir, "challenge.json"),
        JSON.stringify({ unique_code: code, flags: ["F"], container_ports: [80], image: "img" }),
      );
    };
    write("a", "same_code");
    write("b", "same_code");
    expect(() => loadCatalog(root)).toThrowError(/重复/);
    rmSync(root, { recursive: true, force: true });
  });
});
