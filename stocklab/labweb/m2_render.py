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
from stocklab.m2 import daily as m2_daily

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
        # 已入库但未接入的基准：**显式待办**。指名的「本轮基准」取自载荷里真实的
        # 基准行（P55 起可能不止一个），不是写死的沪深300。
        current = "、".join(
            f"`{s['account_id']}`" for s in data["sides"]
            if s["role"] == "benchmark")
        lines.append("⚠️ " + "；".join(
            f"`{d['code']}`（{d['name']}）**已经落进 `bars_daily`**，但本模块尚未把它"
            f"接入 —— 本轮只对 {current}" for d in blocked))
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
    """③ 错判案例集的**表格**（方向错的样本 + 候选 + 人工结论）。"""
    if not data or not data.get("cases"):
        return cases_summary(data)
    head = ('<tr><th>标的</th><th>决策日 → 目标日</th><th>预测 / 实际</th>'
            '<th>实际收益</th><th>插桩 / 版本</th><th>PIT 输入 sha256</th>'
            '<th>候选（程序）</th><th>结论（人工）</th></tr>')
    rows = []
    for case in data["cases"]:
        cands = case.get("attribution_auto") or []
        cand_cells = ("".join(
            f'<div>{NONE_MARK} {esc(c["label_text"])} —— {esc(c["signal_text"])}'
            f'；阈值 {esc(str(c["threshold"]))}；实测 {esc(str(c["at_value"]))}'
            f'<div class="note">{esc(c["step"])}</div></div>' for c in cands)
            or NONE_MARK)
        manual = case.get("attribution_manual")
        manual_cell = (esc(data["labels"].get(manual, manual)) if manual else NONE_MARK)
        rows.append(
            f'<tr><td class="l"><code>{esc(case["code"])}</code></td>'
            f'<td class="num">{esc(case["asof_date"])} → {esc(case["target_date"])}</td>'
            f'<td class="num">{esc(str(case["predicted_class"]))} / '
            f'{esc(str(case["actual_class"]))}</td>'
            f'<td class="num">{ratio_pct(case["actual_pct"])}</td>'
            f'<td class="num">{esc(case["plugin_id"])} · '
            f'<code>{esc(case["script_version"])}</code></td>'
            f'<td class="num"><code>{esc(str(case["input_sha256"])[:16])}…</code></td>'
            f'<td class="l">{cand_cells}</td>'
            f'<td class="num" title="{esc(str(case.get("attribution_manual_text") or ""))}">'
            f'{manual_cell}</td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}'
            + "".join(rows) + '</table></div>')


def _pct_text(value: object, *, digits: int = 2) -> str:
    """比率 → **纯文本**百分数（`None` → 「无」）。

    与 `ratio_pct` 分开是因为 `rich()` 会先把整串转义再替换标记：把
    `ratio_pct` 生成的 `<span>` 塞进 `rich()` 就会被**原样显示成文本**
    （`render.glance_html` 的 docstring 里记着这个坑）。
    """
    if value is None:
        return "无"
    return f"{float(value) * 100:+.{digits}f}%"


def cycles_block(data: Mapping) -> str:
    """④ 自评估判定与熔断（P49 §1/§2/§3）：**只读**，一个写入口都没有。

    三样东西并列摆出来，谁也不替谁下结论：

    1. **判定**——`m2_judgements` 里已落库的**建议 + 依据**（`insufficient` 明写
       「证据不足」：门禁没过时不许出现结论性判定）；
    2. **熔断事件**——`validation_events` 里 `circuit_breaker` 的 `at_value` 与
       `threshold` **分列显示**（只留一个数，读者无法判断有没有越界）；
    3. **待复核清单**——错判案例回流模块1 的**只读汇总**，归因列恒「空」（D-31）。

    文案里不用「原因」二字：这一节的三样都是**读数与依据**，不是对判错的解释
    （解释只能由人工在 P50 填，`tests/test_p49_selfeval.py` 逐字钉住）。
    """
    if not data or not data.get("available"):
        return f'<p class="note">{rich((data or {}).get("reason") or "没有验证周期")}</p>'
    blocks = [_cycle_card(c, data) for c in data["cycles"]]
    lines = [data["no_write_note"], data["criteria_note"]]
    bounds = data.get("boundaries") or {}
    labels = data.get("boundary_labels") or {}
    if bounds:
        lines.append("主干边界（`config/limits.py` 单一真源，AI 不可改；越界即拒、"
                     "**不 clamp**）：" + "；".join(
                         f"{labels.get(k, k)} = "
                         + (f"{float(bounds[k]) * 100:.2f}%"
                            if k == "circuit_breaker_drawdown" else str(bounds[k]))
                         for k in sorted(bounds)))
    return "".join(blocks) + glance(lines)


