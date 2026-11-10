-- ============================================================
-- 02_clean_behavior.sql
-- ods → dwd：清洗、规范化、刷单过滤、session 切分
--
-- 上游：ods_userbehavior(user_id, item_id, category_id, behavior_type, timestamp)
-- 下游：dwd_behavior_fact(user_id, item_id, category_id, behavior_type, dt, hour, ts, session_id)
--       再被 03_daily_agg.sql / 04_behavior_path.sql 消费
--
-- 依赖：MySQL 8.0（窗口函数 + CTE）
--
-- 口径要点（写进《指标口径文档》时必须原样复述）：
--   1. 时间戳单位自适应：>= 1e11 视为毫秒，否则视为秒，统一归一为“秒”
--   2. 时区固定 +08:00，避免容器时区不同导致 dt/hour 漂移
--   3. 完全重复的行为记录只保留一条
--   4. 刷单用户-日整日剔除（阈值见文件头变量，可调）
--   5. session 切分：同一用户相邻行为间隔 >= 30 分钟即切新会话；不按零点切分
-- ============================================================

USE ecom_chatbi;

-- 时区固定，保证 dt / hour 可复现
SET SESSION time_zone = '+08:00';

-- ------------------------------------------------------------
-- 可调参数（改这里即可复跑实验，不要把数字散落在 SQL 里）
-- ------------------------------------------------------------
SET @session_gap_sec      := 1800;   -- 30 分钟无行为即切新会话
SET @spam_buy_per_day     := 50;     -- 单日购买行为 >= 50 次，判定为刷单
SET @spam_buy_ratio       := 0.80;   -- 单日 buy/pv 行为比 >= 0.8 且购买 >= 10 次，判定异常
SET @spam_min_buy         := 10;     -- 上面那条规则的最小购买次数门槛

DROP TABLE IF EXISTS dwd_behavior_fact;

