"""NL2SQL 主链路：召回 → few-shot → 生成 → 校验 → 执行 → 自修复。

FastAPI 层只需要调用 ask()；评测脚本也调用同一个入口，
保证「线上跑的」和「评测测的」是同一套逻辑——这是执行准确率可信的前提。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Protocol

from app.analysis.chart_router import route_chart
from app.analysis.insight import InsightGenerator
from app.nl2sql.embeddings import Embedder, get_embedder
from app.nl2sql.fewshot import FewShotLibrary
from app.nl2sql.generator import SQLGenerator
from app.nl2sql.llm import LLMClient
from app.nl2sql.repair import SQLRepairer
from app.nl2sql.schema_recall import SchemaRetriever
from app.nl2sql.validator import SQLValidator
from app.semantic.loader import SemanticLayer


class Executor(Protocol):
    def query(self, sql: str, max_rows: int) -> tuple[list[str], list[dict]]: ...
    def time_range(self) -> dict | None: ...


@dataclass
class QueryResult:
    question: str
    action: str                                  # sql / refuse / error
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    reason: str | None = None
    metric_refs: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    recall: dict | None = None
    chart: dict | None = None
    insight: dict | None = None
    attempts: int = 0
    repair_notes: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "question": self.question,
            "action": self.action,
            "sql": self.sql,
            "row_count": len(self.rows),
            "metric_refs": self.metric_refs,
            "attempts": self.attempts,
            "reason": self.reason,
            "timings_ms": self.timings_ms,
            "recall": self.recall,
            "warnings": self.validation_warnings,
            "chart": self.chart,
            "insight": self.insight,
        }


class NL2SQLPipeline:
    def __init__(self, semantic: SemanticLayer | None = None,
                 llm: LLMClient | None = None,
                 executor: Executor | None = None,
                 embedder: Embedder | None = None,
                 fewshot_path: str | None = None,
                 use_semantic: bool = True,
                 raw_schema: str | None = None) -> None:
        self.semantic = semantic or SemanticLayer.load()
        self.llm = llm or LLMClient()
        self.embedder = embedder or get_embedder()
        self.use_semantic = use_semantic
        self.raw_schema = raw_schema
        self.retriever = SchemaRetriever(self.semantic, self.embedder)
        self.fewshots = FewShotLibrary.load(fewshot_path, self.embedder)
        self.validator = SQLValidator(self.semantic)
        self.generator = SQLGenerator(self.semantic, self.llm)
        self.repairer = SQLRepairer(self.semantic, self.llm, self.validator)
        self.executor = executor
        self._time_context: dict | None = None

    # ---------------- 主入口 ----------------
    def ask(self, question: str, execute: bool | None = None,
            fewshot_k: int = 3, with_insight: bool = False) -> QueryResult:
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        execute = self.executor is not None if execute is None else execute

        recall = self.retriever.recall(question)
        timings["recall"] = _ms(t0)

        # 确定性拒答优先：不存在的维度不该交给模型「猜」
        refusal = self.semantic.refusal_reason(question)
        if refusal:
            return QueryResult(question=question, action="refuse", reason=refusal,
                               recall=recall.as_dict(), timings_ms=timings)

        t = time.perf_counter()
        examples = self.fewshots.select(question, k=fewshot_k)
        time_context = self._get_time_context() if self.executor else None
        timings["fewshot"] = _ms(t)

        t = time.perf_counter()
        try:
            generation = self.generator.generate(
                question, recall, examples, time_context,
                use_semantic=self.use_semantic, raw_schema=self.raw_schema)
        except Exception as exc:  # noqa: BLE001 - 生成失败要作为可解释结果返回
            return QueryResult(question=question, action="error", reason=f"生成失败：{exc}",
                               recall=recall.as_dict(), timings_ms=timings)
        timings["generate"] = _ms(t)

        if generation.action == "refuse":
            return QueryResult(question=question, action="refuse", reason=generation.reason,
                               metric_refs=generation.metric_refs,
                               recall=recall.as_dict(), timings_ms=timings)

        sql = generation.sql or ""
        t = time.perf_counter()
        check = self.validator.validate(sql)
        timings["validate"] = _ms(t)

        attempts = 1
        repair_notes: list[str] = []
        if not check.ok:
            t = time.perf_counter()
            outcome = self.repairer.repair(question, sql, check.reason, recall)
            timings["repair"] = _ms(t)
            attempts += outcome.attempts
            repair_notes = outcome.errors
            if not outcome.sql:
                return QueryResult(question=question, action="error",
                                   sql=sql, reason=f"SQL 校验失败：{check.reason}",
                                   validation_warnings=check.warnings,
                                   metric_refs=generation.metric_refs, recall=recall.as_dict(),
                                   attempts=attempts, repair_notes=repair_notes,
                                   timings_ms=timings)
            sql = outcome.sql
            check = self.validator.validate(sql)
            if not check.ok:
                return QueryResult(question=question, action="error", sql=sql,
                                   reason=f"修复后仍未通过校验：{check.reason}",
                                   validation_warnings=check.warnings,
                                   metric_refs=generation.metric_refs, recall=recall.as_dict(),
                                   attempts=attempts, repair_notes=repair_notes,
                                   timings_ms=timings)

        if not execute:
            return QueryResult(question=question, action="sql", sql=check.sql,
                               metric_refs=generation.metric_refs,
                               validation_warnings=check.warnings,
                               recall=recall.as_dict(), attempts=attempts,
                               repair_notes=repair_notes, timings_ms=timings)

        # ---------------- 执行 + 报错修复 ----------------
        max_rows = int((self.semantic.guardrails or {}).get("auto_limit", 1000))
        error: str | None = None
        for _ in range(self.repairer.max_rounds + 1):
            t = time.perf_counter()
            try:
                columns, rows = self.executor.query(check.sql, max_rows)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - 数据库报错即反馈给修复器
                timings["execute"] = _ms(t)
                error = str(exc)
                t = time.perf_counter()
                outcome = self.repairer.repair(question, check.sql, error, recall)
                timings["repair"] = _ms(t)
                attempts += outcome.attempts
                repair_notes += outcome.errors
                if not outcome.sql:
                    break
                sql = outcome.sql
                check = self.validator.validate(sql)
                if not check.ok:
                    break
                continue

            timings["execute"] = _ms(t)

            # 图表推荐是纯规则，成本极低，始终生成；归因解读要调模型，按需开启。
            # 放在 execute 的 try 之外：图表/解读失败不该被当成 SQL 报错去触发修复。
            t = time.perf_counter()
            chart = route_chart(question, columns, rows).to_dict()
            timings["chart"] = _ms(t)

            insight = None
            if with_insight and rows:
                t = time.perf_counter()
                insight = InsightGenerator(self.llm).generate(
                    question, columns, rows).to_dict()
                timings["insight"] = _ms(t)

            return QueryResult(question=question, action="sql", sql=check.sql,
                               columns=columns, rows=rows,
                               metric_refs=generation.metric_refs,
                               validation_warnings=check.warnings,
                               recall=recall.as_dict(), attempts=attempts,
                               repair_notes=repair_notes, timings_ms=timings,
                               chart=chart, insight=insight)

        return QueryResult(question=question, action="error", sql=check.sql,
                           reason=f"执行失败：{error}",
                           metric_refs=generation.metric_refs,
                           validation_warnings=check.warnings, recall=recall.as_dict(),
                           attempts=attempts, repair_notes=repair_notes, timings_ms=timings)

    def _get_time_context(self) -> dict | None:
        if self._time_context is None and self.executor:
            self._time_context = self.executor.time_range()
        return self._time_context


class MySQLExecutor:
    """只读执行器。数据库账号本身只给 SELECT 权限（第二道防线）。"""

    def __init__(self, url: str | None = None, timeout_ms: int | None = None) -> None:
        self.url = url or os.getenv(
            "DATABASE_URL",
            "mysql+pymysql://chatbi_ro:chatbi_ro@127.0.0.1:3306/ecom_chatbi?charset=utf8mb4",
        )
        self.timeout_ms = int(timeout_ms or 15000)
        self._engine = None

    @property
    def engine(self):  # noqa: ANN201
        if self._engine is None:
            from sqlalchemy import create_engine  # 延迟导入，避免评测环境强依赖

            self._engine = create_engine(self.url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        return self._engine

    def query(self, sql: str, max_rows: int) -> tuple[list[str], list[dict]]:
        with self.engine.connect() as conn:
            conn.exec_driver_sql(f"SET SESSION max_execution_time = {self.timeout_ms}")
            # 走底层游标且不传参数：SQLAlchemy 会给 PyMySQL 传空参数元组，
            # PyMySQL 随即对整条 SQL 做 % 格式化，导致字面量 '%'（如 CONCAT(x,'%')、
            # LIKE '%abc%'）报 "unsupported format character"。
            # 这里 args=None，PyMySQL 跳过格式化，SQL 原样下发。
            dbapi_conn = conn.connection.dbapi_connection
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute(sql)
                columns = [d[0] for d in (cursor.description or [])]
                rows = [dict(zip(columns, r)) for r in cursor.fetchmany(max_rows)]
            finally:
                cursor.close()
        return columns, rows

    def time_range(self) -> dict | None:
        try:
            with self.engine.connect() as conn:
                result = conn.exec_driver_sql(
                    "SELECT min_dt, max_dt FROM meta_dataset_range WHERE id = 1"
                )
                row = result.fetchone()
                return {"min_dt": str(row[0]), "max_dt": str(row[1])} if row else None
        except Exception:  # noqa: BLE001 - 元信息缺失不应阻断问答
            return None

    def health(self) -> str:
        """区分三种状态：ok / empty / error。

        之前 health 只调 time_range，异常时也返回 None，
        于是「数据库连不上」被误报成「数据集为空」——排查会被带偏。
        """
        try:
            with self.engine.connect() as conn:
                result = conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM meta_dataset_range WHERE min_dt IS NOT NULL"
                )
                row = result.fetchone()
                return "ok" if row and row[0] else "empty"
        except Exception as exc:  # noqa: BLE001
            return f"error: {type(exc).__name__}: {str(exc)[:80]}"


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
