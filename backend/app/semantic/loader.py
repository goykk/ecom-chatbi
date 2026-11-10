"""语义层运行时加载器。

把 metrics.yaml / dimensions.yaml 读成程序可直接使用的对象，
并提供 Prompt 拼装所需的文本块。

设计原则：
- 这里是「口径唯一可信源」，Prompt、校验、评测都从这里取，不在代码里另写一份；
- 加载即校验：指标引用了不存在的维度、派生指标引用了不存在的指标，启动时就报出来。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

DEFAULT_DIR = Path(__file__).resolve().parent


def _tuple(v: Any) -> tuple:
    return tuple(v) if v else ()


@dataclass(frozen=True)
class Metric:
    biz_name: str
    name: str
    description: str = ""
    define_type: str = "ATOMIC"          # ATOMIC / DERIVED
    define_by: str = "MEASURE"           # MEASURE / FIELD / METRIC
    expr: str | None = None
    formula: str | None = None
    source_table: str | None = None
    grain: tuple = ()
    dimensions: tuple = ()
    filters: tuple = ()
    aliases: tuple = ()
    unit: str | None = None
    status: str = "DRAFT"
    note: str | None = None
    caveat: str | None = None
    params: dict = field(default_factory=dict)

    @property
    def is_derived(self) -> bool:
        return self.define_type.upper() == "DERIVED"

    def prompt_line(self) -> str:
        parts = [f"{self.biz_name}（{self.name}）", f"类型={self.define_type}/{self.define_by}"]
        if self.formula:
            parts.append(f"公式={self.formula}")
        if self.expr:
            parts.append(f"表达式={self.expr}")
        if self.source_table:
            parts.append(f"来源表={self.source_table}")
        if self.grain:
            parts.append(f"粒度={'/'.join(self.grain)}")
        if self.dimensions:
            parts.append(f"可用维度={'/'.join(self.dimensions)}")
        if self.filters:
            parts.append(f"过滤条件={'；'.join(self.filters)}")
        if self.aliases:
            parts.append(f"别名={'、'.join(self.aliases)}")
        if self.caveat:
            parts.append(f"限制={self.caveat}")
        if self.note:
            parts.append(f"说明={self.note}")
        return "- " + "；".join(parts)


@dataclass(frozen=True)
class Dimension:
    biz_name: str
    name: str
    type: str                            # DATE / ID / CATEGORY
    semantic_type: str
    description: str = ""
    expr: str | None = None
    source_table: str | None = None
    aliases: tuple = ()
    synonyms: tuple = ()
    value_maps: tuple = ()
    exposure: str = "group_by_allowed"   # group_by_allowed / filter_only / hidden
    note: str | None = None

    def prompt_line(self) -> str:
        parts = [f"{self.biz_name}（{self.name}）", f"{self.type}/{self.semantic_type}"]
        if self.expr:
            parts.append(f"表达式={self.expr}")
        if self.source_table:
            parts.append(f"来源表={self.source_table}")
        if self.aliases or self.synonyms:
            parts.append("别名=" + "、".join(_tuple(self.aliases) + _tuple(self.synonyms)))
        if self.value_maps:
            mapping = []
            for vm in self.value_maps:
                labels = "|".join(vm.get("labels", []))
                mapping.append(f"{vm.get('value')}={labels}")
            parts.append("枚举映射: " + "，".join(mapping))
        if self.exposure != "group_by_allowed":
            parts.append(f"使用限制={self.exposure}")
        return "- " + "；".join(parts)


@dataclass
class SemanticLayer:
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    dataset: dict
    business_rules: dict
    terms: list
    guardrails: dict
    context_policy: dict
    joins: list
    no_answer: list
    unavailable: list
    time_resolution: dict
    virtual_grain: dict
    alias_index: dict
    source_dir: Path

    # ---------------- 加载 ----------------
    @classmethod
    def load(cls, base_dir: str | Path | None = None) -> "SemanticLayer":
        base = Path(base_dir) if base_dir else Path(os.getenv("SEMANTIC_DIR", DEFAULT_DIR))
        metrics_doc = _read_yaml(base / "metrics.yaml")
        dims_doc = _read_yaml(base / "dimensions.yaml")

        metrics = {
            m["biz_name"]: Metric(
                biz_name=m["biz_name"],
                name=m.get("name", m["biz_name"]),
                description=m.get("description", ""),
                define_type=m.get("define_type", "ATOMIC"),
                define_by=m.get("define_by", "MEASURE"),
                expr=m.get("expr"),
                formula=m.get("formula"),
                source_table=m.get("source_table"),
                grain=_tuple(m.get("grain")),
                dimensions=_tuple(m.get("dimensions")),
                filters=_tuple(m.get("filters")),
                aliases=_tuple(m.get("aliases")),
                unit=m.get("unit"),
                status=m.get("status", "DRAFT"),
                note=m.get("note"),
                caveat=m.get("caveat"),
                params=m.get("params") or {},
            )
            for m in metrics_doc.get("metrics", [])
        }
        dimensions = {
            d["biz_name"]: Dimension(
                biz_name=d["biz_name"],
                name=d.get("name", d["biz_name"]),
                type=d.get("type", "CATEGORY"),
                semantic_type=d.get("semantic_type", "category"),
                description=d.get("description", ""),
                expr=d.get("expr"),
                source_table=d.get("source_table"),
                aliases=_tuple(d.get("aliases")),
                synonyms=_tuple(d.get("synonyms")),
                value_maps=tuple(d.get("value_maps", [])),
                exposure=d.get("exposure", "group_by_allowed"),
                note=d.get("note"),
            )
            for d in dims_doc.get("dimensions", [])
        }

        virtual = {g["biz_name"]: g for g in dims_doc.get("virtual_grain", [])}

        alias_index: dict[str, tuple[str, str]] = {}
        for m in metrics.values():
            for word in (m.name, m.biz_name, *m.aliases):
                alias_index.setdefault(word.lower(), ("metric", m.biz_name))
        for d in dimensions.values():
            for word in (d.name, d.biz_name, *d.aliases, *d.synonyms):
                alias_index.setdefault(word.lower(), ("dimension", d.biz_name))

        layer = cls(
            metrics=metrics,
            dimensions=dimensions,
            dataset=metrics_doc.get("dataset", {}),
            business_rules=metrics_doc.get("business_rules", {}),
            terms=metrics_doc.get("terms", []),
            guardrails=metrics_doc.get("guardrails", {}),
            context_policy=metrics_doc.get("context_policy", {}),
            joins=dims_doc.get("joins", []),
            no_answer=metrics_doc.get("no_answer", []),
            unavailable=dims_doc.get("unavailable", []),
            time_resolution=dims_doc.get("time_resolution", {}),
            virtual_grain=virtual,
            alias_index=alias_index,
            source_dir=base,
        )
        for problem in layer.validate():
            # 加载阶段只告警不中断：允许 DRAFT 指标暂时引用未建字段
            print(f"[semantic] WARN: {problem}")
        return layer

    # ---------------- 校验 ----------------
    def validate(self) -> list[str]:
        problems: list[str] = []
        known_metric_names = set(self.metrics)
        for m in self.metrics.values():
            for dim in m.dimensions:
                if dim not in self.dimensions and dim not in self.virtual_grain:
                    problems.append(f"指标 {m.biz_name} 引用了未定义维度 {dim}")
            if m.is_derived and m.formula:
                refs = set(re.findall(r"[a-z][a-z0-9_]*", m.formula))
                unknown = {r for r in refs if r not in known_metric_names
                           and not r.startswith(("cr_", "n_"))}
                if unknown:
                    problems.append(f"派生指标 {m.biz_name} 引用了未知指标 {sorted(unknown)}")
                def_by = {x.biz_name for x in self.metrics.values() if x.define_by == "METRIC"}
                if m.biz_name not in def_by:
                    problems.append(f"派生指标 {m.biz_name} 的 define_by 应为 METRIC")
        whitelist = set(self.guardrails.get("table_whitelist", []))
        for m in self.metrics.values():
            if m.source_table and whitelist and m.source_table not in whitelist:
                problems.append(f"指标 {m.biz_name} 的来源表 {m.source_table} 不在白名单")
        for d in self.dimensions.values():
            if d.source_table and whitelist and d.source_table not in whitelist:
                problems.append(f"维度 {d.biz_name} 的来源表 {d.source_table} 不在白名单")
        return problems

    # ---------------- 查询 ----------------
    def metric(self, biz_name: str) -> Metric | None:
        return self.metrics.get(biz_name)

    def dimension(self, biz_name: str) -> Dimension | None:
        return self.dimensions.get(biz_name)

    def resolve(self, word: str) -> tuple[str, str] | None:
        return self.alias_index.get(word.strip().lower())

    def metric_names(self) -> list[str]:
        return list(self.metrics)

    # ---------------- Prompt 文本块 ----------------
    def metrics_block(self, names: Iterable[str]) -> str:
        lines = [self.metrics[n].prompt_line() for n in names if n in self.metrics]
        return "\n".join(lines) if lines else "（无匹配指标）"

    def dimensions_block(self, names: Iterable[str]) -> str:
        lines = []
        for n in names:
            if n in self.dimensions:
                lines.append(self.dimensions[n].prompt_line())
        return "\n".join(lines) if lines else "（无匹配维度）"

    def table_cards(self) -> dict[str, str]:
        """按表聚合维度信息，生成 DDL 级说明，供 schema 召回与 Prompt 使用。"""
        tables: dict[str, list[Dimension]] = {}
        for d in self.dimensions.values():
            if d.source_table:
                tables.setdefault(d.source_table, []).append(d)
        flat = _flatten_tables(self.dataset.get("tables", {}))
        for t in flat:
            tables.setdefault(t, [])

        cards: dict[str, str] = {}
        for table, dims in tables.items():
            if dims:
                cols = "\n".join(f"  - {d.prompt_line()[2:]}" for d in dims)
                cards[table] = f"表 {table}\n{cols}"
            else:
                cards[table] = f"表 {table}（ADS 汇总表，字段以 ads 层定义为准）"
        return cards

    def joins_block(self) -> str:
        if not self.joins:
            return ""
        lines = []
        for j in self.joins:
            # YAML 1.1 会把未加引号的 on 解析成布尔 True，这里做兼容
            on = j.get("on") or j.get(True) or ""
            left_field = on.split("=")[0].strip().split(".")[-1]
            lines.append(f"- {j['left']}.{left_field} {j.get('type', '')} 关联 {j['right']}")
        return "\n".join(lines)

    def time_block(self) -> str:
        tr = self.time_resolution
        if not tr:
            return ""
        rules = []
        for r in tr.get("rules", []):
            rules.append(f"- {'/'.join(r.get('phrase', []))} -> {r.get('resolve')}")
        return (f"时间基准：{tr.get('reference_point', '')}\n" + "\n".join(rules) +
                f"\n硬约束：{tr.get('guardrail', '')}")

    def refusal_block(self) -> str:
        items = self.no_answer + self.unavailable
        if not items:
            return ""
        lines = []
        for i in items:
            lines.append(f"- {i.get('name') or i.get('topic')}：{i.get('reason', '')}"
                         f"（示例：{'、'.join(i.get('examples', i.get('aliases', []))[:3])}）")
        return "\n".join(lines)

    def refusal_reason(self, question: str) -> str | None:
        """确定性拒答：命中已知不存在的维度就直接拒，不等模型「自由发挥」。

        这是一条兜底护栏，和 Prompt 里的拒答清单形成双保险：
        测评集 no_answer 类别必须 100% 拒答，不能依赖模型每次都听话。
        """
        q = question.lower()
        for item in self.unavailable:
            for alias in item.get("aliases", []):
                if len(alias) < 2:
                    continue
                if alias.isascii():
                    if re.search(rf"\b{re.escape(alias.lower())}\b", q):
                        return self._refusal_text(item)
                elif alias.lower() in q:
                    return self._refusal_text(item)
        return None

    @staticmethod
    def _refusal_text(item: dict) -> str:
        text = f"{item.get('name')}：{item.get('reason', '')}"
        if item.get("fallback"):
            text += f"；{item['fallback']}"
        return text


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"语义层文件缺失：{path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _flatten_tables(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_flatten_tables(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_flatten_tables(v))
    return out