def _cycle_card(cycle: Mapping, data: Mapping) -> str:
    state = {"open": "进行中", "ended": "已收尾",
             "fused": "**已熔断（本轮验证已终止）**"}[cycle["state"]]
    head = (f"<p class=\"note\"><b>周期 #{cycle['cycle_id']}</b> · 策略版本 "
            f"<code>{esc(str(cycle['script_id']))}</code> · 账户 "
            f"<code>{esc(cycle['account_id'])}</code> · 起跑 {esc(cycle['start_date'])} · "
            f"计划 {cycle['planned_rounds']} 轮 / 单轮 {cycle['planned_days']} 天 · "
            f"已落 {cycle['n_rounds']} 轮 · 状态 {rich(state)}</p>")
    crit = (f"<p class=\"note\">判据**原文**（落库后逐字节不许改）："
            f"<code>{esc(cycle['criteria_text'])}</code></p>")
    return ("<div class=\"card\">" + head + crit
            + _judgements_block(cycle, data) + _events_block(cycle)
            + _review_block(cycle["review"]) + "</div>")


def _judgements_block(cycle: Mapping, data: Mapping) -> str:
    labels = data.get("branch_labels") or {}
    if not cycle["judgements"]:
        return ('<p class="note">尚无判定 —— 判定由 CLI 触发（'
                '`stocklab m2 cycle judge --cycle '
                f'{cycle["cycle_id"]} --asof <交易日> --fix-kind none|logic|params`）；'
                '本页**不提供触发按钮**</p>')
    head = '<tr><th>判定日</th><th>建议</th><th>结论性</th><th>依据摘要</th></tr>'
    rows = []
    for j in cycle["judgements"]:
        ev = j["evidence"]
        gate = ev.get("gate") or {}
        cases = ev.get("cases") or {}
        summary = (f"门禁 {gate.get('label', '无')}"
                   f"（{gate.get('n_sessions')} 个交易日 / 门槛 {gate.get('threshold')}）；"
                   f"轮次 {ev.get('n_rounds')}/{ev.get('planned_rounds')}；"
                   f"相对基准超额 {_pct_text(ev.get('excess_vs_index_300'))}；"
                   f"方向判错样本 {cases.get('n_cases')} 条")
        rows.append(
            f'<tr><td class="num">{esc(j["asof_date"])}</td>'
            f'<td class="num"><b>{esc(labels.get(j["branch"], j["branch"]))}</b></td>'
            f'<td class="num">{"是" if ev.get("conclusion") else "<b>否</b>（证据不足）"}</td>'
            f'<td class="l">{rich(summary)}</td></tr>')
    table = (f'<div class="scroll-x"><table class="tbl">{head}'
             + "".join(rows) + '</table></div>')
    detail = []
    for j in cycle["judgements"]:
        ev = j["evidence"]
        detail.append(
            f'<p class="note"><b>{esc(labels.get(j["branch"], j["branch"]))}</b> · '
            f'{esc(j["asof_date"])}：{rich(ev.get("reason"))}'
            + "".join(f'<br>建议动作：{rich(a)}' for a in (ev.get("actions") or []))
            + "".join(f'<br>算到哪一步：{rich(s)}' for s in (ev.get("steps") or []))
            + '</p>')
    return (table + more("".join(detail), label="查看详细：判定依据与建议动作")
            + '<p class="note">**建议不是执行**：这一列不改策略版本状态、不改账户、'
              '不改参数；三个分支的落地动作一律要人 `approve`（D-1/D-24）</p>')


