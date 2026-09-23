"""`/lab/m2` 页面与 `m2 report` 的渲染（P48 §3）：**一个数都不算，只负责摆**。

页面与报告是两条渲染路径（HTML / Markdown），但入参永远是同一个
`m2_data.panel()` 的返回 —— 「同一份数据函数」是 P48 §3 的判据，
「两张皮各算一遍」是这条判据要防的病（`paper_data` / `paper_render` 同款分工）。

## 「无」而不是「未知」

P48 §3 原文要求缺数据显示「**无**」+ 原因。这里沿用既有页面的 `.s-unknown`
样式（视觉约定不变），只把字换成「无」—— 缺的东西各有各的原因，统统写在
`title` / 行内括号里；**一个 0 都不许出现**（0% 收益与「没有收益」不是一回事）。

## 样本不足时页面文本里不许出现那六个词

`跑赢 / 跑输 / 优于 / 劣于 / 领先 / 胜过` —— 门禁没过时逐词断言
（ADR-023 的措辞纪律，渲染路径两条都查）。所以本模块的**静态文案**里也
一个都不出现（哪怕是用来说「不排名」的否定句：关键词扫描读不出否定）。
"""

from __future__ import annotations

from collections.abc import Mapping

from stocklab.labweb.render import (esc, glance, layout, more, num, ratio_pct,
                                     rich, section)
from stocklab.labweb.m2_data import METRIC_LABELS

#: 缺值的标记（P48 §3 原文：写「无」，不写 0）。样式沿用既有 `.s-unknown`。
NONE_MARK = '<span class="s-unknown">无</span>'

_ROLE_ORDER = {"ai": 0, "mirror": 1, "benchmark": 2}


def _metric_cell(row: Mapping, key: str) -> str:
    """一格指标。缺值 → 「无」+ `title` 里的原因（裸 `None` / `nan` 一律不许出现）。"""
    value = row.get(key)
    reason = (row.get("missing") or {}).get(key)
    if value is None:
        inner = NONE_MARK
    elif key == "max_drawdown":
        # ADR-023 的展示约定：`backtest/metrics` 的真源是负值，页面写正值幅度，
        # 与 `/lab/paper` 既有那一列同一个写法（不是第二套口径）。
        inner = ratio_pct(abs(float(value)))
    elif key == "profit_loss_ratio":
        inner = num(value, 4)
    else:
        inner = ratio_pct(float(value))
    title = f' title="{esc(reason)}"' if reason else ""
    return f'<td class="num"{title}>{inner}</td>'


def three_way_block(data: Mapping) -> str:
    """① 三方对标表：每方一行 × 五指标 + 样本量门禁 + 基准口径标注。"""
    if not data or not data.get("available"):
        return f'<p class="note">{rich((data or {}).get("reason") or "没有绩效读数")}</p>'
    gate = data["sample_gate"]
    keys = data["metric_keys"]
    head = ('<tr><th>方</th>'
            + "".join(f'<th>{esc(METRIC_LABELS[k])}</th>' for k in keys)
            + '<th>相对基准</th><th>样本</th></tr>')
    rows = []
    for side in sorted(data["sides"], key=lambda s: _ROLE_ORDER.get(s["role"], 9)):
        if not side["available"]:
            cells = "".join(f'<td class="num" title="{esc(side["reason"])}">'
                            f'{NONE_MARK}</td>' for _ in keys)
            rows.append(
                f'<tr class="mut"><td class="l"><b>{esc(side["label"])}</b>'
                f'<div class="note">{esc(side["account_id"])} —— {esc(side["reason"])}</div>'
                f'</td>{cells}<td class="num">{NONE_MARK}</td>'
                f'<td class="num">{NONE_MARK}</td></tr>')
            continue
        cells = "".join(_metric_cell(side, k) for k in keys)
        rows.append(
            f'<tr><td class="l"><b>{esc(side["label"])}</b>'
            f'<div class="note"><code>{esc(side["account_id"])}</code></div></td>'
            f'{cells}'
            f'<td class="num">{ratio_pct(side["excess_vs_index_300"])}</td>'
            f'<td class="num">{esc(str(side["n_sessions"]))}</td></tr>')
    table = (f'<div class="scroll-x"><table class="tbl">{head}'
             + "".join(rows) + '</table></div>')

    lines = [f"窗口 {data['window'][0]} ~ {data['asof']}，{data['n_sessions']} 个交易日"
             f"（门槛 {gate['threshold']}）｜{gate['label']}",
             data["scope"]]
    blocked = data.get("deferred_blocked") or []
    if blocked:
        lines.append("⚠️ " + "；".join(
            f"`{d['code']}`（{d['name']}）**已经落进 `bars_daily`**，但本模块尚未把它"
            f"接入 —— 本轮只对 `{data['sides'][-1]['account_id']}`" for d in blocked))
    for item in data.get("deferred") or []:
        if not item["present_in_db"]:
            lines.append(f"未接入 `{item['code']}`（{item['name']}）：{item['missing']}"
                         f"；前置条件：{item['prerequisite']}")
    lines.append(data["benchmark_caveat"])
    return table + glance(lines)


