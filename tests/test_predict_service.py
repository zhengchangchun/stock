"""预测服务（Task 31）：PIT 取数、复权缺口硬拒绝、退化集成、可回放。

这里钉住的四件事，每一件都对应一条本项目踩过的坑：
  1. **前视**：asof 之后的行情不许影响 asof 的预测（ERROR_DIARY 2026-09-15 两次）；
  2. **复权缺口**：不可用区间**硬拒绝**，绝不回退到不复权价（2026-09-14「回退」那条）；
  3. **NULL 特征**：payload 必须与 `features_daily` 的内容无关（全 NULL 不得当 0）；
  4. **退化集成**：只有被否证的策略时，载荷必须自己说出来。
"""

import math

import pytest

from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.universe import Instrument
from stocklab.data import adjust
from stocklab.data.models import Bar, CorpAction
from stocklab.predict import service as S
from stocklab.predict import version as V
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T19:00:00+08:00"
CODE = "000333"


def bars(code=CODE, *, n=200, start="2024-01-01", base=10.0, drift=0.002, wave=0.0):
    """造 n 根日 K（日期连续编号，够 PIT 测试用；不追求真实日历）。"""
    out = []
    d0 = _to_ord(start)
    for i in range(n):
        c = base * (1 + drift * i + wave * math.sin(i / 5.0))
        out.append(Bar(code=code, date=_to_date(d0 + i), open=c, high=c * 1.01,
                       low=c * 0.99, close=c, volume=1000, amount=c * 1000,
                       turnover=1.0, source="test", adj_mode="none"))
    return out


def _to_ord(d):
    y, m, dd = (int(x) for x in d.split("-"))
    return y * 372 + m * 31 + dd


def _to_date(o):
    y, rem = divmod(o, 372)
    m, dd = divmod(rem, 31)
    return f"{y:04d}-{m:02d}-{dd:02d}"


def seed(path, bars_by_code, *, events_by_code=None, cal_dates=None):
    init_db(path)
    conn = connect(path)
    repo.upsert_instruments(
        conn, [Instrument(c, c, "sz", "main") for c in bars_by_code], now=NOW)
    for code, rows in bars_by_code.items():
        repo.insert_bars(conn, rows, now=NOW)
    for code, evs in (events_by_code or {}).items():
        repo.insert_corp_actions(conn, evs, now=NOW)
    for code in bars_by_code:
        _, chain = adjust.load_chain(conn, code)
        repo.insert_adj_factors(conn, code, chain, source="test", now=NOW)
    if cal_dates is None:      # 默认：日历 = 行情轴上出现过的日期
        cal_dates = sorted({b.date for rows in bars_by_code.values() for b in rows})
    Calendar.from_dates(cal_dates).save(conn, source="test", now=NOW)
    return conn


# ---------- 1. PIT：未来行情不许改变今天的预测 ----------

def test_future_bars_do_not_change_the_payload(tmp_db, tmp_path):
    """同一个 asof，一个库只有历史、另一个库额外塞了「未来」的暴涨 —— 载荷必须一致。"""
    hist = bars(n=200)
    asof = hist[-1].date

    c1 = seed(tmp_path / "a.db", {CODE: hist})
    p1 = S.build_predictions(c1, asof, [CODE])

    spike = [Bar(code=CODE, date=_to_date(_to_ord(asof) + i), open=99.0, high=99.0,
                 low=99.0, close=99.0, volume=1, amount=1.0, turnover=0.0,
                 source="test", adj_mode="none") for i in (1, 2, 3)]
    c2 = seed(tmp_path / "b.db", {CODE: hist + spike})
    p2 = S.build_predictions(c2, asof, [CODE])

    assert p1["payload_sha256"] == p2["payload_sha256"]
    assert p1["predictions"][0] == p2["predictions"][0]
    c1.close()
    c2.close()


# ---------- 2. 复权缺口：硬拒绝，不回退 ----------

