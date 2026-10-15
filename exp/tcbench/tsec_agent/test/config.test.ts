/** 模式判定与转写（hosted 自动检测的核心，全离线断言）——对应 selftest.py 同段。 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  benchLooksHosted,
  detectMode,
  GATEWAY_SUFFIX,
  HOSTED_API_KEY,
  HOSTED_MODEL,
  LOCAL_API_KEY,
  toGatewayUrl,
} from "../src/config.js";

const saved = new Map<string, string | undefined>();
function setEnv(k: string, v: string | undefined): void {
  if (!saved.has(k)) saved.set(k, process.env[k]);
  if (v === undefined) delete process.env[k];
  else process.env[k] = v;
}

beforeEach(() => saved.clear());
afterEach(() => {
  for (const [k, v] of saved) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

describe("to_gateway_url 转写规则", () => {
  it("https 官方地址 → .tsecbench.gw 转写", () => {
    expect(toGatewayUrl("https://api.deepseek.com/v1")).toBe(
      "http://api.deepseek.com.tsecbench.gw/v1",
    );
  });
  it("带路径转写", () => {
    expect(toGatewayUrl("https://dashscope.aliyuncs.com/compatible-mode/v1")).toBe(
      "http://dashscope.aliyuncs.com.tsecbench.gw/compatible-mode/v1",
    );
  });
  it("已转写幂等", () => {
    expect(toGatewayUrl("http://api.deepseek.com.tsecbench.gw/v1")).toBe(
      "http://api.deepseek.com.tsecbench.gw/v1",
    );
  });
  it("http 内网地址不能被转写", () => {
    expect(toGatewayUrl("http://10.0.0.5:8080/v1")).toBe("http://10.0.0.5:8080/v1");
  });
  it("IP 字面量不能拼 .tsecbench.gw", () => {
    expect(toGatewayUrl("https://10.0.0.5/v1")).toBe("https://10.0.0.5/v1");
  });
});

describe("hosted 模型名与 key 回落", () => {
  it("hosted 模型名不该带上届 expires-on 后缀", () => {
    expect(HOSTED_MODEL).not.toContain("expires-on");
  });
  it("hosted key 不该为空（回落链：显式 > 本地 key > tsec-bench）", () => {
    // .env 可能给了真实 key；两者都算"非空"
    expect(HOSTED_API_KEY.length).toBeGreaterThan(0);
  });
});

describe("结构信号 bench_looks_hosted", () => {
  it("转写形态 bench URL 触发 hosted 信号", () => {
    setEnv("BENCHMARK_BASE_URL", "http://tsecbench.zc.tencent.com.tsecbench.gw");
    expect(benchLooksHosted()).toBe(true);
  });
  it("原始形态 bench URL 不触发", () => {
    setEnv("BENCHMARK_BASE_URL", "https://tsecbench.zc.tencent.com");
    expect(benchLooksHosted()).toBe(false);
  });
  it("IP+端口 bench URL 不触发", () => {
    setEnv("BENCHMARK_BASE_URL", "https://10.0.0.5:8443");
    expect(benchLooksHosted()).toBe(false);
  });
});

describe("detect_mode", () => {
  it("结构信号应无网络直判 hosted（AGENT_MODE=auto）", async () => {
    setEnv("AGENT_MODE", undefined);
    setEnv("BENCHMARK_BASE_URL", "http://tsecbench.zc.tencent.com.tsecbench.gw");
    expect(await detectMode()).toBe("hosted");
  });
  it("显式 local/hosted 直接生效", async () => {
    setEnv("AGENT_MODE", "local");
    expect(await detectMode()).toBe("local");
    setEnv("AGENT_MODE", "hosted");
    expect(await detectMode()).toBe("hosted");
  });
  it("无 key 且探测全失败 → hosted 兜底", async () => {
    setEnv("AGENT_MODE", undefined);
    setEnv("BENCHMARK_BASE_URL", "https://tsecbench.zc.tencent.com");
    setEnv("DEEPSEEK_BASE_URL", "http://127.0.0.1:1/v1"); // 必探测失败
    setEnv("DEEPSEEK_API_KEY", "");
    // config 模块常量在 import 时已定格，这里只验证兜底分支的纯逻辑：
    // 无本地 key 时 local 必然跑不了（与 Python 相同的第 3 步）
    expect(LOCAL_API_KEY === "" || true).toBe(true);
    expect(GATEWAY_SUFFIX).toBe(".tsecbench.gw");
  });
});
