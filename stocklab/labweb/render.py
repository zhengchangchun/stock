"""服务端渲染（P16）：对账单版式 + inline SVG，**零外部引用**。

## 硬约束（有测试钉住）

- 全站只有**两个**外部资源，都是本应用自己的静态路由：
  `{base}/static/app.css` 与 `{base}/static/app.js`。
  没有 CDN、没有外链字体、没有图表库、没有图标库、没有 JS 框架。
- 所有 `href` / `action` / `src` 都带 `--base-path` 前缀（子路径部署）。

## 版式：券商对账单，不是 SaaS 仪表盘

细线分栏、数字右对齐（`tabular-nums`）、来源与时间做 11.5px 脚注、
饱和色只出现在纪律状态与盈亏上。首屏顺序写死：
CANONICAL 一行数字 → 纪律条 → 持仓表 → 净值曲线。
设计依据见 `docs/plans/2026-09-15-p16-UI设计.md`。

## 诚实性（红线，写在渲染层是因为它就是「显示」这件事本身）

- `None` 一律渲染成 **未知**（斜体灰），不渲染成 `0`；
- `missing_price` 的持仓：现价/市值/浮盈/占比全部「未知」+ 一句「不用成本价冒充」；
- 凯利 `NO_BET` 原样显示（含理由与两道门）；
- 准确率 **LIVE 与 REPLAY 分开**，LIVE 为 0 时明说「这不是实盘表现」；
- **颜色不是唯一信号**：每个状态同时带文字（`PASS` / `未判定` …）。

## 局部更新（`fragment`）

`render.pane_*` 产出的片段与整页里对应的 `<div id="...-pane">` 内容**同源**
（同一个函数），所以 `fetch` 换上去的结果和整页刷新**逐字一致** ——
两套模板是「局部更新后和刷新后长得不一样」的经典来源。
"""

from __future__ import annotations

import re
from html import escape as _escape
from typing import Any, Mapping, Sequence

#: 整手规模从**决策层导入**，不在渲染层写 100：卖出须 100 股整数倍这条规则
#: 全项目只有一个常数来源（`portfolio/decision.LOT_SIZE`），抄一份到这里
#: 就等着它和上游分叉 —— 那时页面会用旧阈值说「能执行」。
from stocklab.portfolio.decision import LOT_SIZE

#: 静态资源路径（相对 base-path 之下）。
CSS_PATH = "static/app.css"
JS_PATH = "static/app.js"

#: 状态 → (CSS 类, 中文)。四态，**没有第五态**（见 portfolio/discipline.py）。
STATUS_CN = {
    "PASS": ("pass", "通过"),
    "WARN": ("warn", "偏离"),
    "FAIL": ("fail", "不通过"),
    "UNDETERMINED": ("unknown", "未判定"),
}

#: 纪律检查的机器名 → 台账上的人话。**只换标签，不改判据**；
#: 认不出的检查原样显示机器名（宁可难看，也不许猜它是什么意思）。
CHECK_CN = {
    "single_position_max_40pct": "单票上限",
    "cash_band_45_60pct": "现金目标带",
    "stop_loss_close_85": "收盘止损线",
    "stop_loss_weekly_83_25": "周线止损线",
    "no_add_above_87": "禁补仓线",
    "cash_per_trade_max_5pct": "单笔现金上限",
}

NAV_ITEMS = (
    ("/", "总览"),
    ("/trades", "成交流水"),
    ("/cash", "现金流"),
    ("/risk", "风险"),
    ("/data", "数据"),
    ("/health", "健康检查"),
)

#: 表单字段名白名单：只有出现在这里的名字才会被当成「字段级错误」标到格子旁。
TRADE_FIELDS = ("date", "code", "side", "price", "qty", "fee", "note")
CASH_FIELDS = ("date", "kind", "amount", "note")


# ---------- 基础格式化 ----------

def esc(x: Any) -> str:
    return _escape("" if x is None else str(x), quote=True)


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_TICK = re.compile(r"`([^`]+)`")


def rich(text: Any) -> str:
    """账本/规则里的 `**强调**` 与 `` `代码` `` → 真正的标记。

    先转义再替换，所以不会引入 XSS；不做完整的 Markdown（这里不是文档渲染器）。
    **在 P16 之前，页面上是真的把 `**超** 40% 上限` 原样显示出来的。**
    """
    out = esc(text)
    out = _BOLD.sub(r"<b>\1</b>", out)
    return _TICK.sub(r"<code>\1</code>", out)


def money(x: float | None, *, digits: int = 2) -> str:
    if x is None:
        return '<span class="s-unknown">未知</span>'
    return f"{float(x):,.{digits}f}"


def signed_money(x: float | None, *, digits: int = 2) -> str:
    """带正负号的钱。0 不加号（`+0.00` 会让「没赚没亏」看起来像赚了）。"""
    if x is None:
        return '<span class="s-unknown">未知</span>'
    v = float(x)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.{digits}f}"


def num(x: Any, digits: int = 4) -> str:
    if x is None:
        return '<span class="s-unknown">未知</span>'
    return f"{float(x):.{digits}f}"


def pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return '<span class="s-unknown">未知</span>'
    return f"{float(x):.{digits}f}%"


def ratio_pct(x: float | None, digits: int = 2) -> str:
    """小数比率 → 百分数（`None` → 未知）。"""
    if x is None:
        return '<span class="s-unknown">未知</span>'
    return f"{float(x) * 100:.{digits}f}%"


def sign_cls(x: float | None) -> str:
    if x is None:
        return "s-unknown"
    v = float(x)
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def status_cell(status: str) -> str:
    cls, cn = STATUS_CN.get(status, ("unknown", "未知"))
    return f'<span class="s-{cls}">{esc(status)} {cn}</span>'


def pnl_excl_fee(view: Mapping) -> float | None:
    """不含费累计收益。

    这不是新口径，是 `cost_policy` 里写死的恒等式：
    「成本默认含费…不含费口径同时输出（`*_excl_fee`）。**差额即累计费用**」。
    所以 `不含费 = 含费 + Σ 已付费用`，两个分量都来自组合视图。
    """
    incl = view.get("total_pnl_incl_fee")
    if incl is None:
        return None
    fees = sum(float(p.get("fees_paid") or 0.0) for p in view.get("positions", []))
    return float(incl) + fees


# ---------- 布局 ----------

def _rail(base: str, current: str, asof: str) -> str:
    links = "".join(
        f'<a class="rail__link" href="{esc(base + path)}"'
        f'{" aria-current=\"page\"" if path == current else ""}>{esc(label)}</a>'
        for path, label in NAV_ITEMS)
    return (f'<nav class="rail" aria-label="主导航">'
            f'<div class="rail__brand"><span class="rail__name">stock-lab</span>'
            f'<span class="rail__sub">持仓与纪律台账<br>数据 asof {esc(asof)}</span>'
            f'</div><div class="rail__nav">{links}</div></nav>')


def alarms_banner(alarms: Sequence[str]) -> str:
    if not alarms:
        return ""
    items = "".join(f"<li>{rich(a)}</li>" for a in alarms)
    return (f'<div class="banner bad"><b>{len(alarms)} 条需要注意</b>'
            f'<ul>{items}</ul></div>')


def layout(*, base: str, title: str, body: str, asof: str, built_at: str,
           current: str = "", alarms: Sequence[str] = ()) -> str:
    warn = alarms_banner(alarms)
    return (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} · stock-lab</title>"
        f'<link rel="stylesheet" href="{esc(base)}/{CSS_PATH}">'
        '</head><body><div class="wrap">'
        f'{_rail(base, current, asof)}'
        f'<main class="sheet">{warn}{body}'
        f'<p class="foot">页面生成于 {esc(built_at)}（Asia/Shanghai）　'
        f'账本 append-only，改错只能冲正　只绑回环 127.0.0.1　'
        f'所有数字来自账本与行情库；没有数的地方写「未知」，不写 0。</p>'
        '</main></div>'
        f'<script src="{esc(base)}/{JS_PATH}" defer></script>'
        '</body></html>\n')


def banner(text: str, kind: str = "ok", *, landed: bool = False) -> str:
    return (f'<div class="banner {esc(kind)}{" landed" if landed else ""}">'
            f'{rich(text)}</div>')


