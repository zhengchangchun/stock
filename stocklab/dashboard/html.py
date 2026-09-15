"""单文件看板渲染（P14 / Task 61）：零外部依赖、可离线双击打开。

## 硬约束（有测试钉住）

- **无任何外部引用**：不引 CDN、不引外部字体、不引 JS 框架，连
  `xmlns="http://www.w3.org/2000/svg"` 都不写 —— 那串里含 `http://`，
  既是外部字符串也让正则扫描失去意义。HTML5 里 inline SVG **不需要** xmlns
  （解析器自己把它放进 SVG 命名空间），所以省掉它不损失任何渲染能力。
- **无 `<script>`**：整页是静态 HTML + inline CSS + inline SVG。看板只读，
  不需要交互；没有 JS 就没有「页面自己改了数字」这件事。
- **相对 URL**：页面不含任何绝对路径资源，便于挂在 nginx 子路径下。

## 诚实性

页面上最显眼的位置留给两件事：① 需要人来看的告警；② 「方向预测能力 ≈ 0」
与「趋势状态不预测方向」这两条**硬事实**。看板的默认读者会把它当成
「系统说可以买」，所以它必须先说清楚自己不知道什么。
"""

from __future__ import annotations

import hashlib
import html
from collections.abc import Mapping, Sequence

#: 看板自述的两条硬事实（与 CLAUDE.md《准确率优先》同源，引用自实测结论）。
HARD_FACTS = (
    "方向预测能力 ≈ 0：`pit-rw-v1.0.1` 行级方向准确率 38.14%、Brier 0.6581"
    "（按日聚类 CI 0.3819 ± 0.0070）—— 不构成任何买卖依据。",
    "趋势状态（UP / DOWN / FLAT）**不预测方向**：条件命中率 Δ 的按日聚类 CI 含基线，"
    "且符号在标的上不一致（000333、沪深300 为负）。它只做**风险分层**与状态描述。",
    "所有准确率均**按交易日聚类**（同一天多标的不算独立样本）；不足 120 交易日"
    "一律标注「样本不足，仅供观察」，不得据此下注。",
)

CSS = """
:root {
  --bg:#f5f5f2; --card:#ffffff; --ink:#1c1c1e; --muted:#6b7280;
  --line:#e3e3de; --ok:#2e7d32; --warn:#b26a00; --fail:#b3261e; --nd:#6b7280;
  --accent:#3b6ea5; --accent2:#c2703d;
}
* { box-sizing:border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC",
  "Source Han Sans SC","PingFang SC","Microsoft YaHei",sans-serif; }
.wrap { max-width:1080px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:16px; margin:0 0 12px; padding-bottom:6px;
  border-bottom:1px solid var(--line); }
.sub { color:var(--muted); font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:16px 18px; margin:16px 0; }
.grid { display:flex; flex-wrap:wrap; gap:12px; }
.tile { flex:1 1 160px; background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:12px 14px; }
.tile .k { color:var(--muted); font-size:12px; }
.tile .v { font-size:20px; font-variant-numeric:tabular-nums; margin-top:2px; }
.tile .n { color:var(--muted); font-size:12px; margin-top:2px; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th,td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--line);
  font-size:13px; }
th:first-child, td:first-child { text-align:left; }
th { color:var(--muted); font-weight:600; }
.pill { display:inline-block; min-width:82px; text-align:center; padding:1px 8px;
  border-radius:999px; font-size:12px; font-weight:600; border:1px solid; }
.PASS { color:var(--ok); border-color:var(--ok); background:#eaf4ea; }
.WARN { color:var(--warn); border-color:var(--warn); background:#fdf3e3; }
.FAIL { color:var(--fail); border-color:var(--fail); background:#fbeae8; }
.UNDETERMINED { color:var(--nd); border-color:var(--nd); background:#f0f0ee; }
.alarm { border:1px solid var(--fail); background:#fbeae8; border-radius:10px;
  padding:12px 16px; margin:16px 0; }
.alarm h2 { border:0; color:var(--fail); margin-bottom:6px; }
.alarm ul, .facts ul { margin:6px 0 0; padding-left:20px; }
.facts { border:1px solid var(--line); background:#fff; border-radius:10px;
  padding:12px 16px; margin:16px 0; color:#3f3f46; }
.facts h2 { border:0; }
.muted { color:var(--muted); }
code { background:#f0f0ee; padding:1px 5px; border-radius:4px; font-size:12px; }
.tag { font-size:12px; color:var(--muted); }
footer { color:var(--muted); font-size:12px; margin:24px 0 8px; }
footer p { margin:4px 0; }
"""

