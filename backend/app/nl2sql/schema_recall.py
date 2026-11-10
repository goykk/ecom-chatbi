"""Schema 召回：把自然语言问题映射到「用哪些表 / 哪些指标 / 哪些维度 / 哪些术语」。

这是 NL2SQL 准确率的第一道闸门：
把整库 DDL 塞给模型会稀释注意力，也容易让模型自己编字段；
只召回相关的那几块，SKILL 文档里的 Schema Linking 说的就是这一步。

召回结果同时被 generator（拼 Prompt）和 validator（判断表是否合法）使用。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from app.nl2sql.embeddings import Embedder, cosine, get_embedder
from app.semantic.loader import SemanticLayer


@dataclass
class RecallResult:
    tables: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    terms: list[dict] = field(default_factory=list)
    value_hits: list[dict] = field(default_factory=list)   # 命中的枚举映射，供 Prompt 显式提示
    scores: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "tables": self.tables,
            "metrics": self.metrics,
            "dimensions": self.dimensions,
            "terms": [t.get("name") for t in self.terms],
            "value_hits": self.value_hits,
        }


class SchemaRetriever:
    def __init__(self, semantic: SemanticLayer, embedder: Embedder | None = None) -> None:
        self.semantic = semantic
        self.embedder = embedder or get_embedder()
        self._docs: list[tuple[str, str, str]] = []      # (kind, key, text)
        self._vectors: list[list[float]] = []
        self._build_index()

    # ---------------- 建索引 ----------------
    def _build_index(self) -> None:
        semantic = self.semantic
        for table, card in semantic.table_cards().items():
            self._docs.append(("table", table, card))
        for m in semantic.metrics.values():
            text = " | ".join(filter(None, [
                m.name, m.biz_name, m.description,
                "别名：" + "、".join(m.aliases) if m.aliases else "",
                m.formula or "", m.expr or "", m.caveat or "",
            ]))
            self._docs.append(("metric", m.biz_name, text))
        for d in semantic.dimensions.values():
            labels = [lab for vm in d.value_maps for lab in vm.get("labels", [])]
            text = " | ".join(filter(None, [
                d.name, d.biz_name, d.description,
                "、".join(d.aliases), "、".join(d.synonyms),
                "枚举：" + "、".join(labels) if labels else "",
            ]))
            self._docs.append(("dimension", d.biz_name, text))
        for term in semantic.terms:
            text = " | ".join(filter(None, [
                term.get("name", ""), term.get("description", ""),
                "、".join(term.get("aliases", [])),
                str(term.get("disambiguation", "")),
            ]))
            self._docs.append(("term", term.get("name", ""), text))

        self._vectors = self.embedder.encode([t for _, _, t in self._docs])
        self._bm25 = _BM25([t for _, _, t in self._docs])

    # ---------------- 召回 ----------------
    def recall(self, question: str) -> RecallResult:
        q = question.strip()
        q_lower = q.lower()
        q_vec = self.embedder.encode([q])[0]

        # 三个分量一起决定相关性：向量相似度、BM25 归一值、字面命中
        scored: dict[str, list[tuple[float, str, float]]] = {
            "table": [], "metric": [], "dimension": [], "term": []}
        bm25_raw = self._bm25.scores(q)
        bm25_max = max(bm25_raw) if bm25_raw else 0.0
        for i, ((kind, key, text), vec) in enumerate(zip(self._docs, self._vectors)):
            # 向量负责语义相近，BM25 负责「用户」这类高频词的降权，二者互补
            bm25_norm = bm25_raw[i] / bm25_max if bm25_max > 0 else 0.0
            score = 0.55 * cosine(q_vec, vec) + 0.45 * bm25_norm
            if key.lower() in q_lower or _contains_alias(text, q_lower):
                score += 0.25          # 字面命中直接加权重，避免哈希向量漏掉专业词
            # 指标/维度必须与问题有「非停用词」的字面交集，否则纯靠「用户」也会沾边
            if kind in ("metric", "dimension") and not _specific_overlap(q, text):
                continue
            scored[kind].append((score, key, bm25_norm))

        policy = self.semantic.context_policy or {}
        max_metrics = int(policy.get("metric_inject_max", 8))
        max_dims = int(policy.get("dimensions_max", 12))

        # BM25 下限用来滤掉「只共享 用户/数据 这种高频词」的假相关
        metrics = self._take(scored["metric"], max_metrics,
                             min_score=0.18, rel=0.40, bm25_min=0.25)
        dimensions = self._take(scored["dimension"], max_dims,
                                min_score=0.18, rel=0.40, bm25_min=0.25)
        dimensions = [d for d in dimensions
                      if self._injectable(self.semantic.dimensions[d])]
        terms = self._take(scored["term"], 3, min_score=0.25, rel=1.0, bm25_min=0.20)
        terms = [t for name in terms
                 for t in self.semantic.terms if t.get("name") == name]

        # 术语里绑定的指标/维度也要带进上下文，这是「口径对齐」的关键
        for term in terms:
            for m in term.get("related_metrics", [])[:2]:
                if m in self.semantic.metrics and m not in metrics:
                    metrics.append(m)
            for d in term.get("related_dimensions", [])[:2]:
                if d in self.semantic.dimensions and d not in dimensions:
                    dimensions.append(d)

        # 常驻维度 + 指标自身要求的维度
        for d in policy.get("always_include", []):
            if d in self.semantic.dimensions and d not in dimensions:
                dimensions.append(d)
        for name in list(metrics):
            m = self.semantic.metric(name)
            if not m:
                continue
            for d in m.dimensions:
                if d in self.semantic.dimensions and d not in dimensions:
                    dimensions.append(d)

        # 枚举值命中（如“晚上8点”“双11”），必须显式告诉模型映射到什么值
        value_hits = self._value_hits(q_lower, dimensions)

        # 表只从指标/维度的来源推导，不做独立的自由文本表格召回——
        # 表格说明文本太长，容易把不相关的 ads 表带进上下文。
        table_set: set[str] = set()
        for name in metrics:
            m = self.semantic.metric(name)
            if m and m.source_table:
                table_set.add(m.source_table)
        for name in dimensions:
            d = self.semantic.dimension(name)
            if d and d.source_table:
                table_set.add(d.source_table)
        table_set |= self._join_closure(table_set)

        if not table_set:
            fact = (self.semantic.dataset.get("tables") or {}).get("fact")
            if fact:
                table_set.add(fact)

        return RecallResult(
            tables=sorted(table_set),
            metrics=metrics,
            dimensions=dimensions,
            terms=terms,
            value_hits=value_hits,
            scores={f"metric:{m}": s for s, m, _ in scored["metric"][:max_metrics]},
        )

    # ---------------- 工具 ----------------
    @staticmethod
    def _injectable(dimension) -> bool:
        """哪些维度值得进 Prompt：
        - hidden：完全不给（user_id）
        - filter_only + ID：只用于过滤的实体键，不给（session_id / item_id 视情况）
        - 其余都注入；behavior_type 是 filter_only 的枚举，必须给
        """
        if dimension.exposure == "hidden":
            return False
        if dimension.exposure == "filter_only" and dimension.semantic_type == "id":
            return False
        return True

    @staticmethod
    def _take(pairs: list[tuple[float, str, float]], k: int,
              min_score: float = 0.0, rel: float = 0.6,
              bm25_min: float = 0.0) -> list[str]:
        """相对阈值 + BM25 下限双重过滤。

        纯绝对阈值在哈希向量下会放进一堆「沾边」指标；
        相对阈值压掉长尾，BM25 下限再滤掉只共享高频词（用户/数据）的假相关。
        """
        ranked = sorted(pairs, key=lambda x: x[0], reverse=True)
        if not ranked:
            return []
        floor = max(min_score, ranked[0][0] * rel)
        return [key for score, key, bm25 in ranked[:k] if score >= floor and bm25 >= bm25_min]

    def _value_hits(self, q_lower: str, dimensions: Iterable[str]) -> list[dict]:
        hits = []
        for name in dimensions:
            d = self.semantic.dimension(name)
            if not d:
                continue
            for vm in d.value_maps:
                for label in vm.get("labels", []):
                    # 单字标签（如“买”）会命中“没买”这类否定表达，只认 2 字以上
                    if len(label) >= 2 and label.lower() in q_lower:
                        hits.append({"dimension": d.biz_name, "value": vm.get("value"),
                                     "label": label})
                        break
        return hits

    def _join_closure(self, tables: set[str]) -> set[str]:
        """补上被 join 的维表：例如选了事实表，类目名展示需要 dim_category。"""
        extra: set[str] = set()
        for j in self.semantic.joins:
            left, right = j.get("left"), j.get("right")
            # 只补维表，不把 ads 表互相牵连进来（否则上下文迅速膨胀）
            if left in tables and right and right.startswith("dim_"):
                extra.add(right)
            if right in tables and left and left.startswith("dim_"):
                extra.add(left)
        return extra - tables


def _contains_alias(text: str, q_lower: str) -> bool:
    """粗略的别名字面命中：取文本中的中文/英文片段做子串判断。"""
    for token in re.split(r"[ |、，,;；]+", text):
        token = token.strip().lower()
        if len(token) >= 2 and token in q_lower:
            return True
    return False


_TEXT_TOKEN = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")

# 这些词几乎出现在每个指标/维度描述里，不能作为「相关」的证据
_STOP_TERMS = {
    "用户", "人数", "多少", "占比", "分布", "数据", "时间", "日期",
    "查询", "分析", "统计", "每天", "当日", "现在", "情况", "一下",
    "什么", "怎么", "哪些", "哪个", "是否", "一个", "可以", "需要",
}


# 中文虚词：含这些字的 2-gram 不具备区分度（「的用」「户有」「有多」都是噪声）
_FUNC_CHARS = set("的了有是在吗呢但和与或就都也很把被给对从向以为这那我你他它")


def _informative(text: str) -> set[str]:
    """只保留有区分度的词：英文词 + 不含虚词的汉字 2-gram。"""
    out: set[str] = set()
    for term in _terms(text):
        if term.isascii():
            out.add(term)
        elif len(term) >= 2 and not any(ch in _FUNC_CHARS for ch in term):
            out.add(term)
    return out - _STOP_TERMS


def _specific_overlap(question: str, text: str) -> bool:
    return bool(_informative(question) & _informative(text))


def _terms(text: str) -> list[str]:
    """英文按词、中文按 2-gram 切分，兼顾专有词与中文短语。"""
    out: list[str] = []
    for chunk in _TEXT_TOKEN.findall(text.lower()):
        if chunk.isascii() or len(chunk) == 1:
            out.append(chunk)
        else:
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return out


class _BM25:
    """极简 BM25：用来压制「用户」「数据」这类到处都出现的高频词。"""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = [Counter(_terms(d)) for d in docs]
        self.lengths = [sum(c.values()) for c in self.docs]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter = Counter()
        for c in self.docs:
            df.update(c.keys())
        n = len(docs) or 1
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str) -> list[float]:
        q_terms = Counter(_terms(query))
        out: list[float] = []
        for doc, length in zip(self.docs, self.lengths):
            score = 0.0
            for term, _ in q_terms.items():
                tf = doc.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1))
                score += self.idf.get(term, 0.0) * (tf * (self.k1 + 1)) / denom
            out.append(score)
        return out