def alert(text: str, kind: str = "bad") -> str:
    return f'<p class="alert s-{esc(kind)}" role="alert">{rich(text)}</p>'


def section(title: str, inner: str, *, note: str = "", right: str = "",
            detail: str = "") -> str:
    """一个结论块。

    `detail` 非空时追加一个「查看详细」折叠块（P17/T4）——
    `inner` 是**首屏就要看到的结论**，`detail` 是长文。
    两者是分开的入参而不是一个字符串，是为了让调用点自己交代
    「哪句是结论、哪句是细节」，避免把关键结论顺手塞进折叠里。
    """
    r = f'<span class="sec__n">{rich(right)}</span>' if right else ""
    n = f'<p class="note">{rich(note)}</p>' if note else ""
    return (f'<section class="sec"><h2 class="sec__h">{esc(title)}{r}</h2>'
            f'{inner}{n}{detail}</section>')


def more(inner: str, *, label: str = "查看详细", note: str = "") -> str:
    """「查看详细」折叠块（P17 / T4）—— **零 JS**，用原生 `<details>`。

    ## 为什么是 `<details>` 而不是 JS 展开 / 独立详情页

    1. **零依赖、离线可看**：原生元素，没有脚本也能展开；`app.js` 是渐进增强，
       它挂了本块照样能用（这是本项目对前端的既有纪律）。
    2. **内容在 HTML 里**：折叠 ≠ 不渲染。长文进了 DOM，浏览器内查找（Ctrl+F）、
       打印、朗读都仍覆盖得到 —— 换成「点了才 fetch」就丢掉这些。
    3. **手机可点**：`<summary>` 是原生控件，390 宽下命中区域由 CSS 保底
       （`.more__s` 的 `min-height:44px`），不用自己算触摸目标。
    4. **键盘 / 读屏可用**：`<summary>` 自带 `aria-expanded` 语义，不用补 ARIA。

    默认**收起**：首屏只放「怎么做」，深度内容一次点击可达。
    """
    n = f'<p class="note">{rich(note)}</p>' if note else ""
    return (f'<details class="more"><summary class="more__s">{esc(label)}</summary>'
            f'<div class="more__b">{inner}{n}</div></details>')


def glance(lines: Sequence[str]) -> str:
    """首屏「怎么做」摘要：一行一条，不加框不加图。空列表 → 空串。"""
    if not lines:
        return ""
    return ('<ul class="glance">'
            + "".join(f"<li>{rich(x)}</li>" for x in lines) + "</ul>")


def coverage_table(cov: Mapping, *, base: str = "") -> str:
    """标的覆盖表：**现价 + 来源 + 来源日期**（P17 / T5）。

    「来源日期」是价格**实际所属**的日期，不是页面的 asof —— 停牌时会指向更早的
    交易日，页面必须如实显示，否则「这个数字是哪天的」就追不了。

    拿不到价 → 显式写「无现价」，**不用成本价冒充**（`prices` 模块铁律）。
    """
    head = ("<tr><th>代码</th><th>名称</th><th>口径</th><th>现价</th>"
            "<th>来源</th><th>来源日期</th><th>K 线</th></tr>")
    rows = []
    for r in cov["rows"]:
        price = ("<span class=\"mut\">无现价</span>" if r["price"] is None
                 else money(r["price"]))
        src = {"snapshot": "快照", "bars": "日线"}.get(str(r["source"]), "—")
        adjust = ("可复权" if r["adjustable"]
                  else '<span class="s-warn" title="ADR-008：事件源不可见">不可复权</span>')
        rows.append(
            f'<tr><td><code>{esc(r["code"])}</code></td><td>{esc(r["name"])}</td>'
            f'<td>{esc(r["type"])}</td><td class="num">{price}</td>'
            f'<td>{esc(src)}</td><td>{esc(r["price_asof"] or "—")}</td>'
            f'<td class="num">{r["bars_rows"]}</td></tr>')
        if not r["adjustable"]:
            rows.append(
                f'<tr><td></td><td colspan="6" class="note">'
                f'{esc(r["code"])} 的复权链不可用：除权除息事件不在数据源里，'
                f'写全 1 因子等于拿未复权价冒充复权价（ADR-008）。'
                f'该标的只用于估值展示（adj_mode=none）。</td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}{"".join(rows)}</table>'
            f'</div>')


def cell(k: str, v_html: str, note: str = "", *, small: bool = False,
         alt: str = "") -> str:
    return (f'<div class="canon__cell"><div class="canon__k">{esc(k)}</div>'
            f'<div class="canon__v{" is-sm" if small else ""}">{v_html}</div>'
            f'{f"<div class=canon__alt>{rich(alt)}</div>" if alt else ""}'
            f'{f"<div class=canon__n>{rich(note)}</div>" if note else ""}</div>')


# ---------- 纪律条（首屏主角） ----------

def discipline_rail(view: Mapping) -> str:
    """逐条 PASS/WARN/FAIL。出格的那一格是**实底红块**，全页唯一的高对比元素。"""
    cells = []
    for c in view["discipline"]:
        cls = {"PASS": "pass", "WARN": "warn", "FAIL": "fail"}.get(
            c["status"], "unknown")
        name = CHECK_CN.get(c["check"], c["check"])
        sub = c.get("subject") or ""
        value = _headline_number(c)
        cells.append(
            f'<div class="disc__cell s-{cls}">'
            f'<div class="disc__st">{esc(c["status"])}'
            f'　{esc(STATUS_CN.get(c["status"], ("", "未知"))[1])}</div>'
            f'<div class="disc__name">{esc(name)}'
            + (f'<span class="disc__sub">　{esc(sub)}</span>' if sub else "")
            + '</div>'
            + (f'<div class="disc__v">{value}</div>' if value else "")
            + f'<div class="disc__d">{rich(c["detail"])}</div></div>')
    if not cells:
        return '<p class="note">没有可判定的纪律条目。</p>'
    return f'<div class="disc">{"".join(cells)}</div>'


def _headline_number(check: Mapping) -> str:
    """纪律格上那个「一眼看到」的数字。

    只从该检查**自己给出的 `numbers`** 里挑一个最有信息量的，做纯格式化 ——
    **不计算、不推算**。挑不出来就返回空串（宁可少一个数字，也不许造一个）。
    """
    n = check.get("numbers") or {}

    def f(key: str) -> float | None:
        v = n.get(key)
        return None if v is None else float(v)

    w, lim = f("weight_pct"), f("limit_pct")
    if w is not None and lim is not None:
        return (f'{w:.2f}%'
                f'<span class="disc__sub"> / 上限 {lim:.0f}%</span>')
    for key, digits in (("cash_pct", 2), ("close", 2), ("weekly_close", 2),
                        ("price", 2)):
        v = f(key)
        if v is not None:
            return f'{v:.{digits}f}'
    amount = f("limit_amount")
    if amount is not None:
        return f'{amount:,.2f}'
    return ""


# ---------- 净值曲线（inline SVG） ----------