def readings_block(data: Mapping) -> str:
    """② 预测校验读数：每种 `plugin_id` + `script_version`（+ 账户）一行。"""
    if not data or not data.get("available"):
        return f'<p class="note">{rich((data or {}).get("reason") or "没有预测记录")}</p>'
    head = ('<tr><th>插桩 / 脚本版本</th><th>可评分</th><th>胜率</th>'
            '<th>偏差中位</th><th>偏差 p10 / p90</th><th>盈亏比</th>'
            '<th>最大回撤</th><th>门禁</th></tr>')
    rows = []
    for group in data["groups"]:
        gate = group["gate"]
        dev = group["deviation"]
        rows.append(
            f'<tr><td class="l"><b>{esc(group["plugin_id"])}</b>'
            f'<div class="note">版本 <code>{esc(group["script_version"])}</code>'
            f'　账号 <code>{esc(group["account_id"])}</code></div></td>'
            f'<td class="num">{group["n_scored"]}</td>'
            f'<td class="num">{ratio_pct(group["win_rate"])}</td>'
            f'<td class="num">{(ratio_pct(dev["p50"]) if dev else NONE_MARK)}</td>'
            f'<td class="num">{(ratio_pct(dev["p10"]) + " / " + ratio_pct(dev["p90"])) if dev else NONE_MARK}</td>'
            f'<td class="num">{num(group["profit_loss_ratio"], 4) if group["profit_loss_ratio"] is not None else NONE_MARK}</td>'
            f'<td class="num">{ratio_pct(group["max_drawdown"]) if group["max_drawdown"] is not None else NONE_MARK}</td>'
            f'<td class="num">{esc(gate["label"])}</td></tr>')
    table = (f'<div class="scroll-x"><table class="tbl">{head}'
             + "".join(rows) + '</table></div>')
    detail = []
    for group in data["groups"]:
        detail.append(
            f'<p class="note"><b>{esc(group["plugin_id"])}</b> · '
            f'<code>{esc(group["script_version"])}</code> · '
            f'<code>{esc(group["account_id"])}</code>：'
            f'命中 {group["n_hits"]}/{group["n_scored"]}；'
            f'区间内 {group["n_inside_range"]}；'
            f'失效 {group["n_invalidated"]}、失效不可判定 '
            f'{group["n_invalidate_undetermined"]}；'
            f'不可评分 {group["n_unscorable"]} '
            f'（{esc(str(group["unscorable_reasons"]) )}）；'
            f'未打分 {group["n_pending"]}'
            + "".join(f'<br>{esc(why)}' for why in group["missing"].values())
            + '</p>')
    lines = [f"窗口内预测 {data['n_forecasts']} 条，已打分 {data['n_scored']} 条，"
             f"未打分 {data['n_unscored']} 条（目标日未到 / 尚未跑 `m2 score`）",
             f"门槛 {data['threshold']} 个交易日 —— 不到就只给读数："
             f"「{data['insufficient_wording']}」",
             data["per_version_rule"], data["metric_note"]]
    return table + glance(lines) + more("".join(detail), label="查看详细：逐版本计数")


