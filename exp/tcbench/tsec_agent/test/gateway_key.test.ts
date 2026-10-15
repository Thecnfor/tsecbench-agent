/** 网关 key 候选发现 + 401 热切换（离线：stub fetch 模拟假网关）——对应 selftest.py 同段。
 *
 * 假网关规则：匿名和 tsec-bench 被 401，DEEPSEEK_API_KEY（real-key-1234）放行。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CFG } from "../src/config.js";
import {
  clientWithKey,
  discoverGatewayKey,
  LlmHttpError,
  resetAuthState,
  setReauthTried,
  tryReauth,
} from "../src/llm.js";

const saved = new Map<string, string | undefined>();
function setEnv(k: string, v: string | undefined): void {
  if (!saved.has(k)) saved.set(k, process.env[k]);
  if (v === undefined) delete process.env[k];
  else process.env[k] = v;
}

const PROBE_LOG: string[] = [];

/** 假网关：凭 Authorization 头判定（匿名 = 无头或占位），只放行 real-key-1234。 */
function installFakeGateway(acceptKey: string | null): void {
  vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const auth = headers.Authorization ?? "<none>";
    PROBE_LOG.push(auth);
    if (acceptKey === null || auth !== `Bearer ${acceptKey}`) {
      return new Response(
        JSON.stringify({ error: { message: "Authentication Fails, Your api key is invalid" } }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({ choices: [{ message: { role: "assistant", content: "正常" } }] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  });
}

describe("key 候选发现（匿名探测/probe_all/全拒/非401/热切换）", () => {
  const oldKey = CFG.llm_api_key;
  const oldMode = CFG.mode;
  beforeEach(() => {
    saved.clear();
    PROBE_LOG.length = 0;
    resetAuthState();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const [k, v] of saved) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    CFG.llm_api_key = oldKey;
    CFG.mode = oldMode;
    resetAuthState();
  });

  it("probe_all：匿名被拒、真 key 放行 → 按优先级选中真 key", async () => {
    CFG.mode = "hosted";
    CFG.llm_api_key = "tsec-bench";
    setEnv("DEEPSEEK_API_KEY", "real-key-1234");
    installFakeGateway("real-key-1234");

    const nc = await discoverGatewayKey("selftest", undefined, true);
    expect(nc).not.toBeNull();
    expect(CFG.llm_api_key).toBe("real-key-1234");
    // 匿名候选被探测过（无 Authorization 头）
    expect(PROBE_LOG).toContain("<none>");
    // 匿名排第一：探针至少 3 个候选（匿名/token/真实key + current-config）
    expect(PROBE_LOG.length).toBeGreaterThanOrEqual(3);
  });

  it("401 热切换：排除当前 key 后其余全被拒 → null 且不改 key；只热切换一次", async () => {
    CFG.mode = "hosted";
    CFG.llm_api_key = "real-key-1234";
    setEnv("DEEPSEEK_API_KEY", "real-key-1234");
    installFakeGateway("real-key-1234");

    // 非 401 不触发，也不置位 REAUTH_TRIED
    setReauthTried(false);
    expect(await tryReauth(new LlmHttpError("connection reset", null))).toBeNull();
    // 401：排除当前 key 后其余候选（匿名/tsec-bench...）全被拒 → null，key 不变
    const nc2 = await tryReauth(new LlmHttpError("auth", 401));
    expect(nc2).toBeNull();
    expect(CFG.llm_api_key).toBe("real-key-1234");

    // 只热切换一次（防死循环）：置位后直接 null
    setReauthTried(true);
    expect(await tryReauth(new LlmHttpError("auth", 401))).toBeNull();
  });

  it("全候选被拒 → null（不动 CFG.llm_api_key）", async () => {
    CFG.mode = "hosted";
    CFG.llm_api_key = "tsec-bench";
    setEnv("DEEPSEEK_API_KEY", "");
    setEnv("DEEPSEEK_API_KEY_HOSTED", "");
    setEnv("BENCHMARK_TOKEN", "");
    installFakeGateway(null); // 谁都不放行
    const nc = await discoverGatewayKey("selftest-allfail");
    expect(nc).toBeNull();
    expect(CFG.llm_api_key).toBe("tsec-bench");
  });

  it("匿名 client：不带 Authorization 头", async () => {
    installFakeGateway(null);
    await clientWithKey("")
      .create({
        model: "m",
        messages: [{ role: "user", content: "hi" }],
      })
      .catch(() => undefined); // 401 也会走到 fetch，探针头即证据
    expect(PROBE_LOG).toContain("<none>");
  });
});
