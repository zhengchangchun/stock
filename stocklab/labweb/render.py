"""服务端渲染（P15）：内嵌 CSS + inline SVG，**零外部引用**。

## 硬约束（有测试钉住）

- 不出现 `http(s)://`、`<link`、`<script src` —— 没有 CDN、没有外部字体、
  没有 JS 框架。唯一的 JS 是一句 inline 的 `onsubmit="return confirm(...)"`
  给「冲正」这类不可逆动作加一道确认。
- 所有 `href` / 表单 `action` 都带 `--base-path` 前缀（子路径部署）。

## 诚实性（红线，写在渲染层是因为它就是「显示」这件事本身）

- `None` 一律渲染成 **未知**，不渲染成 `0`、不渲染成 `—` 之后就当没这回事；
- `missing_price` 的持仓：现价/市值/浮盈/占比全部「未知」+ 一句
  「不用成本价冒充」；
- 凯利 `NO_BET` 原样显示（含理由与两道门）；
- 准确率 **LIVE 与 REPLAY 分开**，LIVE 为 0 时明说「这不是实盘表现」，
  样本门槛未达显示「样本不足，仅供观察」。

**颜色不是唯一信号**：每个状态同时带文字（`PASS` / `未判定` …），
色盲用户与黑白打印都能读。
"""

from __future__ import annotations

from html import escape as _escape
from typing import Any, Mapping, Sequence