_STATUS_ICON = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "UNDETERMINED": "❔"}


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def num(x, digits: int = 2) -> str:
    """金额 / 数量的显示。`None` 显示 `—`，**不显示 0**。

    「没有这个数」和「这个数是零」是两件事：把 `None` 渲染成 `0.00`，
    页面上就再也看不出「这只票没有现价」。
    """
    if x is None:
        return "—"
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, int):
        return f"{x:,}"
    return f"{float(x):,.{digits}f}"


def frac_pct(x, digits: int = 2) -> str:
    """小数 → 百分数（0.3814 → 38.14%）。"""
    return "—" if x is None else f"{float(x) * 100:.{digits}f}%"


def pct100(x, digits: int = 2) -> str:
    """**已经是百分数**的值直接加 `%`（43.41 → 43.41%）。

    两个函数分开写，是因为口径混用不会报错，只会静默差 100 倍
    （`weight_of_total_assets` 存的是 43.41，`direction_ci95` 存的是 0.38）。
    """
    return "—" if x is None else f"{float(x):.{digits}f}%"


def _svg_rects(parts: Sequence[tuple[str, float, str]], *, width: int = 640,
               bar_h: int = 26) -> str:
    """横向堆叠条（现金 / 持仓占比）。`parts = [(label, value, color), ...]`。"""
    total = sum(max(0.0, float(v)) for _, v, _ in parts)
    if total <= 0:
        return '<p class="muted">无可绘制的市值构成（总资产为 0 或无现价）</p>'
    x = 0.0
    rects: list[str] = []
    for label, value, color in parts:
        w = max(0.0, float(value)) / total * width
        if w <= 0:
            continue
        rects.append(
            f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="{bar_h}" fill="{color}">'
            f'<title>{esc(label)}：{num(value)}（{value / total * 100:.2f}%）</title>'
            f'</rect>')
        x += w
    legend = " ".join(
        f'<span class="tag"><span style="display:inline-block;width:9px;height:9px;'
        f'background:{c};margin-right:4px;border-radius:2px"></span>{esc(l)} '
        f'{num(v)}（{v / total * 100:.2f}%）</span>'
        for l, v, c in parts if float(v) > 0)
    return (
        f'<svg viewBox="0 0 {width} {bar_h}" width="100%" height="{bar_h}" '
        f'role="img" aria-label="资产构成">{"".join(rects)}</svg>'
        f'<div style="margin-top:6px">{legend}</div>')