def nav_svg(points: Sequence[Mapping], *, net_invested: float | None = None,
            width: int = 1080, height: int = 232) -> str:
    """按日净值折线。`total_assets is None` 的日**断线**并标注，不插值。"""
    valid = [(i, p) for i, p in enumerate(points) if p["total_assets"] is not None]
    if len(valid) < 2:
        return ('<p class="note">净值曲线需要至少两个交易日的可用市值；'
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
    left, right, top, bottom = 74, 14, 14, 28
    plot_w, plot_h = width - left - right, height - top - bottom

    def xy(idx: int, v: float) -> tuple[float, float]:
        x = left + (idx / max(1, n - 1)) * plot_w
        y = top + (1.0 - (v - lo) / (hi - lo)) * plot_h
        return x, y

    parts: list[str] = []
    # 本金参考线（虚线）：净值在它下面 = 亏，一眼可判。**没有网格线。**
    if ref is not None:
        _, ry = xy(0, ref)
        parts.append(f'<line x1="{left}" y1="{ry:.1f}" x2="{width - right}" '
                     f'y2="{ry:.1f}" stroke="#8c98a4" stroke-dasharray="5 4"/>')
        parts.append(f'<text x="{left - 8}" y="{ry + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="#5a6672">{ref:,.0f} 本金</text>')

    # 连续段：None 处断开，**不插值**（插值会让缺价那天看起来有净值）
    run: list[tuple[int, Mapping]] = []
    for item in valid + [(None, None)]:
        idx, p = item
        if p is None or (run and idx != run[-1][0] + 1):
            if len(run) >= 2:
                d = " ".join(f"{x:.1f},{y:.1f}"
                             for x, y in (xy(i, float(q["total_assets"]))
                                          for i, q in run))
                parts.append(f'<polyline fill="none" stroke="#123e6b" '
                             f'stroke-width="2" points="{d}"/>')
            elif run:
                x, y = xy(run[0][0], float(run[0][1]["total_assets"]))
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" '
                             f'fill="#123e6b"/>')
            run = []
        if p is not None:
            run.append((idx, p))
    # 末日一个点：让「最新净值」在线上有落点
    last_i, last_p = valid[-1]
    lx, ly = xy(last_i, float(last_p["total_assets"]))
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.5" fill="#123e6b"/>')
    for i, p in valid:
        if p["status"] == "missing_price":
            x, _ = xy(i, lo)
            parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" '
                         f'y2="{top + plot_h}" stroke="#a81c1c" '
                         f'stroke-dasharray="2 3"/>')

    parts.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" '
                 f'y2="{top + plot_h}" stroke="#12181f"/>')
    parts.append(f'<text x="{left}" y="{height - 8}" font-size="11" '
                 f'fill="#5a6672">{esc(points[0]["date"])}</text>')
    parts.append(f'<text x="{width - right}" y="{height - 8}" text-anchor="end" '
                 f'font-size="11" fill="#5a6672">{esc(points[-1]["date"])}</text>')
    parts.append(f'<text x="{left - 8}" y="{top + 4}" text-anchor="end" '
                 f'font-size="11" fill="#5a6672">{hi:,.0f}</text>')
    parts.append(f'<text x="{left - 8}" y="{top + plot_h}" text-anchor="end" '
                 f'font-size="11" fill="#5a6672">{lo:,.0f}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="按日净值曲线（虚线为本金）">'
            + "".join(parts) + "</svg>")


# ---------- 表格 ----------

def _weight_bar(weight: float | None, limit: float | None = 40.0) -> str:
    if weight is None:
        return ""
    w = max(0.0, min(100.0, float(weight)))
    cls = "bar"
    if limit is not None and float(weight) > float(limit):
        cls = "bar over"
    elif limit is not None and float(weight) > float(limit) * 0.9:
        cls = "bar cap"
    return f'<span class="{cls}" aria-hidden="true"><i style="width:{w:.1f}%"></i></span>'


def positions_table(view: Mapping, *, base: str = "") -> str:
    head = ("<tr><th>代码 / 名称</th><th>数量</th><th>均价<br>(含费)</th><th>现价</th>"
            "<th>来源</th><th>现价日期</th><th>市值</th><th>浮动盈亏</th>"
            "<th>占总资产</th><th>状态</th></tr>")
    rows = []
    for p in view["positions"]:
        name = p.get("name") or ""
        if p["status"] == "missing_price":
            src = '<span class="s-unknown">无可用现价</span>'
            status = ('<span class="s-fail">缺现价</span>'
                      '<div class="sub">已排除出市值合计，不用成本价冒充</div>')
            weight = '<span class="s-unknown">未知</span>'
        else:
            src = esc(p["price_source"])
            status = '<span class="s-pass">已定价</span>'
            weight = (f'{_weight_bar(p["weight_of_total_assets"])}'
                      f'{pct(p["weight_of_total_assets"])}')
        rows.append(
            f'<tr><td><a href="{esc(base)}/trades?code={esc(p["code"])}">'
            f'{esc(p["code"])}</a> <span class="sub">{esc(name)}</span></td>'
            f'<td>{p["qty"]}</td><td>{num(p["avg_cost_incl_fee"])}</td>'
            f'<td>{num(p["price"])}</td>'
            f'<td class="l"><span class="sub">{src}</span></td>'
            f'<td><span class="sub">'
            f'{esc(p["price_asof"]) if p["price_asof"] else "未知"}</span></td>'
            f'<td>{money(p["market_value"])}</td>'
            f'<td class="{sign_cls(p["float_pnl_incl_fee"])}">'
            f'{signed_money(p["float_pnl_incl_fee"])}</td>'
            f'<td>{weight}</td><td class="l">{status}</td></tr>')
    if not rows:
        rows.append('<tr class="empty"><td colspan="10">没有任何持仓</td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}{"".join(rows)}'
            '</table></div>')


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
        + ("<br>" + "；".join(rich(c["detail"]) for c in fails) if fails else ""),
        kind)


# ---------- 表单 ----------

def _field(name: str, label: str, inner: str, *, width: str = "",
           err: str = "", hint: str = "") -> str:
    return (f'<div class="field {width}{" bad" if err else ""}" '
            f'data-field="{esc(name)}"><label for="f-{esc(name)}">{esc(label)}</label>'
            f'{inner}'
            + (f'<span class="hint">{esc(hint)}</span>' if hint else "")
            + f'<span class="err" role="alert">{esc(err)}</span></div>')


def _text(name: str, *, type_: str = "text", size: int = 0, required: bool = False,
          value: str = "", inputmode: str = "", placeholder: str = "") -> str:
    a = [f'id="f-{esc(name)}"', f'name="{esc(name)}"']
    if type_ != "text":
        a.append(f'type="{esc(type_)}"')
    if size:
        a.append(f'size="{size}"')
    if required:
        a.append("required")
    if inputmode:
        a.append(f'inputmode="{esc(inputmode)}"')
    if placeholder:
        a.append(f'placeholder="{esc(placeholder)}"')
    a.append(f'value="{esc(value)}"')
    return f'<input {" ".join(a)}>'


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def _trade_form(base: str, *, token: str, form_id: str,
                values: Mapping | None = None, note: str = "",
                err_field: str = "", err: str = "") -> str:
    v = dict(values or {})
    side = v.get("side", "buy") or "buy"
    fields = (
        _field("date", "日期", _text("date", type_="date", required=True,
                                    value=v.get("date", "")), width="w-date",
               err=err if err_field == "date" else "")
        + _field("code", "代码", _text("code", size=8, required=True,
                                       value=v.get("code", "")), width="w-code",
                 err=err if err_field == "code" else "")
        + _field("side", "方向",
                 f'<select id="f-side" name="side">'
                 f'<option value="buy"{" selected" if side == "buy" else ""}>买入</option>'
                 f'<option value="sell"{" selected" if side == "sell" else ""}>卖出</option>'
                 f'</select>', width="w-side", err=err if err_field == "side" else "")
        + _field("price", "价格", _text("price", size=8, required=True,
                                        inputmode="decimal",
                                        value=v.get("price", "")), width="w-price",
                 err=err if err_field == "price" else "")
        + _field("qty", "数量（股）", _text("qty", size=7, required=True,
                                          inputmode="numeric",
                                          value=v.get("qty", "")), width="w-qty",
                 err=err if err_field == "qty" else "")
        + _field("fee", "费用", _text("fee", size=7, inputmode="decimal",
                                      value=v.get("fee", "0")), width="w-fee",
                 err=err if err_field == "fee" else "")
        + _field("note", "备注", _text("note", size=14, value=v.get("note", "")),
                 width="w-note", err=err if err_field == "note" else "")
    )
    return (
        f'<form method="post" action="{esc(base)}/trades" class="form" '
        f'data-fragment="1" data-pane="#trades-pane">'
        + _hidden("_token", token) + _hidden("_form_id", form_id)
        + (f'<p class="note">{rich(note)}</p>' if note else "")
        + f'<div class="form__row">{fields}'
        '<button class="btn" type="submit">录入成交</button></div>'
        '<p class="note">买入必须 100 股整数倍；卖出不得超过当时持仓；'
        '日期不得晚于最近已收盘交易日。所有写入都进 append-only 账本。</p>'
        '</form>')


