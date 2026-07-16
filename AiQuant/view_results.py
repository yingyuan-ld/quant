# -*- coding: utf-8 -*-
"""
从 AiQuant/results 生成本地 HTML 回测报告（无需打开聚宽浏览器）。

用法:
  python view_results.py
  python view_results.py --tag 2020-01-01_2026-7-12
  python view_results.py --open
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from collections import defaultdict
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CANVAS_PATH = (
    Path.home() / ".cursor" / "projects" / "e-fe-lianghua" / "canvases" / "aiquant-backtest-results.canvas.tsx"
)


def _find_latest_tag() -> str:
    summaries = sorted(RESULTS_DIR.glob("summary_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not summaries:
        raise FileNotFoundError(f"未找到 summary 文件: {RESULTS_DIR}")
    name = summaries[0].name
    return name[len("summary_") : -len(".json")]


def _load(tag: str) -> dict:
    summary = json.loads((RESULTS_DIR / f"summary_{tag}.json").read_text(encoding="utf-8"))
    equity = json.loads((RESULTS_DIR / f"equity_{tag}.json").read_text(encoding="utf-8"))["records"]
    trades_path = RESULTS_DIR / f"trades_{tag}.json"
    trades = []
    if trades_path.exists():
        trades = json.loads(trades_path.read_text(encoding="utf-8")).get("records", [])
    return {"summary": summary, "equity": equity, "trades": trades, "tag": tag}


def _aggregate(data: dict) -> dict:
    summary = {k: v for k, v in data["summary"].items() if not k.startswith("_")}
    equity = data["equity"]

    monthly: dict[str, float] = {}
    for r in equity:
        monthly[r["date"][:7]] = r["total_value"]

    year_start: dict[str, float] = {}
    year_end: dict[str, float] = {}
    for r in equity:
        y = r["date"][:4]
        year_start.setdefault(y, r["total_value"])
        year_end[y] = r["total_value"]

    yearly = []
    for y in sorted(year_end):
        s = year_start[y]
        e = year_end[y]
        yearly.append({"year": y, "return_pct": round((e / s - 1) * 100, 2), "end_value": round(e, 2)})

    peak = equity[0]["total_value"]
    dd_month: dict[str, float] = {}
    for r in equity:
        v = r["total_value"]
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak else 0
        m = r["date"][:7]
        dd_month[m] = max(dd_month.get(m, 0), dd)

    by_year: dict[str, dict[str, int]] = defaultdict(lambda: {"buy": 0, "sell": 0})
    for t in data["trades"]:
        by_year[t["date"][:4]][t["side"]] += 1

    return {
        "summary": summary,
        "month_series": [{"month": k, "value": round(v, 2)} for k, v in sorted(monthly.items())],
        "yearly": yearly,
        "dd_series": [{"month": k, "dd_pct": round(v, 2)} for k, v in sorted(dd_month.items())],
        "trade_years": [{"year": y, **by_year[y]} for y in sorted(by_year)],
        "bull_days": sum(1 for r in equity if r["is_bull"]),
        "hold_days": sum(1 for r in equity if not r["not_hold"]),
    }


def _json(arr) -> str:
    return json.dumps(arr, ensure_ascii=False)


def render_canvas(payload: dict) -> str:
    s = payload["summary"]
    months = [x["month"] for x in payload["month_series"]]
    equity = [round(x["value"]) for x in payload["month_series"]]
    dd = [x["dd_pct"] for x in payload["dd_series"]]
    years = [x["year"] for x in payload["yearly"]]
    yr_ret = [x["return_pct"] for x in payload["yearly"]]
    ty = payload["trade_years"]
    buys = [x["buy"] for x in ty]
    sells = [x["sell"] for x in ty]
    ty_years = [x["year"] for x in ty]
    yearly_rows = [[x["year"], f"{x['return_pct']}%", f"{x['end_value']:,.0f}"] for x in payload["yearly"]]
    trade_rows = [[x["year"], str(x["buy"]), str(x["sell"])] for x in ty]

    return f"""import {{
  BarChart,
  Card,
  CardBody,
  CardHeader,
  Grid,
  H1,
  LineChart,
  Stack,
  Stat,
  Table,
  Text,
  useHostTheme,
}} from "cursor/canvas";

const SUMMARY = {{
  start: "{s['start_date']}",
  end: "{s['end_date']}",
  days: {s['days']},
  initial: {s['initial_cash']},
  final: {s['final_value']},
  totalReturn: {s['total_return']},
  maxDd: {s['max_drawdown_pct']},
  trades: {s['trade_count']},
  bullDays: {payload['bull_days']},
}};

const MONTHS = {_json(months)};
const EQUITY = {_json(equity)};
const DD = {_json(dd)};
const YEARS = {_json(years)};
const YEAR_RET = {_json(yr_ret)};
const TRADE_YEARS = {_json(ty_years)};
const BUYS = {_json(buys)};
const SELLS = {_json(sells)};
const YEARLY_ROWS = {_json(yearly_rows)};
const TRADE_ROWS = {_json(trade_rows)};

