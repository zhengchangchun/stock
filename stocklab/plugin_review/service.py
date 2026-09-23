"""插桩5 的服务层（P58）：解析在役版本 → 装配输入 → 跑脚本 → 落报告与台账。

## 复盘不是每日动作

它在文档 01 里属于「AI 优化子流程内部」，所以它**不进** `candidate run` 的 12 步
主干、也**不进** `CLOSE_STEPS`。它是一条独立命令，并作为**非阻断步骤**挂在月度链上
（月度链负责「定期」，命令本身负责「这件事怎么做」）。

## 走既有生命周期，一个闸门都不松

`lifecycle.active_script_id(..., required=True)` ＋ `lifecycle.call_active(...)`：
预检（`guard.check_source`）、超时、返回结构校验（`contract.validate_return`）都是
既有实现，本模块不碰 `plugin/` 下的四个闸门文件。

## 没有在役版本 = 显式失败

`NoActivePlugin` 一路上抛到 CLI ⇒ exit 2、点名「插桩5 没有 active 版本」、**零写入**
（不落台账、不写报告）。**不兜底**：兜底会让报告里的策略名与实际执行的东西脱钩。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from stocklab.config import paths
from stocklab.plugin import lifecycle, store as plugin_store
from stocklab.plugin_review import inputs, render, store

#: 插桩5 的编号（文档 01 的插桩清单第 5 行）。
PLUGIN_ID = "5"

#: 桩的返回里那个标记（现役 v1.0.0 的 `analysis_result["status"]`）。
#: 见到它 ⇒ 报告第 1 行必须自报「尚未实现」，且脚本输出**原文落库**。
STUB_STATUS = "not_implemented"


def report_path(asof: str, *, report_dir: Path | str | None = None) -> Path:
    """报告落点：`<报告根>/plugin-review/<asof>.md`。

    ⚠️ **不是** `<报告根>/<asof>-review.md`（session review 的地盘，patrol ⑤ 按
    那个名字判存亡）。
    """
    root = Path(report_dir) if report_dir else paths.REPORT_DIR
    return root / render.REPORT_SUBDIR / f"{asof}.md"


def run_review(conn: sqlite3.Connection, *, asof: str, now: str,
               report_dir: Path | str | None = None) -> dict:
    """跑一次复盘并返回载荷。**写两类东西**：报告文件 ＋ `plugin_reviews` 一行。

    失败面（由 CLI 映射成退出码）：
    - `lifecycle.NoActivePlugin` ⇒ 没有在役版本（exit 2，零写入）；
    - `guard.PluginGuardError` / `runtime.PluginTimeout` /
      `contract.PluginContractError` ⇒ 有版本但跑不出来（exit 1，零写入）。
    """
    script_id = lifecycle.active_script_id(conn, PLUGIN_ID, required=True)
    row = plugin_store.get_script(conn, script_id)
    ctx = inputs.build_ctx(conn, asof, script_id=script_id,
                           script_version=str(row["version"]))
    result = lifecycle.call_active(conn, PLUGIN_ID, ctx, script_id=script_id)
    analysis = result["analysis_result"]
    bad_cases = result["bad_case_list"]
    stub = analysis.get("status") == STUB_STATUS
    status = STUB_STATUS if stub else "ok"

    summary = inputs.summarize(ctx)
    body = render.render(asof=asof, script_id=script_id,
                         script_version=str(row["version"]), stub=stub,
                         analysis=analysis, bad_cases=bad_cases, ctx=ctx,
                         summary=summary)
    out = report_path(asof, report_dir=report_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")

    ledger, inserted = store.insert_review(
        conn, asof=asof, plugin_id=PLUGIN_ID, script_id=script_id,
        script_version=str(row["version"]), source_sha256=str(row["source_sha256"]),
        status=status, analysis=analysis, bad_cases=bad_cases, inputs=summary,
        report_path=str(out),
        report_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(), now=now)

    return {
        "ok": True, "exit_code": 0, "asof": asof,
        "plugin_id": PLUGIN_ID, "script_id": script_id,
        "script_version": str(row["version"]), "stub": stub, "status": status,
        "report_path": str(out), "review_id": ledger.get("review_id"),
        "inserted": inserted,
        "n_samples": len(ctx["samples"]), "n_candidates": ctx["n_candidates"],
        "n_dropped": len(ctx["sample_drops"]),
        "n_backtests": len(ctx["backtests"]),
        "n_bad_cases": len(bad_cases),
    }