CREATE TABLE dwd_behavior_fact (
    user_id       BIGINT      NOT NULL COMMENT '用户ID',
    item_id       BIGINT      NOT NULL COMMENT '商品ID',
    category_id   BIGINT      NOT NULL COMMENT '类目ID',
    behavior_type VARCHAR(8)  NOT NULL COMMENT '规范化行为：pv/cart/fav/buy',
    dt            DATE        NOT NULL COMMENT '行为日期（+08:00）',
    hour          TINYINT     NOT NULL COMMENT '行为小时 0-23',
    ts            BIGINT      NOT NULL COMMENT '行为时间戳（秒，已归一）',
    session_id    BIGINT      NOT NULL COMMENT '会话ID = user_id*1000000 + 会话序号',
    PRIMARY KEY (user_id, ts, item_id, behavior_type)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '行为事实表：清洗后的用户行为明细，session 已切分';

INSERT INTO dwd_behavior_fact
    (user_id, item_id, category_id, behavior_type, dt, hour, ts, session_id)
WITH raw AS (
    -- 基础合法性过滤：ID 必须为正，时间戳落在合理区间（2000-01-01 ~ 2100-01-01）
    SELECT
        user_id,
        item_id,
        category_id,
        TRIM(behavior_type) AS behavior_type_raw,
        timestamp           AS ts_raw
    FROM ods_userbehavior
    WHERE user_id > 0
      AND item_id > 0
      AND category_id > 0
      AND timestamp IS NOT NULL
      -- 同时接受“秒”和“毫秒”两种原始单位，非法时间戳直接丢弃
      AND (
            timestamp BETWEEN 946684800    AND 4102444800       -- 秒（2000 ~ 2100）
         OR timestamp BETWEEN 946684800000 AND 4102444800000    -- 毫秒
      )
),
normalized AS (
    -- ① 时间戳单位归一：>= 1e11 视为毫秒
    -- ② 行为类型规范化：兼容英文、数字、中文三种写法
    SELECT
        user_id,
        item_id,
        category_id,
        CASE LOWER(behavior_type_raw)
            WHEN 'pv'   THEN 'pv'
            WHEN '1'    THEN 'pv'
            WHEN '浏览'  THEN 'pv'
            WHEN 'cart' THEN 'cart'
            WHEN '2'    THEN 'cart'
            WHEN '加购'  THEN 'cart'
            WHEN 'fav'  THEN 'fav'
            WHEN '3'    THEN 'fav'
            WHEN '收藏'  THEN 'fav'
            WHEN 'buy'  THEN 'buy'
            WHEN '4'    THEN 'buy'
            WHEN '购买'  THEN 'buy'
        END AS behavior_type,
        CASE WHEN ts_raw >= 100000000000 THEN FLOOR(ts_raw / 1000) ELSE ts_raw END AS ts
    FROM raw
),
dedup AS (
    -- 完全重复的行为只保留一条（抽样/多次导入可能产生重复）
    SELECT
        user_id, item_id, category_id, behavior_type, ts
    FROM (
        SELECT
            n.*,
            ROW_NUMBER() OVER (
                PARTITION BY user_id, item_id, category_id, behavior_type, ts
                ORDER BY ts
            ) AS rn
        FROM normalized n
        WHERE n.behavior_type IS NOT NULL
          AND n.ts BETWEEN 946684800 AND 4102444800
    ) t
    WHERE rn = 1
),
with_dt AS (
    SELECT
        user_id, item_id, category_id, behavior_type, ts,
        DATE(FROM_UNIXTIME(ts))  AS dt,
        HOUR(FROM_UNIXTIME(ts))  AS hour
    FROM dedup
),
windowed AS (
    -- 只保留完整日窗口内的行为：
    -- 原始数据存在极少量 2017-09 / 2019 / 2022 的杂散时间戳，
    -- 它们不属于本次分析的 9 天窗口，混入会让「全周期」类指标失真
    SELECT w.*
    FROM with_dt w
    WHERE w.dt BETWEEN (SELECT min_dt FROM meta_dataset_range WHERE id = 1)
                   AND (SELECT max_dt FROM meta_dataset_range WHERE id = 1)
),
user_day AS (
    -- 用户-日维度统计，用于刷单判定
    SELECT
        user_id,
        dt,
        SUM(CASE WHEN behavior_type = 'buy'  THEN 1 ELSE 0 END) AS buy_cnt,
        SUM(CASE WHEN behavior_type = 'pv'   THEN 1 ELSE 0 END) AS pv_cnt,
        COUNT(*)                                                AS action_cnt
    FROM windowed
    GROUP BY user_id, dt
),
spam AS (
    -- 两类刷单：绝对量异常；或“几乎全是购买行为”的比例异常
    SELECT user_id, dt
    FROM user_day
    WHERE buy_cnt >= @spam_buy_per_day
       OR (buy_cnt >= @spam_min_buy AND buy_cnt / NULLIF(pv_cnt, 0) >= @spam_buy_ratio)
       OR (pv_cnt = 0 AND buy_cnt >= @spam_min_buy)   -- 零浏览纯购买，基本不可能是真实用户
),
clean AS (
    -- 反连接用 NOT EXISTS：spam 已按 (user_id, dt) 唯一，
    -- 语义与 LEFT JOIN ... IS NULL 完全一致，但避免大表上的嵌套循环
    SELECT w.*
    FROM windowed w
    WHERE NOT EXISTS (
        SELECT 1 FROM spam s
        WHERE s.user_id = w.user_id AND s.dt = w.dt
    )
),
ordered AS (
    -- 同一用户内按时间排序，计算与上一条行为的间隔
    SELECT
        c.*,
        LAG(c.ts) OVER (PARTITION BY c.user_id ORDER BY c.ts, c.item_id, c.behavior_type) AS prev_ts
    FROM clean c
),
flagged AS (
    SELECT
        o.*,
        CASE
            WHEN o.prev_ts IS NULL THEN 1
            WHEN o.ts - o.prev_ts >= @session_gap_sec THEN 1
            ELSE 0
        END AS is_new_session
    FROM ordered o
),
sessioned AS (
    -- 前缀和得到会话序号：同一用户内单调递增，天然稳定
    SELECT
        f.*,
        SUM(f.is_new_session) OVER (
            PARTITION BY f.user_id
            ORDER BY f.ts, f.item_id, f.behavior_type
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS session_seq
    FROM flagged f
)
SELECT
    user_id,
    item_id,
    category_id,
    behavior_type,
    dt,
    hour,
    ts,
    -- 会话ID：user_id * 1e6 + 会话序号，确定性且可跨脚本复现
    -- 9 天数据下单用户会话数远小于 1e6，不会溢出；若数据集扩大需改方案
    CAST(user_id * 1000000 + session_seq AS UNSIGNED) AS session_id
FROM sessioned;

-- ------------------------------------------------------------
-- 二级索引在数据灌完后建：
-- 大表逐行维护 5 个索引会把写入放大数倍，先插后建通常快 3~5 倍
-- ------------------------------------------------------------
ALTER TABLE dwd_behavior_fact
    ADD KEY idx_dt (dt),
    ADD KEY idx_session (session_id),
    ADD KEY idx_user_ts (user_id, ts),
    ADD KEY idx_behavior_dt (behavior_type, dt),
    ADD KEY idx_category_dt (category_id, dt);

ANALYZE TABLE dwd_behavior_fact;

-- ============================================================
-- 自检（跑完手动执行）
-- ============================================================
-- 1) 各行为类型占比，确认规范化后没有 NULL 残留
-- SELECT behavior_type, COUNT(*) AS cnt
-- FROM dwd_behavior_fact GROUP BY behavior_type ORDER BY cnt DESC;
--
-- 2) 清洗前后行数对比（评估刷单过滤强度，建议保留在 3% 以内）
-- SELECT
--   (SELECT COUNT(*) FROM ods_userbehavior) AS ods_rows,
--   (SELECT COUNT(*) FROM dwd_behavior_fact) AS dwd_rows;
--
-- 3) 会话规模分布：平均路径长度决定路径表的可用性
-- SELECT
--   COUNT(*) / COUNT(DISTINCT session_id) AS avg_actions_per_session,
--   COUNT(DISTINCT session_id)            AS sessions
-- FROM dwd_behavior_fact;
--
-- 4) 时间跨度，确认 dt 与抽样范围一致（并把结果写入 meta_dataset_range）
-- SELECT MIN(dt) AS min_dt, MAX(dt) AS max_dt, COUNT(DISTINCT dt) AS days
-- FROM dwd_behavior_fact;
