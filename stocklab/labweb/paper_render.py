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
**AI 操盘手家族 = 紫**（P69 §T3：家族按**前缀** `arm-agent*` 纳入，不列举 id）——
家族内靠**线型**分档：LLM 臂实线、随机对照虚线、通路 A 点线；
`params.live=false`（已停飞）一律灰线。大盘 = 深灰点线。
颜色不是唯一信号：图下每条线都有色块 + 文字 + 最新值。

## 「智能体臂」与「AI 操盘手」两节

- 「智能体臂（P52）」答的是**台账那一侧**：当前 spec、试错计数、复现性
  （`paper.engine.agent_block`，与 `paper show` 同源）。
- 「AI 操盘手」（P69 §T4）答的是用户问的那三件事：**操盘记录 / 盈利净值 / 口径史**。
  三块的数都从 `paper_data.track` 已经算好的 `arms` / `performance` /
  `paper_agent_decisions` 拿（见 `paper_data.agent_ops` 的 docstring）——
  一列新口径都不造，同一个数在页面上只有一个来源。

## 「智能体臂」这一节

P52 起它回答的是「**AI 当操盘手**这段时间做了什么」：每交易日一条决策（方向 ＋ 仓位
＋ 池内选标的），台账里存着载荷、模型指纹、候选池与上下文指纹；P37 的 spec 复审
同表保留（`decision_kind='spec'`），但**不再给这条臂下单**（D-34）。
所以本节每个数都从 `paper.engine.agent_block` 拿（与 `paper show` 同源），
连 `delta_vs_random` 的 `null` 也是上游写好的 `null` —— 见下。

## `null` 与 `0` 在页面上必须长得不一样

`delta_vs_random` 在随机对照臂还没有决策时恒为 `null`（差分**不存在**，
不是 0）。页面上它显示为「无法判定」+ 原因，绝不显示 `+0.00%`。
同理，复现性还没被真正检验过时写「无法判定」而不是「可复现」。

## 「AI 自己编排的东西，用上了没有」这一节

对比图回答「现在谁多少钱」，这一节回答「那条 AI 线到底跑的是什么」：模型准确率
与插桩/候选池的产出去向都摆在同一页上，**全部用计数回答**。

这一节的首屏几行走 `glance_html`（行里已经是拼好的 HTML），因为 `rich()` 会把
`<a>`、`<span class="s-warn">` 转义成可见文本 —— 同一个坑在本页的
`_overlap_note` / `_why` 上都踩过，已一并修好。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from stocklab.labweb.paper_data import INDEX_LABEL, METRIC_KEYS, METRIC_LABELS
from stocklab.labweb.render import (cell, esc, glance, glance_html, layout, money,
                                    more, num, ratio_pct, rich, section,
                                    sign_cls, signed_money)
from stocklab.paper.config import (AGENT_ARM_PREFIX, ARM_AGENT, ARM_AGENT_RANDOM,
                                   ARM_KIND_AGENT, ARM_KIND_AGENT_RANDOM,
                                   EXECUTOR_AGENT_DECISION, EXECUTOR_CHANNEL_A,
                                   HALTED_LABEL, NOT_COMPARABLE,
                                   RULE_CITATIONS_AGENT)

#: 账户 → 人话。**只换标签**，不改任何数字。
#: ⚠️ 只钉**没有执行者声明**的两条静态线（`arm-now` / `arm-hold`）—— AI 家族一律走
#: `_agent_label`（按 `params.executor` 分档）。把 `arm-agent*` 钉在这里就是 P56 §8.4
#: 那个错标签的成因：`arm-agent-ds-v1/-v2` 的 `arm` 也是 `agent`，按 `arm` 一刀切
#: 会把**决策台账臂**叫成「智能体 spec 编排」。
_LABELS: dict[str, str] = {
    "arm-now": "我 · 实盘账本镜像",
    "arm-hold": "什么都不做 · 起跑日冻结快照",
}

#: AI 家族的**唯一颜色**（紫）。家族内的线型区分见 `arm_style`。
_AGENT_COLOR = "#6a3d9a"
#: 家族三档线型：LLM 实线 / 随机虚线 / 通路 A 点线。
_AGENT_STYLES: dict[str, tuple[str, str, float]] = {
    "llm": (_AGENT_COLOR, "", 2.0),
    "random": (_AGENT_COLOR, "6 3", 1.6),
    "channel_a": (_AGENT_COLOR, "2 3", 1.8),
}

#: 停飞（`params.live=false`）：**灰线**。与「什么都不做」的灰虚线、认不出的账户
#: 各用一组线型分开 —— 灰是一族，线型才是身份证。
#: ⚠️ 它不是 `_UNKNOWN_STYLE`：停飞是**已知**状态（我们认识这条臂），
#: 认不出是**未知**状态，两件事都不许被读成对方。
_HALTED_STYLE: tuple[str, str, float] = ("#8c98a4", "3 3", 1.4)

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

#: 展示顺序：**我 → 什么都不做 → AI 三档**。这是给读者的阅读顺序（先看自己的线），
#: 不是排名。AI 家族（`arm-agent*`）紧随其后，按「在飞 → 停飞、再按 id」排 ——
#: 家族成员**不许在这里列举**（P69 §T3）：新开一条版本账户不该要改渲染代码。
_DISPLAY_ORDER = ("arm-now", "arm-hold", "arm-discipline-05",
                  "arm-discipline-10", "arm-discipline-15")

#: AI 家族的两条内置成员（`arm-agent` 自己**不带**前缀那个连字符，要单列）。
_AGENT_BUILTINS = (ARM_AGENT, ARM_AGENT_RANDOM)


def in_agent_family(account_id: str) -> bool:
    """这个账户 id 属于 AI 操盘手家族吗（`arm-agent` / `arm-agent-*`）。

    按**前缀 + 两个内置名**判定，不按枚举：新版本账户 `arm-agent-<版本>` 自动入族。
    """
    aid = str(account_id)
    return aid in _AGENT_BUILTINS or aid.startswith(AGENT_ARM_PREFIX)

_TABLE_HEAD = ("<tr><th>线</th><th>净值</th><th>累计收益</th><th>相对大盘</th>"
               "<th>相对我</th><th>最大回撤</th><th>累计成本</th>"
               "<th>持仓</th><th>成交</th></tr>")

UNKNOWN = '<span class="s-unknown">未知</span>'


def _is_live(arm: Mapping) -> bool:
    """这条臂还在飞吗。**缺省 `True`**（与 `engine.live_of` 同一个默认值）。

    页面读的是 `paper_data.arm_descriptor` 的投影；投影没了（老库/手工拼的 dict）
    就按「在飞」处理 —— 那是 `params.live` 本身的缺省语义。
    """
    return bool(arm.get("live", True))


def _halted_suffix(arm: Mapping) -> str:
    """停飞标签后缀（`HALTED_LABEL` 的唯一来源，`paper agent show` 同源）。"""
    return "" if _is_live(arm) else f' · {HALTED_LABEL}'


def _executor_of(arm: Mapping) -> str | None:
    value = arm.get("executor")
    return None if value is None else str(value)


def _family_style(arm: Mapping) -> tuple[str, str, float]:
    """AI 家族内部的线型：LLM 实线 / 随机虚线 / 通路 A 点线；停飞一律灰。"""
    if not _is_live(arm):
        return _HALTED_STYLE
    if _executor_of(arm) == EXECUTOR_CHANNEL_A:
        return _AGENT_STYLES["channel_a"]
    if str(arm.get("arm") or "") == ARM_KIND_AGENT_RANDOM:
        return _AGENT_STYLES["random"]
    return _AGENT_STYLES["llm"]