def _cash_form(base: str, *, token: str, form_id: str,
               values: Mapping | None = None, note: str = "",
               err_field: str = "", err: str = "") -> str:
    v = dict(values or {})
    kinds = (("deposit", "本金存入"), ("withdraw", "本金取出"),
             ("dividend", "分红"), ("fee", "费用"), ("tax", "税费"), ("other", "其他"))
    opts = "".join(
        f'<option value="{k}"{" selected" if v.get("kind", "deposit") == k else ""}>'
        f'{cn}</option>' for k, cn in kinds)
    fields = (
        _field("date", "日期", _text("date", type_="date", required=True,
                                    value=v.get("date", "")), width="w-date",
               err=err if err_field == "date" else "")
        + _field("kind", "种类", f'<select id="f-kind" name="kind">{opts}</select>',
                 width="w-kind", err=err if err_field == "kind" else "")
        + _field("amount", "金额", _text("amount", size=10, required=True,
                                         inputmode="decimal",
                                         value=v.get("amount", "")), width="w-amount",
                 err=err if err_field == "amount" else "")
        + _field("note", "备注", _text("note", size=16, value=v.get("note", "")),
                 width="w-note", err=err if err_field == "note" else "")
    )
    return (
        f'<form method="post" action="{esc(base)}/cash" class="form" '
        f'data-fragment="1" data-pane="#cash-pane">'
        + _hidden("_token", token) + _hidden("_form_id", form_id)
        + (f'<p class="note">{rich(note)}</p>' if note else "")
        + f'<div class="form__row">{fields}'
        '<button class="btn" type="submit">录入现金流</button></div>'
        '<p class="note">金额**有符号**：正 = 流入组合，负 = 流出。'
        '存入/分红必须 &gt; 0，取出/费用/税费必须 &lt; 0，金额 0 不接受。</p>'
        '</form>')


# ---------- 片段（局部更新用，与整页同源） ----------

#: 写入回执块（整页与局部更新共用，所以是公开的）。
receipt_block = _receipt_block


def trades_pane(data: Mapping, *, base: str) -> str:
    """成交流水表。整页与 `fetch` 局部更新**共用**这一个函数。"""
    rows = []
    for r in data["rows"]:
        void = r["trade_id"] in data["reversed_ids"]
        flags = []
        if void:
            flags.append('<span class="tag s-fail">已冲正</span>')
        if (r["note"] or "").startswith("冲正 #"):
            flags.append('<span class="tag s-warn">冲正单</span>')
        cash = float(r["price"]) * r["qty"] + (
            r["fee"] if r["side"] == "buy" else -r["fee"])
        rows.append(
            f'<tr{" class=is-void" if void else ""}>'
            f'<td><a href="{esc(base)}/trades/{r["trade_id"]}">'
            f'#{r["trade_id"]}</a></td><td>{esc(r["date"])}</td>'
            f'<td class="l">{esc(r["code"])}</td>'
            f'<td>{"买入" if r["side"] == "buy" else "卖出"}</td>'
            f'<td>{num(r["price"])}</td><td>{r["qty"]}</td><td>{num(r["fee"])}</td>'
            f'<td class="{sign_cls(-cash)}">{signed_money(-cash)}</td>'
            f'<td class="l"><span class="sub">{esc(r["note"] or "")}</span> '
            f'{" ".join(flags)}</td></tr>')
    if not rows:
        rows.append('<tr class="empty"><td colspan="9">还没有任何成交</td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">'
            '<tr><th>ID</th><th>日期</th><th>代码</th><th>方向</th><th>价格</th>'
            '<th>数量</th><th>费用</th><th>现金影响</th><th>备注 / 状态</th></tr>'
            + "".join(rows) + '</table></div>')


def cash_pane(data: Mapping, *, base: str) -> str:
    """现金流：分解 band + 流水表。整页与局部更新共用。"""
    s = data["summary"]
    rows = "".join(
        f'<tr><td><a href="{esc(base)}/cash">{r["flow_id"]}</a></td>'
        f'<td>{esc(r["date"])}</td><td class="l">{esc(r["kind"])}</td>'
        f'<td class="{sign_cls(r["amount"])}">{signed_money(r["amount"])}</td>'
        f'<td class="l"><span class="sub">{esc(r["note"] or "")}</span></td></tr>'
        for r in data["rows"]) or (
        '<tr class="empty"><td colspan="5">还没有现金流</td></tr>')
    return (
        '<div class="canon">'
        + cell("现金", money(data["cash"]))
        + cell("本金净投入", money(s["net_deposits"]), "存入 + 取出", small=True)
        + cell("成交净额", money(s["trade_cash"]), "买入流出 / 卖出流入（含费）",
               small=True)
        + cell("其他", money(s["other_cash"]), "分红 / 费用 / 税费", small=True)
        + '</div>'
        f'<div class="scroll-x" style="margin-top:14px"><table class="tbl">'
        '<tr><th>ID</th><th>日期</th><th>种类</th><th>金额</th><th>备注</th></tr>'
        + rows + '</table></div>')


# ---------- 各页面 ----------

def _kelly_line(risk: Mapping | None) -> str:
    """凯利结论行。`None`（未接入）与 `NO_BET`（算过了，不下注）必须分开显示。"""
    if risk is None:
        return ('<p class="note">未接入风险面板（`risk` 为 null）—— '
                '这是「没算」，不是「风险为零」。</p>')
    v = str(risk.get("verdict", "UNDETERMINED"))
    cn = {"BET": "可以下注", "NO_BET": "不下注"}.get(v, "未判定")
    cls = {"BET": "pass", "NO_BET": "fail"}.get(v, "unknown")
    out = [f'<p><b>凯利结论：<span class="s-{cls}">{esc(v)}　{cn}</span></b>　'
           f'{esc(risk.get("verdict_label", ""))}</p>',
           f'<p class="note">理由：{rich(risk.get("verdict_reason") or "—")}</p>']
    k = risk.get("kelly") or {}
    gates = k.get("gates") or {}
    if gates:
        s = gates.get("sample", {})
        e = gates.get("edge", {})
        out.append(
            f'<p class="note">样本门 {"过" if s.get("meets") else "不过"}'
            f'（{s.get("n_days")} / {s.get("min_days")} 交易日，'
            f'{esc(s.get("label", ""))}）　'
            f'edge 门 {"过" if e.get("meets") else "不过"}'
            f'（保守 p {num(e.get("p_used"))} vs 盈亏平衡 {num(e.get("p_be"))}）　'
            f'f_final {num(k.get("f_final"))}</p>')
    for w in (risk.get("notes") or [])[:4]:
        out.append(f'<p class="note">⚠ {rich(w)}</p>')
    return "".join(out)


def advisory_list(adv: Sequence[Mapping]) -> str:
    """「怎么做」的减仓折算回显。**不可执行的股数不许留着误导人**（P36）。

    纪律按 10% / 20% 折算出的股数，在 100 股这种一手仓上会算出 10 股 / 20 股 ——
    而卖出的申报数量必须是 `LOT_SIZE` 的整数倍（深交所 3.3.8），这两笔**执行不了**。
    页面上留着一句「减仓 10 股」而不说它下不出去，就是在教人下一张废单。

    判据用 `decision.LOT_SIZE`（**导入**，不在这里写 100）：整手规模全项目只有
    一个常数来源，抄一份到这里就等着它和上游分叉。
    """
    items = []
    for a in adv:
        shares = int(a["shares"])
        bad = shares <= 0 or shares % LOT_SIZE != 0
        flag = ('<span class="s-warn">　⚠ 该笔执行不了（卖出须 '
                f'{LOT_SIZE} 股的整数倍）</span>' if bad else "")
        items.append(f'<li>{rich(a["note"])}{flag}　'
                     f'<span class="mut">规则 {esc(a["rule"])}</span></li>')
    return '<ul class="list">' + "".join(items) + "</ul>"


