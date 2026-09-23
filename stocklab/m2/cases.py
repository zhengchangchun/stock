"""模块2 错判案例集：**唯一**的取数口径（从 `labweb/m2_data.py` 原样搬来，P58 §3）。

## 为什么搬家

P48 定的口径是「错判案例集**没有筛选参数**」——不按幅度、不按标的、不按版本挑，
能挑就等于能把不好看的那几条藏起来。这条口径必须**只有一份**。
它原先长在 `labweb/m2_data.py` 里（web 层，`import m2_charts` 等页面依赖），
于是任何非页面的消费者（P58 的插桩5 复盘）要么反向依赖页面层，要么把口径抄一遍
——后者正是「同口径才可比」这句话最容易被破坏的入口。

所以这一段搬进 `m2/`：页面侧改为从本模块 import（公开名字一个不少），
新消费者也读同一段代码。`m2_data.py` 的 `_cycle_cases` / `case_summary` /
`review_backlog` 继续调这里的 `miss_cases`。

## 缺数据一律 `None` + 原因，**不写 0**

`0` 在这几块里是**有意义的读数**（0 次命中、0 条错判），拿它表示「没有」会让
「这天没有错的样本」与「不知道这天」长得一模一样。
"""

from __future__ import annotations

import sqlite3

from stocklab.m2 import config as m2_config
from stocklab.m2 import store as m2_store

#: 错判案例的条数上限（P48 §2：**不是可选参数**，见 `m2/config.CASE_LIMIT`）。
CASE_LIMIT: int = m2_config.CASE_LIMIT

#: 归因字段名（`m2/config.ATTRIBUTION_FIELD`）。P50 的人工确认填的就是这一格，
#: 本期它**恒为 `None`**（D-31）。
ATTR_FIELD: str = m2_config.ATTRIBUTION_FIELD

#: 归因字段的处置（D-31）：四分类代码判不了，这里**恒空**，结构位留给 P50 的人工确认。
ATTRIBUTION_NOTE = (
    "归因恒空：D-31 明令四分类（大盘冲击 / 行业黑天鹅 / 个股突发利空 / 因子失效）"
    "代码判不了，只能由人工确认（P50）。本页与报告**不自动填充**任何一档 —— "
    "编一个标签比留空危险得多，因为它看起来像一个结论")


def miss_cases(conn: sqlite3.Connection, asof: str) -> dict:
    """方向错的样本，按 `target_date` **倒序**取最近 `CASE_LIMIT` 条（P48 §2 第 5 行）。

    **没有筛选参数**：不按幅度、不按标的、不按版本挑 —— 能挑就等于能把
    不好看的那几条藏起来。样本总量与条数上限都显示出来，读者自己知道
    看到的是最近的一段，不是全部。
    """
    scores = m2_store.list_scores(conn, asof=asof)
    by_forecast = {int(f["forecast_id"]): f
                   for f in m2_store.list_forecasts(conn)}
    misses = [s for s in scores if s["scorable"] and s["hit_direction"] == 0]
    misses.sort(key=lambda s: (str(s["target_date"]), str(s["code"]),
                               int(s["forecast_id"])), reverse=True)
    cases = []
    for s in misses[:CASE_LIMIT]:
        f = by_forecast.get(int(s["forecast_id"])) or {}
        cases.append({
            # 分数行与预测行的主键都带出来：回流汇总要按 `forecast_id` 取来源行的
            # 落库时间（生成时间），页面/报告里也是「这条案例连着哪条预测」的锚
            "score_id": int(s["score_id"]), "forecast_id": int(s["forecast_id"]),
            "code": str(s["code"]), "asof_date": str(s["asof_date"]),
            "target_date": str(s["target_date"]),
            "plugin_id": str(s["plugin_id"]),
            "script_version": str(s["script_version"]),
            "account_id": str(s["account_id"]),
            # 溯源：这条预测是在哪份 PIT 输入上算出来的
            "input_sha256": f.get("input_sha256"),
            "predicted_class": s["pred_class"], "actual_class": s["actual_class"],
            "actual_pct": s["actual_pct"],
            "direction": f.get("direction"),
            # D-31：恒空，结构位留给 P50 的人工确认
            "attribution": None,
        })
    return {
        "asof": asof,
        "cases": cases,
        "n_cases": len(cases),
        "n_miss_total": len(misses),
        "n_scored": sum(1 for s in scores if s["scorable"]),
        "limit": CASE_LIMIT,
        "attribution_note": ATTRIBUTION_NOTE,
        "empty_reason": (None if cases else
                         ("窗口内没有可评分的预测" if not scores else
                          "窗口内没有方向判错的样本")),
    }
