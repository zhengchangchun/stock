"""P11：复盘报告 —— **口径不许漂**（本文件的第一性目标）。

库里全部验证行都来自 PIT 历史回放。回放是合法的样本外评估，但它**不是实盘记录**。
报告必须把 LIVE / REPLAY 分列，并在 LIVE 为空时明说「实盘累计 0 行」，
而**不是**把回放的 38.14% / Brier 0.6581 写成「实盘表现」。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stocklab.data.models import Bar
from stocklab.predict.service import build_predictions
from stocklab.predict.store import insert_prediction
from stocklab.session.review import (INSUFFICIENT, PROVENANCE_RULE, build_review,
                                     classify, render_markdown, rolling_accuracy)
from tests.test_predict_service import seed

CODE = "000333"
START = "2026-05-02"
REPLAY_NOW = "2026-09-15T07:04:51+08:00"    # 事后回放 → REPLAY


def live_now(asof: str) -> str:
    """`asof` **当天**收盘后出预测 → LIVE。

    判据是 `created_at[:10] == asof_date`（见 `PROVENANCE_RULE`），
    所以「实盘」的构造方式必须是**把入库时刻钉在 asof 当天**；
    用别的日期（哪怕只差一天）入库就是补跑，按判据属于 REPLAY。
    """
    return f"{asof}T15:05:00+08:00"


def _bars(n=124):
    d0 = date.fromisoformat(START)
    return [Bar(code=CODE, date=(d0 + timedelta(days=i)).isoformat(),
                open=10.0 * (1 + 0.002 * i), high=10.2 * (1 + 0.002 * i),
                low=9.8 * (1 + 0.002 * i), close=10.0 * (1 + 0.002 * i),
                volume=1000, amount=None, turnover=None, source="test")
            for i in range(n)]


@pytest.fixture
def conn(tmp_path):
    c = seed(tmp_path / "a.db", {CODE: _bars()})
    yield c
    c.close()


def _predict(conn, asof: str, *, now: str) -> str:
    rep = build_predictions(conn, asof, [CODE])
    for p in rep["predictions"]:
        insert_prediction(conn, p, now=now)
    return rep["target_date"]


def _verify(conn, target: str, *, now: str) -> None:
    from stocklab.verify.service import verify_target

    verify_target(conn, target, now=now)


# ---------- 判据本身 ----------

def test_classify_live_and_replay():
    assert classify("2026-08-31", "2026-08-31T15:05:00+08:00") == "live"
    assert classify("2026-08-31", "2026-09-15T07:04:51+08:00") == "replay"
    assert classify("2026-08-31", "") == "replay"          # 缺时间戳 → 保守算回放


def test_classify_does_not_fall_into_the_utc_trap():
    """`created_at` 带 +08:00；凌晨 00:30 的预测**不该**被折回前一天。

    这是刻意不用 SQL `date()` 的原因（它会先折 UTC）—— 用字符串前 10 位没有时区陷阱。
    """
    assert classify("2026-08-31", "2026-08-31T00:30:00+08:00") == "live"


# ---------- 分桶 ----------

def test_rolling_splits_live_and_replay(conn):
    t1 = _predict(conn, "2026-08-30", now=live_now("2026-08-30"))
    _verify(conn, t1, now=live_now("2026-08-30"))
    t2 = _predict(conn, "2026-08-31", now=REPLAY_NOW)
    _verify(conn, t2, now=REPLAY_NOW)

    roll = rolling_accuracy(conn, end_date="2026-09-01", n_sessions=30)
    assert roll["provenance"]["live"]["n_rows"] == 1
    assert roll["provenance"]["replay"]["n_rows"] == 1
    assert roll["live"]["n_rows"] == 1 and roll["replay"]["n_rows"] == 1
    assert roll["live"]["effective_n_days"] == 1
    assert roll["live"]["sample_gate"]["meets"] is False
    assert roll["live"]["sample_gate"]["label"] == INSUFFICIENT


def test_all_replay_means_no_live_bucket_and_no_live_performance(conn):
    """**红线**：全部是回放时，报告不许出现任何被称为「实盘表现」的数字。"""
    t = _predict(conn, "2026-08-30", now=REPLAY_NOW)
    _verify(conn, t, now=REPLAY_NOW)

    rep = build_review(conn, "2026-09-01")
    assert rep["rolling"]["live"] is None
    assert rep["rolling"]["replay"]["n_rows"] == 1
    assert rep["disclosure"]["is_live_performance"] is False
    assert "LIVE(实盘累计) 0 行" in rep["disclosure"]["accuracy_provenance"]

    md = render_markdown(rep)
    # 行首的 `**` 是渲染时的加粗标记（`_window_line`），判的是同一句话
    assert "**LIVE（实盘累计）**：窗口内 0 行" in md
    assert "REPLAY（PIT 历史回放）" in md
    assert "不得" in md and "实盘表现" in md
    assert PROVENANCE_RULE in md


def test_accuracy_carries_ci_and_daily_clustering(conn):
    """**有效样本量 = 交易日数**，且样本量小的时候区间必须诚实地宽。

    两天**一命中一落空**：若两天结果相同，`stdev` 为 0 → 正态近似区间缩成
    `[1.0, 1.0]`，把「2 个样本」说成「确定」—— 那正是本测试最后一条断言要挡的事。
    """
    p1 = _predict(conn, "2026-08-28", now=live_now("2026-08-28"))
    p2 = _predict(conn, "2026-08-29", now=live_now("2026-08-29"))

    # 让 2026-08-30（p2 的目标日）涨 +5%，远超 FLAT_BAND(0.5%) → 实际分类 up，
    # 而模型预测 flat → 该日**不命中**。
    # 预测只用 asof 之前的 bar（`build_predictions` 已完成），所以改 target 日的
    # 行情不违反 PIT —— 它只影响「事后打分」这一步用的实际涨跌幅。
    prev_close = conn.execute(
        "SELECT close FROM bars_daily WHERE code=? AND date=?",
        (CODE, "2026-08-29")).fetchone()["close"]
    conn.execute("UPDATE bars_daily SET close=? WHERE code=? AND date=?",
                 (prev_close * 1.05, CODE, "2026-08-30"))
    conn.commit()

    _verify(conn, p1, now=live_now("2026-08-28"))
    _verify(conn, p2, now=live_now("2026-08-29"))

    roll = rolling_accuracy(conn, end_date="2026-09-01", n_sessions=30)
    live = roll["live"]
    assert live["effective_n_days"] == 2          # 两个交易日 = 2 个有效样本
    assert live["n_rows"] == 2
    assert live["direction_accuracy_daily"] == 0.5    # 一命中一落空
    assert live["direction_ci95"] is not None
    assert "always_up" in live["baselines_daily"]
    # 区间必须宽到能看出来「还不知道」
    lo, hi = live["direction_ci95"]
    assert hi - lo > 0.5


def test_empty_window_is_not_zero_accuracy(conn):
    roll = rolling_accuracy(conn, end_date="2026-09-01", n_sessions=30)
    assert roll["window"]["n_sessions"] == 0
    assert roll["live"] is None and roll["replay"] is None
    assert "没有样本" in roll["note"]


# ---------- 报告其余部分 ----------

def test_report_sections_and_gap_evidence(conn):
    rep = build_review(conn, "2026-09-01")
    for key in ("doctor", "session", "freshness", "day", "rolling", "experiments",
                "gaps", "disclosure"):
        assert key in rep
    gaps = rep["gaps"]
    # 建仓历史 = `_bars()` 的 124 根（2026-05-02 起连续 124 天），
    # 不是某个更小的数：这个断言的含义是「报告如实数了 bars_daily 的行数」
    assert gaps["bars_daily_rows"] == len(_bars()) == 124
    assert gaps["amount_non_null"] == 0 and gaps["amount_first_date"] is None
    assert gaps["turnover_non_null"] == 0
    assert "保持 NULL" in gaps["note"]
    assert rep["experiments"]["rows"] == 0            # 台账只读，本报告一行不写


def test_report_is_read_only(conn):
    """复盘只读：跑完前后所有表行数逐个不变。"""
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'")]
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in tables}
    build_review(conn, "2026-09-01")
    render_markdown(build_review(conn, "2026-09-01"))
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in tables}
    assert before == after


def test_report_is_byte_reproducible(conn):
    """同输入两次 → 逐字节一致（正文不含生成时刻）。"""
    a = render_markdown(build_review(conn, "2026-09-01"))
    b = render_markdown(build_review(conn, "2026-09-01"))
    assert a == b
    assert "生成时间" not in a and "generated_at" not in a


def test_no_rolling_deletion_promise_is_in_the_report(conn):
    rep = build_review(conn, "2026-09-01")
    assert "不做任何滚动清理" in rep["disclosure"]["no_rolling_deletion"]
    assert "不做任何滚动清理" in render_markdown(rep)
