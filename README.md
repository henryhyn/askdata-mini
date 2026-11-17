# askdata-mini

一个用自然语言查数据的 Agent，用来学习“问数”这类工具型 Agent 怎么设计、怎么评测、怎么一步步改进。它是《Agent 面试：从原理到追问》C2 进阶练习“智能问数 Agent”的配套代码，设计参考了一个已上线的企业取数 Agent，去掉了业务细节，只保留数据字典、检索、查询 IR、Agent 和评测的核心。

每个里程碑对应一个 Git 标签，`git checkout m1` 就能看到当时的代码：

| 标签 | 里程碑 | 你会学到 |
| - | - | - |
| `m1` | 固定流水线基线 | 数据字典、找表、查询 IR 与编译期护栏、按执行结果判分的评测集 |
| `m2` | 工具化 Agent | 把流水线拆成工具，让模型自己决定搜什么、看哪张表、什么时候结束；Trace |
| `m3` | 取值确认 | 过滤前先查真实取值；值集要带上“是否完整”的标记 |
| `m4` | 护栏与多轮 | 敏感字段、行级权限、分区表约束；多轮追问 |

## 快速开始

需要 Python 3.12 及以上和 [uv](https://docs.astral.sh/uv/)，以及任意一个 OpenAI 兼容接口的模型 (豆包、DeepSeek、通义千问等)。

```bash
uv sync
uv run python data/build_shop.py        # 生成演示数据库 data/shop.db (固定随机种子，每次都一样)

export OPENAI_API_KEY=你的火山方舟 Key
# 默认模型是火山方舟上的 doubao-seed-2-1-pro-260915。换别的模型时再设置下面两项：
# export OPENAI_BASE_URL=https://api.deepseek.com/v1
# export ASKDATA_MODEL=deepseek-chat

uv run askdata "上周的 GMV 是多少"
uv run askdata                           # 交互模式
uv run askdata --mode pipeline "..."     # 用 M1 的固定流水线回答
```

```
$ uv run askdata "上周的 GMV 是多少"
GMV
------------
7,386,298.52

查询返回 1 行。

口径 (用到的表：orders)：
SELECT SUM(t."pay_amount") AS "GMV"
FROM "orders" t
WHERE t."order_time" >= '2026-06-22' AND t."order_time" < '2026-06-29'
  AND t."status" IN (2, 3, 4)
LIMIT 200

── result · 模型调用 3 次 · 输入 2936 / 输出 115 token · 6.1 秒
```

演示数据的“今天”固定是 2026-06-30，所以“上周”是 6 月 22 日到 28 日。

## 演示数据

`data/build_shop.py` 生成一个电商业务库：14 张表，3000 个用户，3 万笔订单，2026 年 1–6 月的数据。库里刻意放了问数里常见的难点：

| 难点 | 例子 | 不处理会怎样 |
| - | - | - |
| 编码字段 | `orders.status` 存 1–5，含义只在字典里 | 模型猜“已取消”是 4，查询能跑通，数是错的 |
| 高基数编码字段 | `users.city_id` 有 40 个取值 | 只能按城市名在字典里搜 |
| 只在老数据里出现的取值 | App 6.0 版本只在 1–2 月出现 | 抽样看不到，误以为不存在 |
| 时间字段 | `order_time` 精确到秒 | `<= '2026-06-28'` 会漏掉 28 号一整天 |
| 干扰表 | `orders_archive_2025` 是去年的归档表 | 选错表 |
| 敏感字段 | `users.phone`、`users.real_name` | 泄露个人信息 |
| 数据里的提示注入 | 某条客服工单的内容是一段“系统通知” | 模型被数据里的文字指挥 |

`data/dictionary.json` 是**数据字典**：表和字段的中文描述、编码字段的取值含义、指标口径 (GMV、日活等)、表之间怎么连接。模型看不到数据库，它对数据的全部了解都来自这里。

## 代码结构

```
askdata/
├── catalog.py    数据字典：读 dictionary.json，从数据库推断字段类型
├── retrieval.py  找表：jieba 分词 + SQLite FTS5 全文检索
├── brief.py      把字典写成给模型看的文字 (表级摘要 / 字段说明)
├── ir.py         查询 IR：Pydantic 模型，字段规则写在 Field(description) 里
├── compiler.py   IR → SQL，护栏都在这里
├── executor.py   校验 → 编译 → 只读执行；流水线和 Agent 共用
├── llm.py        OpenAI 兼容接口的薄封装，记录调用次数、token、耗时
├── pipeline.py   M1 固定流水线
├── tools.py      M2 Agent 的工具：参数模型 (描述写在 docstring 和 Field 里)、执行、本轮状态
├── agent.py      M2 工具 Agent：主循环、以执行记录为准的收尾
├── trace.py      Trace：记录每一步，按步骤查看
├── engine.py     按模式创建引擎，命令行和评测共用
└── cli.py        命令行入口
evals/
├── cases.jsonl   评测集：问题 + 标准 SQL，按难点分层
├── compare.py    判分：比较执行结果，不比较 SQL 文本
└── run.py        评测运行器
```

## M1：固定流水线基线

### 流水线

`askdata/pipeline.py`，每一步调用一次模型，控制流写死在代码里：

```
理解问题：要不要查数？检索用什么关键词？      → 闲聊、敏感请求直接回复
检索：关键词 → 候选表 (只有表名和描述)          → 没找到就如实说
选表：从候选表里选出要用的表
生成 IR：给出选中表及其一跳可连接表的字段说明 → 执行；失败带着错误重新生成，最多 2 次
```

### 找表

`askdata/retrieval.py`。表只有十几到几百张时，词法检索就够用，不必上向量库：

- 每张表的表名、描述、字段名、字段描述、枚举含义用 jieba 分词后建 FTS5 全文索引；指标名挂到口径里提到的表上，用户说“GMV”能找到 `orders`。
- 模型给的第一个关键词是必含词，其余是扩展词。全部当必含词，一个词对不上就零召回；全部当扩展词，噪声太多。
- 必含词一个都没命中时退回纯 OR 查询，并告诉调用方“结果是放宽过的”。
- 关键词里的 FTS5 语法字符 (`"`、`*`、括号) 全部去掉：关键词是模型生成的，不能原样拼进查询。

### 查询 IR 和编译期护栏

模型不写 SQL，写一个结构化的查询 IR (`askdata/ir.py`)，由 `askdata/compiler.py` 编译成 SQL。护栏在结构上做，而不是在字符串上做：

| 位置 | 细节 | 为什么 |
| - | - | - |
| `ir.py` | 所有模型 `extra='forbid'` | 默认会静默丢掉拼错的键：把 `where` 写成 `filters`，过滤条件整条消失，查询照样能跑 |
| `ir.py` | 填写规则写在 `Field(description)` 和 docstring 里 | 它们进 JSON Schema，每次都发给模型；`#` 注释模型看不到 |
| `compiler.py` | 时间字段用日期比较时，上界换成“小于下一天” | `order_time <= '2026-06-28'` 只到 28 号 0 点 |
| `compiler.py` | 有聚合时，普通列必须在 `group_by` 里 | SQLite 允许不写，但会随便挑一行的值 |
| `compiler.py` | 只能查候选表 | 防止模型连到没看过字段、或没有权限的表 |
| `compiler.py` | 行数默认 200，最多 1000；最多连 3 张表 | 防止被诱导拉全表、连表失控 |
| `compiler.py` | 错误信息列出真实的列名、可用的别名 | 错误是写给模型看的，要能照着改 |
| `executor.py` | 只读连接、10 秒超时 | IR 本来写不出修改语句，这是第二道保险 |

### 评测

`evals/cases.jsonl` 是 34 道题，按难点分 8 层：单表、枚举、时间、连表、口径、无答案、安全、多轮。每道题写一条标准 SQL，判分时执行它，和 Agent 的结果比较 (`evals/compare.py`)：行数相同，行的顺序不管，标准答案的每一列都要在结果里找到对应的列 (允许多出几列)，数值保留两位小数比较。“无答案”和“安全”层期望 Agent 用文字回复，不取数。

```bash
uv run python -m evals.run --verify              # 先检查标准答案本身：能执行、非空、排名没有并列
uv run python -m evals.run --mode pipeline -n 3  # 每题跑 3 次
```

**M1 基线** (doubao-seed-2-1-pro-260915，每题 3 次，共 102 次运行)：

| 分层 | 单表 | 枚举 | 时间 | 连表 | 口径 | 无答案 | 安全 | 多轮 | 合计 |
| - | - | - | - | - | - | - | - | - | - |
| 通过 | 12/12 | 9/21 | 12/12 | 14/15 | 15/15 | 9/9 | 9/9 | 0/9 | **80/102 (78%)** |

平均每题调用模型 2.8 次，输入 2341 token，耗时 P50 3.9 秒、P95 7.0 秒。

失败集中在三处，后面的里程碑逐个解决：

1. **猜枚举值** (枚举层 12 次失败)：“已取消”猜成 `status = 4`，“小程序”直接写成 `platform = '小程序'`。查询能跑通，数是错的，或者返回 0 行。→ M3
2. **检索漏召回**：“华东区”没找到 `users` 表 (字典里写的是“华东”)，选表环节只看到了日活表，于是回答“没有大区字段”。流水线的重试只会在同一批表上打转，救不回来。→ M2
3. **多轮追问**：流水线没有对话历史，“只看 iOS 的，按周拆开”无从下手。→ M4

**练习**：

1. 把 `ir.py` 里的 `extra='forbid'` 去掉，跑 `tests/test_compiler.py::test_misspelled_key_is_rejected_not_ignored`，看看拼错的键会怎样。
2. 去掉 `compiler.py` 里时间上界的处理，跑“时间”层评测，看哪些题会错、错多少。
3. 在 `cases.jsonl` 里加 5 道你自己的题。先想清楚口径，再写标准 SQL，最后跑 `--verify`。

## M2：工具化 Agent

M1 的弱点写死在控制流里：检索漏掉了正确的表，后面的重试只会在同一批错表上打转。M2 把流水线拆成工具，让模型自己决定搜什么、看哪张表、什么时候结束。

### 工具

`askdata/tools.py`：

| 工具 | 作用 | 设计要点 |
| - | - | - |
| `find_tables(keywords)` | 检索候选表 | 只返回表名、描述、相关度，不带字段，控制 token；必含词没命中时在结果里说明“已放宽” |
| `get_table_columns(tables)` | 看字段，连同一跳可连接的表 | 返回的表进入本轮的**候选集**，`run_query` 只能查候选集里的表 |
| `run_query(ir)` | 执行查询 | 参数就是 M1 的 IR；成功返回 `result_id`、列名、行数和 5 行样例，完整结果不经过模型 |
| `final_answer(kind, summary, result_id)` | 结束本轮 | `result`：交付某次查询的结果；`reply`：文字回复 |

工具的描述写在参数模型的 docstring 和 `Field(description)` 里，由 Pydantic 生成 JSON Schema；`$ref` 展开成内联 schema，不是所有 OpenAI 兼容接口都支持引用。参数解析失败、校验失败、查询报错，都把错误作为工具结果返回给模型，不抛异常。

### 主循环

`askdata/agent.py` 的 `ask()`，大约 50 行：

```
把问题交给模型 (系统提示词 + 工具定义)
循环，最多 16 次请求:
    没有工具调用 → 当作文字回复，结束
    依次执行每个工具调用，结果加入对话
    调用了 final_answer → 结束
收尾：以执行记录为准
```

| 位置 | 细节 | 为什么 |
| - | - | - |
| `agent.py` | `final_answer` 也是一个工具 | 结束是一个带类型的出口：调了就是结束，没调就是还在干活，不用猜模型的一段文字算不算答案 |
| `agent.py` | 每个工具调用都回一条结果，`final_answer` 也回 | 少一条，下一次请求会被接口拒绝 |
| `agent.py` | 系统提示词和工具定义在一次提问内不变 | 构成稳定的前缀，命中模型侧的前缀缓存：Trace 里每次调用的“缓存”token 就是这部分 |
| `llm.py` | `tool_choice` 只用 `auto` | 有的 OpenAI 兼容网关在 `required` 时会返回空的工具调用 |
| `agent.py` | `finalize()` 以执行记录为准 | 见下文“交付错位” |

### Trace

`askdata/trace.py` 订阅 Agent 的事件，每次运行写一个 `traces/<日期>/<时间>-<编号>.jsonl`，记录每次模型调用的 token、耗时，每次工具调用的参数和结果。命令行结束时会打印 Trace 路径，评测结果里每题也带着 Trace 路径。

```
$ uv run python -m askdata.trace traces/2026-11-17/113523-050.jsonl
   0.0s  ▶ 各仓库现在一共有多少件库存？
   3.8s    模型调用：输入 2797 (缓存 1848) / 输出 48 token，3.82 秒
   3.8s  [1] find_tables({"keywords": ["库存", "仓库", "inventory", "stock", "warehouse"]})
         ⎿ {"tables": [{"name": "inventory", "description": "当前库存快照：每个仓库每个商品的库存件数", ...}]}
   7.1s  [2] get_table_columns({"tables": ["inventory"]})
  11.1s  [3] run_query({"ir": {"table": "inventory", "alias": "i", "joins": [{"table": "warehouses", ...
         ⎿ {"ok": true, "result_id": 1, "columns": ["仓库名称", "仓库ID", "库存件数"], "row_count": 5, ...}
  14.7s  [4] run_query({"ir": {"table": "inventory", "select": [{"agg": "sum", "column": "stock", "as": "总库存件数"}]}})
         ⎿ {"ok": true, "result_id": 2, "columns": ["总库存件数"], "row_count": 1, "sample_rows": [[152154]], ...}
  18.1s  [5] final_answer({"kind": "result", "summary": "...共 5 个仓库，总计 152,154 件。", "result_id": 2})
```

这条 Trace 是评测里的一次“失败”：模型先按仓库分组，又查了一次总数，最后交付了总数。读 Trace 才发现问题出在题目上：“各仓库一共有多少件”本身有歧义。题目改成了“每个仓库现在分别有多少件库存”。**评测失败不一定是 Agent 的错，先读 Trace，再下结论。**

### 交付错位：一次评测发现的设计问题

M2 第一版的收尾规则是“交付最后一次成功查询的结果”。评测跑下来只有 70%，比流水线还低。读 Trace 发现两种行为：

- 模型把“上半年每个月的订单数”拆成 6 次查询，每月查一次，在 summary 里写出了 6 个正确的数，但交付的是最后一次 (6 月) 的结果；
- 模型先跑一次探索性查询 (按状态分组看分布)，猜出了答案，没有再跑一次直接回答问题的查询。

在系统提示词里要求“最后再执行一次回答问题的查询”，改善有限。最后的改法是**让交付变成显式的**：每次成功的 `run_query` 返回一个 `result_id`，`final_answer(kind="result")` 必须指明交付哪一次，代码校验这个 id 确实是本轮成功执行过的查询。同时在 `run_query` 的描述里写上“按月、按类别的统计用分组，不要拆成多次查询”。改完后从 70% 回到 81%。

### 和 M1 对比

同一个评测集，每题 3 次：

| | M1 流水线 | M2 Agent |
| - | - | - |
| 通过 | 80/102 (78%) | 84/102 (82%) |
| 枚举层 | 9/21 | 14/21 |
| 选表正确率 | 82% | 86% |
| 平均模型调用 | 2.8 次 | 4.4 次 |
| 平均输入 token | 2341 | 15067 |
| 耗时 P50 / P95 | 3.9 / 7.0 秒 | 8.4 / 19.8 秒 |

- **赢在哪**：检索漏召回时 Agent 能换词再搜 (“华东区”这题流水线 3 次全错，Agent 3 次全对)；不确定编码时会先查一下取值分布再写过滤条件。
- **输在哪**：调用次数和延迟都翻倍，输入 token 涨了 6 倍 (每次请求都要重发工具定义和全部历史，前缀缓存能省下大部分费用)；同一个问题每次走的路可能不一样。
- **还没解决的**：枚举层仍有 7 次失败。模型没有渠道和退款原因的编码表，只能从数据里推断，“好友邀请”被推断成了 `channel = 4`。→ M3

**练习**：

1. 把 `finalize()` 改回“交付最后一次成功的查询”，跑 `--layer 时间`，在 Trace 里找到拆成多次查询的那一次。
2. 去掉 `find_tables` 结果里的“已放宽”说明，看模型会不会在不相关的表上硬凑。
3. 把 `SAMPLE_ROWS` 改成 50，比较输入 token 和通过率。
