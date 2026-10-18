/** shell 工具行为（边沿等待/分页/落盘/上限/省略 action）+ 多模态 view —— 对应 selftest.py 同段。 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { withFixNote } from "../src/agent.js";
import { CFG } from "../src/config.js";
import { rebuildMessages } from "../src/records.js";
import { imageSize, ShellManager, setTaskBranch, setTaskCwd, setTaskShell } from "../src/shell.js";
import { dispatchShell, SHELL } from "../src/shell_tool.js";

function jobIdOf(payload: string): string {
  return payload.split("[job ")[1].split("]")[0];
}

describe("shell：边沿等待 / 分页 / 落盘 / job 上限", () => {
  let w = "";
  beforeEach(() => {
    w = fs.mkdtempSync(path.join(os.tmpdir(), "selftest_shell_"));
  });
  afterEach(() => {
    setTaskCwd("");
    setTaskBranch(0);
    setTaskShell(new ShellManager());
  });

  it("run 带 wait_sec 边沿返回，status 分页带 next_offset，kill 生效", async () => {
    setTaskCwd(w); // ALS：enterWith 必须在测试自身的 async 上下文里生效
    const p = await SHELL.run({
      command: "for i in $(seq 1 200); do printf 'L%03d abcdefghij\\n' $i; sleep 0.05; done",
      wait_ms: 0,
      wait_sec: 4,
    });
    expect(p).toContain("hard limit");
    const jid = jobIdOf(p);
    if (p.includes("running")) {
      // 提前返回也带回了输出
      expect(Number(p.split("out=")[1].split("B")[0])).toBeGreaterThan(0);
    }
    const pg = SHELL.status(jid, undefined, 3);
    expect(pg).toContain("output[tail]:");
    expect(pg).toContain("next_offset=");
    await SHELL.kill(jid);
  }, 60_000);

  it("out_file 直接落盘 workdir", async () => {
    setTaskCwd(w);
    const of = await SHELL.run({
      command: "echo ONDISK; sleep 1",
      out_file: "s.log",
      wait_ms: 0,
      wait_sec: 2,
    });
    expect(fs.existsSync(path.join(w, "s.log"))).toBe(true);
    await SHELL.kill(jobIdOf(of));
  }, 30_000);

  it("省略 action 只给 job_id → status 兜底", async () => {
    expect(String(await dispatchShell({ job_id: "nonexist" }))).toMatch(/^error:/);
  });

  it("job 上限：满了拒绝新 run", async () => {
    setTaskCwd(w);
    const old = CFG.shell_max_jobs;
    CFG.shell_max_jobs = 1;
    try {
      const sl = await SHELL.run({ command: "sleep 10", wait_ms: 0 });
      const second = await SHELL.run({ command: "echo x", wait_ms: 0 });
      expect(String(second)).toContain("后台 job 已满");
      await SHELL.kill(jobIdOf(sl));
    } finally {
      CFG.shell_max_jobs = old;
    }
  }, 30_000);

  it("label 语义名：list/status 显示，wait 可按名指认，重名报错", async () => {
    setTaskCwd(w);
    const p = await SHELL.run({
      command: "echo labeled; sleep 0.3",
      label: "oa-brute",
      wait_ms: 0,
      wait_sec: 3,
    });
    const jid = jobIdOf(p);
    expect(p).toContain("label=oa-brute");
    // wait/status/kill 按 label 寻址（唯一同名）
    const byLabel = String(await dispatchShell({ action: "wait", job_id: "oa-brute" }));
    expect(byLabel).toContain(`[job ${jid}]`);
    expect(byLabel).toContain("labeled");
    expect(String(SHELL.status("oa-brute"))).toContain("label=oa-brute");
    // list 显示 label
    expect(String(await dispatchShell({ action: "list" }))).toContain("label=oa-brute");
    // 重名 label：寻址报错（但 run 本身不拒绝）
    const d1 = jobIdOf(String(await SHELL.run({ command: "sleep 5", label: "dup", wait_ms: 0 })));
    const d2 = jobIdOf(String(await SHELL.run({ command: "sleep 5", label: "dup", wait_ms: 0 })));
    const dup = String(await dispatchShell({ action: "status", job_id: "dup" }));
    expect(dup).toContain("对应多个 job");
    await SHELL.kill(jid);
    await SHELL.kill(d1);
    await SHELL.kill(d2);
  }, 30_000);

  it("每道题独立 ShellManager：job 表/寻址完全隔离", async () => {
    setTaskCwd(w);
    const a = new ShellManager();
    const b = new ShellManager();
    setTaskShell(a);
    const p = await dispatchShell({ action: "run", command: "sleep 5", wait_ms: 0 });
    const jid = jobIdOf(String(p));
    expect(String(await dispatchShell({ action: "list" }))).toContain(jid);
    setTaskShell(b);
    expect(String(await dispatchShell({ action: "list" }))).not.toContain(jid);
    expect(String(await dispatchShell({ action: "status", job_id: jid }))).toContain("unknown");
    setTaskShell(a);
    expect(String(await dispatchShell({ action: "status", job_id: jid }))).toContain(
      `[job ${jid}]`,
    );
    await a.kill(jid);
  }, 30_000);

  it("job 归属 Attempt：list/status/kill 只看本 Attempt", async () => {
    setTaskCwd(w);
    setTaskBranch(1);
    const p = await SHELL.run({ command: "sleep 5", wait_ms: 0 });
    const jid = jobIdOf(p);
    expect(String(await dispatchShell({ action: "list" }))).toContain(jid);
    setTaskBranch(2);
    expect(String(await dispatchShell({ action: "list" }))).not.toContain(jid);
    expect(String(await dispatchShell({ action: "status", job_id: jid }))).toContain("unknown");
    setTaskBranch(1);
    expect(String(await dispatchShell({ action: "status", job_id: jid }))).toContain(
      `[job ${jid}]`,
    );
    await SHELL.kill(jid);
  }, 30_000);
});

describe("多模态 action=view", () => {
  const PNG_B64 =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
  let imgdir = "";
  beforeEach(() => {
    imgdir = fs.mkdtempSync(path.join(os.tmpdir(), "selftest_view_"));
    fs.writeFileSync(path.join(imgdir, "x.png"), Buffer.from(PNG_B64, "base64"));
  });
  afterEach(() => {
    setTaskCwd("");
    setTaskBranch(0);
    setTaskShell(new ShellManager());
  });

  it("返回 [text, image_url] 两段，尺寸 1x1，data URL", async () => {
    setTaskCwd(imgdir);
    const img = await dispatchShell({ action: "view", path: "x.png" });
    expect(Array.isArray(img)).toBe(true);
    const parts = img as Array<{ type: string; text?: string; image_url?: any }>;
    expect(parts.map((p) => p.type)).toEqual(["text", "image_url"]);
    expect(parts[0].text).toContain("1x1");
    expect(parts[1].image_url.url.startsWith("data:image/png;base64,")).toBe(true);
    expect(parts[1].image_url.detail).toBe("low");
    // 尺寸解析独立断言
    const [w, h] = imageSize(fs.readFileSync(path.join(imgdir, "x.png")));
    expect([w, h]).toEqual([1, 1]);
  });

  it("AGENT_VISION=off → 文本模式（只有元数据，不做本地 OCR）", async () => {
    setTaskCwd(imgdir);
    const old = CFG.vision;
    CFG.vision = "off";
    try {
      const txt = await dispatchShell({ action: "view", path: "x.png" });
      expect(typeof txt).toBe("string");
      expect(String(txt)).toContain("文本模式");
      expect(String(txt)).not.toContain("OCR");
    } finally {
      CFG.vision = old;
    }
  });

  it("错误分支：文件不存在 / 格式不支持", async () => {
    expect(String(await dispatchShell({ action: "view", path: "nope.png" }))).toMatch(/^error:/);
    expect(String(await dispatchShell({ action: "view", path: "/etc/hostname" }))).toMatch(
      /^error:/,
    );
  });

  it("修复说明前缀合入图片 parts 的首个 text 段（不破坏结构）", async () => {
    setTaskCwd(imgdir);
    const img = (await dispatchShell({ action: "view", path: "x.png" })) as any[];
    const m = withFixNote(img, "测试修复") as any[];
    expect(m[0].text.startsWith("[参数已自动修复")).toBe(true);
    expect(m[1].type).toBe("image_url");
  });

  it("重建上下文时图片 parts 原样还原（不能被转成 JSON 字符串）", () => {
    const img = [
      { type: "text", text: "header" },
      { type: "image_url", image_url: { url: "data:image/png;base64,xx", detail: "low" } },
    ];
    const jl = path.join(imgdir, "conv.jsonl");
    fs.writeFileSync(
      jl,
      `${[
        JSON.stringify({ kind: "system", content: "S" }),
        JSON.stringify({
          kind: "assistant",
          output: "",
          tool_calls: [{ id: "c1", name: "shell", arguments: "{}" }],
        }),
        JSON.stringify({ kind: "tool", tool_call_id: "c1", result: img }),
      ].join("\n")}\n`,
    );
    const rebuilt = rebuildMessages(jl);
    const last = rebuilt[rebuilt.length - 1];
    expect(Array.isArray(last.content)).toBe(true);
    expect((last.content as any[])[1].type).toBe("image_url");
  });
});

describe("分页读取受 shell_max_output 约束（arena 旁路 bug 回归）", () => {
  it("lines 大窗拉取不绕过截断（曾硬编码 want*160 → 单条 32KB 进上下文）", async () => {
    // 长行大输出（每行 150 字符 × 4000 = 600KB；lines=200 尾读即 ~30KB——
    // 与 arena 实测 32711 字符单条同形态；seq 短行会掩盖窗口 bug）
    const out = await SHELL.run({
      command: `python3 -c "print(('x'*150+'\\n')*4000)"`,
      wait_ms: 8000,
    });
    const jid = jobIdOf(String(out));
    expect(jid).toBeTruthy();
    const page = SHELL.status(jid, undefined, 200); // lines=200：修复前返回 ~32KB
    const text = String(page);
    // header + 截断输出 + tail 的总预算受约束（给 header/cmd/tail 留余量）
    expect(text.length).toBeLessThan(9500);
    expect(text).toContain(`job ${jid}`);
    // offset 分页仍工作（修复未破坏既有语义）
    const p2 = String(SHELL.status(jid, 100, 10));
    expect(p2).toContain("from_offset=100");
    await SHELL.kill(jid);
  });
});
