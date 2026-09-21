"""历史回放（Task 35，P7）：逐日 `predict` + 次日打分，产出第一份准确率报告。

## 为什么「历史回放」在本项目里是**合法的样本外评估**

P6 的模型是**纯 PIT**：每个 `asof` 只用库里 `<= asof` 的行算 `Normal(mu, sigma)`，
**没有任何参数拟合**（`WINDOW` / `LEVEL_WINDOW` 是口径、`FLAT_BAND` 由总纲 §8.2 规定，
本任务一个都没调）。所以「第 t 天的预测」与「t 那天真的在线跑一次」**是同一个东西** ——
回放不是在拟合出来的模型上回头找好日子，而是把同一个函数在历史上跑一遍。

这条性质是**前提**，不是修辞：一旦哪天模型开始用样本内数据拟合参数，
这份报告立刻失效。故报告里必须写明 `provenance`（见 `report.summarize`）。

## 一次回放做三件事

1. **逐日预测并落库**（`build_predictions` + `insert_prediction`）——
   走的是 `predict run` **完全同一条代码路径**，`test_backfill_payloads_match_predict_run`
   逐日比对 `payload_sha256` 钉住这一点（回放 ≠ 另写一套算法）；
2. **次日打分并落库**（`verify_target`）；
3. **从库里读出全部 verification 行**聚合成报告 —— 报告不依赖内存里攒的中间量，
   所以「重跑 → 读到的行一样 → 报告逐字节一样」。

`PitCache` 让第 1 步从实测 0.16 秒/日降到约 0.02 秒/日（见其 docstring）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from stocklab.config.costs import CostModel
from stocklab.predict.service import PitCache, build_predictions
from stocklab.predict.store import PredictionConflict, insert_prediction
from stocklab.store import repo
from stocklab.verify.score import CAPITAL
from stocklab.verify.service import NoPredictions, verify_target


def session_dates(conn: sqlite3.Connection, codes: Sequence[str]) -> list[str]:
    """可用于 `asof` / `target_date` 的交易日 = **日历 ∩ 行情轴**。

    与 `predict.service.assert_session` 同一判据（那边是逐日断言，这边要一次取全）。
    取交集而不是只看日历：日历说开市、库里却没有那天的行情，是**采集缺口**，
    拿它当交易日会产出「用旧价冒充今天」的预测（P6 已把这条钉成硬拒绝）。
    """
    from stocklab.calendar.trading_calendar import Calendar

    cal = set(Calendar.load(conn).all_dates)
    marks = ",".join("?" * len(codes))
    axis = {r["date"] for r in conn.execute(
        f"SELECT DISTINCT date FROM bars_daily WHERE code IN ({marks})",
        tuple(codes))}
    return sorted(cal & axis)


def backfill(conn: sqlite3.Connection, from_date: str, to_date: str, *,
             codes: Sequence[str] | None = None, costs: CostModel | None = None,
             capital: float = CAPITAL, cache: PitCache | None = None,
             now: str | None = None, progress=None) -> dict:
    """回放 `[from_date, to_date]`（**按 `target_date` 计**）内的每一天。

    对每个目标日 `t`：`asof` = `t` 的**上一个**交易日 → 出预测 → 给 `t` 打分。
    没有「上一个交易日」的（区间起点即最早交易日）跳过并记进 `skipped`。

    返回 `{"pairs", "skipped", "storage", "conflicts", "summaries"}`；
    报告由调用方从库里读行生成（本函数只负责把行**写进去**）。
    """
    cache = cache or PitCache()
    costs = costs or CostModel()
    now = now or repo.now_iso()

    if codes is None:
        codes = [r["code"] for r in conn.execute(
            "SELECT code FROM instruments WHERE active=1 AND type='stock'"
            " ORDER BY code")]
    codes = list(codes)
    sessions = session_dates(conn, codes)
    index_of = {d: i for i, d in enumerate(sessions)}

    pairs: list[dict] = []
    skipped: dict[str, str] = {}
    storage: dict[str, str] = {}
    conflicts: dict[str, str] = {}

    targets = [d for d in sessions if from_date <= d <= to_date]
    for target in targets:
        i = index_of[target]
        if i == 0:
            skipped[target] = "区间起点是本库最早的交易日，没有「上一交易日」"
            continue
        asof = sessions[i - 1]
        try:
            rep = build_predictions(conn, asof, codes, cache=cache)
        except ValueError as exc:            # 非交易日 / 日历为空
            skipped[target] = f"asof={asof} 不可预测：{exc}"
            continue
        if not rep["predictions"]:
            skipped[target] = f"asof={asof} 没有任何可出预测的标的：{rep['skipped']}"
            continue
        for p in rep["predictions"]:
            try:
                state, pred_id = insert_prediction(conn, p, now=now, origin="replay")
            except PredictionConflict as exc:
                conflicts[f"{p['code']}@{asof}"] = str(exc)
                continue
            storage[str(pred_id)] = f"{p['code']}@{asof}:{state}"
        try:
            v = verify_target(conn, target, costs=costs, capital=capital,
                              codes=codes, cache=cache, now=now,
                              # 只评**本次回放刚写下**的那一版：不带这个过滤，跨版本重放
                              # 会把旧版本已评分的行按新口径重算 ⇒ 撞上 append-only 守卫
                              # （2026-09-21 实测）。见 `verify_target` 的 `model_version`
                              # 段与 `docs/plans/2026-09-21-MODEL_VERSION-v1.0.2-复权口径升版.md`。
                              model_version=rep["model_version"])
        except NoPredictions as exc:
            skipped[target] = str(exc)
            continue
        pairs.append({"asof": asof, "target": target,
                      "predicted": sorted(storage_codes(rep)),
                      "by_model_version": v["by_model_version"]})
        if progress is not None:
            progress(target, len(pairs), len(targets))

    return {"from_date": from_date, "to_date": to_date, "codes": codes,
            "pairs": pairs, "skipped": skipped, "storage": storage,
            "conflicts": conflicts,
            "n_sessions_in_range": len(targets)}


def storage_codes(rep: dict) -> list[str]:
    """该日实际出预测的标的（报告与日志用）。"""
    return [p["code"] for p in rep["predictions"]]


def load_verification_rows(conn: sqlite3.Connection, from_date: str,
                           to_date: str) -> list[dict]:
    """从库里读出 `[from, to]` 内的全部 verification 行（报告的唯一数据来源）。

    刻意 join `predictions` 拿 `model_version` 与 `code`：`verifications` 表本身
    没有这两列，而报告必须按 `model_version` 分组（v1.0.0 的错误预测不许混进 v1.0.1）。
    """
    from stocklab.verify.store import verification_from_row

    rows = conn.execute(
        "SELECT v.*, p.code AS p_code, p.asof_date AS p_asof,"
        " p.model_version AS p_model_version"
        " FROM verifications v JOIN predictions p ON p.pred_id = v.pred_id"
        " WHERE v.target_date BETWEEN ? AND ?"
        " ORDER BY p.model_version, v.target_date, p.code",
        (from_date, to_date)).fetchall()
    out: list[dict] = []
    for r in rows:
        payload = verification_from_row(r)
        out.append({**payload, "code": r["p_code"], "asof_date": r["p_asof"],
                    "model_version": r["p_model_version"],
                    "verification_id": int(r["verification_id"])})
    return out
