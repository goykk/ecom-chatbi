-- ============================================================
-- 03_daily_agg.sql
-- dws 轻度汇总 + 两张静态维度表
--
-- 上游：dwd_behavior_fact（02 产出）、meta_dataset_range（01 产出）
-- 下游：05_funnel_rfm_retention.sql、FineBI 看板、NL2SQL 的常用查询
--
-- 产出：
--   dim_date            日期维度（含周/月/星期，供同比环比与热力图）
--   dim_category        类目维度
--   dws_user_daily      用户-日汇总（UV/PV/各行为次数/会话数）
--   dws_category_daily  类目-日汇总（各项人数与次数）
--
-- 为什么不是一张大宽表：
--   用户粒度与类目粒度的去重口径不同（同一用户在同一天买 3 个类目，
--   用户表记 1 人，类目表记 3 人）。混在一张表里必然出现“汇总后 UV 翻倍”。
-- ============================================================

USE ecom_chatbi;

SET SESSION time_zone = '+08:00';

-- ------------------------------------------------------------
-- 1. dim_date
-- ------------------------------------------------------------
DROP TABLE IF EXISTS dim_date;

CREATE TABLE dim_date (
    dt          DATE     NOT NULL COMMENT '日期',
    year        SMALLINT NOT NULL COMMENT '年',
    month       TINYINT  NOT NULL COMMENT '月',
    day         TINYINT  NOT NULL COMMENT '日',
    week_of_year TINYINT NOT NULL COMMENT '年内第几周',
    weekday     TINYINT  NOT NULL COMMENT '星期：1=周一 … 7=周日',
    is_weekend  TINYINT  NOT NULL COMMENT '是否周末',
    PRIMARY KEY (dt)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '日期维度，范围由 meta_dataset_range 决定';

INSERT INTO dim_date
WITH RECURSIVE dates (dt) AS (
    -- min_dt 为 NULL 说明 ods 没导进来；这里不生成任何行，由 run_all 的前置断言负责报错
    SELECT min_dt FROM meta_dataset_range WHERE id = 1 AND min_dt IS NOT NULL
    UNION ALL
    SELECT dt + INTERVAL 1 DAY
    FROM dates
    WHERE dt < (SELECT max_dt FROM meta_dataset_range WHERE id = 1)
)
SELECT
    dt,
    YEAR(dt),
    MONTH(dt),
    DAY(dt),
    WEEK(dt, 3),                                  -- 模式 3：周一为一周起点，符合国内习惯
    WEEKDAY(dt) + 1,                              -- MySQL 的 WEEKDAY 是 0=周一，这里转成 1-7
    CASE WHEN WEEKDAY(dt) >= 5 THEN 1 ELSE 0 END  -- 周六=5、周日=6
FROM dates;

-- ------------------------------------------------------------
-- 2. dim_category
--    数据集只有 category_id，没有类目名。这里先落占位名，
--    后续拿到真实映射表只需 UPDATE dim_category，不用改任何 SQL。
-- ------------------------------------------------------------
DROP TABLE IF EXISTS dim_category;

CREATE TABLE dim_category (
    category_id   BIGINT       NOT NULL COMMENT '类目ID',
    category_name VARCHAR(64)  NOT NULL COMMENT '类目名称（占位或真实映射）',
    name_source   VARCHAR(16)  NOT NULL COMMENT 'placeholder / manual',
    PRIMARY KEY (category_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '类目维度；回答中必须说明名称来源，避免编造类目名';

INSERT INTO dim_category (category_id, category_name, name_source)
SELECT
    category_id,
    CONCAT('类目', category_id),
    'placeholder'
FROM (
    SELECT DISTINCT category_id FROM dwd_behavior_fact
) t;

-- ------------------------------------------------------------
-- 3. dws_user_daily
--    粒度：user_id + dt。用于 UV、留存、RFM 的活跃判定
-- ------------------------------------------------------------
DROP TABLE IF EXISTS dws_user_daily;

CREATE TABLE dws_user_daily (
    user_id       BIGINT   NOT NULL COMMENT '用户ID',
    dt            DATE     NOT NULL COMMENT '日期',
    pv_cnt        INT      NOT NULL DEFAULT 0 COMMENT '浏览次数',
    cart_cnt      INT      NOT NULL DEFAULT 0 COMMENT '加购次数',
    fav_cnt       INT      NOT NULL DEFAULT 0 COMMENT '收藏次数',
    buy_cnt       INT      NOT NULL DEFAULT 0 COMMENT '成交笔数',
    action_cnt    INT      NOT NULL DEFAULT 0 COMMENT '行为总数',
    pv_item_cnt   INT      NOT NULL DEFAULT 0 COMMENT '浏览的去重商品数',
    session_cnt   INT      NOT NULL DEFAULT 0 COMMENT '会话数',
    first_hour    TINYINT  NULL COMMENT '当日首次行为小时',
    last_hour     TINYINT  NULL COMMENT '当日最后行为小时',
    is_active     TINYINT  NOT NULL DEFAULT 1 COMMENT '当日是否活跃（恒为 1，便于留存 JOIN）',
    PRIMARY KEY (user_id, dt),
    KEY idx_dt (dt)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '用户-日汇总：UV / 留存 / RFM 的输入';

INSERT INTO dws_user_daily
    (user_id, dt, pv_cnt, cart_cnt, fav_cnt, buy_cnt, action_cnt,
     pv_item_cnt, session_cnt, first_hour, last_hour, is_active)
SELECT
    user_id,
    dt,
    SUM(CASE WHEN behavior_type = 'pv'   THEN 1 ELSE 0 END)                     AS pv_cnt,
    SUM(CASE WHEN behavior_type = 'cart' THEN 1 ELSE 0 END)                     AS cart_cnt,
    SUM(CASE WHEN behavior_type = 'fav'  THEN 1 ELSE 0 END)                     AS fav_cnt,
    SUM(CASE WHEN behavior_type = 'buy'  THEN 1 ELSE 0 END)                     AS buy_cnt,
    COUNT(*)                                                                    AS action_cnt,
    COUNT(DISTINCT CASE WHEN behavior_type = 'pv' THEN item_id END)             AS pv_item_cnt,
    COUNT(DISTINCT session_id)                                                  AS session_cnt,
    MIN(hour)                                                                   AS first_hour,
    MAX(hour)                                                                   AS last_hour,
    1
FROM dwd_behavior_fact
GROUP BY user_id, dt;

-- ------------------------------------------------------------
-- 4. dws_category_daily
--    粒度：dt + category_id。用于类目排行、类目转化率、大促类目对比
--    注意：buy_users 与 dws_user_daily 的 buy 人数不可相加（跨类目会重复）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS dws_category_daily;

CREATE TABLE dws_category_daily (
    dt           DATE    NOT NULL COMMENT '日期',
    category_id  BIGINT  NOT NULL COMMENT '类目ID',
    pv_cnt       INT     NOT NULL DEFAULT 0 COMMENT '浏览次数',
    pv_users     INT     NOT NULL DEFAULT 0 COMMENT '浏览人数',
    uv_users     INT     NOT NULL DEFAULT 0 COMMENT '访问人数（该类目有任意行为的去重用户）',
    cart_cnt     INT     NOT NULL DEFAULT 0 COMMENT '加购次数',
    cart_users   INT     NOT NULL DEFAULT 0 COMMENT '加购人数',
    fav_users    INT     NOT NULL DEFAULT 0 COMMENT '收藏人数',
    buy_cnt      INT     NOT NULL DEFAULT 0 COMMENT '成交笔数',
    buy_users    INT     NOT NULL DEFAULT 0 COMMENT '成交人数',
    item_cnt     INT     NOT NULL DEFAULT 0 COMMENT '涉及的去重商品数',
    session_cnt  INT     NOT NULL DEFAULT 0 COMMENT '会话数',
    PRIMARY KEY (dt, category_id),
    KEY idx_category (category_id, dt)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '类目-日汇总：类目转化率与排名的输入；人数指标不可跨类目加总';

INSERT INTO dws_category_daily
    (dt, category_id, pv_cnt, pv_users, uv_users, cart_cnt, cart_users,
     fav_users, buy_cnt, buy_users, item_cnt, session_cnt)
SELECT
    dt,
    category_id,
    SUM(CASE WHEN behavior_type = 'pv'   THEN 1 ELSE 0 END)                     AS pv_cnt,
    COUNT(DISTINCT CASE WHEN behavior_type = 'pv'   THEN user_id END)            AS pv_users,
    -- 访问人数：不区分行为类型，与全站 uv 口径一致（真实数据存在无浏览直接购买）
    COUNT(DISTINCT user_id)                                                      AS uv_users,
    SUM(CASE WHEN behavior_type = 'cart' THEN 1 ELSE 0 END)                     AS cart_cnt,
    COUNT(DISTINCT CASE WHEN behavior_type = 'cart' THEN user_id END)            AS cart_users,
    COUNT(DISTINCT CASE WHEN behavior_type = 'fav'  THEN user_id END)            AS fav_users,
    SUM(CASE WHEN behavior_type = 'buy'  THEN 1 ELSE 0 END)                     AS buy_cnt,
    COUNT(DISTINCT CASE WHEN behavior_type = 'buy'  THEN user_id END)            AS buy_users,
    COUNT(DISTINCT item_id)                                                      AS item_cnt,
    COUNT(DISTINCT session_id)                                                   AS session_cnt
FROM dwd_behavior_fact
GROUP BY dt, category_id;

ANALYZE TABLE dws_user_daily, dws_category_daily, dim_date, dim_category;

-- ============================================================
-- 自检（跑完手动执行，三条口径不变量）
-- ============================================================
-- 1) 用户日表总额必须与 dwd 完全相等（PV / 行为数）
-- SELECT
--   (SELECT SUM(pv_cnt)     FROM dws_user_daily)    AS dws_pv,
--   (SELECT COUNT(*)        FROM dwd_behavior_fact WHERE behavior_type = 'pv') AS dwd_pv,
--   (SELECT SUM(action_cnt) FROM dws_user_daily)    AS dws_actions,
--   (SELECT COUNT(*)        FROM dwd_behavior_fact) AS dwd_actions;
--
-- 2) 类目日表 PV 总额与用户日表相等，但人数不可相加（这是预期差异，不是 bug）
-- SELECT
--   (SELECT SUM(pv_cnt)   FROM dws_category_daily) AS cat_pv,
--   (SELECT SUM(pv_users) FROM dws_category_daily) AS cat_pv_users_sum,
--   (SELECT COUNT(DISTINCT user_id) FROM dwd_behavior_fact WHERE behavior_type = 'pv') AS real_pv_users;
-- -- cat_pv_users_sum 必然大于 real_pv_users，说明“人数不可加总”
--
-- 3) 留存的分母：每日活跃人数，检查有无断层
-- SELECT dt, COUNT(*) AS active_users
-- FROM dws_user_daily GROUP BY dt ORDER BY dt;
--
-- 4) 类目 Top10，确认占位名与业务直觉不冲突
-- SELECT c.category_id, c.category_name, SUM(d.buy_cnt) AS buy_cnt
-- FROM dws_category_daily d
-- JOIN dim_category c ON c.category_id = d.category_id
-- GROUP BY c.category_id, c.category_name
-- ORDER BY buy_cnt DESC LIMIT 10;
