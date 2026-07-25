# -*- coding: utf-8 -*-
"""
聚宽 · 分钟级全市场数据采集（按分钟截面入库）

参考：入库/joinquant-api/主要API速查.md

重要：聚宽环境只能靠日志导出数据（无可靠文件下载）。
      本脚本把「每个分钟的全市场截面」做成与本地落盘一致的 JSON：
        {_meta: {trade_date, time, ...}, bars: {code: {ohlcv...}}}
      再 zlib + base64 分片写入 log.info。
      回测结束后用本地脚本抽日志再解析，不要在网页里「打开全部日志」（会卡死）。

流程：
  1. 上传本文件到聚宽回测，设日期区间
  2. 每日 15:00：按股票批拉取 → 折成 minutes[time][code]
     → 按分钟发出 JQMIN_*（键=HHMM）+ 可选日频 JQMIN_*(键=daily)
  3. 本地：提取JQData日志.py 保存日志 txt
  4. 本地：python 解析JQData分钟日志.py 日志.txt
     → JQData/minute/年/日期/HHMM.json
     → JQData/daily_snap/年/日期.json

建议：
  - 先 MAX_STOCKS=20 验证
  - 全市场用 STOCK_SHARDS=4~8 分多次回测（日志更小、网页更不易炸）
  - STOCK_BATCH_SIZE 默认 40（仅影响 API 拉取批大小，落盘仍按分钟）
"""

from jqdata import *
import json
import math
import datetime
import zlib
import base64

# ======================== 可配置 ========================
BAR_FIELDS = ['open', 'high', 'low', 'close', 'volume', 'money']
MINUTE_BARS = 240

# API 拉取批大小（与落盘维度无关）
STOCK_BATCH_SIZE = 40
MAX_STOCKS = 0                # >0 只采前 N 只；全市场=0
STOCK_SHARDS = 1              # 全市场建议 4~8，多次跑
SHARD_ID = 0

COLLECT_DAILY_SNAP = True
COLLECT_INDEX_MINUTE = True
INDEX_CODE = '000001.XSHG'
SECURITY_TYPES = ['stock']

FQ_NONE = None
SCHEMA_VERSION = '2.3.0'
PLAN = 'M'
LOG_CHUNK_SIZE = 5000
ZLIB_LEVEL = 6


def initialize(context):
    set_option('use_real_price', True)
    log.info('Minute采集(按分钟截面) batch=%s max=%s shard=%s/%s' % (
        STOCK_BATCH_SIZE, MAX_STOCKS, SHARD_ID, STOCK_SHARDS))
    log.info('提示: 勿在网页展开全部日志；用本地提取脚本保存后再解析')
    run_daily(collect_minute_data, time='15:00', reference_security='000300.XSHG')