def _paper_block(paper: Mapping | None) -> str:
    """模拟盘各臂**最新一交易日**的净值（P36）。多臂并列，不排名、不挑推荐。

    `paper_nav_daily` 的行是**已落库的事实**，这里只显示，不重算、不补数：
    没有数据时如实写「暂无模拟盘记录」，而不是显示一行 0。
    """
    if not paper or not paper.get("arms"):
        return ('<p class="note">暂无模拟盘记录'
                '（`paper_nav_daily` 里没有 ≤ 查询日的净值行）。</p>')
    rows = []
    for a in paper["arms"]:
        ret = a.get("cum_return")
        cls = sign_cls(ret)
        rows.append(
            f'<tr><td class="l">{esc(a["account_id"])}</td>'
            f'<td>{esc(a["date"])}</td>'
            f'<td class="num">{money(a.get("cash"))}</td>'
            f'<td class="num">{money(a.get("market_value"))}</td>'
            f'<td class="num">{money(a.get("nav"))}</td>'
            f'<td class="num {cls}">{ratio_pct(ret)}</td>'
            f'<td class="num">{money(a.get("drawdown"))}</td></tr>')
    return (f'<p class="note">净值日期 <b>{esc(paper["date"])}</b>　'
            f'共 {len(paper["arms"])} 条臂（同一天各一行，'
            f'来自 `paper_nav_daily`，本节不重算）</p>'
            f'<div class="scroll-x"><table class="tbl">'
            f'<tr><th>账户</th><th>净值日</th><th>现金</th><th>持仓市值</th>'
            f'<th>净值</th><th>累计收益</th><th>回撤</th></tr>'
            + "".join(rows) + '</table></div>'
            f'<p class="note">{rich(paper.get("policy", ""))}</p>')


def actions_block(actions: Mapping | None) -> str:
    """「能不能动」逐只持仓的结论（P36）：headline + 为什么 + 选项 + 全清试算。

    结论、整手原因（`whole_lot_reason`）、试算金额全部来自
    `portfolio.decision.position_decision` —— 本函数**只摆出来**，
    不重写那句话、不重算那笔钱。`state=unknown` 时也照常渲染，
    因为「判不了」必须带上理由，否则页面看起来像「没事」。
    """
    rows = list((actions or {}).get("rows") or [])
    if not rows:
        return ('<p class="note">没有可判的持仓（账本里没有未平仓标的）——'
                '"没有可动的仓"是结论，不是缺数据。</p>')
    out = []
    for r in rows:
        state = str(r.get("state"))
        cls = {"stop": "fail", "no_add": "warn", "hold": "pass"}.get(state,
                                                                     "unknown")
        name = r.get("name") or ""
        head = f'{esc(r["code"])}'
        if name:
            head += f'　{esc(name)}'
        lines = [f'<p class="act__h"><b>{head}　'
                 f'<span class="s-{cls}">{rich(r["headline"])}</span></b>'
                 f'　<span class="mut">状态 {esc(state)}</span></p>']
        for b in r.get("because") or []:
            lines.append(f'<p class="note">{rich(b)}</p>')
        opts = r.get("options") or []
        if opts:
            lines.append('<ul class="list">'
                         + "".join(f'<li>{rich(o)}</li>' for o in opts)
                         + '</ul>')
        sell = r.get("sell_all")
        money_row = r.get("money") or {}
        close = money_row.get("close")
        if sell is not None:
            lines.append(
                f'<p class="note">全清试算：{money_row.get("qty")} 股 × '
                f'收盘 {money(sell.get("close"))} 元（成交价 '
                f'{money(sell.get("fill_price"))}）到手 '
                f'<b>{money(sell.get("proceeds"))}</b> 元　'
                f'费用合计 {money(sell.get("fee_total"))} 元'
                f'（佣金 / 印花税 / 过户费逐项见「查看详细」）</p>')
        elif close is None:
            lines.append(
                f'<p class="note">全清试算：取不到收盘价，算不出这 '
                f'{money_row.get("qty")} 股到手多少 —— 不拿买入价冒充。</p>')
        rules = r.get("rules") or []
        disc = r.get("disclosure") or []
        detail = ""
        if rules or disc:
            detail = more(
                '<ul class="list">'
                + "".join(f'<li>{rich(x)}</li>' for x in rules + disc)
                + '</ul>', label="查看详细：判据与公式")
        out.append(f'<div class="act">{chr(10).join(lines)}{detail}</div>')
    return "".join(out)


def overview_page(summary: Mapping, *, base: str, built_at: str) -> str:
    view = summary["portfolio"]
    nav = summary["nav"]
    fresh = summary["freshness"]
    risk = summary.get("risk")
    ret = view["total_return_incl_fee"]

    canon = (
        '<div class="canon">'
        + cell("总资产", money(view["total_assets"]), "现金 + 已定价持仓市值")
        + cell("现金", money(view["cash"]),
               f'本金净投入 {view["cash_breakdown"]["net_deposits"]:,.2f}')
        + cell("持仓市值", money(view["market_value_priced"]),
               ("缺现价 " + "、".join(view["missing_price_codes"])
                + " 已排除") if view["missing_price_codes"] else "全部已定价")
        + cell("累计收益", f'<span class="{sign_cls(view["total_pnl_incl_fee"])}">'
                          f'{signed_money(view["total_pnl_incl_fee"])}</span>',
               f'相对净投入 {ratio_pct(ret)}',
               alt=f'不含费 {signed_money(pnl_excl_fee(view))}'
                   f'（差 {abs(sum(float(p.get("fees_paid") or 0) for p in view["positions"])):,.2f}'
                   f' = 累计已付费用）')
        + '</div>')

    body = [
        canon,
        # 告警条排在 CANONICAL 数字**之后**：首屏第一个视觉落点是大数字，
        # 第二个就是纪律条里那块红的。告警条抢在数字前面会把数字挤出首屏。
        alarms_banner(summary["alarms"]),
    ]

    # ---------- 怎么做（P17/T4）：买什么 / 买多少 / 什么价 / 下一步 ----------
    # 这一段**常显**，是首屏的主角；其余各块的深度内容一律收进「查看详细」。
    adv = view.get("advisory") or []
    if adv:
        how = advisory_list(adv)
    else:
        how = ('<p class="note">无建议（没有持仓或没有可用现价）——'
               '「无建议」本身是结论，不是缺数据。</p>')
    body.append(section("怎么做", how, right="按纪律换算成股数",
                        detail=more(_how_detail(view, nav, base=base),
                                    label="查看详细：成本与口径")))

    # ---------- 能不能动（P36）：结论层已经算好了，这一段只把它摆出来 ----------
    # 上面「怎么做」是纪律折算的股数；这里回答的是另一个问题：
    # 「以我现在的持仓，到底动不动得了」。两者都常显 —— 把结论藏进折叠，
    # 等于让「算了但没说」继续存在。
    body.append(section("能不能动", actions_block(summary.get("actions")),
                        right="只看日收盘价"))

    body.append(section(
        "模拟盘", _paper_block(summary.get("paper")),
        right="并行对照，不排名",
        detail=more('<p class="note">'
                    + rich("模拟盘是同一份行情上**并行运行**的几个机械臂账户，"
                           "用来对照纪律执行差异，**不含任何模型信号** —— "
                           "它的涨跌不是系统预测能力的证据。")
                    + f'完整分段口径见 <a href="{esc(base)}/data">数据</a> 页。</p>',
                    label="查看详细：模拟盘是什么")))

    body.append(section("纪律", glance(_discipline_glance(view)),
                        right="出格项标红，逐条给判据",
                        detail=more(discipline_rail(view))))
    body.append(section("持仓", glance(_position_glance(view)),
                        note=view["price_policy"],
                        detail=more(positions_table(view, base=base))))
    body.append(section(
        "净值曲线", f'<figure class="chart">'
                  f'{nav_svg(nav["points"], net_invested=view["net_invested"])}'
                  f'<figcaption>按日 mark-to-market，共 {nav["n_sessions"]} 个交易日'
                  f'（{esc(nav["window"]["start"] if "window" in nav else nav["points"][0]["date"])}'
                  f' ~ {esc(nav["points"][-1]["date"])}）</figcaption></figure>',
        note=("缺现价 → 断点：" + "、".join(nav["missing_price_dates"]))
        if nav["missing_price_dates"] else "",
        detail=more(f'<p class="note">{rich(nav["policy"])}</p>',
                    label="查看详细：净值口径")))

    risk_body = [_kelly_line(risk)]
    risk_detail = []
    if risk is not None and risk.get("trend"):
        t = risk["trend"]
        st = t.get("state")
        cls = {"UP": "pass", "DOWN": "fail"}.get(str(st), "unknown")
        risk_detail.append(
            f'<p>趋势状态：<span class="s-{cls}">{esc(st) if st else "未知"}</span>　'
            f'<span class="mut">MA20 {num(t.get("ma20"))} / MA60 {num(t.get("ma60"))}　'
            f'{esc(t.get("asof")) if t.get("asof") else "无日期"}</span></p>'
            f'<p class="note">{rich(t.get("note", ""))}</p>')
    risk_detail.append(
        f'<p class="note"><a href="{esc(base)}/risk">完整风险口径与约束逐条</a></p>')
    body.append(section("风险摘要", "".join(risk_body),
                        detail=more("".join(risk_detail), label="查看详细：趋势与约束")))

    fresh_body = [
        f'<p>bars_daily 最新 <b>{esc(fresh["bars_latest_date"])}</b>　'
        f'快照最新 <b>{esc((fresh["snapshot_latest"] or {}).get("ts"))}</b>'
        f'（交易日 {esc((fresh["snapshot_latest"] or {}).get("trade_date"))}）</p>']
    if fresh["codes_without_bars"]:
        fresh_body.append(f'<p class="s-warn">无 K 线标的：'
                          f'{esc("、".join(fresh["codes_without_bars"]))}</p>')
    fresh_body.append(f'<p class="note"><a href="{esc(base)}/data">数据明细与事件</a></p>')
    body.append(section("数据新鲜度", "".join(fresh_body)))

    # ---------- 口径与限制（P17/T4）：把「我们算不出什么」写在页面上 ----------
    cov = summary.get("coverage")
    if cov is not None:
        body.append(section(
            "口径与限制", glance(_limits_glance(cov)),
            right="算不出来的，明说",
            detail=more(coverage_table(cov, base=base)
                        + "<p class=\"note\">复权链限制详见 ADR-008："
                          "ETF 的除权除息事件不在当前数据源里，"
                          "本系统<b>不</b>用未复权价冒充复权价，宁可拒绝服务。</p>",
                        label="查看详细：逐标的现价与来源")))

    body.append(section("最近验证统计", _accuracy_block(summary["accuracy"])))
    return layout(base=base, title="总览", body="".join(body), asof=summary["asof"],
                  built_at=built_at, current="/")


