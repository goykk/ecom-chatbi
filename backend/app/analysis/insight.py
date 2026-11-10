"""归因解读：把结果集压成结构化摘要，再让模型给出 2~3 句解释。

三条硬约束（写进 Prompt 也写进代码）：
1. 只能基于「结果集摘要」说话，禁止引入数据之外的因素（竞品、天气、政策…）；
2. 数据不足以归因时，明确说「数据不支持」，而不是硬编一个因果故事；
3. 没有模型时必须有可用的规则版解读——否则缺 key 的环境整条功能哑火。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # 避免 analysis ←→ nl2sql 的循环导入（运行时靠鸭子类型）
    from app.nl2sql.llm import LLMClient

SYSTEM_PROMPT = """你是电商数据分析师，只根据给定的查询结果写 2~3 句解读。

硬约束：
1. 只能使用【结果集摘要】里的数字与维度，禁止引入任何外部信息或常识推测。
2. 禁止编造数据；摘要里没有的量（金额、退款、地域、用户画像等）一律不许出现。
3. 如果数据不足以支撑归因，直接说明「数据不足以归因」，不要编因果。
4. 不要复述问题，不要写"根据数据"这类空话，直接给结论。
5. 只输出 JSON：
   {"summary":"一句话结论","drivers":["可验证的驱动因素"],"caveats":["口径限制"]}
"""

USER_TEMPLATE = """【问题】
{question}

【结果集摘要】
{summary}

注意：本数据集无金额/退款/地域/用户画像字段，涉及这些内容必须说明不可得。
请输出 JSON。"""


@dataclass
class Insight:
    summary: str
    drivers: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    source: str = "rule"

    def to_dict(self) -> dict:
        return {"summary": self.summary, "drivers": self.drivers,
                "caveats": self.caveats, "source": self.source}


def summarize_rows(columns: Sequence[str], rows: Sequence[dict],
                   max_rows: int = 30) -> str:
    """把结果集压成模型能读、且不会超 token 的摘要。"""
    columns = list(columns)
    data = [dict(r) for r in list(rows)[:max_rows]]
    total = len(rows)
    lines = [f"列：{', '.join(columns)}", f"行数：{total}"]

    if total > max_rows:
        lines.append(f"（仅展示前 {max_rows} 行，模型不得据此推断全量）")

    for row in data:
        parts = []
        for c in columns:
            v = row.get(c)
            if isinstance(v, Decimal):
                v = round(float(v), 4)
            elif isinstance(v, (dt.date, dt.datetime)):
                v = v.isoformat()
            parts.append(f"{c}={v}")
        lines.append("- " + "，".join(parts))

    return "\n".join(lines)


class InsightGenerator:
    def __init__(self, llm: LLMClient | None = None, max_rows: int = 30) -> None:
        self.llm = llm
        self.max_rows = max_rows

    def generate(self, question: str, columns: Sequence[str],
                 rows: Sequence[dict]) -> Insight:
        if not rows:
            return Insight("结果集为空，无法给出解读。",
                           caveats=["查询未返回数据"], source="rule")

        if self.llm is None:
            return rule_based_insight(columns, rows)

        try:
            raw = self.llm.chat(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": USER_TEMPLATE.format(
                     question=question, summary=summarize_rows(columns, rows, self.max_rows))}],
                temperature=0.2, json_mode=True,
            )
            return _parse_insight(raw)
        except Exception as exc:  # noqa: BLE001 - 模型失败要降级而不是让接口报错
            fallback = rule_based_insight(columns, rows)
            fallback.caveats.append(f"模型解读不可用，已降级为规则解读（{type(exc).__name__}）")
            return fallback


def rule_based_insight(columns: Sequence[str], rows: Sequence[dict]) -> Insight:
    """无模型时的确定性解读：只做可验证的对比与占比陈述。"""
    columns = list(columns)
    data = [dict(r) for r in rows]
    caveats = ["本数据集无金额字段，无法给出金额类解释"]

    numeric = [c for c in columns
               if all(isinstance(r.get(c), (int, float, Decimal)) and not isinstance(r.get(c), bool)
                      for r in data if r.get(c) is not None)
               and any(r.get(c) is not None for r in data)]
    temporal = [c for c in columns if c.lower() in {"dt", "date", "day"}]
    categorical = [c for c in columns if c not in numeric and c not in temporal]

    if not numeric:
        return Insight("结果集没有可比较的数值列，无法给出结论。", caveats=caveats)

    num = numeric[0]
    values = [float(r[num]) for r in data if r.get(num) is not None]

    # 时间序列：最新一天 vs 前几日均值
    if temporal and len(data) >= 3 and len(values) >= 3:
        latest, prior = values[-1], values[:-1]
        base = sum(prior) / len(prior)
        change = (latest - base) / base if base else None
        direction = "高于" if latest >= base else "低于"
        text = (f"最新一天（{data[-1].get(temporal[0])}）的 {num} 为 {latest:g}，"
                f"{direction}前 {len(prior)} 天均值 {base:.4g}")
        if change is not None:
            text += f"，变动 {change:+.2%}"
        return Insight(text + "。", caveats=caveats)

    # 单行：直接陈述值
    if len(data) == 1:
        return Insight(f"{num} = {values[0]:g}。", caveats=caveats)

    # 分类 + 数值：给出首位与占比
    if categorical:
        cat = categorical[0]
        pairs = [(str(r.get(cat)), float(r[num])) for r in data if r.get(num) is not None]
        pairs.sort(key=lambda p: p[1], reverse=True)
        total = sum(v for _, v in pairs) or 1
        top_name, top_value = pairs[0]
        share = top_value / total
        text = (f"{cat} 中 {top_name} 的 {num} 最高，为 {top_value:g}，"
                f"占全部 {len(pairs)} 项的 {share:.2%}")
        if len(pairs) >= 2:
            text += f"；最低为 {pairs[-1][0]}（{pairs[-1][1]:g}）"
        return Insight(text + "。", caveats=caveats)

    return Insight(f"{num} 取值范围 {min(values):g} ~ {max(values):g}，均值 "
                   f"{sum(values) / len(values):.4g}。", caveats=caveats)


def _parse_insight(raw: str) -> Insight:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    obj = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return Insight(text[:200] or "模型未返回可解析解读。", source="llm")
    return Insight(
        summary=str(obj.get("summary") or "").strip() or "模型未给出结论。",
        drivers=[str(x) for x in obj.get("drivers", [])][:4],
        caveats=[str(x) for x in obj.get("caveats", [])][:4],
        source="llm",
    )
