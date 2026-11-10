# 电商 ChatBI · 自然语言问数

把「取数」变成一句话：**自然语言 → SQL → 结果集 → 图表/归因**。
面向电商用户行为数据，包含数仓分层、语义层、NL2SQL 校验沙箱与 200 条评测集。

> 差异化不在于"能生成 SQL"，而在于三件事有据可查：
> ① 语义层（指标口径可配置）② 执行准确率（结果集比对，不是 SQL 字符串匹配）③ 行为路径表（加购未购/深度浏览流失）。

---

## 一分钟跑起来

```bash
cd deploy && cp .env.example .env && docker compose up -d mysql redis   # ① 起库（本机 3306 被占用时改 MYSQL_PORT=3307）
cd ../backend && python -m pip install -r requirements.txt              # ② 装依赖
cd warehouse && mysql -h 127.0.0.1 -P 3306 -u root -p --local-infile=1 < run_all.sql  # ③ 建数仓
```

也可以整栈容器化（含 API，本机 8000 被占用时改 `API_PORT`）：

```bash
cd deploy
docker compose --profile app up -d --build     # api(:8000) + mysql(:3306) + redis(:6379)
```

起来后：

```bash
cd backend
python -m uvicorn app.main:app --port 8000     # 接口文档 http://127.0.0.1:8000/docs
python -m eval.runner                          # 跑评测，报告写进 eval/reports/
```

---

## 架构

```
提问入口       自然语言问题（"昨天加购但没买的用户有多少"）
   │
服务层         FastAPI：结构化日志（trace_id 贯穿）· SSE 流式 · CORS（鉴权/限流尚未实现）
   │
语义召回       向量 + BM25 + 字面命中三重打分 → 相关表 / 指标口径 / 维度 / 枚举映射 / few-shot
   │
确定性拒答     命中「金额 / 地域 / 品牌 / 渠道 / 双11」直接 REFUSE，不交给模型猜
   │
SQL 生成       Prompt = 时间基准 + 指标口径 + 维度字典 + 枚举映射 + 拒答清单 + 3 条示例
   │
校验沙箱       sqlglot 语法 → 单条 SELECT → 表白名单 → 危险函数 → 自动补 LIMIT
   │
执行层         MySQL 数仓（ods → dwd → dws → path → ads），账号仅 SELECT 权限
   │
自修复         报错回填重试 ≤2 轮，同一错误签名重复即终止
   │
呈现层         图表规则路由（直接产出 ECharts option）+ 归因解读（仅基于结果集，无模型时降级规则版）
             追问改写（「按类目拆一下」「换成成交笔数」→ 补全成可独立执行的完整问题）
```

三道防线，缺一不可：

| 防线 | 位置 | 特点 |
|---|---|---|
| Prompt 约束 | `generator.py` | 可被绕过，仅降低概率 |
| 静态校验 | `validator.py` | 确定性拦 DDL/DML/越库/危险函数 |
| 账号权限 | `chatbi_ro` | 数据库层兜底，无法绕过 |

---

## 目录结构

