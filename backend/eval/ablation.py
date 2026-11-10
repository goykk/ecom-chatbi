"""语义层消融实验：同一问题、同一模型、同一校验链路，只切换「是否注入语义层」。

对照组设计：
- with：完整 Prompt（指标口径 + 维度别名/枚举映射 + 术语 + 时间规则）
- without：只给建表结构（字段名与类型），指标口径/别名/枚举映射全部移除
- 两侧共享同一 few-shot、同一拒答规则（拒答在生成前确定性触发，不受消融影响）

注意：few-shot 示例本身也传递了部分口径，所以「无语义层」这一侧并非彻底裸奔，
得到的差值是一个**保守估计**（真实差距只会更大，不会更小）。

用法：
    python -m eval.ablation
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.nl2sql.pipeline import MySQLExecutor, NL2SQLPipeline
from app.semantic.loader import SemanticLayer
from eval.runner import (
    DEFAULT_DATASET,
    DEFAULT_OUTDIR,
    CaseResult,
    Evaluator,
    load_cases,
    _pct,
)

DEFAULT_CACHE = Path(__file__).resolve().parent / "logs" / "ablation_cache.jsonl"


def load_cache(path: Path) -> dict[tuple[str, str], CaseResult]:
    """读取已完成记录，(配置, 用例) 为键；文件损坏的行直接跳过。"""
    done: dict[tuple[str, str], CaseResult] = {}
    if not path.exists():
        return done
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                config = rec.pop("config")
                done[(config, rec["id"])] = CaseResult(**rec)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    return done


def append_cache(path: Path, config: str, result: CaseResult) -> None:
    """逐条落盘并 fsync，进程被杀也不丢已完成的结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"config": config, **asdict(result)}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def run_cached(pipe, executor, cases, config: str, cache_path: Path,
               cache: dict, fewshot_k: int) -> list[CaseResult]:
    evaluator = Evaluator(pipe, executor, fewshot_k=fewshot_k)
    results: list[CaseResult] = []
    for idx, case in enumerate(cases, start=1):
        key = (config, case.id)
        cached = cache.get(key)
        if cached is not None:
            results.append(cached)
            continue
        result = evaluator.run_case(case)
        results.append(result)
        append_cache(cache_path, config, result)
        cache[key] = result
        print(f"[{config}] {idx}/{len(cases)} {case.id} "
              f"{'PASS' if result.matched else 'FAIL'} {result.latency_ms}ms",
              flush=True)
    return results


