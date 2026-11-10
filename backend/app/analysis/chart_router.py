"""图表推荐：按结果集的形状（列类型 + 行数）做确定性路由。

为什么用规则而不是让 LLM 选图：
1. 选图是纯数据形状问题，规则 100% 可预测、可单元测试；
2. 模型选图容易给出"看着合理但数据无法支撑"的类型（如 1 个点画折线）；
3. 图表类型错了会直接误导结论，这类错误不能交给概率模型。

输出直接是 ECharts option 骨架，前端 `setOption` 即可，不需要再做适配层。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

GOLDEN_HOURS = (20, 21, 22)
TIME_COLUMNS = {"dt", "date", "day", "month", "week"}
# hour 在物理上是整数，但在图表语义上是「离散时段」，必须当分类维度处理，
# 否则会被当成数值轴，导致热力图退化成散点、黄金时段标注失效
DISCRETE_NUMERIC = {"hour"}
RATIO_HINTS = ("rate", "ratio", "占比", "率")
SEGMENT_HINTS = ("rfm_segment", "segment", "分层")


@dataclass
class ChartSpec:
    chart_type: str
    title: str
    option: dict = field(default_factory=dict)
    reason: str = ""
    x: str | None = None
    y: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chart_type": self.chart_type,
            "title": self.title,
            "option": self.option,
            "reason": self.reason,
            "x": self.x,
            "y": self.y,
        }


def route_chart(question: str, columns: Sequence[str],
                rows: Sequence[dict], max_rows: int = 200) -> ChartSpec:
    columns = list(columns)
    data = [dict(r) for r in list(rows)[:max_rows]]
    title = (question or "查询结果").strip()[:60]

    if not data:
        return ChartSpec("empty", title, {}, "结果集为空，不生成图表")

    numeric = [c for c in columns
               if c.lower() not in DISCRETE_NUMERIC and _is_numeric_column(c, data)]
    temporal = [c for c in columns if _is_temporal_column(c, data)]
    categorical = [c for c in columns if c not in numeric and c not in temporal]

    # 1) 单值 KPI
    if len(data) == 1 and len(numeric) == 1 and not categorical:
        return _metric_card(title, numeric[0], data[0][numeric[0]])

    # 2) 单行多指标 → 漏斗（如 pv_users / cart_users / buy_users）
    if len(data) == 1 and len(numeric) >= 3:
        return _funnel(title, numeric, data[0])

    # 3) 时间 + 数值 → 折线（多数值拆多系列）
    if temporal and numeric:
        x = temporal[0]
        if categorical:
            return _multi_line(title, x, categorical[0], numeric[0], data,
                               "时间序列 + 一个分类维度，按分类拆多系列折线")
        return _multi_line(title, x, None, None, data, numeric,
                           "时间序列，趋势优先用折线")

    # 4) 两个数值 + 一个标签 → 散点（RFM 的 R/F 分布就是这种形状）
    if len(numeric) >= 2 and len(categorical) == 1 and len(data) >= 5:
        return _scatter(title, numeric[0], numeric[1], categorical[0], data)

    # 5) 两个分类维度 + 一个数值 → 热力图 / 分组柱
    if len(categorical) >= 2 and len(numeric) == 1:
        if _has_hour(categorical, data) and len(data) >= 6:
            return _heatmap(title, categorical, numeric[0], data)
        return _grouped_bar(title, categorical, numeric[0], data)

    # 6) 一个分类 + 一个数值 → 饼图（占比）/ 柱状（对比）
    if len(categorical) == 1 and len(numeric) == 1:
        cat, num = categorical[0], numeric[0]
        if _is_proportion(cat, num, data) and 2 <= len(data) <= 8:
            return _pie(title, cat, num, data)
        if cat == "hour":
            return _bar_with_golden(title, cat, num, data)
        return _bar(title, cat, num, data)

    # 7) 多个数值、无分类 → 分组柱（横向比较）
    if len(numeric) >= 2 and not categorical:
        return _bar(title, columns[0], numeric[0], data)

    return ChartSpec("table", title,
                     {"type": "table", "columns": columns, "rows": data[:50]},
                     "形状无法可靠映射到图表，退回表格")


# ------------------------------------------------------------
# 图表构造
# ------------------------------------------------------------
def _metric_card(title: str, column: str, value: Any) -> ChartSpec:
    return ChartSpec("metric_card", title,
                     {"type": "metric_card", "label": column, "value": _num(value)},
                     "单行单值，KPI 卡片比图表更直观", y=[column])


def _funnel(title: str, columns: Sequence[str], row: dict) -> ChartSpec:
    data = [{"name": c, "value": _num(row[c])} for c in columns]
    data = [d for d in data if d["value"] is not None]
    data.sort(key=lambda d: d["value"], reverse=True)
    return ChartSpec("funnel", title,
                     {"type": "funnel", "series": [{"type": "funnel", "data": data}]},
                     "单行包含多个阶段指标，漏斗图能直接表达转化损耗", y=list(columns))


def _multi_line(title: str, x: str, series_col: str | None,
                single_y: str | None, data: list[dict],
                ys: Sequence[str] | None = None, reason: str = "") -> ChartSpec:
    xs = [_label(r[x]) for r in data]
    if series_col:
        groups: dict[str, list] = {}
        for r in data:
            groups.setdefault(str(r[series_col]), []).append(_num(r[single_y]))  # type: ignore[index]
        series = [{"name": k, "type": "line", "smooth": True, "data": v}
                  for k, v in groups.items()]
        y_names = [single_y] if single_y else []
    else:
        y_names = list(ys or [])
        series = [{"name": c, "type": "line", "smooth": True,
                   "data": [_num(r[c]) for r in data]} for c in y_names]
    return ChartSpec("line", title,
                     {"type": "line", "xAxis": {"type": "category", "data": xs},
                      "yAxis": {"type": "value"}, "series": series,
                      "legend": {"data": [s["name"] for s in series]}},
                     reason or "时间序列优先折线", x=x, y=list(y_names))


def _bar(title: str, cat: str, num: str, data: list[dict]) -> ChartSpec:
    xs = [_label(r[cat]) for r in data]
    return ChartSpec("bar", title,
                     {"type": "bar", "xAxis": {"type": "category", "data": xs,
                                               "axisLabel": {"interval": 0, "rotate": len(xs) > 8 and 30 or 0}},
                      "yAxis": {"type": "value"},
                      "series": [{"name": num, "type": "bar",
                                  "data": [_num(r[num]) for r in data]}]},
                     "分类对比优先柱状图", x=cat, y=[num])


def _bar_with_golden(title: str, cat: str, num: str, data: list[dict]) -> ChartSpec:
    spec = _bar(title, cat, num, data)
    spec.reason = "时段分布用柱状图；黄金时段（20-22 点）单独标注"
    spec.option["series"][0]["markArea"] = {
        "silent": True,
        "itemStyle": {"color": "rgba(255, 193, 7, 0.15)"},
        "data": [[{"xAxis": str(GOLDEN_HOURS[0])}, {"xAxis": str(GOLDEN_HOURS[-1])}]],
    }
    return spec


def _pie(title: str, cat: str, num: str, data: list[dict]) -> ChartSpec:
    items = [{"name": _label(r[cat]), "value": _num(r[num])} for r in data]
    return ChartSpec("pie", title,
                     {"type": "pie", "series": [{"type": "pie", "radius": ["40%", "70%"],
                                                 "data": items}]},
                     "占比类且类别不多，饼图可直接读出结构", x=cat, y=[num])


def _grouped_bar(title: str, cats: Sequence[str], num: str, data: list[dict]) -> ChartSpec:
    x, series_col = cats[0], cats[1]
    xs = sorted({_label(r[x]) for r in data}, key=str)
    groups: dict[str, list] = {}
    for r in data:
        groups.setdefault(_label(r[series_col]), {})[_label(r[x])] = _num(r[num])
    series = [{"name": k, "type": "bar",
               "data": [g.get(xv) for xv in xs]} for k, g in groups.items()]
    return ChartSpec("bar", title,
                     {"type": "bar", "xAxis": {"type": "category", "data": xs},
                      "yAxis": {"type": "value"}, "series": series,
                      "legend": {"data": [s["name"] for s in series]}},
                     "两个分类维度 + 一个数值，按第二维分组做簇状柱", x=x, y=[num])


def _heatmap(title: str, cats: Sequence[str], num: str,
             data: list[dict]) -> ChartSpec:
    x, y = cats[0], cats[1]
    xs = sorted({_label(r[x]) for r in data}, key=_sort_key)
    ys = sorted({_label(r[y]) for r in data}, key=str)
    points = [[_label(r[x]), _label(r[y]), _num(r[num])] for r in data]
    values = [p[2] for p in points if p[2] is not None]
    return ChartSpec("heatmap", title,
                     {"type": "heatmap",
                      "xAxis": {"type": "category", "data": xs},
                      "yAxis": {"type": "category", "data": ys},
                      "visualMap": {"min": min(values) if values else 0,
                                    "max": max(values) if values else 0,
                                    "calculable": True},
                      "series": [{"type": "heatmap", "data": points}]},
                     "两个分类维度且含时段，热力图能看出「时段 × 类别」的密度", x=x,
                     y=[num])


def _scatter(title: str, x: str, y: str, label: str, data: list[dict]) -> ChartSpec:
    points = [[_num(r[x]), _num(r[y]), _label(r[label])] for r in data]
    return ChartSpec("scatter", title,
                     {"type": "scatter", "xAxis": {"type": "value", "name": x},
                      "yAxis": {"type": "value", "name": y},
                      "series": [{"type": "scatter", "data": points}]},
                     "两个数值维度，散点能看出分布与离群点", x=x, y=[y, label])


def _table(title: str, columns: Sequence[str], data: list[dict]) -> ChartSpec:
    return ChartSpec("table", title,
                     {"type": "table", "columns": list(columns), "rows": data[:50]},
                     "结构不规整，表格最稳妥")


# ------------------------------------------------------------
# 类型判断
# ------------------------------------------------------------
def _is_numeric_column(col: str, rows: list[dict]) -> bool:
    values = [r.get(col) for r in rows if r.get(col) is not None]
    return bool(values) and all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
                                 for v in values)


def _is_temporal_column(col: str, rows: list[dict]) -> bool:
    if col.lower() in TIME_COLUMNS:
        return True
    values = [r.get(col) for r in rows if r.get(col) is not None]
    if not values:
        return False
    if all(isinstance(v, (dt.date, dt.datetime)) for v in values):
        return True
    return all(isinstance(v, str) and len(v) == 10 and v[4] == "-" for v in values[:3])


def _is_proportion(col: str, num: str, rows: list[dict]) -> bool:
    if any(h in num.lower() or h in num for h in RATIO_HINTS):
        return True
    if col.lower() in SEGMENT_HINTS:
        return True
    values = [r.get(num) for r in rows if r.get(num) is not None]
    return bool(values) and all(isinstance(v, float) and 0 <= v <= 1 for v in values)


def _has_hour(cats: Sequence[str], rows: list[dict]) -> bool:
    return any(c == "hour" for c in cats)


def _sort_key(value: Any):
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _label(value: Any) -> str:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _num(value: Any) -> float | None:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return None
