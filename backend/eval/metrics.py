"""评测指标：结果集比对 + 汇总统计。

核心判断是**执行准确率（execution accuracy）**：
比较 SQL 跑出来的结果集，而不是比较 SQL 字符串。
理由：同一个指标可以有无数种等价写法（JOIN / 子查询 / CTE），
字符串匹配会把正确 SQL 判错，把错误 SQL 判对——这是 ChatBI 评测最常见的坑。
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from decimal import Decimal
from typing import Any, Sequence


def normalize_value(value: Any, ndigits: int = 4) -> Any:
    """把数据库返回值归一：小数四舍五入、日期转字符串、布尔转整数。"""
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity"
        return round(value, ndigits)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return value


def _rows_to_counter(columns: Sequence[str], rows: Sequence[Any],
                     ndigits: int) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        if isinstance(row, dict):
            values = tuple(normalize_value(row.get(c), ndigits) for c in columns)
        else:
            values = tuple(normalize_value(v, ndigits) for v in row)
        counter[values] += 1
    return counter


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _value_close(a: Any, b: Any, abs_tol: float, rel_tol: float = 1e-4) -> bool:
    """整数必须完全相等；小数允许绝对容差或相对容差。

    为什么要容差：
      1) MySQL 的 DECIMAL 除法在「CTE / 派生表的列」上会被截断到 4 位小数，
         写在表达式里却保留更高精度 → 差 1e-4；
      2) 模型常对结果 ROUND(x, 1)，而 gold 返回未取整的 AVG → 差 0.05。
    这两种都是表示差异，不是口径错误；真实口径错误（分子分母用错）差异在 1e-2 以上。
    因此取「绝对 1e-3 或相对 1e-4，二者满足其一」。
    """
    if _is_number(a) and _is_number(b):
        if isinstance(a, int) and isinstance(b, int):
            return a == b
        fa, fb = float(a), float(b)
        diff = abs(fa - fb)
        return diff <= abs_tol or diff <= rel_tol * max(abs(fa), abs(fb))
    return a == b


def _rows_close(gold_row: tuple, pred_row: tuple, abs_tol: float) -> bool:
    if len(gold_row) != len(pred_row):
        return False
    return all(_value_close(g, p, abs_tol) for g, p in zip(gold_row, pred_row))


def result_sets_match(gold_columns: Sequence[str], gold_rows: Sequence[Any],
                      pred_columns: Sequence[str], pred_rows: Sequence[Any],
                      ndigits: int = 4, abs_tol: float = 1e-3) -> tuple[bool, str]:
    """结果集是否一致（忽略列名与行序，保留重复行计数）。

    整数与字符串必须完全一致；小数允许 abs_tol 的绝对误差（默认 1e-3），
    用于吸收 DECIMAL 除法的中间舍入差异。
    """
    if len(gold_columns) != len(pred_columns):
        return False, f"列数不同：gold={len(gold_columns)} / pred={len(pred_columns)}"

    gold = _rows_to_counter(gold_columns, gold_rows, ndigits)
    pred = _rows_to_counter(pred_columns, pred_rows, ndigits)

    if gold == pred:
        return True, "结果集一致"

    # 容差匹配：按「行 + 重数」消费，避免出现一行 pred 同时匹配多行 gold
    remaining = list(pred.elements())
    unmatched_gold: list[tuple] = []
    for gold_row, repeat in gold.items():
        for _ in range(repeat):
            for idx, pred_row in enumerate(remaining):
                if _rows_close(gold_row, pred_row, abs_tol):
                    remaining.pop(idx)
                    break
            else:
                unmatched_gold.append(gold_row)

    if not unmatched_gold and not remaining:
        return True, "结果集一致（小数容差内）"

    detail = (f"gold {sum(gold.values())} 行 / pred {sum(pred.values())} 行；"
              f"仅 gold 有 {len(unmatched_gold)} 行，仅 pred 有 {len(remaining)} 行")
    if unmatched_gold:
        detail += f"；示例差异 {unmatched_gold[0]}"
    return False, detail


def summarize(results: Sequence[Any]) -> dict:
    """results 为 CaseResult 列表（由 runner 定义），这里只读取属性，保持解耦。"""
    items = list(results)
    exec_cases = [r for r in items if getattr(r, "expect", "SQL") != "REFUSE"]
    refuse_cases = [r for r in items if getattr(r, "expect", "SQL") == "REFUSE"]

    exec_ok = sum(1 for r in exec_cases if r.matched)
    refuse_ok = sum(1 for r in refuse_cases if r.matched)

    latencies = sorted(r.latency_ms for r in items)
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0.0

    by_category: dict[str, dict] = {}
    for r in items:
        cat = getattr(r, "category", "unknown")
        stat = by_category.setdefault(cat, {"total": 0, "matched": 0})
        stat["total"] += 1
        stat["matched"] += int(r.matched)

    return {
        "total": len(items),
        "exec_cases": len(exec_cases),
        "exec_matched": exec_ok,
        "execution_accuracy": round(exec_ok / len(exec_cases), 4) if exec_cases else None,
        "refuse_cases": len(refuse_cases),
        "refuse_matched": refuse_ok,
        "refusal_accuracy": round(refuse_ok / len(refuse_cases), 4) if refuse_cases else None,
        "overall_accuracy": round((exec_ok + refuse_ok) / len(items), 4) if items else None,
        "first_pass_rate": round(
            sum(1 for r in exec_cases if r.matched and r.attempts <= 1) / len(exec_cases), 4
        ) if exec_cases else None,
        "error_rate": round(
            sum(1 for r in items if r.predicted_action == "error") / len(items), 4
        ) if items else None,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "p95_latency_ms": round(p95, 1),
        "by_category": {k: {**v, "accuracy": round(v["matched"] / v["total"], 4)}
                        for k, v in by_category.items()},
    }