def _family_label(arm: Mapping) -> str:
    """AI 家族 → 人话（P69 §T3 的三档）。

    - `executor=m2_channel_a` ⇒ 「通路A · 插桩脚本（`m2_a1`…）· v<策略版本>」；
    - `executor=agent_decision` ＋ 有预注册 ⇒ 「LLM 操盘臂 · `<model_id>` · <臂名后缀>」；
    - `arm=agent_random` ⇒ 「随机对照（同护栏同成本）」；
    - 执行者字段缺失（手工拼的 dict / 老库）⇒ 退回按 `arm` 给一句人话，**不写「口径未知」**。
    """
    aid = str(arm.get("account_id"))
    executor = _executor_of(arm)
    kind = str(arm.get("arm") or "")

    if executor == EXECUTOR_CHANNEL_A:
        hooks = "、".join(str(h) for h in (arm.get("plugin_hooks") or [])) or "未声明插桩"
        ver = str(arm.get("strategy_version") or "未声明版本")
        return f"通路A · 插桩脚本（{hooks}）· {ver}"
    if executor == EXECUTOR_AGENT_DECISION:
        if kind == ARM_KIND_AGENT_RANDOM:
            return "随机对照（同护栏同成本）"
        model = arm.get("model_id")
        if model:
            # 臂名后缀 = 账户名去掉家族前缀（`arm-agent-ds-v2` → `ds-v2`）。
            suffix = aid[len(AGENT_ARM_PREFIX):] if aid.startswith(AGENT_ARM_PREFIX) \
                else aid
            return f"LLM 操盘臂 · {model} · {suffix}"
        return f"{aid} · 无预注册（内置占位臂）"
    if kind == ARM_KIND_AGENT:
        return f"{aid} · AI 操盘手（`executor` 未声明）"
    if kind == ARM_KIND_AGENT_RANDOM:
        return "随机对照（同护栏同成本）"
    return f"{aid}（口径未知）"


def arm_label(arm: Mapping) -> str:
    """账户 → 人话（P69 §T3：按 `params.executor` + 台账分档，**不再按 `arm=='agent'` 一刀切**）。

    修的是 P56 §8.4 点名的错标签：`arm-agent-ds-v1/-v2` 的 `arm` 字段也是 `agent`，
    按 `arm` 一刀切会把**决策台账臂**叫成「智能体 spec 编排」（P37 的旧文案）——
    读表的人会误判口径。停飞臂一律加 `HALTED_LABEL`，与 `paper agent show` 同源。
    """
    aid = str(arm.get("account_id"))
    if aid in _LABELS:
        return _LABELS[aid]
    if in_agent_family(aid):
        return _family_label(arm) + _halted_suffix(arm)
    kind = str(arm.get("arm") or "")
    if kind == "discipline" and arm.get("etf_target_pct") is not None:
        return f"AI 纪律臂 · ETF 目标 {float(arm['etf_target_pct']):.0f}%"
    return f"{aid}（口径未知）"


def arm_style(arm: Mapping) -> tuple[str, str, float]:
    """账户 → (颜色, 虚线, 线宽)。家族按**前缀**纳入 ⇒ 新版本账户自动上紫线。"""
    aid = str(arm.get("account_id"))
    if aid in _STYLES:
        return _STYLES[aid]
    if in_agent_family(aid):
        return _family_style(arm)
    return _UNKNOWN_STYLE


def _ordered(arms: Sequence[Mapping]) -> list[Mapping]:
    """阅读顺序：固定五条 → AI 家族（在飞在前、同状态按 id）→ 其余按 id。

    家族**不在这里列举 id**（P69 §T3）：加一条 `arm-agent-xx-v9` 不该要改渲染代码。
    """
    fixed = {a_id: i for i, a_id in enumerate(_DISPLAY_ORDER)}
    known, family, rest = [], [], []
    for a in arms:
        aid = str(a["account_id"])
        if aid in fixed:
            known.append(a)
        elif in_agent_family(aid):
            family.append(a)
        else:
            rest.append(a)
    return (sorted(known, key=lambda a: fixed[str(a["account_id"])])
            + sorted(family, key=lambda a: (0 if _is_live(a) else 1,
                                            str(a["account_id"])))
            + sorted(rest, key=lambda a: str(a["account_id"])))


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
    # 注意：这里走 `glance_html` 的同一条规矩 —— 本字符串是**拼好的 HTML**，
    # 所以数据值一律过 `esc()`，不交给 `rich()`（它会连 `<a>` 一起转义成文本）。
    return ('<p class="note"><b>「我」与「什么都不做」目前完全重合</b> —— '
            f'实盘账本在起跑日 {esc(data["start_date"])} 之后{esc(extra)}，'
            '<code>arm-now</code> 重放出来的持仓因此与冻结快照逐点相同。'
            '这不是画错了：往账本里记一笔真成交（'
            f'<a href="{esc(base + "/trades")}">成交流水</a>页或 '
            '<code>stocklab trade add</code>），两条线下一个交易日就分开。</p>')


def _why(data: Mapping) -> list[str]:
    """「三条线各是什么」——返回**拼好的 HTML** 行（调用方走 `glance_html`）。"""
    idx = data.get("index") or {}
    lines = [
        f'「我」= <code>arm-now</code>：把 <code>real_trades</code> + '
        f'<code>cash_flows</code> 逐笔重放到 {esc(data["date"])}，就是实盘账本本身，'
        f'不是另一个账户。',
        '「AI 纪律臂」= <code>arm-discipline-05/10/15</code>：三条<b>同一套写死条文</b>的账户',
        '（止损 + 单票 ≤40% + ETF 分散），只差「ETF 目标占比」这一个数 —— '
        '单变量对照，<b>不含任何模型方向预测</b>。',
        '「AI 操盘手」= <code>arm-agent*</code> 家族（P69 起按 `params.executor` 分档，'
        '见本页「AI 操盘手」专节）：<b>每交易日一条决策</b>'
        '（方向 ＋ 仓位 ＋ 池内选标的），落在 <code>paper_agent_decisions</code>；'
        '<code>arm-agent-random</code> 是随机对照 —— <b>同护栏、同成本、同候选池</b>，'
        '只是标的与权重随机抽。两条都<b>不用模型做方向预测</b>：载荷在项目外产出，'
        '本页只报「按台账执行了几笔」。',
        f'「相对我」= 该线累计收益 − 我（<code>{esc(data.get("now_account_id"))}</code>）'
        '的累计收益，一个减法，不是独立口径。',
    ]
    if idx.get("base_level_missing"):
        lines.append('<span class="s-warn">起跑日没有大盘收盘价 → 大盘那条线整条'
                     '不画</span>（不拿别日的点位顶替）。')
    elif idx.get("n_missing"):
        lines.append(f'大盘在 {esc(idx["n_missing"])} 个日期没有收盘价 → '
                     f'那几个点断线，不插值、不用前值滚动。')
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


# ---------- 智能体臂（P37） ----------


def _spec_html(spec: Mapping) -> str:
    """spec 的五个数 → 一行可读文本（每个值都过 `esc()`）。"""
    stop = spec.get("stop_loss_pct")
    stop_txt = ("关掉（判定不做，但风险不会因此消失）"
                if stop == "off" else f'{float(stop):.6f}%')
    return (f'ETF 目标 <code>{esc(spec.get("etf_target_pct"))}</code>%、'
            f'止损 <code>{esc(stop_txt)}</code>、'
            f'单票上限 <code>{esc(spec.get("max_single_pct"))}</code>%、'
            f'现金下限 <code>{esc(spec.get("cash_floor_pct"))}</code>%、'
            f'复审节奏 <code>{esc(spec.get("rebalance_cadence"))}</code> 交易日')