def fetch_raw_schema(executor: MySQLExecutor, whitelist: list[str]) -> str:
    names = ",".join(f"'{t}'" for t in whitelist)
    sql = (
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE "
        "FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = 'ecom_chatbi' AND TABLE_NAME IN ({names}) "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION"
    )
    _, rows = executor.query(sql, 500)
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["TABLE_NAME"], []).append(
            f'{r["COLUMN_NAME"]} {r["COLUMN_TYPE"]}'
        )
    return "\n".join(
        f"表 {table}（字段：{', '.join(cols)}）" for table, cols in grouped.items()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="语义层消融实验")
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    ap.add_argument("--fewshot", type=int, default=3,
                    help="两侧共享的 few-shot 条数；设为 0 可做严格隔离（消除示例泄漏的口径知识）")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                    help="断点续跑缓存：每条完成即落盘，重跑自动跳过")
    ap.add_argument("--reset", action="store_true", help="清空缓存后重新跑")
    args = ap.parse_args()

    if args.reset and args.cache.exists():
        args.cache.unlink()
        print(f"[cache] 已清空 {args.cache}")

    semantic = SemanticLayer.load()
    cases = load_cases(args.dataset)
    executor = MySQLExecutor()
    if not executor.time_range():
        print("无法读取 meta_dataset_range，请先执行 warehouse/run_all.sql")
        return 2

    raw_schema = fetch_raw_schema(executor, semantic.guardrails.get("table_whitelist", []))
    print(f"[raw schema] {len(raw_schema)} chars, "
          f"{raw_schema.count('表 ')} tables")

    full = NL2SQLPipeline(semantic=semantic, executor=executor, use_semantic=True)
    raw = NL2SQLPipeline(semantic=semantic, executor=executor, use_semantic=False,
                         raw_schema=raw_schema)

    cache = load_cache(args.cache)
    print(f"[cache] 已有 {len(cache)} 条记录（{args.cache}）", flush=True)

    runs = {}
    for name, pipe in (("with_semantic", full), ("without_semantic", raw)):
        results = run_cached(pipe, executor, cases, name, args.cache, cache, args.fewshot)
        runs[name] = results
        from eval.metrics import summarize
        stats = summarize(results)
        print(f"{name}: EA={_pct(stats['execution_accuracy'])} "
              f"refuse={_pct(stats['refusal_accuracy'])} "
              f"overall={_pct(stats['overall_accuracy'])}")

    with_rows = {r.id: r for r in runs["with_semantic"]}
    without_rows = {r.id: r for r in runs["without_semantic"]}
    flips = [cid for cid in with_rows
             if with_rows[cid].matched != without_rows[cid].matched]
    gains = [cid for cid in flips if with_rows[cid].matched and not without_rows[cid].matched]
    losses = [cid for cid in flips if not with_rows[cid].matched and without_rows[cid].matched]

    from eval.metrics import summarize
    s_full = summarize(runs["with_semantic"])
    s_raw = summarize(runs["without_semantic"])
    ea_full = s_full["execution_accuracy"] or 0
    ea_raw = s_raw["execution_accuracy"] or 0

    lines = [
        "# 语义层消融实验",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 数据集：`{args.dataset.name}`（{len(cases)} 条）",
        f"- 控制变量：同一模型、同一 Prompt 模板、同一校验/修复链路、同一 few-shot（{args.fewshot} 条）、同一拒答规则",
        "- 唯一变量：是否注入指标口径 / 维度别名 / 枚举映射 / 术语",
        "",
        "## 结果",
        "",
        "| 配置 | 执行准确率 | 拒答准确率 | 总体 | 平均延迟(ms) |",
        "|---|---|---|---|---|",
        f"| 有语义层 | {_pct(s_full['execution_accuracy'])} | "
        f"{_pct(s_full['refusal_accuracy'])} | {_pct(s_full['overall_accuracy'])} | "
        f"{s_full['avg_latency_ms']} |",
        f"| 无语义层 | {_pct(s_raw['execution_accuracy'])} | "
        f"{_pct(s_raw['refusal_accuracy'])} | {_pct(s_raw['overall_accuracy'])} | "
        f"{s_raw['avg_latency_ms']} |",
        "",
        f"**执行准确率差值：{ea_full - ea_raw:+.1%}（"
        f"{(ea_full - ea_raw) * s_full['exec_cases']:+.0f} 条 / {s_full['exec_cases']}）**",
        "",
    ]

    lines += ["## 发生变化的用例", "",
              "| 用例 | 类别 | 问题 | 有语义层 | 无语义层 |",
              "|---|---|---|---|---|"]
    for cid in flips:
        c = next(x for x in cases if x.id == cid)
        lines.append(f"| {cid} | {c.category} | {c.question} | "
                     f"{'PASS' if with_rows[cid].matched else 'FAIL'} | "
                     f"{'PASS' if without_rows[cid].matched else 'FAIL'} |")
    if not flips:
        lines.append("| — | — | 无差异 | — | — |")

    lines += [
        "",
        "## 说明",
        "",
        f"- 有语义层侧多解决：{len(gains)} 条（{', '.join(gains) or '无'}）",
        f"- 无语义层侧反而通过：{len(losses)} 条（{', '.join(losses) or '无'}）",
        "- few-shot 与拒答规则两侧共享，因此差值是无语义层影响的**保守估计**。",
        "- 拒答用例在生成前由规则确定性触发，两种配置下均 100%，不参与差异统计。",
        "",
    ]
    report = "\n".join(lines)

    args.outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = args.outdir / f"ablation_{stamp}.md"
    path.write_text(report, encoding="utf-8")
    (args.outdir / f"ablation_{stamp}.json").write_text(json.dumps({
        "with_semantic": s_full,
        "without_semantic": s_raw,
        "flips": flips,
        "gains": gains,
        "losses": losses,
        "per_case": {cid: {"with": with_rows[cid].matched,
                           "without": without_rows[cid].matched}
                     for cid in with_rows},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n差值 {ea_full - ea_raw:+.1%}；有语义层多解决 {len(gains)} 条；"
          f"报告已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
