# -*- coding: utf-8 -*-
"""
逐日回测 → 滚动提取 #daily-logs-container → 解析入库 JQData/minute

前置：
  1. Chrome 以 --remote-debugging-port=9222 启动，并打开聚宽「空策略看数据」编辑页
  2. 策略代码已是 聚宽策略-数据采集.py（v2.3 按分钟截面）
  3. 已安装 playwright（与 提取JQData日志.py 相同）

用法：
  python run_backfill.py --from 2026-07-13 --to 2026-07-01
  python run_backfill.py --from 2026-07-13 --days 5
  python run_backfill.py --from 2026-07-13 --to 2026-07-01 --dry-run
  python run_backfill.py --dates 2026-07-13,2026-07-10 --force
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

SKILL_DIR = Path(__file__).resolve().parent.parent
INGEST_DIR = SKILL_DIR.parent  # .../入库
PKG_ROOT = INGEST_DIR.parent  # .../python-dataformart

if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))
if str(INGEST_DIR) not in sys.path:
    sys.path.insert(0, str(INGEST_DIR))

from paths import LOGS_DIR, MINUTE_DIR  # noqa: E402

CDP_URL = "http://127.0.0.1:9222"
JQ_URL_KEY = "joinquant.com/algorithm"
CHUNK_SIZE = 3_000_000

# 全市场约 240 分钟 + 1 个 daily；分片跑会更少，用下限判定
MIN_SLOTS_DONE = 200
DEFAULT_WAIT_SEC = 3600
POLL_SEC = 5
# 日志长度/槽位无进展超过此时长，视为卡住并点「取消编译」终止
STUCK_SEC = 300

# 上交所节假日休市闭区间（不含周末本身；周末另跳）。来源：上证公告休市安排。
# 覆盖回填常用区间；缺少年份时仍跳周末，休市日会卡死后跳过。
_ASHARE_CLOSED_RANGES: tuple[tuple[date, date], ...] = tuple(
    (datetime.strptime(a, "%Y-%m-%d").date(), datetime.strptime(b, "%Y-%m-%d").date())
    for a, b in (
        # 2024
        ("2024-01-01", "2024-01-01"),
        ("2024-02-09", "2024-02-17"),
        ("2024-04-04", "2024-04-06"),
        ("2024-05-01", "2024-05-05"),
        ("2024-06-08", "2024-06-10"),
        ("2024-09-15", "2024-09-17"),
        ("2024-10-01", "2024-10-07"),
        # 2025
        ("2025-01-01", "2025-01-01"),
        ("2025-01-28", "2025-02-04"),
        ("2025-04-04", "2025-04-06"),
        ("2025-05-01", "2025-05-05"),
        ("2025-05-31", "2025-06-02"),
        ("2025-10-01", "2025-10-08"),
        # 2026
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),  # 春节：2/24 开市
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    )
)


def _load_mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def is_ashare_trading_day(d: date) -> bool:
    """工作日且不在上交所节假日休市区间内。"""
    if d.weekday() >= 5:
        return False
    for a, b in _ASHARE_CLOSED_RANGES:
        if a <= d <= b:
            return False
    return True


def iter_dates_backwards(
    start: date,
    *,
    end: date | None = None,
    days: int | None = None,
    skip_non_trading: bool = True,
) -> list[date]:
    """从 start 往前（含），到 end（含）或共 days 个交易日（默认可跳过周末+A股休市）。"""
    if end is not None and end > start:
        raise ValueError("--to 必须 ≤ --from（逐日往前）")
    if end is None and days is None:
        return [start]

    out: list[date] = []
    cur = start
    while True:
        if end is not None and cur < end:
            break
        if days is not None and len(out) >= days:
            break
        if skip_non_trading and not is_ashare_trading_day(cur):
            cur -= timedelta(days=1)
            continue
        out.append(cur)
        cur -= timedelta(days=1)
        if len(out) > 5000:
            raise RuntimeError("日期范围过大")
    return out


def day_already_ingested(d: date) -> bool:
    meta = MINUTE_DIR / f"{d.year}" / d.isoformat() / "meta.json"
    if not meta.exists():
        return False
    try:
        obj = json.loads(meta.read_text(encoding="utf-8"))
        return obj.get("layout") == "by_minute" and int(obj.get("minute_count") or 0) >= 200
    except Exception:
        return False


def find_jq_page(browser, page_index: int = 0):
    pages = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if JQ_URL_KEY in pg.url:
                pages.append(pg)
    if not pages:
        raise RuntimeError("未找到聚宽策略页面，请保持编辑页在 Chrome 中打开")
    if page_index < 0 or page_index >= len(pages):
        raise RuntimeError(f"页签索引无效，当前聚宽页签数: {len(pages)}")
    return pages[page_index]


def set_backtest_dates(page, d: date) -> None:
    ds = d.isoformat()
    page.evaluate(
        """(ds) => {
          const $ = window.jQuery;
          const start = document.querySelector('#startTime');
          const end = document.querySelector('#endTime');
          if (!start || !end) throw new Error('找不到 #startTime/#endTime');
          if ($ && $.fn && $.fn.datepicker) {
            $(start).datepicker('setDate', ds);
            $(end).datepicker('setDate', ds);
          } else {
            start.value = ds;
            end.value = ds;
            start.dispatchEvent(new Event('change', { bubbles: true }));
            end.dispatchEvent(new Event('change', { bubbles: true }));
            start.dispatchEvent(new Event('input', { bubbles: true }));
            end.dispatchEvent(new Event('input', { bubbles: true }));
          }
          return { start: start.value, end: end.value };
        }""",
        ds,
    )


def click_compile_run(page) -> None:
    """点击「编译运行」(#validate-button)，日志留在编辑页 #daily-logs-container。
    勿点「运行回测」——会跳到回测详情页，编排脚本拿不到同页日志。
    「取消编译」仅在卡住超过 STUCK_SEC 时由 wait_backtest_done 触发，勿常规定点。
    """
    ok = page.evaluate(
        """() => {
          const btn = document.querySelector('#validate-button');
          if (!btn) return false;
          btn.click();
          return true;
        }"""
    )
    if not ok:
        raise RuntimeError("找不到「编译运行」按钮 (#validate-button)")


def click_cancel_compile(page) -> bool:
    """点击「取消编译」。仅用于确认卡住后的终止，不要在正常启动流程里调用。"""
    return bool(
        page.evaluate(
            """() => {
              const cancel = document.querySelector('#cancel-daily-backtest-button');
              if (!cancel) return false;
              cancel.click();
              return true;
            }"""
        )
    )


def log_date_info(page) -> dict:
    return page.evaluate(
        """() => {
          const text = document.querySelector('#daily-logs-tab')?.innerText || '';
          const ends = [...text.matchAll(/Minute结束 (\\d{4}-\\d{2}-\\d{2})/g)].map(m => m[1]);
          const begins = [...text.matchAll(/JQMIN_BEGIN\\|(\\d{4}-\\d{2}-\\d{2})\\|/g)].map(m => m[1]);
          const starts = [...text.matchAll(/Minute开始 (\\d{4}-\\d{2}-\\d{2})/g)].map(m => m[1]);
          return {
            logLen: text.length,
            begin: (text.match(/JQMIN_BEGIN\\|/g) || []).length,
            end: (text.match(/JQMIN_END\\|/g) || []).length,
            parts: (text.match(/JQMIN_PART\\|/g) || []).length,
            hasMinuteEnd: /Minute结束/.test(text),
            minuteEndDates: [...new Set(ends)],
            beginDates: [...new Set(begins)],
            startDates: [...new Set(starts)],
          };
        }"""
    )


def _progress_key(info: dict, ds: str) -> tuple:
    """用于判断是否仍在推进（与目标日相关）。"""
    return (
        int(info.get("logLen") or 0),
        int(info.get("begin") or 0),
        int(info.get("end") or 0),
        ds in (info.get("startDates") or []),
        ds in (info.get("beginDates") or []),
        ds in (info.get("minuteEndDates") or []),
    )


def wait_backtest_done(page, d: date, *, timeout_sec: int, min_slots: int) -> dict:
    """等待当日采集写完：滚动加载日志，直到目标日 Minute结束 且槽位齐。
    若进度超过 STUCK_SEC 无变化，点「取消编译」并抛错，交给外层进入下一日。
    """
    ds = d.isoformat()
    deadline = time.time() + timeout_sec
    last = {}
    last_key = None
    last_progress_at = time.time()
    page.evaluate(
        """() => {
          document.querySelector('a[href*=\"daily-logs-tab\"]')?.click?.();
          document.querySelector('#daily-logs-tab')?.click?.();
        }"""
    )
    while time.time() < deadline:
        # 虚拟列表必须滚动，否则 tab.innerText 看不到尾部的 Minute结束
        page.evaluate(
            """async () => {
              const container = document.querySelector('#daily-logs-container');
              if (!container) return;
              for (let i = 0; i < 30; i++) {
                container.scrollTop = container.scrollHeight;
                container.dispatchEvent(new Event('scroll', { bubbles: true }));
                await new Promise(r => setTimeout(r, 30));
              }
            }"""
        )
        last = log_date_info(page)
        end_dates = last.get("minuteEndDates") or []
        key = _progress_key(last, ds)
        if key != last_key:
            last_key = key
            last_progress_at = time.time()

        print(
            f"  wait {ds}: len={last.get('logLen')} begin={last.get('begin')} "
            f"end={last.get('end')} ends={end_dates} "
            f"idle={int(time.time() - last_progress_at)}s",
            flush=True,
        )

        has_day = (
            ds in (last.get("beginDates") or [])
            and ds in end_dates
            and last.get("begin", 0) >= min_slots
            and last.get("end", 0) >= min_slots
            and last.get("begin") == last.get("end")
        )
        if has_day:
            return last

        if time.time() - last_progress_at >= STUCK_SEC:
            clicked = click_cancel_compile(page)
            raise TimeoutError(
                f"卡住超过 {STUCK_SEC}s，已取消编译={clicked}: {ds} last={last}"
            )

        time.sleep(POLL_SEC)
    raise TimeoutError(f"回测/采集超时 ({timeout_sec}s): {ds} last={last}")


def scroll_load_all(page) -> dict:
    """操纵 #daily-logs-container 滚到底，直到日志长度与 JQMIN_END 稳定。"""
    return page.evaluate(
        """async () => {
          const container = document.querySelector('#daily-logs-container');
          const tab = document.querySelector('#daily-logs-tab');
          const getStats = () => {
            const text = tab ? tab.innerText : '';
            return {
              logLen: text.length,
              begin: (text.match(/JQMIN_BEGIN\\|/g) || []).length,
              end: (text.match(/JQMIN_END\\|/g) || []).length,
              parts: (text.match(/JQMIN_PART\\|/g) || []).length,
              hasMinuteEnd: /Minute结束/.test(text),
            };
          };
          if (!container) return getStats();
          document.querySelector('#daily-logs-tab')?.click?.();
          let prev = getStats();
          for (let round = 0; round < 40; round++) {
            let stable = 0;
            for (let i = 0; i < 100; i++) {
              container.scrollTop = container.scrollHeight;
              container.dispatchEvent(new Event('scroll', { bubbles: true }));
              await new Promise(r => setTimeout(r, 40));
              const cur = getStats();
              if (cur.logLen === prev.logLen && cur.end === prev.end) {
                stable++;
                if (stable >= 10) break;
              } else {
                stable = 0;
                prev = cur;
              }
            }
            if (stable >= 10 && round >= 2) break;
          }
          return prev;
        }"""
    )


def extract_log_text(page, out_path: Path) -> dict:
    page.evaluate("() => document.querySelector('#daily-logs-tab')?.click?.()")
    stats = scroll_load_all(page)
    total_len = int(stats["logLen"])
    if total_len == 0:
        raise RuntimeError("日志为空")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for start in range(0, total_len, CHUNK_SIZE):
            chunk = page.evaluate(
                """([start, end]) => {
                  const text = document.querySelector('#daily-logs-tab')?.innerText || '';
                  return text.slice(start, end);
                }""",
                [start, start + CHUNK_SIZE],
            )
            f.write(chunk)
            print(f"  chunk {start // CHUNK_SIZE + 1}: {len(chunk)} chars", flush=True)

    stats["out_path"] = str(out_path)
    stats["size_mb"] = round(out_path.stat().st_size / 1024 / 1024, 2)
    return stats


def parse_and_ingest(log_path: Path, *, force: bool = True) -> dict:
    parse_mod = _load_mod(
        "jqmin_parse",
        INGEST_DIR / "解析JQData分钟日志.py",
    )
    from paths import DAILY_SNAP_DIR

    return parse_mod.extract(
        log_path,
        MINUTE_DIR,
        DAILY_SNAP_DIR,
        skip_existing=not force,
    )


def process_one_day(
    page,
    d: date,
    *,
    timeout_sec: int,
    min_slots: int,
    force: bool,
    skip_run: bool,
) -> dict:
    ds = d.isoformat()
    result = {"date": ds, "status": "ok"}

    if day_already_ingested(d) and not force:
        result["status"] = "skipped_exists"
        print(f"[skip] {ds} 已入库", flush=True)
        return result

    if not skip_run:
        print(f"[run] 设置回测日期 {ds} ~ {ds}", flush=True)
        set_backtest_dates(page, d)
        click_compile_run(page)
        print(f"[wait] 等待 Minute结束 / JQMIN 槽位…", flush=True)
        wait_backtest_done(page, d, timeout_sec=timeout_sec, min_slots=min_slots)
    else:
        print(f"[skip-run] 仅提取当前页日志，期望日期 {ds}", flush=True)

    log_path = LOGS_DIR / f"minute_{ds}.txt"
    print(f"[extract] 滚动 #daily-logs-container → {log_path}", flush=True)
    ext = extract_log_text(page, log_path)
    result["extract"] = ext

    print(f"[parse] 解析入库", flush=True)
    stats = parse_and_ingest(log_path, force=True)
    result["parse"] = stats

    if not day_already_ingested(d):
        # 宽松：至少写出了部分分钟
        meta = MINUTE_DIR / f"{d.year}" / ds / "meta.json"
        if not meta.exists():
            result["status"] = "parse_incomplete"
        else:
            result["status"] = "ok"
    return result


def main():
    parser = argparse.ArgumentParser(description="逐日往前：回测 → 提取日志 → 解析入库")
    parser.add_argument("--from", dest="date_from", default=None, help="起始日（含），从此往前")
    parser.add_argument("--to", dest="date_to", default=None, help="结束日（含），须 ≤ --from")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="往前采集的交易日数（默认跳过周末与 A 股节假日休市）",
    )
    parser.add_argument("--dates", default=None, help="显式日期列表，逗号分隔，覆盖 from/to/days")
    parser.add_argument("--page-index", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=DEFAULT_WAIT_SEC, help="单日等待秒数")
    parser.add_argument("--min-slots", type=int, default=MIN_SLOTS_DONE, help="判定完成的最少 JQMIN 槽位数")
    parser.add_argument("--force", action="store_true", help="已入库也重跑")
    parser.add_argument(
        "--keep-weekend",
        action="store_true",
        help="不跳过周末/休市（按自然日推进；一般勿用）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将处理的日期")
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="不点编译运行，只对当前页日志做滚动提取+解析（单日）",
    )
    args = parser.parse_args()

    if args.dates:
        dates = [_parse_date(x.strip()) for x in args.dates.split(",") if x.strip()]
    else:
        if not args.date_from:
            parser.error("需要 --from 或 --dates")
        start = _parse_date(args.date_from)
        end = _parse_date(args.date_to) if args.date_to else None
        if end is None and args.days is None:
            dates = [start]
        else:
            dates = iter_dates_backwards(
                start,
                end=end,
                days=args.days,
                skip_non_trading=not args.keep_weekend,
            )

    print("计划日期:", ", ".join(d.isoformat() for d in dates), flush=True)
    if args.dry_run:
        return

    summary = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        page = find_jq_page(browser, args.page_index)
        page.set_default_timeout(300_000)

        for i, d in enumerate(dates):
            print(f"\n===== [{i + 1}/{len(dates)}] {d.isoformat()} =====", flush=True)
            try:
                r = process_one_day(
                    page,
                    d,
                    timeout_sec=args.timeout,
                    min_slots=args.min_slots,
                    force=args.force,
                    skip_run=args.extract_only,
                )
            except Exception as e:
                r = {"date": d.isoformat(), "status": "error", "error": str(e)}
                print(f"[error] {d}: {e}", file=sys.stderr, flush=True)
            summary.append(r)
            if args.extract_only:
                break

    print("\n===== 汇总 =====", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failed = [x for x in summary if x.get("status") == "error"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
