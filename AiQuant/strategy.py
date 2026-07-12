# -*- coding: utf-8 -*-
"""基于 JQData 复现 quant/聚宽策略.py 逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class StrategyConfig:
    stock_nums: int = 4
    bear_pct: float = 0.3
    bear_pos: bool = True
    sell_rank: int = 10
    buy_rank: int = 9
    inc_1d: float = 0.087
    pb_min: float = 0.01
    pb_max: float = 30.0
    weights: list[int] = field(default_factory=lambda: [5, 5, 8, 4, 10])
    choose_time_signal: bool = True
    threshold: float = 0.003
    buy_again: int = 5


@dataclass
class StrategyState:
    cfg: StrategyConfig = field(default_factory=StrategyConfig)
    is_bull: bool = False
    chosen_stock_list: list[str] = field(default_factory=list)
    not_hold: bool = True
    sold_stock: dict[str, int] = field(default_factory=dict)


def filter_candidates(candidates: dict, sold_stock: dict, cfg: StrategyConfig) -> list[str]:
    """对应 get_stock_list：从 candidates 重跑过滤。"""
    result: list[str] = []
    for code, c in candidates.items():
        if code in sold_stock:
            continue
        name = c.get("name") or ""
        if (
            c.get("day_open") == c.get("high_limit")
            or c.get("day_open") == c.get("low_limit")
            or c.get("paused")
            or c.get("is_st")
            or "ST" in name
            or "*" in name
            or "退" in name
            or code.startswith("300")
            or code.startswith("301")
            or code.startswith("688")
        ):
            continue
        pb = c.get("pb_ratio")
        if pb is None or not (cfg.pb_min <= pb <= cfg.pb_max):
            continue
        pct = c.get("pct_change_1d")
        if pct is None or pct >= cfg.inc_1d:
            continue
        result.append(code)
    return result


def update_bull_bear(state: StrategyState, index: dict) -> None:
    """对应 get_bull_bear_signal_minute。"""
    closes = index.get("close_10d") or []
    if len(closes) < 2:
        return
    ma_old = float(np.mean(closes))
    last_close = float(index.get("close_last") or closes[-1])
    cfg = state.cfg
    if state.is_bull:
        if last_close * (1 + cfg.threshold) <= ma_old:
            state.is_bull = False
    else:
        if last_close > ma_old * (1 + cfg.threshold):
            state.is_bull = True


def rank_stocks(state: StrategyState, daily: dict) -> None:
    """对应 get_stock_rank_m_m：对 rank_pool 多因子打分。"""
    cfg = state.cfg
    chosen = set(state.chosen_stock_list)
    rank_pool = daily.get("rank_pool") or {}

    rows = []
    for code, item in rank_pool.items():
        if code not in chosen:
            continue
        if any(item.get(k) is None for k in ("circulating_market_cap", "market_cap", "last_close", "inc_60d", "volume_5d_sum")):
            continue
        rows.append(
            {
                "code": code,
                "circulating_market_cap": float(item["circulating_market_cap"]),
                "market_cap": float(item["market_cap"]),
                "last_close": float(item["last_close"]),
                "inc_60d": float(item["inc_60d"]),
                "volume_5d_sum": float(item["volume_5d_sum"]),
            }
        )
    if not rows:
        state.chosen_stock_list = []
        return

    df = pd.DataFrame(rows).set_index("code")
    s_inc60d = df["inc_60d"]
    s_volume5d = df["volume_5d_sum"]
    s_current = df["last_close"]

    increase60d = np.log(s_inc60d.min()) - np.log(s_inc60d)
    volume5d = np.log(s_volume5d.min()) - np.log(s_volume5d)
    current_price = np.log(s_current.min()) - np.log(s_current)
    circulating_market_cap = np.log(df["circulating_market_cap"].min()) - np.log(df["circulating_market_cap"])
    market_cap = np.log(df["market_cap"].min()) - np.log(df["market_cap"])

    s_total = (
        increase60d * cfg.weights[4]
        + volume5d * cfg.weights[3]
        + current_price * cfg.weights[2]
        + circulating_market_cap * cfg.weights[1]
        + market_cap * cfg.weights[0]
    )
    state.chosen_stock_list = list(s_total.sort_values(ascending=False).index[: cfg.sell_rank])


def get_quote(daily: dict, code: str) -> tuple[float | None, float | None]:
    """返回 (last_price, high_limit)。"""
    c = (daily.get("candidates") or {}).get(code)
    if c:
        return c.get("last_price"), c.get("high_limit")
    r = (daily.get("rank_pool") or {}).get(code)
    if r:
        return r.get("last_close"), None
    return None, None
