"""事件旁路触发（P50 §4 / D-45）：机械信号命中 ⇒ 不等验证周期结束即**重打分**。

## 触发源只有机械信号，没有外部推送

D-45 原文：`corp_actions` 新增事件（类型白名单）／单日跌幅 ≥ 阈值／`data_quality`
异常。**不接外部推送、不新增网络依赖** —— 本模块只读库里已有的三张表
（`corp_actions` / `bars_daily` / `data_quality`），信号与阈值来自
`stocklab/config/m2_signals.py`。

## 幂等是**结构性**的

同一 `(标的, 信号, asof)` 只触发一次，靠一条**表达式唯一索引**
（`uq_system_events_m2_bypass`：`json_extract(context_json,'$.fingerprint')`，
只覆盖 `module='m2_bypass'` 的行）。指纹是**语义键**（ADR-005）：
`sha256(版本|信号|标的|asof|语义键)` —— 语义键里放事件类型/除权日，**不放实测数值**。
把 `-6.2%` 放进指纹的话，同一条信号会在每次数据重采后换一个键，幂等就没了。

留痕写**既有台账** `system_events`（`store/repo.py::log_event`）——
不新造事件表。每条带信号原文、阈值、实测值与「算到哪一步」。

## 触发 ≠ 结论

「重打分」= 走**既有**打分器（`m2/score.py`）把到期但还没打分的预测补上，
产物仍落 `m2_forecast_scores`（append-only，一条预测一行）；本模块
**不改策略状态、不改账户、不改参数、不 approve、不写预测**。
判定（要不要冻结/优化）仍由人在 CLI 上触发（P49）—— 所以本模块
不 import 执行/上线路径（`tests/test_p50_bypass.py` 静态钉住）。

## 网页端没有触发按钮

页面只列已落库的事件（`stocklab m2 bypass scan` 才触发）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from stocklab.config import m2_signals as S
from stocklab.m2 import attribution as m2_attr
from stocklab.m2 import score as m2_score
from stocklab.m2 import store as m2_store
from stocklab.predict.service import market_axis
from stocklab.store import repo


def signal_fingerprint(kind: str, code: str, asof: str,
                       semantic: str) -> str:
    """`(信号, 标的, 日, 语义键)` → 指纹（**不含实测数值**，见模块 docstring）。"""
    payload = f"{S.FINGERPRINT_VERSION}|{kind}|{code}|{asof}|{semantic}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scan_signals(conn: sqlite3.Connection, *, asof: str) -> list[dict]:
    """`asof` 当天命中的机械信号（**只读**：一条也不写）。"""
    out: list[dict] = []

    for row in conn.execute(
            "SELECT code, cqr, content FROM corp_actions WHERE cqr = ?"
            " ORDER BY code", (str(asof),)):
        content = str(row["content"] or "")
        for pattern in S.CORP_ACTION_TEXT_WHITELIST:
            if not re.search(pattern, content):
                continue
            out.append({
                "kind": S.SIGNAL_CORP_ACTION, "code": str(row["code"]),
                "asof": str(asof), "semantic": f"{pattern}:{row['cqr']}",
                "signal_text": S.SIGNAL_TEXTS[S.SIGNAL_CORP_ACTION],
                "threshold": None, "at_value": None,
                "step": (f"读 `corp_actions`（`cqr = {asof}`）：原文「{content}」"
                         f"命中白名单模式「{pattern}」⇒ 触发一次重打分"),
                "detail": {"content": content, "keyword": pattern,
                           "cqr": str(row["cqr"])},
            })

    codes = [str(r["code"]) for r in conn.execute(
        "SELECT code FROM instruments WHERE type = 'stock' ORDER BY code")]
    for code in codes:
        value, how = m2_attr.pct_chg(conn, code, str(asof))
        if value is None or value > S.SINGLE_DAY_DROP_THRESHOLD:
            continue
        if m2_attr.is_suspended(conn, code, str(asof)):
            continue     # 停牌日没有可比跌幅（且 `pre_close` 口径在停牌日无意义）
        out.append({
            "kind": S.SIGNAL_STOCK_PCT_CHG, "code": code, "asof": str(asof),
            "semantic": "",
            "signal_text": S.SIGNAL_TEXTS[S.SIGNAL_STOCK_PCT_CHG],
            "threshold": S.SINGLE_DAY_DROP_THRESHOLD,
            "at_value": round(value, 6),
            "step": (f"读 `bars_daily`（{how}）：跌幅 {value:+.4%} ≤ "
                     f"{S.SINGLE_DAY_DROP_THRESHOLD:+.2%} ⇒ 触发一次重打分"),
            "detail": {"how": how},
        })

    for row in conn.execute(
            "SELECT code, issue_type, severity, detail FROM data_quality"
            " WHERE date = ? ORDER BY code, issue_type", (str(asof),)):
        issue_type = str(row["issue_type"])
        if issue_type not in S.DATA_QUALITY_TYPE_WHITELIST:
            continue
        out.append({
            "kind": S.SIGNAL_DATA_QUALITY, "code": str(row["code"]),
            "asof": str(asof), "semantic": issue_type,
            "signal_text": S.SIGNAL_TEXTS[S.SIGNAL_DATA_QUALITY],
            "threshold": None, "at_value": None,
            "step": (f"读 `data_quality`（{asof}）：异常类型 `{issue_type}`"
                     f"（{row['severity']}）在白名单内 ⇒ 触发一次重打分"),
            "detail": {"issue_type": issue_type, "severity": str(row["severity"]),
                       "detail": str(row["detail"] or "")},
        })

    for item in out:
        item["fingerprint"] = signal_fingerprint(
            item["kind"], item["code"], item["asof"], item["semantic"])
    out.sort(key=lambda x: (S.BYPASS_KINDS.index(x["kind"]), x["code"]))
    return out


def find_trigger(conn: sqlite3.Connection, fingerprint: str) -> dict | None:
    """该指纹是否已经触发过（幂等判据；返回既有那条留痕）。"""
    row = conn.execute(
        "SELECT * FROM system_events WHERE module = ?"
        " AND json_extract(context_json, '$.fingerprint') = ?",
        (S.BYPASS_MODULE, str(fingerprint))).fetchone()
    return None if row is None else dict(row)


def _rescore_code(conn: sqlite3.Connection, *, code: str, asof: str,
                  now: str) -> int:
    """把该标的**已到期但还没打分**的预测补上（走既有打分器，不写预测）。"""
    sessions = sorted(market_axis(conn))
    written = 0
    for row in m2_store.list_forecasts(conn):
        if str(row["code"]) != str(code):
            continue
        if m2_store.find_score(conn, int(row["forecast_id"])) is not None:
            continue
        target = m2_score.next_session(sessions, str(row["asof_date"]))
        if target is None or target > str(asof):
            continue
        payload = m2_score.score_forecast(conn, row, target=target)
        m2_store.insert_score(conn, score=payload, now=now, commit=False)
        written += 1
    conn.commit()
    return written


def trigger(conn: sqlite3.Connection, *, asof: str, now: str) -> dict:
    """扫一遍机械信号并逐条触发（幂等：同指纹返回「已存在」，**零写入**）。"""
    fired: list[dict] = []
    already: list[dict] = []
    n_rescored = 0
    for item in scan_signals(conn, asof=str(asof)):
        existing = find_trigger(conn, item["fingerprint"])
        if existing is not None:
            already.append({**item, "status": "already",
                            "event_id": int(existing["event_id"]),
                            "note": "已存在：该信号已触发过，本次一个字节都没写"})
            continue
        rescored = _rescore_code(conn, code=item["code"], asof=asof, now=now)
        n_rescored += rescored
        context = {
            "fingerprint": item["fingerprint"], "kind": item["kind"],
            "code": item["code"], "asof": str(asof),
            "signal": item["signal_text"], "threshold": item["threshold"],
            "at_value": item["at_value"], "step": item["step"],
            "detail": item["detail"], "rescored": rescored,
            "conclusion_note": ("触发 ≠ 结论：本条只是「该重打分」的留痕，"
                                "判定与上线仍走既有链路 + 人工闸门"),
        }
        message = (f"旁路触发 {item['kind']} · {item['code']} · {asof}："
                   f"{item['step']}（本次补打分 {rescored} 条）")
        try:
            event_id = repo.log_event(conn, S.BYPASS_MODULE, S.BYPASS_LEVEL,
                                      message, context=context, now=now)
            status, note = "fired", None
        except sqlite3.IntegrityError:
            # 唯一索引兜底（并发/绕过预读）：退回「已存在」而不是报错
            row = find_trigger(conn, item["fingerprint"])
            event_id = int(row["event_id"]) if row else None
            status, note = "already", "唯一索引拦下重复触发"
        fired.append({**item, "status": status, "event_id": event_id,
                      "rescored": rescored, "note": note,
                      "message": message})
    return {
        "asof": str(asof), "n_signals": len(fired) + len(already),
        "n_fired": len([f for f in fired if f["status"] == "fired"]),
        "n_already": len([f for f in fired if f["status"] == "already"])
        + len(already),
        "rescored": n_rescored,
        "fired": fired, "already": already,
        "note": ("触发 ≠ 结论：重打分的产物落 `m2_forecast_scores`（既有链路），"
                 "判定与上线仍要人在 CLI 上做（P49 / D-1）；本路径不 approve、"
                 "不改账户与参数"),
    }


def events(conn: sqlite3.Connection, *, asof: str | None = None) -> list[dict]:
    """已落库的旁路事件（只读；页面用它，不提供触发按钮）。"""
    sql = ("SELECT * FROM system_events WHERE module = ?")
    args: list = [S.BYPASS_MODULE]
    if asof is not None:
        sql += " AND json_extract(context_json, '$.asof') <= ?"
        args.append(str(asof))
    sql += " ORDER BY event_id"
    out = []
    for row in conn.execute(sql, tuple(args)):
        item = dict(row)
        try:
            context = json.loads(item.pop("context_json") or "{}")
        except ValueError:
            context = {}
        item["context"] = context
        out.append(item)
    return out
