# 聚宽 JoinQuant API 本地速查

- **来源**：[聚宽 API 文档](https://www.joinquant.com/help/api/help#name:api) / [www.joinquant.com/api](https://www.joinquant.com/api)
- **本地全文摘录**：同目录 `官网API全文摘录.md`（约 300KB，可全文搜索）
- **抓取日期**：2026-07-21
- **说明**：下文只整理**策略研究 / 数据采集 / 回测对齐**最常用的接口；带 ♠ 的为回测/模拟专用。

---

## 0. 通用约定（必读）

| 点 | 说明 |
|----|------|
| 代码格式 | `000001.XSHE`（深），`600000.XSHG`（沪） |
| volume | 单位是**股** |
| 1 分钟 K | 后对齐；一天约 **240** 根；无 09:30，从 **09:31** 到 **15:00**；09:31 的 open 含集合竞价 |
| 真实价格 | 强烈建议 `set_option('use_real_price', True)`，避免复权未来函数 |
| 勿跨日缓存 | 开启真实价格后，`history`/`get_price` 等返回的是「站在当天」的前复权价，**不要跨日期缓存** |
| end_date 分钟陷阱 | `get_price` 取分钟时若 `end_date` 只有日期，日内当作 `00:00:00`，**不含当天盘中** |

与本仓库关系：

- `聚宽策略.py`：`history` / `get_bars` / `get_fundamentals` / `get_current_data` / `order_*`
- `入库/聚宽策略-数据采集.py`：`get_price(1m)` / `get_bars` / `get_all_securities` / `get_fundamentals` / `write_file`

---

## 1. 策略骨架与定时

### `initialize` / `run_daily`

```python
def initialize(context):
    set_option('use_real_price', True)
    run_daily(before_market_open, time='before_open', reference_security='000300.XSHG')
    run_daily(my_trade, '14:47')
    run_daily(my_buy, '14:52')
```

| 参数 | 含义 |
|------|------|
| `time` | `'every_bar'` / `'before_open'` / `'after_close'` / `'14:50'` 等 |
| 精确到秒 | 如 `14:50` → `14:50:00` 执行 |
| `reference_security` | 交易日历参照标的 |

---

## 2. 设置类

### `set_option('use_real_price', True)`

- 成交用真实价；数据 API 返回「基于当天」的前复权价
- 送转分红会自动调整持仓数量

### `set_order_cost` / `set_slippage`

股票常见默认思路：

- 佣金万三，最低 5 元
- 卖出印花税千分之一
- 可设固定/百分比滑点

本地 AiQuant 若要对齐聚宽收益，需自行模拟这些成本。

---

## 3. 行情数据（核心）

### `get_price` — 多标的、多字段、按区间

```python
get_price(
    security, start_date=None, end_date=None,
    frequency='daily', fields=None,
    skip_paused=False, fq='pre', count=None,
    panel=True, fill_paused=True
)
```

| 参数 | 要点 |
|------|------|
| `count` 与 `start_date` | **二选一** |
| `frequency` | `'1d'`/`'daily'`/`'1m'`/`'minute'`/`'5m'`… |
| `fields` | 默认 OHLCV+money；还可 `factor/high_limit/low_limit/paused/...` |
| `fq` | `'pre'` / `None` 不复权 / `'post'` |
| `panel` | 多标的时建议 **`panel=False`**（pandas 新版本已无 Panel） |
| `fill_paused` | True 用 pre_close 填停牌；False 用 NaN |

**采集注意（本仓库踩过）**：

- 取「某一整日」分钟线：`start_date=end_date='YYYY-MM-DD'`，`frequency='1m'`
- 解析返回时**不要**对 numpy 数组写 `a or b`（会报 ambiguous truth value）

### `history` — 多标的、单字段、最近 N 根 ♠

```python
history(count, unit='1d', field='avg', security_list=None, df=True, skip_paused=False, fq='pre')
```

- **日线不含当天**（即使 15:00）；**分钟不含当前分钟**
- `聚宽策略.py` 盘前用 `history(2,'1d','close')` 算「昨日涨幅」靠的就是这个语义

### `attribute_history` — 单标的、多字段 ♠

```python
attribute_history(security, count, unit='1d',
    fields=['open','close','high','low','volume','money'],
    skip_paused=True, df=True, fq='pre')
```

- 同样：**天数据不含当天**；默认跳过停牌

### `get_bars` — 标准 K 线（含当前 bar 可选）

```python
get_bars(security, count, unit='1d',
    fields=['date','open','high','low','close'],
    include_now=False, end_dt=None, fq_ref_date=None, df=False)
```

| 参数 | 要点 |
|------|------|
| `unit` | `'1m','5m','15m','30m','60m','120m','1d','1w','1M'` |
| `include_now` | True 可含当前未走完的 bar（策略里算当日 MA/涨幅常用） |
| `df=False` | 单标的 → ndarray；多标的 → `{code: ndarray}` |
| 停牌 | **不填充**，实际根数可能 &lt; count |

`聚宽策略.py` 择时：

```python
get_bars('000001.XSHG', 10, '1d', 'close', include_now=True)
```

### `get_current_data` — 当日快照 ♠

```python
current_data = get_current_data()
current_data['000001.XSHE'].last_price
current_data['000001.XSHE'].day_open
current_data['000001.XSHE'].high_limit / low_limit
current_data['000001.XSHE'].paused / is_st / name
```

用于涨跌停开盘过滤、是否可卖等。

### Tick（研究级可选）

- `get_ticks` / `get_current_tick`：体量大；选股策略通常不需要

---

## 4. 证券列表与日历

### `get_all_securities`

```python
get_all_securities(types=[], date=None)
```

- `types=['stock']`；**空 list 默认也是股票**
- **建议始终传 `date=`**，避免幸存者偏差 / 拿到未上市股票
- 返回 DataFrame：`display_name, name, start_date, end_date, type`

### `get_security_info(code)` / `get_index_stocks(index_symbol, date)`

- 单标的信息；指数历史成分（防未来函数）

### `get_trade_days` / `get_all_trade_days`

```python
get_trade_days(start_date=None, end_date=None, count=None)
```

---

## 5. 财务 / 估值

### `get_fundamentals(query_object, date=None, statDate=None)`

```python
q = query(
    valuation.code, valuation.pb_ratio,
    valuation.circulating_market_cap, valuation.market_cap
).filter(
    valuation.code.in_(stock_list),
    valuation.pb_ratio.between(0.01, 30)
).order_by(
    valuation.circulating_market_cap.asc()
).limit(1000)
df = get_fundamentals(q)          # 回测里 date 默认可视为「昨天可见」
# 或
df = get_fundamentals(q, date='2015-10-15')
```

| 参数 | 要点 |
|------|------|
| `date` | 该日**收盘后能看到**的最近数据（估值表按天；财报按季） |
| `statDate` | 指定财报期如 `'2015q1'` / `'2015'`（易踩未来函数） |
| 限制 | 单次最多约 **5000** 行 |
| 常用表 | `valuation` / `income` / `balance` / `cash_flow` / `indicator` |

字段字典：[财务数据](https://www.joinquant.com/data/dict/fundamentals)

### `get_extras('is_st', ...)`

ST / 基金净值 / 期货结算等附加序列。

---

## 6. 交易函数 ♠

| API | 作用 |
|-----|------|
| `order(sec, amount)` | 按股数，正买负卖 |
| `order_target(sec, amount)` | 调到目标股数（`0`=清仓） |
| `order_value(sec, value)` | 按金额 |
| `order_target_value(sec, value)` | 调到目标市值（策略主用） |
| `cancel_order` / `get_open_orders` / `get_orders` | 撤单与查询 |

注意：

- A 股通常 **100 股整数倍**（清仓例外；科创板规则不同）
- 停牌 / 涨跌停 / 未上市可能导致返回 `None`、部分成交
- `order_target*` 会取消该标的未完成订单

`聚宽策略.py`：`order_target_value(stock, 0)` / `order_value(stock, to_buy)`。

---

## 7. 账户对象（简述）

`context.portfolio`：

- `total_value` / `available_cash` / `positions_value`
- `positions[code].total_amount` / `closeable_amount` / `value` / `avg_cost`

---

## 8. 对本仓库的实践映射

| 聚宽 API | 本地落点 |
|----------|----------|
| 全日 `get_price(..., frequency='1m')` | 日志按分钟截面 → `JQData/minute/.../HHMM.json`（方案 M v2.3） |
| 日频估值+状态 | `JQData/daily_snap/` |
| Plan-A 聚合 candidates/rank_pool | `JQData/daily/`（旧方案，仍可供 AiQuant） |
| `history(2,1d,close)` 语义 | 采集应用 **previous_date 两根日 K**，勿混入当日 |

采集脚本务必：

1. **`OUTPUT_MODE='file'`**，禁止把分钟 JSON 打进日志（否则网页「加载日志」假死）
2. 解析 ndarray 时用 `is not None`，禁用 `a or b`

---

## 9. 官方链接

- API 总览：https://www.joinquant.com/help/api/help#name:api
- API 新版页：https://www.joinquant.com/api
- 财务字段：https://www.joinquant.com/data/dict/fundamentals
- jqdatasdk 源码：https://github.com/JoinQuant/jqdatasdk
