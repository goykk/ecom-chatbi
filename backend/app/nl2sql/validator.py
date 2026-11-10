"""SQL 校验沙箱：语法、只读、表白名单、自动补 LIMIT。

对应 metrics.yaml 的 guardrails 段，是「Prompt 约束」之外的硬约束：
Prompt 可能被绕过，校验不会。

四道检查：
1. 能否被 sqlglot 解析（语法）
2. 是否只有一条语句、根节点是 SELECT / UNION（防 DML/DDL 注入）
3. 所有物理表是否在白名单（防 info_schema / 系统库探测）
4. 是否含危险函数与文件写入（SLEEP / LOAD_FILE / INTO OUTFILE）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

try:
    import sqlglot
    from sqlglot import exp
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("缺少 sqlglot，请执行 pip install sqlglot") from exc

from app.semantic.loader import SemanticLayer

FORBIDDEN_PATTERNS = [
    ("INTO OUTFILE", "禁止写文件"),
    ("INTO DUMPFILE", "禁止写文件"),
    ("LOAD_FILE", "禁止读取服务器文件"),
    ("SLEEP(", "禁止延时函数"),
    ("BENCHMARK(", "禁止压测函数"),
    ("INFORMATION_SCHEMA.", "禁止访问系统库"),
    ("PERFORMANCE_SCHEMA.", "禁止访问系统库"),
    ("MYSQL.", "禁止访问系统库"),
]


@dataclass
class ValidationResult:
    ok: bool
    sql: str
    tables: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limit_added: bool = False

    @property
    def reason(self) -> str:
        return "；".join(self.errors)


class SQLValidator:
    def __init__(self, semantic: SemanticLayer, default_limit: int | None = None,
                 max_limit: int | None = None) -> None:
        guard = semantic.guardrails or {}
        self.whitelist = set(guard.get("table_whitelist", [])) or None
        self.default_limit = int(default_limit or guard.get("auto_limit", 1000))
        self.max_limit = int(max_limit or max(self.default_limit, 5000))
        self.forbidden_keywords = {k.upper() for k in guard.get("forbidden_keywords", [])}

    def validate(self, sql: str) -> ValidationResult:
        raw = (sql or "").strip().rstrip(";").strip()
        errors: list[str] = []
        warnings: list[str] = []

        if not raw:
            return ValidationResult(False, raw, errors=["SQL 为空"])

        upper = raw.upper()
        for token, msg in FORBIDDEN_PATTERNS:
            if token in upper:
                errors.append(f"{msg}：{token}")
        for kw in self.forbidden_keywords:
            if re.search(rf"\b{re.escape(kw)}\b", upper):
                errors.append(f"包含禁用关键字 {kw}")

        try:
            parsed = sqlglot.parse(raw, read="mysql")
        except Exception as exc:  # sqlglot.errors.ParseError
            return ValidationResult(False, raw, errors=[f"语法错误：{exc}"])

        parsed = [p for p in parsed if p is not None]
        if len(parsed) != 1:
            return ValidationResult(False, raw, errors=[f"只允许单条语句，实际 {len(parsed)} 条"])

        query = parsed[0]
        if not isinstance(query, (exp.Select, exp.Union, exp.Subquery)):
            return ValidationResult(False, raw, errors=[
                f"只允许 SELECT / UNION，实际为 {type(query).__name__}"
            ])

        cte_names = {cte.alias_or_name.lower() for cte in query.find_all(exp.CTE)}
        tables: list[str] = []
        for table in query.find_all(exp.Table):
            name = (table.name or "").lower()
            if not name:
                continue
            if table.db and table.db.lower() not in ("ecom_chatbi", ""):
                errors.append(f"禁止跨库访问：{table.db}.{name}")
            if name in cte_names:
                continue
            tables.append(name)
            if self.whitelist and name not in self.whitelist:
                errors.append(f"表 {name} 不在白名单")

        if not tables:
            errors.append("未引用任何物理表")

        normalized, limit_added = self._apply_limit(query)
        normalized_sql = normalized.sql(dialect="mysql")

        if "SELECT *" in upper:
            warnings.append("使用了 SELECT *，建议显式列出字段")

        return ValidationResult(
            ok=not errors,
            sql=normalized_sql,
            tables=sorted(set(tables)),
            errors=errors,
            warnings=warnings,
            limit_added=limit_added,
        )

    # ---------------- LIMIT ----------------
    def _apply_limit(self, query: exp.Expression) -> tuple[exp.Expression, bool]:
        limit_node = query.args.get("limit")
        if limit_node is not None:
            try:
                current = int(limit_node.expression.name)
            except (AttributeError, ValueError):
                current = self.default_limit
            if current > self.max_limit:
                return query.limit(self.max_limit), True
            return query, False

        try:
            return query.limit(self.default_limit), True
        except Exception:  # noqa: BLE001 - 某些结构不支持 limit 构造
            sql = query.sql(dialect="mysql") + f"\nLIMIT {self.default_limit}"
            return sqlglot.parse_one(sql, read="mysql"), True