def _events_block(cycle: Mapping) -> str:
    if not cycle["events"]:
        return '<p class="note">没有事件（未熔断、未收尾）</p>'
    head = ('<tr><th>事件</th><th>实测值 at_value</th><th>判据阈值 threshold</th>'
            '<th>判据原文</th><th>留痕</th></tr>')
    kinds = {"circuit_breaker": "**熔断**（本轮策略失效、验证终止）",
             "validation_end": "验证收尾", "freeze": "冻结", "unfreeze": "解冻"}
    rows = []
    for e in cycle["events"]:
        at = (ratio_pct(e["at_value"]) if e["at_value"] is not None else NONE_MARK)
        thr = (ratio_pct(e["threshold"]) if e["threshold"] is not None else NONE_MARK)
        rows.append(
            f'<tr><td class="num">{rich(kinds.get(e["kind"], e["kind"]))}</td>'
            f'<td class="num">{at}</td><td class="num">{thr}</td>'
            f'<td class="l"><code>{esc(str(e["criteria_text"])[:60])}</code></td>'
            f'<td class="l">{esc(str(e["reason"]))}<div class="note">'
            f'{esc(str(e["created_at"]))}</div></td></tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}'
            + "".join(rows) + '</table></div>')


def _review_block(review: Mapping) -> str:
    if not review or not review.get("n_cases"):
        return ('<p class="note">待复核清单：空（本周期内没有方向判错的样本）—— '
                '**空清单不是「没问题」的结论**，它只是一种取不到依据的状态</p>')
    fp = review["fingerprints"]
    rows = []
    for g in review["codes"]:
        items = "".join(
            f'<div class="note">{esc(i["asof_date"])} → {esc(i["target_date"])}：'
            f'{esc(str(i["predicted_class"]))} / {esc(str(i["actual_class"]))} · '
            f'{esc(i["plugin_id"])} · <code>{esc(i["script_version"])}</code></div>'
            for i in g["items"])
        rows.append(
            f'<tr><td class="l"><code>{esc(g["code"])}</code></td>'
            f'<td class="num">{esc(g["window"][0])} ~ {esc(g["window"][1])}</td>'
            f'<td class="num">{g["n"]}</td>'
            f'<td class="l">{items}</td>'
            f'<td class="num">{NONE_MARK}</td></tr>')
    head = ('<tr><th>标的</th><th>区间</th><th>条数</th><th>明细</th>'
            '<th>归因（恒空）</th></tr>')
    table = (f'<div class="scroll-x"><table class="tbl">{head}'
             + "".join(rows) + '</table></div>')
    lines = [
        f"待复核清单：{review['n_codes']} 个标的 / {review['n_cases']} 条"
        f"（按标的聚，**只读汇总、不是结论**）",
        f"来源指纹：脚本版本 {'、'.join(fp['script_versions'])}；"
        f"PIT 输入 sha256 {'、'.join(s[:16] + '…' for s in fp['input_sha256'])}",
        f"生成时间 {review['generated_at']} —— {review['generated_at_note']}",
        review["attribution_note"], review["read_only_note"],
    ]
    return table + glance(lines)


def last_run_line(last: Mapping | None) -> str:
    """`/lab/m2` 顶部那一句：**上次通路运行**（P54 §1.6）。

    只读台账里最后一行（`m2/daily.py::last_run`）—— 页面不推算「今天跑没跑」，
    也**没有任何触发按钮**（触发只走 CLI 与收盘链）。
    没跑过写「从未」：写 0、或写一个今天的日期，都会让「没跑」看起来像「跑了」。

    日期与状态**不加标记**（不用 `<code>` / `<b>`）：这一句的读者要的是一句人话，
    而 `rich()` 的标签会把「`日期` / `状态`」切碎（任务书 §1.6 给的就是这个形状）。
    """
    if not last:
        return '<p class="note">' + rich(
            f"{m2_daily.LAST_RUN_LABEL}：从未（`m2_channel_runs` 一行都没有）"
            "—— 跑过 `ops close`（链上 `m2_daily` 一步）或手工 "
            "`stocklab m2 daily --asof <交易日>` 之后这里才有读数") + '</p>'
    return '<p class="note">' + rich(
        f"{m2_daily.LAST_RUN_LABEL}：{last['asof']} / {last['status']}"
        f"（通路 {last['channel']} · {last['account_id']}）"
        "—— 读的是 `m2_channel_runs` 的最后一行，本页不重算") + '</p>'


