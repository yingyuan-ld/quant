# -*- coding: utf-8 -*-
"""
聚宽策略 - Plan-A 数据采集（日志输出版）

用途：按 JQData/schema.json 方案 A 格式，采集「聚宽策略.py」所需的全部远端数据，
      通过 log.info 输出 JSON，回测结束后复制日志到本地解析。

使用方式：
  1. 上传到聚宽研究/回测环境
  2. 设置回测日期范围（如 2025-01-01 ~ 2025-12-31）
  3. 每日 14:50 自动采集并打印日志
  4. 回测结束后，复制日志内容保存为本地 .md 或 .txt
  5. 运行: python 解析JQData日志.py 日志文件.txt

日志输出说明（按顺序）：
  ① 采集开始提示          → 打印当前交易日期
  ② JQDATA_BEGIN|日期     → JSON 数据块开始标记（解析用，勿删）
  ③ JQDATA_PART|日期|...  → JSON 正文分片（拼合后即为 daily/年/日期.json）
  ④ JQDATA_END|日期       → JSON 数据块结束标记（解析用，勿删）
  ⑤ 采集完成摘要          → 打印各阶段股票数量和 JSON 总大小

格式说明：见 quant/JQData/schema.json
"""

from jqdata import *
import json
import datetime
import numpy as np

# ========== 与 聚宽策略.py 保持一致 ==========
TRADE_DAYS = 300          # 上市满多少天才纳入股票池
PB_MIN = 0.01             # 市净率下限
PB_MAX = 30               # 市净率上限
INC_1D = 0.087            # 1日涨幅上限（8.7%），超过则过滤
INDEX_CODE = '000001.XSHG'  # 牛熊择时用的上证指数
MA_DAYS = 10              # 上证几日均线
MINUTE_BARS = 1200        # 约5个交易日的1分钟K线根数
CLOSE_BARS = 61           # 61根日K，用于计算60日涨幅
RANK_POOL_LIMIT = 100     # 精选池最多100只
CANDIDATE_LIMIT = 1000    # 候选池最多1000只
SCHEMA_VERSION = '1.0.0'
PLAN = 'A'
LOG_CHUNK_SIZE = 6000     # 单条日志最大字符数，避免聚宽截断


def initialize(context):
    set_option('use_real_price', True)
    # 14:50 执行：此时分钟成交量、61日K线均已可用，与交易策略 14:47 精选时点接近
    run_daily(collect_plan_a_data, '14:50')