def _agent_history_table(ev: Mapping) -> str:
    hist = ev.get("history") or []
    if not hist:
        return ('<p class="note">' + rich(
                    '台账里还没有任何一行 —— 现在是**默认 spec**'
                    '（= `arm-discipline-10` 口径），不是「没有条文」。') + '</p>')
    head = ("<tr><th>@asof</th><th>decision_id</th><th>来源</th><th>试错</th>"
            "<th>被拒</th><th>spec sha256</th><th>为什么改</th><th>spec</th></tr>")
    rows = "".join(
        f'<tr><td>{esc(d["asof"])}</td>'
        f'<td class="num">{int(d["decision_id"])}</td>'
        f'<td><code>{esc(d["agent_kind"])}</code> / <code>{esc(d["model_id"])}</code></td>'
        f'<td class="num">{int(d["n_trials"])}</td>'
        f'<td class="num">{int(d["n_rejected"])}</td>'
        f'<td><code>{esc(str(d["spec_sha256"])[:12])}</code></td>'
        f'<td class="l">{esc(str(d.get("rationale") or "（未写理由）"))}</td>'
        f'<td class="l"><code>{esc(str(d["spec_after"]))}</code></td></tr>' for d in hist)
    return (f'<div class="scroll-x"><table class="tbl">{head}{rows}</table></div>'
            + '<p class="note">' + rich(
                '只列最近几版；台账本身 append-only、不截断。'
                '`来源` 列写的是「谁写的这一版」：阶段 1–2 的 spec 由人手写或人工'
                '复核后落库，**不是模型自动改的**。') + '</p>')


def _agent_change_space(ev: Mapping) -> str:
    space = ev.get("change_space") or {}
    default = ev.get("default_spec") or {}
    items = "".join(
        f'<li><code>{esc(k)}</code>：{rich(v)}　默认 <code>{esc(default.get(k))}</code></li>'
        for k, v in space.items())
    return ('<ul class="list">' + items + '</ul>'
            + '<p class="note">' + rich(
                '这张白名单就是智能体**能改的全部**：越界、未知字段、类型不符一律'
                '拒绝并记入 `rejected`，**不夹紧**。成本口径 / PIT 判据 / 整手口径 /'
                ' ETF 白名单 / append-only 纪律都不在白名单里 ——'
                '所以「扩大自己的变更空间」在这张表上不可表达。') + '</p>')


def _agent_reproducibility(ev: Mapping) -> str:
    rep = ev.get("reproducibility") or {}
    if not rep:
        return ''
    n = int(rep.get("n_groups_tested") or 0)
    if rep.get("reproducible") is None:
        head = ('<span class="s-warn">复现性：无法判定</span> —— 还没有一组 '
                '<code>(context_sha256, model_id, prompt_sha256, seed)</code> 重复'
                '出现过，所以「同输入同输出」这条判据<b>还没被检验</b>，'
                '不是已经通过。')
    elif rep["reproducible"]:
        head = (f'复现性：{esc(n)} 组同指纹的复审给出了同一个 spec —— 通过。')
    else:
        head = (f'<span class="s-warn">复现性：不可复现</span> —— {esc(n)} 组重复'
                f'指纹里出现了不同结果，逐条列在下方。')
    viol = rep.get("violations") or []
    body = "".join(
        f'<li><code>{esc(v["key"]["context_sha256"])}</code> … '
        f'于 {esc("、".join(str(a) for a in v["asof"]))} 给出 '
        f'{esc(v["n_distinct_specs"])} 个不同的 spec</li>' for v in viol)
    return head + (f'<ul class="list">{body}</ul>' if body else '')


# ---------- 对照臂同轴（P52 / D-36） ----------

def comparison_block(data: Mapping) -> str:
    """5 条对照臂 ＋ 随机臂**同轴并列**（六条，不排名、不挑「推荐」）。

    「不可比」是本节的**一等公民**：基金等权臂没有持仓与成本、指数不可直接交易、
    缺数据时点断线。这些行在表里写 `不可比`，**不写 0、不留空**——
    留空会被读成「没问题」，写 0 会被读成「那天没涨没跌」。
    """
    cmp = data.get("comparison") or {}
    if not cmp:
        return ('<p class="note">对照块取不到（旧库）—— 本页不编数。</p>')
    gate = cmp.get("sample_gate") or {}
    head = ("<tr><th>臂</th><th>口径</th><th>累计收益</th><th>可交易</th>"
            "<th>成本</th><th>备注</th></tr>")
    rows = []
    for a in cmp.get("arms") or []:
        latest = a.get("latest")
        ret = (f'<b class="{sign_cls(latest)}">{ratio_pct(latest)}</b>'
               if latest is not None
               else f'<span class="s-warn">{esc(NOT_COMPARABLE)}</span>')
        note = str(a.get("note") or "—")
        dec = a.get("decision") or {}
        # 「今日无决策」与「有决策但不动手」**不同形**：这一段只在台账里没有当天
        # 那一条（或缺当天那一条）时出现，说的是「没决定」，不是「决定不动手」。
        dec_html = (f'<div class="note">{rich(str(dec.get("note")))}</div>'
                    if dec.get("note") else "")
        rows.append(
            f'<tr><td class="l">{rich(str(a.get("label")))}'
            f'<div class="note"><code>{esc(str(a.get("id")))}</code></div></td>'
            f'<td>{esc(str(a.get("kind")))}</td>'
            f'<td class="num">{ret}</td>'
            f'<td>{"是" if a.get("tradable") else "否"}</td>'
            f'<td>{"有" if a.get("has_cost") else "无"}</td>'
            f'<td class="l">{rich(note)}{dec_html}</td></tr>')
    d = cmp.get("delta_vs_random")
    if cmp.get("delta_vs_random_available") and d is not None:
        delta_html = (f'Δ(AI 操盘手 − 随机对照) = '
                      f'<b class="{sign_cls(d)}">{ratio_pct(d)}</b>'
                      f'（AI {esc(cmp.get("n_decisions_agent"))} 条决策 / 随机 '
                      f'{esc(cmp.get("n_decisions_random"))} 条）。'
                      f'这是**归因**用的差分，不是成绩单。')
    else:
        delta_html = ('<span class="s-warn">Δ(AI 操盘手 − 随机对照) '
                      '**不存在**</span>（不是 0）：'
                      + rich(str(cmp.get("delta_vs_random_note") or "")))
    lines = [
        f'样本 {esc(cmp.get("n_sessions"))} 个交易日，门槛 '
        f'{esc(gate.get("threshold"))} 日 → '
        + (f'<b>ok</b>' if gate.get("status") == "ok"
           else f'<span class="s-warn">{esc(gate.get("status"))}</span>'
                f'（{rich(str(gate.get("note") or ""))}）'),
        delta_html,
        rich(str(cmp.get("not_comparable_note") or "")),
        '基金等权臂的清单与判据见 <code>stocklab/fund/nav.py</code>；'
        '它是**持仓未知的基金组合**，<b>不是指数</b>。',
        rich(str(cmp.get("disclaimer_extra") or "")),
    ]
    return (glance_html(lines)
            + f'<div class="scroll-x"><table class="tbl">{head}{"".join(rows)}</table></div>')


