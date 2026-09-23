"""模块2 可视化报表（P50 §3）：净值 / 回撤 / 命中率三条曲线 —— **只复用，不另画**。

## 一处绘图、一处取数

- **绘图真源** = `paper_render.race_svg`（`/lab/paper` 那张图用的就是它）。
  本模块只组装 `races`（`{"color","dash","width","points"}`）并交给它渲染 ——
  自己拼折线元素的话，两台页面迟早会在坐标轴、断线规则、0% 线上分歧。
- **取数真源** = `paper_data.track`（净值与指数，P41/P47 既有）、
  `paper/engine.py::drawdown`（回撤，净值口径的唯一定义）、
  `m2_forecast_scores` 的**已落库行**（命中率，P48 打分判据的产物）。

## 缺数据写「无」+ 理由，**不画空曲线、不填 0**

`svg=None` + `reason` 才是「没有」的表示。铺一条平线、或者把没有数据画成 0%，
会把「不知道」显示成「没涨没跌」—— 本项目反复踩过这个坑。

## 命中率曲线是「逐日累计」，不是「当日胜率」

逐日胜率在样本少的日子里只有 0% 或 100% 两个取值，画出来像一条电锯。本模块按
`target_date` 累进：第 i 个点的值 = 到那一天为止的 `命中数 / 可评分数`
（分子分母与 P48 的 `win_rate` **同一口径**，只是不折叠成一个数）。每个点带
`n`，caption 里给出区间与样本量 —— 样本 <120 交易日时它仍然只是读数。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from stocklab.config import m2_signals as S
from stocklab.m2 import store as m2_store
from stocklab.labweb import paper_data, paper_render
from stocklab.paper.engine import drawdown

#: 三条曲线的键（页面、报告、测试都按它取）。
KEYS: tuple[str, ...] = ("nav", "drawdown", "win_rate")

TITLES: dict[str, str] = {
    "nav": "净值曲线（累计收益 %，与 `/lab/paper` 同一张图、同一份取数）",
    "drawdown": "回撤曲线（自峰值，正数幅度；算法 = `paper/engine.py::drawdown`）",
    "win_rate": "命中率曲线（A3/B1 预测的逐日累计方向命中率，P48 口径）",
}

#: 指数在回撤图上的说明：指数不可交易、无成本 ⇒ 「它的回撤」不是任何账户的回撤。
INDEX_CAVEAT = "基准指数不可直接交易、无成本 ⇒ 它的回撤只作参照"

SAMPLE_NOTE = ("样本 <120 交易日时这些曲线只是**读数**：不许据此下结论"
               "（CLAUDE.md 度量纪律第 3 条）")


def _block(key: str, *, available: bool, reason: str | None, svg: str | None,
           caption: list[str], legend: list[dict] | None = None) -> dict:
    return {"key": key, "title": TITLES[key], "available": available,
            "reason": reason, "svg": svg, "caption": caption,
            "legend": legend or []}


# ---------- ① 净值曲线 ----------


def nav_block(track: Mapping) -> dict:
    """累计收益多线图 —— 直接喂 `paper_render` 的既有 races。"""
    if not track.get("available"):
        return _block("nav", available=False, svg=None, caption=[],
                      reason=(track.get("reason") or
                              "库里没有可画的净值行（`paper_nav_daily` 为空）—— "
                              "不画空曲线，也不用起点值铺一条平线"))
    races = [r for r in paper_render._races(track, with_index=True)
             if any(v is not None for v in r["points"])]
    svg = paper_render.race_svg(track["dates"], races)
    n_sessions = int(track.get("n_sessions") or 0)
    return _block(
        "nav", available=True, reason=None, svg=svg,
        legend=[{"color": r["color"], "label": ""} for r in races],
        caption=[f"窗口 {track.get('start_date')} ~ {track.get('date')}，"
                 f"{n_sessions} 个交易日（横轴 {len(track.get('dates') or [])} 个点："
                 "起跑日锚点 + 净值日）",
                 "线与 `/lab/paper` 完全同源（同一 `paper_data.track` + 同一 "
                 "`race_svg`）；本页**不重算**任何净值",
                 SAMPLE_NOTE])


# ---------- ② 回撤曲线 ----------


def _nav_rows(conn: sqlite3.Connection, account_id: str, asof: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT date, nav FROM paper_nav_daily WHERE account_id = ? AND date <= ?"
        " ORDER BY date", (str(account_id), str(asof)))]


def drawdown_block(conn: sqlite3.Connection, track: Mapping) -> dict:
    """每个账户一条「自峰值回撤」曲线（**逐日调既有 `drawdown`**，不另写算法）。"""
    if not track.get("available"):
        return _block("drawdown", available=False, svg=None, caption=[],
                      reason=(track.get("reason") or
                              "没有净值行 ⇒ 算不出回撤（回撤是净值的函数）—— "
                              "不画空曲线、也不写 0（0 回撤是「没跌过」）"))
    dates = list(track["dates"])
    races, missing = [], []
    for arm in track["arms"]:
        rows = _nav_rows(conn, str(arm["account_id"]), str(track["asof"]))
        by_date = {str(r["date"]): float(r["nav"]) for r in rows}
        if not rows:
            missing.append(str(arm["account_id"]))
            continue
        history = [float(r["nav"]) for r in rows]
        series = {str(r["date"]): drawdown(
            history[:i], float(r["nav"])) for i, r in enumerate(rows)}
        color, dash, width = paper_render.arm_style(arm)
        races.append({"color": color, "dash": dash, "width": width,
                      "points": [series.get(d) for d in dates]})
    races = [r for r in races if any(v is not None for v in r["points"])]
    if not races:
        return _block("drawdown", available=False, svg=None, caption=[],
                      reason="这些账户一条净值行都没有 ⇒ 回撤无从谈起")
    svg = paper_render.race_svg(dates, races)
    caption = [f"每个账户一条线（{len(races)} 条）：逐日 `paper/engine.py::drawdown`"
               " 的结果，与「最大回撤」列是同一个算法",
               INDEX_CAVEAT + "（指数因此不在本图里）",
               SAMPLE_NOTE]
    if missing:
        caption.append(f"这些账户没有净值行、不在图上：{'、'.join(missing)}")
    return _block("drawdown", available=True, reason=None, svg=svg, caption=caption)


# ---------- ③ 命中率曲线 ----------


def _hit_series(rows: Sequence[Mapping]) -> dict:
    """逐日累计命中率（分子分母 = P48 的 `hit_direction` 与可评分数）。"""
    days = sorted({str(r["target_date"]) for r in rows})
    hits = scored = 0
    points, ns = [], []
    for day in days:
        for row in rows:
            if str(row["target_date"]) != day:
                continue
            if row["hit_direction"] is None:
                continue
            scored += 1
            hits += 1 if int(row["hit_direction"]) == 1 else 0
        points.append(hits / scored if scored else None)
        ns.append(scored)
    return {"days": days, "points": points, "n": ns}


def win_rate_block(conn: sqlite3.Connection, asof: str) -> dict:
    """A3/B1 预测的逐日累计命中率（每个 `plugin_id` + `script_version` 一条线）。"""
    scores = m2_store.list_scores(conn, asof=asof)
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in scores:
        if not row["scorable"]:
            continue
        key = (str(row["plugin_id"]), str(row["script_version"]),
               str(row["account_id"]))
        groups.setdefault(key, []).append(row)
    groups = {k: v for k, v in groups.items() if v}
    if not groups:
        return _block(
            "win_rate", available=False, svg=None, caption=[],
            reason=("`m2_forecast_scores` 里没有可评分的行 —— 先跑 "
                    "`stocklab m2 score --asof <交易日>`。命中率是打分的产物，"
                    "不打分就没有这条线 —— 这与「命中率为零」是两件事"))
    style = [("#2f6f9f", "", 2.4), ("#b45309", "6 4", 2.0), ("#7c3aed", "2 3", 2.0),
             ("#0f766e", "8 3", 1.8), ("#9f1239", "1 3", 1.8)]
    axis = sorted({str(r["target_date"]) for rows in groups.values() for r in rows})
    races, legends, caps = [], [], []
    for i, key in enumerate(sorted(groups)):
        series = _hit_series(groups[key])
        color, dash, width = style[i % len(style)]
        races.append({"color": color, "dash": dash, "width": width,
                      "points": [series["points"][series["days"].index(d)]
                                 if d in series["days"] else None for d in axis]})
        legends.append({"color": color, "label": f"{key[0]} · {key[1]} · {key[2]}"})
        caps.append(f"{key[0]} · `{key[1]}` · `{key[2]}`：{len(groups[key])} 条可评分"
                    f"预测，末日累计命中率 "
                    f"{(series['points'][-1] or 0.0):.2%}（n={series['n'][-1]}）")
    races = [r for r in races if any(v is not None for v in r["points"])]
    svg = paper_render.race_svg(axis, races)
    return _block(
        "win_rate", available=True, reason=None, svg=svg,
        legend=legends,
        caption=[f"逐日**累计**命中率（{len(groups)} 组，按 `plugin_id` + "
                 "`script_version` + 账户**分列**、不许相加）",
                 *caps,
                 "口径与 P48 的 `win_rate` 同一分子分母（`hit_direction` / 可评分数），"
                 "只是按目标日展开成曲线",
                 SAMPLE_NOTE])


# ---------- 三条合一（页面与离线报告的唯一入口） ----------


def chart_panel(conn: sqlite3.Connection, asof: str) -> dict:
    """三条曲线（页面 = 离线单文件报告 = 本函数，**同源**）。"""
    track = paper_data.track(conn, asof)
    blocks = [nav_block(track), drawdown_block(conn, track),
              win_rate_block(conn, asof)]
    return {
        "asof": asof,
        "keys": list(KEYS),
        "blocks": blocks,
        "available": any(b["available"] for b in blocks),
        "source_note": ("曲线取数只有一处真源：`paper_data.track` / "
                        "`paper/engine.py::drawdown` / `m2_forecast_scores`；"
                        "绘图只有一处真源：`paper_render.race_svg`"),
        "signal_note": (f"「{S.CANDIDATE_MARK}」与归因判据见配置视图一节："
                        "四分类由程序给候选、人工写结论（D-31）"),
        "notes": [SAMPLE_NOTE],
    }