def collect_plan_a_data(context):
    trade_date = context.current_dt.strftime('%Y-%m-%d')
    prev_date = context.previous_date
    check_date = prev_date - datetime.timedelta(days=TRADE_DAYS)

    # 【日志①】采集开始提示，方便在日志里定位每一天的数据块
    log.info('===== Plan-A 数据采集开始: %s =====' % trade_date)

    # ------------------------------------------------------------------
    # 步骤1：拉取股票池（对应 聚宽策略.py → get_all_securities）
    # 写入 payload._meta.pool_size
    # ------------------------------------------------------------------
    pool = list(get_all_securities(date=check_date).index)
    pool_size = len(pool)

    # ------------------------------------------------------------------
    # 步骤2：拉取候选股基本面（对应 get_fundamentals 初筛）
    # PB 在 0.01~30 之间，按流通市值升序取前 1000 只
    # 后续写入 payload.candidates（约1000只股票）
    # ------------------------------------------------------------------
    q_candidates = query(
        valuation.code,
        valuation.pb_ratio,
        valuation.circulating_market_cap,
        valuation.market_cap,
    ).filter(
        valuation.code.in_(pool),
        valuation.pb_ratio.between(PB_MIN, PB_MAX),
    ).order_by(
        valuation.circulating_market_cap.asc()
    ).limit(CANDIDATE_LIMIT)

    fund_df = get_fundamentals(q_candidates).dropna()
    candidate_codes = list(fund_df['code'])

    # ------------------------------------------------------------------
    # 步骤3：拉取实时快照（对应 get_current_data）
    # 字段：name, day_open, high_limit, low_limit, last_price, is_st, paused
    # 写入 payload.candidates.{code} 各字段
    # ------------------------------------------------------------------
    curr_data = get_current_data()

    # ------------------------------------------------------------------
    # 步骤4：拉取候选股 2 日收盘价（对应 history(2, '1d', 'close')）
    # 用于计算 1 日涨跌幅 pct_change_1d，供后续过滤和写入 candidates
    # ------------------------------------------------------------------
    h2 = history(2, '1d', 'close', candidate_codes)
    pct_1d = h2.pct_change().iloc[-1] if len(h2) >= 2 else None

    # 组装 candidates 字典：每只股票一条记录，key 为股票代码
    candidates = {}
    for _, row in fund_df.iterrows():
        code = row['code']
        cd = curr_data[code]
        close_2d = _build_close_2d(h2, code)
        pct = None
        if pct_1d is not None and code in pct_1d.index:
            val = pct_1d[code]
            if val == val:  # 过滤 NaN
                pct = float(val)

        candidates[code] = {
            'code': code,
            'name': cd.name,                                    # 股票名称
            'pb_ratio': _f(row['pb_ratio']),                    # 市净率
            'circulating_market_cap': _f(row['circulating_market_cap']),  # 流通市值(亿)
            'market_cap': _f(row['market_cap']),                # 总市值(亿)
            'day_open': _f(cd.day_open),                        # 开盘价
            'high_limit': _f(cd.high_limit),                    # 涨停价
            'low_limit': _f(cd.low_limit),                      # 跌停价
            'last_price': _f(cd.last_price),                    # 最新价
            'is_st': bool(cd.is_st),                            # 是否ST
            'paused': bool(cd.paused),                          # 是否停牌
            'close_2d': close_2d,                               # 近2日收盘价
            'pct_change_1d': pct,                               # 1日涨跌幅
        }

    # ------------------------------------------------------------------
    # 步骤5：过滤候选股（对应 get_stock_list 中的过滤逻辑）
    # 剔除：涨跌停开盘、停牌、ST、创业板、科创板、1日涨幅>=8.7%
    # 过滤后的数量写入 payload._meta.filtered_count
    # ------------------------------------------------------------------
    filtered_codes = _filter_stocks(candidate_codes, curr_data, pct_1d)

    # ------------------------------------------------------------------
    # 步骤6：拉取精选池（对应 get_stock_rank_m_m 中的 get_fundamentals）
    # 从过滤后的列表中，取流通市值最小的 100 只
    # 后续写入 payload.rank_pool（约100只股票）
    # ------------------------------------------------------------------
    rank_q = query(
        valuation.code,
        valuation.circulating_market_cap,
        valuation.market_cap,
    ).filter(
        valuation.code.in_(filtered_codes),
    ).order_by(
        valuation.circulating_market_cap.asc()
    ).limit(RANK_POOL_LIMIT)

    rank_df = get_fundamentals(rank_q).dropna()
    rank_codes = list(rank_df['code'])

    # ------------------------------------------------------------------
    # 步骤7：拉取精选池分钟成交量（对应 history(1200, '1m', 'volume').sum()）
    # 只存汇总值 volume_5d_sum，不存1200根原始K线
    # ------------------------------------------------------------------
    vol5d = history(MINUTE_BARS, '1m', 'volume', rank_codes).sum()

    # ------------------------------------------------------------------
    # 步骤8：拉取精选池 61 日收盘价（对应 get_bars(61, '1d', 'close')）
    # 用于计算 60 日涨幅 inc_60d 和最新价 last_close
    # ------------------------------------------------------------------
    h61 = get_bars(rank_codes, CLOSE_BARS, '1d', ['close'], include_now=True)

    rank_pool = {}
    for _, row in rank_df.iterrows():
        code = row['code']
        close_list = _extract_close_list(h61, code)
        inc_60d = None
        if len(close_list) >= CLOSE_BARS and close_list[0]:
            inc_60d = close_list[-1] / close_list[0]

        rank_pool[code] = {
            'code': code,
            'circulating_market_cap': _f(row['circulating_market_cap']),  # 流通市值(亿)
            'market_cap': _f(row['market_cap']),                          # 总市值(亿)
            'last_close': close_list[-1] if close_list else None,         # 最新收盘价
            'close_61d': close_list,                                      # 61日收盘价数组
            'inc_60d': inc_60d,                                           # 60日涨幅
            'volume_5d_sum': _f(vol5d[code]) if code in vol5d.index else None,  # 5日成交量之和
        }

    # ------------------------------------------------------------------
    # 步骤9：拉取上证指数 10 日收盘价（对应 get_bull_bear_signal_minute）
    # 写入 payload._index，用于牛熊择时
    # ------------------------------------------------------------------
    index_close = get_bars(INDEX_CODE, MA_DAYS, '1d', 'close', include_now=True)['close']
    index_list = [float(x) for x in index_close]

    # ------------------------------------------------------------------
    # 步骤10：组装完整 JSON（即本地 daily/年/YYYY-MM-DD.json 的内容）
    #
    # payload 结构：
    #   _meta        → 元信息（日期、各阶段数量、采集时间）
    #   _index       → 上证指数10日K线（牛熊择时）
    #   candidates   → 候选股池 ~1000只（盘前初筛用）
    #   rank_pool    → 精选股池 ~100只（14:47多因子打分用）
    # ------------------------------------------------------------------
    payload = {
        '_meta': {
            'version': SCHEMA_VERSION,
            'plan': PLAN,
            'trade_date': trade_date,                               # 交易日期
            'prev_trade_date': prev_date.strftime('%Y-%m-%d'),      # 上一交易日
            'check_date': check_date.strftime('%Y-%m-%d'),          # 上市天数筛选基准日
            'strategy': '聚宽策略.py',
            'pool_size': pool_size,                                 # 股票池总数
            'candidate_count': len(candidates),                     # 候选股数量
            'filtered_count': len(filtered_codes),                  # 过滤后数量
            'rank_pool_count': len(rank_pool),                      # 精选池数量
            'fetched_at': context.current_dt.strftime('%Y-%m-%d %H:%M:%S'),
        },
        '_index': {
            'code': INDEX_CODE,
            'close_10d': index_list,        # 近10日收盘价
            'close_last': index_list[-1] if index_list else None,
            'ma_10': float(np.mean(index_list)) if index_list else None,  # 10日均线
        },
        'candidates': candidates,
        'rank_pool': rank_pool,
    }

    # ------------------------------------------------------------------
    # 步骤11：输出 JSON 到日志
    # 【日志②③④】见 _log_json_payload 函数说明
    # ------------------------------------------------------------------
    content = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    _log_json_payload(trade_date, content)

    # 【日志⑤】采集完成摘要：各阶段数量和 JSON 总大小，便于确认是否完整
    size_kb = len(content.encode('utf-8')) / 1024.0
    log.info('采集完成(日志输出): daily/%s/%s.json' % (trade_date[:4], trade_date))
    log.info('  pool=%s candidates=%s filtered=%s rank_pool=%s size=%.1fKB' % (
        pool_size, len(candidates), len(filtered_codes), len(rank_pool), size_kb))


