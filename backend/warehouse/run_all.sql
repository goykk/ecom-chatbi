-- ============================================================
-- run_all.sql —— 数仓一键重建（ods → dwd → dws → path → ads）
--
-- 使用方式（必须用 mysql 命令行客户端，SOURCE 是客户端指令）：
--   cd backend/warehouse
--   mysql -h 127.0.0.1 -P 3306 -u root -p --local-infile=1 < run_all.sql
--
-- 前提：
--   1. 数据文件已就位：/data/ods/userbehavior_sample.csv（docker 挂载）
--   2. MySQL 8.0，server 端 local_infile=1
--   3. 不要加 --force —— 默认遇错即停，避免“脏的半成品”继续往下跑
--
-- 只想重建下游（ODS 已导入过）时，注释掉第 1 段即可，可省 80% 时间
-- ============================================================

SET NAMES utf8mb4;
SET SESSION time_zone = '+08:00';

-- 库不存在时先建库，保证 01 里的 USE 不会失败
CREATE DATABASE IF NOT EXISTS ecom_chatbi
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;

USE ecom_chatbi;

SET @pipeline_start := NOW(6);

-- ------------------------------------------------------------
-- 1. ods：原始数据导入 + 数据范围登记
-- ------------------------------------------------------------
SELECT '>>> [1/5] ods import' AS stage;
SOURCE ods/01_import_userbehavior.sql;

-- ------------------------------------------------------------
-- 2. dwd：清洗 / 刷单过滤 / session 切分
-- ------------------------------------------------------------
SELECT '>>> [2/5] dwd clean' AS stage;
SOURCE dwd/02_clean_behavior.sql;

-- ------------------------------------------------------------
-- 前置断言：数据为空就立刻中断
-- 否则 03/05 会因为 min_dt/max_dt 为 NULL 报出 Column 'dt' cannot be null 这类
-- 看不出根因的错误，或者更糟——产出一堆空表却显示「构建成功」
-- 注意：DELIMITER 是 mysql 客户端指令，GUI 里请手动执行本文件末尾的等价 SELECT
-- ------------------------------------------------------------
DROP PROCEDURE IF EXISTS assert_dataset_ready;

DELIMITER $$
CREATE PROCEDURE assert_dataset_ready()
BEGIN
    IF (SELECT COUNT(*) FROM meta_dataset_range WHERE min_dt IS NOT NULL) = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT =
            'DATASET EMPTY: ods 没有数据行。请先用 scripts/download_sample.py 生成抽样文件，并确认 01 的 LOAD DATA 路径正确';
    END IF;
END$$
DELIMITER ;

CALL assert_dataset_ready();

-- ------------------------------------------------------------
-- 3. dws：静态维度 + 用户日/类目日汇总
-- ------------------------------------------------------------
SELECT '>>> [3/5] dws aggregate' AS stage;
SOURCE dws/03_daily_agg.sql;

-- ------------------------------------------------------------
-- 4. path：行为路径表（必须在 dws 之后，依赖 dwd 的 session_id）
-- ------------------------------------------------------------
SELECT '>>> [4/5] path build' AS stage;
SOURCE path/04_behavior_path.sql;

-- ------------------------------------------------------------
-- 5. ads：漏斗 / RFM / 留存
-- ------------------------------------------------------------
SELECT '>>> [5/5] ads build' AS stage;
SOURCE ads/05_funnel_rfm_retention.sql;

-- ============================================================
-- 构建结果报告（直接打印到终端，也可作为 README 的“证据链”截图）
-- ============================================================
SELECT '===== PIPELINE REPORT =====' AS report;

SELECT
    min_dt, max_dt, days, row_count, user_count, ts_unit, loaded_at
FROM meta_dataset_range;

