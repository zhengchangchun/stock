"""模拟盘对照页渲染（P19 展示层）：「我 vs AI 纪律臂 vs 大盘」。

## 只摆数字，不做排名

`chain/accuracy.PAPER_NO_PICK` 是同一条纪律：几个交易日上的第 1 名是多重比较下的
必然噪声，不是 edge。所以本页

- **并行展示**，不排名、不写「最好/推荐/更值得跟」；
- 「相对大盘」与「相对我」各一列 —— 那是减法，不是新口径（脚注写明公式）；
- 引用的每个数都来自 `paper_data.track`（它自己不产生口径），本模块不加总、不重算。

## 认不出的账户名原样显示

`cand_render` 的 `STAGE_CN` 同一条规矩：字典里没有的 `account_id` 就显示机器名
+「口径未知」，**不猜、不套最像的那条**。将来加臂时页面不会静默画错标签。

## 三条线的视觉

我 = accent 深蓝实线（最粗）；什么都不做 = 灰虚线；AI 三档 = 绿 / 琥珀 / 青
（同一规则、不同参数，色相分开是为了在图上分得清，**不是**区分好坏）；
大盘 = 深灰点线。颜色不是唯一信号：图下每条线都有色块 + 文字 + 最新值。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from stocklab.labweb.render import (cell, esc, glance, layout, money, more,
                                    num, ratio_pct, rich, section, sign_cls)

#: 账户 → 人话。**只换标签**，不改任何数字。
_LABELS: dict[str, str] = {
    "arm-now": "我 · 实盘账本镜像",
    "arm-hold": "什么都不做 · 起跑日冻结快照",
}

#: 账户 → (颜色, 虚线 dash, 线宽)。我 = accent 实线最粗；大盘单列在 `_INDEX_STYLE`。
_STYLES: dict[str, tuple[str, str, float]] = {
    "arm-now": ("#123e6b", "", 2.6),
    "arm-hold": ("#8c98a4", "7 4", 1.6),
    "arm-discipline-05": ("#0f6b3b", "", 1.8),
    "arm-discipline-10": ("#8a5a00", "", 1.8),
    "arm-discipline-15": ("#2a6f8f", "", 1.8),
}

#: 大盘：深灰点线。与「什么都不做」的灰虚线靠**线型**分开（不只是靠颜色深浅）。
_INDEX_STYLE: tuple[str, str, float] = ("#5a6672", "2 3", 2.0)

#: 认不出的账户：中性灰 + 短虚线（一眼看出它不是几条已知线之一）。
_UNKNOWN_STYLE: tuple[str, str, float] = ("#5a6672", "4 3", 1.6)

#: 展示顺序：**我 → 什么都不做 → AI 三档**。这是给读者的阅读顺序
#: （先看自己的线），不是排名。字典里没有的账户排最后，按 id 升序。
_DISPLAY_ORDER = ("arm-now", "arm-hold", "arm-discipline-05",
                  "arm-discipline-10", "arm-discipline-15")

_TABLE_HEAD = ("<tr><th>线</th><th>净值</th><th>累计收益</th><th>相对大盘</th>"
               "<th>相对我</th><th>最大回撤</th><th>累计成本</th>"
               "<th>持仓</th><th>成交</th></tr>")

UNKNOWN = '<span class="s-unknown">未知</span>'


def arm_label(arm: Mapping) -> str:
    """账户 → 人话。AI 纪律臂按 `etf_target_pct` 拼；认不出的显示机器名。"""
    aid = str(arm.get("account_id"))
    if aid in _LABELS:
        return _LABELS[aid]
    if arm.get("arm") == "discipline" and arm.get("etf_target_pct") is not None:
        return f"AI 纪律臂 · ETF 目标 {float(arm['etf_target_pct']):.0f}%"
    return f"{aid}（口径未知）"


def arm_style(arm: Mapping) -> tuple[str, str, float]:
    aid = str(arm.get("account_id"))
    if aid in _STYLES:
        return _STYLES[aid]
    return _UNKNOWN_STYLE


def _ordered(arms: Sequence[Mapping]) -> list[Mapping]:
    known = [a for a in arms if str(a["account_id"]) in _DISPLAY_ORDER]
    rest = sorted((a for a in arms if str(a["account_id"]) not in _DISPLAY_ORDER),
                  key=lambda a: str(a["account_id"]))
    return sorted(known,
                  key=lambda a: _DISPLAY_ORDER.index(str(a["account_id"]))) + rest


def _now_arm(data: Mapping) -> Mapping | None:
    return next((a for a in data["arms"]
                 if str(a["account_id"]) == str(data.get("now_account_id"))), None)


def _swatch(color: str, dash: str, *, width: float = 2.4) -> str:
    d = f' stroke-dasharray="{esc(dash)}"' if dash else ""
    return (f'<svg class="swatch" viewBox="0 0 28 8" width="28" height="8"'
            f' aria-hidden="true"><line x1="0" y1="4" x2="28" y2="4"'
            f' stroke="{esc(color)}" stroke-width="{width:.1f}"{d}/></svg>')


# ---------- 图 ----------

def race_svg(dates: Sequence[str], races: Sequence[Mapping], *,
             width: int = 1080, height: int = 330) -> str:
    """累计收益（%）多线对照图。`None` 处**断线**，不插值、不用前值滚动。

    `races` 每项：`{"color", "dash", "width", "points"}`，`points` 与 `dates`
    逐位对齐。所有线共用同一 y 轴（不同 y 轴的多线图是最常见的骗人手法）。
    """
    n = len(dates)
    vals = [float(v) for r in races for v in r["points"] if v is not None]
    if n < 2 or not vals:
        return ('<p class="note">至少要有两个交易日的净值才画线；当前不足 —— '
                '不画线，也不用起点值铺一条平线充数。</p>')
    lo, hi = min([*vals, 0.0]), max([*vals, 0.0])   # 0% 本金线永远在范围内
    if hi - lo < 1e-9:
        hi, lo = hi + 0.005, lo - 0.005
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 84, 16, 16, 30
    pw, ph = width - left - right, height - top - bottom

    def x(i: int) -> float:
        return left + (i / (n - 1)) * pw

    def y(v: float) -> float:
        return top + (1.0 - (v - lo) / (hi - lo)) * ph

    def txt(s: str, px: float, py: float, *, anchor: str = "start") -> str:
        return (f'<text x="{px:.1f}" y="{py:.1f}" text-anchor="{anchor}"'
                f' font-size="11" fill="#5a6672">{esc(s)}</text>')

    parts: list[str] = []
    zy = y(0.0)
    parts.append(f'<line x1="{left}" y1="{zy:.1f}" x2="{width - right}"'
                 f' y2="{zy:.1f}" stroke="#8c98a4" stroke-dasharray="5 4"/>')
    if top + 14 < zy < top + ph - 8:      # 与上下刻度太近就不标，避免叠字
        parts.append(txt("0%", left - 8, zy + 4, anchor="end"))

    for r in races:
        segments: list[list[tuple[int, float]]] = []
        run: list[tuple[int, float]] = []
        for i, v in enumerate(r["points"]):
            if v is None:
                if run:
                    segments.append(run)
                    run = []
                continue
            if run and i != run[-1][0] + 1:
                segments.append(run)
                run = []
            run.append((i, float(v)))
        if run:
            segments.append(run)
        dash = (f' stroke-dasharray="{esc(str(r["dash"]))}"'
                if r.get("dash") else "")
        color = esc(str(r["color"]))
        for seg in segments:
            if len(seg) >= 2:
                d = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in seg)
                parts.append(f'<polyline fill="none" stroke="{color}"'
                             f' stroke-width="{float(r["width"]):.1f}"'
                             f' stroke-linejoin="round" points="{d}"{dash}/>')
            else:
                i, v = seg[0]
                parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.2"'
                             f' fill="{color}"/>')
        last = segments[-1]
        if len(last) >= 2:                # 末日一个落点：最新值在线上看得见
            i, v = last[-1]
            parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.4"'
                         f' fill="{color}"/>')

    parts.append(f'<line x1="{left}" y1="{top + ph}" x2="{width - right}"'
                 f' y2="{top + ph}" stroke="#12181f"/>')
    parts.append(txt(str(dates[0]), left, height - 9))
    parts.append(txt(str(dates[-1]), width - right, height - 9, anchor="end"))
    parts.append(txt(f"{hi * 100:+.2f}%", left - 8, top + 5, anchor="end"))
    parts.append(txt(f"{lo * 100:+.2f}%", left - 8, top + ph, anchor="end"))
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}"'
            f' role="img" aria-label="累计收益对照曲线：我、什么都不做、'
            f'AI 纪律臂三档与沪深300指数（虚线横轴为 0%）">'
            + "".join(parts) + "</svg>")


def _races(data: Mapping, *, with_index: bool = True) -> list[dict]:
    races = []
    for arm in _ordered(data["arms"]):
        color, dash, width = arm_style(arm)
        races.append({"color": color, "dash": dash, "width": width,
                      "points": arm["points"]})
    idx = data.get("index")
    if with_index and idx is not None:
        color, dash, width = _INDEX_STYLE
        races.append({"color": color, "dash": dash, "width": width,
                      "points": idx["points"]})
    return races


# ---------- 图例与对照表 ----------

def _latest(arm: Mapping) -> float | None:
    vals = [v for v in arm["points"] if v is not None]
    return vals[-1] if vals else None


def legend(data: Mapping) -> str:
    """图例：色块 + 名称 + 最新累计收益。图与表之间的桥。"""
    items = []
    for arm in _ordered(data["arms"]):
        color, dash, _ = arm_style(arm)
        v = _latest(arm)
        items.append(f'<span class="legend__i">{_swatch(color, dash)}'
                     f'<span>{esc(arm_label(arm))}</span>'
                     f'<b class="{sign_cls(v)}">{ratio_pct(v)}</b></span>')
    idx = data.get("index")
    if idx is not None:
        color, dash, _ = _INDEX_STYLE
        v = idx["points"][-1]
        items.append(f'<span class="legend__i">{_swatch(color, dash)}'
                     f'<span>{esc(idx["label"])}</span>'
                     f'<b class="{sign_cls(v)}">{ratio_pct(v)}</b></span>')
    return f'<p class="legend">{"".join(items)}</p>'


def _count(x: Any) -> str:
    return UNKNOWN if x is None else f"{int(x)}"


def _excess_vs_now(arm: Mapping, data: Mapping) -> str:
    if str(arm["account_id"]) == str(data.get("now_account_id")):
        return '<span class="mut">基准自身</span>'
    v = arm.get("excess_vs_now")
    return UNKNOWN if v is None else f'<span class="{sign_cls(v)}">{ratio_pct(v)}</span>'


def compare_table(data: Mapping) -> str:
    """对照表：一行一条线。缺净值的臂明说「当日无净值」并给出它最后那次。"""
    rows = []
    for arm in _ordered(data["arms"]):
        color, dash, _ = arm_style(arm)
        date_note = "" if arm["has_nav_on_display_date"] else (
            f'<div class="note s-warn">当日无净值；最后一行 '
            f'{esc(str(arm["latest_nav_date"] or "无"))}</div>')
        rows.append(
            f'<tr><td class="l">{_swatch(color, dash)}'
            f'<b>{esc(arm_label(arm))}</b>'
            f'<div class="note"><code>{esc(str(arm["account_id"]))}</code></div>'
            f'{date_note}</td>'
            f'<td class="num">{money(arm["nav"])}</td>'
            f'<td class="num {sign_cls(arm["cum_return"])}">'
            f'{ratio_pct(arm["cum_return"])}</td>'
            f'<td class="num">{ratio_pct(arm["excess_vs_index_300"])}</td>'
            f'<td class="num">{_excess_vs_now(arm, data)}</td>'
            f'<td class="num">{ratio_pct(arm["max_drawdown"])}</td>'
            f'<td class="num">{money(arm["cum_cost"])}</td>'
            f'<td class="num">{_count(arm["n_positions"])}</td>'
            f'<td class="num">{_count(arm["n_trades"])}</td></tr>')
    idx = data.get("index")
    if idx is not None:
        color, dash, _ = _INDEX_STYLE
        rows.append(
            f'<tr class="mut"><td class="l">{_swatch(color, dash)}'
            f'<b>{esc(idx["label"])}</b>'
            f'<div class="note"><code>{esc(str(idx["code"]))}</code>　'
            f'{esc(str(idx["price_asof"] or "无日期"))} 收盘</div></td>'
            f'<td class="num">{num(idx["level"], 2)} 点</td>'
            f'<td class="num {sign_cls(idx["return_since_start"])}">'
            f'{ratio_pct(idx["return_since_start"])}</td>'
            f'<td class="num"><span class="mut">它就是大盘</span></td>'
            f'<td class="num {sign_cls(idx["excess_vs_now"])}">'
            f'{ratio_pct(idx["excess_vs_now"])}</td>'
            f'<td class="num">{UNKNOWN}</td><td class="num">{UNKNOWN}</td>'
            f'<td class="num"><span class="mut">不适用</span></td>'
            f'<td class="num"><span class="mut">不适用</span></td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{_TABLE_HEAD}'
            + "".join(rows) + '</table></div>')


# ---------- 各段 ----------

def _headline_cells(data: Mapping) -> str:
    now = _now_arm(data)
    idx = data.get("index") or {}
    now_ret = now["cum_return"] if now else None
    cells = [
        cell("我 · 实盘镜像",
             f'<span class="{sign_cls(now_ret)}">{ratio_pct(now_ret)}</span>',
             f'净值 {money(now["nav"] if now else None)}　'
             f'净入金 {money(now["net_deposits"] if now else None)}'),
        cell("大盘 · 沪深300",
             f'<span class="{sign_cls(idx.get("return_since_start"))}">'
             f'{ratio_pct(idx.get("return_since_start"))}</span>',
             f'{num(idx.get("level"), 2)} 点（{esc(str(idx.get("price_asof") or "无"))}）',
             alt="指数不可交易、无成本 —— 对照偏乐观"),
        cell("我 − 大盘",
             f'<span class="{sign_cls(now["excess_vs_index_300"] if now else None)}">'
             f'{ratio_pct(now["excess_vs_index_300"] if now else None)}</span>',
             "正数 = 跑赢指数；单笔样本，推不出能力",
             small=True),
        cell("观察窗口", f'{data["n_sessions"]} 个交易日',
             f'{esc(data["start_date"])} ~ {esc(str(data["date"]))}',
             alt="<120 个交易日不算结论"),
        cell("AI 纪律臂",
             f'{len([a for a in data["arms"] if a["arm"] == "discipline"])} 档',
             "ETF 目标 5/10/15%，其余规则完全相同",
             alt="规则执行，不含方向预测"),
    ]
    return '<div class="canon">' + "".join(cells) + '</div>'


def _overlap_note(data: Mapping, *, base: str = "") -> str:
    if not data.get("mirror_equals_hold"):
        return ""
    n = data.get("real_trades_after_start") or 0
    extra = ("没有新成交" if not n else f"只有 {n} 笔新成交")
    return ('<p class="note">' + rich(
        f'**「我」与「什么都不做」目前完全重合** —— 实盘账本在起跑日 '
        f'{esc(data["start_date"])} 之后{extra}，`arm-now` 重放出来的持仓因此与'
        f'冻结快照逐点相同。这不是画错了：往账本里记一笔真成交'
        f'（<a href="{esc(base + "/trades")}">成交流水</a>页或 '
        f'`stocklab trade add`），两条线下一个交易日就分开。')
        + '</p>')


def _why(data: Mapping) -> list[str]:
    idx = data.get("index") or {}
    lines = [
        f'「我」= `arm-now`：把 `real_trades` + `cash_flows` 逐笔重放到 '
        f'{esc(str(data["date"]))}，就是实盘账本本身，不是另一个账户。',
        '「AI 纪律臂」= `arm-discipline-05/10/15`：三条**同一套写死条文**的账户'
        '（止损 + 单票 ≤40% + ETF 分散），只差「ETF 目标占比」这一个数 —— '
        '单变量对照，**不含任何模型方向预测**。',
        f'「相对我」= 该线累计收益 − 我（`{esc(str(data.get("now_account_id")))}`）'
        '的累计收益，一个减法，不是独立口径。',
    ]
    if idx.get("base_level_missing"):
        lines.append('<span class="s-warn">起跑日没有大盘收盘价 → 大盘那条线整条'
                     '不画</span>（不拿别日的点位顶替）。')
    elif idx.get("n_missing"):
        lines.append(f'大盘在 {idx["n_missing"]} 个日期没有收盘价 → 那几个点断线，'
                     f'不插值、不用前值滚动。')
    return lines


def _disclosure(data: Mapping) -> str:
    idx = data.get("index") or {}
    extra = [f"大盘基期 {esc(str(idx.get('base_date') or data['start_date']))}"
             f" 收盘 {num(idx.get('base_level'), 2)}"
             f"（`{esc(str(idx.get('code') or ''))}`，不复权）"]
    if idx.get("note"):
        extra.append(str(idx["note"]))
    extra.append("起跑日锚点 = 各臂 `paper_accounts.initial_nav` ÷ "
                 "`params_json.initial_capital`（两列都来自库，不是补数）")
    extra.append("净值序列读的是 `paper_nav_daily.cum_return` 列，页面不重算")
    items = "".join(f"<li>{rich(x)}</li>" for x in (data.get("disclosure") or ()))
    return ('<ul class="list">' + items
            + "".join(f"<li>{rich(x)}</li>" for x in extra) + "</ul>"
            + (f'<p class="note">{rich(data["disclaimer"])}</p>'
               if data.get("disclaimer") else "")
            + (f'<p class="note">{rich(data["sample_note"])}</p>'
               if data.get("sample_note") else ""))


def _trades_detail(data: Mapping) -> str:
    """「这段时间发生了什么」：实盘账本 + 各臂机械成交，逐笔列出。"""
    real = data.get("real_trades") or []
    real_rows = "".join(
        f'<tr><td>{esc(str(t["date"]))}</td><td class="l">实盘账本</td>'
        f'<td>{esc(str(t["side"]))}</td><td>{esc(str(t["code"]))}</td>'
        f'<td class="num">{int(t["qty"])}</td>'
        f'<td class="num">{num(t["price"], 4)}</td>'
        f'<td class="num">{money(t["fee"])}</td>'
        f'<td class="l">{esc(str(t.get("note") or ""))}</td></tr>' for t in real)
    paper = data.get("paper_trades") or []
    paper_rows = "".join(
        f'<tr><td>{esc(str(t["date"]))}</td>'
        f'<td class="l"><code>{esc(str(t["account_id"]))}</code></td>'
        f'<td>{esc(str(t["side"]))}</td><td>{esc(str(t["code"]))}</td>'
        f'<td class="num">{int(t["qty"])}</td>'
        f'<td class="num">{num(t["fill_price"], 4)}</td>'
        f'<td class="num">{money(t["fee_total"])}</td>'
        f'<td class="l">{rich(t.get("reason") or "")}</td></tr>' for t in paper)
    head = ("<tr><th>日期</th><th>账户</th><th>方向</th><th>标的</th><th>股数</th>"
            "<th>成交价</th><th>费用</th><th>触发理由</th></tr>")
    if not real and not paper:
        return ('<p class="note">起跑日至今**一笔成交都没有** —— '
                '各臂都还拿着起跑持仓，这本身就是结论。</p>')
    return ('<div class="scroll-x"><table class="tbl">' + head + real_rows
            + paper_rows + '</table></div>'
            + '<p class="note">「实盘账本」那几笔就是「我」这条线的全部内容；'
              '其余行是各臂的机械成交，理由列写的是它触发的条文。'
              '本页只读，不改库。</p>')


def _empty_body(data: Mapping) -> str:
    return section(
        "还没有模拟盘净值",
        glance([
            f'`paper_accounts` {"没有账户" if data["db_missing"] else "有账户"}，'
            f'但 `paper_nav_daily` 里没有任何 ≤ {esc(data["asof"])} 的净值行。',
            "建账：`stocklab paper init` —— 它按 `paper/config.py` 的声明口径"
            "校验实盘账本，不符直接报错。",
            "出净值：`stocklab paper step --date <交易日>`；日链 15:30 收盘任务里"
            "已有这一步。",
        ]),
        note="没有净值行就是没有 —— 本页不拿成本价、也不拿 0 冒充一条曲线。")


def paper_page(data: Mapping, *, base: str, built_at: str) -> str:
    """模拟盘对照页（整页）。"""
    head = ['<p class="note">' + rich(
        "研究用对照：**我（实盘账本镜像）** vs **AI 纪律臂** vs **大盘** vs "
        "**什么都不做**。金额很小、样本很短 —— 这一页是记录，不是结论。") + '</p>']

    if not data["available"]:
        return layout(base=base, title="模拟盘对照",
                      body="".join(head) + _empty_body(data),
                      asof=data["asof"], built_at=built_at, current="/paper")

    now = _now_arm(data)
    span = (f'横轴 {esc(str(data["dates"][0]))} ~ {esc(str(data["date"]))}，'
            f'共 {data["n_sessions"]} 个交易日（起点那个点是起跑日 '
            f'{esc(data["start_date"])} 的锚点）。虚线横轴为 0%（本金）。')
    fig = (f'<figure class="chart">{race_svg(data["dates"], _races(data))}'
           f'{legend(data)}'
           f'<figcaption>全范围：纵轴 = 累计收益（%，已扣各臂自己的成本），'
           f'包含大盘；{span}</figcaption></figure>')
    fig_zoom = (
        f'<figure class="chart">'
        f'{race_svg(data["dates"], _races(data, with_index=False))}'
        f'<figcaption>只看账户（去掉大盘，纵轴自动放大）：四条账户线挨得很近，'
        f'不放大就看不见差别。<b>两张图的纵轴范围不同，别跨图比斜率</b> —— '
        f'每张图的上下刻度都写了自己的范围。具体差距见下一节「相对我」列。'
        f'</figcaption></figure>')

    summary = glance([
        f'窗口 {esc(data["start_date"])} ~ {esc(str(data["date"]))}，'
        f'{data["n_sessions"]} 个交易日。',
        f'实盘账本共 {len(data.get("real_trades") or [])} 笔成交、'
        f'各臂机械成交 {len(data.get("paper_trades") or [])} 笔。',
        f'我 {ratio_pct(now["cum_return"] if now else None)}　'
        f'大盘 {ratio_pct((data.get("index") or {}).get("return_since_start"))}',
    ])

    limits = glance([
        'LIVE 样本仍为 0：本页所有数字都是模拟臂或实盘账本的静态快照，'
        '不是真实成交的验证结果。',
        '本页**不做排名**、不输出买卖建议、不使用任何模型方向预测'
        '（生产模型方向能力 ≈ 0）。',
        '数字只读：页面从 `paper_accounts` / `paper_nav_daily` / `real_trades` / '
        '`bars_daily` 读出来，不重算、不改库。',
    ])

    body = [
        _headline_cells(data),
        section("三条线一起看",
                glance(_why(data)) + _overlap_note(data, base=base) + fig + fig_zoom,
                right="并行对照，不排名"),
        section("逐条对照", compare_table(data),
                note="「相对大盘」「相对我」都是减法，不是新口径；指数不可交易，"
                     "所以它那两行没有成本与回撤 —— 与各臂比时口径偏乐观。"),
        section("这段时间发生了什么", summary,
                detail=more(_trades_detail(data), label="查看详细：逐笔成交")),
        section("口径与限制", limits, right="模拟盘 ≠ 实盘",
                detail=more(_disclosure(data),
                            label="查看详细：模拟盘口径（不许改）")),
    ]
    return layout(base=base, title="模拟盘对照", body="".join(head + body),
                  asof=data["asof"], built_at=built_at, current="/paper")