def _svg_ci(rows: Sequence[tuple[str, float, float, float]], *,
            width: int = 640, row_h: int = 34) -> str:
    """方向准确率的点估 + 95% CI 误差棒（x 轴 0–100%）。

    `rows = [(label, mean, lo, hi), ...]`。**必须画 CI**：只画点估的图
    会让人把 28.3% 读成一个确定的数字。
    """
    if not rows:
        return '<p class="muted">窗口内没有可绘制的准确率</p>'
    pad_l, pad_r = 96, 24
    inner = width - pad_l - pad_r
    height = row_h * len(rows) + 26
    out: list[str] = []
    for pct in (0.0, 0.25, 0.5, 0.75, 1.0):
        gx = pad_l + pct * inner
        out.append(f'<line x1="{gx:.1f}" y1="0" x2="{gx:.1f}" y2="{row_h * len(rows)}" '
                   f'stroke="#e3e3de" stroke-width="1"/>')
        out.append(f'<text x="{gx:.1f}" y="{row_h * len(rows) + 16}" fill="#6b7280" '
                   f'font-size="11" text-anchor="middle">{pct * 100:.0f}%</text>')
    for i, (label, mean, lo, hi) in enumerate(rows):
        y = i * row_h + row_h / 2
        x1, x2 = pad_l + lo * inner, pad_l + hi * inner
        xm = pad_l + mean * inner
        out.append(f'<text x="{pad_l - 10}" y="{y + 4}" fill="#1c1c1e" font-size="12" '
                   f'text-anchor="end">{esc(label)}</text>')
        out.append(f'<line x1="{x1:.1f}" y1="{y:.1f}" x2="{x2:.1f}" y2="{y:.1f}" '
                   f'stroke="#3b6ea5" stroke-width="3" stroke-linecap="round" '
                   f'opacity="0.45"/>')
        out.append(f'<circle cx="{xm:.1f}" cy="{y:.1f}" r="5" fill="#3b6ea5">'
                   f'<title>{esc(label)}：{frac_pct(mean)}'
                   f'（CI {frac_pct(lo)} ~ {frac_pct(hi)}）</title></circle>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="方向准确率与 95% 置信区间">{"".join(out)}</svg>')


def _tile(key: str, value: str, note: str = "") -> str:
    return (f'<div class="tile"><div class="k">{esc(key)}</div>'
            f'<div class="v">{esc(value)}</div>'
            + (f'<div class="n">{esc(note)}</div>' if note else "") + "</div>")


# ---------- 各段 ----------

def _alarm_section(summary: Mapping) -> str:
    alarms = summary.get("alarms") or []
    if not alarms:
        return ""
    items = "".join(f"<li>{esc(a)}</li>" for a in alarms)
    return (f'<div class="alarm"><h2>⚠️ 需要人来看（{len(alarms)} 条）</h2>'
            f"<ul>{items}</ul></div>")


def _facts_section() -> str:
    items = "".join(f"<li>{esc(f)}</li>" for f in HARD_FACTS)
    return (f'<div class="facts"><h2>先读这一段：这个系统测不出方向</h2>'
            f"<ul>{items}</ul></div>")


def _risk_section(risk: Mapping | None) -> str:
    """风险 / 凯利面板。`risk is None` = 未接入，**不是**「风险为零」。"""
    if risk is None:
        return ('<div class="card"><h2>风险与仓位（凯利）</h2>'
                '<p class="muted">风险面板本轮未接入（`risk` 为 null）。'
                '这不代表「风险为零」，只代表这一格没有数据。</p></div>')
    verdict = str(risk.get("verdict", "UNDETERMINED"))
    cls = {"NO_BET": "FAIL", "BET": "PASS"}.get(verdict, "UNDETERMINED")
    lines = [f'<p><span class="pill {cls}">{esc(verdict)}</span> '
             f'<b>{esc(risk.get("verdict_label", ""))}</b></p>']
    if risk.get("verdict_reason"):
        lines.append(f'<p>{esc(risk["verdict_reason"])}</p>')
    for w in risk.get("notes") or []:
        lines.append(f'<p class="muted">· {esc(w)}</p>')
    return ('<div class="card"><h2>风险与仓位（凯利）</h2>'
            + "".join(lines) + "</div>")


