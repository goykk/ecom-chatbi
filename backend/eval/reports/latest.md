# ChatBI 评测报告

- 时间：2026-11-10 12:26:03
- 数据集：`D:\我的文件\temp_20261105_02\ecom-chatbi\backend\eval\dataset\seed.jsonl`
- 模型：gemma4:12b
- 结果比对：执行准确率（结果集比对，小数容差 1e-3，整数严格相等）

## 总览

| 指标 | 值 |
|---|---|
| 用例数 | 2 |
| 执行准确率 | 50.0%（1/2） |
| 拒答准确率 | —（0/0） |
| 总体准确率 | 50.0% |
| 一次通过率 | 50.0% |
| 错误率 | 0.0% |
| 平均 / P95 延迟 | 12128.3 ms / 16758.1 ms |

## 分类表现

| 类别 | 准确数 / 总数 | 准确率 |
|---|---|---|
| traffic | 1/2 | 50.0% |

## 失败样例（1 条）

### tc_002｜traffic｜期望 SQL
- 问题：昨天的浏览人数是多少
- 实际 action：sql；attempts：1
- 原因：gold 1 行 / pred 1 行；仅 gold 有 1 行，仅 pred 有 1 行；示例差异 (23816,)
- 生成 SQL：
```sql
SELECT COUNT(1) AS pv FROM dwd_behavior_fact WHERE dt = (SELECT DATE_SUB(max_dt, INTERVAL '1' DAY) FROM meta_dataset_range WHERE id = 1) AND behavior_type = 'pv' LIMIT 1000
```
- gold SQL：
```sql
SELECT COUNT(DISTINCT user_id) AS pv_users FROM dwd_behavior_fact WHERE behavior_type = 'pv' AND dt = (SELECT DATE_SUB(max_dt, INTERVAL 1 DAY) FROM meta_dataset_range WHERE id = 1)
```


## 明细

| id | 类别 | 结果 | attempts | 延迟(ms) | 说明 |
|---|---|---|---|---|---|
| tc_001 | traffic | PASS | 1 | 16758.1 | 结果集一致 |
| tc_002 | traffic | FAIL | 1 | 7498.5 | gold 1 行 / pred 1 行；仅 gold 有 1 行，仅 pred 有 1 行；示例差异 (23816,) |
