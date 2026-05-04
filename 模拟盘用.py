import datetime
import numpy as np
import math
import pandas as pd
pd.set_option('display.max_rows', None)
pd.set_option('display.max_columns', None)


def initialize(context):
    g.is_bull = False
    g.chosen_stock_list = []
    g.not_hold = True
    g.sold_stock = {}
    g.stock_nums = 4
    g.bear_pct = 0.3
    g.bear_pos = True
    g.sell_rank = 10
    g.buy_rank = 9
    g.trade_days = 300
    g.inc_1d = 0.087
    g.pb_min = 0.01
    g.pb_max = 30
    g.weights = [5, 5, 8, 4, 10]
    # 均线择时标的：国金上证指数代码可为 000001.SS 或 000001.XSHG，按平台要求调整
    g.MA = ['000001.SS', 10]
    g.choose_time_signal = True
    g.threshold = 0.003
    g.buy_again = 5
    if not is_trade():
        set_limit_mode('UNLIMITED')


def before_trading_start(context, data):
    # 对应聚宽 run_daily(before_market_open, time='before_open')
    tmp_sold_stock = {}
    for stock in g.sold_stock:
        if g.sold_stock[stock] + 1 < g.buy_again:
            tmp_sold_stock[stock] = g.sold_stock[stock] + 1
    g.sold_stock = tmp_sold_stock
    get_stock_list(context)


def get_stock_list(context):
    # 上市满 g.trade_days 天：国金无 get_all_securities(date)，用上一交易日向前推约 300 天取日期的逻辑，用全 A 或指数成分股替代
    prev_date = get_trading_day(-1)
    check_date = (prev_date - datetime.timedelta(days=g.trade_days)).strftime('%Y%m%d')
    # 国金：用 get_Ashares() 得全 A 股，或 get_index_stocks 得到初选池（此处用全 A 以尽量贴近“全市场”）
    stock_list = get_Ashares(check_date)
    # 市净率 + 流通市值：
    df = get_fundamentals(stock_list, 'valuation',
                          fields=['pb', 'float_value', 'total_value'],
                          is_dataframe=True)
    if 'pb' in df.columns:
        df = df[(df['pb'] >= g.pb_min) & (df['pb'] <= g.pb_max)]
    df = df.dropna(subset=['float_value'])
    df = df.sort_values(by='float_value').head(1000)
    stock_list = list(df.index)
    # 过滤：创业板、科创、ST、停牌、涨跌停、近期卖出
    halt = get_stock_status(stock_list, 'HALT')
    limit_info_raw = check_limit(stock_list) or {}
    stock_list = [s for s in stock_list if not (
        halt.get(s, True) or
        limit_info_raw.get(s, 0) == 1 or
        limit_info_raw.get(s, 0) == -1 or
        s.startswith('300') or s.startswith('301') or s.startswith('688') or
        s in g.sold_stock
    )]
    stock_list = filter_stock_by_status(stock_list, filter_type=["ST", "HALT", "DELISTING"], query_date=None)
    name_map = get_stock_name(stock_list)
    stock_list = [s for s in stock_list if not any(c in name_map.get(s, '') for c in ('退', 'ST', 'S', '*'))]
    # 昨日涨幅 < g.inc_1d
    h = get_history(2, '1d', 'close', security_list=stock_list, include=False, is_dict=True)

    result = []
    for stock in stock_list:
        if stock not in h:
            continue
        close_arr = h[stock]['close']
        if close_arr is None or len(close_arr) < 2:
            continue
        if close_arr[0] <= 0:
            continue
        pct = close_arr[-1] / close_arr[0] - 1
        if pct < g.inc_1d:
            result.append(stock)
    g.chosen_stock_list = result