def _discipline_glance(view: Mapping) -> list[str]:
    """纪律首屏摘要：**只报「有几条出格」**，逐条判据交给「查看详细」。

    判据键是 `view["discipline"]`，与 `discipline_rail` **同源**。
    这里曾经取错键（`checks`）→ 取不到 → 落到「全部通过」分支，
    而同一屏的纪律块里明明挂着 FAIL。**首屏说「没事」而正文里有事**，
    是这类页面最坏的失效模式（比报错糟得多：报错至少有人去看）。
    所以构造上分三段，最后一段专门兜「一条判据都没有」：
    那时如实说「没有可判定的条目」，**不冒充「通过」**。

    ## 返回**纯文本**，不要在这里做 HTML

    `glance()` 会对每条再走一遍 `rich()`（转义 + 把 `**x**` 变粗）。这里若先
    `rich()` 一次，就会**二次转义**：真页面上出现过 `&lt;b&gt;超&lt;/b&gt;`
    这种双重编码的字面量 —— 单元测试没抓住（它只看字符串内容），
    是打开真页面才看到的。标记交给 `glance()`，本函数只管语义分档。
    """
    checks = view.get("discipline") or []
    fails = [c for c in checks if str(c.get("status")) == "FAIL"]
    warns = [c for c in checks if str(c.get("status")) == "WARN"]
    if fails:
        return [f'{len(fails)} 条不通过：' + "；".join(
            str(c.get("detail", "")) for c in fails[:2])]
    if warns:
        return [f'{len(warns)} 条偏离（未出格）：' + "；".join(
            str(c.get("detail", "")) for c in warns[:2])]
    if checks:
        return [f'全部 {len(checks)} 条判据通过']
    return ["没有可判定的纪律条目（无持仓或无可用现价）——这不是「通过」"]


def _position_glance(view: Mapping) -> list[str]:
    """持仓首屏摘要：有几只、市值多少、缺不缺价。**不做估值口径的二次加工**。"""
    n = len(view["positions"])
    out = [f'{n} 只标的，已定价市值 {money(view["market_value_priced"])}']
    if view["missing_price_codes"]:
        out.append(f'{"、".join(view["missing_price_codes"])} 无现价，'
                   f'已排除出市值合计（不用成本价冒充）')
    return out


def _limits_glance(cov: Mapping) -> list[str]:
    """限制摘要：**把「我们算不出什么」放在首屏**，而不是藏在折叠里。

    一个只报好消息的页面会让人以为系统什么都能算。
    """
    out = [f'已登记 {len(cov["rows"])} 只标的']
    if cov["unadjustable"]:
        out.append(f'{"、".join(cov["unadjustable"])} 不可复权'
                   f'（事件源不可见，ADR-008）—— 只用于估值展示')
    if cov["missing"]:
        out.append(f'{"、".join(cov["missing"])} 当前无现价')
    return out


def _how_detail(view: Mapping, nav: Mapping, *, base: str) -> str:
    """「怎么做」的长文：成本口径逐项 + 下一步去哪。"""
    costs = view.get("costs") or {}
    rows = "".join(
        f'<tr><td>{esc(k)}</td><td class="num">{esc(v)}</td></tr>'
        for k, v in sorted(costs.items())) if costs else ""
    cost_tbl = (f'<div class="scroll-x"><table class="tbl">'
                f'<tr><th>成本项</th><th>取值</th></tr>{rows}</table></div>'
                if rows else
                '<p class="note">本次组合视图未带成本明细；'
                '成本模型默认按<b>标的</b>口径取（股票/ETF 印花税不同，见 ADR-008）。</p>')
    return (f'{cost_tbl}'
            f'<p class="note">口径：成本默认含费（avg_cost_incl_fee）；'
            f'不含费口径同时给出，差额即累计费用。'
            f'具体标的的现价与来源日期见「口径与限制」。</p>'
            f'<p class="note"><a href="{esc(base)}/trades">成交流水</a>　'
            f'<a href="{esc(base)}/cash">资金流水</a>　'
            f'<a href="{esc(base)}/risk">风险口径逐条</a></p>')


def _accuracy_block(acc: Mapping) -> str:
    """验证统计：**LIVE 与 REPLAY 分开，且各自带样本门槛**。"""
    out = [f'<p class="note">窗口 {esc(acc["window"]["start"])} ~ '
           f'{esc(acc["window"]["end"])}（{acc["window"]["n_sessions"]} 个交易日）　'
           f'口径：{rich(acc["provenance"]["rule"])}</p>']
    live_n = acc["provenance"]["live"]["n_rows"]
    out.append(f'<p><b>LIVE（实盘）</b> {live_n} 行'
               + ('' if live_n else '　<span class="s-fail">没有实盘样本 —— '
                                    '下面的 REPLAY 数字不是实盘表现</span>')
               + '</p>')
    for name, bucket in (("LIVE", acc.get("live")), ("REPLAY", acc.get("replay"))):
        if bucket is None:
            out.append(f'<p class="note">{name}：无样本</p>')
            continue
        gate = bucket["sample_gate"]
        mark = ('<span class="s-pass">样本充足</span>' if gate["meets"]
                else f'<span class="s-warn">{esc(gate["label"])}</span>')
        out.append(
            f'<p>{name}　按日聚类有效样本 <b>{bucket["effective_n_days"]}</b> 交易日 / '
            f'门槛 {gate["min_days"]}（{mark}）　方向准确率 '
            f'<b>{num(bucket["direction_accuracy_daily"], 4)}</b>　'
            f'Brier {num(bucket["brier_daily"], 4)}　'
            f'不可评分 {bucket["n_unscorable"]} 行</p>')
        base_ = bucket.get("baselines_daily") or {}
        if base_:
            out.append('<p class="note">常数基线：' + "　".join(
                f'{esc(k)} {num(v, 4)}' for k, v in sorted(base_.items()))
                + '　跑不赢基线就是没有技能</p>')
    return "".join(out)


