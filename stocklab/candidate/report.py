"""步骤11：候选池 Markdown 报告。

## 报告必须自带「已知限制」

本轮的三个近似——① ST 判据非 PIT；② 财务数据未接、中期/长期池的因子
是桩；③ 只有 20 只种子、不是文档要求的全标的扫描——**逐字写在报告里**。

理由：这三个限制恰恰会让报告「看起来」比实际更可信。一份带假中期
打分的报告，如果不写明，读的人会以为中期池是被真实财务因子筛出来的。
`CLAUDE.md`《准确度纪律》要求跑不赢就说跑不赢，这里是同一个原则的
另一面：**没做到就说没做到**。

## 确定性

同样的输入必须产出逐字节相同的输出（设计文档 §11.2）。所以不用
`dict` 的迭代顺序、不用当前时间戳做内容（`generated_at` 由调用方传入）。
"""

from __future__ import annotations

import json

from stocklab.candidate.pools import ALL_POOLS, POOL_TOPN

POOL_TITLES: dict[str, str] = {
    "short": "短期池", "mid": "中期池", "long": "长期池",
}


def _risks(risk_json: str) -> str:
    try:
        items = json.loads(risk_json)
    except (TypeError, ValueError):
        return risk_json or "—"
    return "、".join(str(x) for x in items) if items else "—"


def _pool_section(pool: str, members: list[dict]) -> list[str]:
    rows = [m for m in members if m["pool"] == pool]
    rows.sort(key=lambda m: (-m["adj_score"], m["code"]))
    lines = [f"## {POOL_TITLES[pool]}（上限 {POOL_TOPN[pool]} 只）", ""]
    if not rows:
        lines.append("（空）")
        lines.append("")
        return lines
    lines.append("| 代码 | 原始分 | 修正分 | 状态 | 理由 | 风险 |")
    lines.append("|---|---|---|---|---|---|")
    for m in rows:
        lines.append(
            f"| {m['code']} | {m['raw_score']:.2f} | {m['adj_score']:.2f} "
            f"| {m['status']} | {m['reason']} | {_risks(m['risk_json'])} |")
    lines.append("")
    return lines


def render_report(*, asof: str, run_kind: str, loaded: dict,
                  generated_at: str) -> str:
    snap = loaded["snapshot"]
    members = loaded["members"]
    rejects = loaded["rejects"]

    out: list[str] = [
        f"# 候选池报告 · {asof}",
        "",
        f"- 生成时间：{generated_at}",
        f"- 快照 ID：{snap['snapshot_id']}（类型 `{run_kind}`）",
        f"- 种子范围：{snap['params'].get('seed_count', '?')} 只",
        f"- 入池 {len(members)} 条 / 淘汰 {len(rejects)} 条",
        "",
        "> ⚠️ **本报告仅作研究观察，不构成投资建议。**",
        "",
    ]

    for pool in ALL_POOLS:
        out += _pool_section(pool, members)

    out += ["## 淘汰清单", ""]
    if not rejects:
        out.append("无")
    else:
        out.append("| 代码 | 阶段 | 原因 |")
        out.append("|---|---|---|")
        for r in rejects:
            out.append(f"| {r['code']} | {r['stage']} | {r['reason']} |")
    out.append("")

    out += [
        "## 已知限制（本轮）",
        "",
        "1. **财务数据未接**：中期池与长期池的景气/护城河因子是**桩**，"
        "返回的是占位分数，不是真实财务打分。财报类排雷"
        "（审计非标、财务造假）同样留桩未实现。",
        "2. **ST 判据非 PIT**：取自标的**当前**名称字段，历史某日的 ST "
        "状态无法还原，仅为近似。",
        "3. **样本范围不是全标的扫描**：本轮只跑固定种子清单，"
        "文档 07 要求的「全标的扫描」尚未实现。",
        "4. **结论门槛**：候选池的因子有效性结论需 ≥120 交易日样本，"
        "且须走 walk-forward 与按日聚类，不足则只作观察。",
        "",
    ]
    return "\n".join(out)