def agent_arm_block(data: Mapping) -> str:
    """智能体臂一段：当前 spec / 台账 / 与随机臂的差分 / 复现性。

    数字全部来自 `paper_data.agent_track`（它调 `engine.agent_block`），
    本函数**不重算**任何口径、不做排名。
    """
    ev = data.get("agent") or {}
    if not ev:
        return ('<p class="note">这一段没取到数据（旧库 / 缺表）—— '
                '本页不编数。</p>')
    if not ev.get("available"):
        return (f'<p class="note">这一段没法回答：{rich(ev.get("reason") or "未知原因")}'
                f'</p>')

    spec = ev.get("spec") or {}
    lines: list[str] = [
        f'当前 spec：{_spec_html(spec)}；'
        f'它推出的止损线是 <code>{num(ev.get("stop_loss_line"), 2)}</code> 元。'
        f'spec sha256 <code>{esc(str(ev.get("spec_sha256"))[:12])}</code>。',
        f'台账：{esc(ev.get("n_reviews"))} 次复审、累计试错 '
        f'{esc(ev.get("n_trials_total"))} 版（每次上限 '
        f'{esc(ev.get("max_trials_per_review"))}）、被拒 {esc(ev.get("n_rejected"))} 条；'
        f'最近一次复审 {esc(ev.get("last_asof") or "（还没有）")}。',
    ]

    if ev.get("delta_vs_random_available") and ev.get("delta_vs_random") is not None:
        d = float(ev["delta_vs_random"])
        lines.append(
            f'与随机对照臂的累计收益差 '
            f'<b class="{sign_cls(d)}">{ratio_pct(d)}</b>'
            f'（<code>arm-agent</code> − <code>arm-agent-random</code>）。')
    else:
        lines.append('<span class="s-warn">与随机对照臂的差分<b>不存在</b></span>'
                     '（不是 0）：'
                     + rich(ev.get("delta_vs_random_note") or ""))

    lines.append(
        '这两条臂<b>都不用模型做方向预测</b>：AI 操盘手（<code>arm-agent</code>）'
        '每交易日一条决策（方向 ＋ 仓位 ＋ 池内选标的），载荷由<b>项目外</b>产出、'
        '经 <code>paper agent decide</code> 校验后落 <code>paper_agent_decisions</code>；'
        '<code>arm-agent-random</code> 是它的随机对照 —— <b>同护栏、同成本、同候选池</b>。'
        '本页只报「按台账执行了几笔」，不替它解释理由。')

    detail = (_agent_history_table(ev) + _agent_change_space(ev)
              + '<p class="note">复现性判据（同 context + 同 model + 同 prompt + 同 seed '
                '→ 同 spec）：</p>' + _agent_reproducibility(ev))
    return glance_html(lines) + more(detail, label="查看详细：spec 台账、变更空间与复现性")


# ---------- 「AI 自己编排的东西，用上了没有」 ----------

#: 写死条文 → 人话。**只换标签**，不改条文本身。
_RULE_LABELS: dict[str, str] = {"stop_loss": "止损", "single_max": "单票≤40%",
                                "etf_first_build": "分散建仓"}


def _rule_short(text: str) -> str:
    """条文全文 → 短标签。`rule_citation` 里冒号前那段就是它自己的名字。"""
    return esc(text.split("：", 1)[0]) or "（空条文）"


def _scripts_table(ev: Mapping) -> str:
    scripts = ev.get("scripts") or []
    if not scripts:
        return ('<p class="note">`plugin_scripts` 是空的 —— '
                '自己编排的脚本一版都没有。</p>')
    head = ("<tr><th>script_id</th><th>plugin</th><th>版本</th><th>状态</th>"
            "<th>入库</th><th>备注</th></tr>")
    rows = "".join(
        f'<tr><td class="num">{int(s["script_id"])}</td>'
        f'<td><code>{esc(str(s["plugin_id"]))}</code></td>'
        f'<td>{esc(str(s["version"]))}</td>'
        f'<td>{esc(str(s["state"]))}</td>'
        f'<td>{esc(str(s["created_at"])[:19])}</td>'
        f'<td class="l">{esc(str(s["note"]))}</td></tr>' for s in scripts)
    return f'<div class="scroll-x"><table class="tbl">{head}{rows}</table></div>'


def _backtests_table(ev: Mapping) -> str:
    bts = ev.get("backtests") or []
    if not bts:
        return '<p class="note">`plugin_backtests` 里没有回测记录 —— 没跑过就没有。</p>'
    head = ("<tr><th>#</th><th>候选脚本</th><th>基线</th><th>池</th><th>窗口</th>"
            "<th>verdict</th><th>过拟合标记</th></tr>")
    rows = "".join(
        f'<tr><td class="num">{int(b["backtest_id"])}</td>'
        f'<td class="num">{b["candidate_script_id"]}</td>'
        f'<td class="num">{b["baseline_script_id"]}</td>'
        f'<td>{esc(str(b["pool"]))}</td>'
        f'<td>{esc(str(b["window_start"]))} ~ {esc(str(b["window_end"]))}</td>'
        f'<td>{esc(str(b["verdict"]))}</td>'
        f'<td>{esc(str(b["overfit_flag"]))}</td></tr>' for b in bts)
    return f'<div class="scroll-x"><table class="tbl">{head}{rows}</table></div>'


def _consumption_detail(ev: Mapping) -> str:
    """消费明细。注意：本函数的输出进 `more()`，**不走 `rich()`**，

    所以每一段文字自己过 `rich()`（纯文本 + `**`/反引号），从库里来的值过 `esc()`。
    """
    cons = ev.get("consumption") or {}
    cited = [str(c) for c in (cons.get("cited_rules") or [])]
    unknown = [str(c) for c in (cons.get("unknown_rules") or [])]
    spec = [str(c) for c in (cons.get("spec_rules") or [])]
    items = "".join(
        f'<li><code>{_rule_short(c)}</code> —— '
        + (rich('在两张条文表**之外**（模型信号 / 插桩脚本）') if c in unknown
           else (rich('落在 `paper/config.RULE_CITATIONS_AGENT` 内（智能体 spec）')
                 if c in spec else rich('落在 `paper/config.RULE_CITATIONS` 内')))
        + '</li>' for c in cited)
    keys = esc(", ".join(cons.get("param_keys") or [])) or "（无）"
    refs = [str(x) for x in (cons.get("param_refs") or [])]
    return (
        '<p class="note">' + rich(
            '判据：把 `paper_trades` 每一行的 `rule_citation` 与两张条文表'
            '（`paper/config.RULE_CITATIONS` ＋ `RULE_CITATIONS_AGENT`）逐条对表。'
            '两张表**之外**为空 ⇒ 没有任何一笔成交由模型预测或插桩脚本触发'
            '（真接了模型/插桩会以新条文或新参数键出现，所以这一段将来会自己变成非空）。')
        + '</p>'
        + (f'<ul class="list">{items}</ul>' if items
           else '<p class="note">`paper_trades` 里还没有任何成交行。</p>')
        + f'<p class="note">账户参数键：<code>{keys}</code>　'
        + '参数里出现的模型/插桩标记：'
        + (f'<code>{esc(", ".join(refs))}</code>' if refs
           else rich('**无**（一个都没有）'))
        + '</p>')


def _ai_next_steps() -> str:
    """「要真的用上，得动什么」——同样进 `more()`，每行自己过 `rich()`。"""
    return ('<ol class="list">'
            '<li>' + rich(
                '**加一条「模型臂」**：照 `predictions` 的信号机械调仓，与纪律臂并列 '
                '—— 这是唯一能直接回答「AI 操盘 vs 人操盘」的做法，但它要动 '
                '`paper/config.py` 的口径定义与那条源码扫描测试，'
                '**属于口径变更，得先拍板**。') + '</li>'
            '<li>' + rich(
                '**把插桩接进选股**：候选池已经接了（见上表 routing）；'
                '模拟盘当前只管纪律与分散，选股是另一条链路。') + '</li>'
            '<li>' + rich(
                '**保持现状**：模拟盘继续做「纪律 vs 人 vs 大盘」的对照，'
                '本段只把准确率与去向摆出来。') + '</li>'
            '</ol>')


