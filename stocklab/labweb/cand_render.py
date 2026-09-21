"""模块1 渲染（候选池页面）。

## 复用，不新建版式

壳（`layout` / `.rail`）、表格（`table.tbl`）、空行（`tr.empty`）、折叠（`more`）、
横幅（`banner` / `alert`）全部复用 `render.py` 的既有件 —— **一行 CSS 都不加**，
`app.css` 里现有的类就够（实测 `.tbl` / `tr.empty` / `.sub` / `.scroll-x` 都在）。

## 映射表只换标签，不改判据

`STAGE_CN` / `RUN_KIND_CN` 与 `render.py` 的 `STATUS_CN` / `CHECK_CN` 同一条规矩：
**认不出的原样显示机器名**（宁可难看，也不许猜它是什么意思）。
`plugin_id IS NULL` 显示「未知」，不写 0、不写空。

## 诚实性

- 未出现差集是**差额**，不是原因 —— 文案里说清楚（设计文档 §8.4）；
- 「淘汰记录」不是完整差集 —— 脚注写明（评审 S1）；
- 没有数的地方写「未知 / 无 / 本池为空」，不写 0。

## 选择器为什么是链接不是 `<select>`

快照的唯一键是 `(asof, run_kind)` **两个**值，一个原生 `<select>` 只能提交一个
参数（`<option value="2026-09-18|weekly">` 要再发明一套分隔符解析）。
链接既保住了 §3.2 的 URL 契约（`?asof=…&run_kind=…`），又不依赖 JS。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from stocklab.candidate.run import RUN_KINDS
from stocklab.labweb.render import (_field, _hidden, _text, alert, banner, esc,
                                    layout, more, num, rich, section)

#: 池 → (中文名, 调仓周期)。周期来自设计文档 §5.3 的池定义。
POOL_CN: dict[str, tuple[str, str]] = {
    "short": ("短期池", "5 日调仓"),
    "mid": ("中期池", "20 日调仓"),
    "long": ("长期池", "60 日调仓"),
}

#: 淘汰环节 → 人话。**只换标签**；认不出的原样显示机器名（见模块 docstring）。
STAGE_CN: dict[str, str] = {
    "pre_screen": "盘前排雷",
    "industry_screen": "行业排雷",
    "score": "打分未过",
}

#: 环节的展示顺序 = 12 步主干的先后（不按字典序）。
STAGE_ORDER: tuple[str, ...] = ("pre_screen", "industry_screen", "score")

RUN_KIND_CN: dict[str, str] = {
    "light": "轻量（盘中）",
    "weekly": "每周",
    "quarterly": "季度",
}

#: 摘要截断长度（超过就加省略号 —— 截断必须看得出来）。
_BRIEF = 68


def pool_label(pool: str) -> tuple[str, str]:
    """池 → (中文名, 周期)；认不出的池名原样返回，不猜。"""
    return POOL_CN.get(pool, (pool, "周期未知"))


def stage_label(stage: str) -> str:
    return STAGE_CN.get(stage, stage)


def run_kind_label(kind: str) -> str:
    return RUN_KIND_CN.get(kind, kind)


def _brief(text: Any, limit: int = _BRIEF) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[:limit] + "…"


def _score(x: Any) -> str:
    return num(x, 2)


def _risk_cell(row: Mapping) -> str:
    state, items = row["risk_state"], row["risk_items"]
    if state == "empty":
        return '<span class="mut">无</span>'
    if state == "bad":
        return ('<span class="s-warn">风险明细解析失败</span>'
                f'<span class="sub">（原文：{esc(_brief(row["risk_json"]))}）</span>')
    return (f'{len(items)} 条<span class="sub"> '
            f'{esc(_brief("、".join(str(x) for x in items)))}</span>')


def _name_cell(row: Mapping) -> str:
    """名称列：查不到就**显示代码本身**（不编名字）。"""
    if row.get("name"):
        return esc(row["name"])
    return f'<span class="mut">{esc(row["code"])}</span>'


def _pool_table(rows: Sequence[Mapping]) -> str:
    if not rows:
        return ('<div class="scroll-x"><table class="tbl">'
                '<tr><th>代码</th><th>名称</th><th>原始分</th><th>调整分</th>'
                '<th>理由</th><th>风险</th><th>状态</th></tr>'
                '<tr class="empty"><td colspan="7">本池为空</td></tr>'
                '</table></div>')
    body = "".join(
        f'<tr><td class="l">{esc(r["code"])}</td>'
        f'<td class="l">{_name_cell(r)}</td>'
        f'<td>{_score(r["raw_score"])}</td>'
        f'<td>{_score(r["adj_score"])}</td>'
        f'<td class="l">{esc(r["reason"])}</td>'
        f'<td class="l">{_risk_cell(r)}</td>'
        f'<td class="l">{esc(r["status"])}</td></tr>'
        for r in rows)
    return ('<div class="scroll-x"><table class="tbl">'
            '<tr><th>代码</th><th>名称</th><th>原始分</th><th>调整分</th>'
            '<th>理由</th><th>风险</th><th>状态</th></tr>'
            + body + '</table></div>')


def _group_rejects(rejects: Sequence[Mapping]) -> list[dict]:
    """按 `stage` 分组，顺序 = 主干先后，认不出的排最后（并按名字排序）。

    **只分组、不筛选**：每一条淘汰都必须在页面上出现一次。
    """
    order: dict[str, int] = {s: i for i, s in enumerate(STAGE_ORDER)}
    groups: dict[str, list[Mapping]] = {}
    for r in rejects:
        groups.setdefault(r["stage"], []).append(r)
    keys = sorted(groups, key=lambda s: (order.get(s, len(order)), s))
    return [{"stage": s, "label": stage_label(s), "rows": groups[s],
             "known": s in STAGE_CN} for s in keys]


def _rejects_table(rows: Sequence[Mapping]) -> str:
    body = "".join(
        f'<tr><td class="l">{esc(r["code"])}</td>'
        f'<td class="l">{esc(r["stage"])}</td>'
        f'<td class="l">{esc(r["reason"])}</td>'
        '<td class="l">'
        + (esc(r["plugin_id"]) if r.get("plugin_id")
           else '<span class="mut">未知</span>')
        + '</td></tr>'
        for r in rows)
    return ('<div class="scroll-x"><table class="tbl">'
            '<tr><th>代码</th><th>环节</th><th>原因</th><th>插桩版本</th></tr>'
            + body + '</table></div>')


def _reject_group(g: Mapping) -> str:
    """一个淘汰环节的小标题 + 表格。认不出的环节**额外标出库中的机器名**。"""
    raw = ('' if g["known"]
           else f'　<span class="mut">库中机器名 {esc(g["stage"])}</span>')
    return (f'<p class="note">▸ {esc(g["label"])}（{len(g["rows"])} 条）{raw}</p>'
            + _rejects_table(g["rows"]))


def _rejects_block(data: Mapping) -> str:
    groups = _group_rejects(data["rejects"])
    note = ("「淘汰记录」**不是完整差集**：它只含被显式记下的淘汰。"
            "既没入池、也没有淘汰记录的标的，见下面「未出现」。")
    if not groups:
        return section(
            "淘汰记录",
            '<p class="note">' + rich(
                "无 —— 本快照一条淘汰都没有记录。这**不代表**所有种子都入池了"
                "（见下面的「未出现」）。") + '</p>',
            note=note)
    counts = "　".join(f'{esc(g["label"])} {len(g["rows"])} 条' for g in groups)
    tables = "".join(_reject_group(g) for g in groups)
    return section(f'淘汰记录（{data["n_rejects"]} 条）',
                   f'<p>{counts}</p>',
                   note=note,
                   detail=more(tables, label="逐条明细"))


def _missing_block(data: Mapping) -> str:
    missing = data["missing"]
    note = ("这是**差额，不是原因**：它们既没入池、也没有淘汰记录。"
            "可能被排雷、可能打分未过但这轮没记、也可能该轮根本没覆盖 —— "
            "**原因不在快照里**。种子清单取的是 `SEED_CODES`（当前常量），"
            "快照时点的清单可能不同（`params_json` 只存了种子**数量**）。")
    if not missing:
        return section("未出现", '<p>没有 —— 每一只种子都出现在入池或淘汰里。</p>',
                       note=note)
    items = "、".join(
        (f'{m["code"]}（{esc(m["name"])}）' if m.get("name") else m["code"])
        for m in missing)
    return section(f'未出现（{len(missing)} 只）', f'<p>{items}</p>', note=note)


def _selector(data: Mapping, *, base: str) -> str:
    """快照选择器：一条一个链接，URL 带 `(asof, run_kind)` 两个参数。"""
    keys = data["keys"]
    chosen = data["chosen"]
    links = "".join(
        f'<li>'
        f'<a href="{esc(base)}/candidate?asof={esc(k.asof)}'
        f'&amp;run_kind={esc(k.run_kind)}"'
        + (' aria-current="page"' if k == chosen else '')
        + f'>{esc(k.label)}</a></li>' for k in keys)
    # `<ul>` 不能嵌在 `<p>` 里（浏览器会把 `<p>` 提前合上），所以分两块写
    return (f'<p class="note">本库共 {len(keys)} 条快照；当前显示 '
            f'<b>{esc(chosen.label)}</b>。选另一条：</p>'
            f'<ul class="list">{links}</ul>')


def _meta_block(data: Mapping) -> str:
    snap = data["snapshot"]
    topn = snap["params"].get("topn") or {}
    topn_txt = " / ".join(
        f'{pool_label(k)[0][:2]} {topn[k]}' for k in ("short", "mid", "long")
        if k in topn) or "未知"
    seed = snap["params"].get("seed_count")
    return ('<table class="kv">'
            f'<tr><th>快照</th><td>#{snap["snapshot_id"]}　'
            f'{esc(snap["asof"])} · {esc(snap["run_kind"])}'
            f'（{esc(run_kind_label(snap["run_kind"]))}）</td></tr>'
            f'<tr><th>参数</th><td>种子 '
            f'{seed if seed is not None else "<span class=mut>未知</span>"}'
            f' 只　TOPN {esc(topn_txt)}</td></tr>'
            f'<tr><th>生成于</th><td>{esc(snap["created_local"] or "未知")}'
            f'　<span class="mut">（Asia/Shanghai；库中 UTC '
            f'{esc(snap.get("created_at"))}）</span></td></tr>'
            f'<tr><th>成员 / 淘汰</th><td>{data["n_members"]} 行 / '
            f'{data["n_rejects"]} 条　'
            f'<span class="mut">同一代码可同时在多池出现，行数 ≠ 只数</span></td></tr>'
            f'<tr><th>报告</th><td><code>{esc(data["report_path"])}</code></td></tr>'
            '</table>')


def _run_form(*, base: str, token: str, form_id: str,
              default_asof: str, values: Mapping | None = None,
              error: str = "") -> str:
    v = dict(values or {})
    asof = v.get("asof") or default_asof
    kind = v.get("run_kind") or "weekly"
    opts = "".join(
        f'<option value="{esc(k)}"{" selected" if k == kind else ""}>'
        f'{esc(k)}（{esc(run_kind_label(k))}）</option>' for k in RUN_KINDS)
    fields = (
        _field("asof", "asof（跑哪一天）",
               _text("asof", type_="date", required=True, value=asof),
               width="w-date")
        + _field("run_kind", "run_kind",
                 f'<select id="f-run_kind" name="run_kind">{opts}</select>')
    )
    return ('<form method="post" action="' + esc(base) + '/candidate/run" '
            'class="form">'
            + _hidden("_token", token) + _hidden("_form_id", form_id)
            + (alert(error) if error else "")
            + f'<div class="form__row">{fields}'
            '<button class="btn" type="submit">跑一次候选池</button></div>'
            '<p class="note">' + rich(
                "跑的是**与 CLI 同一段代码**（21 只种子一遍打分，实测不到 1 秒），"
                "并落一份 `reports/candidate/<asof>-<kind>.md`。"
                "同一 `(asof, run_kind)` 只会有一条快照（幂等，不会覆盖已有结果）；"
                "快照 append-only，写错了只能靠新日期重跑。") + '</p>'
            '</form>')


def _notice(*, ran: str, sid: int | None) -> str:
    if ran == "new":
        return banner(f"✅ 已跑出新快照 #{sid}。下面的内容就是它的结果。",
                      "ok", landed=True)
    if ran == "exists":
        return banner(f"该 (asof, run_kind) 已有快照 #{sid}，**未重跑**（幂等）。"
                      "要看新结果请换 asof 或 run_kind —— 快照 append-only，"
                      "旧的一条不会被覆盖。", "warn")
    return ""


def candidate_page(data: Mapping, *, base: str, built_at: str, token: str,
                   form_id: str, default_asof: str, ran: str = "",
                   sid: int | None = None, error: str = "",
                   values: Mapping | None = None) -> str:
    """候选池页面（整页）。`default_asof` = 页面口径的今天（`ctx.lab.asof`）。"""
    head = [_notice(ran=ran, sid=sid),
            '<p class="note">' + rich(
                "本页是模块1 的展示层：看**系统选出了什么、为什么**。"
                "插桩版本迭代、沙盒回放结论还没上页面（设计文档 §7「不做」）。")
            + '</p>']

    if not data["db_exists"]:
        head.append(section(
            "数据库文件不存在",
            f'<p><code>{esc(data["db_path"])}</code></p>'
            '<p class="note">' + rich(
                "本页**不建库、不采数**：先 `stocklab db init`，采集脚本把行情与"
                "财报灌进去，再来这里看。正常起服务时这个分支进不到 —— "
                "`lab serve` 在启动期就检查库并退出，只有直接构造 `Context` "
                "的测试会看到本页。") + '</p>'))
        return layout(base=base, title="候选池", body="".join(head),
                      asof=default_asof, built_at=built_at, current="/candidate")

    body = list(head)
    body.append(section("跑一次", _run_form(
        base=base, token=token, form_id=form_id, default_asof=default_asof,
        values=values, error=error)))

    if not data["keys"]:
        body.append(section(
            "还没有跑过候选池",
            '<p>本库里 `candidate_snapshots` 一条都没有。</p>'
            '<p class="note">' + rich(
                "用上面「跑一次」写入第一条；或走 CLI："
                "`stocklab candidate run --asof <日期> --run-kind weekly`。")
            + '</p>'))
        return layout(base=base, title="候选池", body="".join(body),
                      asof=default_asof, built_at=built_at, current="/candidate")

    if data["stale"]:
        body.append(banner(
            "URL 里那条 `(asof, run_kind)` 在库里不存在，已回落到最新一条快照。",
            "warn"))
    body.append(section("当前快照", _meta_block(data)))
    body.append(_selector(data, base=base))

    for p in data["pools"]:
        title, cycle = pool_label(p["pool"])
        n = len(p["rows"])
        body.append(section(
            f"{title} · {cycle}", _pool_table(p["rows"]),
            right=f"{n} 只" if n else "",
            note="按调整分降序（同分按代码升序），取前 N —— 排序与报告、CLI 同源。"))

    body.append(_rejects_block(data))
    body.append(_missing_block(data))

    return layout(base=base, title="候选池", body="".join(body),
                  asof=default_asof, built_at=built_at, current="/candidate")


__all__ = ["POOL_CN", "RUN_KIND_CN", "STAGE_CN", "STAGE_ORDER", "candidate_page",
           "pool_label", "run_kind_label", "stage_label"]
