"""P11：收盘回填 —— 「历史 NULL 保持 NULL」这件事的**逐条**证明。

用户明确要求：历史补不了就从今天起积累，**任何表都不做滚动清理**，
历史 NULL 不许填 0 / 插值 / 估算。本文件把这条拆成可执行的断言。
"""

from __future__ import annotations

import json

import pytest

from stocklab.data.models import Bar
from stocklab.session import close as close_mod
from stocklab.session.store import insert_snapshot
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T15:30:00+08:00"
CODE = "000333"
TODAY = "2026-09-15"
YESTERDAY = "2026-09-14"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _bar(date: str, code: str = CODE, *, amount=None, turnover=None,
         adj_mode: str = "none", close: float = 10.0) -> Bar:
    return Bar(code=code, date=date, open=close, high=close, low=close, close=close,
               volume=1000, amount=amount, turnover=turnover, source="test",
               adj_mode=adj_mode)


def _snap(conn, *, ts: str, code: str = CODE, amount=925_191_406.0,
          turnover=0.15, trade_date: str = TODAY):
    return insert_snapshot(conn, {
        "code": code, "trade_date": trade_date, "ts": ts, "price": 87.56,
        "pre_close": 86.8, "open": 86.7, "high": 88.1, "low": 86.44,
        "volume": 10_558_300, "amount": amount, "turnover": turnover,
        "source": "tencent"}, now=NOW)[0]


def _amount_of(conn, date: str, code: str = CODE):
    row = conn.execute("SELECT amount, turnover FROM bars_daily WHERE code=? AND date=?",
                       (code, date)).fetchone()
    return None if row is None else (row["amount"], row["turnover"])


# ---------- 基本行为 ----------

def test_fills_amount_and_turnover_for_the_day(conn):
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 1
    assert out["filled"] == [{"code": CODE, "amount": 925_191_406.0,
                              "turnover": 0.15, "snapshot_id": 1,
                              "ts": "20260915150003"}]
    assert _amount_of(conn, TODAY) == (925_191_406.0, 0.15)


def test_latest_snapshot_of_the_day_wins(conn):
    """`amount` 是**当日累计**量：必须用最后一条，否则会写成「半天成交额」。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915100000", amount=100.0, turnover=0.01)
    _snap(conn, ts="20260915150003", amount=999.0, turnover=0.09)
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert _amount_of(conn, TODAY) == (999.0, 0.09)


def test_is_idempotent(conn):
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    assert close_mod.backfill_close_amounts(conn, TODAY, now=NOW)["updated"] == 1
    again = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert again["updated"] == 0
    assert again["skipped_already_filled"] == [CODE]
    assert _amount_of(conn, TODAY) == (925_191_406.0, 0.15)


# ---------- 红线：历史 NULL 一行不动 ----------

def test_history_stays_null(conn):
    """回填只作用于**指定的那一天**；其余日期的 NULL 逐行保持 NULL。"""
    repo.insert_bars(conn, [_bar(YESTERDAY), _bar("2026-09-11"), _bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)

    nulls = conn.execute(
        "SELECT date FROM bars_daily WHERE amount IS NULL ORDER BY date").fetchall()
    assert [r["date"] for r in nulls] == ["2026-09-11", YESTERDAY]
    assert conn.execute("SELECT COUNT(amount) FROM bars_daily").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 3


def test_never_fills_a_date_that_has_no_snapshot(conn):
    """给 09-14 补跑：那天没有快照 → 什么都不做，且**不报 OK 假象**。"""
    repo.insert_bars(conn, [_bar(YESTERDAY)], now=NOW)
    out = close_mod.backfill_close_amounts(conn, YESTERDAY, now=NOW)
    assert out["updated"] == 0
    assert out["reason"] == "no_snapshots"
    assert _amount_of(conn, YESTERDAY) == (None, None)


def test_existing_values_are_not_overwritten(conn):
    """`COALESCE`：已有值的行原样不动（回填与源站修订**互不覆盖**）。"""
    repo.insert_bars(conn, [_bar(TODAY, amount=1.0, turnover=0.5)], now=NOW)
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 0 and out["skipped_already_filled"] == [CODE]
    assert _amount_of(conn, TODAY) == (1.0, 0.5)


def test_missing_value_is_not_fabricated(conn):
    """源站没给 amount/turnover → 保持 NULL，进 `skipped_no_value` 留痕。**不填 0**。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003", amount=None, turnover=None)
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 0 and out["skipped_no_value"] == [CODE]
    assert _amount_of(conn, TODAY) == (None, None)


