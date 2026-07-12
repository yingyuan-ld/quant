# -*- coding: utf-8 -*-
"""通过 CDP 从当前 Chrome 聚宽页面提取日志并保存（支持虚拟滚动 + 分块）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

JQ_URL_KEY = "joinquant.com/algorithm"
LOGS_DIR = Path(__file__).resolve().parent / "JQData" / "logs"
CHUNK_SIZE = 3_000_000


def _find_pages(browser):
    pages = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if JQ_URL_KEY in pg.url:
                pages.append(pg)
    return pages


def _find_page(browser, page_index: int = 0):
    pages = _find_pages(browser)
    if not pages:
        return None
    if page_index < 0 or page_index >= len(pages):
        raise RuntimeError(f"页签索引 {page_index} 无效，当前聚宽页签数: {len(pages)}")
    return pages[page_index]


def scroll_logs(page) -> dict:
    return page.evaluate(
        """async () => {
          const container = document.querySelector('#daily-logs-container');
          const tab = document.querySelector('#daily-logs-tab');
          const getStats = () => {
            const text = tab ? tab.innerText : '';
            const ends = [...new Set([...text.matchAll(/JQDATA_END\\|(\\d{4}-\\d{2}-\\d{2})/g)].map(m => m[1]))].sort();
            const begins = [...new Set([...text.matchAll(/JQDATA_BEGIN\\|(\\d{4}-\\d{2}-\\d{2})/g)].map(m => m[1]))].sort();
            const incomplete = begins.filter(d => !ends.includes(d));
            return {
              logLen: text.length,
              count: ends.length,
              firstEnd: ends[0] || null,
              lastEnd: ends[ends.length - 1] || null,
              incompleteCount: incomplete.length,
            };
          };
          if (!container) return getStats();
          let prev = getStats();
          for (let round = 0; round < 10; round++) {
            let stable = 0;
            for (let i = 0; i < 100; i++) {
              container.scrollTop = container.scrollHeight;
              container.dispatchEvent(new Event('scroll', { bubbles: true }));
              await new Promise(r => setTimeout(r, 50));
              const cur = getStats();
              if (cur.lastEnd === prev.lastEnd && cur.logLen === prev.logLen) {
                stable++;
                if (stable >= 6) break;
              } else {
                stable = 0;
                prev = cur;
              }
            }
            if (prev.incompleteCount === 0 && round >= 1) break;
          }
          return getStats();
        }"""
    )


def extract_chunk(page, start: int, end: int) -> str:
    return page.evaluate(
        """([start, end]) => {
          const text = document.querySelector('#daily-logs-tab')?.innerText || '';
          return text.slice(start, end);
        }""",
        [start, end],
    )


def extract_log(out_path: Path, page_index: int = 0) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        page = _find_page(browser, page_index)
        if not page:
            raise RuntimeError("未找到聚宽策略页面，请保持页面在 Chrome 中打开")

        page.set_default_timeout(300_000)
        page.evaluate("() => document.querySelector('#daily-logs-tab')?.click?.()")
        stats = scroll_logs(page)
        total_len = stats["logLen"]
        if total_len == 0:
            raise RuntimeError("日志为空，请确认回测已完成且日志标签页有内容")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for start in range(0, total_len, CHUNK_SIZE):
                chunk = extract_chunk(page, start, start + CHUNK_SIZE)
                f.write(chunk)
                print(f"chunk {start // CHUNK_SIZE + 1}: {len(chunk)} chars", flush=True)

    stats["out_path"] = str(out_path)
    stats["size_mb"] = round(out_path.stat().st_size / 1024 / 1024, 2)
    stats["chunks"] = (total_len + CHUNK_SIZE - 1) // CHUNK_SIZE
    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="从聚宽 Chrome 页签提取 JQData 日志")
    parser.add_argument("out", nargs="?", default="jqdata_log.txt", help="输出文件名（相对 JQData/logs）")
    parser.add_argument("--page-index", type=int, default=0, help="聚宽页签索引，0 为第一个")
    args = parser.parse_args()

    out = LOGS_DIR / args.out
    stats = extract_log(out, page_index=args.page_index)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