def get_bull_bear_signal_minute(context):
    # 均线择时：上证指数 10 日收盘均线
    # Ptrade：get_history(..., is_dict=True) 为 {代码: 带字段名的 ndarray}，含 close
    try:
        close_data = get_history(g.MA[1], '1d', 'close', security_list=[g.MA[0]],
                                fq=None, include=True, is_dict=True)
    except Exception:
        return

    if not close_data:
        return

    sec = close_data.get(g.MA[0])
    if sec is None:
        return

    if isinstance(sec, np.ndarray) and sec.dtype.names and 'close' in sec.dtype.names:
        arr = np.asarray(sec['close'], dtype=float)
    else:
        arr = np.asarray(sec, dtype=float)
    if arr.size < g.MA[1]:
        return
    ma_old = float(np.mean(arr))
    last_close = float(arr[-1])
    if g.is_bull:
        if last_close * (1 + g.threshold) <= ma_old:
            g.is_bull = False
    else:
        if last_close > ma_old * (1 + g.threshold):
            g.is_bull = True



def get_stock_rank_m_m(context, data):
    stock_list = g.chosen_stock_list
    if len(stock_list) == 0:
        return
    # Ptrade：get_trading_day 返回 datetime.date
    prev_date = get_trading_day(-1)
    date_str = prev_date.strftime('%Y-%m-%d')
    try:
        df = get_fundamentals(stock_list, 'valuation',
                              fields=['float_value', 'total_value'],
                              date=date_str, is_dataframe=True)
    except Exception:
        return
    if df is None or df.empty:
        return
    df = df.dropna().sort_values(by='float_value').head(100)
    stock_list = list(df.index)
    if not stock_list:
        return
    # 5 日成交量（分钟）：国金 get_history(1200, '1m', 'volume', ...)
    try:
        vol_1m = get_history(1200, '1m', 'volume', security_list=stock_list,
                             fq=None, include=True, is_dict=True)
    except Exception:
        vol_1m = {}

    vol_map = {}
    for s in stock_list:
        bar = vol_1m.get(s) if vol_1m else None
        if bar is None or (isinstance(bar, np.ndarray) and bar.size == 0):
            vol_map[s] = 0.0
            continue
        if isinstance(bar, np.ndarray) and bar.dtype.names and 'volume' in bar.dtype.names:
            v = bar['volume']
        else:
            v = bar
        vol_map[s] = float(np.nansum(np.asarray(v, dtype=float)))
    s_volume5d = pd.Series(vol_map)
    # 60 日涨跌幅与最新价
    try:
        h = get_history(61, '1d', 'close', security_list=stock_list, include=True, is_dict=True)
    except Exception:
        return
    d_inc60d = {}
    d_current = {}
    for s in stock_list:
        if s not in h:
            continue
        hs = h[s]
        cl = hs['close'] if (isinstance(hs, np.ndarray) and hs.dtype.names and 'close' in hs.dtype.names) else hs
        cl = np.asarray(cl)
        if cl.size < 2:
            continue
        d_inc60d[s] = float(cl[-1]) / float(cl[0])
        d_current[s] = float(cl[-1])
    s_inc60d = pd.Series(d_inc60d)
    s_current = pd.Series(d_current)
    # 60日增幅
    increase60d = math.log(s_inc60d.min()) - s_inc60d.apply(math.log)
    # 5日成交量
    volume5d = math.log(s_volume5d.min() + 1e-8) - (s_volume5d + 1e-8).apply(math.log)
    # 当前收盘价格
    current_price = math.log(s_current.min()) - s_current.apply(math.log)
    # 流通市值
    circulating_market_cap = math.log(df['float_value'].min()) - df['float_value'].apply(math.log)
    # 市值
    market_cap = math.log(df['total_value'].min()) - df['total_value'].apply(math.log)
    
    s_total = (
        increase60d * g.weights[4] + volume5d * g.weights[3] + current_price * g.weights[2]
        + circulating_market_cap * g.weights[1] + market_cap * g.weights[0]
    )
    sort_res = s_total.sort_values(ascending=False)
    g.chosen_stock_list = list(sort_res.index)[:g.sell_rank]
    log.info("选股结果：%s" % g.chosen_stock_list)


def clear_position(context):
    for stock in list(context.portfolio.positions.keys()):
        order_target_value(stock, 0)


