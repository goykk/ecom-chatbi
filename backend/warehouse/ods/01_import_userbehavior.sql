-- ============================================================
-- 01_import_userbehavior.sql
-- ods 层：原始行为数据落库 + 数据范围元信息登记
--
-- 数据源：天池 UserBehavior（约 1 亿条，100 万用户 / 4000 万商品 / 9 天）
-- 落库规模：抽样 500 万 ~ 1000 万行（全量入库会让后续每次迭代都卡在运维上）
--
-- 抽样在 Python 侧完成（scripts/download_sample.py），本脚本只负责：
--   1. 建 ods 表（不建主键，避免脏数据被静默丢弃）
--   2. LOAD DATA 导入抽样 CSV
--   3. 建 meta_dataset_range，把数据真实时间范围登记下来
--      —— dimensions.yaml 的 time_resolution、metrics.yaml 的 window 都依赖它
--
-- 前置条件：
--   - MySQL 8.0，且 client/server 均开启 local_infile
--   - CSV 无 BOM，字段顺序 user_id,item_id,category_id,behavior_type,timestamp
-- ============================================================

USE ecom_chatbi;

-- 时区必须在任何 FROM_UNIXTIME 之前固定，否则 dt 会随容器时区漂移
SET SESSION time_zone = '+08:00';

-- 允许客户端本地文件导入（Docker 环境需要 server 端也开 local_infile=1）
SET GLOBAL local_infile = 1;

-- ------------------------------------------------------------
-- 只读账号：与 docker-compose 的 DATABASE_URL、语义层 guardrails.readonly_account 对应
-- 这是校验沙箱之外的第三道防线——账号权限无法被 Prompt 绕过
-- 数据库级授权同样覆盖后续新建的表，无需每次跑完重新 GRANT
-- ------------------------------------------------------------
CREATE USER IF NOT EXISTS 'chatbi_ro'@'%' IDENTIFIED BY 'chatbi_ro';
GRANT SELECT ON ecom_chatbi.* TO 'chatbi_ro'@'%';
FLUSH PRIVILEGES;

DROP TABLE IF EXISTS ods_userbehavior;

CREATE TABLE ods_userbehavior (
    user_id       BIGINT       NOT NULL COMMENT '用户ID',
    item_id       BIGINT       NOT NULL COMMENT '商品ID',
    category_id   BIGINT       NOT NULL COMMENT '类目ID',
    behavior_type VARCHAR(16)  NOT NULL COMMENT '原始行为：pv/cart/fav/buy（未清洗）',
    timestamp     BIGINT       NOT NULL COMMENT '原始时间戳，秒或毫秒，由 02 归一'
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '原始行为数据（抽样），不做任何清洗，保留可回溯的证据';

-- ------------------------------------------------------------
-- 导入抽样文件
-- 路径按 docker-compose 的挂载点约定：./data → /data
-- 若用 mysql CLI 在容器内执行，且文件已在容器内，可改用下一段不带 LOCAL 的写法
-- ------------------------------------------------------------
LOAD DATA LOCAL INFILE '/data/ods/userbehavior_sample.csv'
INTO TABLE ods_userbehavior
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ','
       OPTIONALLY ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(user_id, item_id, category_id, behavior_type, timestamp);

-- 备选写法（文件已在容器内，且 secure_file_priv 指向该目录）：
-- LOAD DATA INFILE '/var/lib/mysql-files/userbehavior_sample.csv'
-- INTO TABLE ods_userbehavior
-- FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
-- LINES TERMINATED BY '\n' IGNORE 1 LINES
-- (user_id, item_id, category_id, behavior_type, timestamp);

-- 导入后建索引：先灌数据再建索引，比带索引导入快一个量级
ALTER TABLE ods_userbehavior
    ADD KEY idx_user (user_id),
    ADD KEY idx_item (item_id),
    ADD KEY idx_behavior (behavior_type);

-- ------------------------------------------------------------
-- 数据范围元信息（供语义层做相对时间解析、供评测做窗口口径）
-- max_dt / min_dt 只取「完整日」：低于日均 2%（且 <10 行）的日期视为不完整日，
-- 否则最后半天会污染「最近一天」类问题（留存、类目排行都会被带偏）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS meta_dataset_range;

CREATE TABLE meta_dataset_range (
    id        TINYINT   NOT NULL DEFAULT 1 COMMENT '单行表',
    min_dt    DATE      NULL COMMENT '首个完整日期（+08:00）',
    max_dt    DATE      NULL COMMENT '最后一个完整日期（+08:00）',
    days      INT       NULL COMMENT '完整日天数',
    row_count BIGINT    NULL COMMENT 'ods 行数（全量，含不完整日）',
    user_count BIGINT   NULL COMMENT '去重用户数',
    ts_unit   VARCHAR(8) NULL COMMENT '原始时间戳单位：s / ms',
    loaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '数据集元信息：相对时间（昨天/上个月）全部以 max_dt 为基准；不完整日不计入';

INSERT INTO meta_dataset_range
    (id, min_dt, max_dt, days, row_count, user_count, ts_unit)
WITH daily AS (
    SELECT
        DATE(FROM_UNIXTIME(CASE WHEN timestamp >= 100000000000
                                THEN FLOOR(timestamp / 1000) ELSE timestamp END)) AS dt,
        COUNT(*) AS row_cnt
    FROM ods_userbehavior
    GROUP BY 1
),
stat AS (
    SELECT AVG(row_cnt) AS avg_cnt FROM daily
),
complete_days AS (
    SELECT d.dt
    FROM daily d, stat s
    WHERE d.row_cnt >= GREATEST(10, s.avg_cnt * 0.02)
)
SELECT
    1,
    (SELECT MIN(dt) FROM complete_days),
    (SELECT MAX(dt) FROM complete_days),
    (SELECT COUNT(*) FROM complete_days),
    (SELECT COUNT(*) FROM ods_userbehavior),
    (SELECT COUNT(DISTINCT user_id) FROM ods_userbehavior),
    -- 单位判定：任一毫秒量级即记为 ms（真实数据应为同一单位，混合属异常）
    (SELECT CASE WHEN SUM(CASE WHEN timestamp >= 100000000000 THEN 1 ELSE 0 END) > 0
                 THEN 'ms' ELSE 's' END FROM ods_userbehavior);

-- 固定时区后再计算，确保与 02 的 dt/hour 一致（已在本文件开头设置）

ANALYZE TABLE ods_userbehavior;

-- ============================================================
-- 自检（跑完手动执行）
-- ============================================================
-- 1) 范围元信息是否合理：days 应约等于 9，min_dt 与 max_dt 跨大促日
-- SELECT * FROM meta_dataset_range;
--
-- 2) 行为类型分布：若出现非 pv/cart/fav/buy 的值，说明源文件格式有问题
-- SELECT behavior_type, COUNT(*) FROM ods_userbehavior GROUP BY behavior_type ORDER BY 2 DESC;
--
-- 3) 抽样是否保住了用户量级：user_count 应在 50 万以上量级
--    （抽样按用户维度做分层抽样，不能纯随机按行抽，否则会话被切碎）
-- SELECT user_count, row_count, ROUND(row_count / user_count, 2) AS rows_per_user
-- FROM meta_dataset_range;
--
-- 4) 每天行数是否均匀：大促当天应显著高于均值，这是真实性校验
-- SELECT DATE(FROM_UNIXTIME(CASE WHEN timestamp >= 100000000000
--                                THEN FLOOR(timestamp/1000) ELSE timestamp END)) AS dt,
--        COUNT(*) AS cnt
-- FROM ods_userbehavior GROUP BY 1 ORDER BY 1;