def ai_block(ev: Mapping) -> str:
    """「AI 自己编排的东西，用上了没有」—— 全部用计数回答，不写形容词。

    首屏几行是**拼好的 HTML**（走 `glance_html`）：所以每个从库里取的值都过
    `esc()`，`num()` 的「未知」占位当 HTML 用（它本来就是 HTML），而
    `**强调**` / 反引号这类标记在这里不管用 —— 这里直接写 `<b>` 和 `<code>`。
    """
    if not ev:
        return ('<p class="note">这一段没取到数据（旧库 / 缺表）—— '
                '本页不编数。</p>')
    counts = ev.get("counts") or {}
    cons = ev.get("consumption") or {}
    acc = ev.get("accuracy") or {}
    win = acc.get("window") or {}
    replay = acc.get("replay")
    lines: list[str] = []

    if replay:
        ci = replay.get("direction_ci95") or [None, None]
        base = (replay.get("baselines_daily") or {}).get("always_down")
        tail = f'；「永远猜跌」基线 {num(base, 4)}' if base is not None else ""
        lines.append(
            f'<b>AI 的准确率</b>（模型 <code>{esc(acc.get("model_version"))}</code>，'
            f'回放口径，窗口 {esc(win.get("start"))} ~ {esc(win.get("end"))} 共 '
            f'{esc(win.get("n_sessions"))} 个交易日）：方向命中（按日聚类）'
            f'<b>{num(replay.get("direction_accuracy_daily"), 4)}</b>'
            f'（CI95 {num(ci[0], 4)}–{num(ci[1], 4)}）、Brier '
            f'<b>{num(replay.get("brier_daily"), 4)}</b>{tail}。')
    else:
        lines.append('<b>AI 的准确率</b>：窗口内没有任何验证行 —— '
                     '不是「准确率是 0」，是没有样本。')

    if acc.get("live") is None:
        lines.append('<b>LIVE 0 行</b>：窗口内一条实盘预测都没有 ⇒ 上面那些数字全是'
                     '历史回放，<b>不是实盘表现</b>。')
    gate = (replay or {}).get("sample_gate") or {}
    if gate and not gate.get("meets", True):
        lines.append(f'样本门槛 {esc(gate.get("min_days"))} 个交易日未达'
                     f'（{esc(gate.get("label"))}）—— 该窗口的排名与差值都还是'
                     f'噪声，只能当读数、不能当结论。')
    excluded = (acc.get("excluded") or {}).get("n_rows")
    if excluded:
        lines.append(rich((acc.get("excluded") or {}).get("note")))

    by_state = ev.get("by_state") or {}
    lines.append(
        f'<b>自己编排的产出</b>：插桩脚本 {esc(counts.get("plugin_scripts", 0))} 版'
        + '（' + "、".join(f'{esc(k)} {esc(v)}' for k, v in sorted(by_state.items()))
        + f'）、插件回测 {esc(counts.get("plugin_backtests", 0))} 条、候选池快照 '
        f'{esc(counts.get("candidate_snapshots", 0))} 个 / 席位 '
        f'{esc(counts.get("candidate_members", 0))}；库里的模型预测 '
        f'{esc(counts.get("predictions", 0))} 条、验证 '
        f'{esc(counts.get("verifications", 0))} 条。')

    routes = [r for r in (ev.get("routing") or [])
              if r.get("active_script_id") is not None]
    if routes:
        lines.append('<b>候选池在用</b>：' + "；".join(
            f'{esc(r["label"])} = plugin <code>{esc(r["plugin_id"])}</code> 的 active '
            f'版本 script {esc(r["active_script_id"])}（v{esc(r["version"])}）'
            for r in routes)
            + '。打分内核每次通过 <code>lifecycle.active_script_id</code> 取版本，'
              '换一版不用改代码。')
    else:
        lines.append('<span class="s-warn">候选池也<b>没有可用的 active 版本</b>'
                     '—— 打分管线会直接报错，不兜底。</span>')

    unknown = [str(c) for c in (cons.get("unknown_rules") or [])]
    refs = [str(x) for x in (cons.get("param_refs") or [])]
    cited = [str(c) for c in (cons.get("cited_rules") or [])]
    n_spec = int(cons.get("n_trades_by_spec") or 0)
    n_decision = int(cons.get("n_trades_by_decision") or 0)
    if not unknown and not refs:
        lines.append(
            '<span class="s-warn">模型与插桩没用上</span>：'
            f'{esc(cons.get("n_accounts", 0))} 个账户的 '
            f'{esc(cons.get("n_trades", 0))} 笔成交，'
            f'触发理由全部落在 {len(cited)} 条已登记条文里（'
            + "、".join(_rule_short(c) for c in cited)
            + '）；账户参数键 <code>'
            + (esc(", ".join(cons.get("param_keys") or [])) or "（无）")
            + '</code> 里没有 plugin / script_id / model_version 之一 ⇒ '
              '<b>引用模型预测 0 条、引用插桩脚本 0 条</b>。')
    else:
        lines.append(
            f'<b>模拟盘已经接了已登记条文之外的东西</b>：{len(unknown)} 条表外触发理由'
            + '（' + "、".join(_rule_short(u) for u in unknown) + '）'
            + f'、参数标记 {esc(", ".join(refs))} —— 逐条见下。')

    if n_spec:
        lines.append(
            f'<b>智能体 spec 臂用上了</b>：{n_spec} 笔成交的触发理由落在 '
            '<code>RULE_CITATIONS_AGENT</code> 里（条文来自 '
            '<code>paper_agent_decisions</code> 台账的当前 spec）。这是'
            '<b>同一条纪律的参数化</b>，不是模型信号 —— 两条 AI 线都不含方向预测。')

    if n_decision:
        lines.append(
            f'<b>AI 操盘手用上了</b>：{n_decision} 笔成交的触发理由落在 '
            '<code>RULE_CITATIONS_AGENT_DECISION</code> 里（条文来自 '
            '<code>paper_agent_decisions</code> 当日那一条的 '
            '<code>target_weight_pct</code>）。它<b>不是模型方向预测</b>：'
            '载荷由项目外产出、写入口只校验，本页只报「按台账执行了几笔」。')

    lines.append('这个「没用上」是<b>被钉住的</b>，不是漏接：'
                 '<code>test_paper_never_imports_model_or_kelly</code> 用源码扫描'
                 '禁止 <code>stocklab/paper/</code> 碰模型与凯利 —— 因为上面那条'
                 '准确率。')

    return glance_html(lines) + more(
        _scripts_table(ev) + _backtests_table(ev) + _consumption_detail(ev)
        + '<p class="note">要真的用上，三条路：</p>' + _ai_next_steps(),
        label="查看详细：产出清单与消费明细")


# ---------- AI 操盘手专节（P69 / T4） ----------

#: 权重的方向 → 人话。**只换标签**，权重数字原样来自台账载荷。
_SIDE_LABELS = {"buy": "买", "sell": "卖", "hold": "不动"}


def _side_label(side: Any) -> str:
    return _SIDE_LABELS.get(str(side), str(side))


def _weights_line(weights: Sequence[Mapping]) -> str:
    """决策载荷里的标的 → 一行（方向 ＋ 代码 ＋ 目标权重%）。"""
    if not weights:
        return '<span class="mut">（没有标的）</span>'
    return "、".join(
        f'{_side_label(w.get("side"))} <code>{esc(str(w.get("code")))}</code> '
        f'{num(w.get("target_weight_pct"), 2)}%' for w in weights)


