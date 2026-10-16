/** 契约测试：起真 HTTP server（端口 0）+ FakeDriver + 真题包，逐条断言官方 §5 语义。 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { loadCatalog } from "../src/catalog.js";
import { arenaRoot, loadConfig, type ArenaConfig } from "../src/config.js";
import { FakeDriver } from "../src/fake.js";
import { buildServer, listen } from "../src/server.js";
import { ArenaState } from "../src/state.js";

const SAMPLE = resolve(arenaRoot(), "test/fixtures/challenges"); // 专用 fixtures：加题不改断言
const FLAG = "FLAG{arena_demo_web_sqli}";
const TOKEN = "test-token";

interface Boot {
  base: string;
  state: ArenaState;
  driver: FakeDriver;
  cfg: ArenaConfig;
  dir: string;
  req: (path: string, method?: string, body?: unknown, token?: string | null) => Promise<Response>;
  stop: () => Promise<void>;
}

async function boot(opts: {
  challengesDir?: string;
  totalLimitS?: number;
  maxActive?: number;
  readyDelayMs?: number;
} = {}): Promise<Boot> {
  const dir = mkdtempSync(join(tmpdir(), "arena-test-"));
  const cfg = loadConfig({
    ARENA_TOKEN: TOKEN,
    ARENA_PORT: "0",
    ARENA_STATE_FILE: join(dir, "state.json"),
    ARENA_CHALLENGES_DIR: opts.challengesDir ?? SAMPLE,
    ARENA_TOTAL_LIMIT_S: opts.totalLimitS === undefined ? "" : String(opts.totalLimitS),
    ARENA_MAX_ACTIVE: opts.maxActive === undefined ? "" : String(opts.maxActive),
  });
  const driver = new FakeDriver({ readyDelayMs: opts.readyDelayMs ?? 30 });
  const state = new ArenaState(loadCatalog(cfg.challengesDir), driver, cfg);
  const server = buildServer(state, cfg);
  const port = await listen(server, cfg);
  const base = `http://127.0.0.1:${port}`;
  const req = async (path: string, method = "GET", body?: unknown, token: string | null = TOKEN) => {
    const headers: Record<string, string> = {};
    if (token !== null) headers.BENCHMARK_TOKEN = token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(base + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  };
  const stop = async () => {
    server.closeAllConnections();
    await new Promise<void>((r) => server.close(() => r()));
    rmSync(dir, { recursive: true, force: true });
  };
  return { base, state, driver, cfg, dir, req, stop };
}

/** 轮询 list 直到该题处于目标状态 */
async function waitStatus(b: Boot, code: string, status: string): Promise<Record<string, any>> {
  const deadline = Date.now() + 3000;
  for (;;) {
    const resp = await b.req("/openapi/v1/challenges");
    const items = (await resp.json()) as Record<string, any>[];
    const row = items.find((c) => c.unique_code === code);
    if (row && row.container_status === status) return row;
    if (Date.now() > deadline) throw new Error(`等待 ${code} → ${status} 超时，当前 ${row?.container_status}`);
    await new Promise((r) => setTimeout(r, 10));
  }
}