SELECT
    (SELECT COUNT(*) FROM ods_userbehavior)     AS ods_rows,
    (SELECT COUNT(*) FROM dwd_behavior_fact)    AS dwd_rows,
    (SELECT COUNT(DISTINCT session_id) FROM dwd_behavior_fact) AS sessions,
    (SELECT COUNT(*) FROM path_user_session)    AS path_rows,
    (SELECT COUNT(*) FROM dws_user_daily)       AS user_daily_rows,
    (SELECT COUNT(*) FROM dws_category_daily)   AS category_daily_rows,
    (SELECT COUNT(*) FROM ads_rfm_segments)     AS rfm_users,
    (SELECT COUNT(*) FROM dim_user WHERE is_buyer = 1) AS buyer_users;

-- 清洗留存率：低于 90% 说明过滤规则过激，需要回看 02 的阈值
SELECT
    ROUND((SELECT COUNT(*) FROM dwd_behavior_fact)
        / NULLIF((SELECT COUNT(*) FROM ods_userbehavior), 0), 4) AS clean_keep_ratio;

-- 每日漏斗（宽口径 vs 路径口径），差值即口径差
SELECT dt, pv_users, cart_users, buy_users,
       ROUND(pv_to_buy_rate, 4)      AS conversion_rate,
       strict_cart_users,
       ROUND(strict_cart_to_buy_rate, 4) AS strict_cart_to_buy_rate
FROM ads_funnel_daily
ORDER BY dt;

-- RFM 分层分布，确认没有空分层
SELECT rfm_segment, COUNT(*) AS users,
       ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 4) AS ratio
FROM ads_rfm_segments
GROUP BY rfm_segment
ORDER BY users DESC;

-- 留存矩阵（末尾日期的 d7_rate 为 NULL 属正常）
SELECT dt, d0_users, d1_users, ROUND(d1_rate, 4) AS d1_rate,
       d7_users, ROUND(d7_rate, 4) AS d7_rate
FROM ads_retention_matrix
ORDER BY dt;

-- ============================================================
-- 一致性校验：以下每条期望返回 0 行，返回非 0 行说明链路有口径错位
-- ============================================================
SELECT '===== CONSISTENCY CHECKS =====' AS checks;

-- C1：dws 与 dwd 的 PV 总额必须一致
SELECT 'C1 pv_sum_mismatch' AS check_name,
       (SELECT COALESCE(SUM(pv_cnt), 0) FROM dws_user_daily)
     - (SELECT COUNT(*) FROM dwd_behavior_fact WHERE behavior_type = 'pv') AS diff;

-- C2：RFM 人数守恒
SELECT 'C2 rfm_count_mismatch' AS check_name,
       (SELECT COUNT(*) FROM ads_rfm_segments)
     - (SELECT COUNT(*) FROM dim_user WHERE is_buyer = 1) AS diff;

-- C3：留存分母与每日活跃人数一致
SELECT 'C3 retention_denominator_mismatch' AS check_name, COUNT(*) AS bad_rows
FROM ads_retention_matrix r
JOIN (SELECT dt, COUNT(*) AS active_users FROM dws_user_daily GROUP BY dt) u
  ON u.dt = r.dt
WHERE r.d0_users <> u.active_users;

-- C4：路径表不应出现“成交但无浏览”的异常会话（真实数据可少量存在，超过 1% 需排查）
SELECT 'C4 buy_without_pv_ratio' AS check_name,
       ROUND(SUM(CASE WHEN has_pv = 0 AND has_buy = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 4) AS ratio
FROM path_user_session;

SELECT
    CONCAT('pipeline finished in ', ROUND(TIMESTAMPDIFF(SECOND, @pipeline_start, NOW(6)), 1), ' s') AS summary;

-- ============================================================
-- 附：等价的 Shell 版本（CI 或没有 mysql 客户端的场景）
-- ============================================================
-- #!/usr/bin/env bash
-- set -euo pipefail
-- cd "$(dirname "$0")"
-- MYSQL="mysql -h 127.0.0.1 -P 3306 -u root -p$MYSQL_ROOT_PASSWORD --local-infile=1 ecom_chatbi"
-- for f in ods/01_import_userbehavior.sql dwd/02_clean_behavior.sql \
--          dws/03_daily_agg.sql path/04_behavior_path.sql \
--          ads/05_funnel_rfm_retention.sql; do
--   echo ">>> $f"
--   $MYSQL < "$f"
-- done
