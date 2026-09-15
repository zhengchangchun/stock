"""实盘账本写入口（P12 / Task 55）：成交录入、现金流录入、冲正。

## 为什么这里没有一个 `UPDATE`

`real_trades` / `cash_flows` 是 append-only（触发器钉住，见 schema.sql）。
账本是「我真做过什么」的唯一记录，事后改写会让所有归因失效。
**改错的唯一合法路径是冲正**：追加一笔反向记录 + `note` 说明原因，
于是历史仍然完整可读，而不是被抹掉。`reverse_trade()` 就是这条路径的实现。

## 重复录入：为什么**不用**唯一约束

真实成交**可能同价同量同费** —— 同一天分两笔各买 100 股 @86.80、佣金都是 5.09
是完全正常的真单。对 `(date, code, side, price, qty, fee)` 加 UNIQUE
等于让系统**静默吃掉真单**，而且吞的时候一声不响。

所以用两层，都**不阻止真单**：

1. `idem_key`（推荐）：键落在 `ledger_idem` 表。同一个 key 第二次进来 →
   `state="identical"`，不写第二行，退出码照常成功。解决「命令重试 / 补跑」。
2. 无 key 时元组完全相同 → **硬拒**，并提示要么给 `--idempotency-key`、
   要么给 `--allow-duplicate` 确认这是另一笔真单。解决「手滑敲两遍」。

只有调用方知道自己是不是在重试 —— 业务元组本身区分不了这两件事，
所以键必须由调用方给，不能由数据推。
"""

from __future__ import annotations

import sqlite3
from datetime import date as _date
from datetime import datetime
from typing import Mapping

from stocklab.calendar.trading_calendar import Calendar
from stocklab.portfolio.positions import LedgerError, qty_held_before

__all__ = [
    "LedgerError", "TradeValidationError", "CashFlowValidationError",
    "DuplicateTradeError", "CASH_KINDS", "INFLOW_KINDS", "OUTFLOW_KINDS",
    "latest_closed_trading_day", "validate_trade", "record_trade",
    "record_cash_flow", "reverse_trade",
]

#: 收盘时刻（本地时间）。≥ 它，当日才算「已收盘」。
CLOSE_HHMM = (15, 0)

#: 现金流种类。`amount` 的符号必须与 kind 一致（见 `_validate_amount_sign`）。
CASH_KINDS = ("deposit", "withdraw", "dividend", "fee", "tax", "other")
INFLOW_KINDS = ("deposit", "dividend")
OUTFLOW_KINDS = ("withdraw", "fee", "tax")
SIDES = ("buy", "sell")


class TradeValidationError(LedgerError):
    """成交校验不过。"""


class CashFlowValidationError(LedgerError):
    """现金流校验不过。"""


class DuplicateTradeError(LedgerError):
    """疑似重复录入（无幂等键，且元组完全相同）。"""


# ---------- 时间 ----------

def _parse_now(now: str) -> datetime:
    try:
        return datetime.fromisoformat(now)
    except ValueError as exc:
        raise LedgerError(f"now 不是 ISO8601 时间：{now!r}") from exc


def latest_closed_trading_day(calendar: Calendar, now: datetime) -> str:
    """**已收盘**的最近交易日 —— 录单的日期上界。

    判据：今天在日历里且本地时刻 ≥ 15:00 → 今天；否则上一交易日。
    盘中（< 15:00）时今天是「还没走完的一天」，它的收盘价还不存在，
    更不可能有成交 —— 所以此时上界是上一交易日。

    日历为空抛 `LedgerError`。**不猜**：没有日历就没有「已收盘」这个概念，
    猜出来的日期会变成一条看不见的假约束。
    """
    if not calendar.all_dates:
        raise LedgerError("日历为空（trading_calendar 无数据）；先跑 `ingest index`")
    today = now.date().isoformat()
    hhmm = (now.hour, now.minute)
    if calendar.is_open(today) and hhmm >= CLOSE_HHMM:
        return today
    try:
        return calendar.prev_trading_day(today)
    except IndexError as exc:
        raise LedgerError(
            f"日历里没有早于 {today} 的交易日，无法确定最近已收盘交易日") from exc


