"""模拟盘写入口（P19）：`paper_*` 三表的**唯一**读写通道。

## append-only 的三重防线

1. 本模块**不提供** UPDATE / DELETE 函数（改错只能新增一行冲正）；
2. schema 的 `BEFORE UPDATE/DELETE` 触发器 `RAISE(ABORT)`；
3. `db.connect()` 打开 `recursive_triggers`，使 `INSERT OR REPLACE` 的隐式删行
   也会被触发器拦住（P6 实测：不开这个 PRAGMA，隐式 DELETE 不触发 DELETE 触发器，
   静默覆盖会成功）。

## 幂等单元 = `(account_id, date)`

`nav_exists()` 是**先决判据**：`paper step` 在决定要不要下单**之前**先问
「这个账户今天的净值落库了吗」。顺序不能反 —— ERROR_DIARY #25 记过这个坑：
先做业务层查重再让幂等键兜底，会把**自己刚写下的那一行**当成重复。
这里的幂等键不是业务元组（同一天买两次同样的量可能是真事），
而是**调用方给的日期**：一天一条净值，一天只决策一次。

`paper_trades` 的 `UNIQUE(account_id, date, code, side, qty, rule_citation)` 是
第二道结构性防线：即便上层判据将来被改坏，也不可能在同一天对同一账户重复下同一单。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.paper.rules import Decision

TABLE_ACCOUNTS = "paper_accounts"
TABLE_TRADES = "paper_trades"
TABLE_NAV = "paper_nav_daily"


# ---------- 账户 ----------

def account_exists(conn: sqlite3.Connection, account_id: str) -> bool:
    return conn.execute(
        f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE account_id = ?", (account_id,)
    ).fetchone() is not None


def insert_account(conn: sqlite3.Connection, *, account_id: str, arm: str,
                   etf_target_pct: float | None, start_date: str,
                   initial_cash: float, initial_positions: list[dict],
                   initial_nav: float, params: dict, now: str) -> None:
    conn.execute(
        f"INSERT INTO {TABLE_ACCOUNTS} (account_id, arm, etf_target_pct, start_date,"
        " initial_cash, initial_positions_json, initial_nav, params_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (account_id, arm, etf_target_pct, start_date, initial_cash,
         json.dumps(initial_positions, ensure_ascii=False, sort_keys=True),
         initial_nav, json.dumps(params, ensure_ascii=False, sort_keys=True), now),
    )
    conn.commit()


def load_accounts(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {TABLE_ACCOUNTS} ORDER BY account_id")]


# ---------- 成交 ----------

def insert_trade(conn: sqlite3.Connection, *, account_id: str, date: str,
                 decision: Decision, now: str, commit: bool = True) -> int:
    """写一条模拟成交。只接受 `Decision.is_trade` 的决定（hold 不该走到这里）。

    `commit=False` 供 `engine.step` 把**整个 step** 放进一个事务
    （一次失败不得留下「前 4 个账户已写、第 5 个没写」的半截状态）。
    """
    if not decision.is_trade:
        raise ValueError(f"hold 决定不能写成交：{decision.action!r} / {decision.reason!r}")
    f = decision.fees
    cur = conn.execute(
        f"INSERT INTO {TABLE_TRADES} (account_id, date, code, side, ref_price,"
        " fill_price, qty, commission, stamp_tax, transfer_fee, slippage_cost,"
        " fee_total, asset_class, rule_citation, reason, binding_json, price_source,"
        " price_asof, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, date, decision.code, decision.action, decision.ref_price,
         decision.fill_price, decision.qty,
         f.get("commission", 0.0), f.get("stamp_tax", 0.0),
         f.get("transfer_fee", 0.0), f.get("slippage_cost", 0.0),
         f.get("total", 0.0), decision.asset_class, decision.rule_citation,
         decision.reason,
         json.dumps(list(decision.binding_constraints), ensure_ascii=False),
         decision.price_source or "", decision.price_asof or "", now),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def load_trades(conn: sqlite3.Connection, *, account_id: str | None = None,
                asof: str | None = None) -> list[dict]:
    sql = f"SELECT * FROM {TABLE_TRADES}"
    where, args = [], []
    if account_id is not None:
        where.append("account_id = ?")
        args.append(account_id)
    if asof is not None:
        where.append("date <= ?")
        args.append(asof)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date, trade_id"
    return [dict(r) for r in conn.execute(sql, args)]


def trades_on(conn: sqlite3.Connection, account_id: str, date: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {TABLE_TRADES} WHERE account_id = ? AND date = ?"
        " ORDER BY trade_id", (account_id, date))]


# ---------- 净值 ----------

def nav_exists(conn: sqlite3.Connection, account_id: str, date: str) -> bool:
    return conn.execute(
        f"SELECT 1 FROM {TABLE_NAV} WHERE account_id = ? AND date = ?",
        (account_id, date)).fetchone() is not None


def insert_nav(conn: sqlite3.Connection, *, account_id: str, date: str,
               cash: float, positions: list[dict], market_value: float, nav: float,
               drawdown: float, cum_cost: float, cum_return: float,
               net_deposits: float, index_300_level: float | None,
               index_300_asof: str | None, now: str, commit: bool = True) -> None:
    conn.execute(
        f"INSERT INTO {TABLE_NAV} (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, date, cash,
         json.dumps(positions, ensure_ascii=False, sort_keys=True),
         market_value, nav, drawdown, cum_cost, cum_return, net_deposits,
         index_300_level, index_300_asof, now),
    )
    if commit:
        conn.commit()


def load_nav(conn: sqlite3.Connection, account_id: str, *,
             asof: str | None = None) -> list[dict]:
    sql = f"SELECT * FROM {TABLE_NAV} WHERE account_id = ?"
    args: list = [account_id]
    if asof is not None:
        sql += " AND date <= ?"
        args.append(asof)
    sql += " ORDER BY date"
    return [dict(r) for r in conn.execute(sql, args)]


def latest_nav(conn: sqlite3.Connection, account_id: str, *,
               asof: str | None = None) -> dict | None:
    rows = load_nav(conn, account_id, asof=asof)
    return rows[-1] if rows else None


def latest_nav_date(conn: sqlite3.Connection) -> str | None:
    """全库**最新净值日期**（任一账户有净值即可）。一条都没有 → `None`。"""
    row = conn.execute(f"SELECT MAX(date) AS d FROM {TABLE_NAV}").fetchone()
    return row["d"] if row and row["d"] else None


def nav_date_exists(conn: sqlite3.Connection, date: str) -> bool:
    """该日期是否**已经有**净值行（任一账户）。"""
    return conn.execute(f"SELECT 1 FROM {TABLE_NAV} WHERE date = ? LIMIT 1",
                        (date,)).fetchone() is not None
