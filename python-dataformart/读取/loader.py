# -*- coding: utf-8 -*-
"""从 JQData 加载 daily 缓存。"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from paths import DAILY_DIR


def list_trade_dates(date_from: str | None = None, date_to: str | None = None) -> list[str]:
    dates: list[str] = []
    for year_dir in sorted(DAILY_DIR.iterdir()):
        if not year_dir.is_dir():
            continue
        for fp in year_dir.glob("*.json"):
            d = fp.stem
            if date_from and d < date_from:
                continue
            if date_to and d > date_to:
                continue
            dates.append(d)
    return sorted(set(dates))


@lru_cache(maxsize=512)
def load_daily(trade_date: str) -> dict:
    fp = DAILY_DIR / trade_date[:4] / f"{trade_date}.json"
    if not fp.exists():
        raise FileNotFoundError(f"缺少 daily 数据: {fp}")
    with fp.open("r", encoding="utf-8") as f:
        return json.load(f)