def _log_json_payload(trade_date, content):
    """
    分片输出 JSON 到日志，本地用 解析JQData日志.py 还原为 daily/年/日期.json

    打印内容：
      JQDATA_BEGIN|2026-05-20
        → 标记开始，后面紧跟该日的 JSON 分片

      JQDATA_PART|2026-05-20|1|62|{"_meta":{...},"_index":{...},...
        → 第1片/共62片，内容是 JSON 字符串的一段（需按序号拼合）

      JQDATA_PART|2026-05-20|2|62|...}
        → 第2片，依此类推

      JQDATA_END|2026-05-20
        → 标记结束，该日数据输出完毕
    """
    # 【日志②】开始标记
    log.info('JQDATA_BEGIN|%s' % trade_date)

    total = (len(content) + LOG_CHUNK_SIZE - 1) // LOG_CHUNK_SIZE
    for i in range(total):
        chunk = content[i * LOG_CHUNK_SIZE:(i + 1) * LOG_CHUNK_SIZE]
        # 【日志③】JSON 正文分片：日期|当前片序号|总片数|JSON片段
        log.info('JQDATA_PART|%s|%s|%s|%s' % (trade_date, i + 1, total, chunk))

    # 【日志④】结束标记
    log.info('JQDATA_END|%s' % trade_date)


def _filter_stocks(stock_list, curr_data, pct_1d):
    """与 聚宽策略.py get_stock_list 相同的过滤规则（不含 sold_stock）"""
    stock_list = [s for s in stock_list if not (
        (curr_data[s].day_open == curr_data[s].high_limit) or  # 涨停开盘
        (curr_data[s].day_open == curr_data[s].low_limit) or    # 跌停开盘
        curr_data[s].paused or                                   # 停牌
        curr_data[s].is_st or                                   # ST
        ('ST' in curr_data[s].name) or
        ('*' in curr_data[s].name) or
        ('退' in curr_data[s].name) or
        s.startswith('300') or                                  # 创业板
        s.startswith('301') or                                  # 创业板
        s.startswith('688')                                     # 科创板
    )]
    if pct_1d is None:
        return stock_list
    return [s for s in stock_list if s in pct_1d.index and pct_1d[s] < INC_1D]


def _build_close_2d(h2, code):
    """从2日收盘价 history 中提取 [前一日, 当日]"""
    if code not in h2.columns:
        return []
    series = h2[code].dropna()
    if series.empty:
        return []
    if len(series) >= 2:
        return [float(series.iloc[-2]), float(series.iloc[-1])]
    return [float(series.iloc[-1])]


def _extract_close_list(h61, code):
    """从61日 get_bars 结果中提取收盘价列表"""
    if code not in h61:
        return []
    closes = h61[code]['close']
    return [float(x) for x in closes]


def _f(value):
    """float 转换，NaN 转为 None"""
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except Exception:
        pass
    return float(value)
