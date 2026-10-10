// recorder —— 每题一个的流量录制 / 重放 sidecar（architecture.md §1.2）。
//
// 定位：不是工具，是「客观性层」——
//   1. prior.assets 的客观来源（录到的请求/响应，不依赖 LLM 自述）；
//   2. 探测去重的物理基础（(method, url, body-hash) 索引）；
//   3. Flag Harvester 的第二条收割通道（flag 常在响应体里）。
//
// 范围纪律（§1.2，防止长成第二个项目）：
//   只做 HTTP/HTTPS 录制 + 按 key 重放 + 落盘 JSONL。
//   明确不做：websocket、脚本化改写、证书固定绕过、任何 UI、任何查询语言。
//
// 运行形态：solver 经 HTTP_PROXY/HTTPS_PROXY 指向本进程（architecture.md §2.1），
// 只监听 127.0.0.1；CA 证书路径经环境变量注入。
package main

import (
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"
)

var (
	addr     = flag.String("addr", "127.0.0.1:0", "listen address (loopback only)")
	outDir   = flag.String("out", "out/traffic", "JSONL output directory (per-workspace)")
	stateDir = flag.String("state", "out/traffic/.state", "dedup index directory")
	maxMB    = flag.Int("max-mb", 0, "traffic cap in MB (TRAFFIC_MAX_MB); 0 = unlimited")
)

func main() {
	flag.Parse()
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

	// 硬约束：绝不允许 0.0.0.0（托管全程流量审计，runbook.md §4.2）
	host, _, err := net.SplitHostPort(*addr)
	if err == nil && host != "127.0.0.1" && host != "localhost" && host != "::1" {
		log.Fatalf("recorder must bind loopback, got %q", host)
	}
	for _, dir := range []*string{outDir, stateDir} {
		if err := os.MkdirAll(*dir, 0o755); err != nil {
			log.Fatalf("mkdir %s: %v", *dir, err)
		}
	}

	// TODO(10/11-12 里程碑):
	//   1. 反代 + TLS 动态签发（crypto/tls + net/http/httputil）
	//   2. JSONL 落盘：时间戳、目标、状态码、body 摘要与全文引用（§2.2）
	//   3. (method, url, body-hash) 去重索引与按 key 重放
	//   4. 磁盘上限：打到 maxMB 后降级为 header+摘要，不 OOM
	//   5. 绕过代理的裸 socket 连接：录不到，但必须在日志显式告警
	ln, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	fmt.Printf("recorder: listening on %s out=%s state=%s (skeleton)\n", ln.Addr(), *outDir, *stateDir)

	// sidecar 生命周期：随 solver 进程组启停；收到 SIGINT/SIGTERM 再退出
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Print("recorder: shutting down")
}