describe("API 契约（官方 §5）", () => {
  let b: Boot;
  beforeAll(async () => {
    b = await boot();
  });
  afterAll(async () => {
    await b.stop();
  });

  it("token 缺失/错误 → 404 task_not_found（不是 401）", async () => {
    for (const token of [null, "wrong-token"]) {
      const resp = await b.req("/openapi/v1/challenges", "GET", undefined, token);
      expect(resp.status).toBe(404);
      expect(((await resp.json()) as Record<string, string>).code).toBe("task_not_found");
    }
  });

  it("list：字段齐全、初始 stopped、container_addr 为空", async () => {
    const resp = await b.req("/openapi/v1/challenges");
    expect(resp.status).toBe(200);
    const items = (await resp.json()) as Record<string, any>[];
    expect(items).toHaveLength(1);
    const row = items[0];
    expect(Object.keys(row).sort()).toEqual(
      [
        "container_addr",
        "container_status",
        "correct_flag_count",
        "description",
        "difficulty",
        "flag_count",
        "is_completed",
        "level",
        "total_score",
        "unique_code",
      ].sort(),
    );
    expect(row).toMatchObject({
      unique_code: "web_sqli_01",
      difficulty: "easy",
      level: 1,
      total_score: 100,
      flag_count: 1,
      correct_flag_count: 0,
      is_completed: false,
      container_status: "stopped",
      container_addr: [],
    });
  });

  it("start 未知题 → 404 challenge_not_found", async () => {
    const resp = await b.req("/openapi/v1/challenges/start?unique_code=nope", "POST");
    expect(resp.status).toBe(404);
    expect(((await resp.json()) as Record<string, string>).code).toBe("challenge_not_found");
  });

  it("start：地址即时返回；list 仅 available 时暴露地址；start 幂等", async () => {
    const resp = await b.req("/openapi/v1/challenges/start?unique_code=web_sqli_01", "POST");
    expect(resp.status).toBe(200);
    const started = (await resp.json()) as Record<string, any>;
    expect(started.unique_code).toBe("web_sqli_01");
    expect(started.container_addr).toEqual(["127.0.0.1:9100"]); // pending 也带地址（官方 §5.2）

    // list 在 available 前不暴露地址
    const row = await waitStatus(b, "web_sqli_01", "available");
    expect(row.container_addr).toEqual(["127.0.0.1:9100"]);

    const again = await b.req("/openapi/v1/challenges/start?unique_code=web_sqli_01", "POST");
    expect(again.status).toBe(200);
    expect(((await again.json()) as Record<string, any>).container_addr).toEqual(["127.0.0.1:9100"]);
  });

  it("submit：错 flag → correct:false；对 flag → 全字段；重复 → 409 duplicate", async () => {
    const wrong = await b.req("/openapi/v1/challenges/submit", "POST", {
      unique_code: "web_sqli_01",
      flag: "FLAG{nope}",
    });
    expect(wrong.status).toBe(200);
    expect(await wrong.json()).toEqual({
      correct: false,
      awarded: 0,
      cumulative_score: 0,
      correct_flag_count: 0,
      total_flag_count: 1,
      matched_flag_index: null,
    });

    const ok = await b.req("/openapi/v1/challenges/submit", "POST", {
      unique_code: "web_sqli_01",
      flag: FLAG,
    });
    expect(ok.status).toBe(200);
    expect(await ok.json()).toEqual({
      correct: true,
      awarded: 100,
      cumulative_score: 100,
      correct_flag_count: 1,
      total_flag_count: 1,
      matched_flag_index: 0,
    });

    const dup = await b.req("/openapi/v1/challenges/submit", "POST", {
      unique_code: "web_sqli_01",
      flag: FLAG,
    });
    expect(dup.status).toBe(409);
    expect(((await dup.json()) as Record<string, string>).code).toBe("duplicate");

    const items = (await (await b.req("/openapi/v1/challenges")).json()) as Record<string, any>[];
    expect(items[0].is_completed).toBe(true);
  });

  it("已通关题取 hint → 409 invalid_state", async () => {
    const resp = await b.req("/openapi/v1/challenges/hint?unique_code=web_sqli_01");
    expect(resp.status).toBe(409);
    expect(((await resp.json()) as Record<string, string>).code).toBe("invalid_state");
  });

  it("422（框架形状）：空 flag / 超长 flag / 坏 body / 缺字段", async () => {
    const cases: Array<[unknown, string]> = [
      [{ unique_code: "web_sqli_01", flag: "" }, "string_too_short"],
      [{ unique_code: "web_sqli_01", flag: "x".repeat(4097) }, "string_too_long"],
      [{ unique_code: "web_sqli_01" }, "string_type"],
    ];
    for (const [body, type] of cases) {
      const resp = await b.req("/openapi/v1/challenges/submit", "POST", body);
      expect(resp.status).toBe(422);
      const payload = (await resp.json()) as { detail: Array<{ type: string }> };
      expect(payload.detail[0].type).toBe(type);
    }
    const raw = await fetch(`${b.base}/openapi/v1/challenges/submit`, {
      method: "POST",
      headers: { BENCHMARK_TOKEN: TOKEN, "Content-Type": "application/json" },
      body: "{not json",
    });
    expect(raw.status).toBe(422);
    expect(((await raw.json()) as { detail: Array<{ type: string }> }).detail[0].type).toBe("json_invalid");
  });

  it("close：容器回收、地址清空、进度保留；close 幂等", async () => {
    const resp = await b.req("/openapi/v1/challenges/close?unique_code=web_sqli_01", "POST");
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ unique_code: "web_sqli_01", closed: true });
    const row = (await (await b.req("/openapi/v1/challenges")).json() as Record<string, any>[])[0];
    expect(row.container_status).toBe("stopped");
    expect(row.container_addr).toEqual([]);
    expect(row.is_completed).toBe(true); // 进度不随容器回收而丢

    const again = await b.req("/openapi/v1/challenges/close?unique_code=web_sqli_01", "POST");
    expect(again.status).toBe(200);
    expect((await again.json() as Record<string, boolean>).closed).toBe(true);

    expect(b.driver.managed.size).toBe(0); // FakeDriver 侧确认容器已删
  });
});

