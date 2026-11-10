-- ============================================================
-- 04_behavior_path.sql
-- 行为路径表 path_user_session —— 电商 ChatBI 的差异化资产
--
-- 上游：dwd_behavior_fact(user_id, item_id, category_id, behavior_type, dt, hour, ts, session_id)
-- 下游：metrics.yaml 中 cart_no_buy_users / cart_buy_users /
--       browse_no_cart_users / cart_to_buy_rate
--
-- 粒度：session_id + category_id（一行 = 一个会话在一个类目下的完整路径）
-- 说明：
--   1. 依赖 MySQL 8.0 的窗口函数；5.7 不兼容
--   2. 依赖 dwd 保留原始 ts（毫秒时间戳），仅靠 dt + hour 无法稳定排序
--   3. 跨零点的 session 归属到 MIN(dt)，避免一行跨两天造成重复计数
-- ============================================================

USE ecom_chatbi;

-- path_str 用 GROUP_CONCAT 拼接，默认 1024 会被截断，先调大
SET SESSION group_concat_max_len = 4096;

DROP TABLE IF EXISTS path_user_session;

CREATE TABLE path_user_session (
    session_id        BIGINT       NOT NULL COMMENT '会话ID',
    category_id       BIGINT       NOT NULL COMMENT '类目ID（会话在该类目下的子路径）',
    user_id           BIGINT       NOT NULL COMMENT '用户ID',
    dt                DATE         NOT NULL COMMENT '会话归属日期（MIN(dt)）',
    start_hour        TINYINT      NULL     COMMENT '会话开始小时',
    end_hour          TINYINT      NULL     COMMENT '会话结束小时',

    -- 行为是否发生
    has_pv            TINYINT      NOT NULL DEFAULT 0 COMMENT '是否浏览',
    has_cart          TINYINT      NOT NULL DEFAULT 0 COMMENT '是否加购',
    has_fav           TINYINT      NOT NULL DEFAULT 0 COMMENT '是否收藏',
    has_buy           TINYINT      NOT NULL DEFAULT 0 COMMENT '是否成交',

    -- 各行为首次出现的行为序号（用于判定先后顺序）
    pv_first_seq      INT          NULL     COMMENT '首次浏览序号',
    cart_first_seq    INT          NULL     COMMENT '首次加购序号',
    fav_first_seq     INT          NULL     COMMENT '首次收藏序号',
    buy_first_seq     INT          NULL     COMMENT '首次成交序号',

    -- 路径特征
    pv_item_cnt       INT          NOT NULL DEFAULT 0 COMMENT '浏览的去重商品数',
    action_cnt        INT          NOT NULL DEFAULT 0 COMMENT '行为总条数',
    path_len          INT          NOT NULL DEFAULT 0 COMMENT '路径长度=action_cnt',
    path_str          VARCHAR(255) NULL     COMMENT '行为序列，如 pv>pv>cart>buy',

    -- 派生标记（供语义层直接过滤）
    is_cart_before_buy TINYINT     NOT NULL DEFAULT 0 COMMENT '先加购后成交',
    is_buy_before_cart TINYINT     NOT NULL DEFAULT 0 COMMENT '先成交后加购（买后加购）',
    is_browse_heavy    TINYINT     NOT NULL DEFAULT 0 COMMENT '浏览>=阈值且未加购未成交',

    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, category_id),
    KEY idx_dt_category (dt, category_id),
    KEY idx_user (user_id),
    KEY idx_buy (has_buy, has_cart)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '会话级行为路径表，支撑加购未购/浏览未加购等流失分析';