def _ops_arms_table(ops: Mapping) -> str:
    """「盈利 / 净值」表：每臂一行（**数字全部来自 `track` 已算好的 `arms`**）。"""
    rows = ops.get("arms") or []
    if not rows:
        return '<p class="note">没有 `params.executor` 非空的账户 —— 现在没有在跑的 AI 臂。</p>'
    head = ("<tr><th>臂</th><th>净值</th><th>净收益(元)</th><th>累计收益</th>"
            "<th>相对大盘</th>"
            "<th>相对「不动」</th><th>最大回撤</th><th>持仓</th><th>累计成本</th>"
            "<th>Δ vs 随机</th></tr>")
    body = []
    for a in rows:
        color, dash, _ = arm_style(a)
        note = f'<div class="note"><code>{esc(str(a["account_id"]))}</code>'
        if a.get("model_id"):
            note += f'　模型 <code>{esc(str(a["model_id"]))}</code>'
        if a.get("latest_nav_date") and a["latest_nav_date"] != ops.get("asof"):
            note += (f'　<span class="s-warn">当日无净值；最后一行 '
                     f'{esc(str(a["latest_nav_date"]))}</span>')
        note += "</div>"
        d = a.get("delta_vs_random")
        if d is None:
            delta_html = (f'<span class="mut">{rich(str(a.get("delta_vs_random_note") or ""))}</span>'
                          if str(a["account_id"]) == ARM_AGENT_RANDOM
                          else f'<span class="s-warn">{UNKNOWN}</span>')
        else:
            delta_html = f'<span class="{sign_cls(d)}">{ratio_pct(d)}</span>'
        body.append(
            f'<tr><td class="l">{_swatch(color, dash)}'
            f'<b>{esc(arm_label(a))}</b>{note}</td>'
            f'<td class="num">{money(a.get("nav"))}</td>'
            f'<td class="num {sign_cls(a.get("profit_cny"))}">'
            f'{signed_money(a.get("profit_cny"))}</td>'
            f'<td class="num {sign_cls(a.get("cum_return"))}">'
            f'{ratio_pct(a.get("cum_return"))}</td>'
            f'<td class="num">{ratio_pct(a.get("excess_vs_index_300"))}</td>'
            f'<td class="num">{ratio_pct(a.get("excess_vs_hold"))}</td>'
            f'<td class="num">{ratio_pct(a.get("max_drawdown"))}</td>'
            f'<td class="num">{_count(a.get("n_positions"))}</td>'
            f'<td class="num">{money(a.get("cum_cost"))}</td>'
            f'<td class="num">{delta_html}</td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}{"".join(body)}</table></div>'
            + '<p class="note">数字**全部**来自 `paper.engine.build_report`（同一份实现），'
              '本页不重算；「相对大盘 / 相对不动 / Δ vs 随机」都是**一次减法**。'
              '`Δ` 为 `未知` = 那条臂或随机对照还没有累计收益 ⇒ **差分不存在，不是 0**。'
              '「**净收益(元)**」= 净值 − 累计净入金（`net_deposits`）：入金会让'
              '「累计收益」的分母变大，只有与它并列才能看出**是赚了还是只是加钱了**'
              '（用户 2026-09-26 第三条：预测准确度是手段，考核目标是扣完成本后的净收益）。'
              '</p>')


def _decision_state_cell(row: Mapping) -> str:
    """「今天有没有决策」一格。四种状态**开头就不同形**（P56 §2 的既有纪律）。"""
    if row.get("ledger_driven") is False:
        return '<span class="mut">不适用</span><div class="note">该臂的决策不走台账</div>'
    if row.get("decision_id") is not None:
        return '<span class="mut">有决策</span>'
    if row.get("missing_decision"):
        return '<span class="s-warn">**缺决策**</span><div class="note">交易日没决定</div>'
    return ('<span class="mut">无决策</span>'
            '<div class="note">非交易日或该臂还没起跑</div>')


def _ops_records_table(ops: Mapping) -> str:
    """「操盘记录」表：逐日一行（台账 ＋ 当日成交 ＋ 当日净值）。"""
    rows = ops.get("records") or []
    if not rows:
        return ('<p class="note">AI 臂的台账与净值都还是空的 —— '
                '**不是「没亏损」**，是还没有记录。</p>')
    label_of = {str(a["account_id"]): a for a in (ops.get("arms") or [])}
    head = ("<tr><th>日期</th><th>臂</th><th>产出者</th><th>决策摘要</th>"
            "<th>成交</th><th>净值</th><th>累计收益</th><th>决策</th></tr>")
    body = []
    for r in rows:
        aid = str(r["account_id"])
        arm = label_of.get(aid) or {"account_id": aid,
                                    "arm": None, "live": r.get("live", True)}
        producer = (f'<code>{esc(str(r["producer"]))}</code>'
                    if r.get("producer") else '<span class="mut">—</span>')
        body.append(
            f'<tr><td>{esc(str(r["date"]))}</td>'
            f'<td class="l"><b>{esc(arm_label(arm))}</b>'
            f'<div class="note"><code>{esc(aid)}</code></div></td>'
            f'<td>{producer}</td>'
            f'<td class="l">{_weights_line(r.get("weights") or [])}</td>'
            f'<td class="num">{_count(r.get("n_trades"))}</td>'
            f'<td class="num">{money(r.get("nav"))}</td>'
            f'<td class="num {sign_cls(r.get("cum_return"))}">'
            f'{ratio_pct(r.get("cum_return"))}</td>'
            f'<td>{_decision_state_cell(r)}</td></tr>')
    detail = []
    for r in rows:
        if not r.get("weights") and not r.get("rationale"):
            continue
        items = "".join(
            f'<li><code>{esc(str(w["code"]))}</code> · {_side_label(w.get("side"))} · '
            f'目标 {num(w.get("target_weight_pct"), 2)}%　'
            f'{rich(str(w.get("reason") or ""))}</li>' for w in (r.get("weights") or []))
        detail.append(
            f'<p class="note"><b>{esc(str(r["date"]))}　{esc(str(r["account_id"]))}</b>'
            + (f'　现金 {num(r.get("cash_pct"), 2)}%' if r.get("cash_pct") is not None else "")
            + '</p>'
            + (f'<p class="note">{rich(str(r["rationale"]))}</p>' if r.get("rationale") else "")
            + (f'<ul class="list">{items}</ul>' if items else ""))
    return (f'<div class="scroll-x"><table class="tbl">{head}{"".join(body)}</table></div>'
            + more("".join(detail), label="展开：逐条决策原文（方向 / 标的 / 目标权重 / 理由）")
            + '<p class="note">「产出者」是台账里的 `model_id` **原样字面量**'
              '（随机对照臂是 `random-control`，它不是模型）。'
              '成交笔数是**当日由这条决策执行出来的**笔数 ——'
              '「有决策但一笔没成交」与「没有决策」不是一件事。</p>')


def _caliber_llm_table(cal: Mapping) -> str:
    rows = cal.get("llm_versions") or []
    if not rows:
        return '<p class="note">没有任何**预注册**的 LLM 版本账户（`D-48`）。</p>'
    head = ("<tr><th>账户</th><th>model_id（原样字面量）</th><th>prompt_sha256</th>"
            "<th>建账时刻</th><th>状态</th><th>决策数</th><th>首 / 末决策日</th></tr>")
    body = "".join(
        f'<tr><td class="l"><code>{esc(r["account_id"])}</code></td>'
        f'<td><code>{esc(r["model_id"])}</code></td>'
        f'<td><code>{esc(r["prompt_sha256"][:12])}</code></td>'
        f'<td>{esc(r["created_at"][:19])}</td>'
        f'<td>{esc("在飞" if r["live"] else HALTED_LABEL)}</td>'
        f'<td class="num">{int(r["n_decisions"])}</td>'
        f'<td>{esc(str(r["first_asof"] or "—"))} ~ {esc(str(r["last_asof"] or "—"))}</td></tr>'
        for r in rows)
    return (f'<div class="scroll-x"><table class="tbl">{head}{body}</table></div>'
            + '<p class="note">换模型 / 换提示词 = **开新版本账户**（D-48），'
              '所以「哪天变过口径」在这一列上看得见；旧账户**保留不删**、台账不重写 ——'
              '这条纪律只挡一件事：换到好看为止。</p>')


