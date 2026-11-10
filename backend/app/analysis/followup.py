"""追问改写：把「那按类目拆一下」补全成可独立执行的完整问题。

为什么改写问题、而不是改写 SQL：
- 改写 SQL 必须理解上一轮的 JOIN / 聚合结构，一旦改错会绕过语义层口径；
- 改写问题可以完整复用召回 → 生成 → 校验整条链路，口径仍然由 metrics.yaml 管；
- 代价是多一次生成调用，换来的是「不破坏口径」这个更重要的保证。

只做两类最常见、且能确定性处理的追问：
1. 追加维度（按 X 拆 / 分一下 / 按 X 看）
2. 换指标（换成 X / 改看 X）
其余情况原样返回，交给模型当新问题处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.semantic.loader import SemanticLayer

FOLLOWUP_LEADS = ("那", "再", "如果", "换成", "改看", "只看", "然后", "接着")
SPLIT_PATTERNS = [
    re.compile(r"按[「\"']?([\u4e00-\u9fffA-Za-z_]{2,12})[」\"']?(?:拆|分|看|切)"),
    re.compile(r"再按[「\"']?([\u4e00-\u9fffA-Za-z_]{2,12})[」\"']?(?:拆|分|看)"),
]
SWITCH_PATTERNS = [
    re.compile(r"(?:换成|改看|改成|换成看)[「\"']?([\u4e00-\u9fffA-Za-z_]{2,12})[」\"']?"),
]


@dataclass
class TurnContext:
    question: str
    sql: str | None = None
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)


@dataclass
class FollowupResult:
    question: str
    is_followup: bool
    kind: str = "none"          # none / split_by / switch_metric
    target: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"question": self.question, "is_followup": self.is_followup,
                "kind": self.kind, "target": self.target, "reason": self.reason}


class FollowupResolver:
    def __init__(self, semantic: SemanticLayer, max_length: int = 200) -> None:
        self.semantic = semantic
        self.max_length = max_length

    def looks_like_followup(self, question: str) -> bool:
        q = question.strip()
        if q.startswith(FOLLOWUP_LEADS):
            return True
        return any(p.search(q) for p in SPLIT_PATTERNS + SWITCH_PATTERNS)

    def resolve(self, question: str, ctx: TurnContext | None) -> FollowupResult:
        q = question.strip()
        if not q:
            return FollowupResult(question=q, is_followup=False, reason="空问题")
        if ctx is None or not ctx.question:
            return FollowupResult(question=q, is_followup=False, reason="无上一轮上下文，按新问题处理")

        for pattern in SPLIT_PATTERNS:
            m = pattern.search(q)
            if not m:
                continue
            dim = self._find_dimension(m.group(1))
            if not dim:
                # 维度字典里没有，但用户显然在追问：把原句追加到上一轮问题上，
                # 让语义层/模型自己说明「该维度不存在」，而不是留下一个不可执行的残句
                merged = f"{ctx.question.rstrip('。？?')}，{q}"
                return FollowupResult(merged[:self.max_length], True, "split_by", m.group(1),
                                      f"识别为追加维度，但「{m.group(1)}」不在维度字典中，已原样追加")
            rewritten = self._append_dimension(ctx.question, dim.name, dim.biz_name)
            if len(rewritten) > self.max_length:
                return FollowupResult(q, True, "split_by", dim.biz_name,
                                      "改写后超出长度上限，保持原问题")
            return FollowupResult(rewritten, True, "split_by", dim.biz_name,
                                  f"继承上一轮问题并追加维度「{dim.name}」")

        for pattern in SWITCH_PATTERNS:
            m = pattern.search(q)
            if not m:
                continue
            metric = self._find_metric(m.group(1))
            if not metric:
                merged = f"{ctx.question.rstrip('。？?')}，{q}"
                return FollowupResult(merged[:self.max_length], True, "switch_metric", m.group(1),
                                      f"识别为换指标，但「{m.group(1)}」不在指标字典中，已原样追加")
            rewritten = (ctx.question.rstrip("。？?") +
                         f"，但指标换成「{metric.name}」（{metric.biz_name}）")
            return FollowupResult(rewritten[:self.max_length], True, "switch_metric",
                                  metric.biz_name, f"继承上一轮问题，替换指标为「{metric.name}」")

        if q.startswith(FOLLOWUP_LEADS):
            rewritten = f"{ctx.question.rstrip('。？?')}，补充条件：{q}"
            return FollowupResult(rewritten[:self.max_length], True, "none", None,
                                  "识别为追问语气，把上一轮问题与补充条件合并")
        return FollowupResult(q, False, "none", None,
                              "未命中追问模式，按新问题处理")

    # ---------------- 解析 ----------------
    def _find_dimension(self, word: str):
        word = word.strip()
        resolved = self.semantic.resolve(word)
        if resolved and resolved[0] == "dimension":
            return self.semantic.dimension(resolved[1])
        for name, dim in self.semantic.dimensions.items():
            if word in (name, dim.biz_name) or any(word in a for a in dim.aliases):
                return dim
        return None

    def _find_metric(self, word: str):
        word = word.strip()
        resolved = self.semantic.resolve(word)
        if resolved and resolved[0] == "metric":
            return self.semantic.metric(resolved[1])
        for name, metric in self.semantic.metrics.items():
            if word in (name, metric.biz_name) or any(word in a for a in metric.aliases):
                return metric
        return None

    @staticmethod
    def _append_dimension(base: str, dim_name: str, dim_biz: str) -> str:
        base = base.rstrip("。？?")
        if dim_biz in base or dim_name in base:
            return base + "。"
        return f"{base}，按「{dim_name}」拆分。"
