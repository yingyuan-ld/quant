# -*- coding: utf-8 -*-
"""从聚宽回测日志中解析 JQDATA_PART 分片，还原为 daily/年/日期.json。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BEGIN_RE = re.compile(r"JQDATA_BEGIN\|(\d{4}-\d{2}-\d{2})")
END_RE = re.compile(r"JQDATA_END\|(\d{4}-\d{2}-\d{2})")
PART_RE = re.compile(r"JQDATA_PART\|(\d{4}-\d{2}-\d{2})\|(\d+)\|(\d+)\|(.*)")

DEFAULT_LOG = Path(__file__).resolve().parent / "JQData" / "logs" / "2023-01-01_2024-12-31.txt"
DEFAULT_OUT = Path(__file__).resolve().parent / "JQData" / "daily"


def daily_json_path(out_dir: Path, date: str) -> Path:
    return out_dir / date[:4] / f"{date}.json"


def parse_log(log_path: Path) -> dict[str, dict[int, tuple[int, str]]]:
    """扫描日志，返回 {日期: {分片序号: (总分片数, 内容)}}。"""
    parts: dict[str, dict[int, tuple[int, str]]] = {}
    begins: set[str] = set()
    ends: set[str] = set()

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if m := BEGIN_RE.search(line):
                begins.add(m.group(1))
            elif m := END_RE.search(line):
                ends.add(m.group(1))
            elif m := PART_RE.search(line):
                date, idx, total, chunk = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
                parts.setdefault(date, {})[idx] = (total, chunk)

    incomplete = sorted(begins - ends)
    if incomplete:
        print(f"警告: {len(incomplete)} 个日期有 BEGIN 但无 END: {incomplete[:5]}", file=sys.stderr)

    return parts


def reconstruct(date: str, chunks: dict[int, tuple[int, str]]) -> dict:
    if not chunks:
        raise ValueError(f"{date}: 无 JQDATA_PART 分片")

    total = next(iter(chunks.values()))[0]
    missing = [i for i in range(1, total + 1) if i not in chunks]
    if missing:
        raise ValueError(f"{date}: 缺少分片 {missing[:5]}{'...' if len(missing) > 5 else ''} ({len(missing)}/{total})")

    content = "".join(chunks[i][1] for i in range(1, total + 1))
    return json.loads(content)


def in_range(date: str, date_from: str | None, date_to: str | None) -> bool:
    if date_from and date < date_from:
        return False
    if date_to and date > date_to:
        return False
    return True


def extract(
    log_path: Path,
    out_dir: Path,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    skip_existing: bool = True,
    indent: int = 2,
) -> dict:
    parts = parse_log(log_path)
    dates = sorted(d for d in parts if in_range(d, date_from, date_to))

    out_dir.mkdir(parents=True, exist_ok=True)

    stats = {"ok": 0, "skipped": 0, "failed": 0, "dates": []}
    for date in dates:
        out_path = daily_json_path(out_dir, date)
        if skip_existing and out_path.exists():
            stats["skipped"] += 1
            continue

        try:
            obj = reconstruct(date, parts[date])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=indent)
                f.write("\n")
            stats["ok"] += 1
            stats["dates"].append(date)
        except Exception as e:
            stats["failed"] += 1
            print(f"失败 {date}: {e}", file=sys.stderr)

    return stats


def main():
    parser = argparse.ArgumentParser(description="从聚宽日志还原 JQData daily JSON")
    parser.add_argument("log", nargs="?", default=str(DEFAULT_LOG), help="日志文件路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出根目录，默认 JQData/daily（按年分子目录）")
    parser.add_argument("--from", dest="date_from", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 JSON")
    args = parser.parse_args()

    stats = extract(
        Path(args.log),
        Path(args.out),
        date_from=args.date_from,
        date_to=args.date_to,
        skip_existing=not args.force,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