def test_missing_bar_row_is_reported_not_papered_over(conn):
    """当日 bar 还没采到 → 显式留痕（含 system_events），**不**去造一根 bar 出来。"""
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 0 and out["skipped_missing_bar"] == [CODE]
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0
    events = conn.execute(
        "SELECT level, message FROM system_events WHERE module='session'").fetchall()
    assert len(events) == 1
    assert events[0]["level"] == "warn" and "无当日行" in events[0]["message"]


def test_refuses_to_touch_a_non_none_adj_mode_row(conn):
    """铁律①：不复权是 `bars_daily` 的唯一合法口径，回填不准绕过这把闸。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    conn.execute("UPDATE bars_daily SET adj_mode='qfq' WHERE code=? AND date=?",
                 (CODE, TODAY))
    conn.commit()
    _snap(conn, ts="20260915150003")
    with pytest.raises(ValueError, match="adj_mode"):
        close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert _amount_of(conn, TODAY) == (None, None)      # 拒绝了就一行没写


def test_only_the_codes_with_snapshots_are_touched(conn):
    repo.insert_bars(conn, [_bar(TODAY), _bar(TODAY, code="600690")], now=NOW)
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["codes_with_snapshots"] == [CODE]
    assert _amount_of(conn, TODAY, "600690") == (None, None)


def test_fill_is_recorded_as_an_event(conn):
    """回填是**改写既有行**（不像快照是 append-only），所以必须留下可查的痕迹。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    row = conn.execute("SELECT level, message, context_json FROM system_events"
                       " WHERE module='session'").fetchone()
    assert row["level"] == "info" and "回填" in row["message"]
    assert "925191406" in row["context_json"]


def test_repository_reinsert_does_not_wipe_a_filled_value(conn):
    """**回归**：`ingest bars` 重跑（窗口含昨天）不许把刚回填的值抹回 NULL。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)      # 源站日K 里没有 amount 字段
    assert _amount_of(conn, TODAY) == (925_191_406.0, 0.15)


# ---------- P22：快照必须取自收盘后，盘中快照不许当全天值 ----------

def test_stale_intraday_snapshot_is_not_used(conn):
    """`amount` 是**当日累计**量：若当天只有盘中快照（无 15:00 后），回填必须跳过。

    实测背景（2026-09-16）：15:05 的收盘 tick 排队未执行，当日快照只有 09:35 与 14:30，
    旧实现会静默拿 14:30 那条填 amount/turnover → 成交额系统性低估且**无任何告警**。
    """
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915093500", amount=100.0, turnover=0.01)
    _snap(conn, ts="20260915143000", amount=999.0, turnover=0.09)
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 0
    assert out["skipped_stale_snapshot"] == [CODE]
    assert out["reason"] == "stale_snapshot"
    assert _amount_of(conn, TODAY) == (None, None)      # 一行没写


def test_stale_snapshot_leaves_a_warn_event(conn):
    """跳过必须**看得见**：`system_events` 留一条 warn，含 code/trade_date/ts/job。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915093500", amount=100.0, turnover=0.01)
    _snap(conn, ts="20260915143000", amount=999.0, turnover=0.09)
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    row = conn.execute("SELECT level, message, context_json FROM system_events"
                       " WHERE module='session'").fetchone()
    assert row is not None, "stale 跳过必须留痕，不能静默"
    assert row["level"] == "warn"
    assert "143000" in row["message"]
    ctx = json.loads(row["context_json"])
    assert ctx["code"] == CODE
    assert ctx["trade_date"] == TODAY
    assert ctx["ts"] == "20260915143000"                # 用**实际那条**最新快照的 ts
    assert ctx["job"] == "session_backfill_close"


