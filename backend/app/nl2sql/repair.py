"""SQL 自修复：把数据库报错当成反馈重新生成。

三条纪律（否则会陷入死循环、越修越错）：
1. 最多 2 轮（guardrails.max_repair_rounds）
2. 同一错误签名重复出现立即终止 —— 说明模型没有理解报错
3. 修完必须再过一次 validator，不能让“修好的 SQL”绕过白名单
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.nl2sql.llm import LLMClient
from app.nl2sql.schema_recall import RecallResult
from app.nl2sql.validator import SQLValidator
from app.semantic.loader import SemanticLayer

REPAIR_SYSTEM = """你是 SQL 调试器。根据原问题、当前 SQL 和数据库报错，输出修正后的 SQL。

规则：
1. 只输出一段 SQL，不要 JSON、不要解释、不要 Markdown 代码块标记。
2. 必须仍是只读 SELECT，只能使用给定的表和字段。
3. 只做必要修改，不要重写整体结构，不要改变指标口径。
4. 报错涉及未知字段时，回到指标/维度定义里找正确字段，不要猜。
"""

REPAIR_TEMPLATE = """【可用表与字段】
{tables_block}

【指标口径】
{metrics_block}

【原问题】
{question}

【当前 SQL】
{sql}

【数据库报错】
{error}

请输出修正后的 SQL。"""


@dataclass
class RepairOutcome:
    sql: str | None
    attempts: int
    errors: list[str] = field(default_factory=list)


class SQLRepairer:
    def __init__(self, semantic: SemanticLayer, llm: LLMClient,
                 validator: SQLValidator | None = None, max_rounds: int | None = None) -> None:
        self.semantic = semantic
        self.llm = llm
        self.validator = validator or SQLValidator(semantic)
        self.max_rounds = int(
            max_rounds or (semantic.guardrails or {}).get("max_repair_rounds", 2)
        )

    def repair(self, question: str, sql: str, error: str,
               recall: RecallResult | None = None) -> RepairOutcome:
        current = sql
        seen: set[str] = set()
        history: list[str] = []

        for attempt in range(1, self.max_rounds + 1):
            signature = fingerprint(error)
            if signature in seen:
                history.append(f"第 {attempt} 轮：错误重复（{signature}），终止避免死循环")
                break
            seen.add(signature)

            candidate = self._call(question, current, error, recall)
            if not candidate:
                history.append(f"第 {attempt} 轮：模型未给出可用 SQL")
                break

            check = self.validator.validate(candidate)
            if check.ok:
                return RepairOutcome(check.sql, attempt, history)
            history.append(f"第 {attempt} 轮修复未通过校验：{check.reason}")
            current, error = candidate, check.reason

        return RepairOutcome(None, len(history), history)

    def _call(self, question: str, sql: str, error: str,
              recall: RecallResult | None) -> str | None:
        metrics = recall.metrics if recall else []
        tables = recall.tables if recall else []
        user = REPAIR_TEMPLATE.format(
            tables_block="\n\n".join(
                self.semantic.table_cards().get(t, f"表 {t}") for t in tables
            ) or "（未召回，按原 SQL 中的表）",
            metrics_block=self.semantic.metrics_block(metrics) if metrics else "（无）",
            question=question,
            sql=sql,
            error=error[:1200],
        )
        raw = self.llm.chat(
            [{"role": "system", "content": REPAIR_SYSTEM},
             {"role": "user", "content": user}],
            temperature=0.0,
        )
        return extract_sql(raw)


def extract_sql(text: str) -> str | None:
    text = text.strip()
    text = re.sub(r"^```(?:sql)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()

    # 模型可能仍返回 JSON，取其中的 sql 字段
    m = re.search(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', text, flags=re.S)
    if m:
        return m.group(1).encode("utf-8").decode("unicode_escape")

    m = re.search(r"(?is)((?:with|select)\b.*)", text)
    if not m:
        return None
    sql = m.group(1).strip()
    # 去掉结尾的解释性文字（只保留到分号或末尾）
    if ";" in sql:
        sql = sql.split(";")[0] + ";"
    return sql.rstrip(";").strip()


def fingerprint(error: str) -> str:
    """把报错里的具体字段名、行号归一化，用于识别“同一个错误”。"""
    normalized = re.sub(r"`[^`]*`", "`?`", error)
    normalized = re.sub(r"\d+", "N", normalized)
    return normalized.strip().lower()[:200]
