# KB 检索引擎（kb CLI + kb-hunter 子 agent）

目标：让 agent 在离线知识库上**先查再猜**，并且把"词汇不同形"的查询也能救回来。
设计原则：**索引层不做语义**（语义交给模型），**机械匹配给机器**。

## 组件

| 组件 | 位置 | 作用 |
| --- | --- | --- |
| `build_kb_index.py` | `exp/tcbench/scripts/` | 全量构建索引（FTS5 + 中文二元切分 + 常数倒排 + 标题倒排），约 6 秒 |
| `build_cve_data.py` | 同上 | NVD 年度镜像 + CISA KEV → 紧凑 JSONL（`B-漏洞与利用/03-cve-data/`） |
| `kb` | 同上 | 检索 CLI：find / sig / head / cat / tpl / cve / grep / raw |
| `kb-hunter.py` + `kb-hunter.md` | 同上 | 改写检索子 agent（驱动器 + 契约） |
| `kb_eval.py` + `kb_gold.tsv` | 同上 | 回归评测：无偏抽样（标题→期望命中同文件）+ 人工样例哨兵 |
| 索引产物 | `<KB>/.index/` | `kb.sqlite`（正文 `fts` 与 CVE `fts_cve` 分表）、`INDEX.tsv`、`CONST.tsv` |

## 索引要点

- **分词**：FTS5 `unicode61` + 中文二元切分（bigram）。不用 trigram——它要求 token ≥3 字符，
  "逆向/注入/隧道"这类两字词会静默失配。
- **切块**：markdown 按标题链切（heading 父级链保留），YAML 按顶层键切，无扩展名文件（GTFOBins）
  按文本收；路径本身也编入索引（文件名自带语义）。
- **CVE/KEV 独立表**：10 万条 CVE 描述不与正文抢 IDF——混在同一 FTS 表里会拉低全体教程排序
  （实测无偏 hit@5 从 51% 掉到 42%，拆表后完整恢复）。
- **不做同义词规则**：规则挖出的对照对截断/一对多噪声约 30~40%，越修越脏；
  查询方的同义词能力由模型承担（`kb-hunter` 的改写循环）。

## 通道选择

| 线索 | 通道 | 说明 |
| --- | --- | --- |
| CVE 号 / 模板 id | `kb tpl CVE-xxxx-xxxxx` | 文件名+front-matter 精确匹配，无命中退 FTS |
| 魔数 / 操作名 / 术语 | `kb sig 0x9e3779b9` | 常数倒排 + 内置通用词提示 |
| 做法/方法论 | `kb find "JWT 签名绕过 测试步骤"` | BM25；多词默认 OR |
| 产品/版本 → 漏洞 | `kb cve gitlab` | `kind IN (cve,kev)`，KEV 在野利用置顶 |
| 标题定位 | `kb head vm` | `INDEX.tsv` |
| 读命中节 | `kb cat <路径> <行号>` | 只打命中节附近 |
| 自由全文 | `kb grep '<正则>' [--glob *.md]` | 不排序不分层不依赖索引；短词注意 `\b` |

## NOHIT / WEAK 回执（工具替模型记着下一步）

`kb find` 无命中或弱命中时输出诊断回执：`GREPHIT`（grep 预检贴原文现场）、`DIAG`、
`NEXT1-3`（换技术词 / kb grep / 唤起 kb-hunter）、`WEAKCOV`（top1 按标题+开头 400 字
只覆盖部分查询词）。弱命中判据：中文单元 bigram 覆盖率 <0.6 或整体覆盖 <0.5——
不看"长文档深处恰好出现过这个词"。

## kb-hunter（改写检索子 agent）

- 触发：`kb find` 无果/弱命中；意译型查询；词汇不同形；**识别触发**（看到叫不出名字的东西就查，
  不必等卡住）；卡住熔断（连续 8 轮无新证据 / 同脚本重跑 ≥3 次 / 空间 >10⁴）。
- 契约见 `kb-hunter.md`：每轮一个白名单动作（find/sig/head/cat/tpl/grep），≤6 轮，
  收工必须给 top-3 **逐字引用**（path:line + quote，不许改写），找不到就 `top:[]` 并说明试过什么。
- 驱动器 stdlib-only，白名单执行、输出截断 4KB；模型/密钥走 `AGENT_MODEL_LOCAL` / `DEEPSEEK_*`。

## 回归评测（任何引擎改动必跑）

```bash
python3 scripts/build_kb_index.py --kb security-knowledge-base --out /tmp/opencode/kbindex
python3 scripts/kb_eval.py --index /tmp/opencode/kbindex     # 末行 SUMMARY 可 diff
```

基线（2026-10-17）：`auto@1=0.362 auto@5=0.508 gold@1=0.840 gold@5=1.000`。
人工样例（25 条）只是哨兵；泛化以 600 条无偏抽样为准。

## 镜像接线

```dockerfile
COPY scripts/build_kb_index.py scripts/build_cve_data.py scripts/kb \
     scripts/kb-hunter.py scripts/kb-hunter.md /usr/local/bin/
RUN python3 /usr/local/bin/build_kb_index.py --kb /opt/security-knowledge-base
```

KEV/NVD 数据以 JSONL 形式固化在 KB 快照内（`B-漏洞与利用/03-cve-data/`），
镜像内不联网；更新快照后重跑构建即可。

## 已知边界

- 无偏 hit@5 ≈51%：词汇不同形的查询由 kb-hunter 兜底，索引层不追求语义召回。
- KEV 1,748 条（在野利用，优先级最高）；NVD 2023+ high/critical 103,626 条（v3/v4 评分）——
  漏洞条目≠可执行弹药，能直接打的是 KEV + nuclei 模板（3,661 个 high/critical）。
- 未装 nuclei 引擎不影响：模板的 `requests` 段就是 payload 配方，翻译成 curl 即可。