def test_snapshot_at_exactly_the_close_minute_is_accepted(conn):
    """边界：15:00:00 整算收盘后（判据是 `>= 15:00:00`，不是 `> 15:00:00`）。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150000", amount=999.0, turnover=0.09)
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["skipped_stale_snapshot"] == []
    assert out["updated"] == 1
    assert _amount_of(conn, TODAY) == (999.0, 0.09)


def test_fresh_snapshot_after_close_is_filled(conn):
    """15:05 的收盘快照 → 正常回填（这条是上一组的正向对照）。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150500", amount=925_191_406.0, turnover=0.15)
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 1
    assert out["skipped_stale_snapshot"] == []
    assert "reason" not in out                            # 做成了事就不该带 reason
    assert _amount_of(conn, TODAY) == (925_191_406.0, 0.15)


def test_stale_code_is_skipped_while_fresh_code_keeps_its_own_ts(conn):
    """逐标的判：同一交易日两个标的的快照时刻可以不同，互不牵连。"""
    repo.insert_bars(conn, [_bar(TODAY), _bar(TODAY, code="600690")], now=NOW)
    _snap(conn, ts="20260915143000", amount=100.0, turnover=0.01)
    _snap(conn, ts="20260915150500", code="600690", amount=200.0, turnover=0.02)
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["skipped_stale_snapshot"] == [CODE]
    assert out["updated"] == 1
    assert "reason" not in out                            # 有标的正经补上了，不是「什么都没做」
    assert _amount_of(conn, TODAY) == (None, None)
    assert _amount_of(conn, TODAY, "600690") == (200.0, 0.02)


def test_unparseable_ts_is_treated_as_not_closed(conn):
    """`ts` 解不出时刻时**不许当成已收盘**（fail-closed）—— 拿不准就不写。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915", amount=999.0, turnover=0.09)
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["skipped_stale_snapshot"] == [CODE]
    assert _amount_of(conn, TODAY) == (None, None)


def test_stale_skip_is_idempotent(conn):
    """跳过也是幂等的：连跑两次结果一致，不因第一次的 warn 而改变行为。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915143000", amount=999.0, turnover=0.09)
    first = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    second = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert first["skipped_stale_snapshot"] == second["skipped_stale_snapshot"] == [CODE]
    assert second["updated"] == 0
    assert _amount_of(conn, TODAY) == (None, None)


def test_no_rolling_cleanup_anywhere_in_the_module():
    """取消「只保留近 30 天」：任何表都不许有滚动清理路径。"""
    import inspect

    src = inspect.getsource(close_mod).upper()
    assert "DELETE" not in src               # 没有任何删除路径
    assert "LIMIT" not in src                # 也没有「只取最近 N 天」的写法


# ---------- 当日 K 线「定型」判据（P46 §T3） ----------
#
# 这是 `predict run --asof 今天` 的闸门判据：**当天的 K 线是不是收盘后采到的终值**。
# 唯一可信的信号是 `bars_daily.fetched_at`（这一行最后一次从源站采到的时刻，
# `repo.insert_bars` 的 `ON CONFLICT DO UPDATE` 会刷新它），**不是**墙上的钟。
#
# 为什么不用另外两个更顺手的判据（都有实测反例，见 `docs/errors/ERROR_DIARY.md` #60）：
# - 「`session backfill-close` 当天跑过」：2026-09-22 patrol 15:05 跑过它且 exit 0，
#   而预测照样基于半截 bar —— 闸门恒放行；
# - 「当天 bar 的 amount 非 NULL」：`session tick` **自己就会回填**（`tick.py:229`），
#   15:05:19 已把 17 只填好 —— 15:05:22 的 predict run 看到 amount 非 NULL，照样放行。
# 两者度量的是「快照/回填跑没跑」，而定型问的是「**那根 K 线的收盘价**是不是终值」：
# `backfill-close` 只补 amount/turnover，**一行都不碰 close**。

