"""步骤8：风险权重修正（设计文档 §7，调插桩4）。

## 插桩4 看到的 ctx 是「调用方 ctx + 打分结果」

原样透传调用方的 ctx（含 asof/code/board 等），再覆盖 `raw_score` 与
`risk_list`。**覆盖而不是合并**：这两个键是插桩4 的输入契约，调用方
即使传了同名键也不该生效（否则会出现「ctx 里的 raw_score 与打分结果
不一致」这种极难查的错）。
"""

from __future__ import annotations

import sqlite3

from stocklab.candidate.score import ScoreOutcome
from stocklab.plugin import lifecycle

#: 风险加权插桩编号。
RISK_PLUGIN = "4"


def adjust(conn: sqlite3.Connection, outcome: ScoreOutcome,
           ctx: dict, *, plugin_overrides: dict[str, int] | None = None) -> tuple[float, list[str]]:
    """返回 `(final_score, risk_out)`。没有 active 插桩4 → `NoActivePlugin`。

    `plugin_overrides`：`{plugin_id: script_id}`，指定时用该版本（不要求 active）。
    """
    payload = dict(ctx)
    payload["raw_score"] = outcome.raw_score
    payload["risk_list"] = list(outcome.risk_list)
    payload["pool"] = outcome.pool
    payload["code"] = outcome.code

    result = lifecycle.call_active(
        conn, RISK_PLUGIN, payload,
        script_id=(plugin_overrides or {}).get(RISK_PLUGIN))
    return float(result["final_score"]), list(result["risk_out"])
