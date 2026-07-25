# -*- coding: utf-8 -*-
"""
从聚宽回测日志解析 JQMIN_* 分片，还原为分钟级数据文件。

支持：
  - v2.3+ 按分钟截面：键=HHMM，payload 与落盘 HHMM.json 同构
  - v2.3 日频：键=daily → daily_snap
  - 旧版股票批次：键=整数，bars 为股票×时间序列（自动折成分钟）
  - zlib + base64 / 明文 JSON

输出：
  JQData/minute/年/日期/HHMM.json
  JQData/minute/年/日期/meta.json
  JQData/daily_snap/年/日期.json

用法：
  python 解析JQData分钟日志.py JQData/logs/xxx.txt
  python 解析JQData分钟日志.py xxx.txt --force
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import zlib
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from paths import DAILY_SNAP_DIR, LOGS_DIR, MINUTE_DIR

BAR_FIELDS = ("open", "high", "low", "close", "volume", "money")

# JQMIN_BEGIN|date|slot|total|zlib_b64|...
# slot = HHMM | daily | 旧版 batch_index
BEGIN_RE = re.compile(
    r"JQMIN_BEGIN\|(\d{4}-\d{2}-\d{2})\|([^|]+)\|(\d+)(?:\|([^\s|]+))?"
)
END_RE = re.compile(r"JQMIN_END\|(\d{4}-\d{2}-\d{2})\|([^|]+)\|(\d+)")
PART_RE = re.compile(
    r"JQMIN_PART\|(\d{4}-\d{2}-\d{2})\|([^|]+)\|(\d+)\|(\d+)\|(.*)"
)


def _slot_sort_key(slot: str):
    if slot == "daily":
        return (2, slot)
    if re.fullmatch(r"\d{4}", slot):
        return (0, slot)
    try:
        return (1, f"{int(slot):06d}")
    except ValueError:
        return (1, slot)


def parse_log(log_path: Path) -> dict[str, dict[str, dict]]:
    """
    {日期: {slot: {"slot_total", "encoding", "parts": {idx: (total, chunk)}}}}
    """
    days: dict[str, dict[str, dict]] = {}
    begins: set[tuple[str, str]] = set()
    ends: set[tuple[str, str]] = set()

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if m := BEGIN_RE.search(line):
                date, slot, total = m.group(1), m.group(2), int(m.group(3))
                enc = m.group(4) or "json"
                begins.add((date, slot))
                s = days.setdefault(date, {}).setdefault(
                    slot, {"slot_total": total, "encoding": enc, "parts": {}}
                )
                s["slot_total"] = total
                s["encoding"] = enc
            elif m := END_RE.search(line):
                date, slot, total = m.group(1), m.group(2), int(m.group(3))
                ends.add((date, slot))
                s = days.setdefault(date, {}).setdefault(
                    slot, {"slot_total": total, "encoding": "json", "parts": {}}
                )
                s["slot_total"] = total
            elif m := PART_RE.search(line):
                date = m.group(1)
                slot = m.group(2)
                idx = int(m.group(3))
                total = int(m.group(4))
                chunk = m.group(5)
                s = days.setdefault(date, {}).setdefault(
                    slot, {"slot_total": 0, "encoding": "json", "parts": {}}
                )
                s["parts"][idx] = (total, chunk)

    missing = sorted(begins - ends)
    if missing:
        print(
            f"警告: {len(missing)} 个槽位有 BEGIN 无 END，例: {missing[:5]}",
            file=sys.stderr,
        )
    return days


def reconstruct_slot(date: str, slot: str, info: dict) -> dict:
    parts: dict[int, tuple[int, str]] = info.get("parts") or {}
    if not parts:
        raise ValueError(f"{date} slot={slot}: 无 PART")

    total = next(iter(parts.values()))[0]
    missing = [i for i in range(1, total + 1) if i not in parts]
    if missing:
        raise ValueError(
            f"{date} slot={slot}: 缺分片 {missing[:5]} ({len(missing)}/{total})"
        )
    blob = "".join(parts[i][1] for i in range(1, total + 1))
    enc = (info.get("encoding") or "json").strip()

    if enc in ("zlib_b64", "zlib", "b64zlib"):
        raw = zlib.decompress(base64.b64decode(blob))
        return json.loads(raw.decode("utf-8"))

    return json.loads(blob)


def time_to_filename(t: str) -> str:
    """'09:31' -> '0931.json'"""
    return t.replace(":", "") + ".json"


def is_series_bars(bars: dict) -> bool:
    """旧版：bars[code] 含 time 列表。"""
    if not bars:
        return False
    sample = next(iter(bars.values()))
    return isinstance(sample, dict) and isinstance(sample.get("time"), list)


def is_cross_section_bars(bars: dict) -> bool:
    """新版：bars[code] = {open,high,...} 标量字段。"""
    if not bars:
        return False
    sample = next(iter(bars.values()))
    if not isinstance(sample, dict):
        return False
    if "time" in sample and isinstance(sample.get("time"), list):
        return False
    return any(f in sample for f in BAR_FIELDS)


def accumulate_bars(bars_by_code: dict, minutes: dict[str, dict[str, dict]]) -> None:
    """股票×时间序列 → minutes[time][code]。"""
    for code, series in (bars_by_code or {}).items():
        if not isinstance(series, dict):
            continue
        times = series.get("time") or []
        for i, t in enumerate(times):
            bar = {
                f: series[f][i]
                for f in BAR_FIELDS
                if f in series and isinstance(series[f], list) and i < len(series[f])
            }
            minutes.setdefault(t, {})[code] = bar


def ingest_payload(
    obj: dict,
    minutes: dict[str, dict[str, dict]],
    merged_daily: dict,
) -> dict | None:
    """把一个还原后的 payload 并入 minutes / daily，返回 _meta。"""
    meta = obj.get("_meta") or {}
    kind = meta.get("kind")
    layout = meta.get("layout") or obj.get("layout")

    daily = obj.get("daily") or {}
    if daily:
        merged_daily.update(daily)

    # 日频专用包
    if kind == "daily":
        return meta

    # v2.3 单分钟截面（与 HHMM.json 同构）
    if meta.get("time") and isinstance(obj.get("bars"), dict) and is_cross_section_bars(
        obj["bars"]
    ):
        t = meta["time"]
        minutes.setdefault(t, {}).update(obj["bars"])
        return meta

    # 一批里带多个分钟
    if isinstance(obj.get("minutes"), dict):
        for t, bars in obj["minutes"].items():
            if isinstance(bars, dict):
                minutes.setdefault(t, {}).update(bars)
        return meta

    bars = obj.get("bars") or {}
    if is_series_bars(bars):
        accumulate_bars(bars, minutes)
        return meta
    if layout == "by_minute" and is_cross_section_bars(bars) and meta.get("time"):
        minutes.setdefault(meta["time"], {}).update(bars)
        return meta

    # 兜底：若像截面但无 time，忽略分钟（可能是脏数据）
    return meta


def write_minute_day(
    day_dir: Path,
    trade_date: str,
    minutes: dict[str, dict[str, dict]],
    *,
    batch_metas: list | None = None,
    daily_snap_count: int = 0,
    extra_meta: dict | None = None,
) -> dict:
    day_dir.mkdir(parents=True, exist_ok=True)
    times = sorted(minutes)
    minute_files = []
    stock_ids: set[str] = set()

    for t in times:
        bars = minutes[t]
        stock_ids.update(bars)
        fname = time_to_filename(t)
        minute_files.append(fname)
        obj = {
            "_meta": {
                "trade_date": trade_date,
                "time": t,
                "freq": "1m",
                "fields": list(BAR_FIELDS),
                "stock_count": len(bars),
            },
            "bars": bars,
        }
        (day_dir / fname).write_text(
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    meta = {
        "trade_date": trade_date,
        "layout": "by_minute",
        "minute_files": minute_files,
        "minute_count": len(minute_files),
        "stock_count": len(stock_ids),
        "daily_snap_count": daily_snap_count,
        "batches": batch_metas or [],
    }
    if extra_meta:
        meta.update(extra_meta)
    (day_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def convert_part_dir(day_dir: Path, *, remove_parts: bool = True) -> dict:
    parts = sorted(day_dir.glob("part_*.json"))
    if not parts:
        raise FileNotFoundError(f"无 part_*.json: {day_dir}")

    trade_date = day_dir.name
    minutes: dict[str, dict[str, dict]] = {}
    merged_daily: dict = {}
    batch_metas: list = []

    for p in parts:
        obj = json.loads(p.read_text(encoding="utf-8"))
        ingest_payload(obj, minutes, merged_daily)
        batch_metas.append(obj.get("_meta") or {"file": p.name})

    meta = write_minute_day(
        day_dir,
        trade_date,
        minutes,
        batch_metas=batch_metas,
        daily_snap_count=len(merged_daily),
        extra_meta={"converted_from": "part_*.json"},
    )

    if remove_parts:
        for p in parts:
            p.unlink()

    return meta


def extract(
    log_path: Path,
    minute_dir: Path,
    snap_dir: Path,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    skip_existing: bool = True,
) -> dict:
    days = parse_log(log_path)
    dates = sorted(
        d
        for d in days
        if (not date_from or d >= date_from) and (not date_to or d <= date_to)
    )

    stats = {"ok_slots": 0, "skipped": 0, "failed": 0, "dates": [], "minutes": {}}

    for date in dates:
        day_dir = minute_dir / date[:4] / date
        snap_path = snap_dir / date[:4] / f"{date}.json"
        meta_path = day_dir / "meta.json"

        if skip_existing and meta_path.exists():
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
                if old.get("layout") == "by_minute" and old.get("minute_count", 0) > 0:
                    stats["skipped"] += 1
                    stats["dates"].append(date)
                    continue
            except Exception:
                pass

        minutes: dict[str, dict[str, dict]] = {}
        merged_daily: dict = {}
        batch_metas: list = []
        slots = days[date]

        for slot in sorted(slots, key=_slot_sort_key):
            try:
                obj = reconstruct_slot(date, slot, slots[slot])
                meta = ingest_payload(obj, minutes, merged_daily)
                batch_metas.append(meta or {"slot": slot})
                stats["ok_slots"] += 1
            except Exception as e:
                stats["failed"] += 1
                print(f"失败 {date} slot={slot}: {e}", file=sys.stderr)

        if day_dir.exists():
            for p in day_dir.glob("part_*.json"):
                p.unlink()

        meta = write_minute_day(
            day_dir,
            date,
            minutes,
            batch_metas=batch_metas,
            daily_snap_count=len(merged_daily),
        )
        stats["minutes"][date] = meta["minute_count"]

        if merged_daily:
            snap_path.parent.mkdir(parents=True, exist_ok=True)
            if (not skip_existing) or (not snap_path.exists()):
                snap_path.write_text(
                    json.dumps(
                        {
                            "_meta": {"trade_date": date, "count": len(merged_daily)},
                            "stocks": merged_daily,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )

        stats["dates"].append(date)

    return stats


def main():
    parser = argparse.ArgumentParser(description="解析聚宽分钟采集日志 → JQData/minute")
    parser.add_argument("log", nargs="?", default=str(LOGS_DIR / "minute_log.txt"))
    parser.add_argument("--minute-out", default=str(MINUTE_DIR))
    parser.add_argument("--snap-out", default=str(DAILY_SNAP_DIR))
    parser.add_argument("--from", dest="date_from", default=None)
    parser.add_argument("--to", dest="date_to", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--convert-dir",
        default=None,
        help="把已有 part_*.json 目录转为按分钟布局（不读日志）",
    )
    parser.add_argument(
        "--keep-parts",
        action="store_true",
        help="转换时保留 part_*.json",
    )
    args = parser.parse_args()

    if args.convert_dir:
        meta = convert_part_dir(Path(args.convert_dir), remove_parts=not args.keep_parts)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return

    stats = extract(
        Path(args.log),
        Path(args.minute_out),
        Path(args.snap_out),
        date_from=args.date_from,
        date_to=args.date_to,
        skip_existing=not args.force,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