function Caption({{ children }}: {{ children: string }}) {{
  const theme = useHostTheme();
  return (
    <Text style={{{{ color: theme.text.tertiary, fontSize: 12, marginTop: 8 }}}}>
      {{children}}
    </Text>
  );
}}

export default function AiQuantBacktestResults() {{
  const theme = useHostTheme();
  const fmtMoney = (n: number) => n.toLocaleString("zh-CN", {{ maximumFractionDigits: 0 }});

  return (
    <Stack gap={{20}} style={{{{ padding: 4 }}}}>
      <Stack gap={{4}}>
        <H1>聚宽策略 · 本地回测</H1>
        <Text style={{{{ color: theme.text.secondary }}}}>
          {{SUMMARY.start}} ~ {{SUMMARY.end}} · {{SUMMARY.days}} 个交易日 · 初始 {{fmtMoney(SUMMARY.initial)}} 元
        </Text>
      </Stack>

      <Grid columns={{4}} gap={{12}}>
        <Stat value={{"+" + SUMMARY.totalReturn + "%"}} label="总收益率" tone="success" />
        <Stat value={{fmtMoney(SUMMARY.final)}} label="期末总资产（元）" />
        <Stat value={{SUMMARY.maxDd + "%"}} label="最大回撤" tone="danger" />
        <Stat value={{SUMMARY.trades}} label="成交笔数" />
      </Grid>

      <Card>
        <CardHeader>月度总资产（元）</CardHeader>
        <CardBody>
          <LineChart
            categories={{MONTHS}}
            series={{[{{ name: "月末总资产", data: EQUITY }}]}}
            height={{280}}
            beginAtZero={{false}}
            referenceLines={{[{{ value: SUMMARY.initial, label: "初始 100 万", tone: "neutral" }}]}}
          />
          <Caption>Source: AiQuant/results/equity · 每月最后一个交易日收盘总资产</Caption>
        </CardBody>
      </Card>

      <Grid columns={{2}} gap={{16}}>
        <Card>
          <CardHeader>年度收益率（%）</CardHeader>
          <CardBody>
            <BarChart categories={{YEARS}} series={{[{{ name: "年度收益", data: YEAR_RET }}]}} height={{220}} valueSuffix="%" showValues />
            <Caption>各自然年首末交易日资产变化</Caption>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>月度最大回撤（%）</CardHeader>
          <CardBody>
            <LineChart categories={{MONTHS}} series={{[{{ name: "月内最大回撤", data: DD, tone: "danger" }}]}} height={{220}} valueSuffix="%" beginAtZero />
            <Caption>{{`月内峰值到谷底的最大回撤 · 全区间最大 ${{SUMMARY.maxDd}}%`}}</Caption>
          </CardBody>
        </Card>
      </Grid>

      <Card>
        <CardHeader>年度买卖笔数</CardHeader>
        <CardBody>
          <BarChart
            categories={{TRADE_YEARS}}
            series={{[
              {{ name: "买入", data: BUYS, tone: "info" }},
              {{ name: "卖出", data: SELLS, tone: "warning" }},
            ]}}
            height={{220}}
          />
          <Caption>{{`Source: AiQuant/results/trades · 牛市持仓日 ${{SUMMARY.bullDays}} 天`}}</Caption>
        </CardBody>
      </Card>

      <Grid columns={{2}} gap={{16}}>
        <Card>
          <CardHeader>年度收益明细</CardHeader>
          <CardBody style={{{{ padding: 0 }}}}>
            <Table headers={{["年份", "收益率", "年末资产（元）"]}} rows={{YEARLY_ROWS}} columnAlign={{["left", "right", "right"]}} framed={{false}} />
          </CardBody>
        </Card>
        <Card>
          <CardHeader>年度成交明细</CardHeader>
          <CardBody style={{{{ padding: 0 }}}}>
            <Table headers={{["年份", "买入", "卖出"]}} rows={{TRADE_ROWS}} columnAlign={{["left", "right", "right"]}} framed={{false}} />
          </CardBody>
        </Card>
      </Grid>

      <Text style={{{{ color: theme.text.tertiary, fontSize: 12 }}}}>
        本地 HTML 报告: quant/AiQuant/results/report.html · 运行 python view_results.py --open 刷新并打开
      </Text>
    </Stack>
  );
}}
"""


def render_html(payload: dict) -> str:
    s = payload["summary"]
    chart = json.dumps(payload, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>AiQuant 回测报告 · {payload['tag']}</title>
  <style>
    :root {{ font-family: "Segoe UI", system-ui, sans-serif; color: #111; background: #f6f7f9; }}
    body {{ margin: 0; padding: 24px; }}
    .wrap {{ max-width: 1100px; margin: 0 auto; }}
    h1 {{ font-size: 22px; margin: 0 0 4px; }}
    .sub {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }}
    .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 16px; }}
    .card .label {{ font-size: 12px; color: #666; }}
    .card .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
    .card .value.pos {{ color: #059669; }}
    .card .value.neg {{ color: #dc2626; }}
    section {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    section h2 {{ font-size: 15px; margin: 0 0 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 8px 6px; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    canvas {{ width: 100%; height: 260px; }}
    .note {{ font-size: 12px; color: #888; margin-top: 8px; }}
  </style>
</head>
<body>
<div class="wrap">
  <h1>聚宽策略 · 本地回测报告</h1>
  <div class="sub">{s['start_date']} ~ {s['end_date']} · {s['days']} 个交易日 · 标签 {payload['tag']}</div>

  <div class="grid">
    <div class="card"><div class="label">总收益率</div><div class="value {'pos' if s['total_return']>=0 else 'neg'}">{s['total_return']}%</div></div>
    <div class="card"><div class="label">期末总资产</div><div class="value">{s['final_value']:,.0f}</div></div>
    <div class="card"><div class="label">最大回撤</div><div class="value neg">{s['max_drawdown_pct']}%</div></div>
    <div class="card"><div class="label">成交笔数</div><div class="value">{s['trade_count']}</div></div>
  </div>

  <section>
    <h2>月度净值曲线</h2>
    <canvas id="equity"></canvas>
    <div class="note">Source: AiQuant/results · 月末收盘总资产（元）</div>
  </section>

  <section>
    <h2>月度最大回撤</h2>
    <canvas id="drawdown"></canvas>
    <div class="note">月内峰值到谷底的最大回撤（%）</div>
  </section>

  <section>
    <h2>年度收益</h2>
    <table id="yearly"></table>
  </section>

  <section>
    <h2>年度成交统计</h2>
    <table id="trades"></table>
  </section>
</div>
<script>
const DATA = {chart};

function drawLine(canvasId, labels, values, color, suffix='') {{
  const c = document.getElementById(canvasId);
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr; c.height = h * dpr; ctx.scale(dpr, dpr);
  ctx.clearRect(0,0,w,h);
  const pad = {{l:48,r:12,t:16,b:32}};
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  ctx.strokeStyle = '#e5e7eb'; ctx.lineWidth = 1;
  for (let i=0;i<5;i++) {{
    const y = pad.t + (h-pad.t-pad.b)*i/4;
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(w-pad.r,y); ctx.stroke();
    const v = max - range*i/4;
    ctx.fillStyle='#888'; ctx.font='11px sans-serif'; ctx.textAlign='right';
    ctx.fillText(v.toFixed(0)+suffix, pad.l-6, y+4);
  }}
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
  values.forEach((v,i) => {{
    const x = pad.l + (w-pad.l-pad.r)*i/(values.length-1);
    const y = pad.t + (h-pad.t-pad.b)*(1-(v-min)/range);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y);
  }});
  ctx.stroke();
  ctx.fillStyle='#666'; ctx.font='11px sans-serif'; ctx.textAlign='center';
  [0, Math.floor(labels.length/2), labels.length-1].forEach(i => {{
    const x = pad.l + (w-pad.l-pad.r)*i/(labels.length-1);
    ctx.fillText(labels[i], x, h-8);
  }});
}}

const months = DATA.month_series.map(x=>x.month);
const equity = DATA.month_series.map(x=>x.value);
const dd = DATA.dd_series.map(x=>x.dd_pct);
drawLine('equity', months, equity, '#2563eb');
drawLine('drawdown', DATA.dd_series.map(x=>x.month), dd, '#dc2626', '%');

const ytable = document.getElementById('yearly');
ytable.innerHTML = '<tr><th>年份</th><th>收益率</th><th>年末资产</th></tr>' +
  DATA.yearly.map(r=>`<tr><td>${{r.year}}</td><td>${{r.return_pct}}%</td><td>${{r.end_value.toLocaleString()}}</td></tr>`).join('');

const ttable = document.getElementById('trades');
ttable.innerHTML = '<tr><th>年份</th><th>买入</th><th>卖出</th></tr>' +
  DATA.trade_years.map(r=>`<tr><td>${{r.year}}</td><td>${{r.buy}}</td><td>${{r.sell}}</td></tr>`).join('');
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="生成本地 HTML 回测报告")
    parser.add_argument("--tag", default=None, help="结果标签，默认取最新 summary")
    parser.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    parser.add_argument("--no-canvas", action="store_true", help="不更新 Cursor Canvas 文件")
    args = parser.parse_args()

    tag = args.tag or _find_latest_tag()
    raw = _load(tag)
    payload = _aggregate(raw)
    payload["tag"] = tag

    chart_cache = RESULTS_DIR / "_chart_data.json"
    chart_cache.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    out = RESULTS_DIR / "report.html"
    out.write_text(render_html(payload), encoding="utf-8")
    print(f"报告已生成: {out}")

    if not args.no_canvas:
        CANVAS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CANVAS_PATH.write_text(render_canvas(payload), encoding="utf-8")
        print(f"Canvas 已更新: {CANVAS_PATH}")
    if args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
