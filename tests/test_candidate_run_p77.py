"""P77：候选池打分的价格口径（因子侧 PIT 复权价 / 判定侧未复权价）。

任务书：`docs/tasks/2026-09-25-p77-候选池打分价格口径.md`（D1–D4 / T1–T3）。

改前基线（= `stocklab/candidate/run.py` 里「一次取数喂两边」）在这三条判据上的表现：
`ctx["bars"]` 吃未复权价 ⇒ 除权日的假跌幅直接进插桩1 的动量。本站的判据按
「回退 T1 会不会红」设计（红行见任务书 §7）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stocklab.candidate import run as candidate_run
from stocklab.candidate import pools
from stocklab.candidate.run import PipelineResult, score_pipeline
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK, Instrument
from stocklab.data import adjust
from stocklab.plugin import lifecycle, store
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
ASOF = "2026-09-17"

#: 插桩1 的**观测器**：把「插桩1 的动量输入」压成一个数 —— 最后一日的收盘价比值。
#: 未复权（除权日跳水）⇒ 比值 = 0.95；复权（除权日不跳变）⇒ 比值 = 1.0。
#: 这样「ctx 里喂的是哪一档价」在 `raw_score` 上直接可观测，不需要打桩 `build_ctx`。
PLUGINS = {
    "0": "def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n",
    "1": ("def run(ctx):\n"
          "    bars = ctx['bars']\n"
          "    ret = bars[-1]['close'] / bars[-2]['close']\n"
          "    return {'score': ret, 'pass_flag': True, 'reason': '末段收益',"
          " 'risk_list': []}\n"),
    "2": "def run(ctx):\n    return {'score': 60.0, 'pass_flag': True,"
         " 'reason': '景气', 'risk_list': []}\n",
    "3": "def run(ctx):\n    return {'score': 40.0, 'pass_flag': True,"
         " 'reason': '护城河', 'risk_list': []}\n",
    "4": "def run(ctx):\n    return {'final_score': ctx['raw_score'],"
         " 'risk_out': []}\n",
}

#: 真实样本（真库 `data/stocklab.db` 的 `bars_daily`，代码 `000333`）：
#: 2014-04-30 除权（`corp_actions.content = '10派20元转15股'`），不复权价当日
#: 46.99 → 17.27（**−63.2%**），复权后仅 −4.0%。真库只读复制到 `/tmp` 后
#: 用 `SELECT date,open,high,low,close,volume FROM bars_daily WHERE code='000333'
#: AND date BETWEEN '2014-04-23' AND '2014-05-08' ORDER BY date` 取出。
REAL_BARS = [
    ("2014-04-23", 45.81, 46.90, 45.72, 46.09, 10349600),
    ("2014-04-24", 45.99, 47.27, 45.52, 47.17, 11668000),
    ("2014-04-25", 47.39, 47.57, 46.75, 47.01, 9257200),
    ("2014-04-28", 47.00, 47.23, 44.99, 46.44, 9554300),
    ("2014-04-29", 46.70, 47.89, 45.89, 46.99, 18898200),
    ("2014-04-30", 17.88, 17.89, 17.14, 17.27, 27266600),
    ("2014-05-05", 17.23, 17.69, 16.81, 17.55, 21753200),
    ("2014-05-06", 17.40, 17.60, 17.05, 17.15, 16311100),
    ("2014-05-07", 17.05, 17.06, 16.68, 16.70, 19678300),
    ("2014-05-08", 16.68, 17.20, 16.51, 16.84, 18181300),
]
REAL_CQR = "2014-04-30"
REAL_CONTENT = "10派20元转15股"


def _new_db(tmp_db):
    init_db(tmp_db)
    conn = connect(tmp_db)
    for pid, text in PLUGINS.items():
        sid = store.insert_script(conn, plugin_id=pid, version="1.0.0",
                                  source_text=text, note=None, now=NOW)
        lifecycle.record_submit(conn, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(conn, sid, actor="t", reason="ok", now=NOW)
    conn.commit()
    return conn


def _weekdays(n_days: int, end: str = ASOF) -> list[str]:
    days: list[str] = []
    cur = date.fromisoformat(end)
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur -= timedelta(days=1)
    return sorted(days)


def _add_instrument(conn, code, *, name=None, board="main", asset_type=ASSET_STOCK):
    conn.execute(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,?,?,?,?)",
        (code, name or f"标的{code}", "sh", board, asset_type, NOW))


def _add_bars(conn, code, rows):
    conn.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,?,'none','x',?)",
        [(code, d, o, h, low_, close, vol, NOW)
         for d, o, h, low_, close, vol in rows])
    conn.commit()


def _build_factor_chain(conn, code):
    """按 `adj rebuild` 的口径把因子链 + 缺口记录落库（= 「有因子行」的标的）。"""
    bars, chain = adjust.load_chain(conn, code)
    repo.insert_adj_factors(conn, code, chain, source="test", now=NOW)
    return bars, chain


# ---------------------------------------------------------------------------
# T1 两条取数
# ---------------------------------------------------------------------------

def test_p77_real_sample_date_sequences_are_identical(tmp_db):
    """T1：同一天 `_load_bars` 与 `_load_bars_adjusted` 的 `date` 序列逐位相同。

    样本取自**真库**（`000333` 2014-04-30 除权窗口，见 `REAL_BARS` 注释），
    且该标的有因子行（`adj rebuild` 的口径），因此 `assert_blackout_current`
    的核对路径也被走到。价格本身必须**不同** —— 否则这条判据不能用来说明
    「复权真的生效了」。
    """
    conn = _new_db(tmp_db)
    _add_instrument(conn, "000333")
    _add_bars(conn, "000333", REAL_BARS)
    conn.execute(
        "INSERT INTO corp_actions (code, cqr, djr, fh_sh, content, source,"
        " first_seen, last_seen) VALUES ('000333',?,?,NULL,?,'tencent',?,?)",
        (REAL_CQR, "2014-04-29", REAL_CONTENT, NOW, NOW))
    conn.commit()
    _build_factor_chain(conn, "000333")
    assert conn.execute("SELECT COUNT(*) FROM adj_factors WHERE code='000333'"
                        ).fetchone()[0] > 0, "样本必须是「有因子行」的标的"

    raw = candidate_run._load_bars(conn, "000333", asof=REAL_CQR)
    adj, fallback = candidate_run._load_bars_adjusted(
        conn, "000333", asof=REAL_CQR, raw=raw)

    assert fallback is None
    assert [b.date for b in adj] == [b.date for b in raw]
    assert [(b.open, b.high, b.low, b.close) for b in adj] != \
           [(b.open, b.high, b.low, b.close) for b in raw], "复权未生效"
    conn.close()


def test_p77_pipeline_feeds_screen_with_raw_bars(tmp_db, monkeypatch):
    """T1/D2 的**机械**自证：`score_pipeline` 交给 `screen.screen` 的就是 `_load_bars`。

    行为版见 T3 的第二条断言（有咬合力）；这条是定点版 —— 直接把两侧的 bars
    抓下来逐位比对，`screen.py` 一行不改、喂进去的价也不许换。
    """
    conn, scan, _ = _dividend_universe(tmp_db)
    seen = {}
    real_screen = candidate_run.screen.screen

    def spy(inst, bars, *, asof):
        seen[inst.code] = [(b.date, b.close, b.adj_mode) for b in bars]
        return real_screen(inst, bars, asof=asof)

    monkeypatch.setattr(candidate_run.screen, "screen", spy)
    score_pipeline(conn, asof=ASOF, universe=scan)
    assert seen["600104"] == [
        (b.date, b.close, b.adj_mode)
        for b in candidate_run._load_bars(conn, "600104", asof=ASOF)]
    assert {m[2] for m in seen["600104"]} == {"none"}, "判定侧吃了复权价"
    conn.close()


# ---------------------------------------------------------------------------
# T2 ETF / 缺因子标的：回退 + 计数 + 可见
# ---------------------------------------------------------------------------

def _fallback_universe(tmp_db):
    """无因子行的个股 + ETF + 真样本个股（后者用来证明「不是所有标的都回退」）。"""
    conn = _new_db(tmp_db)
    days = _weekdays(300)
    _add_instrument(conn, "600519")
    _add_bars(conn, "600519", [(d, 10.0, 10.0, 10.0, 10.0, 1000) for d in days])
    _add_instrument(conn, "510300", name="沪深300ETF", asset_type=ASSET_ETF)
    _add_bars(conn, "510300", [(d, 4.5, 4.5, 4.5, 4.5, 1000) for d in days])
    scan = [
        Instrument(code="600519", name="贵州茅台", market="sh", board="main"),
        Instrument(code="510300", name="沪深300ETF", market="sh", board="main",
                   asset_type=ASSET_ETF),
    ]
    return conn, scan


def test_p77_fallback_members_are_bytewise_identical_to_old_caliber(
        tmp_db, monkeypatch):
    """T2 反证：ETF / 无因子行的标的在改前改后**逐位相同**（因子恒 1 ⇒ 价 = 未复权价）。

    旧口径用 `_load_bars_adjusted → (raw, None)` 复现（= 改前那条
    「一次取数喂两边」的路径）。若两者不等，说明这次顺手改了别的东西。
    """
    conn, scan = _fallback_universe(tmp_db)
    after = score_pipeline(conn, asof=ASOF, universe=scan)

    monkeypatch.setattr(candidate_run, "_load_bars_adjusted",
                        lambda conn, code, *, asof, raw: (raw, None))
    before = score_pipeline(conn, asof=ASOF, universe=scan)

    key = lambda r: [(m.code, m.pool, m.raw_score, m.adj_score, m.reason,
                      m.risk_json, m.status) for m in r.members]
    assert key(after) == key(before)
    assert [(r.code, r.stage, r.reason) for r in after.rejects] == \
           [(r.code, r.stage, r.reason) for r in before.rejects]
    assert after.eligible == before.eligible
    conn.close()


def test_p77_fallback_is_counted_and_visible_in_params(tmp_db):
    """T2/D3：两类回退都计数、ETF 不抛异常、计数进 `params`。"""
    conn, scan = _fallback_universe(tmp_db)
    pipe = score_pipeline(conn, asof=ASOF, universe=scan)
    assert isinstance(pipe, PipelineResult)
    # 两只都不复权可用：600519 无事件（链为空）、510300 是 ETF（ADR-008 拒绝服务）
    assert pipe.params["n_adj_fallback"] == 2
    assert pipe.params["scoring_price_mode"] == \
        candidate_run.SCORING_PRICE_MODE
    conn.close()


def test_p77_snapshot_params_has_two_new_keys_and_history_untouched(tmp_db):
    """T2/D4：新键进快照 `params_json`；**历史快照行一个字节都不许改**。"""
    conn, scan = _fallback_universe(tmp_db)
    legacy = json.dumps({"seed_count": 2, "topn": dict(pools.POOL_TOPN)},
                        ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
        " created_at) VALUES (?,?,?,?)", (ASOF, "light", legacy, NOW))
    conn.commit()

    result = candidate_run.run_candidate(conn, asof=ASOF, run_kind="light",
                                        now=NOW, universe=scan)
    assert result.skipped is True
    assert result.params == {"seed_count": 2, "topn": dict(pools.POOL_TOPN)}
    assert conn.execute(
        "SELECT params_json FROM candidate_snapshots WHERE run_kind='light'"
    ).fetchone()[0] == legacy, "老快照行被改写了（D4 禁止）"

    fresh = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly",
                                        now=NOW, universe=scan)
    params = json.loads(conn.execute(
        "SELECT params_json FROM candidate_snapshots WHERE run_kind='weekly'"
    ).fetchone()[0])
    assert params == fresh.params
    assert params["scoring_price_mode"] == "adjusted_factor_side+raw_screen"
    assert params["n_adj_fallback"] == 2
    old = {k: v for k, v in params.items()
           if k not in ("scoring_price_mode", "n_adj_fallback")}
    from stocklab.config.universes import seed21_sha256
    assert old == {"seed_count": 2, "topn": dict(pools.POOL_TOPN),
                   "universe_id": "seed21",
                   "members_sha256": seed21_sha256()}
    conn.close()


# ---------------------------------------------------------------------------
# T3 红-绿自检
# ---------------------------------------------------------------------------

def _dividend_universe(tmp_db):
    """除权日为 `asof` 的标的：未复权跳水 5%，复权后**连续**（收益 0）。"""
    conn = _new_db(tmp_db)
    days = _weekdays(300)
    rows = [(d, 10.0, 10.0, 10.0, 10.0, 1000) for d in days[:-1]]
    rows.append((days[-1], 9.5, 9.5, 9.5, 9.5, 1000))
    _add_instrument(conn, "600104")
    _add_bars(conn, "600104", rows)
    conn.execute(
        "INSERT INTO corp_actions (code, cqr, djr, fh_sh, content, source,"
        " first_seen, last_seen) VALUES ('600104',?,?,5.0,'10派5元','tencent',?,?)",
        (ASOF, ASOF, NOW, NOW))
    conn.commit()
    _build_factor_chain(conn, "600104")
    scan = [Instrument(code="600104", name="上汽集团", market="sh",
                       board="main")]
    return conn, scan, days[-1]


def test_p77_t3a_plugin1_sees_continuous_adjusted_prices(tmp_db):
    """断言①：插桩1 的动量输入（`ctx["bars"]` 收益序列）在除权日**不跳变**。"""
    conn, scan, _ = _dividend_universe(tmp_db)
    pipe = score_pipeline(conn, asof=ASOF, universe=scan)
    short = {m.code: m.raw_score for m in pipe.members if m.pool == "short"}
    assert short["600104"] == pytest.approx(1.0), (
        "插桩1 看到的除权日收益应为 1.0（复权），实际 "
        f"{short['600104']}（0.95 = 未复权）")

    raw = candidate_run._load_bars(conn, "600104", asof=ASOF)
    assert raw[-1].close / raw[-2].close == pytest.approx(0.95), \
        "fixture 本身要能区分两档价（未复权比值 0.95）"
    # 有因子行、复权成功的标的**不该**进回退计数（计数不是「恒等于 universe 大小」）
    assert pipe.params["n_adj_fallback"] == 0
    conn.close()


def test_p77_t3a_real_sample_ex_div_jump_is_removed(tmp_db):
    """断言①（真样本）：`000333` 2014-04-30 的 −63.2% 假跌幅在复权序列里消失。"""
    conn = _new_db(tmp_db)
    _add_instrument(conn, "000333")
    _add_bars(conn, "000333", REAL_BARS)
    conn.execute(
        "INSERT INTO corp_actions (code, cqr, djr, fh_sh, content, source,"
        " first_seen, last_seen) VALUES ('000333',?,?,'19.0',?,'tencent',?,?)",
        (REAL_CQR, "2014-04-29", REAL_CONTENT, NOW, NOW))
    conn.commit()
    _build_factor_chain(conn, "000333")

    raw = candidate_run._load_bars(conn, "000333", asof=REAL_CQR)
    adj, fallback = candidate_run._load_bars_adjusted(conn, "000333",
                                                     asof=REAL_CQR, raw=raw)
    assert fallback is None
    raw_ret = raw[-1].close / raw[-2].close - 1.0
    adj_ret = adj[-1].close / adj[-2].close - 1.0
    assert raw_ret == pytest.approx(-0.6325, abs=1e-4)     # 46.99 → 17.27
    assert adj_ret == pytest.approx(-0.0403, abs=1e-3)     # 假跌幅已还原
    conn.close()


def test_p77_t3b_screen_still_judges_limit_down_on_raw(tmp_db):
    """断言②：未复权价构成连续跌停时，`screen` 仍判跌停（判定侧吃未复权）。

    fixture：连续 3 日各「10送2股」除权 ⇒ 未复权价每日 −16.7%（> 板块 10%
    阈值，构成跌停），而复权后**逐日收益为 0**。若 pipeline 把复权价喂给
    `screen`，这只标的会通过排雷 —— 正是 D2 要防的那种「顺手改到别处」。
    """
    conn = _new_db(tmp_db)
    days = _weekdays(303)
    drops = days[-3:]
    closes = {}
    for d in days[:-3]:
        closes[d] = 12.0
    for i, d in enumerate(drops):
        closes[d] = closes[days[days.index(d) - 1]] / 1.2
    _add_instrument(conn, "600690")
    _add_bars(conn, "600690", [(d, c, c, c, c, 1000) for d, c in closes.items()])
    conn.executemany(
        "INSERT INTO corp_actions (code, cqr, djr, fh_sh, content, source,"
        " first_seen, last_seen) VALUES ('600690',?,?,NULL,'10送2股','tencent',?,?)",
        [(d, d, NOW, NOW) for d in drops])
    conn.commit()
    _build_factor_chain(conn, "600690")

    scan = [Instrument(code="600690", name="青岛海尔", market="sh",
                       board="main")]
    pipe = score_pipeline(conn, asof=ASOF, universe=scan)
    rejects = {r.code: r.reason for r in pipe.rejects}
    assert rejects.get("600690") == "consecutive_limit_down", (
        f"screen 应吃未复权价并判跌停，实际 rejects={rejects}、"
        f"members={[m.code for m in pipe.members]}")

    # 有咬合力的对照：同一天的**复权**序列没有跌停（三个除权日收益全为 0）
    raw = candidate_run._load_bars(conn, "600690", asof=ASOF)
    adj, fallback = candidate_run._load_bars_adjusted(conn, "600690",
                                                     asof=ASOF, raw=raw)
    assert fallback is None
    adj_drops = [(b.close - a.close) / a.close
                 for a, b in zip(adj, adj[1:])]
    assert max(adj_drops) >= -0.10, "复权序列本不该有跌停 —— 否则这条判据没咬合力"
    conn.close()


def test_p77_t3c_two_runs_are_bytewise_identical(tmp_db):
    """断言③：同一天两次 `score_pipeline(universe=...)` 逐位相同（确定性）。"""
    conn, scan, _ = _dividend_universe(tmp_db)
    a = score_pipeline(conn, asof=ASOF, universe=scan)
    b = score_pipeline(conn, asof=ASOF, universe=scan)
    dump = lambda r: json.dumps({
        "members": [(m.code, m.pool, m.raw_score, m.adj_score, m.reason,
                     m.risk_json, m.status) for m in r.members],
        "rejects": [(x.code, x.stage, x.reason) for x in r.rejects],
        "eligible": r.eligible, "params": r.params}, ensure_ascii=False,
        sort_keys=True)
    assert dump(a) == dump(b)
    assert a.params["n_adj_fallback"] == b.params["n_adj_fallback"]
    conn.close()