def collect_minute_data(context):
    trade_date = context.current_dt.strftime('%Y-%m-%d')
    prev_date = context.previous_date.strftime('%Y-%m-%d')
    fetched_at = context.current_dt.strftime('%Y-%m-%d %H:%M:%S')
    end_dt = datetime.datetime(
        context.current_dt.year, context.current_dt.month, context.current_dt.day, 15, 0, 0
    )
    start_dt = datetime.datetime(
        context.current_dt.year, context.current_dt.month, context.current_dt.day, 9, 0, 0
    )

    all_codes = list(get_all_securities(types=SECURITY_TYPES, date=trade_date).index)
    all_codes = _apply_shard(sorted(all_codes))
    if MAX_STOCKS > 0:
        all_codes = all_codes[:MAX_STOCKS]

    batches = _chunk(all_codes, STOCK_BATCH_SIZE)
    batch_total = len(batches)

    log.info('Minute开始 %s stocks=%s api_batches=%s end_dt=%s layout=by_minute' % (
        trade_date, len(all_codes), batch_total, end_dt))

    # minutes[time][code] = {open,high,low,close,volume,money}
    minutes = {}
    merged_daily = {}
    curr_data = get_current_data() if COLLECT_DAILY_SNAP else None

    if COLLECT_INDEX_MINUTE:
        idx_bars = fetch_minute_bars([INDEX_CODE], start_dt, end_dt)
        fold_series_to_minutes(idx_bars, minutes)
        log.info('指数分钟已折入 %s' % INDEX_CODE)

    for batch_index, batch in enumerate(batches, start=1):
        log.info('拉取 %s %s/%s n=%s' % (trade_date, batch_index, batch_total, len(batch)))
        series = fetch_minute_bars(batch, start_dt, end_dt)
        fold_series_to_minutes(series, minutes)
        if COLLECT_DAILY_SNAP:
            merged_daily.update(fetch_daily_snap(batch, trade_date, prev_date, curr_data))

    times = sorted(minutes.keys())
    minute_total = len(times)
    log.info('折算完成 %s minutes=%s stocks≈%s ，开始按分钟写日志' % (
        trade_date, minute_total,
        max((len(minutes[t]) for t in times), default=0)))

    base_meta = {
        'version': SCHEMA_VERSION,
        'plan': PLAN,
        'layout': 'by_minute',
        'freq': '1m',
        'fields': list(BAR_FIELDS),
        'fq': 'none',
        'encoding': 'zlib_b64',
        'trade_date': trade_date,
        'prev_trade_date': prev_date,
        'fetched_at': fetched_at,
        'start_dt': start_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'end_dt': end_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'shard_id': SHARD_ID,
        'shard_total': STOCK_SHARDS,
        'api': 'get_bars|get_price',
        'minute_total': minute_total,
    }

    for i, t in enumerate(times):
        bars = minutes[t]
        hhmm = time_to_hhmm(t)
        meta = dict(base_meta)
        meta.update({
            'time': t,
            'hhmm': hhmm,
            'minute_index': i,
            'stock_count': len(bars),
            'kind': 'minute',
        })
        # 与本地 HHMM.json 同构
        payload = {'_meta': meta, 'bars': bars}
        content = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        emit_log_payload(trade_date, hhmm, minute_total, content, len(bars))

    if COLLECT_DAILY_SNAP and merged_daily:
        daily_meta = dict(base_meta)
        daily_meta.update({
            'kind': 'daily',
            'stock_count': len(merged_daily),
            'time': None,
            'hhmm': 'daily',
        })
        payload = {'_meta': daily_meta, 'daily': merged_daily}
        content = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        emit_log_payload(trade_date, 'daily', 1, content, len(merged_daily))

    log.info('Minute结束 %s minutes=%s shard=%s 请本地提取后运行 解析JQData分钟日志.py' % (
        trade_date, minute_total, SHARD_ID))


def fold_series_to_minutes(bars_by_code, minutes):
    """{code: {time:[], open:[], ...}} → minutes[time][code] = {ohlcv}"""
    for code, series in (bars_by_code or {}).items():
        if not isinstance(series, dict):
            continue
        times = series.get('time') or []
        for i, t in enumerate(times):
            if not t:
                continue
            bar = {}
            for f in BAR_FIELDS:
                vals = series.get(f)
                if isinstance(vals, list) and i < len(vals):
                    bar[f] = vals[i]
            if not bar:
                continue
            if t not in minutes:
                minutes[t] = {}
            minutes[t][str(code)] = bar


def time_to_hhmm(t):
    """'09:31' / datetime → '0931'"""
    if t is None:
        return ''
    if hasattr(t, 'strftime'):
        return t.strftime('%H%M')
    s = str(t)
    if ' ' in s:
        s = s.split(' ')[-1]
    if len(s) >= 5 and s[2] == ':':
        return s[:2] + s[3:5]
    return s.replace(':', '')


# --------------------- 行情拉取（官方 API） ---------------------

def fetch_minute_bars(codes, start_dt, end_dt):
    """
    拉取 [start_dt, end_dt] 的 1 分钟 OHLCV（股票×时间序列，随后折成分钟截面）。

    优先 get_bars；回退 get_price（end_date 必须带时分）。
    """
    if not codes:
        return {}

    try:
        raw = get_bars(
            codes,
            count=MINUTE_BARS,
            unit='1m',
            fields=['date'] + list(BAR_FIELDS),
            include_now=True,
            end_dt=end_dt,
            fq_ref_date=FQ_NONE,
            df=True,
        )
        parsed = bars_from_bars_df(raw, codes)
        if parsed:
            return parsed
        log.info('get_bars 解析为空，尝试 get_price')
    except Exception as e:
        log.info('get_bars 失败: %s ，尝试 get_price' % e)

    try:
        df = get_price(
            codes,
            start_date=start_dt,
            end_date=end_dt,
            frequency='1m',
            fields=list(BAR_FIELDS),
            skip_paused=False,
            fq=FQ_NONE,
            panel=False,
            fill_paused=False,
        )
        return bars_from_price_df(df, codes)
    except Exception as e:
        log.info('get_price 失败: %s' % e)
        return {}