def test_blackout_window_is_hard_rejected(tmp_db, tmp_path):
    """600690 式标的：asof 早于可用下界 → **拒绝出预测**，绝不退回不复权价。

    不复权价在除权日有假跌幅（10 派 20 元转 15 股那天跌 ~63%），
    拿它算 `mu`/`sigma` 会得到一个「看起来正常」的预测 —— 这正是最危险的形态：
    数字合法、无任何报错、结论全错。
    """
    rows = bars(code="600690", n=200, base=20.0)
    ev = CorpAction(code="600690", cqr=rows[100].date, djr=rows[99].date,
                    content="", fh_sh="", source="test")     # 空原文 → 无法定价
    conn = seed(tmp_path / "a.db", {"600690": rows}, events_by_code={"600690": [ev]})
    brk = adjust.usable_from(conn, "600690")
    assert brk == rows[100].date

    rep = S.build_predictions(conn, rows[50].date, ["600690"])
    assert rep["predictions"] == []
    assert "复权" in rep["skipped"]["600690"]
    assert rep["skipped"]["600690"].startswith("UnusableWindow")
    assert "600690" not in rep["payload_sha256"]

    # 可用下界之后仍然正常出预测（拒绝的是区间，不是标的一票否决）
    ok = S.build_predictions(conn, rows[-1].date, ["600690"])
    assert [p["code"] for p in ok["predictions"]] == ["600690"]
    conn.close()


def test_load_pit_bars_never_returns_unadjusted_prices(tmp_db, tmp_path):
    """直接钉住取数函数：不可用区间抛 `UnusableWindow`，**不是**返回一堆价格。"""
    rows = bars(code="600690", n=200, base=20.0)
    ev = CorpAction(code="600690", cqr=rows[100].date, djr=rows[99].date,
                    content="", fh_sh="", source="test")
    conn = seed(tmp_path / "a.db", {"600690": rows}, events_by_code={"600690": [ev]})
    with pytest.raises(S.UnusableWindow):
        S.load_pit_bars(conn, "600690", rows[50].date)
    # 可用区间：价格是**复权**的（锚定 asof，最新价与原始价一致），且全部 <= asof
    got = S.load_pit_bars(conn, "600690", rows[-1].date)
    assert got and max(b.date for b in got) == rows[-1].date
    assert got[-1].close == pytest.approx(rows[-1].close)
    conn.close()


# ---------- 3. NULL 特征：payload 与 features_daily 无关 ----------

