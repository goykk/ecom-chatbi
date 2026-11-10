"""动态 few-shot：按问题相似度注入 3 条示例，而不是把所有示例塞进 Prompt。

为什么值得单独一个模块：
- 固定 few-shot 在电商场景会「带偏」——问留存却给了漏斗样例，模型会照抄维度；
- 这里的示例同时是评测集的来源：线上发现的新说法，补进 fewshot 再进评测集，
  形成「badcase → 示例 → 回归」的闭环，面试可讲。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import yaml

from app.nl2sql.embeddings import Embedder, cosine, get_embedder

DEFAULT_PATH = Path(__file__).resolve().parent / "fewshot.yaml"


@dataclass(frozen=True)
class Example:
    id: str
    question: str
    sql: str
    metric_refs: tuple = ()
    tags: tuple = ()


class FewShotLibrary:
    def __init__(self, examples: Sequence[Example], embedder: Embedder | None = None) -> None:
        self.examples = list(examples)
        self.embedder = embedder or get_embedder()
        self._vectors = self.embedder.encode([e.question for e in self.examples])

    @classmethod
    def load(cls, path: str | Path | None = None, embedder: Embedder | None = None) -> "FewShotLibrary":
        p = Path(path) if path else DEFAULT_PATH
        if not p.exists():
            return cls([], embedder)
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        examples = [
            Example(
                id=str(item.get("id", f"f{i:03d}")),
                question=item["question"],
                sql=item["sql"].strip(),
                metric_refs=tuple(item.get("metric_refs", [])),
                tags=tuple(item.get("tags", [])),
            )
            for i, item in enumerate(doc.get("examples", []), start=1)
        ]
        return cls(examples, embedder)

    def select(self, question: str, k: int = 3) -> list[Example]:
        if not self.examples:
            return []
        q_vec = self.embedder.encode([question])[0]
        q_lower = question.lower()
        scored = []
        for ex, vec in zip(self.examples, self._vectors):
            score = cosine(q_vec, vec)
            if any(tag.lower() in q_lower for tag in ex.tags):
                score += 0.2
            if any(ref.split("_")[0] in q_lower for ref in ex.metric_refs):
                score += 0.1
            scored.append((score, ex))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ex for _, ex in scored[:k]]

    def render(self, examples: Sequence[Example]) -> str:
        if not examples:
            return "（无示例）"
        blocks = []
        for i, ex in enumerate(examples, start=1):
            blocks.append(f"示例{i}\n问题：{ex.question}\nSQL:\n{ex.sql}")
        return "\n\n".join(blocks)