def bars_from_bars_df(df, codes):
    """get_bars(df=True) → {code: {time, open, ...}}"""
    out = {}
    if df is None:
        return out
    try:
        if len(df) == 0:
            return out
    except Exception:
        return out

    work = df.reset_index()
    cols_lower = {str(c).lower(): c for c in work.columns}
    rename = {}
    if 'code' in cols_lower:
        rename[cols_lower['code']] = 'code'
    elif 'level_0' in cols_lower:
        rename[cols_lower['level_0']] = 'code'
    for key in ('date', 'time', 'datetime', 'level_1', 'index'):
        if key in cols_lower:
            rename[cols_lower[key]] = 'time'
            break
    if rename:
        work = work.rename(columns=rename)

    if 'time' not in work.columns:
        return out

    if 'code' not in work.columns:
        if len(codes) == 1:
            work['code'] = codes[0]
        else:
            return out

    for code, g in work.groupby('code'):
        try:
            g = g.sort_values('time')
        except Exception:
            pass
        item = _row_to_bar_item(g)
        if item is not None:
            out[str(code)] = item
    return out


def bars_from_price_df(df, codes):
    """get_price(panel=False) → {code: {time, open, ...}}"""
    out = {}
    if df is None:
        return out
    try:
        if len(df) == 0:
            return out
    except Exception:
        return out

    work = df.reset_index()
    cols_lower = {str(c).lower(): c for c in work.columns}
    rename = {}
    if 'code' in cols_lower:
        rename[cols_lower['code']] = 'code'
    elif 'level_0' in cols_lower:
        rename[cols_lower['level_0']] = 'code'
    for key in ('time', 'date', 'datetime', 'index', 'level_1'):
        if key in cols_lower:
            rename[cols_lower[key]] = 'time'
            break
    if rename:
        work = work.rename(columns=rename)

    if 'time' not in work.columns:
        return out
    if 'code' not in work.columns:
        if len(codes) == 1:
            work['code'] = codes[0]
        else:
            return out

    for code, g in work.groupby('code'):
        try:
            g = g.sort_values('time')
        except Exception:
            pass
        item = _row_to_bar_item(g)
        if item is not None:
            out[str(code)] = item
    return out


def _row_to_bar_item(g):
    times = [fmt_time(t) for t in g['time'].tolist()]
    item = {'time': times}
    for f in BAR_FIELDS:
        if f in g.columns:
            item[f] = [to_float(x) for x in g[f].tolist()]
        else:
            item[f] = [None] * len(times)
    if not any(x is not None for x in item['close']):
        return None
    return item


# --------------------- 日频快照 ---------------------

def fetch_daily_snap(codes, trade_date, prev_date, curr_data):
    snap = {}
    if not codes:
        return snap

    fund_map = load_valuation(codes, trade_date, prev_date)
    day_map = load_daily_ohlcv(codes, trade_date)
    close2_map = load_close_2d(codes, prev_date)

    for code in codes:
        cd = curr_data[code] if curr_data is not None else None
        fund = fund_map.get(code)
        day = day_map.get(code)
        c2 = close2_map.get(code) or []
        pct = None
        if len(c2) >= 2 and c2[0] not in (None, 0):
            try:
                pct = float(c2[-1]) / float(c2[0]) - 1.0
            except Exception:
                pct = None

        snap[code] = {
            'code': code,
            'name': getattr(cd, 'name', None) if cd is not None else None,
            'day_open': to_float(getattr(cd, 'day_open', None)) if cd is not None else None,
            'high_limit': to_float(getattr(cd, 'high_limit', None)) if cd is not None else None,
            'low_limit': to_float(getattr(cd, 'low_limit', None)) if cd is not None else None,
            'last_price': to_float(getattr(cd, 'last_price', None)) if cd is not None else None,
            'is_st': bool(getattr(cd, 'is_st', False)) if cd is not None else None,
            'paused': bool(getattr(cd, 'paused', False)) if cd is not None else None,
            'pb_ratio': to_float(fund['pb_ratio']) if fund is not None else None,
            'pe_ratio': to_float(fund['pe_ratio']) if fund is not None else None,
            'circulating_market_cap': to_float(fund['circulating_market_cap']) if fund is not None else None,
            'market_cap': to_float(fund['market_cap']) if fund is not None else None,
            'open': to_float(day['open']) if day is not None else None,
            'high': to_float(day['high']) if day is not None else None,
            'low': to_float(day['low']) if day is not None else None,
            'close': to_float(day['close']) if day is not None else None,
            'volume': to_float(day['volume']) if day is not None else None,
            'money': to_float(day['money']) if day is not None else None,
            'close_2d': c2,
            'pct_change_1d': pct,
        }
    return snap