```
ecom-chatbi/
├── backend/
│   ├── app/
│   │   ├── main.py                  FastAPI 入口：trace 中间件 / lifespan / /health
│   │   ├── core/config.py           配置（全部读环境变量）
│   │   ├── api/v1/
│   │   │   ├── query.py             问数接口（同步 + SSE）
│   │   │   └── meta.py              指标/维度/术语/拒答清单
│   │   ├── semantic/                ★ 语义层
│   │   │   ├── metrics.yaml         19 指标 · 7 术语 · 拒答规则 · 执行护栏
│   │   │   ├── dimensions.yaml      9 维度 · 4 join · 时间解析规则
│   │   │   └── loader.py            加载 + 启动校验 + Prompt 文本块 + 确定性拒答
│   │   └── nl2sql/                  ★ 核心链路
│   │       ├── embeddings.py        哈希向量（默认）/ BGE-M3（可选）
│   │       ├── schema_recall.py     三重打分召回
│   │       ├── fewshot.py + .yaml   动态示例库（12 条）
│   │       ├── generator.py         Prompt 组装 + JSON 解析
│   │       ├── validator.py         sqlglot 校验沙箱
│   │       ├── repair.py            报错回填自修复
│   │       └── pipeline.py          主链路 + MySQLExecutor
│   │   └── analysis/                ★ 结果呈现
│   │       ├── chart_router.py      图表规则路由（9 类图形，输出 ECharts option）
│   │       ├── insight.py           归因解读（LLM + 规则降级）
│   │       └── followup.py          追问改写（追加维度 / 换指标）
│   ├── warehouse/                   ★ 数仓（按执行顺序编号）
│   │   ├── ods/01_import_userbehavior.sql
│   │   ├── dwd/02_clean_behavior.sql
│   │   ├── dws/03_daily_agg.sql
│   │   ├── path/04_behavior_path.sql      行为路径表
│   │   ├── ads/05_funnel_rfm_retention.sql
│   │   └── run_all.sql              一键重建 + 一致性校验
│   ├── eval/                        ★ 评测（与业务同级，核心资产）
│   │   ├── dataset/seed.jsonl       200 条（180 SQL + 20 拒答）
│   │   ├── runner.py                执行准确率评测
│   │   ├── metrics.py               结果集比对
│   │   └── reports/                 历次报告，进 git（迭代证据链）
│   └── scripts/download_sample.py   按用户抽样（不切碎会话）
├── deploy/                          docker-compose / .env.example
└── docs/
    ├── METRICS.md                   ★ 指标口径文档
    └── EVAL_REPORT.md               ★ 评测方法与迭代记录（含失败归因）
```

---

## 关键设计

### 1. 语义层是口径唯一来源
指标定义写在 `metrics.yaml`，Prompt、校验、评测全部从它读取，不在代码里复制第二份。
启动时自动校验：指标引用不存在的维度、派生指标引用未定义指标、来源表不在白名单——都会打印告警。

结构参考：
- **SuperSonic**（tencentmusic）：`define_type`（ATOMIC/DERIVED）+ `define_by`（MEASURE/FIELD/METRIC），派生指标只能引用已定义指标
- **SQLBot**（dataease）：术语库带同义词与作用范围，few-shot 按相似度动态注入

### 2. 执行准确率，不是字符串匹配
`eval/metrics.py` 比较**结果集**：忽略列名与行序，保留重复行计数，小数按 4 位容差。
同一条指标可以用 JOIN 写、也可以用子查询写，字符串匹配会把正确的判错。

### 3. 行为路径表 `path_user_session`
把 session 内的「浏览 → 加购 → 收藏 → 购买」序列物化成表，支撑宽表给不出的分析：

| 派生列 | 用途 |
|---|---|
| `is_cart_before_buy` | 先加购后成交（严格串行口径） |
| `is_buy_before_cart` | 买后加购（加购不是转化前置动作） |
| `is_browse_heavy` | 浏览 ≥5 个商品且未加购未成交（深度浏览流失） |

排序用原始 `ts` 而非 `dt + hour`：同一小时内多条行为的顺序必须稳定，否则 `cart_first_seq < buy_first_seq` 不成立。

### 4. 拒答是确定性判断
`loader.refusal_reason()` 命中「金额 / 地域 / 品牌 / 渠道 / 具体大促日」直接返回 REFUSE。
不依赖模型每次听话——否则 `no_answer` 类评测永远拿不到 100%。

---

## 数仓分层

| 层 | 表 | 要点 |
|---|---|---|
| ods | `ods_userbehavior` | 原样落库不设主键，脏数据不静默丢弃；`meta_dataset_range` 登记真实时间范围 |
| dwd | `dwd_behavior_fact` | 时间戳单位归一、行为类型规范化、去重、刷单过滤（3 条规则）、30 分钟切 session |
| dws | `dws_user_daily` / `dws_category_daily` / `dim_date` / `dim_category` | 用户粒度与类目粒度分表，避免"按类目汇总后 UV 翻倍" |
| path | `path_user_session` | session × 类目粒度，四个顺序列 + 三个派生标记 |
| ads | `ads_funnel_daily` / `ads_rfm_segments` / `ads_retention_matrix` / `dim_user` | 漏斗双口径（宽口径 vs 路径严格口径）、RFM 分层、留存矩阵 |

`run_all.sql` 末尾跑 4 条一致性校验，期望全部为 0：