-- ------------------------------------------------------------
-- Step 1：会话内按 ts 排序，生成行为序号
-- ------------------------------------------------------------
INSERT INTO path_user_session (
    session_id, category_id, user_id, dt, start_hour, end_hour,
    has_pv, has_cart, has_fav, has_buy,
    pv_first_seq, cart_first_seq, fav_first_seq, buy_first_seq,
    pv_item_cnt, action_cnt, path_len, path_str,
    is_cart_before_buy, is_buy_before_cart, is_browse_heavy
)
WITH seqed AS (
    SELECT
        session_id,
        user_id,
        category_id,
        dt,
        hour,
        behavior_type,
        item_id,
        ROW_NUMBER() OVER (
            PARTITION BY session_id, category_id
            ORDER BY ts, item_id
        ) AS seq
    FROM dwd_behavior_fact
    WHERE session_id IS NOT NULL
      AND behavior_type IN ('pv', 'cart', 'fav', 'buy')
),
agg AS (
    SELECT
        session_id,
        category_id,
        user_id,
        MIN(dt)                                                              AS dt,
        MIN(hour)                                                            AS start_hour,
        MAX(hour)                                                            AS end_hour,
        MAX(CASE WHEN behavior_type = 'pv'   THEN 1 ELSE 0 END)              AS has_pv,
        MAX(CASE WHEN behavior_type = 'cart' THEN 1 ELSE 0 END)              AS has_cart,
        MAX(CASE WHEN behavior_type = 'fav'  THEN 1 ELSE 0 END)              AS has_fav,
        MAX(CASE WHEN behavior_type = 'buy'  THEN 1 ELSE 0 END)              AS has_buy,
        MIN(CASE WHEN behavior_type = 'pv'   THEN seq END)                   AS pv_first_seq,
        MIN(CASE WHEN behavior_type = 'cart' THEN seq END)                   AS cart_first_seq,
        MIN(CASE WHEN behavior_type = 'fav'  THEN seq END)                   AS fav_first_seq,
        MIN(CASE WHEN behavior_type = 'buy'  THEN seq END)                   AS buy_first_seq,
        COUNT(DISTINCT CASE WHEN behavior_type = 'pv' THEN item_id END)      AS pv_item_cnt,
        COUNT(*)                                                             AS action_cnt,
        MAX(seq)                                                             AS path_len,
        GROUP_CONCAT(behavior_type ORDER BY seq SEPARATOR '>')               AS path_str
    FROM seqed
    GROUP BY session_id, category_id, user_id
)
SELECT
    session_id,
    category_id,
    user_id,
    dt,
    start_hour,
    end_hour,
    has_pv,
    has_cart,
    has_fav,
    has_buy,
    pv_first_seq,
    cart_first_seq,
    fav_first_seq,
    buy_first_seq,
    pv_item_cnt,
    action_cnt,
    path_len,
    LEFT(path_str, 255) AS path_str,
    -- 先加购后成交：两个序号都存在且加购在前
    CASE
        WHEN cart_first_seq IS NOT NULL AND buy_first_seq IS NOT NULL
             AND cart_first_seq < buy_first_seq THEN 1 ELSE 0
    END AS is_cart_before_buy,
    -- 买后加购：成交在前，说明“加购”不是转化的前置动作
    CASE
        WHEN cart_first_seq IS NOT NULL AND buy_first_seq IS NOT NULL
             AND buy_first_seq < cart_first_seq THEN 1 ELSE 0
    END AS is_buy_before_cart,
    -- 深度浏览未转化：浏览商品数达阈值，且全程无加购、无成交
    CASE
        WHEN pv_item_cnt >= 5 AND has_cart = 0 AND has_buy = 0 THEN 1 ELSE 0
    END AS is_browse_heavy
FROM agg;

-- ============================================================
-- 自检（跑完手动执行，确认口径与 metrics.yaml 一致）
-- ============================================================
-- 1) 行数应与 dwd 的 session×category 组合数一致
-- SELECT COUNT(*) AS session_category_rows,
--        COUNT(DISTINCT session_id) AS sessions
-- FROM path_user_session;
--
-- 2) 原子指标关系：加购人数 >= 加购未购人数 + 加购成交人数（同一用户可能多会话，故用 >=）
-- SELECT
--   (SELECT COUNT(DISTINCT user_id) FROM path_user_session WHERE has_cart = 1)                    AS cart_users,
--   (SELECT COUNT(DISTINCT user_id) FROM path_user_session WHERE has_cart = 1 AND has_buy = 0)    AS cart_no_buy_users,
--   (SELECT COUNT(DISTINCT user_id) FROM path_user_session
--     WHERE is_cart_before_buy = 1)                                                              AS cart_buy_users,
--   (SELECT COUNT(DISTINCT user_id) FROM path_user_session WHERE is_buy_before_cart = 1)          AS buy_before_cart_users;
--
-- 3) 路径分布抽样，确认 path_str 有序
-- SELECT path_str, COUNT(*) AS cnt
-- FROM path_user_session
-- GROUP BY path_str
-- ORDER BY cnt DESC
-- LIMIT 20;
--
-- 4) 阈值分布：pv_item_cnt 达到 5 的用户占比，决定 browse_no_cart_users 是否可用
-- SELECT
--   SUM(CASE WHEN pv_item_cnt >= 5 AND has_cart = 0 AND has_buy = 0 THEN 1 ELSE 0 END)
--     / COUNT(*) AS browse_heavy_ratio
-- FROM path_user_session;
