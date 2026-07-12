# -*- coding: utf-8 -*-
"""
基于 JQData 本地数据的量化回测引擎（AiQuant）。

复现 quant/聚宽策略.py 的选股与调仓逻辑，数据读取自 ../JQData/daily/。

用法:
  python backtest.py
  python backtest.py --from 2020-01-01 --to 2024-12-31
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_QUANT_ROOT = Path(__file__).resolve().parent.parent
_READ_DIR = _QUANT_ROOT / "python-dataformart" / "读取"
if str(_READ_DIR) not in sys.path:
    sys.path.insert(0, str(_READ_DIR))

from loader import list_trade_dates, load_daily
from strategy import StrategyConfig, StrategyState, filter_candidates, get_quote, rank_stocks, update_bull_bear

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# 回测结果字段中文说明（写入 JSON 的 _字段说明）
SUMMARY_FIELD_DOCS = {
    "start_date": "回测起始交易日",
    "end_date": "回测结束交易日",
    "days": "回测交易日数量",
    "initial_cash": "初始资金（元）",
    "final_value": "期末总资产（元）",
    "total_return": "总收益率（%）",
    "max_drawdown_pct": "最大回撤（%）",
    "trade_count": "成交笔数（买+卖）",
}

EQUITY_FIELD_DOCS = {
    "date": "交易日期",
    "total_value": "当日收盘总资产（现金+持仓市值，元）",
    "cash": "当日收盘现金余额（元）",
    "positions": "当日收盘持仓股票只数",
    "is_bull": "当日择时信号：true=牛市，false=熊市",
    "not_hold": "当日是否空仓：true=空仓，false=持仓",
}

TRADE_FIELD_DOCS = {
    "date": "成交日期",
    "code": "股票代码",
    "side": "买卖方向：buy=买入，sell=卖出",
    "price": "成交价格（元）",
    "value": "成交金额（元）",
}


def _with_field_docs(data: dict, docs: dict) -> dict:
    return {"_字段说明": docs, **data}


def _equity_output(records: list[dict]) -> dict:
    return {"_字段说明": EQUITY_FIELD_DOCS, "records": records}


@dataclass
class Position:
    shares: float = 0.0
    cost: float = 0.0

    @property
    def value(self) -> float:
        return self.shares * self.cost

    def market_value(self, price: float) -> float:
        return self.shares * price


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def positions_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for code, pos in self.positions.items():
            if pos.shares <= 0:
                continue
            price = prices.get(code, pos.cost)
            total += pos.shares * price
        return total

    def total_value(self, prices: dict[str, float]) -> float:
        return self.cash + self.positions_value(prices)

    def active_codes(self) -> list[str]:
        return [c for c, p in self.positions.items() if p.shares > 0]


class BacktestEngine:
    def __init__(self, initial_cash: float = 1_000_000.0, cfg: StrategyConfig | None = None):
        self.portfolio = Portfolio(cash=initial_cash)
        self.state = StrategyState(cfg=cfg or StrategyConfig())
        self.initial_cash = initial_cash
        self.equity_curve: list[dict] = []
        self.trades: list[dict] = []

    def _collect_prices(self, daily: dict, codes: set[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for code in codes:
            price, _ = get_quote(daily, code)
            if price is not None and price > 0:
                prices[code] = float(price)
        return prices

    def _order_target_value(self, daily: dict, code: str, target_value: float, trade_date: str) -> None:
        price, _ = get_quote(daily, code)
        if price is None or price <= 0:
            return
        pos = self.portfolio.positions.setdefault(code, Position())
        current_value = pos.shares * price
        delta_value = target_value - current_value
        if abs(delta_value) < 1:
            return
        if delta_value < 0:
            sell_value = min(-delta_value, pos.shares * price)
            sell_shares = sell_value / price
            if sell_shares <= 0:
                return
            pos.shares -= sell_shares
            self.portfolio.cash += sell_value
            if pos.shares <= 1e-8:
                pos.shares = 0.0
            self.trades.append({"date": trade_date, "code": code, "side": "sell", "price": price, "value": sell_value})
            if target_value <= 0:
                self.state.sold_stock[code] = 0
        else:
            buy_value = min(delta_value, self.portfolio.cash)
            if buy_value <= 0:
                return
            buy_shares = buy_value / price
            pos.shares += buy_shares
            pos.cost = price
            self.portfolio.cash -= buy_value
            self.trades.append({"date": trade_date, "code": code, "side": "buy", "price": price, "value": buy_value})

    def _order_value(self, daily: dict, code: str, value: float, trade_date: str) -> None:
        if value <= 0:
            return
        price, _ = get_quote(daily, code)
        if price is None or price <= 0:
            return
        pos = self.portfolio.positions.setdefault(code, Position())
        buy_value = min(value, self.portfolio.cash)
        if buy_value <= 0:
            return
        buy_shares = buy_value / price
        pos.shares += buy_shares
        pos.cost = price
        self.portfolio.cash -= buy_value
        self.trades.append({"date": trade_date, "code": code, "side": "buy", "price": price, "value": buy_value})

    def _close_position(self, daily: dict, code: str, trade_date: str) -> None:
        self._order_target_value(daily, code, 0.0, trade_date)

    def _clear_position(self, daily: dict, trade_date: str) -> None:
        for code in list(self.portfolio.active_codes()):
            self._close_position(daily, code, trade_date)

    def _before_market_open(self, daily: dict) -> None:
        st = self.state
        tmp_sold: dict[str, int] = {}
        for stock, days in st.sold_stock.items():
            if days + 1 < st.cfg.buy_again:
                tmp_sold[stock] = days + 1
        st.sold_stock = tmp_sold
        st.chosen_stock_list = filter_candidates(daily.get("candidates") or {}, st.sold_stock, st.cfg)

    def _my_trade(self, daily: dict, trade_date: str) -> None:
        st = self.state
        cfg = st.cfg
        update_bull_bear(st, daily.get("_index") or {})

        if (cfg.choose_time_signal and (not st.is_bull) and (not cfg.bear_pos)) or len(st.chosen_stock_list) < 10:
            self._clear_position(daily, trade_date)
            st.not_hold = True
            return

        rank_stocks(st, daily)
        self._my_adjust_position(daily, trade_date)
        st.not_hold = False

    def _my_adjust_position(self, daily: dict, trade_date: str) -> None:
        st = self.state
        cfg = st.cfg
        codes = set(self.portfolio.active_codes()) | set(st.chosen_stock_list)
        prices = self._collect_prices(daily, codes)
        total_value = self.portfolio.total_value(prices)

        if cfg.choose_time_signal and (not st.is_bull):
            free_value = total_value * cfg.bear_pct
            max_percent = 1.3 / cfg.stock_nums * cfg.bear_pct
        else:
            free_value = total_value
            max_percent = 1.3 / cfg.stock_nums
        buy_cash = free_value / cfg.stock_nums

        for code in list(self.portfolio.active_codes()):
            price, high_limit = get_quote(daily, code)
            if price is None:
                continue
            sell_1 = high_limit is None or price < high_limit
            sell_2 = code not in st.chosen_stock_list
            if sell_2 and sell_1:
                self._close_position(daily, code, trade_date)
            else:
                pos = self.portfolio.positions[code]
                current_percent = (pos.shares * price) / total_value if total_value else 0
                if current_percent > max_percent:
                    self._order_target_value(daily, code, buy_cash, trade_date)

    def _my_buy(self, daily: dict, trade_date: str) -> None:
        st = self.state
        cfg = st.cfg
        if st.not_hold:
            return

        hold_stocks = [s for s in st.chosen_stock_list if s not in st.sold_stock]
        codes = set(self.portfolio.active_codes()) | set(hold_stocks)
        prices = self._collect_prices(daily, codes)
        total_value = self.portfolio.total_value(prices)

        if cfg.choose_time_signal and (not st.is_bull):
            free_value = total_value * cfg.bear_pct
            min_percent = 0.7 / cfg.stock_nums * cfg.bear_pct
        else:
            free_value = total_value
            min_percent = 0.7 / cfg.stock_nums
        buy_cash = free_value / cfg.stock_nums

        for stock in hold_stocks:
            if len(self.portfolio.active_codes()) >= cfg.buy_rank:
                break
            prices = self._collect_prices(daily, set(self.portfolio.active_codes()) | {stock})
            total_value = self.portfolio.total_value(prices)
            positions_value = self.portfolio.positions_value(prices)
            free_cash = free_value - positions_value
            if free_cash <= total_value / (cfg.stock_nums * 10):
                continue

            price, _ = get_quote(daily, stock)
            if price is None or price <= 0:
                continue

            if stock in self.portfolio.positions and self.portfolio.positions[stock].shares > 0:
                pos = self.portfolio.positions[stock]
                current_percent = (pos.shares * price) / total_value if total_value else 0
                if current_percent >= min_percent:
                    continue
                to_buy = min(free_cash, buy_cash - pos.shares * price)
                if to_buy <= 0:
                    continue
            else:
                to_buy = min(buy_cash, free_cash)

            self._order_value(daily, stock, to_buy, trade_date)

    def run_day(self, trade_date: str) -> None:
        daily = load_daily(trade_date)
        self._before_market_open(daily)
        self._my_trade(daily, trade_date)
        self._my_buy(daily, trade_date)

        codes = set(self.portfolio.active_codes())
        prices = self._collect_prices(daily, codes)
        total = self.portfolio.total_value(prices)
        self.equity_curve.append(
            {
                "date": trade_date,
                "total_value": round(total, 2),
                "cash": round(self.portfolio.cash, 2),
                "positions": len(self.portfolio.active_codes()),
                "is_bull": self.state.is_bull,
                "not_hold": self.state.not_hold,
            }
        )

    def run(self, date_from: str | None = None, date_to: str | None = None) -> dict:
        dates = list_trade_dates(date_from, date_to)
        if not dates:
            raise RuntimeError("指定区间无 daily 数据")
        for d in dates:
            self.run_day(d)
        return self.summary()

    def summary(self) -> dict:
        if not self.equity_curve:
            return {}
        start = self.equity_curve[0]["total_value"]
        end = self.equity_curve[-1]["total_value"]
        peak = start
        max_dd = 0.0
        for row in self.equity_curve:
            v = row["total_value"]
            peak = max(peak, v)
            dd = (peak - v) / peak if peak else 0
            max_dd = max(max_dd, dd)
        return {
            "start_date": self.equity_curve[0]["date"],
            "end_date": self.equity_curve[-1]["date"],
            "days": len(self.equity_curve),
            "initial_cash": self.initial_cash,
            "final_value": round(end, 2),
            "total_return": round((end / start - 1) * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "trade_count": len(self.trades),
        }


def main():
    parser = argparse.ArgumentParser(description="AiQuant 本地回测（数据来自 JQData）")
    parser.add_argument("--from", dest="date_from", default="2020-01-01")
    parser.add_argument("--to", dest="date_to", default="2024-12-31")
    parser.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金")
    args = parser.parse_args()

    engine = BacktestEngine(initial_cash=args.cash)
    summary = engine.run(args.date_from, args.date_to)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{args.date_from}_{args.date_to}"
    curve_path = RESULTS_DIR / f"equity_{tag}.json"
    summary_path = RESULTS_DIR / f"summary_{tag}.json"

    with curve_path.open("w", encoding="utf-8") as f:
        json.dump(_equity_output(engine.equity_curve), f, ensure_ascii=False, indent=2)
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(_with_field_docs(summary, SUMMARY_FIELD_DOCS), f, ensure_ascii=False, indent=2)

    trades_path = RESULTS_DIR / f"trades_{tag}.json"
    with trades_path.open("w", encoding="utf-8") as f:
        json.dump(_with_field_docs({"records": engine.trades}, TRADE_FIELD_DOCS), f, ensure_ascii=False, indent=2)

    print(json.dumps(_with_field_docs(summary, SUMMARY_FIELD_DOCS), ensure_ascii=False, indent=2))
    print(f"净值曲线: {curve_path}")
    print(f"摘要: {summary_path}")
    print(f"成交明细: {trades_path}")


if __name__ == "__main__":
    main()
