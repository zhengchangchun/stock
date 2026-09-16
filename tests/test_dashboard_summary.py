"""P14 / Task 61：看板摘要 —— 稳定 JSON + 口径不许漂。

这个文件盯住两件事：
  1. **接口稳定**：顶层字段集合是页面契约，改了必须同步改文档（否则页面静默空格子）；
  2. **口径不漂**：LIVE 与 REPLAY 不许相加、不许互相顶替；样本不足必须标注。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stocklab.dashboard.summary import (SCHEMA_VERSION, SERVICE, TOP_LEVEL_FIELDS,
                                        build_alarms, build_summary, summary_json)
from stocklab.data.models import Bar
from stocklab.session.review import INSUFFICIENT
from tests.test_predict_service import seed

CODE = "000333"
START = "2026-05-02"
REPLAY_NOW = "2026-09-15T07:04:51+08:00"    # 事后回放 → REPLAY
LEDGER_NOW = "2026-09-15T14:23:31+08:00"


def live_now(asof: str) -> str:
    """`asof` 当天收盘后入库 → 按判据属 LIVE。"""
    return f"{asof}T15:05:00+08:00"


def _bars(n=124, *, code=CODE):
    d0 = date.fromisoformat(START)
    return [Bar(code=code, date=(d0 + timedelta(days=i)).isoformat(),
                open=10.0 * (1 + 0.002 * i), high=10.2 * (1 + 0.002 * i),
                low=9.8 * (1 + 0.002 * i), close=10.0 * (1 + 0.002 * i),
                volume=1000, amount=None, turnover=None, source="test")
            for i in range(n)]


def _seed_account(conn):
    """用户真实账本（与 ADR-006 同源）：20,000 本金 + 100 股 @86.80 费 5.09。"""
    conn.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
                 " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (LEDGER_NOW,))
    conn.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
                 " created_at) VALUES ('2026-09-14',?,'buy',86.80,100,5.09,'首笔',?)",
                 (CODE, LEDGER_NOW))


@pytest.fixture
def conn(tmp_path):
    c = seed(tmp_path / "dash.db", {CODE: _bars()})
    _seed_account(c)
    yield c
    c.close()


def _predict(conn, asof: str, *, now: str, origin: str) -> str:
    from stocklab.predict.service import build_predictions
    from stocklab.predict.store import insert_prediction

    rep = build_predictions(conn, asof, [CODE])
    for p in rep["predictions"]:
        insert_prediction(conn, p, now=now, origin=origin)
    return rep["target_date"]


def _verify(conn, target: str, *, now: str) -> None:
    from stocklab.verify.service import verify_target

    verify_target(conn, target, now=now)


# ---------- 接口契约 ----------

def test_top_level_fields_are_pinned(conn):
    """顶层字段集合 = 页面消费的契约。**改这里必须同步改 docs/architecture/dashboard-json.md**。"""
    s = build_summary(conn, "2026-08-31")
    assert set(s) == set(TOP_LEVEL_FIELDS)
    assert s["schema_version"] == SCHEMA_VERSION
    assert s["service"] == SERVICE


def test_summary_is_deterministic(conn):
    """同一份库 + 同一 asof → 逐字节相同。

    不变量：摘要里**不含**挂钟时间（`built_at` 由页面层加）。否则
    `/api/summary` 每次请求都在变，既钉不住也 diff 不了。
    """
    a = summary_json(build_summary(conn, "2026-08-31"))
    b = summary_json(build_summary(conn, "2026-08-31"))
    assert a == b
    assert "built_at" not in json.loads(a)


def test_summary_has_no_wall_clock_field(conn):
    s = build_summary(conn, "2026-08-31")
    flat = json.dumps(s, ensure_ascii=False)
    for bad in ("built_at", "generated_at", "server_time", "now"):
        assert f'"{bad}"' not in flat


# ---------- 口径：LIVE / REPLAY 不许混 ----------

def test_live_bucket_is_null_not_zero_when_there_are_no_live_rows(conn):
    """没有任何实盘行时，`live` 必须是 `null` —— **不是**一个 0% 的桶。

    `null` = 「没有样本」；`0` = 「测出来是 0」。两者在页面上长得一样、
    含义完全相反（这是本项目最贵的一类错，见 P11 复盘口径）。
    """
    t = _predict(conn, "2026-08-30", now=REPLAY_NOW, origin="replay")
    _verify(conn, t, now=REPLAY_NOW)
    s = build_summary(conn, "2026-09-01")
    assert s["accuracy"]["live"] is None
    assert s["accuracy"]["replay"]["n_rows"] == 1
    assert s["accuracy"]["provenance"]["live"]["n_rows"] == 0


def test_live_and_replay_are_counted_separately(conn):
    t1 = _predict(conn, "2026-08-30", now=live_now("2026-08-30"), origin="live")
    _verify(conn, t1, now=live_now("2026-08-30"))
    t2 = _predict(conn, "2026-08-31", now=REPLAY_NOW, origin="replay")
    _verify(conn, t2, now=REPLAY_NOW)

    s = build_summary(conn, "2026-09-01")
    acc = s["accuracy"]
    assert acc["live"]["n_rows"] == 1
    assert acc["replay"]["n_rows"] == 1
    # 相加是**错的**：两个桶口径不同，不许合并成一个「总准确率」。
    assert "total" not in acc and "combined" not in acc


def test_sample_gate_marks_insufficient_below_120_days(conn):
    t = _predict(conn, "2026-08-30", now=REPLAY_NOW, origin="replay")
    _verify(conn, t, now=REPLAY_NOW)
    b = build_summary(conn, "2026-09-01")["accuracy"]["replay"]
    assert b["sample_gate"]["meets"] is False
    assert b["sample_gate"]["label"] == INSUFFICIENT
    assert b["effective_n_days"] < 120


# ---------- 组合口径 ----------

def test_portfolio_is_the_existing_view(conn):
    """摘要里的组合数字必须**就是** `build_portfolio` 的输出（不许二次计算）。"""
    from stocklab.portfolio.view import build_portfolio

    asof = "2026-09-14"
    s = build_summary(conn, asof)
    assert s["portfolio"] == build_portfolio(conn, asof)


def test_missing_price_is_alarmed_not_silently_zero(conn):
    """标的没有 `<= asof` 的行情 → 计入 `missing_price_codes` 并报警。

    600690 在册但**没有 K 线**（fixture 只种了 000333）—— 这正是 ADR-006 D-04
    说的那种情形：拿不到现价就不许给它算市值，更不许拿成本价冒充。
    """
    conn.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
                 " created_at) VALUES (?,'600690','buy',20.0,100,5.0,NULL,?)",
                 ("2026-09-14", LEDGER_NOW))
    s = build_summary(conn, "2026-09-14")
    assert s["portfolio"]["missing_price_codes"] == ["600690"]
    missing = [p for p in s["portfolio"]["positions"] if p["code"] == "600690"][0]
    assert missing["market_value"] is None          # 不是 0，也不是成本价
    assert missing["weight_of_total_assets"] is None
    assert any("无可用现价" in a for a in s["alarms"])


def test_stale_bars_are_alarmed(conn):
    """asof 晚于 bars 最新日期 → 「行情落后」必须出现在 alarms 里。"""
    s = build_summary(conn, "2026-09-14")
    assert s["freshness"]["bars_latest_date"] < "2026-09-14"
    assert any("行情落后" in a for a in s["alarms"])


def test_alarms_are_empty_when_nothing_is_wrong():
    """**告警列表可以为空** —— 一个永远非空的告警列表等于没有告警。"""
    view = {"warnings": []}
    fresh = {"bars_latest_date": "2026-09-14", "codes_without_bars": [],
             "snapshots": {"trade_date": "2026-09-14", "n_rows": 3}}
    assert build_alarms(view, fresh, "2026-09-14") == []


def test_risk_block_defaults_to_null(conn):
    """未接入风险面板 → `risk` 是 `null`（页面必须与「风险为零」区分渲染）。"""
    s = build_summary(conn, "2026-08-31")
    assert s["risk"] is None


def test_risk_block_is_passed_through_verbatim(conn):
    block = {"verdict": "NO_BET", "verdict_label": "不下注"}
    s = build_summary(conn, "2026-08-31", risk_block=block)
    assert s["risk"] == block