def m2_page(data: Mapping, *, base: str, built_at: str) -> str:
    """`/lab/m2`（整页，**只读**：没有任何表单，也没有写入口）。"""
    head = ['<p class="note">' + rich(
        "模块2 对标与校验：**AI 模拟（通路 A） vs 人工镜像 vs 市场基准**，"
        "外加 A3 / B1 插桩预测的事后校验。指标与门禁**全部复用模块2 §4 的既有实现**"
        "（`paper metrics` / `/lab/paper` 同一份代码）—— 本页一个数都不重算。"
        "样本不足时只给读数，不下结论。") + '</p>',
        last_run_line(data.get("last_run"))]
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
        section("自评估判定与熔断（P49）", cycles_block(data["cycles"]),
                right="只读 · 建议≠执行",
                note="判定的输入只有台账与既有读数函数（P41/P48 同源），"
                     "输出只有**建议 + 依据**；熔断只**追加事件**、不改写历史行。"
                     "两者都不在这一页触发 —— 触发只走 CLI。"),
        section("可视化报表（P50 §3）", charts_section(data["charts"]),
                right="复用既有图，不另画一套",
                note="净值 / 回撤 / 命中率三条曲线全部走 `paper_render.race_svg`，"
                     "取数走 `paper_data.track` 与既有回撤算法；页面与离线报告"
                     "（`dashboard build`）渲染的是**同一段** HTML。"),
        section("只读配置视图（P50 §2 / D-32）", config_block(data["config"]),
                right="零写入口",
                note="主干常量的值走 `m2/selfeval.py::BOUNDARIES`（只读值视图），"
                     "每一项的 `来源` 都指到代码里的定义处；网页端**不提供**"
                     "主干常量的写入口。"),
        section("事件旁路触发（P50 §4 / D-45）", bypass_block(data["bypass"]),
                right="只读 · 无触发按钮",
                note="触发源是机械信号清单（`stocklab/config/m2_signals.py`），"
                     "触发走 CLI；同 `(标的, 信号, asof)` 只触发一次"
                     "（唯一索引兜底）。"),
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


def cycles_text(data: Mapping) -> str:
    """④ 判定与熔断的文本版（`m2 report` 用；与页面**同一份**载荷）。"""
    if not data.get("available"):
        return f"# 自评估判定与熔断 · {data.get('asof')}\n\n{data.get('reason')}\n"
    out = [f"# 自评估判定与熔断（P49）· {data['asof']}", ""]
    labels = data.get("branch_labels") or {}
    for cycle in data["cycles"]:
        out += [f"## 周期 #{cycle['cycle_id']}（策略版本 {cycle['script_id']} / "
                f"账户 `{cycle['account_id']}` / 状态 {cycle['state']}）", "",
                f"- 起跑 {cycle['start_date']}，计划 {cycle['planned_rounds']} 轮 / "
                f"单轮 {cycle['planned_days']} 天，已落 {cycle['n_rounds']} 轮",
                f"- 判据原文：`{cycle['criteria_text']}`"]
        if not cycle["events"]:
            out.append("- 事件：无（未熔断、未收尾）")
        for e in cycle["events"]:
            out.append(f"- 事件 `{e['kind']}`：at_value={_cell(e['at_value'], kind='pct')} / "
                       f"threshold={_cell(e['threshold'], kind='pct')}"
                       f"（{e['reason']}）")
        if not cycle["judgements"]:
            out.append("- 判定：尚无（由 `m2 cycle judge` 触发）")
        for j in cycle["judgements"]:
            ev = j["evidence"]
            out.append(f"- 判定 {j['asof_date']}：**{labels.get(j['branch'], j['branch'])}**"
                       f"（结论性：{'是' if ev.get('conclusion') else '否'}）— {ev.get('reason')}")
            out += [f"  - 建议动作：{a}" for a in (ev.get("actions") or [])]
        review = cycle["review"]
        out += ["", f"### 待复核清单（只读汇总、不是结论）", "",
                f"{review['n_codes']} 个标的 / {review['n_cases']} 条；"
                f"生成时间 {review['generated_at']}；"
                f"指纹：脚本版本 {review['fingerprints']['script_versions']}",
                f"> {review['attribution_note']}"]
        for g in review["codes"]:
            out.append(f"- `{g['code']}` {g['window'][0]} ~ {g['window'][1]}："
                       f"{g['n']} 条")
        out.append("")
    out += [f"> {data['no_write_note']}", f"> {data['criteria_note']}"]
    bounds = data.get("boundaries") or {}
    blabels = data.get("boundary_labels") or {}
    if bounds:
        out.append("> 主干边界：" + "；".join(
            f"{blabels.get(k, k)} = {bounds[k]}" for k in sorted(bounds)))
    return "\n".join(out) + "\n"