def _fetched_at_of(conn, date: str, code: str = CODE):
    return conn.execute("SELECT fetched_at FROM bars_daily WHERE code=? AND date=?",
                        (code, date)).fetchone()[0]


def test_bar_fetched_after_the_close_is_final(conn):
    repo.insert_bars(conn, [_bar(TODAY)], now="2026-09-15T15:30:03+08:00")
    ok, why = close_mod.bars_finalized_on(conn, TODAY)
    assert ok is True and why == ""


def test_bar_fetched_exactly_at_the_close_minute_is_final(conn):
    """>= 15:00:00 即已收盘（与 `is_closed_snapshot` 同一把尺子）。"""
    repo.insert_bars(conn, [_bar(TODAY)], now="2026-09-15T15:00:00+08:00")
    assert close_mod.bars_finalized_on(conn, TODAY)[0] is True


def test_bar_fetched_before_the_close_is_not_final(conn):
    """事故的形状：12:06 采到的那根 K 线，收盘价还是盘中值。"""
    repo.insert_bars(conn, [_bar(TODAY)], now="2026-09-15T12:06:00+08:00")
    ok, why = close_mod.bars_finalized_on(conn, TODAY)
    assert ok is False
    assert "12:06" in why and "15:00" in why


def test_no_bar_row_for_the_day_is_not_final(conn):
    """fail-closed：当天一行 bar 都没有 ⇒ 证不出它定型 ⇒ 判**未**定型。

    这一条不是吹毛求疵：没有当日 bar 时 `build_predictions` 会退到「最后一根是昨天」
    的那条路（`NoBarOnAsof` 分支之外），算出来的「今天 asof 预测」用的是旧价。
    """
    ok, why = close_mod.bars_finalized_on(conn, TODAY)
    assert ok is False and "一行" in why


def test_one_stale_row_makes_the_whole_day_unfinal(conn):
    """同一天只要**有一行**早于收盘，整天都不算定型 —— 那行可能正是被预测的标的。"""
    repo.insert_bars(conn, [_bar(TODAY), _bar(TODAY, "600690")],
                     now="2026-09-15T15:30:03+08:00")
    assert close_mod.bars_finalized_on(conn, TODAY)[0] is True
    conn.execute("UPDATE bars_daily SET fetched_at='2026-09-15T12:06:00+08:00'"
                 " WHERE code='600690'")
    conn.commit()
    ok, why = close_mod.bars_finalized_on(conn, TODAY)
    assert ok is False and "600690" in why


def test_unparseable_fetched_at_is_not_final(conn):
    """`fetched_at` 读不出时刻时**不许当成已定型**（fail-closed，同 `_ts_time`）。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    conn.execute("UPDATE bars_daily SET fetched_at='not-a-timestamp' WHERE date=?",
                 (TODAY,))
    conn.commit()
    ok, why = close_mod.bars_finalized_on(conn, TODAY)
    assert ok is False and "not-a-timestamp" in why


def test_a_historical_day_is_judged_by_its_own_fetched_at(conn):
    """判据只看**该日自己**的 `fetched_at`，不看今天是几号 —— 历史日的复算不受影响。"""
    repo.insert_bars(conn, [_bar(YESTERDAY)], now="2026-09-14T15:30:05+08:00")
    assert close_mod.bars_finalized_on(conn, YESTERDAY)[0] is True
