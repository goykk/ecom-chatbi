"""重复评测：同一数据集跑 N 次，报告均值、标准差与不稳定用例。

为什么需要它：
单次 94.4% 只是一个点估计，temperature=0 也不等于完全确定
（服务端批处理、浮点归约都可能带来波动）。
面试被追问「这个数字稳定吗」时，需要给出区间与抖动来源，而不是一句「我跑了一次」。

用法：
    python -m eval.repeat --repeat 3
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

from app.nl2sql.pipeline import MySQLExecutor, NL2SQLPipeline
from app.semantic.loader import SemanticLayer
from eval.metrics import summarize
from eval.runner import DEFAULT_DATASET, DEFAULT_OUTDIR, Evaluator, load_cases, _pct


def stability(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "std": None, "min": None, "max": None}
    return {
        "mean": round(statistics.mean(vals), 4),
        "std": round(statistics.pstdev(vals), 4),
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="重复评测并统计稳定性")
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()

    semantic = SemanticLayer.load()
    cases = load_cases(args.dataset)
    executor = MySQLExecutor()
    if not executor.time_range():
        print("无法读取 meta_dataset_range，请先执行 warehouse/run_all.sql")
        return 2

    pipeline = NL2SQLPipeline(semantic=semantic, executor=executor)
    evaluator = Evaluator(pipeline, executor)

    rounds: list[dict] = []
    per_case: dict[str, list[bool]] = {c.id: [] for c in cases}

    for i in range(args.repeat):
        results = evaluator.run(cases)
        stats = summarize(results)
        rounds.append(stats)
        for r in results:
            per_case[r.id].append(r.matched)
        print(f"round {i + 1}/{args.repeat}: "
              f"EA={_pct(stats['execution_accuracy'])} "
              f"refuse={_pct(stats['refusal_accuracy'])} "
              f"overall={_pct(stats['overall_accuracy'])}")

    ea = stability([r["execution_accuracy"] for r in rounds])
    ra = stability([r["refusal_accuracy"] for r in rounds])
    oa = stability([r["overall_accuracy"] for r in rounds])
    lat = stability([r["avg_latency_ms"] for r in rounds])

    flaky = {cid: v for cid, v in per_case.items() if len(set(v)) > 1}
    always_fail = {cid: v for cid, v in per_case.items() if not any(v)}
    always_pass = sum(1 for v in per_case.values() if all(v))

    lines = [
        "# 稳定性评测（重复运行）",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 数据集：`{args.dataset.name}`（{len(cases)} 条）",
        f"- 重复次数：{args.repeat}",
        f"- 模型：{pipeline.llm.config.model}",
        "",
        "## 逐轮结果",
        "",
        "| 轮次 | 执行准确率 | 拒答准确率 | 总体 | 平均延迟(ms) |",
        "|---|---|---|---|---|",
    ]
    for i, s in enumerate(rounds, 1):
        lines.append(f"| {i} | {_pct(s['execution_accuracy'])} | "
                     f"{_pct(s['refusal_accuracy'])} | {_pct(s['overall_accuracy'])} | "
                     f"{s['avg_latency_ms']} |")

    def fmt(st: dict) -> str:
        if st["mean"] is None:
            return "—"
        return (f"{st['mean'] * 100:.1f}% ± {st['std'] * 100:.1f}pp"
                f"（{st['min'] * 100:.1f}% ~ {st['max'] * 100:.1f}%）")

    lines += [
        "",
        "## 聚合",
        "",
        "| 指标 | 均值 ± 标准差（区间） |",
        "|---|---|",
        f"| 执行准确率 | {fmt(ea)} |",
        f"| 拒答准确率 | {fmt(ra)} |",
        f"| 总体准确率 | {fmt(oa)} |",
        f"| 平均延迟 | {lat['mean']} ms（{lat['min']} ~ {lat['max']}） |",
        "",
        f"- 稳定通过：{always_pass} / {len(cases)} 条",
        f"- 稳定失败：{len(always_fail)} 条" + (f"（{', '.join(always_fail)}）" if always_fail else ""),
        f"- 不稳定（同一用例跨轮结果不一致）：{len(flaky)} 条"
        + (f"（{', '.join(f'{k}={v}' for k, v in flaky.items())}）" if flaky else ""),
        "",
        "> 结论口径：以「均值 ± 标准差」报告，不单取某一次结果；不稳定用例单独列出，避免被平均值掩盖。",
        "",
    ]
    report = "\n".join(lines)

    args.outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = args.outdir / f"repeat_{stamp}.md"
    path.write_text(report, encoding="utf-8")
    (args.outdir / f"repeat_{stamp}.json").write_text(json.dumps({
        "rounds": rounds,
        "aggregate": {"execution_accuracy": ea, "refusal_accuracy": ra,
                      "overall_accuracy": oa, "latency_ms": lat},
        "per_case": per_case,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n执行准确率 {fmt(ea)}")
    print(f"稳定通过 {always_pass}/{len(cases)}，不稳定 {len(flaky)} 条")
    print(f"报告已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