# ---------- ⑤ 可视化报表 / ⑥ 只读配置视图 / ⑦ 旁路触发留痕（P50） ----------


def charts_section(charts: Mapping) -> str:
    """三条曲线（P50 §3）—— **同一段 HTML** 同时进页面与离线单文件报告。

    图本身是 `paper_render.race_svg` 渲染好的（本函数一个坐标都不算）；
    缺数据时写「无」+ 理由，`svg` 为 `None`（不画空曲线、不填 0）。
    """
    if not charts:
        return f'<p class="note">没有图表载荷 —— {NONE_MARK}</p>'
    out = []
    for block in charts.get("blocks") or []:
        head = f'<figcaption><b>{esc(block["title"])}</b>'
        if not block.get("available"):
            out.append(f'<figure class="chart">{head} {NONE_MARK}'
                       f'<div class="note">{esc(block.get("reason") or "无")}</div>'
                       '</figcaption></figure>')
            continue
        out.append(f'<figure class="chart">{block["svg"]}{head}'
                   + glance(list(block.get("caption") or []))
                   + '</figcaption></figure>')
    legend = []
    for block in charts.get("blocks") or []:
        for item in block.get("legend") or []:
            if item.get("label"):
                legend.append(f'{item["label"]}')
    if legend:
        out.append(glance(["分列（不许相加）：" + "；".join(legend)]))
    out.append(glance([charts.get("source_note", ""),
                       charts.get("signal_note", "")]))
    return "".join(out)


def config_block(config: Mapping) -> str:
    """只读配置视图（P50 §2 / D-32）：值原样显示，`来源` 指到代码定义处。"""
    if not config:
        return f'<p class="note">没有配置视图 —— {NONE_MARK}</p>'
    blocks = []
    for group in config.get("groups") or []:
        head = ('<tr><th>项</th><th>值</th><th>来源（文件:符号）</th>'
                '<th>说明</th></tr>')
        rows = []
        for item in group["items"]:
            rows.append(
                f'<tr><td class="l">{esc(item["label"])}</td>'
                f'<td class="num"><b>{esc(item["display"])}</b></td>'
                f'<td class="l"><code>{esc(item["source"])}</code></td>'
                f'<td class="l">{rich(item["note"])}</td></tr>')
        blocks.append(f'<p class="note"><b>{esc(group["label"])}</b></p>'
                      f'<div class="scroll-x"><table class="tbl">{head}'
                      + "".join(rows) + '</table></div>')
    params = config.get("params")
    if params is None:
        body = (f'<p class="note">{NONE_MARK} —— '
                f'{esc(config.get("params_reason") or "无生效参数")}</p>')
    else:
        rows = "".join(
            f'<tr><td class="l"><code>{esc(str(k))}</code></td>'
            f'<td class="num">{esc(str(v))}</td></tr>' for k, v in sorted(params.items()))
        body = (
            f'<p class="note">周期 <code>#{esc(str(config["params_cycle"]["cycle_id"]))}</code>'
            f' · 策略版本 <code>{esc(str(config["params_cycle"]["script_id"]))}</code>'
            f' · 账户 <code>{esc(config["params_cycle"]["account_id"])}</code>'
            f' · 起跑 {esc(config["params_cycle"]["start_date"])}</p>'
            f'<p class="note">来源：<code>{esc(config["params_source"])}</code></p>'
            f'<div class="scroll-x"><table class="tbl">'
            '<tr><th>参数</th><th>值</th></tr>' + rows + '</table></div>'
            f'<p class="note">判据**原文**：<code>{esc(config["params_criteria"])}</code></p>')
    return ("".join(blocks)
            + f'<p class="note"><b>{esc(CONFIG_PARAMS_LABEL)}</b></p>' + body
            + glance(list(config.get("notes") or [])))


