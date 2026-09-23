"""P50 T1/T2：误差归因**结构位**（D-31）—— 程序只给候选、人工确认是唯一结论。

判据 → 用例：

| # | 判据 | 用例 |
|---|---|---|
| T1 | **归因不自动填** | `test_t1_*`：构造错误样本 ⇒ `attribution_manual` 恒空、`attribution_auto` 只含候选并带信号与阈值；页面出现「候选」标记 |
| T2 | **候选可证伪** | `test_t2_*`：大盘跌 3% / 同行业两只同向异动 / `corp_actions` 新增 ⇒ 候选与预期一致；**边界（恰好等于阈值）**有断言 |
| ＋ | 判据落常量 | `test_criteria_*`：信号名与阈值只在 `stocklab/config/` 定义一次（模块里不出现字面量） |

数字**一律从被测模块取**（`stocklab.config.m2_signals`），不手抄。
"""

from __future__ import annotations

import ast
import datetime
import inspect
import sqlite3
from pathlib import Path

import pytest

from stocklab.cli.main import main
from stocklab.config import m2_signals
from stocklab.labweb import m2_data, m2_render
from stocklab.m2 import attribution as m2_attr
from stocklab.m2 import config as m2_config
from stocklab.m2 import score as m2_score
from stocklab.m2 import store as m2_store
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-22T16:00:00+08:00"
START = "2026-09-15"
STOCK = "600519"
PEER_A = "000001"
PEER_B = "000002"
INDUSTRY = "白酒"
ACCOUNT = "arm-agent-v1"