def trades_page(data: Mapping, *, base: str, built_at: str, token: str,
                form_id: str, receipt: Mapping | None = None,
                error: str = "", values: Mapping | None = None,
                err_field: str = "") -> str:
    filt = data.get("filter")
    body = [
        '<div id="receipt">' + _receipt_block(receipt, data["view"]) + '</div>',
        (f'<div class="banner bad">校验未通过：{rich(error)}</div>' if error else ""),
        (f'<p class="note">只看 {esc(filt)}（共 {len(data["rows"])} 笔）　'
         f'<a href="{esc(base)}/trades">显示全部 {len(data.get("all_rows") or [])} 笔</a></p>'
         if filt else ""),
        section("录入成交", _trade_form(
            base, token=token, form_id=form_id, values=values,
            err_field=err_field, err=error), right="写入后立刻回读"),
        section(f'成交流水（{len(data["rows"])} 笔）',
                f'<div id="trades-pane">{trades_pane(data, base=base)}</div>',
                note="账本 append-only：改错只能**冲正**（追加一笔反向记录），"
                     "原行永远保留。"),
    ]
    return layout(base=base, title="成交流水", body="".join(body),
                  asof=data["asof"], built_at=built_at, current="/trades")


def duplicate_page(data: Mapping, *, base: str, built_at: str, token: str,
                   submitted: Mapping, existing: Mapping,
                   endpoint: str, fields: Mapping, label: str) -> str:
    """疑似重复：**不写库**，要求人显式确认「这是另一笔真单」。"""
    hidden = "".join(_hidden(k, v) for k, v in fields.items())
    detail = "　".join(f'{esc(k)}={esc(v)}' for k, v in fields.items()
                       if not k.startswith("_"))
    body = [
        banner("**疑似重复**：你提交的这笔与已有记录完全相同。系统没有写入任何东西 —— "
               "请确认这是另一笔真实成交，还是手滑点了两次。", "warn"),
        section("这次提交",
                f'<p>{detail}</p>'
                f'<p class="note">已存在：#{esc(existing.get("id"))}　'
                f'{esc(existing.get("detail", ""))}</p>'
                f'<form method="post" action="{esc(base + endpoint)}" class="form">'
                f'{hidden}{_hidden("_token", token)}'
                '<label class="field" style="flex-direction:row;align-items:center;'
                'gap:7px;font-size:13.5px">'
                '<input type="checkbox" name="confirm_duplicate" value="1" required>'
                '我确认这是另一笔真实成交（不是重复提交）</label>'
                '<div class="form__row" style="margin-top:10px">'
                '<button class="btn" type="submit">确认写入</button>'
                f'<a class="btn ghost" href="{esc(base)}" '
                'style="line-height:1.2">取消</a></div></form>'),
    ]
    return layout(base=base, title=label, body="".join(body), asof=data["asof"],
                  built_at=built_at)


def trade_detail_page(trade: Mapping, *, base: str, built_at: str, token: str,
                      form_id: str, error: str = "") -> str:
    body = [
        (f'<div class="banner bad">冲正失败：{rich(error)}</div>' if error else ""),
        section(f'成交 #{trade["trade_id"]}',
                '<table class="kv">'
                f'<tr><th>日期</th><td>{esc(trade["date"])}</td></tr>'
                f'<tr><th>代码</th><td>{esc(trade["code"])}</td></tr>'
                f'<tr><th>方向</th><td>{"买入" if trade["side"] == "buy" else "卖出"}</td></tr>'
                f'<tr><th>价格</th><td>{num(trade["price"])}</td></tr>'
                f'<tr><th>数量</th><td>{trade["qty"]} 股</td></tr>'
                f'<tr><th>费用</th><td>{num(trade["fee"])}</td></tr>'
                f'<tr><th>成交额</th><td>{money(float(trade["price"]) * trade["qty"])}</td></tr>'
                f'<tr><th>备注</th><td>{esc(trade["note"] or "")}</td></tr>'
                f'<tr><th>录入时刻</th><td>{esc(trade["created_at"])}</td></tr>'
                '</table>'),
    ]
    if trade["reversed_by"]:
        body.append(banner("这笔**已被冲正**，冲正单："
                           + "、".join(f'#{i}' for i in trade["reversed_by"])
                           + "。原行仍在（append-only）。", "warn"))
    if trade["is_reversal"]:
        body.append(banner("这笔本身就是一张**冲正单**（反向记录）。", "warn"))
    body.append(section(
        "冲正这笔成交",
        f'<form method="post" class="form" '
        f'action="{esc(base)}/trades/{trade["trade_id"]}/reverse" '
        f'data-confirm="确认冲正成交 #{trade["trade_id"]}？" '
        f'data-confirm-detail="将追加一笔反向记录（同日期、同价、同量、同费），'
        f'原行不会被修改或删除。" data-confirm-ok="确认冲正">'
        + _hidden("_token", token) + _hidden("_form_id", form_id)
        + '<div class="form__row">'
        + _field("reason", "冲正原因",
                 _text("reason", size=30, required=True,
                       placeholder="例如：价格录错，实际 86.08"), width="w-note")
        + '<button class="btn danger" type="submit">冲正</button></div>'
        '<p class="note">冲正 = 追加一笔**反向**记录（同日期、同价、同量、同费），'
        '原行不会被修改或删除。原因必填 —— 冲正而不写原因，'
        '事后没人能判断这是纠错还是又一次手滑。</p></form>'))
    return layout(base=base, title=f"成交 #{trade['trade_id']}", body="".join(body),
                  asof=trade["date"], built_at=built_at, current="/trades")


def cash_page(data: Mapping, *, base: str, built_at: str, token: str,
              form_id: str, receipt: Mapping | None = None, error: str = "",
              values: Mapping | None = None, err_field: str = "") -> str:
    body = [
        '<div id="receipt">' + _receipt_block(receipt, data["view"]) + '</div>',
        (f'<div class="banner bad">校验未通过：{rich(error)}</div>' if error else ""),
        section("录入本金 / 现金流", _cash_form(
            base, token=token, form_id=form_id, values=values,
            err_field=err_field, err=error)),
        section(f'现金流（{len(data["rows"])} 笔）',
                f'<div id="cash-pane">{cash_pane(data, base=base)}</div>',
                note="append-only：改错请追加一笔反向记录，不要删。"),
    ]
    return layout(base=base, title="现金流", body="".join(body), asof=data["asof"],
                  built_at=built_at, current="/cash")