#: 状态 → (CSS 类, 中文)。四态，**没有第五态**（见 portfolio/discipline.py）。
STATUS_CN = {
    "PASS": ("pass", "通过"),
    "WARN": ("warn", "偏离"),
    "FAIL": ("fail", "不通过"),
    "UNDETERMINED": ("unknown", "未判定"),
}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--muted:#5b6673;--line:#dfe3e8;
--pass:#1a7f37;--warn:#9a6700;--fail:#b42318;--unknown:#57606a;--accent:#0b5cad;}
*{box-sizing:border-box}
body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
"Noto Sans CJK SC","Source Han Sans SC",sans-serif;background:var(--bg);color:var(--ink)}
header{background:#fff;border-bottom:1px solid var(--line);padding:10px 16px}
header .brand{font-weight:700;font-size:16px}
header .sub{color:var(--muted);font-size:12px;margin-left:8px}
nav{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
nav a{padding:5px 11px;border:1px solid var(--line);border-radius:14px;
text-decoration:none;color:var(--ink);font-size:13px;background:#fff}
nav a.on{background:var(--accent);border-color:var(--accent);color:#fff}
main{max-width:1020px;margin:0 auto;padding:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:13px 15px;margin:0 0 13px}
h2{font-size:15px;margin:0 0 9px}
h3{font-size:14px;margin:14px 0 6px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:right;padding:5px 7px;border-bottom:1px solid var(--line);
white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600}
.tiles{display:flex;flex-wrap:wrap;gap:10px}
.tile{flex:1 1 150px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:11px 13px}
.tile .k{color:var(--muted);font-size:12px}
.tile .v{font-size:20px;font-weight:700;margin-top:3px}
.tile .n{color:var(--muted);font-size:11.5px;margin-top:2px}
.pass{color:var(--pass);font-weight:600}.warn{color:var(--warn);font-weight:600}
.fail{color:var(--fail);font-weight:600}.unknown{color:var(--unknown);font-style:italic}
.muted{color:var(--muted)}
.pos{color:var(--pass)}.neg{color:var(--fail)}
.banner{border-radius:8px;padding:9px 12px;margin:0 0 12px;font-size:13.5px}
.banner.ok{background:#e8f5ec;border:1px solid #a6d8b4;color:#14532d}
.banner.bad{background:#fdeceb;border:1px solid #f3b8b3;color:#7a1c14}
.banner.warn{background:#fdf6e3;border:1px solid #e8d59a;color:#6b4e00}
form{display:flex;flex-wrap:wrap;gap:9px;align-items:flex-end;margin-top:6px}
label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:3px}
input,select{padding:6px 8px;border:1px solid var(--line);border-radius:7px;
font-size:14px;background:#fff;color:var(--ink);min-width:88px}
button{padding:7px 15px;border:0;border-radius:7px;background:var(--accent);
color:#fff;font-size:14px;cursor:pointer}
button.danger{background:var(--fail)}
label.chk{flex-direction:row;align-items:center;gap:6px;font-size:13.5px;color:var(--ink)}
pre{background:#fff;border:1px solid var(--line);border-radius:8px;padding:9px;
overflow-x:auto;font-size:12.5px;margin:0}
footer{max-width:1020px;margin:0 auto;padding:0 14px 26px;color:var(--muted);
font-size:12px}
ul.tight{margin:6px 0;padding-left:18px;font-size:13.5px}
@media(max-width:640px){main{padding:9px}.tile .v{font-size:17px}
table{font-size:12.5px}th,td{padding:4px 5px}}
@media print{nav,form,button{display:none}}
"""

NAV_ITEMS = (
    ("/", "总览"),
    ("/trades", "成交流水"),
    ("/cash", "现金流"),
    ("/risk", "风险"),
    ("/data", "数据"),
    ("/health", "健康"),
)


# ---------- 基础格式化 ----------

def esc(x: Any) -> str:
    return _escape("" if x is None else str(x), quote=True)


def money(x: float | None, *, digits: int = 2) -> str:
    if x is None:
        return '<span class="unknown">未知</span>'
    return f"{float(x):,.{digits}f}"


def num(x: Any, digits: int = 4) -> str:
    if x is None:
        return '<span class="unknown">未知</span>'
    return f"{float(x):.{digits}f}"


def pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return '<span class="unknown">未知</span>'
    return f"{float(x):.{digits}f}%"


def ratio_pct(x: float | None, digits: int = 2) -> str:
    """小数比率 → 百分数（`None` → 未知）。"""
    if x is None:
        return '<span class="unknown">未知</span>'
    return f"{float(x) * 100:.{digits}f}%"


def sign_cls(x: float | None) -> str:
    if x is None:
        return "unknown"
    return "pos" if float(x) > 0 else ("neg" if float(x) < 0 else "")


def status_cell(status: str) -> str:
    cls, cn = STATUS_CN.get(status, ("unknown", "未知"))
    return f'<span class="{cls}">{esc(status)} {cn}</span>'


# ---------- 布局 ----------

def layout(*, base: str, title: str, body: str, asof: str, built_at: str,
           current: str = "", alarms: Sequence[str] = ()) -> str:
    nav = "".join(
        f'<a class="{"on" if path == current else ""}" href="{esc(base + path)}">'
        f'{esc(label)}</a>'
        for path, label in NAV_ITEMS)
    warn = ""
    if alarms:
        items = "".join(f"<li>{esc(a)}</li>" for a in alarms)
        warn = (f'<div class="banner warn"><b>{len(alarms)} 条需要注意</b>'
                f'<ul class="tight">{items}</ul></div>')
    return (
        "<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{esc(title)} · stock-lab</title>"
        f"<style>{CSS}</style></head><body>"
        f'<header><div><span class="brand">stock-lab</span>'
        f'<span class="sub">持仓管理 · 数据 asof {esc(asof)}</span></div>'
        f'<nav>{nav}</nav></header><main>{warn}{body}</main>'
        f'<footer>页面生成于 {esc(built_at)}（Asia/Shanghai）· 只绑回环 127.0.0.1 · '
        f'所有数字来自账本与行情库，未接入风险面板时会显式写「未接入」而不是显示 0。'
        f'</footer></body></html>\n')


def banner(text: str, kind: str = "ok") -> str:
    return f'<div class="banner {esc(kind)}">{text}</div>'


def tile(key: str, value_html: str, note: str = "") -> str:
    n = f'<div class="n">{note}</div>' if note else ""
    return (f'<div class="tile"><div class="k">{esc(key)}</div>'
            f'<div class="v">{value_html}</div>{n}</div>')


# ---------- 净值曲线（inline SVG） ----------

def nav_svg(points: Sequence[Mapping], *, net_invested: float | None = None,
            width: int = 940, height: int = 210) -> str:
    """按日净值折线。`total_assets is None` 的日**断线**并标注，不插值。"""
    valid = [(i, p) for i, p in enumerate(points) if p["total_assets"] is not None]
    if len(valid) < 2:
        return ('<p class="muted">净值曲线需要至少两个交易日的可用市值；'
                '当前不足 —— 不画线，也不拿成本价补点。</p>')
    ys = [float(p["total_assets"]) for _, p in valid]
    lo, hi = min(ys), max(ys)
    ref = None if net_invested is None else float(net_invested)
    if ref is not None:
        lo, hi = min(lo, ref), max(hi, ref)
    if hi - lo < 1e-9:
        hi, lo = hi + 1.0, lo - 1.0
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    n = len(points)
    left, right, top, bottom = 62, 12, 12, 26
    plot_w, plot_h = width - left - right, height - top - bottom

    def xy(idx: int, v: float) -> tuple[float, float]:
        x = left + (idx / max(1, n - 1)) * plot_w
        y = top + (1.0 - (v - lo) / (hi - lo)) * plot_h
        return x, y

    parts: list[str] = []
    # 本金参考线（虚线）：净值低于它 = 亏损，一眼可判
    if ref is not None:
        _, ry = xy(0, ref)
        parts.append(f'<line x1="{left}" y1="{ry:.1f}" x2="{width - right}" '
                     f'y2="{ry:.1f}" stroke="#8c98a4" stroke-dasharray="5 4"/>')
        parts.append(f'<text x="{left - 6}" y="{ry + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="#5b6673">{ref:,.0f} 本金</text>')

    # 连续段：None 处断开，**不插值**（插值会让缺价那天看起来有净值）
    run: list[tuple[int, Mapping]] = []
    for item in valid + [(None, None)]:
        idx, p = item
        if p is None or (run and idx != run[-1][0] + 1):
            if len(run) >= 2:
                d = " ".join(f"{x:.1f},{y:.1f}"
                             for x, y in (xy(i, float(q["total_assets"]))
                                          for i, q in run))
                parts.append(f'<polyline fill="none" stroke="#0b5cad" '
                             f'stroke-width="2" points="{d}"/>')
            elif run:
                x, y = xy(*run[0][:1], float(run[0][1]["total_assets"]))
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" '
                             f'fill="#0b5cad"/>')
            run = []
        if p is not None:
            run.append((idx, p))
    for i, p in valid:
        if p["status"] == "missing_price":
            x, _ = xy(i, lo)
            parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" '
                         f'y2="{top + plot_h}" stroke="#b42318" '
                         f'stroke-dasharray="2 3" stroke-width="1"/>')

    parts.append(f'<text x="{left}" y="{height - 8}" font-size="11" fill="#5b6673">'
                 f'{esc(points[0]["date"])}</text>')
    parts.append(f'<text x="{width - right}" y="{height - 8}" text-anchor="end" '
                 f'font-size="11" fill="#5b6673">{esc(points[-1]["date"])}</text>')
    parts.append(f'<text x="{left - 6}" y="{top + 4}" text-anchor="end" '
                 f'font-size="11" fill="#5b6673">{hi:,.0f}</text>')
    parts.append(f'<text x="{left - 6}" y="{top + plot_h}" text-anchor="end" '
                 f'font-size="11" fill="#5b6673">{lo:,.0f}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="按日净值曲线">'
            f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" '
            f'fill="#fbfcfd" stroke="#dfe3e8"/>' + "".join(parts) + "</svg>")


# ---------- 各页面 ----------

def _kelly_line(risk: Mapping | None) -> str:
    """凯利结论行。`None`（未接入）与 `NO_BET`（算过了，不下注）必须分开显示。"""
    if risk is None:
        return ('<p class="muted">未接入风险面板（`risk` 为 null）—— '
                '这是「没算」，不是「风险为零」。</p>')
    v = str(risk.get("verdict", "UNDETERMINED"))
    cls = {"BET": "pass", "NO_BET": "fail"}.get(v, "unknown")
    out = [f'<p><b>凯利结论：<span class="{cls}">{esc(v)}</span></b> '
           f'{esc(risk.get("verdict_label", ""))}'
           f'　<span class="muted">理由 {esc(risk.get("verdict_reason") or "—")}</span></p>']
    k = risk.get("kelly") or {}
    gates = k.get("gates") or {}
    if gates:
        s = gates.get("sample", {})
        e = gates.get("edge", {})
        out.append(
            f'<p class="muted">样本门：{"过" if s.get("meets") else "不过"}'
            f'（{s.get("n_days")} / {s.get("min_days")} 交易日，{esc(s.get("label", ""))}）'
            f'　edge 门：{"过" if e.get("meets") else "不过"}'
            f'（保守 p {num(e.get("p_used"))} vs 盈亏平衡 {num(e.get("p_be"))}）'
            f'　f_final {num(k.get("f_final"))}</p>')
    for w in (risk.get("notes") or [])[:4]:
        out.append(f'<p class="muted">⚠️ {esc(w)}</p>')
    return "".join(out)


def _tiles(view: Mapping) -> str:
    ret = view["total_return_incl_fee"]
    return ('<div class="tiles">'
            + tile("总资产", money(view["total_assets"]), "现金 + 已定价持仓市值")
            + tile("现金", money(view["cash"]),
                   f'本金净投入 {view["cash_breakdown"]["net_deposits"]:,.2f}')
            + tile("持仓市值", money(view["market_value_priced"]),
                   ("含缺现价 " + ", ".join(view["missing_price_codes"]))
                   if view["missing_price_codes"] else "全部已定价")
            + tile("累计收益(含费)",
                   f'<span class="{sign_cls(view["total_pnl_incl_fee"])}">'
                   f'{money(view["total_pnl_incl_fee"])}</span>',
                   f'相对净投入 {ratio_pct(ret)}')
            + tile("累计净投入", money(view["net_invested"]), "本金口径（分红/税费不计）")
            + '</div>')


def _positions_table(view: Mapping) -> str:
    head = ("<tr><th>代码 / 名称</th><th>数量</th><th>均价(含费)</th><th>现价</th>"
            "<th>来源</th><th>现价日期</th><th>市值</th><th>浮动盈亏</th><th>占比</th>"
            "<th>状态</th></tr>")
    rows = []
    for p in view["positions"]:
        name = p.get("name") or ""
        if p["status"] == "missing_price":
            src = '<span class="unknown">无可用现价</span>'
            status = ('<span class="fail">缺现价</span>'
                      '<div class="muted" style="font-size:11.5px">已排除出市值合计，'
                      '不用成本价冒充</div>')
        else:
            src = esc(p["price_source"])
            status = '<span class="pass">已定价</span>'
        rows.append(
            f'<tr><td><a href="../trades?code={esc(p["code"])}">{esc(p["code"])}'
            f'</a> <span class="muted">{esc(name)}</span></td>'
            f'<td>{p["qty"]}</td><td>{num(p["avg_cost_incl_fee"])}</td>'
            f'<td>{num(p["price"])}</td><td>{src}</td>'
            f'<td>{esc(p["price_asof"]) if p["price_asof"] else num(None)}</td>'
            f'<td>{money(p["market_value"])}</td>'
            f'<td class="{sign_cls(p["float_pnl_incl_fee"])}">'
            f'{money(p["float_pnl_incl_fee"])}</td>'
            f'<td>{pct(p["weight_of_total_assets"])}</td><td>{status}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="10" class="muted">没有任何持仓</td></tr>')
    return f"<table>{head}{''.join(rows)}</table>"


def _discipline(view: Mapping) -> str:
    rows = []
    for c in view["discipline"]:
        rows.append(f'<tr><td>{status_cell(c["status"])}</td>'
                    f'<td style="text-align:left">{esc(c["check"])}</td>'
                    f'<td style="text-align:left">{esc(c["subject"] or "—")}</td>'
                    f'<td style="text-align:left">{esc(c["detail"])}</td></tr>')
    return ("<table><tr><th>状态</th><th>检查</th><th>标的</th><th>说明</th></tr>"
            + "".join(rows) + "</table>")


def _accuracy_block(acc: Mapping) -> str:
    """验证统计：**LIVE 与 REPLAY 分开，且各自带样本门槛**。"""
    out = [f'<p class="muted">窗口 {esc(acc["window"]["start"])} ~ '
           f'{esc(acc["window"]["end"])}（{acc["window"]["n_sessions"]} 个交易日）'
           f'　口径：{esc(acc["provenance"]["rule"])}</p>']
    live_n = acc["provenance"]["live"]["n_rows"]
    out.append(f'<p><b>LIVE（实盘）</b>：{live_n} 行'
               + ('' if live_n else '　<span class="fail">没有实盘样本 —— '
                                    '下面的 REPLAY 数字**不是**实盘表现</span>')
               + '</p>')
    for name, bucket in (("LIVE", acc.get("live")), ("REPLAY", acc.get("replay"))):
        if bucket is None:
            out.append(f'<p class="muted">{name}：无样本</p>')
            continue
        gate = bucket["sample_gate"]
        mark = ('<span class="pass">样本充足</span>' if gate["meets"]
                else f'<span class="warn">{esc(gate["label"])}</span>')
        out.append(
            f'<p>{name}：按日聚类有效样本 '
            f'<b>{bucket["effective_n_days"]}</b> 交易日 / 门槛 {gate["min_days"]} '
            f'（{mark}）　方向准确率 {num(bucket["direction_accuracy_daily"], 4)}　'
            f'Brier {num(bucket["brier_daily"], 4)}　'
            f'不可评分 {bucket["n_unscorable"]} 行</p>')
        base = bucket.get("baselines_daily") or {}
        if base:
            out.append('<p class="muted">常数基线：' + "　".join(
                f'{esc(k)} {num(v, 4)}' for k, v in sorted(base.items()))
                + '　（跑不赢基线就是没有技能）</p>')
    return "".join(out)


# ---------- 页面组装 ----------

def overview_page(summary: Mapping, *, base: str, built_at: str) -> str:
    view = summary["portfolio"]
    nav = summary["nav"]
    fresh = summary["freshness"]
    risk = summary.get("risk")
    body = [
        '<div class="card"><h2>资产</h2>', _tiles(view), '</div>',
        '<div class="card"><h2>净值曲线（按日 mark-to-market）</h2>',
        nav_svg(nav["points"], net_invested=view["net_invested"]),
        f'<p class="muted">{esc(nav["policy"])}</p>',
    ]
    if nav["missing_price_dates"]:
        body.append(f'<p class="fail">缺现价 → 断点：'
                    f'{esc("、".join(nav["missing_price_dates"]))}</p>')
    body += ['</div>',
             '<div class="card"><h2>持仓</h2>', _positions_table(view),
             f'<p class="muted">{esc(view["cost_policy"])}</p>',
             f'<p class="muted">{esc(view["price_policy"])}</p></div>',
             '<div class="card"><h2>纪律</h2>', _discipline(view),
             '<h3>动作建议（按纪律换算成股数）</h3>']
    adv = view.get("advisory") or []
    body.append('<ul class="tight">' + "".join(
        f'<li>{esc(a["note"])}</li>' for a in adv) + '</ul>' if adv
        else '<p class="muted">无建议（没有持仓或没有可用现价）</p>')
    body += ['</div>',
             '<div class="card"><h2>风险摘要</h2>', _kelly_line(risk)]
    if risk is not None and risk.get("trend"):
        t = risk["trend"]
        st = t.get("state")
        cls = {"UP": "pass", "DOWN": "fail"}.get(str(st), "unknown")
        body.append(f'<p>趋势状态：<span class="{cls}">{esc(st) if st else "未知"}</span>'
                    f'（MA20 {num(t.get("ma20"))} / MA60 {num(t.get("ma60"))}，'
                    f'{esc(t.get("asof")) if t.get("asof") else "无"}）'
                    f'　<span class="muted">{esc(t.get("note", ""))}</span></p>')
    body += [f'<p><a href="{esc(base)}/risk">查看完整风险口径 →</a></p></div>',
             '<div class="card"><h2>数据新鲜度</h2>',
             f'<p>bars_daily 最新 {esc(fresh["bars_latest_date"])}'
             f'　快照最新 {esc((fresh["snapshot_latest"] or {}).get("ts"))}'
             f'（{esc((fresh["snapshot_latest"] or {}).get("trade_date"))}）</p>']
    if fresh["codes_without_bars"]:
        body.append(f'<p class="warn">无 K 线标的：'
                    f'{esc("、".join(fresh["codes_without_bars"]))}</p>')
    body += ['</div>',
             '<div class="card"><h2>最近验证统计</h2>',
             _accuracy_block(summary["accuracy"]),
             f'<p><a href="{esc(base)}/data">查看数据与事件 →</a></p></div>']
    return layout(base=base, title="总览", body="".join(body), asof=summary["asof"],
                  built_at=built_at, current="/", alarms=summary["alarms"])


def _trade_form(base: str, *, token: str, form_id: str,
                values: Mapping | None = None, note: str = "") -> str:
    v = dict(values or {})
    return (
        f'<form method="post" action="{esc(base)}/trades">'
        f'<input type="hidden" name="_token" value="{esc(token)}">'
        f'<input type="hidden" name="_form_id" value="{esc(form_id)}">'
        + (f'<p class="muted" style="width:100%">{note}</p>' if note else "")
        + '<label>日期<input type="date" name="date" required value="'
        f'{esc(v.get("date", ""))}"></label>'
        '<label>代码<input name="code" required size="8" value="'
        f'{esc(v.get("code", ""))}"></label>'
        '<label>方向<select name="side">'
        f'<option value="buy"{" selected" if v.get("side", "buy") == "buy" else ""}>买入</option>'
        f'<option value="sell"{" selected" if v.get("side") == "sell" else ""}>卖出</option>'
        '</select></label>'
        '<label>价格<input name="price" required inputmode="decimal" size="8" value="'
        f'{esc(v.get("price", ""))}"></label>'
        '<label>数量(股)<input name="qty" required inputmode="numeric" size="7" value="'
        f'{esc(v.get("qty", ""))}"></label>'
        '<label>费用<input name="fee" inputmode="decimal" size="7" value="'
        f'{esc(v.get("fee", "0"))}"></label>'
        '<label>备注<input name="note" size="14" value="'
        f'{esc(v.get("note", ""))}"></label>'
        '<button type="submit">录入成交</button></form>')


def _cash_form(base: str, *, token: str, form_id: str,
               values: Mapping | None = None, note: str = "") -> str:
    v = dict(values or {})
    kinds = (("deposit", "本金存入"), ("withdraw", "本金取出"),
             ("dividend", "分红"), ("fee", "费用"), ("tax", "税费"), ("other", "其他"))
    opts = "".join(
        f'<option value="{k}"{" selected" if v.get("kind", "deposit") == k else ""}>'
        f'{cn}</option>' for k, cn in kinds)
    return (
        f'<form method="post" action="{esc(base)}/cash">'
        f'<input type="hidden" name="_token" value="{esc(token)}">'
        f'<input type="hidden" name="_form_id" value="{esc(form_id)}">'
        + (f'<p class="muted" style="width:100%">{note}</p>' if note else "")
        + '<label>日期<input type="date" name="date" required value="'
        f'{esc(v.get("date", ""))}"></label>'
        f'<label>种类<select name="kind">{opts}</select></label>'
        '<label>金额<input name="amount" required inputmode="decimal" size="10" value="'
        f'{esc(v.get("amount", ""))}"></label>'
        '<label>备注<input name="note" size="16" value="'
        f'{esc(v.get("note", ""))}"></label>'
        '<button type="submit">录入现金流</button></form>'
        '<p class="muted">金额**有符号**：正 = 流入组合，负 = 流出。'
        '存入/分红必须 &gt; 0，取出/费用/税费必须 &lt; 0，金额 0 不接受。</p>')


def _receipt_block(receipt: Mapping | None, view: Mapping) -> str:
    """写入回执：**写了什么** + **写完之后是什么状态**（同一个页面里给全）。"""
    if not receipt:
        return ""
    fails = [c for c in view["discipline"] if c["status"] == "FAIL"]
    undet = [c for c in view["discipline"] if c["status"] == "UNDETERMINED"]
    if receipt["state"] == "identical":
        head = (f'幂等命中：这份表单已经提交过（{esc(receipt["what"])} '
                f'id={receipt["id"]}），**没有写第二行**。')
        kind = "warn"
    else:
        head = f'已写入：{esc(receipt["what"])} <b>id={receipt["id"]}</b>'
        kind = "ok"
    held = "　".join(
        f'{esc(p["code"])} {p["qty"]} 股（现值 {money(p["market_value"])}，'
        f'占比 {pct(p["weight_of_total_assets"])}）' for p in view["positions"])
    return banner(
        f'{head}<br>写入后：现金 {money(view["cash"])}　总资产 '
        f'{money(view["total_assets"])}　持仓 {held or "无"}<br>'
        f'纪律：{len(fails)} 条 FAIL、{len(undet)} 条未判定'
        + ("<br>" + "；".join(esc(c["detail"]) for c in fails) if fails else ""),
        kind)


def trades_page(data: Mapping, *, base: str, built_at: str, token: str,
                form_id: str, receipt: Mapping | None = None,
                error: str = "", values: Mapping | None = None) -> str:
    rows = []
    for r in data["rows"]:
        flags = []
        if r["trade_id"] in data["reversed_ids"]:
            flags.append('<span class="fail">已冲正</span>')
        if (r["note"] or "").startswith("冲正 #"):
            flags.append('<span class="warn">冲正单</span>')
        rows.append(
            f'<tr><td><a href="{esc(base)}/trades/{r["trade_id"]}">'
            f'#{r["trade_id"]}</a></td><td>{esc(r["date"])}</td>'
            f'<td style="text-align:left">{esc(r["code"])}</td>'
            f'<td>{"买入" if r["side"] == "buy" else "卖出"}</td>'
            f'<td>{num(r["price"])}</td><td>{r["qty"]}</td><td>{num(r["fee"])}</td>'
            f'<td>{num(float(r["price"]) * r["qty"] + (r["fee"] if r["side"] == "buy" else -r["fee"]))}</td>'
            f'<td style="text-align:left" class="muted">{esc(r["note"] or "")} '
            f'{" ".join(flags)}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="9" class="muted">还没有任何成交</td></tr>')
    body = [
        _receipt_block(receipt, data["view"]),
        ('<div class="banner bad">校验未通过：' + esc(error) + '</div>') if error else "",
        '<div class="card"><h2>录入成交</h2>',
        _trade_form(base, token=token, form_id=form_id, values=values,
                    note="买入必须 100 股整数倍；卖出不得超过当时持仓；"
                         "日期不得晚于最近已收盘交易日。"),
        '</div>',
        f'<div class="card"><h2>成交流水（{len(data["rows"])} 笔）</h2>'
        '<p class="muted">账本 append-only：改错只能**冲正**（追加一笔反向记录），'
        '原行永远保留。</p>',
        '<table><tr><th>ID</th><th>日期</th><th>代码</th><th>方向</th><th>价格</th>'
        '<th>数量</th><th>费用</th><th>现金流影响</th><th>备注</th></tr>'
        + "".join(rows) + '</table></div>',
    ]
    return layout(base=base, title="成交流水", body="".join(body),
                  asof=data["asof"], built_at=built_at, current="/trades")


def duplicate_page(data: Mapping, *, base: str, built_at: str, token: str,
                   submitted: Mapping, existing: Mapping,
                   endpoint: str, fields: Mapping, label: str) -> str:
    """疑似重复：**不写库**，要求人显式确认「这是另一笔真单」。"""
    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
        for k, v in fields.items())
    detail = "　".join(f"{esc(k)}={esc(v)}" for k, v in fields.items()
                       if not k.startswith("_"))
    body = [
        banner("<b>疑似重复</b>：你提交的这笔与已有记录完全相同。"
               "系统**没有写入任何东西** —— 请确认这是另一笔真实成交，"
               "还是手滑点了两次。", "warn"),
        f'<div class="card"><h2>这次提交</h2><p>{detail}</p>'
        f'<p class="muted">已存在：#{esc(existing.get("id"))}　'
        f'{esc(existing.get("detail", ""))}</p>'
        f'<form method="post" action="{esc(base + endpoint)}">{hidden}'
        '<input type="hidden" name="_token" value="' + esc(token) + '">'
        '<label class="chk"><input type="checkbox" name="confirm_duplicate" '
        'value="1" required>我确认这是另一笔真实成交（不是重复提交）</label>'
        f'<button type="submit">确认写入</button> '
        f'<a href="{esc(base)}">取消</a></form></div>',
    ]
    return layout(base=base, title=label, body="".join(body), asof=data["asof"],
                  built_at=built_at)


def trade_detail_page(trade: Mapping, *, base: str, built_at: str, token: str,
                      form_id: str, error: str = "") -> str:
    body = [
        ('<div class="banner bad">冲正失败：' + esc(error) + '</div>') if error else "",
        f'<div class="card"><h2>成交 #{trade["trade_id"]}</h2>'
        '<table>'
        f'<tr><td>日期</td><td>{esc(trade["date"])}</td></tr>'
        f'<tr><td>代码</td><td>{esc(trade["code"])}</td></tr>'
        f'<tr><td>方向</td><td>{"买入" if trade["side"] == "buy" else "卖出"}</td></tr>'
        f'<tr><td>价格</td><td>{num(trade["price"])}</td></tr>'
        f'<tr><td>数量</td><td>{trade["qty"]} 股</td></tr>'
        f'<tr><td>费用</td><td>{num(trade["fee"])}</td></tr>'
        f'<tr><td>成交额</td><td>{money(float(trade["price"]) * trade["qty"])}</td></tr>'
        f'<tr><td>备注</td><td>{esc(trade["note"] or "")}</td></tr>'
        f'<tr><td>录入时刻</td><td>{esc(trade["created_at"])}</td></tr></table></div>',
    ]
    if trade["reversed_by"]:
        body.append(banner("这笔**已被冲正**，冲正单："
                           + "、".join(f'#{i}' for i in trade["reversed_by"])
                           + "。原行仍在（append-only）。", "warn"))
    if trade["is_reversal"]:
        body.append(banner("这笔本身就是一张**冲正单**（反向记录）。", "warn"))
    body.append(
        '<div class="card"><h2>冲正这笔成交</h2>'
        '<p class="muted">冲正 = 追加一笔**反向**记录（同日期、同价、同量、同费），'
        '原行**不会被修改或删除**。原因必填 —— 冲正而不写原因，事后没人能判断'
        '这是纠错还是又一次手滑。</p>'
        f'<form method="post" action="{esc(base)}/trades/{trade["trade_id"]}/reverse"'
        ' onsubmit="return confirm(\'确认冲正这一笔？将追加一笔反向记录。\')">'
        '<input type="hidden" name="_token" value="' + esc(token) + '">'
        '<input type="hidden" name="_form_id" value="' + esc(form_id) + '">'
        '<label>冲正原因<input name="reason" required size="30" '
        'placeholder="例如：价格录错，实际 86.08"></label>'
        '<button class="danger" type="submit">确认冲正</button></form></div>')
    return layout(base=base, title=f"成交 #{trade['trade_id']}", body="".join(body),
                  asof=trade["date"], built_at=built_at, current="/trades")


def cash_page(data: Mapping, *, base: str, built_at: str, token: str,
              form_id: str, receipt: Mapping | None = None, error: str = "",
              values: Mapping | None = None) -> str:
    rows = "".join(
        f'<tr><td>#{r["flow_id"]}</td><td>{esc(r["date"])}</td>'
        f'<td style="text-align:left">{esc(r["kind"])}</td>'
        f'<td class="{sign_cls(r["amount"])}">{money(r["amount"])}</td>'
        f'<td style="text-align:left" class="muted">{esc(r["note"] or "")}</td></tr>'
        for r in data["rows"]) or '<tr><td colspan="5" class="muted">还没有现金流</td></tr>'
    s = data["summary"]
    body = [
        _receipt_block(receipt, data["view"]),
        ('<div class="banner bad">校验未通过：' + esc(error) + '</div>') if error else "",
        '<div class="card"><h2>录入本金 / 现金流</h2>',
        _cash_form(base, token=token, form_id=form_id, values=values), '</div>',
        '<div class="card"><h2>现金流分解</h2><div class="tiles">'
        + tile("现金", money(data["cash"]))
        + tile("本金净投入", money(s["net_deposits"]), "deposit + withdraw")
        + tile("成交净额", money(s["trade_cash"]), "买入流出 / 卖出流入（含费）")
        + tile("其他", money(s["other_cash"]), "分红 / 费用 / 税费")
        + '</div></div>',
        f'<div class="card"><h2>流水（{len(data["rows"])} 笔）</h2>'
        '<table><tr><th>ID</th><th>日期</th><th>种类</th><th>金额</th><th>备注</th></tr>'
        + rows + '</table>'
        '<p class="muted">append-only：改错请追加一笔反向记录，不要删。</p></div>',
    ]
    return layout(base=base, title="现金流", body="".join(body), asof=data["asof"],
                  built_at=built_at, current="/cash")


def risk_page(data: Mapping, *, base: str, built_at: str) -> str:
    risk = data["risk"]
    subject = data["subject"]
    body: list[str] = []
    if risk is None:
        body.append('<div class="card"><h2>风险</h2>'
                    '<p class="muted">当前没有任何持仓 → 没有挂靠标的，风险面板'
                    '**未接入**（不是「风险为零」）。</p></div>')
        return layout(base=base, title="风险", body="".join(body),
                      asof=data["asof"], built_at=built_at, current="/risk")
    body.append(f'<div class="card"><h2>风险 · {esc(risk["code"])}'
                f'（{esc(subject.get("name") or "")}）</h2>'
                f'<p class="muted">规则 {esc(risk["rule"])}　视界 {risk["horizon"]} '
                f'交易日　分数凯利 k={risk["frac"]}　asof {esc(risk["asof"])}　'
                f'数据状态 {esc(risk["data_status"])}</p>'
                + _kelly_line(risk) + '</div>')
    k = risk.get("kelly")
    if k:
        g = k["gates"]
        body.append(
            '<div class="card"><h2>凯利口径（完整）</h2><table>'
            f'<tr><td>回放往返</td><td>{k["inputs"]["n_trades"]} 次'
            f'（赢 {k["inputs"]["n_wins"]} / 亏 {k["inputs"]["n_losses"]}）</td></tr>'
            f'<tr><td>有效样本</td><td>{k["inputs"]["n_days"]} 交易日'
            f'（门槛 {g["sample"]["min_days"]}）'
            f'　{"<span class=pass>样本充足</span>" if g["sample"]["meets"] else "<span class=warn>" + esc(g["sample"]["label"]) + "</span>"}</td></tr>'
            f'<tr><td>胜率</td><td>点估 {num(k["p_point"])}　'
            f'保守（Wilson 95% 下界）{num(k["p_used"])}　'
            f'CI {esc(k["p_ci95"])}</td></tr>'
            f'<tr><td>赔率 b</td><td>点估 {num(k["b_point"])}　'
            f'保守（赢下四分位/亏上四分位）{num(k["b_used"])}</td></tr>'
            f'<tr><td>盈亏平衡胜率</td><td>{num(k["p_be"])}</td></tr>'
            f'<tr><td>严格凯利 f*</td><td>{num(k["f_star"])}'
            f'　连续近似 μ/σ² {num(k["f_star_continuous"])}（仅对照，不参与）</td></tr>'
            f'<tr><td>分数凯利</td><td>k={k["k"]} → {num(k["f_fractional"])}'
            f'　上限 {k["f_cap"]}{"（已命中）" if k["f_cap_applied"] else ""}</td></tr>'
            f'<tr><td>最终仓位 f_final</td><td><b>{num(k["f_final"])}</b>'
            f'{"（过注拒绝已 clip）" if k["overbet_rejected"] else ""}</td></tr>'
            f'<tr><td>往返成本</td><td>{num(k["inputs"]["cost_bps"], 2)} bps</td></tr>'
            f'<tr><td>回放窗口</td><td>{esc(k["inputs"]["window"]["start"])} ~ '
            f'{esc(k["inputs"]["window"]["end"])}</td></tr>'
            '</table>'
            f'<p class="muted">{esc(k["inputs"]["cost_policy"])}</p>'
            '<h3>约束逐条</h3><ul class="tight">'
            + "".join(f'<li>{esc(w)}</li>' for w in k["warnings"]) + '</ul></div>')
    m = risk.get("metrics")
    if m:
        vt = m["vol_target"] or {}
        body.append(
            '<div class="card"><h2>风险预算指标</h2><table>'
            f'<tr><td>样本</td><td>{m["sample"]["n_days"]} 日'
            f'（门槛 {m["sample"]["min_days"]}）'
            f'　{"<span class=pass>样本充足</span>" if m["sample"]["meets"] else "<span class=warn>" + esc(m["sample"]["label"]) + "</span>"}</td></tr>'
            f'<tr><td>年化波动率 RV20</td><td>{ratio_pct(m["rv20_annual"])}</td></tr>'
            f'<tr><td>VaR95 / CVaR95</td><td>{ratio_pct((m["var_cvar"] or {}).get("var95"))}'
            f' / {ratio_pct((m["var_cvar"] or {}).get("cvar95"))}</td></tr>'
            f'<tr><td>最大回撤 250日 / 全历史</td>'
            f'<td>{ratio_pct(m["mdd_250d"]["mdd_pct"])} / '
            f'{ratio_pct(m["mdd_all"]["mdd_pct"])}</td></tr>'
            f'<tr><td>索提诺 / Calmar</td><td>{num(m["sortino"])} / '
            f'{num(m["calmar"])}</td></tr>'
            f'<tr><td>波动率目标仓位</td><td>{num(vt.get("f"))}'
            f'　<span class="muted">{esc(vt.get("note", ""))}</span></td></tr>'
            f'<tr><td>破产风险</td><td>{num((m.get("ruin") or {}).get("ruin_risk"))}'
            f'　<span class="muted">{esc((m.get("ruin") or {}).get("note", ""))}</span></td></tr>'
            '</table></div>')
    st = risk.get("stops")
    if st and st.get("levels"):
        rows = "".join(
            f'<tr><td style="text-align:left">{esc(l["name"])}</td>'
            f'<td>{num(l["level"])}</td><td>{esc(l["kind"])}</td>'
            f'<td>{num(l.get("distance_pct"), 2)}%</td>'
            f'<td style="text-align:left" class="muted">{esc(l.get("source", ""))}</td>'
            '</tr>' for l in st["levels"])
        body.append(
            '<div class="card"><h2>止损位并列</h2>'
            f'<p>现价 {num(st["price"])}　最先触发：'
            f'<b>{esc((st.get("first_trigger") or {}).get("name") or "无")}</b>'
            f'　ATR(14) {num(st.get("atr"))}</p>'
            + (f'<p class="warn">{esc(st["overlap_warning"])}</p>'
               if st.get("overlap_warning") else "")
            + '<table><tr><th>线</th><th>价位</th><th>性质</th><th>距现价</th>'
              '<th>来源</th></tr>' + rows + '</table>'
            f'<p class="muted">{esc(st.get("note", ""))}</p></div>')
    if risk["errors"]:
        body.append('<div class="card"><h2>取数错误</h2><ul class="tight">'
                    + "".join(f'<li>{esc(e)}</li>' for e in risk["errors"])
                    + '</ul></div>')
    return layout(base=base, title="风险", body="".join(body), asof=data["asof"],
                  built_at=built_at, current="/risk")


def data_page(data: Mapping, *, base: str, built_at: str) -> str:
    c, b, s = data["calendar"], data["bars"], data["snapshots"]
    fresh = data["freshness"]
    snaps = fresh["snapshots"]
    ev = "".join(
        f'<tr><td>{esc(r["ts"])}</td><td>{esc(r["module"])}</td>'
        f'<td class="{ {"error": "fail", "warn": "warn"}.get(r["level"], "") }">'
        f'{esc(r["level"])}</td>'
        f'<td style="text-align:left">{esc(r["message"])}</td></tr>'
        for r in data["events"]) or '<tr><td colspan="4" class="muted">无事件</td></tr>'
    body = [
        '<div class="card"><h2>数据新鲜度</h2><table>'
        f'<tr><td>bars_daily</td><td>{b["n_rows"]} 行，最新 {esc(b["latest"])}'
        f'　asof 当日：'
        f'{esc(fresh["bars_latest_by_code"])}</td></tr>'
        f'<tr><td>quote_snapshots</td><td>{s["n_rows"]} 行，最新 ts '
        f'{esc(s["latest_ts"])}</td></tr>'
        f'<tr><td>asof 当日快照</td><td>{snaps["n_rows"]} 行 / '
        f'{snaps["n_codes"]} 标的 / {snaps["n_slots"]} 个时点</td></tr>'
        f'<tr><td>trading_calendar</td><td>{c["n_open_days"]} 个交易日，最新 '
        f'{esc(c["latest_open"])}</td></tr>'
        '</table>'
        + (f'<p class="warn">无 K 线标的：{esc("、".join(fresh["codes_without_bars"]))}</p>'
           if fresh["codes_without_bars"] else "")
        + '</div>',
        '<div class="card"><h2>验证统计（LIVE / REPLAY 分开）</h2>'
        + _accuracy_block(data["accuracy"]) + '</div>',
        f'<div class="card"><h2>最近事件（{len(data["events"])} 条）</h2>'
        '<table><tr><th>时刻</th><th>模块</th><th>级别</th><th>消息</th></tr>'
        + ev + '</table></div>',
    ]
    if data["alarms"]:
        body.append('<div class="card"><h2>告警原文</h2><ul class="tight">'
                    + "".join(f'<li>{esc(a)}</li>' for a in data["alarms"])
                    + '</ul></div>')
    return layout(base=base, title="数据", body="".join(body), asof=data["asof"],
                  built_at=built_at, current="/data")


def error_page(*, base: str, status: int, message: str, asof: str,
               built_at: str) -> str:
    body = (f'<div class="card"><h2>{status}</h2><p>{message}</p>'
            f'<p><a href="{esc(base)}/">回到总览</a></p></div>')
    return layout(base=base, title=str(status), body=body, asof=asof,
                  built_at=built_at)


__all__ = ["CSS", "NAV_ITEMS", "banner", "cash_page", "data_page",
           "duplicate_page", "error_page", "esc", "layout", "money", "nav_svg",
           "num", "overview_page", "pct", "ratio_pct", "risk_page", "tile",
           "trade_detail_page", "trades_page"]