def _portfolio_section(view: Mapping) -> str:
    out = ['<div class="card"><h2>组合视图</h2>']
    out.append('<div class="grid">')
    out.append(_tile("总资产", num(view["total_assets"]),
                     f"现金 {num(view['cash'])} + 持仓市值 {num(view['market_value_priced'])}"))
    out.append(_tile("现金", num(view["cash"]),
                     f"本金净投入 {num(view['cash_breakdown']['net_deposits'])}"))
    out.append(_tile("累计收益（含费）", num(view["total_pnl_incl_fee"]),
                     f"净投入 {num(view['net_invested'])}"))
    out.append(_tile("持仓市值（已定价）", num(view["market_value_priced"]),
                     f"缺现价 {len(view['missing_price_codes'])} 只"))
    out.append("</div>")

    if view["positions"]:
        out.append('<h2 style="border:0;margin-top:18px">资产构成</h2>')
        out.append(_svg_rects([
            ("现金", view["cash"], "#3b6ea5"),
            *[(f"{p['code']} {p['name'] or ''}".strip(),
               p["market_value"] or 0.0, "#c2703d") for p in view["positions"]],
        ]))

        out.append('<table><thead><tr><th>代码</th><th>数量</th><th>均价(含费)</th>'
                   '<th>现价</th><th>来源</th><th>市值</th><th>浮盈(含费)</th>'
                   '<th>占总资产</th></tr></thead><tbody>')
        for p in view["positions"]:
            out.append(
                f"<tr><td>{esc(p['code'])} {esc(p['name'] or '')}</td>"
                f"<td>{num(p['qty'])}</td><td>{num(p['avg_cost_incl_fee'], 4)}</td>"
                f"<td>{num(p['price'])}</td><td>{esc(p['price_source'] or '—')}</td>"
                f"<td>{num(p['market_value'])}</td>"
                f"<td>{num(p['float_pnl_incl_fee'])}</td>"
                f"<td>{pct100(p['weight_of_total_assets'])}</td></tr>")
        out.append("</tbody></table>")
        out.append('<p class="tag">口径：成本**默认含费**（均价 4 位小数）；'
                   '不含费口径见 `portfolio show --json`。缺现价的标的'
                   '**排除出市值合计**，不用成本价冒充。</p>')

    out.append('<h2 style="border:0;margin-top:18px">纪律检查</h2>')
    out.append('<table><thead><tr><th>检查</th><th>标的</th><th>状态</th>'
               '<th>说明</th></tr></thead><tbody>')
    for c in view["discipline"]:
        icon = _STATUS_ICON.get(c["status"], "?")
        out.append(
            f"<tr><td>{esc(c['check'])}</td><td>{esc(c['subject'] or '—')}</td>"
            f'<td><span class="pill {esc(c["status"])}">{icon} {esc(c["status"])}'
            f"</span></td><td style=\"text-align:left\">{esc(c['detail'])}</td></tr>")
    out.append("</tbody></table>")
    out.append("</div>")
    return "".join(out)


def _bucket_card(name: str, bucket: Mapping | None, *, empty_note: str) -> str:
    if bucket is None:
        return (f'<div class="tile"><div class="k">{esc(name)}</div>'
                f'<div class="v muted">无样本</div>'
                f'<div class="n">{esc(empty_note)}</div></div>')
    gate = bucket["sample_gate"]
    gate_cls = "PASS" if gate["meets"] else "WARN"
    return (
        f'<div class="tile"><div class="k">{esc(name)}</div>'
        f'<div class="v">{frac_pct(bucket["direction_accuracy_daily"])}</div>'
        f'<div class="n">方向准确率（按日聚类）· '
        f'CI {frac_pct(bucket["direction_ci95"][0])} ~ {frac_pct(bucket["direction_ci95"][1])}'
        f'</div><div class="n">有效交易日 <b>{num(bucket["effective_n_days"])}</b>'
        f' / 行 {num(bucket["n_rows"])} · Brier {num(bucket["brier_daily"], 4)}</div>'
        f'<div style="margin-top:6px"><span class="pill {gate_cls}">'
        f'{esc(gate["label"])}</span></div></div>')