def _caliber_channel_table(cal: Mapping) -> str:
    chains = cal.get("channel_a_versions") or []
    if not chains:
        return '<p class="note">没有通路 A 的账户（`params.executor = m2_channel_a`）。</p>'
    blocks = []
    for ch in chains:
        active = ch.get("active_script_id")
        head = ("<tr><th>脚本</th><th>版本</th><th>状态</th><th>入库时刻</th>"
                "<th>备注</th><th>审核链（submit / sandbox / approve）</th></tr>")
        body = []
        for v in ch.get("versions") or []:
            mark = " <b>（现行）</b>" if v["script_id"] == active else ""
            events = "、".join(
                f'{esc(e["action"])}@{esc(e["created_at"][:10])}'
                + (f'（{esc(e["actor"])}）' if e.get("actor") else "")
                for e in (v.get("events") or [])) or '<span class="mut">没有审核事件</span>'
            body.append(
                f'<tr><td class="num">{int(v["script_id"])}{mark}</td>'
                f'<td><code>{esc(v["version"])}</code></td>'
                f'<td>{esc(v["state"])}</td>'
                f'<td>{esc(v["created_at"][:19])}</td>'
                f'<td class="l">{esc(v["note"])}</td>'
                f'<td class="l">{events}</td></tr>')
        blocks.append(
            f'<p class="note"><code>{esc(ch["account_id"])}</code> 的插桩 '
            f'<code>{esc(ch["hook"])}</code>（策略版本 '
            f'<code>{esc(str(ch.get("strategy_version") or "未声明"))}</code>，'
            f'现行 script id = <code>{esc(str(active))}</code>）：</p>'
            f'<div class="scroll-x"><table class="tbl">{head}{"".join(body)}</table></div>')
    return "".join(blocks) + (
        '<p class="note">现行版本走 `plugin.lifecycle.active_script_id`'
        '（与打分内核**同一个函数**）—— 页面不自己判断哪版生效。</p>')


def _caliber_spec_table(cal: Mapping) -> str:
    rows = cal.get("spec_diffs") or []
    if not rows:
        return ('<p class="note">spec 台账里还没有任何一行 ——'
                '`arm-agent` 家族的 spec 由人手写或人工复核后落库，'
                '它不是模型自动改的（D-34 起这条臂也不再用 spec 下单）。</p>')
    head = ("<tr><th>@asof</th><th>账户</th><th>来源</th><th>改了哪几个键</th>"
            "<th>为什么改</th></tr>")
    body = []
    for r in rows:
        changes = "、".join(
            f'<code>{esc(c["key"])}</code> {esc(str(c["before"]))} → '
            f'<b>{esc(str(c["after"]))}</b>' for c in (r.get("changes") or [])) \
            or '<span class="mut">（逐键相同）</span>'
        body.append(
            f'<tr><td>{esc(r["asof"])}</td>'
            f'<td><code>{esc(r["account_id"])}</code></td>'
            f'<td><code>{esc(r["agent_kind"])}</code> / '
            f'<code>{esc(r["model_id"])}</code></td>'
            f'<td class="l">{changes}</td>'
            f'<td class="l">{esc(r["rationale"] or "（未写理由）")}</td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}{"".join(body)}</table></div>'
            + '<p class="note">只做 `spec_before → spec_after` 的**逐键对照**，'
              '不重算、不改值。</p>')


def ai_operator_block(data: Mapping) -> str:
    """「AI 操盘手」专节（P69 §T4）：**操盘记录 / 盈利净值 / 口径史** 三块。

    三块回答的是用户那句话的三半 ——
    「看不到模拟操盘记录」「看不到盈利」「看不到策略调整」。

    首屏只放读数（谁在跑、跑了几天、赚了多少、Δ vs 随机），细节进 `more()`：
    本页**不做排名、不给买卖建议、不推荐某一条臂**（`paper/config.py` 的禁令词
    用例把这一段一起扫）。
    """
    ops = data.get("agent_ops") or {}
    if not ops:
        return '<p class="note">这一段没取到数据 —— 本页不编数。</p>'
    arms = ops.get("arms") or []
    records = ops.get("records") or []
    live = [a for a in arms if a.get("live", True)]
    with_decision = sum(1 for r in records if r.get("decision_id") is not None)
    missing = sum(1 for r in records if r.get("missing_decision"))

    lines = [
        f'**谁在跑**：{len(live)} 条在飞（'
        + ("、".join(f'<b>{esc(arm_label(a))}</b>' for a in live) or "<span class=\"s-warn\">一条都没有</span>")
        + f'）'
        + (f'；另有 {len(arms) - len(live)} 条{HALTED_LABEL}' if len(arms) > len(live)
           else ""),
        f'**跑了多少**：台账里 {esc(with_decision)} 条决策（下表的「操盘记录」逐日一行）；'
        f'其中 {esc(missing)} 天是**缺决策**（交易日没决定，不是「决定不动手」）。',
        '**赚了多少**：见下表 —— 净值与累计收益**原样读库**，'
        'Δ(AI − 随机对照) 是一次减法；**样本远不足 120 交易日 ⇒ 只是读数，不是结论**。',
    ]
    gate = ((data.get("performance") or {}).get("sample_gate") or {})
    if gate and not gate.get("meets", True):
        lines.append(f'样本门禁：`{esc(gate.get("label"))}`'
                     f'（{esc(gate.get("n_sessions"))} / {esc(gate.get("threshold"))} 个交易日）'
                     f'—— 不据此选臂、不改口径。')

    return (glance_html(lines)
            + section("操盘记录（逐日：谁产出了什么、成交几笔）",
                      _ops_records_table(ops),
                      right="台账 append-only，不截断")
            + section("盈利 / 净值（每臂一行）", _ops_arms_table(ops),
                      right="净值读库里的列，不重算")
            + section("策略调整史（口径哪天变过）",
                      glance([HALTED_LABEL + "的臂仍列在此处：历史口径保留，"
                              "只是不再认领日终。"]),
                      detail=more(_caliber_llm_table(ops.get("caliber") or {})
                                  + _caliber_channel_table(ops.get("caliber") or {})
                                  + _caliber_spec_table(ops.get("caliber") or {}),
                                  label="查看详细：LLM 版本 / 通路 A 插桩版本 / spec 台账"),
                      right="换模型 = 开新版本账户"))


# ---------- 绩效对比（模块2 §4） ----------

#: 「已算出的比率」与「一个比值」两种数在页面上不能长得一样：
#: 前者是百分数、后者是倍数（`2.0` = 平均赢 2 倍于平均亏）。
_PLAIN_METRICS = ("profit_loss_ratio",)
#: 回撤的真源（`backtest/metrics`）是负值；本页既有那一列写的是正值。
#: 符号是展示约定、不是第二套口径 —— 但同一页上同一个事实只能有一种写法，
#: 所以这里取绝对值对齐既有列（`test_drawdown_is_shown_with_the_pages_positive_convention`）。
_NEGATIVE_METRICS = ("max_drawdown",)