def cases_summary(data: Mapping) -> str:
    """③ 的首屏摘要（表格进折叠区 —— 首屏只放「有多少条、看的是一段还是全部」）。"""
    if not data or not data.get("cases"):
        why = (data or {}).get("empty_reason") or "没有可取数的案例"
        return f'<p class="note">{rich(why)}</p>'
    return glance([
        f"方向判错的样本 {data['n_miss_total']} 条，下面列最近 {data['n_cases']} 条"
        f"（**上限 {data['limit']} 条**，按目标日倒序，**没有筛选参数**）",
        f"分母：窗口内可评分的预测共 {data['n_scored']} 条"
        f"（不可评分的**不计入分母**）",
        data["attribution_note"],
    ])


def cases_block(data: Mapping) -> str:
    """③ 错判案例集的**表格**（方向错的样本 + 归因列恒空）。"""
    if not data or not data.get("cases"):
        return cases_summary(data)
    head = ('<tr><th>标的</th><th>决策日 → 目标日</th><th>预测 / 实际</th>'
            '<th>实际收益</th><th>插桩 / 版本</th><th>PIT 输入 sha256</th>'
            '<th>归因</th></tr>')
    rows = []
    for case in data["cases"]:
        rows.append(
            f'<tr><td class="l"><code>{esc(case["code"])}</code></td>'
            f'<td class="num">{esc(case["asof_date"])} → {esc(case["target_date"])}</td>'
            f'<td class="num">{esc(str(case["predicted_class"]))} / '
            f'{esc(str(case["actual_class"]))}</td>'
            f'<td class="num">{ratio_pct(case["actual_pct"])}</td>'
            f'<td class="num">{esc(case["plugin_id"])} · '
            f'<code>{esc(case["script_version"])}</code></td>'
            f'<td class="num"><code>{esc(str(case["input_sha256"])[:16])}…</code></td>'
            f'<td class="num" title="{esc(data["attribution_note"])}">{NONE_MARK}</td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}'
            + "".join(rows) + '</table></div>')


def m2_page(data: Mapping, *, base: str, built_at: str) -> str:
    """`/lab/m2`（整页，**只读**：没有任何表单，也没有写入口）。"""
    head = ['<p class="note">' + rich(
        "模块2 对标与校验：**AI 模拟（通路 A） vs 人工镜像 vs 市场基准**，"
        "外加 A3 / B1 插桩预测的事后校验。指标与门禁**全部复用模块2 §4 的既有实现**"
        "（`paper metrics` / `/lab/paper` 同一份代码）—— 本页一个数都不重算。"
        "样本不足时只给读数，不下结论。") + '</p>']
    body = [
        section("三方对标（P48 §1）", three_way_block(data["three_way"]),
                right="并列，不排名",
                note="三方是「AI 模拟账户 / 人工镜像账户 / 市场基准」，"
                     "与模块1 的「插桩版本相对 index_300 的超额」是两件事（D-30）——"
                     "此处不做模块1 的那份对比。"),
        section("预测校验（P48 §2）", readings_block(data["forecast"]),
                right="五件读数 + 样本量门禁"),
        section("错判案例集（P48 §2 第 5 行）", cases_summary(data["cases"]),
                right="归因恒空（D-31）",
                detail=more(cases_block(data["cases"]), label="查看详细：错判明细")),
    ]
    return layout(base=base, title="模块2", body="".join(head + body),
                  asof=data["asof"], built_at=built_at, current="/m2")


# ---------- 文本报告（`m2 report`，与页面同源） ----------


def _cell(value: float | None, *, kind: str) -> str:
    if value is None:
        return "无"
    if kind == "plain":
        return f"{float(value):.4f}"
    if kind == "drawdown":
        return f"{abs(float(value)) * 100:.2f}%"
    return f"{float(value) * 100:.2f}%"


