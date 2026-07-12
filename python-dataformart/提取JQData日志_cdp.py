# -*- coding: utf-8 -*-
"""通过原生 CDP WebSocket 从 Chrome 聚宽页面分块提取日志。"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

JQ_URL_KEY = "joinquant.com/algorithm"
LOGS_DIR = Path(__file__).resolve().parent / "JQData" / "logs"
CHUNK_SIZE = 3_000_000
CDP = "http://127.0.0.1:9222"


class Cdp:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=300)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 300) -> dict:
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result", {})
        raise TimeoutError(f"CDP timeout: {method}")

    def close(self):
        self.ws.close()


def find_target() -> str:
    targets = json.loads(urllib.request.urlopen(f"{CDP}/json/list", timeout=10).read())
    for t in targets:
        if t.get("type") == "page" and JQ_URL_KEY in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("未找到聚宽策略页面")


SCROLL_JS = """
async () => {
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
}
"""


def eval_js(cdp: Cdp, expression: str, await_promise: bool = False, timeout: float = 600) -> str:
    result = cdp.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        },
        timeout=timeout,
    )
    exc = result.get("exceptionDetails")
    if exc:
        raise RuntimeError(json.dumps(exc, ensure_ascii=False))
    value = result.get("result", {}).get("value")
    if value is None and result.get("result", {}).get("type") == "undefined":
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def extract_log(out_path: Path) -> dict:
    cdp = Cdp(find_target())
    try:
        stats_raw = eval_js(cdp, f"({SCROLL_JS})()", await_promise=True, timeout=600)
        stats = json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
        total_len = int(stats["logLen"])
        if total_len == 0:
            raise RuntimeError("日志为空")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for start in range(0, total_len, CHUNK_SIZE):
                end = min(start + CHUNK_SIZE, total_len)
                chunk = eval_js(
                    cdp,
                    f"(() => {{ const t = document.querySelector('#daily-logs-tab')?.innerText || ''; return t.slice({start}, {end}); }})()",
                    timeout=300,
                )
                f.write(chunk)
                print(f"chunk {start // CHUNK_SIZE + 1}: {len(chunk)} chars", flush=True)
    finally:
        cdp.close()

    stats["out_path"] = str(out_path)
    stats["size_mb"] = round(out_path.stat().st_size / 1024 / 1024, 2)
    stats["chunks"] = (total_len + CHUNK_SIZE - 1) // CHUNK_SIZE
    return stats


def main():
    out = LOGS_DIR / (sys.argv[1] if len(sys.argv) > 1 else "jqdata_log.txt")
    stats = extract_log(out)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