def close_position(context, security):
    log.info("平仓，卖出：%s" % security)
    order_target_value(security, 0)
    g.sold_stock[security] = 0


def my_adjust_position(context, data):
    if g.choose_time_signal and (not g.is_bull):
        free_value = context.portfolio.portfolio_value * g.bear_pct
        max_percent = 1.3 / g.stock_nums * g.bear_pct
    else:
        free_value = context.portfolio.portfolio_value
        max_percent = 1.3 / g.stock_nums
    buy_cash = free_value / g.stock_nums
    for stock in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[stock]
        if pos.amount <= 0:
            continue
        try:
            limit_status = (check_limit(stock) or {}).get(stock, 0)
        except Exception:
            limit_status = 0
        sell_1 = limit_status != 1  # 未涨停可卖
        sell_2 = stock not in g.chosen_stock_list
        if sell_2 and sell_1:
            close_position(context, stock)
        else:
            current_percent = pos.market_value / context.portfolio.portfolio_value
            if current_percent > max_percent:
                order_target_value(stock, buy_cash)


def my_trade(context, data):
    get_bull_bear_signal_minute(context)
    if g.is_bull:
        log.info("当前市场判断为：牛市")
    else:
        log.info("当前市场判断为：熊市")
    if (g.choose_time_signal and (not g.is_bull) and (not g.bear_pos)) or len(g.chosen_stock_list) < 10:
        clear_position(context)
        g.not_hold = True
    else:
        log.info("gogogogo")
        get_stock_rank_m_m(context, data)
        my_adjust_position(context, data)
        g.not_hold = False


def my_buy(context, data):
    if g.not_hold:
        return
    hold_stocks = [s for s in g.chosen_stock_list if s not in g.sold_stock]
    if g.choose_time_signal and (not g.is_bull):
        free_value = context.portfolio.portfolio_value * g.bear_pct
        min_percent = 0.7 / g.stock_nums * g.bear_pct
    else:
        free_value = context.portfolio.portfolio_value
        min_percent = 0.7 / g.stock_nums
    buy_cash = free_value / g.stock_nums
    position_count = len([p for p in context.portfolio.positions.values() if p.amount > 0])
    # 持仓市值 = 总资产 - 现金
    positions_value = context.portfolio.portfolio_value - context.portfolio.cash
    free_cash = free_value - positions_value
    for stock in hold_stocks:
        if position_count >= g.buy_rank:
            break
        if free_cash <= context.portfolio.portfolio_value / (g.stock_nums * 10):
            continue
        if stock in context.portfolio.positions:
            pos = context.portfolio.positions[stock]
            if pos.market_value / context.portfolio.portfolio_value >= min_percent:
                continue
            to_buy = min(free_cash, buy_cash - pos.market_value)
        else:
            to_buy = min(buy_cash, free_cash)
        if to_buy > 0:
            free_cash = free_cash - to_buy
            # 获取当前价格，将金额换算为股数（取整100股）
            current_price = data[stock]['close']
            cap_price = math.ceil(current_price * 1.1 * 100) / 100
            amount = int(to_buy / cap_price / 100) * 100
            print("股票：",stock,"准备金额：", to_buy, "当前价格：",current_price,"限价：", cap_price, "购买数量：",amount)
            # 上证（.SS）委托市价单时须传保护限价，深证（.SZ）可不传
            if stock.endswith('.SS'):
                order_market(stock, amount, 4, cap_price)
            else:
                order_market(stock, amount, 4)
            # order_value(stock, to_buy)
            if stock not in context.portfolio.positions:
                position_count += 1


def handle_data(context, data):
    # 分钟周期下按 K 线序号分派：盘前在 before_trading_start 已执行；14:47 与 14:52 对应约 227、232（参考 demo 14:45=225, 14:50=230）
    k_num = get_current_kline_count()
    if k_num == 1:
        pass  # 盘前已跑
    elif k_num == 227:  # 14:47
        my_trade(context, data)
    elif k_num == 232:  # 14:52
        my_buy(context, data)