def _load_calendar(conn: sqlite3.Connection) -> Calendar:
    try:
        return Calendar.load(conn)
    except ValueError as exc:
        raise LedgerError("日历为空（trading_calendar 无数据）；先跑 `ingest index`") from exc


def _check_date(conn: sqlite3.Connection, date: str, now: str, exc_cls) -> None:
    """日期必须：格式合法 → 日历内有 → 不晚于最近已收盘交易日。"""
    try:
        _date.fromisoformat(date)
    except (ValueError, TypeError) as e:
        raise exc_cls(f"date 格式必须是 YYYY-MM-DD，收到 {date!r}") from e
    cal = _load_calendar(conn)
    # 「未来」先判：它的判据与日历覆盖范围无关，而日历往往还没滚到今天
    # （本仓实测 trading_calendar 只到 2026-09-14）。先判 is_open 的话，
    # 今天录今天的成交会报「不在日历里」，把人引向错误的排查方向。
    upper = latest_closed_trading_day(cal, _parse_now(now))
    if date > upper:
        raise exc_cls(
            f"date {date} 晚于最近已收盘交易日 {upper} —— 禁录未来成交"
            f"（今天的成交要等 15:00 收盘后才存在）")
    if not cal.is_open(date):
        raise exc_cls(f"date {date} 不在 trading_calendar 里（非交易日或日历未覆盖）")


# ---------- 幂等键 ----------

def _find_idem(conn: sqlite3.Connection, key: str, scope: str) -> int | None:
    row = conn.execute(
        "SELECT row_id FROM ledger_idem WHERE idem_key = ? AND scope = ?",
        (key, scope)).fetchone()
    return int(row["row_id"]) if row else None


def _claim_idem(conn: sqlite3.Connection, key: str, scope: str, row_id: int,
                now: str) -> None:
    conn.execute(
        "INSERT INTO ledger_idem (idem_key, scope, row_id, created_at)"
        " VALUES (?,?,?,?)", (key, scope, row_id, now))


# ---------- 成交 ----------

def validate_trade(conn: sqlite3.Connection, *, date: str, code: str, side: str,
                   price: float, qty: int, fee: float, now: str,
                   require_lot: bool = True) -> None:
    """写入口的硬校验。任一条不过就抛，**不静默降级**。

    `require_lot=False` 供冲正使用：冲正一笔零股卖出 = 买回零股，
    不能被「买入必须整手」挡住（那会让错误永远改不回来）。
    """
    if side not in SIDES:
        raise TradeValidationError(f"side 必须是 {SIDES} 之一，收到 {side!r}")
    if not isinstance(qty, int) or isinstance(qty, bool):
        raise TradeValidationError(f"qty 必须是整数股数，收到 {qty!r}")
    if qty <= 0:
        raise TradeValidationError(f"qty 必须 > 0，收到 {qty}")
    if require_lot and side == "buy" and qty % 100 != 0:
        raise TradeValidationError(
            f"买入必须 100 股整数倍（整手），收到 {qty} 股；"
            "卖出才允许零股残量")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        raise TradeValidationError(f"price 必须是数字，收到 {price!r}")
    if float(price) <= 0:
        raise TradeValidationError(f"price 必须 > 0，收到 {price}")
    if not isinstance(fee, (int, float)) or isinstance(fee, bool):
        raise TradeValidationError(f"fee 必须是数字，收到 {fee!r}")
    if float(fee) < 0:
        raise TradeValidationError(f"fee 必须 ≥ 0，收到 {fee}")

    row = conn.execute("SELECT 1 FROM instruments WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise TradeValidationError(
            f"code {code} 未在 instruments 登记；先跑 `ingest bars` 或手工登记")

    _check_date(conn, date, now, TradeValidationError)

    if side == "sell":
        held = qty_held_before(_all_trades(conn), code, date)
        if qty > held:
            raise TradeValidationError(
                f"卖出 {qty} 股超过当时持仓 {held} 股"
                f"（按 (date, trade_id) 逐笔回放到 {date}）")