#: 「当前生效参数」那一节的标题（页面与报告引用同一串）。
CONFIG_PARAMS_LABEL = "当前生效的策略参数（`validation_cycles` 最近一行）"


def bypass_block(data: Mapping) -> str:
    """旁路触发留痕（P50 §4）：**只列已落库的事件**，页面上没有触发按钮。"""
    if not data:
        return f'<p class="note">没有旁路台账 —— {NONE_MARK}</p>'
    lines = [data["signal_note"], data["no_button_note"], data["idempotent_note"],
             data["conclusion_note"], f"触发命令（CLI）：`{data['cli_hint']}`"]
    if not data.get("n_events"):
        return glance([f"事件：{NONE_MARK} —— 最近没有机械信号命中"
                       "（**不是「没有利空」的结论**，只是没触发过）", *lines])
    head = ('<tr><th>#</th><th>信号</th><th>标的</th><th>日</th><th>实测</th>'
            '<th>阈值</th><th>留痕</th><th>指纹</th></tr>')
    rows = []
    for event in data["events"]:
        rows.append(
            f'<tr><td class="num">{event["event_id"]}</td>'
            f'<td class="l">{esc(str(event["kind"]))}</td>'
            f'<td class="num"><code>{esc(str(event["code"]))}</code></td>'
            f'<td class="num">{esc(str(event["asof"]))}</td>'
            f'<td class="num">{esc(str(event["at_value"]))}</td>'
            f'<td class="num">{esc(str(event["threshold"]))}</td>'
            f'<td class="l">{esc(event["step"])}</td>'
            f'<td class="num"><code>{esc(str(event["fingerprint"])[:16])}…</code></td>'
            f'</tr>')
    return (f'<div class="scroll-x"><table class="tbl">{head}'
            + "".join(rows) + '</table></div>'
            + glance([f"共 {data['n_events']} 条已落库事件（台账 = `system_events`，"
                      f"`module={data['module']}`）", *lines]))


def charts_text(data: Mapping) -> str:
    """曲线报告的文本版（曲线本身是 SVG，文本里给参数与样本量）。"""
    if not data:
        return "# 可视化报表\n\n无\n"
    out = [f"# 可视化报表（P50 §3）· {data.get('asof')}", ""]
    for block in data.get("blocks") or []:
        if not block.get("available"):
            out.append(f"- **{block['title']}**：无 —— {block.get('reason') or '无'}")
            continue
        out.append(f"- **{block['title']}**：已渲染（页面/报告用同一个 "
                   "`paper_render.race_svg`）")
        out += [f"  - {line}" for line in block.get("caption") or []]
    out += [f"> {data.get('source_note', '')}", f"> {data.get('signal_note', '')}"]
    return "\n".join(out) + "\n"