| 校验 | 含义 |
|---|---|
| C1 | dws 与 dwd 的 PV 总额一致 |
| C2 | RFM 人数 = `is_buyer=1` 用户数 |
| C3 | 留存分母 = 当日活跃人数 |
| C4 | 成交但无浏览的会话占比（异常数据信号） |

---

## 数据说明（重要）

**当前仓库内的 `data/ods/userbehavior_sample.csv` 是真实天池 UserBehavior 的按用户抽样**：
2,537,528 行 / 25,000 用户 / 6,838 类目 / 740,535 商品 / 9 个完整日（2017-11-25 ~ 2017-12-03）。

真实数据全量：天池 [UserBehavior](https://tianchi.aliyun.com/dataset/dataDetail?dataId=649)（1 亿行 / 98.8 万用户，需登录下载）。

```bash
python backend/scripts/download_sample.py \
  --input data/raw/UserBehavior.csv.zip \
  --target-rows 2500000 --out data/ods/userbehavior_sample.csv   # 按用户抽样
docker cp backend/warehouse chatbi-mysql:/tmp/warehouse
docker exec -w /tmp/warehouse chatbi-mysql sh -c \
  "mysql -uroot -pchatbi_root --local-infile=1 < run_all.sql"      # 覆盖重建
```

抽样必须**按用户**，不能按行随机——按行抽会把会话切碎，路径表、留存、漏斗全部失真。

两个真实数据才暴露的口径问题（合成数据无法验证）：

- **uv 与 pv_users 不等**：约 1.8% 的成交没有浏览行为，所以"转化率/加购率"的分母
  必须是访问人数 `uv`，不是浏览人数 `pv_users`；类目粒度用 `dws_category_daily.uv_users`。
- **存在杂散时间戳**：抽样用户带有 2017-09 / 2019 / 2022 的残留记录，
  02 清洗按 `[min_dt, max_dt]` 完整日窗口过滤，否则「全周期」指标被污染。

> 注意：该数据集**不含双11**。因此大促日定义为数据集最后一天（`max_dt`），
> 用户明确问"双11/双12/618"时走拒答路径，不拿最后一天数据冒充。

---

## 已知限制

- 默认哈希向量**不具备真正的语义泛化能力**，召回靠字面重叠兜底。设 `EMBEDDER=bge` 并安装 `sentence-transformers` 可显著提升（首次会下载模型，离线环境勿开）。
- 本地没有 API key 时可用 Ollama：设 `LLM_PROVIDER=ollama`、`LLM_BASE_URL=http://127.0.0.1:11434`、`LLM_MODEL=qwen3.5:4b`。注意**必须 `LLM_THINK=0`**——思考型模型在 Ollama 的 `/v1` 兼容层会把 token 全耗在 reasoning 上，`content` 为空。
- 图表推荐是**纯规则**实现（不调模型、可单测），归因解读依赖模型；缺 `LLM_API_KEY` 时自动降级为规则版解读（只陈述可验证的对比与占比，更保守）。
- 追问改写只覆盖「追加维度 / 换指标」两类，其余追问按新问题处理。
- 鉴权与限流**尚未实现**（`core/config.py` 只留了配置位），生产部署前必须补齐。
- 评测集 200 条（流量 40 / 时间对比 40 / 分群 30 / 路径 30 / 排名 40 / 拒答 20），覆盖主干场景。
- 数据按用户抽样 2.5 万（原 98.8 万用户），覆盖约 2.5%；9 天窗口使 D7 只有 2 天可算，同比/月度类问题未覆盖。
- 延迟为单机 Docker 串行调用（平均 1.6s / P95 2.9s），未做并发压测。
- 评测的 `fewshot.yaml` 曾针对失败模式修订过两轮，**存在过拟合风险**：扩集到 200 条后准确率从 100% 回落到 96.1%，说明 60 条上的满分不能当泛化能力。
- 语义层消融在 200 条上改用本地 `gemma4:12b`（DeepSeek 余额用尽）：3-shot -1.1pp、0-shot -5.0pp。
  该负值反映小模型无法利用长口径 Prompt，**不能与 DeepSeek 的 96.1% 混用**，也不能据此否定语义层
  （60 条 DeepSeek 0-shot 为 +4.0pp）。
