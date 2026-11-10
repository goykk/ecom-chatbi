"""SQL 生成：把「召回结果 + 指标口径 + few-shot + 时间规则」组装成一次受约束的生成。

这一层的产出质量，八成取决于 Prompt 里能不能做到三件事：
1. 口径写清楚（指标定义、粒度、别名、限制）；
2. 边界写清楚（没有的字段、该拒答的问题）；
3. 输出格式写死（JSON，便于下游校验与重试）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Sequence

from app.nl2sql.fewshot import Example
from app.nl2sql.llm import LLMClient
from app.nl2sql.schema_recall import RecallResult
from app.semantic.loader import SemanticLayer

SYSTEM_PROMPT = """你是电商 ChatBI 的 SQL 生成器，只负责把问题翻译成一条可在 MySQL 8 执行的查询。

硬性规则：
1. 只能输出一条 SELECT（允许 WITH 子句），禁止任何写操作、DDL、临时表、INTO OUTFILE。
2. 只能使用「可用表与字段」中列出的表名和字段名，禁止猜测字段；需要 JOIN 时按「关联关系」写。
3. 指标必须按「指标口径」计算（含去重口径、粒度、过滤条件）。别名问题以口径定义为准。
4. 时间一律基于「数据时间基准」，禁止使用 CURRENT_DATE、NOW()、CURDATE() 等系统时间。
   相对时间（昨天/上个月/前7天）必须先换算成具体条件。
5. 问题涉及金额、退款、地域、用户属性、商品属性、渠道等「明确不存在的维度」时，不允许编造，
   必须返回拒答。
5.1 指标必须严格按「过滤条件」执行：例如复购率用 f_times >= 2 的成交笔数口径，
    不得改成购买天数、不得自行调整时间窗口或阈值。
6. 不要编造结果数字，不要写解释性文字。
7. 结果列只返回问题所问的指标；只有问题明确要求「按某维度拆分」时才输出该维度列。
   不要额外附加日期、ID 等上下文列（如问"昨天加购率"就只返回一个比率列）。
   计算过程中的中间值（前值、后值、分子、分母）必须放进 CTE 或子查询，
   最终 SELECT 的列数严格等于问题所问的指标数（问变化率就只输出变化率一列）。
8. 比率类指标一律返回**小数**：0.1784 表示 17.84%。
   即使问题问「变化了百分之多少」「提升几成」，也返回小数（-0.0029 表示 -0.29%），
   不要乘以 100，也不要用 CONCAT 拼接 '%' 字符串。
9. 涉及类目维度时，展示列用 dim_category.category_name（JOIN dim_category），
   不要直接输出 category_id；用户看的是类目名，不是数字 ID。
10. 只输出 JSON，格式：
   {"action":"sql","sql":"...","metric_refs":["..."],"dimensions":["..."],"assumptions":["..."]}
   或
   {"action":"refuse","reason":"..."}
"""

USER_TEMPLATE = """【数据时间基准】
{time_block}

【关联关系】
{joins_block}

【可用表与字段】
{tables_block}

【指标口径】
{metrics_block}

【可用维度】
{dimensions_block}

【枚举命中（用户说法 -> 字段取值）】
{value_block}

【明确不存在 / 必须拒答】
{refusal_block}

【参考示例】
{fewshot_block}

【本次问题】
{question}