def config_text(data: Mapping) -> str:
    """配置视图的文本版（与页面**同一份**载荷）。"""
    if not data:
        return "# 只读配置视图\n\n无\n"
    out = ["# 只读配置视图（P50 §2 / D-32）", ""]
    for group in data.get("groups") or []:
        out += [f"## {group['label']}", "",
                "| 项 | 值 | 来源 |", "|---|---|---|"]
        for item in group["items"]:
            out.append(f"| {item['label']} | {item['display']} | "
                       f"`{item['source']}` |")
        out.append("")
    out.append(f"## {CONFIG_PARAMS_LABEL}")
    if data.get("params") is None:
        out.append(f"无 —— {data.get('params_reason')}")
    else:
        out += [f"来源 `{data['params_source']}`；周期 "
                f"#{data['params_cycle']['cycle_id']}；"
                f"判据原文 `{data['params_criteria']}`", "",
                "| 参数 | 值 |", "|---|---|"]
        out += [f"| `{k}` | {v} |" for k, v in sorted(data["params"].items())]
    out += ["", *[f"> {n}" for n in data.get("notes") or []]]
    return "\n".join(out) + "\n"


def bypass_text(data: Mapping) -> str:
    """旁路触发留痕的文本版。"""
    if not data:
        return "# 事件旁路触发\n\n无\n"
    out = [f"# 事件旁路触发（P50 §4 / D-45）· {data.get('asof')}", ""]
    if not data.get("n_events"):
        out.append("事件：无 —— 最近没有机械信号命中（不是「没有利空」的结论）")
    else:
        out += ["| # | 信号 | 标的 | 日 | 实测 | 阈值 | 留痕 |", "|" + "---|" * 7]
        for event in data["events"]:
            out.append(f"| {event['event_id']} | {event['kind']} | "
                       f"`{event['code']}` | {event['asof']} | {event['at_value']} | "
                       f"{event['threshold']} | {event['step']} |")
    out += ["", f"> {data['signal_note']}", f"> {data['no_button_note']}",
            f"> {data['idempotent_note']}", f"> {data['conclusion_note']}",
            f"> 触发命令（CLI）：`{data['cli_hint']}`"]
    return "\n".join(out) + "\n"


def cases_text(data: Mapping) -> str:
    if not data.get("cases"):
        return f"# 错判案例集 · {data['asof']}\n\n{data.get('empty_reason', '无案例')}\n"
    out = [f"# 错判案例集 · {data['asof']}", "",
           f"方向判错的样本 {data['n_miss_total']} 条，本表列最近 {data['n_cases']} 条"
           f"（上限 {data['limit']} 条，按目标日倒序，没有筛选参数）", "",
           "| 标的 | 决策日 | 目标日 | 预测 | 实际 | 实际收益 | 插桩 | 脚本版本 | "
           "PIT 输入 sha256 | 候选（程序） | 结论（人工） |",
           "|" + "---|" * 12]
    for c in data["cases"]:
        cands = " / ".join(
            f"{x['label_text']}({x['signal_text']}；阈值 {x['threshold']}；"
            f"实测 {x['at_value']})" for x in c.get("attribution_auto") or [])
        manual = c.get("attribution_manual")
        out.append(f"| `{c['code']}` | {c['asof_date']} | {c['target_date']} | "
                   f"{c['predicted_class']} | {c['actual_class']} | "
                   f"{_cell(c['actual_pct'], kind='pct')} | {c['plugin_id']} | "
                   f"`{c['script_version']}` | `{str(c['input_sha256'])[:16]}…` | "
                   f"{cands or '（无）'} | "
                   f"{data['labels'].get(manual, manual) if manual else '（空）'} |")
    out += ["", f"> 分母：窗口内可评分的预测共 {data['n_scored']} 条",
            f"> {data['attribution_note']}",
            f"> {data['manual_note']}"]
    if data.get("not_scanned_note"):
        out.append(f"> {data['not_scanned_note']}")
    return "\n".join(out) + "\n"


def m2_text(data: Mapping) -> str:
    """`m2 report` 的全文（七段与页面**同一份** `m2_data.panel()`）。"""
    return "\n".join([three_way_text(data["three_way"]),
                      forecast_text(data["forecast"]),
                      cases_text(data["cases"]),
                      charts_text(data["charts"]),
                      config_text(data["config"]),
                      bypass_text(data["bypass"]),
                      cycles_text(data["cycles"])])