def load_valuation(codes, trade_date, prev_date):
    fund_map = {}
    for d in (trade_date, prev_date):
        try:
            q = query(
                valuation.code,
                valuation.pb_ratio,
                valuation.pe_ratio,
                valuation.circulating_market_cap,
                valuation.market_cap,
            ).filter(valuation.code.in_(codes))
            df = get_fundamentals(q, date=d)
            if df is not None and len(df) > 0:
                for _, row in df.iterrows():
                    fund_map[row['code']] = row
                if fund_map:
                    return fund_map
        except Exception as e:
            log.info('get_fundamentals(%s) 失败: %s' % (d, e))
    return fund_map


def load_daily_ohlcv(codes, trade_date):
    day_map = {}
    try:
        df = get_price(
            codes,
            count=1,
            end_date=trade_date,
            frequency='daily',
            fields=list(BAR_FIELDS),
            skip_paused=False,
            fq=FQ_NONE,
            panel=False,
            fill_paused=False,
        )
        if df is None or len(df) == 0:
            return day_map
        work = df.reset_index()
        cols_lower = {str(c).lower(): c for c in work.columns}
        if 'code' not in work.columns:
            if 'level_0' in cols_lower:
                work = work.rename(columns={cols_lower['level_0']: 'code'})
            elif len(codes) == 1:
                work['code'] = codes[0]
        for _, row in work.iterrows():
            if 'code' in row:
                day_map[row['code']] = row
    except Exception as e:
        log.info('日线 get_price 失败: %s' % e)
    return day_map


def load_close_2d(codes, prev_date):
    out = {}
    try:
        df = get_price(
            codes,
            count=2,
            end_date=prev_date,
            frequency='daily',
            fields=['close'],
            skip_paused=False,
            fq=FQ_NONE,
            panel=False,
            fill_paused=False,
        )
        if df is None or len(df) == 0:
            return out
        work = df.reset_index()
        cols_lower = {str(c).lower(): c for c in work.columns}
        if 'code' not in work.columns:
            if 'level_0' in cols_lower:
                work = work.rename(columns={cols_lower['level_0']: 'code'})
            elif len(codes) == 1:
                work['code'] = codes[0]
        if 'code' not in work.columns or 'close' not in work.columns:
            return out
        for code, g in work.groupby('code'):
            closes = [to_float(x) for x in g['close'].tolist()]
            out[str(code)] = closes[-2:] if len(closes) >= 2 else closes
    except Exception as e:
        log.info('close_2d 失败: %s' % e)
    return out


# --------------------- 输出（仅日志） ---------------------

def emit_log_payload(trade_date, slot_key, slot_total, content, rows):
    """
    JSON → zlib → base64 → 分片 log。

    标记（slot_key = HHMM 或 daily）：
      JQMIN_BEGIN|日期|键|总数|zlib_b64|原始KB|压缩KB|rows
      JQMIN_PART|日期|键|片号|总片|base64片段
      JQMIN_END|日期|键|总数
    """
    raw_kb = len(content.encode('utf-8')) / 1024.0
    packed = base64.b64encode(
        zlib.compress(content.encode('utf-8'), ZLIB_LEVEL)
    ).decode('ascii')
    packed_kb = len(packed) / 1024.0

    log.info('JQMIN_BEGIN|%s|%s|%s|zlib_b64|%.1f|%.1f|%s' % (
        trade_date, slot_key, slot_total, raw_kb, packed_kb, rows))

    total = max(1, (len(packed) + LOG_CHUNK_SIZE - 1) // LOG_CHUNK_SIZE)
    for i in range(total):
        chunk = packed[i * LOG_CHUNK_SIZE:(i + 1) * LOG_CHUNK_SIZE]
        log.info('JQMIN_PART|%s|%s|%s|%s|%s' % (
            trade_date, slot_key, i + 1, total, chunk))

    log.info('JQMIN_END|%s|%s|%s' % (trade_date, slot_key, slot_total))


# --------------------- 工具 ---------------------

def _apply_shard(codes):
    if STOCK_SHARDS <= 1:
        return codes
    shard_id = max(0, min(SHARD_ID, STOCK_SHARDS - 1))
    size = int(math.ceil(len(codes) / float(STOCK_SHARDS)))
    start = shard_id * size
    return codes[start:start + size]


def _chunk(items, size):
    if not items:
        return []
    return [items[i:i + size] for i in range(0, len(items), size)]


def fmt_time(t):
    if t is None:
        return ''
    if hasattr(t, 'strftime'):
        return t.strftime('%H:%M')
    s = str(t)
    if ' ' in s:
        s = s.split(' ')[-1]
    if len(s) >= 5 and s[2] == ':':
        return s[:5]
    return s


def to_float(value):
    if value is None:
        return None
    try:
        f = float(value)
        if f != f:
            return None
        return f
    except Exception:
        return None