请输出 JSON。"""


@dataclass
class GenerationResult:
    action: str                        # sql / refuse
    sql: str | None = None
    metric_refs: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    reason: str | None = None
    raw: str = ""


class PromptBuilder:
    def __init__(self, semantic: SemanticLayer) -> None:
        self.semantic = semantic

    def build(self, question: str, recall: RecallResult,
              examples: Sequence[Example],
              time_context: dict | None = None,
              use_semantic: bool = True,
              raw_schema: str | None = None) -> list[dict]:
        s = self.semantic
        # 派生指标要把它引用的原子指标一起注入，否则模型看不到「复购」的口径与过滤条件
        metric_names = list(recall.metrics)
        for name in list(metric_names):
            metric = s.metric(name)
            if metric and metric.is_derived and metric.formula:
                for ref in re.findall(r"[a-z][a-z0-9_]*", metric.formula):
                    if ref in s.metrics and ref not in metric_names:
                        metric_names.append(ref)
        if use_semantic:
            tables_block = "\n\n".join(
                s.table_cards().get(t, f"表 {t}") for t in recall.tables
            ) or "（无）"
            metrics_block = s.metrics_block(metric_names)
            dimensions_block = s.dimensions_block(recall.dimensions)
            value_block = "\n".join(
                f"- {v['dimension']}: 用户说的「{v['label']}」对应取值 {v['value']}"
                for v in recall.value_hits
            ) or "（无）"
        else:
            # 消融：只给建表结构，不给指标口径 / 别名 / 枚举映射 / 术语
            tables_block = raw_schema or s.joins_block() or "（无）"
            metrics_block = "（无指标口径，请根据字段自行判断口径）"
            dimensions_block = "（无维度字典，请根据字段名自行判断）"
            value_block = "（无枚举映射）"
        time_block = s.time_block()
        if time_context:
            time_block += (f"\n当前数据实际范围：{time_context.get('min_dt')} ~ "
                           f"{time_context.get('max_dt')}，基准日 = max_dt")

        fewshot_block = "\n\n".join(
            f"示例{i}\n问题：{ex.question}\nSQL:\n{ex.sql}"
            for i, ex in enumerate(examples, start=1)
        ) or "（无）"

        user = USER_TEMPLATE.format(
            time_block=time_block or "（未配置）",
            joins_block=s.joins_block() or "（无）",
            tables_block=tables_block,
            metrics_block=s.metrics_block(metric_names),
            dimensions_block=s.dimensions_block(recall.dimensions),
            value_block=value_block,
            refusal_block=s.refusal_block() or "（无）",
            fewshot_block=fewshot_block,
            question=question,
        )
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]


class SQLGenerator:
    def __init__(self, semantic: SemanticLayer, llm: LLMClient | None = None) -> None:
        self.semantic = semantic
        self.llm = llm or LLMClient()
        self.builder = PromptBuilder(semantic)

    def generate(self, question: str, recall: RecallResult,
                 examples: Sequence[Example] = (),
                 time_context: dict | None = None,
                 use_semantic: bool = True,
                 raw_schema: str | None = None) -> GenerationResult:
        messages = self.builder.build(question, recall, examples, time_context,
                                      use_semantic=use_semantic, raw_schema=raw_schema)
        raw = self.llm.chat(messages, temperature=0.0, json_mode=True)
        result = parse_generation(raw)

        known = set(self.semantic.metrics)
        result.metric_refs = [m for m in result.metric_refs if m in known]
        result.dimensions = [d for d in result.dimensions
                             if d in self.semantic.dimensions or d in self.semantic.virtual_grain]
        result.raw = raw
        return result


def parse_generation(raw: str) -> GenerationResult:
    """模型偶尔会带 ```json 或前后寒暄，这里做一次稳健解析。"""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    obj = _loads(text)
    if obj is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            obj = _loads(text[start:end + 1])

    if obj is None:
        # 兜底：模型直接吐了 SQL
        if re.match(r"(?is)^(select|with)\b", text):
            return GenerationResult(action="sql", sql=text)
        return GenerationResult(action="refuse", reason="模型输出无法解析为 JSON 或 SQL", raw=raw)

    action = str(obj.get("action", "sql")).lower()
    if action == "refuse":
        return GenerationResult(action="refuse", reason=obj.get("reason") or "数据不支持该问题", raw=raw)

    sql = (obj.get("sql") or "").strip()
    if not sql:
        return GenerationResult(action="refuse", reason="模型未返回 SQL", raw=raw)
    return GenerationResult(
        action="sql",
        sql=sql,
        metric_refs=[str(x) for x in obj.get("metric_refs", [])],
        dimensions=[str(x) for x in obj.get("dimensions", [])],
        assumptions=[str(x) for x in obj.get("assumptions", [])],
        raw=raw,
    )


def _loads(text: str) -> dict | None:
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