def _accuracy_section(acc: Mapping) -> str:
    out = ['<div class="card"><h2>近期验证统计（LIVE / REPLAY 分列）</h2>']
    w = acc["window"]
    out.append(f'<p class="tag">窗口：{esc(w["start"])} ~ {esc(w["end"])}，'
               f'{num(w["n_sessions"])} 个交易日 · 判据：'
               f'{esc(acc["provenance"]["rule"])}</p>')
    out.append('<div class="grid">')
    out.append(_bucket_card("LIVE（实盘累计）", acc["live"],
                            empty_note="窗口内一条实盘记录都没有 —— 这不是「实盘表现 0%」"))
    out.append(_bucket_card("REPLAY（PIT 历史回放）", acc["replay"],
                            empty_note="窗口内没有回放记录"))
    out.append("</div>")
    if acc.get("note"):
        out.append(f'<p class="tag">{esc(acc["note"])}</p>')
    rows = []
    for label, key in (("LIVE", "live"), ("REPLAY", "replay")):
        b = acc.get(key)
        if b:
            rows.append((label, b["direction_accuracy_daily"],
                         b["direction_ci95"][0], b["direction_ci95"][1]))
    out.append(_svg_ci(rows))
    for key, label in (("live", "LIVE"), ("replay", "REPLAY")):
        b = acc.get(key)
        if b and b.get("baselines_daily"):
            base = "，".join(f"{k} {frac_pct(v)}" for k, v in b["baselines_daily"].items())
            out.append(f'<p class="tag">{esc(label)} 基线对照（按日聚类）：{esc(base)}'
                       f' —— 跑不赢这些基线就是没有 edge。</p>')
    out.append("</div>")
    return "".join(out)


def _freshness_section(fresh: Mapping) -> str:
    out = ['<div class="card"><h2>数据新鲜度</h2><div class="grid">']
    out.append(_tile("bars 最新日期", str(fresh["bars_latest_date"] or "—"),
                     " · ".join(f"{c} {d}" for c, d in
                                (fresh["bars_latest_by_code"] or {}).items())))
    snap = fresh["snapshot_latest"]
    out.append(_tile("最新快照 ts", str(snap["ts"] or "—"),
                     f"trade_date {snap['trade_date'] or '—'}（全库最新，未必等于 asof）"))
    s = fresh["snapshots_on_asof"]
    out.append(_tile(f"asof 当日快照（{s['trade_date']}）", num(s["n_rows"]),
                     f"{num(s['n_codes'])} 只 · {num(s['n_slots'])} 个时点"))
    out.append("</div>")
    if fresh["codes_without_bars"]:
        out.append(f'<p class="tag">无 K 线的标的：'
                   f'{esc("、".join(fresh["codes_without_bars"]))}</p>')
    out.append("</div>")
    return "".join(out)


def render_html(summary: Mapping, *, built_at: str | None = None) -> str:
    """摘要 → 单文件 HTML。`built_at` 只影响页脚，不进 `/api/summary`。"""
    view = summary["portfolio"]
    head = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>stock-lab 看板 · asof {esc(summary['asof'])}</title>"
        f"<style>{CSS}</style></head><body><div class=\"wrap\">")
    header = (
        "<h1>stock-lab 看板</h1>"
        f"<p class=\"sub\">asof <b>{esc(summary['asof'])}</b>"
        f"（那一天收盘后我知道什么）· schema v{esc(summary['schema_version'])}"
        + (f" · 页面生成 {esc(built_at)}" if built_at else "")
        + " · 只读 · 单文件（无外部依赖）</p>")
    footer = (
        "<footer>"
        f"<p>价格口径：{esc(view['price_policy'])}</p>"
        f"<p>成本口径：{esc(view['cost_policy'])}</p>"
        "<p>JSON 接口：<code>api/summary</code>（相对路径）· 健康检查："
        "<code>health</code>。看板只读，不含任何下单/写库入口。</p>"
        "</footer></div></body></html>")
    return "".join([
        head, header,
        _alarm_section(summary),
        _facts_section(),
        _risk_section(summary.get("risk")),
        _portfolio_section(view),
        _accuracy_section(summary["accuracy"]),
        _freshness_section(summary["freshness"]),
        footer,
    ])


def html_sha256(doc: str) -> str:
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()