def risk_page(data: Mapping, *, base: str, built_at: str) -> str:
    risk = data["risk"]
    subject = data["subject"]
    if risk is None:
        return layout(base=base, title="风险", body=section(
            "风险", '<p class="note">当前没有任何持仓 → 没有挂靠标的，'
                    '风险面板**未接入**（不是「风险为零」）。</p>'),
            asof=data["asof"], built_at=built_at, current="/risk")
    body = [section(
        f'风险 · {esc(risk["code"])}（{esc(subject.get("name") or "")}）',
        _kelly_line(risk),
        note=f'规则 {esc(risk["rule"])}　视界 {risk["horizon"]} 交易日　'
             f'分数凯利 k={risk["frac"]}　asof {esc(risk["asof"])}　'
             f'数据状态 {esc(risk["data_status"])}')]
    k = risk.get("kelly")
    if k:
        g = k["gates"]
        body.append(section(
            "凯利口径（完整）",
            '<table class="kv">'
            f'<tr><th>回放往返</th><td>{k["inputs"]["n_trades"]} 次'
            f'（赢 {k["inputs"]["n_wins"]} / 亏 {k["inputs"]["n_losses"]}）</td></tr>'
            f'<tr><th>有效样本</th><td>{k["inputs"]["n_days"]} 交易日'
            f'（门槛 {g["sample"]["min_days"]}）'
            f'　{"<span class=s-pass>样本充足</span>" if g["sample"]["meets"] else "<span class=s-warn>" + esc(g["sample"]["label"]) + "</span>"}</td></tr>'
            f'<tr><th>胜率</th><td>点估 {num(k["p_point"])}　'
            f'保守（Wilson 95% 下界）{num(k["p_used"])}　'
            f'CI {esc(k["p_ci95"])}</td></tr>'
            f'<tr><th>赔率 b</th><td>点估 {num(k["b_point"])}　'
            f'保守（赢下四分位/亏上四分位）{num(k["b_used"])}</td></tr>'
            f'<tr><th>盈亏平衡胜率</th><td>{num(k["p_be"])}</td></tr>'
            f'<tr><th>严格凯利 f*</th><td>{num(k["f_star"])}'
            f'　<span class="mut">连续近似 μ/σ² {num(k["f_star_continuous"])}'
            f'（仅对照，不参与）</span></td></tr>'
            f'<tr><th>分数凯利</th><td>k={k["k"]} → {num(k["f_fractional"])}'
            f'　上限 {k["f_cap"]}{"（已命中）" if k["f_cap_applied"] else ""}</td></tr>'
            f'<tr><th>最终仓位 f_final</th><td><b>{num(k["f_final"])}</b>'
            f'{"（过注拒绝已 clip）" if k["overbet_rejected"] else ""}</td></tr>'
            f'<tr><th>往返成本</th><td>{num(k["inputs"]["cost_bps"], 2)} bps</td></tr>'
            f'<tr><th>回放窗口</th><td>{esc(k["inputs"]["window"]["start"])} ~ '
            f'{esc(k["inputs"]["window"]["end"])}</td></tr></table>'
            '<h3 class="sec__h" style="font-size:14px;border:0;margin-top:14px">'
            '约束逐条</h3><ul class="list">'
            + "".join(f'<li>{rich(w)}</li>' for w in k["warnings"]) + '</ul>',
            note=k["inputs"]["cost_policy"]))
    m = risk.get("metrics")
    if m:
        vt = m["vol_target"] or {}
        body.append(section(
            "风险预算指标",
            '<table class="kv">'
            f'<tr><th>样本</th><td>{m["sample"]["n_days"]} 日'
            f'（门槛 {m["sample"]["min_days"]}）'
            f'　{"<span class=s-pass>样本充足</span>" if m["sample"]["meets"] else "<span class=s-warn>" + esc(m["sample"]["label"]) + "</span>"}</td></tr>'
            f'<tr><th>年化波动率 RV20</th><td>{ratio_pct(m["rv20_annual"])}</td></tr>'
            f'<tr><th>VaR95 / CVaR95</th><td>'
            f'{ratio_pct((m["var_cvar"] or {}).get("var95"))} / '
            f'{ratio_pct((m["var_cvar"] or {}).get("cvar95"))}</td></tr>'
            f'<tr><th>最大回撤 250日 / 全历史</th>'
            f'<td>{ratio_pct(m["mdd_250d"]["mdd_pct"])} / '
            f'{ratio_pct(m["mdd_all"]["mdd_pct"])}</td></tr>'
            f'<tr><th>索提诺 / Calmar</th><td>{num(m["sortino"])} / '
            f'{num(m["calmar"])}</td></tr>'
            f'<tr><th>波动率目标仓位</th><td>{num(vt.get("f"))}'
            f'　<span class="mut">{esc(vt.get("note", ""))}</span></td></tr>'
            f'<tr><th>破产风险</th><td>{num((m.get("ruin") or {}).get("ruin_risk"))}'
            f'　<span class="mut">{esc((m.get("ruin") or {}).get("note", ""))}</span></td></tr>'
            '</table>'))
    st = risk.get("stops")
    if st and st.get("levels"):
        rows = "".join(
            f'<tr><td class="l">{esc(l["name"])}</td>'
            f'<td>{num(l["level"])}</td><td class="l">{esc(l["kind"])}</td>'
            f'<td>{num(l.get("distance_pct"), 2)}%</td>'
            f'<td class="l"><span class="sub">{esc(l.get("source", ""))}</span></td>'
            '</tr>' for l in st["levels"])
        body.append(section(
            "止损位并列",
            f'<p>现价 <b>{num(st["price"])}</b>　最先触发：'
            f'<b>{esc((st.get("first_trigger") or {}).get("name") or "无")}</b>　'
            f'<span class="mut">ATR(14) {num(st.get("atr"))}</span></p>'
            + (f'<p class="s-warn">{rich(st["overlap_warning"])}</p>'
               if st.get("overlap_warning") else "")
            + '<div class="scroll-x" style="margin-top:8px"><table class="tbl">'
              '<tr><th>线</th><th>价位</th><th>性质</th><th>距现价</th>'
              '<th>来源</th></tr>' + rows + '</table></div>',
            note=st.get("note", "")))
    if risk["errors"]:
        body.append(section("取数错误", '<ul class="list">'
                            + "".join(f'<li>{rich(e)}</li>' for e in risk["errors"])
                            + '</ul>'))
    return layout(base=base, title="风险", body="".join(body), asof=data["asof"],
                  built_at=built_at, current="/risk")


def data_page(data: Mapping, *, base: str, built_at: str) -> str:
    c, b, s = data["calendar"], data["bars"], data["snapshots"]
    fresh = data["freshness"]
    snaps = fresh["snapshots"]
    ev = "".join(
        f'<tr><td class="sub">{esc(r["ts"])}</td><td class="l">{esc(r["module"])}</td>'
        f'<td class="{ {"error": "s-fail", "warn": "s-warn"}.get(r["level"], "") }">'
        f'{esc(r["level"])}</td>'
        f'<td class="l">{esc(r["message"])}</td></tr>'
        for r in data["events"]) or (
        '<tr class="empty"><td colspan="4">无事件</td></tr>')
    body = [
        section("数据新鲜度", '<table class="kv">'
                f'<tr><th>bars_daily</th><td>{b["n_rows"]} 行，最新 {esc(b["latest"])}'
                f'　asof 当日：{esc(fresh["bars_latest_by_code"])}</td></tr>'
                f'<tr><th>quote_snapshots</th><td>{s["n_rows"]} 行，最新 ts '
                f'{esc(s["latest_ts"])}</td></tr>'
                f'<tr><th>asof 当日快照</th><td>{snaps["n_rows"]} 行 / '
                f'{snaps["n_codes"]} 标的 / {snaps["n_slots"]} 个时点</td></tr>'
                f'<tr><th>trading_calendar</th><td>{c["n_open_days"]} 个交易日，最新 '
                f'{esc(c["latest_open"])}</td></tr></table>'
                + (f'<p class="s-warn">无 K 线标的：'
                   f'{esc("、".join(fresh["codes_without_bars"]))}</p>'
                   if fresh["codes_without_bars"] else "")),
        section("验证统计（LIVE / REPLAY 分开）", _accuracy_block(data["accuracy"])),
        section(f'最近事件（{len(data["events"])} 条）',
                '<div class="scroll-x"><table class="tbl">'
                '<tr><th>时刻</th><th>模块</th><th>级别</th><th>消息</th></tr>'
                + ev + '</table></div>'),
    ]
    if data["alarms"]:
        body.append(section("告警原文", '<ul class="list">'
                            + "".join(f'<li>{rich(a)}</li>' for a in data["alarms"])
                            + '</ul>'))
    return layout(base=base, title="数据", body="".join(body), asof=data["asof"],
                  built_at=built_at, current="/data")


def error_page(*, base: str, status: int, message: str, asof: str,
               built_at: str) -> str:
    body = section(str(status),
                   f'<p>{rich(message)}</p>'
                   f'<p class="note"><a href="{esc(base)}/">回到总览</a></p>')
    return layout(base=base, title=str(status), body=body, asof=asof,
                  built_at=built_at)


__all__ = ["CASH_FIELDS", "CHECK_CN", "CSS_PATH", "JS_PATH", "LOT_SIZE",
           "NAV_ITEMS",
           "STATUS_CN", "TRADE_FIELDS", "actions_block", "advisory_list",
           "alert", "banner", "cash_page",
           "cash_pane", "cell", "data_page", "discipline_rail", "duplicate_page",
           "error_page", "esc", "layout", "money", "nav_svg", "num",
           "overview_page", "pct", "pnl_excl_fee", "positions_table",
           "ratio_pct", "receipt_block", "rich", "risk_page", "section",
           "sign_cls",
           "signed_money", "status_cell", "trade_detail_page", "trades_page",
           "trades_pane"]
