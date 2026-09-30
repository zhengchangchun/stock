"""「你自己过去做过什么」——喂给 AI 操盘手的 `own_history` 块（P84 / K1）。

## 只含 `arm` 自己

这一块是**自省**的输入侧：我这条臂过去 20 天的净值、最近 10 笔成交、
最近 5 条决策台账。它**不含**别的臂、不含大盘以外的对照、不含任何评分。

## 只描述，不预测（K5）

放的全是**已经落下**的行（`paper_nav_daily` / `paper_trades` /
`paper_agent_decisions`，都是 append-only）。**两种窗口**（P84 §0.7 修订）：

- 净值 / 成交：`date <= asof`（沿用 `store.load_nav` / `load_trades`）；
- 决策台账：**`asof` 严格早于决策日**（`asof < 决策日`）。

决策窗口取严格早于决策日，是因为**当天那条决策模型自己还没生成** ——
写进上下文既无用又自指；而且这样上下文只依赖「严格早于决策日、且已落定」的行，
**每个决策日的上下文都可复算**，D-49 的「重放同一条决策 ⇒ 未写入」才成立
（窗口含当天时，写下一行就改变同一天的上下文 ⇒ 重放必然 sha 不等）。

块内不许出现预测价 / 目标价 / 概率 / 信号分 / 评级 / 买卖建议 / 仓位建议。

## 口径复用 `paper/store.py`

`load_nav` / `load_trades` 已经是「只按 `date <= asof`、升序」的既有实现
（PIT 语义在那里定死），本模块**切片取尾部**，不另写一条 SQL 去重述它 ——
两份过滤条件迟早会漂移，而漂移的方向永远是「多看到一天」。
决策台账不在 `store` 里（它不是净值/成交那种「账户状态重放」的输入），
故本模块自带两条 SQL（`COUNT(*)` 与取行），窗口即上面那条**严格早于决策日**。

## 尺寸有界（K6）

净值的全量行数可能上千，上下文里只放最近 ≤20 行；累计数
（`n_nav_days` / `n_trades` / `n_decisions`）另报**全量计数**——
「最近 20 行」与「一共跑了多少天」是两件事，混在一起会让模型把切片长度当历史长度。

## P85 / K5：末尾再加两个子键（`recent_reviews` / `facts`）

- `recent_reviews` —— 本臂最近 ≤3 条**复盘**（`paper_agent_reviews`，升序），
  原样搬运 `items` / `lessons`。这是「我上次复盘说了什么」。
- `facts` —— K4 的**已确认教训**：同一 `key` 在 **≥2 条不同 asof** 的复盘里出现过
  才进来（≤10 条）。这是「我反复看到的那件事」。

两块都**只增子键**（既有子键的名字/顺序/语义一字不动，K8），都受 PIT 约束
（`asof` 过滤）、都不含时间戳/自增 id ⇒ 在 `asof` 之后插一条复盘，
决策日的 `own_history` 与 `decision_context_sha256` **逐字节不变**。

**P86 修订（2026-09-27）**：窗口由 `asof <= 决策日` 收紧为 **`asof < 决策日`**。
原因见 `review._iter_reviews`：生成器要先写当天的复盘、再写当天的决策，而「当天的复盘
进当天决策的上下文」会让 ② 取的指纹立即失效（D-49 闸门必拒、同日重放报冲突）。
收紧后当天的上下文在当天所有写入之后一字不变，复盘仍照旧成为**次日**（也是「下一轮」）
的输入。**列出口**（`paper agent reviews` / `facts`）仍按 `<=` 陈列（不传 `strict`）。

判别 `recent_reviews`（原话）与 `facts`（归纳）是刻意的：原话可被引用、归纳可被检验，
把两者合成一块，「我说过什么」与「我得出的结论是什么」就再也分不开了。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.paper import review, store

#: 切片上限（K6，写进用例）。
NAV_LIMIT = 20
TRADE_LIMIT = 10
DECISION_LIMIT = 5
#: P85 / K5：复盘与已确认教训的条数上限（真源在 `paper/review.py`）。
REVIEW_LIMIT = review.REVIEW_LIMIT
FACT_LIMIT = review.FACT_LIMIT

_TABLE_DECISIONS = "paper_agent_decisions"

_NOTES: tuple[str, ...] = (
    "本块只含 `arm` **自己**已经发生的历史（净值 / 成交 / 决策台账）。窗口分两种："
    "净值与成交按 `date <= asof`（PIT 的唯一实现在 `store.load_nav` / `load_trades`），"
    "决策台账按 **`asof` 严格早于决策日** —— 当天那条决策模型自己还没生成，"
    "且这样每个决策日的上下文都可复算（重放同一天 ⇒ 幂等成立）。"
    "不含任何前向字段（预测价/概率/信号分/评级/买卖建议）。",
    "尺寸有界：净值 ≤20 行、成交 ≤10 笔、决策 ≤5 条（升序；净值/成交的最后一行 = "
    "`asof` 或之前最近一行，决策的最后一条 = **严格早于决策日**的最近一条）；"
    "`n_nav_days` / `n_trades` / `n_decisions` 是**全量计数**，不是切片长度。",
    "`realized_fees_total` 取最新一行净值的 `cum_cost`；没有净值行时由成交的 "
    "`fee_total` 求和 —— 两种口径都在这里写明，不静默换口径。",
)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _round4(x: object) -> float | None:
    return None if x is None else round(float(x), review.DISPLAY_DP)


def _names(conn: sqlite3.Connection, codes: list[str]) -> dict[str, str]:
    """`code → instruments.name`（一条 SQL）。查不到 / 没这张表 ⇒ 空字典。"""
    if not codes or not _has_table(conn, "instruments"):
        return {}
    marks = ",".join("?" * len(codes))
    return {str(r["code"]): str(r["name"]) for r in conn.execute(
        f"SELECT code, name FROM instruments WHERE code IN ({marks})", codes)}


def _n_orders(payload_json: object) -> int | None:
    """该决策行的载荷里有几条腿。解析失败 ⇒ `None`（调用方点名，**不抛**）。"""
    if payload_json is None:
        return None
    try:
        payload = json.loads(str(payload_json))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    decisions = payload.get("decisions")
    return len(decisions) if isinstance(decisions, list) else None


def _n_rejected(rejected_json: object) -> int:
    if rejected_json is None:
        return 0
    try:
        rejected = json.loads(str(rejected_json))
    except (ValueError, TypeError):
        return 0
    return len(rejected) if isinstance(rejected, list) else 0


def _decisions_block(conn: sqlite3.Connection, *, arm: str, asof: str) -> tuple[list[dict], int, bool]:
    """最近 `DECISION_LIMIT` 条决策（升序）＋ 全量条数 ＋ 「有行解析失败」标记。

    窗口 = `asof < 决策日`（P84 §0.7 修订）：当天的决策还没生成，不算历史。
    """
    if not _has_table(conn, _TABLE_DECISIONS):
        return [], 0, False
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM {_TABLE_DECISIONS} WHERE arm = ? AND asof < ?",
        (arm, asof)).fetchone()[0])
    rows = conn.execute(
        f"SELECT decision_id, asof, decision_kind, context_sha256, n_trials,"
        f" payload_json, rejected_json FROM {_TABLE_DECISIONS}"
        " WHERE arm = ? AND asof < ? ORDER BY asof DESC, decision_id DESC LIMIT ?",
        (arm, asof, DECISION_LIMIT)).fetchall()
    broken = False
    out: list[dict] = []
    for r in reversed(rows):                      # 倒序取尾部 → 翻回升序
        n_orders = _n_orders(r["payload_json"])
        if n_orders is None:
            broken = True
            n_orders = 0
        out.append({"decision_id": int(r["decision_id"]),
                    "asof": str(r["asof"]),
                    "decision_kind": str(r["decision_kind"]),
                    "n_orders": n_orders,
                    # 全 64 位 —— 对账要用它，截断了就没法比对（K6 的例外：不是数值）。
                    "context_sha256": str(r["context_sha256"] or ""),
                    "n_trials": int(r["n_trials"] or 0),
                    "n_rejected": _n_rejected(r["rejected_json"])})
    return out, total, broken


def own_history_block(conn: sqlite3.Connection, *, arm: str, asof: str) -> dict:
    """`own_history` 块（字段照 P84 任务书 §0.6，逐字）。

    `load_nav` / `load_trades` 自带 `date <= asof`（PIT 的唯一实现），
    决策台账自带 `asof < 决策日`（P84 §0.7 修订，见模块 docstring）；
    本函数只做切片、取名、计数。缺表 / 空库 ⇒ 各列表为空、各计数为 0，**不抛**。

    P85 起末尾多两个子键 `recent_reviews` / `facts`（K5，见模块 docstring）——
    **只增键**，既有子键逐位不变（K8）。两者自带同一层 PIT 过滤。
    """
    nav_rows = ([dict(r) for r in store.load_nav(conn, arm, asof=asof)]
                if _has_table(conn, "paper_nav_daily") else [])
    trade_rows = ([dict(r) for r in store.load_trades(conn, account_id=arm, asof=asof)]
                  if _has_table(conn, "paper_trades") else [])

    nav_series = [
        {"date": str(r["date"]),
         "nav": _round4(r["nav"]),
         "cum_return": _round4(r["cum_return"]),
         "cum_cost": _round4(r["cum_cost"]),
         "net_deposits": _round4(r["net_deposits"]),
         "drawdown": _round4(r["drawdown"]),
         "index_300_level": _round4(r["index_300_level"])}
        for r in nav_rows[-NAV_LIMIT:]]

    trade_tail = trade_rows[-TRADE_LIMIT:]
    names = _names(conn, sorted({str(t["code"]) for t in trade_tail}))
    trades = [
        {"trade_id": int(t["trade_id"]),
         "date": str(t["date"]),
         "code": str(t["code"]),
         "name": names.get(str(t["code"])),
         "side": str(t["side"]),
         "fill_price": _round4(t["fill_price"]),
         "qty": int(t["qty"]),
         "fee_total": _round4(t["fee_total"]),
         "slippage_cost": _round4(t["slippage_cost"]),
         "rule_citation": str(t["rule_citation"] or ""),
         "reason": str(t["reason"] or "")}
        for t in trade_tail]

    if nav_rows and nav_rows[-1].get("cum_cost") is not None:
        realized = _round4(nav_rows[-1]["cum_cost"])
    else:
        realized = round(sum(float(t["fee_total"] or 0.0) for t in trade_rows), 4)

    decisions, n_decisions, broken = _decisions_block(conn, arm=arm, asof=asof)
    notes = list(_NOTES)
    if broken:
        notes.append("有决策行的 `payload_json` 解析不出 `decisions` 数组 ⇒ "
                     "该行 `n_orders` 记 0（**不是**「零腿下单」）——"
                     "行号见 `decisions[].decision_id`。")
    return {"arm": arm,
            "nav_series": nav_series,
            "n_nav_days": len(nav_rows),
            "trades": trades,
            "n_trades": len(trade_rows),
            "n_buy": sum(1 for t in trade_rows if str(t["side"]) == "buy"),
            "n_sell": sum(1 for t in trade_rows if str(t["side"]) == "sell"),
            "realized_fees_total": realized,
            "decisions": decisions,
            "n_decisions": n_decisions,
            "notes": notes[:4],
            # P85 / K5：**末尾**追加两个子键（既有子键逐位不变，K8）。
            # 都自带 PIT（`review.load_reviews` / `derive_facts` 的 `asof` 过滤；
            # **严格早于决策日** —— P86 修订，理由见 `review._iter_reviews`），
            # 都尺寸有界，都不含时间戳 / 自增 id。
            "recent_reviews": [
                {"asof": str(r["asof"]), "kind": str(r["kind"]),
                 "context_sha256": str(r["context_sha256"] or ""),
                 "n_items": int(r["n_items"]), "n_lessons": int(r["n_lessons"]),
                 "items": r["items"], "lessons": r["lessons"]}
                for r in review.load_reviews(conn, arm=arm, asof=asof,
                                             limit=REVIEW_LIMIT, strict=True)],
            "facts": review.derive_facts(conn, arm=arm, asof=asof,
                                         strict=True)["facts"]}


__all__: list[str] = ["own_history_block", "NAV_LIMIT", "TRADE_LIMIT",
                      "DECISION_LIMIT", "REVIEW_LIMIT", "FACT_LIMIT"]