def _metric_cell(data: Mapping, row: Mapping, key: str) -> str:
    """一格指标。缺值 → 「未知」+ `title` 里写**为什么**缺（不写裸 `None`）。"""
    v = row.get(key)
    reason = (row.get("missing") or {}).get(key)
    if v is None:
        inner = UNKNOWN
    elif key in _PLAIN_METRICS:
        inner = num(v, 4)
    elif key in _NEGATIVE_METRICS:
        inner = ratio_pct(-float(v))
    else:
        inner = ratio_pct(float(v))
    cls = {"total_return": "num", "annualized_return": "num",
           "max_drawdown": "num", "win_rate": "num",
           "profit_loss_ratio": "num"}[key]
    t = f' title="{esc(reason)}"' if reason else ""
    return f'<td class="{cls}"{t}>{inner}</td>'


def _performance_rows(data: Mapping) -> list[str]:
    """每 arm 一行 + 基准一行。`arm-hold` 明写「不动」，不许被读成 AI 表现。"""
    out = []
    excess_idx = data["excess_vs_index_300"]
    excess_hold = data["excess_vs_hold"]
    for row in data["rows"]:
        aid = str(row["account_id"])
        if row["kind"] == "benchmark":
            color, dash, _ = _INDEX_STYLE
            label, sub = INDEX_LABEL, f'<code>{esc(aid)}</code> 收盘'
            tr_cls = ' class="mut"'
        else:
            color, dash, _ = arm_style(row)
            label = esc(arm_label(row))
            sub = f'<code>{esc(aid)}</code>'
            if row["kind"] == "hold":
                sub += '　<span class="s-warn">不动臂（冻结快照，不是 AI 表现）</span>'
            tr_cls = ""
        e_idx, e_hold = excess_idx.get(aid), excess_hold.get(aid)
        out.append(
            f'<tr{tr_cls}><td class="l">{_swatch(color, dash)}<b>{label}</b>'
            f'<div class="note">{sub}</div></td>'
            + "".join(_metric_cell(data, row, k) for k in data["metric_keys"])
            + f'<td class="num {sign_cls(e_idx)}">{ratio_pct(e_idx)}</td>'
            f'<td class="num {sign_cls(e_hold)}">{ratio_pct(e_hold)}</td></tr>')
    return out


def performance_block(data: Mapping) -> str:
    """「绩效对比（模块2 §4）」一节：五个指标 × 每臂一行 + 基准一行 + 样本量门禁。

    数据**只能**来自 `paper_data.performance`（与 `paper metrics` 同一个函数）——
    这里一个数都不算，只负责摆。缺值是「未知」，`None` 与 0 在页面上长得不一样。
    """
    if not data or not data.get("available"):
        why = (data or {}).get("reason") or "没有净值数据"
        gate = (data or {}).get("sample_gate") or {}
        return (f'<p class="note">{rich(why)}</p>'
                f'<p class="note">{rich(gate.get("label", ""))}</p>')

    gate = data["sample_gate"]
    head = ('<tr><th>线</th>'
            + "".join(f'<th>{esc(METRIC_LABELS[k])}</th>'
                      for k in data["metric_keys"])
            + '<th>相对大盘</th><th>相对「不动」</th></tr>')
    table = (f'<div class="scroll-x"><table class="tbl">{head}'
             + "".join(_performance_rows(data)) + '</table></div>')
    return table + glance([f"窗口 {data['window'][0]} ~ {data['asof']}，"
                           f"{data['n_sessions']} 个交易日"
                           f"（门槛 {gate['threshold']}）—— {gate['label']}",
                           "期初：账户取**净入金**（`paper_nav_daily.net_deposits`，"
                           "ADR-023 修正段 D-37）、基准取起跑日 `sh000300` 收盘；"
                           "五个指标由同一条序列推出，与既有「累计收益」列逐位一致。"
                           "回撤按既有列写正值（真源 `backtest/metrics` 是负值）。"])


def performance_text(data: Mapping) -> str:
    """`paper metrics` 的文本表（Markdown）—— 与页面**同一份数据函数**。

    CLI 与页面各写一次渲染是可以的（一个是终端、一个是 HTML），
    但它们**不许各算一次指标**：入参永远是 `paper_data.performance` 的返回。
    """
    if not data or not data.get("available"):
        return f"# 绩效对比 · {data.get('asof', '')}\n\n{(data or {}).get('reason', '无数据')}\n"
    gate = data["sample_gate"]
    L = [f"# 绩效对比（模块2 §4）· {data['asof']}", ""]
    L.append(f"窗口 {data['window'][0]} ~ {data['window'][1]}，"
             f"{data['n_sessions']} 个交易日（门槛 {gate['threshold']}）"
             f"｜{gate['label']}")
    L.append("")
    cols = ["线"] + [METRIC_LABELS[k] for k in data["metric_keys"]] + \
        ["相对大盘", "相对「不动」"]
    L.append("| " + " | ".join(cols) + " |")
    L.append("|" + "---|" * len(cols))
    for row in data["rows"]:
        aid = str(row["account_id"])
        name = INDEX_LABEL if row["kind"] == "benchmark" else arm_label(row)
        if row["kind"] == "hold":
            name += "（不动臂，不是 AI 表现）"
        cells = [f"`{aid}` {name}"]
        for k in data["metric_keys"]:
            v = row.get(k)
            if v is None:
                why = (row.get("missing") or {}).get(k, "缺数据")
                cells.append(f"未知（{why}）")
            elif k in _PLAIN_METRICS:
                cells.append(f"{float(v):.4f}")
            elif k in _NEGATIVE_METRICS:
                cells.append(f"{-float(v) * 100:.2f}%")
            else:
                cells.append(f"{float(v) * 100:.2f}%")
        for d in (data["excess_vs_index_300"], data["excess_vs_hold"]):
            x = d.get(aid)
            cells.append("未知" if x is None else f"{x * 100:.2f}%")
        L.append("| " + " | ".join(cells) + " |")
    L = [*L, "", *[f"> {n}" for n in data["notes"]]]
    return "\n".join(L) + "\n"


def _empty_body(data: Mapping) -> str:
    return (section(
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
    + section("对照臂同轴（D-36：5 条 + 随机臂）", comparison_block(data),
              right="并列，不排名；缺数据写「不可比」")
    + section("智能体臂（P52）：AI 操盘手", agent_arm_block(data),
              right="台账里的决策，不是模型信号")
    + ai_operator_block(data)
    + section("绩效对比（模块2 §4）", performance_block(data.get("performance") or {}),
              right="五个指标 + 样本量门禁")
    + section("AI 自己编排的东西，用上了没有", ai_block(data.get("ai") or {}),
              right="用计数回答"))


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
                glance_html(_why(data)) + _overlap_note(data, base=base)
                + fig + fig_zoom,
                right="并行对照，不排名"),
        section("逐条对照", compare_table(data),
                note="「相对大盘」「相对我」都是减法，不是新口径；指数不可交易，"
                     "所以它那两行没有成本与回撤 —— 与各臂比时口径偏乐观。"),
        section("对照臂同轴（D-36：5 条 + 随机臂）", comparison_block(data),
                right="并列，不排名；缺数据写「不可比」"),
        section("智能体臂（P52）：AI 操盘手", agent_arm_block(data),
                right="台账里的决策，不是模型信号"),
        ai_operator_block(data),
        section("绩效对比（模块2 §4）",
                performance_block(data.get("performance") or {}),
                right="五个指标 + 样本量门禁"),
        section("AI 自己编排的东西，用上了没有", ai_block(data.get("ai") or {}),
                right="用计数回答"),
        section("这段时间发生了什么", summary,
                detail=more(_trades_detail(data), label="查看详细：逐笔成交")),
        section("口径与限制", limits, right="模拟盘 ≠ 实盘",
                detail=more(_disclosure(data),
                            label="查看详细：模拟盘口径（不许改）")),
    ]
    return layout(base=base, title="模拟盘对照", body="".join(head + body),
                  asof=data["asof"], built_at=built_at, current="/paper")
