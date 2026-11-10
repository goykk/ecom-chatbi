-- ============================================================
-- 05_funnel_rfm_retention.sql
-- ads 层：转化漏斗 / RFM 分层 / 留存矩阵（+ 用户维度表）
--
-- 上游：dws_user_daily（03）、path_user_session（04）、meta_dataset_range（01）
-- 下游：metrics.yaml 的 high_value_users / repurchase_users /
--       conversion_rate；FineBI 五大主题看板
--
-- RFM 方案（方案 A）：
--   R = 距数据集 max_dt 的天数
--   F = 窗口内成交笔数
--   M = 窗口内购买的去重商品数（金额缺失，用“购买宽度”近似，
--       比直接用购买次数更接近消费额语义，也避免 F/M 完全共线）
--   三个阈值必须与 metrics.yaml business_rules.rfm_thresholds 一致
-- ============================================================

USE ecom_chatbi;

SET SESSION time_zone = '+08:00';

-- 阈值与语义层同源，改一处必须改两处；后续可抽到 cfg 表
SET @r_hi_days   := 7;    -- R <= 7 天记为高
SET @f_hi_times  := 2;    -- F >= 2 笔记为高
SET @m_hi_items  := 3;    -- M >= 3 个去重商品记为高

-- ------------------------------------------------------------
-- 1. dim_user：用户基础画像（全员，含未购买用户）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS dim_user;