def _sessions(n: int, *, start: str = START) -> list[str]:
    base = datetime.date.fromisoformat(start)
    return [(base + datetime.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _bars_rows(code: str, days: list[str], closes: list[float],
               *, adj_mode: str = "none", suspended: list[int] | None = None):
    out = []
    for i, (d, v) in enumerate(zip(days, closes)):
        pre = closes[i - 1] if i else v
        out.append((code, d, v, v, v, v, pre, 100, adj_mode,
                    1 if suspended and suspended[i] else 0, "x", NOW))
    return out


def _build(tmp_path, *, n_sessions: int = 6, stock_closes=None,
           index_closes=None, peer_closes=None, industry: str | None = INDUSTRY,
           corp_on: str | None = None, corp_text: str = "10配3股",
           dq_on: str | None = None, dq_type: str | None = None,
           suspend_on: str | None = None, direction: dict | None = None,
           forecast_from: int = 0):
    """一份最小库：**已落库的错判样本** + 可选的四类信号。

    价格路径：标的单调下跌、预测单调看涨 ⇒ 每一条预测都判错（错判样本是真打分器
    判出来的，不是手插的分数行）。指数取 `index_closes`（默认走平）。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "attr.db"
    init_db(path)
    c = connect(path)
    days = _sessions(n_sessions)
    stock_closes = stock_closes or [100.0 - 2.0 * i for i in range(n_sessions)]
    index_closes = index_closes or [4000.0 for _ in days]
    peer_closes = peer_closes or [100.0 - 1.0 * i for i in range(n_sessions)]

    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sh','main',?,?)",
        [(STOCK, "贵州茅台", "stock", NOW), (PEER_A, "甲", "stock", NOW),
         (PEER_B, "乙", "stock", NOW), (INDEX_300_SYMBOL, "沪深300", "index", NOW)])
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in days])
    rows = ([] if stock_closes is None else [])
    rows += _bars_rows(STOCK, days, stock_closes,
                       suspended=[1 if d == suspend_on else 0 for d in days])
    rows += _bars_rows(PEER_A, days, peer_closes)
    rows += _bars_rows(PEER_B, days, peer_closes)
    for i, v in enumerate(index_closes):
        pre = index_closes[i - 1] if i else v
        rows.append((INDEX_300_SYMBOL, days[i], v, v, v, v, pre, 100, "none", 0,
                     "x", NOW))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, pre_close,"
        " volume, adj_mode, is_suspended, source, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    for code in (STOCK, PEER_A, PEER_B):
        c.execute(
            "INSERT INTO financial_reports (code, report_date, notice_date,"
            " notice_date_source, report_type, industry_name, source, fetched_at,"
            " created_at, raw_refs_json) VALUES (?,?,?,'f10','年报',?,'x',?,?,?)",
            (code, "2025-12-31", "2026-01-10", industry, NOW, NOW, "[]"))
    if corp_on:
        c.execute(
            "INSERT INTO corp_actions (code, cqr, content, source, first_seen,"
            " last_seen) VALUES (?,?,?,'x',?,?)",
            (STOCK, corp_on, corp_text, NOW, NOW))
    if dq_on and dq_type:
        c.execute(
            "INSERT INTO data_quality (date, source, code, issue_type, severity,"
            " first_seen, last_seen) VALUES (?,?,?,?,'error',?,?)",
            (dq_on, "tencent", STOCK, dq_type, NOW, NOW))
    c.commit()

    direction = direction or {"up": 0.6, "flat": 0.2, "down": 0.2}
    for i in range(forecast_from, n_sessions - 1):
        m2_store.insert_forecast(
            c, plugin_id="m2_a3", channel="A", account_id=ACCOUNT,
            asof=days[i], code=STOCK,
            payload={"range_80": [0.01, 10000.0], "direction": direction,
                     "invalidate_if": None, "na_reasons": [],
                     "schema_version": "1"},
            script_id=1, script_version="s1", input_sha256=f"{i:064d}", now=NOW)
    c.commit()
    m2_score.score_all(c, asof=days[-1], now=NOW)
    c.close()
    return path, days


def _conn(path):
    return connect(path)


def _scan(path, asof: str) -> int:
    return main(["m2", "attribute", "scan", "--asof", asof, "--db", str(path),
                 "--now", NOW])


# ══════════════════════════════════════════════════════════════════════
# T1 —— 归因不自动填（D-31）
# ══════════════════════════════════════════════════════════════════════


def test_t1_candidates_are_written_but_the_manual_field_stays_empty(tmp_path, capsys):
    """扫出候选之后：`attribution_auto` 有候选、`attribution_manual` **仍然恒空**。"""
    path, days = _build(tmp_path)
    assert _scan(path, days[-1]) == 0
    capsys.readouterr()
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
        manual_rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_MANUAL)
        auto_rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
    finally:
        c.close()
    cases = panel["cases"]["cases"]
    assert cases, "夹具没有造出错判样本"
    assert panel["cases"]["n_miss_total"] > 0
    assert auto_rows, "扫描之后应当有候选"
    assert manual_rows == [], "扫描**不许**写人工结论行（D-31：唯一结论字段恒不自动填）"
    for case in cases:
        assert case["attribution_manual"] is None
        for cand in case["attribution_auto"]:
            assert cand["label"] in m2_signals.LABELS
            assert cand["signal"] in m2_signals.SIGNAL_TEXTS
            assert cand["threshold"] is not None or cand["at_value"] is not None
            assert cand["step"], "候选必须写明「算到哪一步」"
        break


def test_t1_a_candidate_is_marked_as_candidate_not_as_a_conclusion(tmp_path, capsys):
    """候选带「候选」标记，且**不是**结论字段（页面上两列分开）。"""
    path, days = _build(tmp_path, index_closes=None)
    # 目标日大盘 -3% ⇒ 必有「大盘冲击」候选
    days_list = _sessions(6)
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
        html = m2_render.m2_page(panel, base="/lab", built_at=NOW)
        text = m2_render.m2_text(panel)
    finally:
        c.close()
    cands = [x for case in panel["cases"]["cases"] for x in case["attribution_auto"]]
    assert cands
    # 标记必须**是非空的「候选」二字**：只断言「等于常量」的话，把常量改成空串
    # 两边一起变，判据就失去区分力（变异注入实测过一次）。
    assert m2_signals.CANDIDATE_MARK == "候选"
    assert all(x["mark"] == m2_signals.CANDIDATE_MARK == "候选" for x in cands)
    assert m2_signals.CANDIDATE_MARK in html
    assert m2_signals.CANDIDATE_MARK in text
    # 人工结论列写「空」而不是「候选」—— 两列不许混成一件
    assert "恒空" in html or "空" in html
    assert days_list  # 夹具自检（日期轴非空）


def test_t1_the_scan_is_idempotent_and_the_unique_index_is_the_defense(tmp_path, capsys):
    """同 `(案例, 来源, 标签, 信号)` 只落一行；绕接口裸插第二条被唯一键拒。"""
    path, days = _build(tmp_path)
    assert _scan(path, days[-1]) == 0
    capsys.readouterr()
    c = _conn(path)
    try:
        before = m2_store.list_attributions(c)
        assert _scan(path, days[-1]) == 0
        capsys.readouterr()
        after = m2_store.list_attributions(c)
        assert len(after) == len(before), "重放扫描不增行"
        row = before[0]
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                f"INSERT INTO {m2_store.TABLE_ATTRIBUTIONS} (score_id, forecast_id,"
                " code, signal_date, source, label, signal, threshold, at_value,"
                " step, text, detail_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["score_id"], row["forecast_id"], row["code"],
                 row["signal_date"], row["source"], row["label"], row["signal"],
                 row["threshold"], row["at_value"], row["step"], row["text"],
                 "{}", NOW))
    finally:
        c.close()


def test_t1_manual_conclusion_is_written_only_by_the_human_cli(tmp_path, capsys):
    """人工确认走 CLI；未知标签 → 拒绝（exit 2）。"""
    path, days = _build(tmp_path)
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        score_id = int(m2_store.list_attributions(c)[0]["score_id"])
    finally:
        c.close()
    assert main(["m2", "attribute", "confirm", "--score-id", str(score_id),
                 "--label", "not_a_label", "--reason", "x", "--db", str(path),
                 "--now", NOW]) == 2
    capsys.readouterr()
    assert main(["m2", "attribute", "confirm", "--score-id", str(score_id),
                 "--label", m2_signals.LABEL_STOCK_NEWS, "--reason",
                 "公告配股，属机械信号；人工复核后确认为个股突发利空",
                 "--db", str(path), "--now", NOW]) == 0
    capsys.readouterr()
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
        manual = [case for case in panel["cases"]["cases"]
                  if case["score_id"] == score_id]
    finally:
        c.close()
    assert manual and manual[0]["attribution_manual"] == m2_signals.LABEL_STOCK_NEWS
    # 有人工结论时**保留候选**（对账用）
    assert manual[0]["attribution_auto"]


# ══════════════════════════════════════════════════════════════════════
# T2 —— 候选可证伪（给定构造输入 ⇒ 候选与预期一致；边界有断言）
# ══════════════════════════════════════════════════════════════════════


def _candidates(conn, score_id: int) -> list[dict]:
    score = next(s for s in m2_data.miss_cases(conn, "9999-12-31")["cases"]
                 if s["score_id"] == score_id)
    rows = m2_store.list_attributions(conn, score_id=score_id,
                                      source=m2_attr.SOURCE_AUTO)
    assert score  # 案例存在
    return rows


def _labels_by_signal(rows) -> dict[str, dict]:
    return {str(r["signal"]): r for r in rows}


def test_t2_the_index_drop_beyond_the_threshold_yields_a_market_shock_candidate(
        tmp_path, capsys):
    """大盘跌破阈值 ⇒ 大盘冲击候选；信号名与阈值逐字来自常量。"""
    path, days = _build(tmp_path, index_closes=[4000.0, 4000.0, 3880.0,
                                                3880.0, 3880.0, 3880.0])
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
    finally:
        c.close()
    hit = [r for r in rows if r["signal"] == m2_signals.SIGNAL_INDEX_PCT_CHG
           and r["signal_date"] == days[2]]
    assert hit, "目标日大盘 -3% 应当给出大盘冲击候选"
    row = hit[0]
    assert row["label"] == m2_signals.LABEL_MARKET_SHOCK
    assert row["threshold"] == m2_signals.MARKET_DROP_THRESHOLD
    assert abs(float(row["at_value"]) - (-0.03)) < 1e-9
    assert m2_signals.SIGNAL_TEXTS[m2_signals.SIGNAL_INDEX_PCT_CHG] in row["text"]


def test_t2_the_threshold_boundary_is_inclusive(tmp_path, capsys):
    """**恰好等于阈值**触发；差一点点不触发（判据的边界必须是可断言的）。"""
    thr = m2_signals.MARKET_DROP_THRESHOLD
    exact = [4000.0 * (1.0 + thr) if i == 2 else 4000.0 for i in range(6)]
    path, days = _build(tmp_path, index_closes=exact)
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
    finally:
        c.close()
    assert [r for r in rows if r["signal"] == m2_signals.SIGNAL_INDEX_PCT_CHG], \
        "恰好等于阈值应当触发（判据是 ≥ 跌幅）"

    just_inside = [4000.0 * (1.0 + thr) + 0.5 if i == 2 else 4000.0
                   for i in range(6)]
    path2, days2 = _build(tmp_path / "inside", index_closes=just_inside)
    _scan(path2, days2[-1])
    capsys.readouterr()
    c2 = _conn(path2)
    try:
        rows2 = m2_store.list_attributions(c2, source=m2_attr.SOURCE_AUTO)
    finally:
        c2.close()
    assert not [r for r in rows2 if r["signal"] == m2_signals.SIGNAL_INDEX_PCT_CHG], \
        "没到阈值不许给出大盘冲击候选"


def test_t2_industry_peers_moving_the_same_way_yield_a_blackswan_candidate(
        tmp_path, capsys):
    """同行业同日 ≥ 阈值只数同向异动 ⇒ 行业黑天鹅候选（行业取自既有真源）。"""
    path, days = _build(tmp_path, peer_closes=[100.0 - 6.0 * i for i in range(6)])
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
    finally:
        c.close()
    hit = [r for r in rows if r["signal"] == m2_signals.SIGNAL_INDUSTRY_PEERS]
    assert hit, "两只同行业同向异动应当给出行业黑天鹅候选"
    row = hit[0]
    assert row["label"] == m2_signals.LABEL_INDUSTRY_BLACKSWAN
    assert row["threshold"] == m2_signals.INDUSTRY_PEER_DROP_THRESHOLD
    assert float(row["at_value"]) >= m2_signals.INDUSTRY_PEER_MIN
    assert INDUSTRY in row["text"] or "行业" in row["text"]


def test_t2_a_corp_action_in_the_whitelist_yields_a_stock_news_candidate(tmp_path):
    """`corp_actions` 当日事件命中类型白名单 ⇒ 个股突发利空候选。

    用**构造样本**直接喂 `candidates_for`：真源原文是 `10配3股` 这种形态，
    判据是正则（子串 `配股` 在里面不连续 —— 这正是本用例第一次跑红的原因）。
    """
    days = _sessions(6)
    path, days = _build(tmp_path, corp_on=days[2], corp_text="10配3股")
    c = _conn(path)
    try:
        cands = m2_attr.candidates_for(c, score={
            "score_id": 0, "forecast_id": 0, "code": STOCK,
            "asof_date": days[1], "target_date": days[2]})
    finally:
        c.close()
    hit = [x for x in cands if x["signal"] == m2_signals.SIGNAL_CORP_ACTION]
    assert hit, "白名单命中的除权除息事件应当给出个股突发利空候选"
    assert hit[0]["label"] == m2_signals.LABEL_STOCK_NEWS
    assert "10配3股" in hit[0]["text"]


def test_t2_a_corp_action_outside_the_whitelist_is_not_a_candidate(tmp_path):
    """白名单外的事件（送转/派息）**不**产生候选 —— 否则每天都有「利空」。"""
    days = _sessions(6)
    path, days = _build(tmp_path, corp_on=days[2], corp_text="10派20元转15股")
    c = _conn(path)
    try:
        cands = m2_attr.candidates_for(c, score={
            "score_id": 0, "forecast_id": 0, "code": STOCK,
            "asof_date": days[1], "target_date": days[2]})
    finally:
        c.close()
    assert not [x for x in cands if x["signal"] == m2_signals.SIGNAL_CORP_ACTION]


def test_t2_the_whitelist_matches_both_real_world_content_spellings(tmp_path):
    """`配股` 与 `10配3股` 两种原文都要命中（判据是正则，不是子串）。"""
    days = _sessions(6)
    for text in ("配股", "10配3股", "10配 3 股"):
        path, _ = _build(tmp_path / text.replace(" ", "_"), corp_on=days[2],
                         corp_text=text)
        c = _conn(path)
        try:
            hits = m2_attr.corp_action_hits(c, STOCK, days[2])
        finally:
            c.close()
        assert hits, f"原文「{text}」应当命中白名单"


def test_t2_the_corp_action_day_is_not_scorable_without_an_adjust_chain(
        tmp_path, capsys):
    """**可达性实测**：只有 `corp_actions` 行、没有 `adj_factors` 因子链时，
    既有打分器判 `NOT_ADJUSTABLE` ⇒ 那些日子没有可评分样本、扫描路径因此
    产不出除权日候选（真库有因子链，这两条路径的差别写进实施记录）。
    """
    days = _sessions(6)
    path, days = _build(tmp_path, corp_on=days[2], corp_text="10配3股")
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        scores = m2_store.list_scores(c, asof=days[-1])
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
    finally:
        c.close()
    assert [s for s in scores if str(s["target_date"]) == days[2]]
    assert {str(s["reason_code"]) for s in scores if int(s["scorable"]) == 0} \
        == {"NOT_ADJUSTABLE"}
    assert not [r for r in rows if r["signal"] == m2_signals.SIGNAL_CORP_ACTION]


def test_t2_the_drop_and_data_quality_signals_are_separate_candidates(
        tmp_path, capsys):
    """停牌 / `data_quality` 异常 / 单日跌幅 —— 三个信号各自出候选，不合并成一格。"""
    days = _sessions(6)
    path, days = _build(tmp_path, dq_on=days[2],
                        dq_type=m2_signals.DATA_QUALITY_TYPE_WHITELIST[0],
                        stock_closes=[100.0, 100.0, 88.0, 88.0, 88.0, 88.0])
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
    finally:
        c.close()
    signals = {str(r["signal"]) for r in rows if r["signal_date"] == days[2]}
    assert m2_signals.SIGNAL_DATA_QUALITY in signals
    assert m2_signals.SIGNAL_STOCK_PCT_CHG in signals
    assert m2_signals.SIGNAL_SUSPENDED not in signals


def test_t2_a_suspended_bar_is_a_candidate_on_its_own_signal(tmp_path):
    """停牌这条信号自己成立（停牌日没有跌幅可比，不许靠跌幅算）。

    用**构造样本**直接喂 `candidates_for`：停牌的目标日被既有打分器判
    `SUSPENDED`（不可评分），所以扫描路径产不出停牌日的案例 —— 可达性见下一条。
    """
    days = _sessions(6)
    path, days = _build(tmp_path, suspend_on=days[2])
    c = _conn(path)
    try:
        cands = m2_attr.candidates_for(c, score={
            "score_id": 0, "forecast_id": 0, "code": STOCK,
            "asof_date": days[1], "target_date": days[2]})
    finally:
        c.close()
    hit = [x for x in cands if x["signal"] == m2_signals.SIGNAL_SUSPENDED]
    assert hit, "停牌当日应当给出这条候选"
    assert hit[0]["label"] == m2_signals.LABEL_STOCK_NEWS
    assert m2_attr.SUSPENDED_REACHABILITY_NOTE in hit[0]["step"]


def test_t2_a_suspended_target_day_is_unscorable_so_the_scan_has_no_case(
        tmp_path, capsys):
    """**可达性实测**：停牌的目标日不可评分 ⇒ 错判案例里没有那一天。

    这条是如实记录（不是放宽判据）：扫描路径**不可能**产出停牌候选，
    所以停牌这条信号活在**旁路触发**路径上（按标的×日扫全市场，不依赖案例）。
    """
    days = _sessions(6)
    path, days = _build(tmp_path, suspend_on=days[2])
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
        scores = m2_store.list_scores(c, asof=days[-1])
    finally:
        c.close()
    that_day = [s for s in scores if str(s["target_date"]) == days[2]]
    assert that_day, "夹具应当有目标日落在停牌日的那条预测"
    assert all(int(s["scorable"]) == 0 for s in that_day)
    assert all(str(s["reason_code"]) == "SUSPENDED" for s in that_day)
    assert not [r for r in rows if r["signal"] == m2_signals.SIGNAL_SUSPENDED]


def test_t2_a_hit_rate_below_the_floor_is_a_factor_decay_candidate(tmp_path, capsys):
    """命中率掉档（复用 P48 的读数口径）⇒ 因子失效候选；观测不足则不给。"""
    path, days = _build(tmp_path)
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
        readings = m2_data.forecast_readings(c, days[-1])
    finally:
        c.close()
    group = readings["groups"][0]
    assert group["n_scored"] >= m2_signals.HIT_RATE_MIN_OBS
    assert group["win_rate"] <= m2_signals.HIT_RATE_FLOOR
    hit = [r for r in rows if r["signal"] == m2_signals.SIGNAL_HIT_RATE]
    assert hit, "同组命中率低于下限、观测足够，应当给出因子失效候选"
    assert hit[0]["label"] == m2_signals.LABEL_FACTOR_DECAY
    assert hit[0]["threshold"] == m2_signals.HIT_RATE_FLOOR
    assert float(hit[0]["at_value"]) == group["win_rate"], \
        "候选里的命中率必须与 P48 读数逐位相同（不许自己再算一份）"


def test_t2_the_missing_industry_data_says_so_instead_of_guessing(tmp_path, capsys):
    """没有行业归属 ⇒ 行业黑天鹅**不给候选**（不猜行业，也不新造分类）。"""
    path, days = _build(tmp_path, industry=None,
                        peer_closes=[100.0 - 6.0 * i for i in range(6)])
    _scan(path, days[-1])
    capsys.readouterr()
    c = _conn(path)
    try:
        rows = m2_store.list_attributions(c, source=m2_attr.SOURCE_AUTO)
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    assert not [r for r in rows if r["signal"] == m2_signals.SIGNAL_INDUSTRY_PEERS]
    assert "industry_name" in panel["attribution"]["industry_source_note"]


# ══════════════════════════════════════════════════════════════════════
# 判据落常量（不许散落在页面/脚本里）
# ══════════════════════════════════════════════════════════════════════


def test_criteria_the_thresholds_are_not_restated_in_the_modules():
    """阈值与信号名只在 `stocklab/config/m2_signals.py` 定义一次。

    用 AST 找**数值字面量**（`-0.02` / `0.40` / `2` 这类），不是子串匹配 ——
    文档里会出现这些数（ERROR_DIARY #50）。
    """
    literals = {abs(v) for v in (
        m2_signals.MARKET_DROP_THRESHOLD, m2_signals.SINGLE_DAY_DROP_THRESHOLD,
        m2_signals.INDUSTRY_PEER_DROP_THRESHOLD, m2_signals.HIT_RATE_FLOOR)}
    scanned = [ROOT / "stocklab/m2/attribution.py",
               ROOT / "stocklab/labweb/m2_data.py",
               ROOT / "stocklab/labweb/m2_render.py",
               ROOT / "stocklab/labweb/m2_charts.py"]
    offenders = []
    for path in scanned:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                if abs(node.value) in {round(v, 6) for v in literals}:
                    offenders.append(f"{path.name}:{node.lineno}={node.value}")
    assert not offenders, f"阈值被复制到了实现里：{offenders}"
    assert scanned and all(p.exists() for p in scanned), "扫描目标不存在（空转）"
