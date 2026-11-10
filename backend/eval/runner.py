"""评测入口：跑数据集 → 执行准确率 → 输出 Markdown 报告。

用法（在 backend/ 目录下）：
    # 1) 不需要 LLM、不需要数据库：先验证数据集与 gold SQL 是否合法
    python -m eval.runner --validate-only

    # 2) 只生成 SQL，人工核对 gold（还没接数据库时用）
    python -m eval.runner --no-execute --outdir eval/reports

    # 3) 完整评测：需要 LLM_API_KEY + DATABASE_URL
    python -m eval.runner --dataset eval/dataset/seed_20.jsonl

报告写在 eval/reports/eval_<时间戳>.md，同时更新 latest.md；
历史报告不要删——「首次 63% → 最终 91%」的曲线就靠这些文件证明。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持 python eval/runner.py 直接执行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.nl2sql.pipeline import MySQLExecutor, NL2SQLPipeline  # noqa: E402
from app.nl2sql.validator import SQLValidator  # noqa: E402
from app.semantic.loader import SemanticLayer  # noqa: E402
from eval.metrics import result_sets_match, summarize  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "dataset" / "seed.jsonl"
DEFAULT_OUTDIR = EVAL_DIR / "reports"


@dataclass
class Case:
    id: str
    question: str
    gold_sql: str | None = None
    expect: str = "SQL"               # SQL / REFUSE
    category: str = "unknown"
    tags: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class CaseResult:
    id: str
    question: str
    category: str
    expect: str
    predicted_action: str
    predicted_sql: str | None = None
    gold_sql: str | None = None
    matched: bool = False
    reason: str = ""
    latency_ms: float = 0.0
    attempts: int = 0
    gold_error: str | None = None


def load_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name}:{lineno} JSON 解析失败：{exc}") from exc
            case_id = str(obj.get("id") or f"line{lineno}")
            if case_id in seen:
                raise SystemExit(f"{path.name}:{lineno} id 重复：{case_id}")
            seen.add(case_id)
            cases.append(Case(
                id=case_id,
                question=obj["question"],
                gold_sql=obj.get("gold_sql"),
                expect=str(obj.get("expect") or "SQL").upper(),
                category=obj.get("category", "unknown"),
                tags=list(obj.get("tags", [])),
                note=obj.get("note", ""),
            ))
    return cases


class Evaluator:
    def __init__(self, pipeline: NL2SQLPipeline, executor: Any,
                 tolerance: int = 4, fewshot_k: int | None = None) -> None:
        self.pipeline = pipeline
        self.executor = executor
        self.tolerance = tolerance
        self.fewshot_k = fewshot_k

    def run(self, cases: list[Case]) -> list[CaseResult]:
        return [self.run_case(case) for case in cases]

    def run_case(self, case: Case) -> CaseResult:
        start = time.perf_counter()
        kwargs = {} if self.fewshot_k is None else {"fewshot_k": self.fewshot_k}
        result = self.pipeline.ask(case.question, execute=self.executor is not None, **kwargs)
        latency = round((time.perf_counter() - start) * 1000, 1)

        item = CaseResult(
            id=case.id, question=case.question, category=case.category,
            expect=case.expect, predicted_action=result.action,
            predicted_sql=result.sql, gold_sql=case.gold_sql,
            latency_ms=latency, attempts=result.attempts, reason=result.reason or "",
        )

        if case.expect == "REFUSE":
            item.matched = result.action == "refuse"
            if not item.matched:
                item.reason = f"期望拒答，实际返回 {result.action}"
            return item

        if result.action != "sql" or not result.sql:
            item.reason = result.reason or f"未生成 SQL（action={result.action}）"
            return item

        if self.executor is None:
            item.matched = False
            item.reason = "未连接数据库，仅生成 SQL（--no-execute）"
            return item

        # gold 必须先跑通；gold 报错是数据集问题，不能算模型的问题
        try:
            gold_columns, gold_rows = self.executor.query(case.gold_sql or "", 1000)
        except Exception as exc:  # noqa: BLE001
            item.gold_error = str(exc)
            item.reason = f"gold SQL 执行失败：{exc}"
            return item

        ok, detail = result_sets_match(
            gold_columns, gold_rows, result.columns, result.rows, self.tolerance
        )
        item.matched = ok
        item.reason = detail
        return item


def validate_only(cases: list[Case], semantic: SemanticLayer) -> int:
    """不依赖 LLM / 数据库的静态校验：数据集结构 + gold SQL 合法性。"""
    validator = SQLValidator(semantic)
    problems: list[str] = []
    for case in cases:
        if case.expect == "REFUSE":
            if case.gold_sql:
                problems.append(f"{case.id}: REFUSE 用例不应有 gold_sql")
            continue
        if not case.gold_sql:
            problems.append(f"{case.id}: SQL 用例缺少 gold_sql")
            continue
        check = validator.validate(case.gold_sql)
        if not check.ok:
            problems.append(f"{case.id}: gold SQL 不合法 -> {check.reason}")
        elif check.warnings:
            print(f"[warn] {case.id}: {'；'.join(check.warnings)}")

    print(f"用例总数 {len(cases)}；拒答 {sum(1 for c in cases if c.expect == 'REFUSE')} 条")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  -", p)
        return 1

    print("gold SQL 全部通过校验")
    return 0


def render_report(results: list[CaseResult], stats: dict, meta: dict) -> str:
    lines = [
        "# ChatBI 评测报告",
        "",
        f"- 时间：{meta['time']}",
        f"- 数据集：`{meta['dataset']}`",
        f"- 模型：{meta['model']}",
        f"- 结果比对：执行准确率（结果集比对，小数容差 1e-3，整数严格相等）",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 用例数 | {stats['total']} |",
        f"| 执行准确率 | {_pct(stats['execution_accuracy'])}（{stats['exec_matched']}/{stats['exec_cases']}） |",
        f"| 拒答准确率 | {_pct(stats['refusal_accuracy'])}（{stats['refuse_matched']}/{stats['refuse_cases']}） |",
        f"| 总体准确率 | {_pct(stats['overall_accuracy'])} |",
        f"| 一次通过率 | {_pct(stats['first_pass_rate'])} |",
        f"| 错误率 | {_pct(stats['error_rate'])} |",
        f"| 平均 / P95 延迟 | {stats['avg_latency_ms']} ms / {stats['p95_latency_ms']} ms |",
        "",
        "## 分类表现",
        "",
        "| 类别 | 准确数 / 总数 | 准确率 |",
        "|---|---|---|",
    ]
    for cat, stat in sorted(stats["by_category"].items()):
        lines.append(f"| {cat} | {stat['matched']}/{stat['total']} | {_pct(stat['accuracy'])} |")

    failed = [r for r in results if not r.matched]
    lines += ["", f"## 失败样例（{len(failed)} 条）", ""]
    if not failed:
        lines.append("无")
    for r in failed:
        lines += [
            f"### {r.id}｜{r.category}｜{'期望拒答' if r.expect == 'REFUSE' else '期望 SQL'}",
            f"- 问题：{r.question}",
            f"- 实际 action：{r.predicted_action}；attempts：{r.attempts}",
            f"- 原因：{r.reason}",
        ]
        if r.predicted_sql:
            lines += ["- 生成 SQL：", "```sql", r.predicted_sql, "```"]
        if r.gold_sql:
            lines += ["- gold SQL：", "```sql", r.gold_sql, "```"]
        lines.append("")

    lines += ["", "## 明细", "",
              "| id | 类别 | 结果 | attempts | 延迟(ms) | 说明 |",
              "|---|---|---|---|---|---|"]
    for r in results:
        mark = "PASS" if r.matched else "FAIL"
        lines.append(f"| {r.id} | {r.category} | {mark} | {r.attempts} | "
                     f"{r.latency_ms} | {r.reason[:60]} |")
    lines.append("")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatBI 执行准确率评测")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--tolerance", type=int, default=4, help="小数比较位数")
    parser.add_argument("--no-execute", action="store_true",
                        help="只生成 SQL，不连接数据库")
    parser.add_argument("--validate-only", action="store_true",
                        help="只校验 gold SQL，不需要 LLM/数据库")
    args = parser.parse_args()

    semantic = SemanticLayer.load()
    cases = load_cases(args.dataset)
    if args.limit:
        cases = cases[:args.limit]

    if args.validate_only:
        return validate_only(cases, semantic)

    executor = None if args.no_execute else MySQLExecutor()
    if executor is not None:
        # 先确认数据库可用，避免跑到一半才失败
        if not executor.time_range():
            print("无法读取 meta_dataset_range，请先执行 warehouse/run_all.sql")
            return 2

    pipeline = NL2SQLPipeline(semantic=semantic, executor=executor)
    evaluator = Evaluator(pipeline, executor, tolerance=args.tolerance)
    results = evaluator.run(cases)

    stats = summarize(results)
    meta = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": str(args.dataset),
        "model": pipeline.llm.config.model,
        "tolerance": args.tolerance,
    }
    report = render_report(results, stats, meta)

    args.outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = args.outdir / f"eval_{stamp}.md"
    report_path.write_text(report, encoding="utf-8")
    (args.outdir / "latest.md").write_text(report, encoding="utf-8")
    raw_path = args.outdir / f"eval_{stamp}.json"
    raw_path.write_text(json.dumps({"meta": meta, "stats": stats,
                                    "results": [r.__dict__ for r in results]},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"执行准确率 {_pct(stats['execution_accuracy'])}；"
          f"拒答准确率 {_pct(stats['refusal_accuracy'])}；"
          f"总体 {_pct(stats['overall_accuracy'])}")
    print(f"报告已写入 {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
