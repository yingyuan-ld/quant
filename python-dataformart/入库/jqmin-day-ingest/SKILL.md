---
name: jqmin-day-ingest
description: >-
  聚宽分钟数据采集入库：按回测日期逐日往前编译运行，滚动加载
  #daily-logs-container 日志，解析后写入 JQData/minute。Use when
  the user mentions 逐日回测、分钟日志入库、daily-logs-container、
  JQMIN、滚动加载日志、或从聚宽页面采集分钟数据。
---

# 聚宽分钟 · 逐日回测采集入库

## 目标

从聚宽策略页把「按分钟截面」日志采到本地：

```
设 startTime=endTime=当日 → 编译运行 → 等 Minute结束
→ 滚动 #daily-logs-container 加载更多 → 提取 txt → 解析
→ JQData/minute/年/日期/HHMM.json + daily_snap
```

逐日**往前**（日期递减）重复。

## 跳过非交易日

编排默认只跑 **A 股交易日**，非交易日一律跳过，包括：

| 类型 | 判定 |
|------|------|
| 周末 | 周六、周日（`weekday >= 5`） |
| 节假日休市 | 上交所公告休市闭区间（见 `scripts/run_backfill.py` 内 `_ASHARE_CLOSED_RANGES`） |

`--from` / `--to` / `--days` 生成计划日期时均按上表过滤；`--days N` 表示往前 N 个**交易日**，不是自然日。缺少年份休市表时仍跳周末，未收录的节假日可能被误跑（回测会卡住或空日志）。若需按自然日推进，加 `--keep-weekend`（一般勿用）。

## 前置条件

1. Chrome 已开 `--remote-debugging-port=9222`，并打开聚宽「空策略看数据」编辑页（`joinquant.com/algorithm/...`）
2. 策略代码为同目录上级的 `聚宽策略-数据采集.py`（v2.3，`layout=by_minute`）
3. 本机可 `import playwright`（与 `提取JQData日志.py` 相同）

## 优先：跑编排脚本

工作目录任意；用绝对路径调用：

```bash
python quant/python-dataformart/入库/jqmin-day-ingest/scripts/run_backfill.py --from YYYY-MM-DD --to YYYY-MM-DD
```

常用参数：

| 参数 | 含义 |
|------|------|
| `--from` | 起始日（含），从此往前 |
| `--to` | 结束日（含），须 ≤ `--from` |
| `--days N` | 往前 N 个**交易日**（跳过周末 + A 股节假日休市） |
| `--dates a,b,c` | 显式日期列表 |
| `--force` | 已有 `meta.json` 也重跑 |
| `--timeout 3600` | 单日等待秒数 |
| `--min-slots 200` | 判定完成的最少 `JQMIN_BEGIN` 数（全市场约 241） |
| `--dry-run` | 只打印计划日期 |
| `--extract-only` | 不点回测，只滚动提取当前页并解析 |
| `--keep-weekend` | 不跳过周末/休市（一般勿用） |

示例：

```bash
# 从 7/13 往前到 7/1（自动跳过周末与节假日休市）
python .../run_backfill.py --from 2026-07-13 --to 2026-07-01

# 只处理当前页已跑完的一天
python .../run_backfill.py --from 2026-07-13 --extract-only
```

脚本步骤（单日）：

1. 写 `#startTime` / `#endTime` 为同一天  
2. 点 `#validate-button`（编译运行）  
3. 轮询日志至出现 `Minute结束` 且 `JQMIN_BEGIN`≈`JQMIN_END`≥`min-slots`  
4. 滚动 `#daily-logs-container` 至长度稳定  
5. 保存 `JQData/logs/minute_YYYY-MM-DD.txt`  
6. 调用 `解析JQData分钟日志.py` → `JQData/minute/.../HHMM.json`

## Agent 手工兜底（脚本不可用时）

用 Chrome DevTools MCP / CDP，严格按序：

1. **设日期**（同日；先确认是交易日，跳过周末与节假日休市）：
   ```js
   () => {
     const ds = 'YYYY-MM-DD';
     const $ = window.jQuery;
     $('#startTime').datepicker('setDate', ds);
     $('#endTime').datepicker('setDate', ds);
   }
   ```
2. **编译运行**：点击 `#validate-button`
3. **等待**：日志出现 `Minute结束 YYYY-MM-DD`，且 `JQMIN_BEGIN`/`END` 数量接近（全市场约 240+1）
4. **滚动加载**：
   ```js
   async () => {
     const c = document.querySelector('#daily-logs-container');
     const tab = document.querySelector('#daily-logs-tab');
     // 反复 c.scrollTop = c.scrollHeight 直到 tab.innerText 长度与 JQMIN_END 稳定
   }
   ```
5. **提取**：`python 入库/提取JQData日志.py minute_YYYY-MM-DD.txt`
6. **解析**：`python 入库/解析JQData分钟日志.py JQData/logs/minute_YYYY-MM-DD.txt --force`
7. 日期减一天，**跳过非交易日（周末 + 节假日休市）**后重复；已有 `minute/年/日期/meta.json` 且 `minute_count≥200` 可跳过

## 关键元素

| 元素 | 用途 |
|------|------|
| `#startTime` / `#endTime` | 回测起止日期（单日采集设相同） |
| `#validate-button` | 编译运行 |
| `#daily-logs-tab` | 日志面板 |
| `#daily-logs-container` | 虚拟列表，必须滚动才能加载全文 |

## 产出校验

```text
JQData/minute/年/日期/0931.json … 1500.json   # 约 240 个
JQData/minute/年/日期/meta.json               # layout=by_minute
JQData/daily_snap/年/日期.json
```

## 相关文件

- `../聚宽策略-数据采集.py` — 聚宽侧采集（日志 zlib）
- `../提取JQData日志.py` — 单次 CDP 提取
- `../解析JQData分钟日志.py` — 解析入库
- `scripts/run_backfill.py` — 本 skill 编排入口