describe("hint 扣分", () => {
  it("先取 hint 再提交 → awarded 按 hint_cost_ratio 扣减", async () => {
    const b = await boot();
    try {
      await b.req("/openapi/v1/challenges/start?unique_code=web_sqli_01", "POST");
      await waitStatus(b, "web_sqli_01", "available");
      const hint = await b.req("/openapi/v1/challenges/hint?unique_code=web_sqli_01");
      expect(hint.status).toBe(200);
      expect(((await hint.json()) as Record<string, any>).hint).toContain("单引号");

      const ok = await b.req("/openapi/v1/challenges/submit", "POST", {
        unique_code: "web_sqli_01",
        flag: FLAG,
      });
      const body = (await ok.json()) as Record<string, any>;
      expect(body.correct).toBe(true);
      expect(body.awarded).toBe(80); // round(100 * (1 - 0.2))
      expect(body.cumulative_score).toBe(80);
    } finally {
      await b.stop();
    }
  });
});

describe("并发闸门与异常回收", () => {
  function writePack(dir: string, code: string): void {
    const packDir = join(dir, code);
    mkdirSync(packDir, { recursive: true });
    writeFileSync(
      join(packDir, "challenge.json"),
      JSON.stringify({
        unique_code: code,
        flags: [`FLAG{${code}}`],
        container_ports: [80],
        image: "fake-image", // 给了 image 就不需要 Dockerfile
      }),
    );
  }

  it("active 达上限 → 409 invalid_state；close 后可再 start", async () => {
    const dir = mkdtempSync(join(tmpdir(), "arena-gate-"));
    writePack(dir, "gate_a");
    writePack(dir, "gate_b");
    const b = await boot({ challengesDir: dir, maxActive: 1, readyDelayMs: 200 });
    try {
      expect((await b.req("/openapi/v1/challenges/start?unique_code=gate_a", "POST")).status).toBe(200);
      const second = await b.req("/openapi/v1/challenges/start?unique_code=gate_b", "POST");
      expect(second.status).toBe(409);
      expect(((await second.json()) as Record<string, string>).code).toBe("invalid_state");

      expect((await b.req("/openapi/v1/challenges/close?unique_code=gate_a", "POST")).status).toBe(200);
      expect((await b.req("/openapi/v1/challenges/start?unique_code=gate_b", "POST")).status).toBe(200);
    } finally {
      await b.stop();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("就绪探测失败 → 容器回收回 stopped", async () => {
    const dir = mkdtempSync(join(tmpdir(), "arena-probe-"));
    writePack(dir, "probe_a");
    const b = await boot({ challengesDir: dir, readyDelayMs: 20 });
    try {
      b.driver.failReady = new Error("simulated probe failure");
      expect((await b.req("/openapi/v1/challenges/start?unique_code=probe_a", "POST")).status).toBe(200);
      await waitStatus(b, "probe_a", "stopped");
      expect(b.driver.managed.size).toBe(0);
    } finally {
      await b.stop();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("任务总时限（终态语义）", () => {
  it("到点后全部接口 409 invalid_state，message 含 finished（agent 判终态即停）", async () => {
    const b = await boot({ totalLimitS: 0.05 });
    try {
      expect((await b.req("/openapi/v1/challenges")).status).toBe(200);
      await new Promise((r) => setTimeout(r, 80));
      const list = await b.req("/openapi/v1/challenges");
      expect(list.status).toBe(409);
      const payload = (await list.json()) as Record<string, string>;
      expect(payload.code).toBe("invalid_state");
      expect(payload.message.toLowerCase()).toContain("finished");
      const submit = await b.req("/openapi/v1/challenges/submit", "POST", { unique_code: "x", flag: "y" });
      expect(submit.status).toBe(409);
    } finally {
      await b.stop();
    }
  });
});

describe("重启对账", () => {
  it("接管仍在跑的托管容器；无主容器清掉", async () => {
    const b = await boot({ readyDelayMs: 10 });
    try {
      b.driver.managed.set("web_sqli_01", [9105]); // 模拟上次进程死后残留
      b.driver.managed.set("ghost", [9150]);
      await b.state.reconcile();
      const row = await waitStatus(b, "web_sqli_01", "available");
      expect(row.container_addr).toEqual(["127.0.0.1:9105"]);
      expect(b.driver.managed.has("ghost")).toBe(false);
    } finally {
      await b.stop();
    }
  });
});
