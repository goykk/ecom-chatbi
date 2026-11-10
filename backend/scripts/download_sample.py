#!/usr/bin/env python3
"""按用户抽样天池 UserBehavior 数据，产出可直接被 01_import_userbehavior.sql 导入的 CSV。

为什么必须按用户抽样：
    按行随机抽样会把用户会话切碎，路径表和留存矩阵全部失真。
    本脚本保证抽中的用户，其全部行为记录都保留。

用法：
    # 本地已有 CSV / ZIP（天池下载需要登录，通常先手动下载）
    python download_sample.py --input ~/Downloads/UserBehavior.csv --target-rows 8000000

    # 或直接给 URL（部分公开镜像可用；天池官方链接带鉴权，可能失败）
    python download_sample.py --url "https://..." --input /tmp/UserBehavior.csv

    # 只估算规模，不写文件
    python download_sample.py --input UserBehavior.csv --dry-run

输出默认写到 ecom-chatbi/data/ods/userbehavior_sample.csv，
与 docker-compose 的 ../data:/data 挂载点、01 脚本的 /data/ods/ 路径一致。
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List

FIELDS = ["user_id", "item_id", "category_id", "behavior_type", "timestamp"]
CST = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[2]      # ecom-chatbi/
DEFAULT_OUT = PROJECT_ROOT / "data" / "ods" / "userbehavior_sample.csv"


def open_text(path: Path) -> io.TextIOBase:
    """支持 .zip（天池原始包）与 .csv。zip 内取第一个 csv 成员。"""
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise SystemExit(f"zip 内未找到 csv：{path}")
        return io.TextIOWrapper(zf.open(members[0]), encoding="utf-8", newline="")
    return open(path, "r", encoding="utf-8", newline="")


def iter_rows(path: Path) -> Iterator[List[str]]:
    """逐行产出 5 个字段，自动跳过表头与脏行。"""
    with open_text(path) as fh:
        for row in csv.reader(fh):
            if len(row) < 5:
                continue
            # 表头或非数字首列一律跳过，避免把 header 当成数据
            if not row[0].strip().isdigit():
                continue
            # 时间戳必须是数字且落在合理区间（2017 年及以后），
            # 过滤 -1/0 等哨兵值，否则 Windows 下 fromtimestamp 会抛 Errno 22
            ts = row[4].strip()
            if not ts.isdigit() or int(ts) < 1_500_000_000:
                continue
            yield [row[0].strip(), row[1].strip(), row[2].strip(),
                   row[3].strip(), ts]


def download(url: str, dest: Path) -> None:
    """可选的直链下载。天池官方链接需要登录态，失败时请手动下载后走 --input。"""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r下载中 {done / total:.1%}", end="", file=sys.stderr)
    print(f"\n已下载到 {dest}", file=sys.stderr)


def to_cst(ts: int) -> datetime | None:
    if ts >= 100_000_000_000:          # 毫秒
        ts //= 1000
    try:
        return datetime.fromtimestamp(ts, CST)
    except (OSError, OverflowError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="UserBehavior 按用户抽样")
    ap.add_argument("--input", type=Path, help="本地 csv / zip 路径")
    ap.add_argument("--url", help="可选直链下载地址")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 csv 路径")
    ap.add_argument("--target-rows", type=int, default=8_000_000,
                    help="目标行数（软上限，实际按整用户输出，可能略超）")
    ap.add_argument("--target-users", type=int, default=0,
                    help="直接指定抽样用户数，优先级高于 target-rows")
    ap.add_argument("--min-users", type=int, default=300_000, help="用户数下限")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，保证可复现")
    ap.add_argument("--dry-run", action="store_true", help="只扫描统计，不写文件")
    args = ap.parse_args()

    if args.url and not args.input:
        name = Path(args.url.split("?")[0]).name or "UserBehavior.csv"
        dest = PROJECT_ROOT / "data" / "raw" / name
        download(args.url, dest)
        args.input = dest
    if not args.input or not args.input.exists():
        raise SystemExit("请用 --input 指定本地数据文件（csv 或 zip）")

    random.seed(args.seed)

    # ---------- pass 1：收集用户、统计规模 ----------
    users: set[int] = set()
    total_rows = 0
    behavior = Counter()
    ts_min, ts_max = None, None
    for row in iter_rows(args.input):
        uid = int(row[0])
        users.add(uid)
        total_rows += 1
        behavior[row[3]] += 1
        ts = int(row[4])
        ts = ts // 1000 if ts >= 100_000_000_000 else ts
        ts_min = ts if ts_min is None else min(ts_min, ts)
        ts_max = ts if ts_max is None else max(ts_max, ts)
        if total_rows % 5_000_000 == 0:
            print(f"\r扫描 {total_rows:,} 行 / {len(users):,} 用户", end="", file=sys.stderr)

    print(f"\n总行数 {total_rows:,}，去重用户 {len(users):,}，"
          f"平均 {total_rows / max(len(users), 1):.1f} 行/用户", file=sys.stderr)
    print(f"行为分布 {dict(behavior)}", file=sys.stderr)
    if ts_min is not None and to_cst(ts_min) and to_cst(ts_max):
        print(f"时间范围 {to_cst(ts_min):%Y-%m-%d} ~ {to_cst(ts_max):%Y-%m-%d} (+08:00)",
              file=sys.stderr)

    # ---------- 选择用户 ----------
    avg_per_user = total_rows / max(len(users), 1)
    if args.target_users > 0:
        k = args.target_users
    else:
        k = int(args.target_rows / max(avg_per_user, 1.0))
    k = max(args.min_users, min(k, len(users)))
    sampled = set(random.sample(list(users), k))

    print(f"抽样用户 {k:,} / {len(users):,}（预计约 {k * avg_per_user:,.0f} 行，"
          f"实际按整用户输出）", file=sys.stderr)

    if args.dry_run:
        return

    # ---------- pass 2：只写抽中用户的全部记录 ----------
    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    written_users: set[int] = set()
    with open(args.out, "w", encoding="utf-8", newline="") as fo:
        w = csv.writer(fo)
        w.writerow(FIELDS)
        for row in iter_rows(args.input):
            uid = int(row[0])
            if uid not in sampled:
                continue
            w.writerow(row)
            written += 1
            written_users.add(uid)
            if written % 2_000_000 == 0:
                print(f"\r写入 {written:,} 行", end="", file=sys.stderr)

    size_mb = args.out.stat().st_size / 1024 / 1024
    print(f"\n已写入 {args.out}\n"
          f"  行数 {written:,} / 用户 {len(written_users):,} / 体积 {size_mb:.1f} MB\n"
          f"  覆盖率 {len(written_users) / len(sampled):.2%}（应为 100%，不足说明数据被截断）",
          file=sys.stderr)


if __name__ == "__main__":
    main()