def three_way_text(data: Mapping) -> str:
    if not data.get("available"):
        return f"# 三方对标 · {data['asof']}\n\n{data.get('reason', '无数据')}\n"
    gate = data["sample_gate"]
    keys = data["metric_keys"]
    out = [f"# 三方对标（模块2 §4 同源）· {data['asof']}", "",
           f"窗口 {data['window'][0]} ~ {data['asof']}，{data['n_sessions']} 个交易日"
           f"（门槛 {gate['threshold']}）｜{gate['label']}", "",
           "| 方 | " + " | ".join(METRIC_LABELS[k] for k in keys)
           + " | 相对基准 | 样本 |",
           "|" + "---|" * (len(keys) + 4)]
    for side in sorted(data["sides"], key=lambda s: _ROLE_ORDER.get(s["role"], 9)):
        cells = [_cell(side.get(k),
                       kind=("plain" if k == "profit_loss_ratio"
                             else "drawdown" if k == "max_drawdown" else "pct"))
                 for k in keys]
        out.append(f"| `{side['account_id']}` {side['label']} | "
                   + " | ".join(cells)
                   + f" | {_cell(side['excess_vs_index_300'], kind='pct')}"
                   + f" | {side['n_sessions'] if side['n_sessions'] is not None else '无'} |")
        if not side["available"]:
            out.append(f"| ↳ 原因：{side['reason']} |" + " |" * (len(keys) + 2))
    out += ["", f"> {data['scope']}"]
    for item in data.get("deferred") or []:
        if not item["present_in_db"]:
            out.append(f"> 未接入 `{item['code']}`（{item['name']}）：{item['missing']}")
    out += ["", f"> {data['benchmark_caveat']}",
            *[f"> {n}" for n in data.get("notes") or []]]
    return "\n".join(out) + "\n"


def forecast_text(data: Mapping) -> str:
    if not data.get("available"):
        return f"# 预测校验 · {data['asof']}\n\n{data.get('reason', '无数据')}\n"
    out = [f"# 预测校验（A3 / B1 · 五件读数）· {data['asof']}", "",
           f"窗口内预测 {data['n_forecasts']} 条，已打分 {data['n_scored']} 条，"
           f"未打分 {data['n_unscored']} 条", "",
           "| 插桩 | 脚本版本 | 账户 | 可评分 | 胜率 | 偏差 p50 | 偏差 p10/p90 | "
           "盈亏比 | 最大回撤 | 门禁 |",
           "|" + "---|" * 10]
    for g in data["groups"]:
        dev = g["deviation"]
        spread = ("无" if dev is None
                  else f"{_cell(dev['p10'], kind='pct')} / {_cell(dev['p90'], kind='pct')}")
        out.append(
            f"| {g['plugin_id']} | `{g['script_version']}` | `{g['account_id']}` | "
            f"{g['n_scored']} | {_cell(g['win_rate'], kind='pct')} | "
            f"{_cell(dev['p50'] if dev else None, kind='pct')} | {spread} | "
            f"{_cell(g['profit_loss_ratio'], kind='plain')} | "
            f"{_cell(g['max_drawdown'], kind='drawdown')} | {g['gate']['label']} |")
    out += ["", f"> 门槛 {data['threshold']} 个交易日 —— 不到就只给读数："
                f"「{data['insufficient_wording']}」",
            f"> {data['per_version_rule']}", f"> {data['metric_note']}"]
    return "\n".join(out) + "\n"


def cases_text(data: Mapping) -> str:
    if not data.get("cases"):
        return f"# 错判案例集 · {data['asof']}\n\n{data.get('empty_reason', '无案例')}\n"
    out = [f"# 错判案例集 · {data['asof']}", "",
           f"方向判错的样本 {data['n_miss_total']} 条，本表列最近 {data['n_cases']} 条"
           f"（上限 {data['limit']} 条，按目标日倒序，没有筛选参数）", "",
           "| 标的 | 决策日 | 目标日 | 预测 | 实际 | 实际收益 | 插桩 | 脚本版本 | "
           "PIT 输入 sha256 | 归因 |",
           "|" + "---|" * 11]
    for c in data["cases"]:
        out.append(f"| `{c['code']}` | {c['asof_date']} | {c['target_date']} | "
                   f"{c['predicted_class']} | {c['actual_class']} | "
                   f"{_cell(c['actual_pct'], kind='pct')} | {c['plugin_id']} | "
                   f"`{c['script_version']}` | `{str(c['input_sha256'])[:16]}…` | "
                   f"（空） |")
    out += ["", f"> 分母：窗口内可评分的预测共 {data['n_scored']} 条",
            f"> {data['attribution_note']}"]
    return "\n".join(out) + "\n"


def m2_text(data: Mapping) -> str:
    """`m2 report` 的全文（三段与页面**同一份** `m2_data.panel()`）。"""
    return "\n".join([three_way_text(data["three_way"]),
                      forecast_text(data["forecast"]),
                      cases_text(data["cases"])])
