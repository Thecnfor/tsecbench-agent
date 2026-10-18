/** vitest setup：必须在任何 src 模块 import 之前清掉会污染自测的宿主环境变量。
 *
 * 对应 selftest.py 的「.env 泄漏隔离」段：项目根 .env 里的 AGENT_TASKS/
 * AGENT_TASK_FILTER 会把自测的假题 t-01/t-02 过滤掉（本地 .env 一设就红）。
 * setupFiles 先于测试文件模块图执行，所以这里改环境是有效的。 */
// 置空而非 delete：config 的 .env 加载只填「不存在」的 key（`k in process.env`），
// 置空能挡住 .env 泄漏；而 env() 把 "" 视作缺省 → 选题过滤为空 = 全部题目。
process.env.AGENT_TASKS = "";
process.env.AGENT_TASK_FILTER = "";
process.env.AGENT_TASKS_FILE = "";
process.env.AGENT_MODE = "local"; // 离线自测不真实连接
process.env.AGENT_PLATFORM_WATCH = "0"; // 离线：不开平台轮询
process.env.VPN_GUARD = "0"; // 离线：不探测预检地址
process.env.AGENT_FORK = "0"; // 单测默认单线；fork 用例自行打开
process.env.AGENT_BRANCH_CONCURRENCY = "1";
process.env.AGENT_NO_WAITER_GIVEUP_MAX = "3"; // 测试保留有限收束，避免 no-waiter 续段无限空转
// 防 .env 里的转写形态触发 hosted 结构信号（置空 → bench_looks_hosted 取默认 URL）
process.env.BENCHMARK_BASE_URL = "https://tsecbench.zc.tencent.com";