def _all_trades(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT trade_id, date, code, side, price, qty, fee FROM real_trades")]


def _find_identical_trade(conn: sqlite3.Connection, *, date: str, code: str,
                          side: str, price: float, qty: int, fee: float) -> int | None:
    row = conn.execute(
        "SELECT trade_id FROM real_trades WHERE date=? AND code=? AND side=?"
        " AND price=? AND qty=? AND fee=?",
        (date, code, side, float(price), qty, float(fee))).fetchone()
    return int(row["trade_id"]) if row else None


def record_trade(conn: sqlite3.Connection, *, date: str, code: str, side: str,
                 price: float, qty: int, fee: float = 0.0, note: str | None = None,
                 now: str, idem_key: str | None = None,
                 allow_duplicate: bool = False,
                 _require_lot: bool = True) -> dict:
    """录入一笔成交。返回 `{state, trade_id, ...}`，`state ∈ {inserted, identical}`。

    `identical` = 同 `idem_key` 已录过 → **幂等命中**，不写第二行。
    无 key 且元组完全相同 → 抛 `DuplicateTradeError`（除非 `allow_duplicate=True`）。
    """
    validate_trade(conn, date=date, code=code, side=side, price=price, qty=qty,
                   fee=fee, now=now, require_lot=_require_lot)

    if idem_key:
        hit = _find_idem(conn, idem_key, "trade")
        if hit is not None:
            return {"state": "identical", "trade_id": hit, "idem_key": idem_key,
                    "date": date, "code": code, "side": side, "qty": qty}

    if not idem_key and not allow_duplicate:
        same = _find_identical_trade(conn, date=date, code=code, side=side,
                                     price=price, qty=qty, fee=fee)
        if same is not None:
            raise DuplicateTradeError(
                f"{date} {code} {side} {qty}@{price} 费{fee} 与已存在的 "
                f"trade_id={same} 完全相同。可能是手滑敲了两遍 —— 若确实是另一笔真单，"
                "请加 `--allow-duplicate` 确认；若是重试，请改用 `--idempotency-key`。")

    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO real_trades (date, code, side, price, qty, fee, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (date, code, side, float(price), qty, float(fee), note, now))
        tid = int(cur.lastrowid)
        if idem_key:
            _claim_idem(conn, idem_key, "trade", tid, now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return {"state": "inserted", "trade_id": tid, "idem_key": idem_key,
            "date": date, "code": code, "side": side, "qty": qty}


def reverse_trade(conn: sqlite3.Connection, trade_id: int, *, reason: str,
                  now: str, idem_key: str | None = None) -> dict:
    """冲正：追加一笔**反向**成交，原行不动。

    `reason` 必填 —— 冲正而不写原因，等于把「为什么账上多了一笔反向单」
    变成无解的谜题，事后没人能判断这是纠错还是又一次手滑。
    """
    if not reason or not str(reason).strip():
        raise LedgerError("冲正必须给 reason（写进 note，供事后追溯）")
    row = conn.execute("SELECT * FROM real_trades WHERE trade_id = ?",
                       (trade_id,)).fetchone()
    if row is None:
        raise LedgerError(f"trade_id={trade_id} 不存在，无法冲正")
    opposite = "sell" if row["side"] == "buy" else "buy"
    note = f"冲正 #{trade_id}（原 {row['side']} {row['qty']}@{row['price']}）：{reason}"
    return record_trade(
        conn, date=row["date"], code=row["code"], side=opposite,
        price=row["price"], qty=row["qty"], fee=row["fee"], note=note,
        now=now, idem_key=idem_key, allow_duplicate=True,
        # 冲正一笔零股卖出 = 买回零股；不能被整手规则挡住
        _require_lot=False,
    )


# ---------- 现金流 ----------

def _validate_amount_sign(kind: str, amount: float) -> None:
    if kind in INFLOW_KINDS and amount <= 0:
        raise CashFlowValidationError(
            f"{kind} 必须 > 0（流入；amount 有符号，正 = 流入组合）；收到 {amount}")
    if kind in OUTFLOW_KINDS and amount >= 0:
        raise CashFlowValidationError(
            f"{kind} 必须 < 0（流出；amount 有符号，负 = 流出组合）；收到 {amount}")
    if amount == 0:
        raise CashFlowValidationError("amount 不能为 0")


def record_cash_flow(conn: sqlite3.Connection, *, date: str, kind: str,
                     amount: float, note: str | None = None, now: str,
                     idem_key: str | None = None,
                     allow_duplicate: bool = False) -> dict:
    """录入一笔本金/现金流。`amount` **有符号**：正 = 流入组合，负 = 流出。

    符号与 `kind` 必须一致 —— 存绝对值再靠 kind 推方向，迟早会在某处被加错符号，
    而且加错了看不出来。
    """
    if kind not in CASH_KINDS:
        raise CashFlowValidationError(
            f"kind 必须是 {CASH_KINDS} 之一，收到 {kind!r}")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise CashFlowValidationError(f"amount 必须是数字，收到 {amount!r}")
    amount = float(amount)
    _validate_amount_sign(kind, amount)
    _check_date(conn, date, now, CashFlowValidationError)

    if idem_key:
        hit = _find_idem(conn, idem_key, "cash")
        if hit is not None:
            return {"state": "identical", "flow_id": hit, "idem_key": idem_key,
                    "date": date, "kind": kind, "amount": amount}

    if not idem_key and not allow_duplicate:
        same = conn.execute(
            "SELECT flow_id FROM cash_flows WHERE date=? AND kind=? AND amount=?"
            " AND IFNULL(note,'') = IFNULL(?,'')",
            (date, kind, amount, note)).fetchone()
        if same is not None:
            raise CashFlowValidationError(
                f"{date} {kind} {amount} 与已存在的 flow_id={same['flow_id']} 完全相同。"
                "若确实是另一笔，请加 `--allow-duplicate`；若是重试，请用 "
                "`--idempotency-key`。")

    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO cash_flows (date, kind, amount, note, created_at)"
            " VALUES (?,?,?,?,?)", (date, kind, amount, note, now))
        fid = int(cur.lastrowid)
        if idem_key:
            _claim_idem(conn, idem_key, "cash", fid, now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return {"state": "inserted", "flow_id": fid, "idem_key": idem_key,
            "date": date, "kind": kind, "amount": amount}


def cash_summary(conn: sqlite3.Connection) -> dict:
    """现金三分解：本金净投入、成交净额、其他（分红/税费）。"""
    rows = [dict(r) for r in conn.execute("SELECT kind, amount FROM cash_flows")]
    deposits = sum(r["amount"] for r in rows if r["kind"] == "deposit")
    withdrawals = sum(r["amount"] for r in rows if r["kind"] == "withdraw")
    others = sum(r["amount"] for r in rows
                 if r["kind"] not in ("deposit", "withdraw"))
    trade_cash = 0.0
    for t in _all_trades(conn):
        gross = float(t["price"]) * int(t["qty"])
        fee = float(t["fee"])
        # 买入：付出去 (价×量 + 费)；卖出：收回来 (价×量 − 费)
        trade_cash += -(gross + fee) if t["side"] == "buy" else gross - fee
    return {
        "net_deposits": deposits + withdrawals,
        "trade_cash": trade_cash,
        "other_cash": others,
        "cash": deposits + withdrawals + trade_cash + others,
    }