def test_payload_is_invariant_to_features_daily(tmp_db, tmp_path):
    """往 `features_daily` 塞非 NULL 的 `pe_pct_3y`，载荷 hash **必须不变**。

    这是「NULL 特征被当成 0/默认值」的反证：一旦实现去读它并 `or 0.0`，
    两次就不同 → 红。本模型压根不读 `features_daily`（`MODEL_SPEC.features_used`）。
    """
    rows = bars(n=200)
    asof = rows[-1].date
    c1 = seed(tmp_path / "a.db", {CODE: rows})
    before = S.build_predictions(c1, asof, [CODE])["payload_sha256"]
    assert S.build_predictions(c1, asof, [CODE])["evidence"][CODE]["features_used"] == []

    c2 = seed(tmp_path / "b.db", {CODE: rows})
    c2.execute(
        "INSERT INTO features_daily (code, date, feature_version, json_payload,"
        " payload_hash, params_hash, data_version, created_at, pe_pct_3y,"
        " main_net_5d, regime_label) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CODE, asof, "v1", "{}", "h", "p", "d1", NOW, 0.97, 12345.0, "bull"))
    c2.commit()
    after = S.build_predictions(c2, asof, [CODE])["payload_sha256"]
    assert after == before
    c1.close()
    c2.close()


# ---------- 4. asof / target_date ----------

def test_asof_must_be_a_session_on_both_calendar_and_bars(tmp_db, tmp_path):
    rows = bars(n=200)
    asof = rows[-1].date
    # 日历里**没有** asof 那天 → 拒绝
    c1 = seed(tmp_path / "a.db", {CODE: rows},
              cal_dates=[b.date for b in rows[:150]])
    with pytest.raises(S.NotASession, match="日历"):
        S.build_predictions(c1, asof, [CODE])
    c1.close()

    # 日历有、但该标的当天没有 K 线（停牌）→ 该标的被跳过，不是整批失败
    c2 = seed(tmp_path / "b.db", {CODE: rows},
              cal_dates=[b.date for b in rows] + [_to_date(_to_ord(asof) + 1)])
    rep = S.build_predictions(c2, asof, [CODE])
    assert len(rep["predictions"]) == 1
    c2.close()


def test_target_date_prefers_calendar_then_falls_back_to_weekday(tmp_db, tmp_path):
    rows = bars(n=200)
    asof = rows[-1].date
    nxt = _to_date(_to_ord(asof) + 1)
    c1 = seed(tmp_path / "a.db", {CODE: rows}, cal_dates=[b.date for b in rows] + [nxt])
    rep = S.build_predictions(c1, asof, [CODE])
    assert rep["target_date"] == nxt and rep["target_date_source"] == "trading_calendar"
    c1.close()

    # 日历耗尽（真实库的现状）→ 工作日外推，并**显式写明**这不是真日历
    c2 = seed(tmp_path / "b.db", {CODE: rows}, cal_dates=[b.date for b in rows])
    rep = S.build_predictions(c2, asof, [CODE])
    assert rep["target_date_source"] == "weekday_fallback"
    assert rep["predictions"][0]["target_date"] == rep["target_date"]
    assert "不是交易日历" in rep["notes"]["target_date"]
    c2.close()


def test_history_replay_produces_a_prediction_for_a_past_asof(tmp_db, tmp_path):
    """历史回放：对 100 个交易日前的 asof 也能给出**当时该给的**预测。

    这是日后测准确率的前提 —— 若回放不可用，「验证」就永远只能从明天开始。
    """
    rows = bars(n=200)
    past = rows[-100].date
    conn = seed(tmp_path / "a.db", {CODE: rows}, cal_dates=[b.date for b in rows])
    rep = S.build_predictions(conn, past, [CODE])
    p = rep["predictions"][0]
    assert p["asof_date"] == past
    assert rep["evidence"][CODE]["inputs"]["last_bar"] == past
    # 回放窗 = 库里 <= past 的全部 K 线（rows[-100] 那根**含**在内 → 101 根）
    assert rep["evidence"][CODE]["inputs"]["n_bars_used"] == \
        sum(1 for b in rows if b.date <= past) == 101
    conn.close()


# ---------- 5. 退化集成 ----------

def test_degenerate_strategy_mix_is_explicit_in_the_payload(tmp_db, tmp_path):
    rows = bars(n=200)
    conn = seed(tmp_path / "a.db", {CODE: rows}, cal_dates=[b.date for b in rows])
    rep = S.build_predictions(conn, rows[-1].date, [CODE])
    mix = rep["predictions"][0]["strategy_mix"]
    assert mix["weights"] == {}
    assert mix["degenerate"] is True
    assert mix["benchmark_only"] == ["buy_and_hold"]
    assert "trend_ma" in mix["excluded"]
    assert V.MODEL_VERSION in mix["excluded"]["trend_ma"] or \
        "docs/experiments/" in mix["excluded"]["trend_ma"]
    # 策略清单里每个已注册策略都必须有明确身份，不许含糊
    ids = {s["strategy_id"]: s["status"] for s in rep["strategies"]}
    assert ids["trend_ma"] == "excluded_falsified"
    assert ids["buy_and_hold"] == "benchmark_only"
    conn.close()


def test_registered_but_unproven_strategy_gets_no_weight(tmp_db, tmp_path, monkeypatch):
    """**注册 ≠ 有 edge**：往全局注册表里塞一个策略，它**不许**自动拿到权重。

    这条测试来自一次真实的踩坑：`strategy_registry` 是**进程内全局**的，
    别的测试模块注册的探针策略会漏进预测路径。若「已注册」= 「可参与集成」，
    一份「多策略集成」就会在**零样本外证据**下凭空出现，
    而且载荷里的 `degenerate` 会翻成 False —— 正好是「假装有集成」。
    """
    from stocklab.strategies.base import Strategy
    from stocklab.strategies.registry import strategy_registry

    @strategy_registry.register
    class _Probe(Strategy):
        strategy_id = "probe_unproven"

        def _generate(self, date, pit_history, pit_features):
            return {}

    try:
        rows = bars(n=200)
        conn = seed(tmp_path / "a.db", {CODE: rows}, cal_dates=[b.date for b in rows])
        rep = S.build_predictions(conn, rows[-1].date, [CODE])
        conn.close()
        ids = {s["strategy_id"]: s["status"] for s in rep["strategies"]}
        assert ids["probe_unproven"] == "inactive_unproven"
        assert rep["strategy_weights"] == {}
        assert rep["predictions"][0]["strategy_mix"]["weights"] == {}
        assert rep["predictions"][0]["strategy_mix"]["degenerate"] is True
    finally:
        strategy_registry._items.pop("probe_unproven", None)