CREATE TABLE dim_user (
    user_id      BIGINT   NOT NULL COMMENT '用户ID',
    first_dt     DATE     NOT NULL COMMENT '首次活跃日期',
    last_dt      DATE     NOT NULL COMMENT '最后活跃日期',
    active_days  INT      NOT NULL COMMENT '活跃天数',
    buy_times    INT      NOT NULL DEFAULT 0 COMMENT '成交笔数',
    buy_days     INT      NOT NULL DEFAULT 0 COMMENT '有成交的天数',
    last_buy_dt  DATE     NULL COMMENT '最近一次成交日期',
    is_buyer     TINYINT  NOT NULL DEFAULT 0 COMMENT '是否购买过',
    PRIMARY KEY (user_id),
    KEY idx_last_buy (last_buy_dt),
    KEY idx_buyer (is_buyer)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '用户维度：全部用户；RFM 只覆盖 is_buyer=1 的子集';

INSERT INTO dim_user
    (user_id, first_dt, last_dt, active_days, buy_times, buy_days, last_buy_dt, is_buyer)
SELECT
    user_id,
    MIN(dt)                                                       AS first_dt,
    MAX(dt)                                                       AS last_dt,
    COUNT(DISTINCT dt)                                            AS active_days,
    SUM(buy_cnt)                                                  AS buy_times,
    COUNT(DISTINCT CASE WHEN buy_cnt > 0 THEN dt END)              AS buy_days,
    MAX(CASE WHEN buy_cnt > 0 THEN dt END)                        AS last_buy_dt,
    CASE WHEN SUM(buy_cnt) > 0 THEN 1 ELSE 0 END                  AS is_buyer
FROM dws_user_daily
GROUP BY user_id;

-- ------------------------------------------------------------
-- 2. ads_rfm_segments：RFM 分层快照（只含购买过的用户）
--    as_of_dt 固定为数据集 max_dt，保证同一份数据重复跑结果一致
--    非购买用户不进入 RFM（经典 RFM 不适用于零购买人群），
--    但保留在 dim_user 中，可用 is_buyer=0 单独统计
-- ------------------------------------------------------------
DROP TABLE IF EXISTS ads_rfm_segments;

CREATE TABLE ads_rfm_segments (
    user_id       BIGINT      NOT NULL COMMENT '用户ID',
    as_of_dt      DATE        NOT NULL COMMENT 'RFM 计算基准日 = 数据集 max_dt',
    r_days        INT         NULL COMMENT '最近一次成交距 as_of_dt 的天数',
    f_times       INT         NOT NULL COMMENT '窗口内成交笔数',
    m_items       INT         NOT NULL COMMENT '窗口内购买的去重商品数（金额近似）',
    r_score       TINYINT     NOT NULL COMMENT 'R 打分 0/1',
    f_score       TINYINT     NOT NULL COMMENT 'F 打分 0/1',
    m_score       TINYINT     NOT NULL COMMENT 'M 打分 0/1',
    rfm_segment   VARCHAR(16) NOT NULL COMMENT '分层：高价值/新客/潜力/流失预警/一般',
    rfm_code      VARCHAR(3)  NOT NULL COMMENT '三位编码，如 111 / 100',
    PRIMARY KEY (user_id),
    KEY idx_segment (rfm_segment, as_of_dt)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = 'RFM 分层快照；阈值与 metrics.yaml 保持一致';

INSERT INTO ads_rfm_segments
    (user_id, as_of_dt, r_days, f_times, m_items,
     r_score, f_score, m_score, rfm_segment, rfm_code)
WITH asof AS (
    SELECT max_dt AS as_of_dt FROM meta_dataset_range WHERE id = 1
),
base AS (
    SELECT
        du.user_id,
        a.as_of_dt,
        DATEDIFF(a.as_of_dt, du.last_buy_dt) AS r_days,
        du.buy_times                          AS f_times,
        COALESCE(m.m_items, 0)                AS m_items
    FROM dim_user du
    CROSS JOIN asof a
    LEFT JOIN (
        -- 购买宽度：该用户成交过的去重商品数
        SELECT user_id, COUNT(DISTINCT item_id) AS m_items
        FROM dwd_behavior_fact
        WHERE behavior_type = 'buy'
        GROUP BY user_id
    ) m ON m.user_id = du.user_id
    WHERE du.is_buyer = 1
),
scored AS (
    SELECT
        b.*,
        CASE WHEN b.r_days <= @r_hi_days  THEN 1 ELSE 0 END AS r_score,
        CASE WHEN b.f_times >= @f_hi_times THEN 1 ELSE 0 END AS f_score,
        CASE WHEN b.m_items >= @m_hi_items THEN 1 ELSE 0 END AS m_score
    FROM base b
)
SELECT
    user_id,
    as_of_dt,
    r_days,
    f_times,
    m_items,
    r_score,
    f_score,
    m_score,
    -- 判定顺序即优先级：高价值 → 新客 → 潜力 → 流失预警 → 一般
    CASE
        WHEN r_score = 1 AND f_score = 1 AND m_score = 1 THEN '高价值'
        WHEN f_times = 1 AND r_score = 1                 THEN '新客'
        WHEN r_score = 1                                 THEN '潜力'
        WHEN r_score = 0                                 THEN '流失预警'
        ELSE '一般'
    END AS rfm_segment,
    CONCAT(r_score, f_score, m_score) AS rfm_code
FROM scored;

-- ------------------------------------------------------------
-- 3. ads_funnel_daily：每日漏斗
--    说明：浏览 → 加购 / 收藏 → 成交 中，加购与收藏是并行分支而非串行，
--          所以这里同时给出“宽口径阶段人数”和“严格串行口径”
--          （严格口径必须用 path_user_session 判定先加购后成交）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS ads_funnel_daily;

CREATE TABLE ads_funnel_daily (
    dt                     DATE     NOT NULL COMMENT '日期',
    pv_users               INT      NOT NULL COMMENT '浏览人数',
    cart_users             INT      NOT NULL COMMENT '加购人数',
    fav_users              INT      NOT NULL COMMENT '收藏人数',
    buy_users              INT      NOT NULL COMMENT '成交人数',
    pv_to_cart_rate        DECIMAL(6,4) NULL COMMENT '加购人数/浏览人数',
    pv_to_buy_rate         DECIMAL(6,4) NULL COMMENT '成交人数/浏览人数（即转化率）',
    strict_cart_users      INT      NOT NULL DEFAULT 0 COMMENT '路径口径加购人数',
    strict_cart_buy_users  INT      NOT NULL DEFAULT 0 COMMENT '先加购后成交人数',
    strict_cart_to_buy_rate DECIMAL(6,4) NULL COMMENT '先加购后成交/路径口径加购',
    PRIMARY KEY (dt)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '每日漏斗：宽口径阶段人数 + 路径口径严格转化';

INSERT INTO ads_funnel_daily
    (dt, pv_users, cart_users, fav_users, buy_users,
     pv_to_cart_rate, pv_to_buy_rate,
     strict_cart_users, strict_cart_buy_users, strict_cart_to_buy_rate)
WITH wide AS (
    SELECT
        dt,
        SUM(CASE WHEN pv_cnt   > 0 THEN 1 ELSE 0 END) AS pv_users,
        SUM(CASE WHEN cart_cnt > 0 THEN 1 ELSE 0 END) AS cart_users,
        SUM(CASE WHEN fav_cnt  > 0 THEN 1 ELSE 0 END) AS fav_users,
        SUM(CASE WHEN buy_cnt  > 0 THEN 1 ELSE 0 END) AS buy_users
    FROM dws_user_daily
    GROUP BY dt
),
strict_funnel AS (
    SELECT
        dt,
        COUNT(DISTINCT CASE WHEN has_cart = 1 THEN user_id END)          AS strict_cart_users,
        COUNT(DISTINCT CASE WHEN is_cart_before_buy = 1 THEN user_id END) AS strict_cart_buy_users
    FROM path_user_session
    GROUP BY dt
)
SELECT
    w.dt,
    w.pv_users,
    w.cart_users,
    w.fav_users,
    w.buy_users,
    w.cart_users / NULLIF(w.pv_users, 0)                                   AS pv_to_cart_rate,
    w.buy_users  / NULLIF(w.pv_users, 0)                                   AS pv_to_buy_rate,
    COALESCE(s.strict_cart_users, 0)                                       AS strict_cart_users,
    COALESCE(s.strict_cart_buy_users, 0)                                   AS strict_cart_buy_users,
    COALESCE(s.strict_cart_buy_users, 0) / NULLIF(s.strict_cart_users, 0)  AS strict_cart_to_buy_rate
FROM wide w
LEFT JOIN strict_funnel s ON s.dt = w.dt;

-- ------------------------------------------------------------
-- 4. ads_retention_matrix：留存矩阵（次日 / 7 日）
--    分母 = 当日在 dws_user_daily 出现的用户；分子 = 该用户在 dt+N 日仍活跃
--    末尾日期不足 N 天时留 NULL，不要用 0 填充（否则会被误读成“留存崩了”）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS ads_retention_matrix;

CREATE TABLE ads_retention_matrix (
    dt          DATE  NOT NULL COMMENT 'cohort 日期',
    d0_users    INT   NOT NULL COMMENT '当日活跃人数',
    d1_users    INT   NULL     COMMENT '次日仍活跃人数；D+1 不完整时为 NULL',
    d1_rate     DECIMAL(6,4) NULL COMMENT '次日留存率；无 D+1 数据时为 NULL',
    d7_users    INT   NULL COMMENT '7 日后仍活跃人数；无 D+7 数据时为 NULL',
    d7_rate     DECIMAL(6,4) NULL COMMENT '7 日留存率；无 D+7 数据时为 NULL',
    PRIMARY KEY (dt)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '留存矩阵：NULL 表示观察窗不足，不表示留存为 0';

INSERT INTO ads_retention_matrix (dt, d0_users, d1_users, d1_rate, d7_users, d7_rate)
WITH asof AS (
    SELECT max_dt AS as_of_dt FROM meta_dataset_range WHERE id = 1
),
cohort AS (
    SELECT
        a.dt,
        a.user_id,
        DATE_ADD(a.dt, INTERVAL 1 DAY) AS dt_1,
        DATE_ADD(a.dt, INTERVAL 7 DAY) AS dt_7
    FROM dws_user_daily a
),
joined AS (
    SELECT
        c.dt,
        c.user_id,
        c.dt_1,
        c.dt_7,
        b1.user_id AS d1_hit,
        b7.user_id AS d7_hit,
        (SELECT as_of_dt FROM asof) AS as_of_dt
    FROM cohort c
    LEFT JOIN dws_user_daily b1
           ON b1.user_id = c.user_id AND b1.dt = c.dt_1
    LEFT JOIN dws_user_daily b7
           ON b7.user_id = c.user_id AND b7.dt = c.dt_7
)
SELECT
    dt,
    COUNT(*)                                                             AS d0_users,
    -- D+1 不完整时（dt_1 超过完整日上限），留存人数与留存率都必须为 NULL
    CASE WHEN dt_1 <= MAX(as_of_dt) THEN COUNT(DISTINCT d1_hit) END       AS d1_users,
    CASE WHEN dt_1 <= MAX(as_of_dt)
         THEN COUNT(DISTINCT d1_hit) / NULLIF(COUNT(*), 0) END           AS d1_rate,
    CASE WHEN dt_7 <= MAX(as_of_dt)
         THEN COUNT(DISTINCT d7_hit) END                                 AS d7_users,
    CASE WHEN dt_7 <= MAX(as_of_dt)
         THEN COUNT(DISTINCT d7_hit) / NULLIF(COUNT(*), 0) END           AS d7_rate
FROM joined
GROUP BY dt, dt_1, dt_7;

ANALYZE TABLE dim_user, ads_rfm_segments, ads_funnel_daily, ads_retention_matrix;

-- ============================================================
-- 自检（跑完手动执行）
-- ============================================================
-- 1) RFM 分层人数与占比：高价值应占购买用户的 5%~20%，若超过 40% 说明阈值太松
-- SELECT rfm_segment, COUNT(*) AS users,
--        ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 4) AS ratio
-- FROM ads_rfm_segments GROUP BY rfm_segment ORDER BY users DESC;
--
-- 2) 人数守恒：分层人数之和必须等于 is_buyer=1 的用户数
-- SELECT
--   (SELECT COUNT(*) FROM ads_rfm_segments)                  AS rfm_total,
--   (SELECT COUNT(*) FROM dim_user WHERE is_buyer = 1)        AS buyer_total;
--
-- 3) 漏斗口径差：宽口径加购人数 vs 路径口径，差值即“买后加购 + 跨类目会话”的影响
-- SELECT dt, cart_users, strict_cart_users,
--        cart_users - strict_cart_users AS diff
-- FROM ads_funnel_daily ORDER BY dt;
--
-- 4) 留存分母应与每日活跃人数一致
-- SELECT r.dt, r.d0_users, u.active_users
-- FROM ads_retention_matrix r
-- JOIN (SELECT dt, COUNT(*) AS active_users FROM dws_user_daily GROUP BY dt) u
--   ON u.dt = r.dt
-- WHERE r.d0_users <> u.active_users;
-- 期望返回 0 行
