"""P50 T6/T7：事件旁路触发（D-45）—— 机械信号、幂等、留痕、不越权。

| # | 判据 | 用例 |
|---|---|---|
| T6 | **旁路触发幂等** | `test_t6_*`：同信号重复触发 ⇒ 台账不增行、返回「已存在」；不同 `asof` 各触发一次；结构防线（表达式唯一索引）单独验 |
| T7 | **触发不越权** | `test_t7_*`：触发后策略状态/账户/参数未变；静态扫描触发路径不调用 approve；网页端无触发按钮 |
| T8 | **缺数据不造数** | `test_t8_*`：没有信号 ⇒ 零写入（连一行都不写），不是「触发了个空的」 |
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

import pytest

from stocklab.cli.main import main
from stocklab.config import m2_signals
from stocklab.labweb import m2_data, m2_render
from stocklab.m2 import bypass as m2_bypass
from stocklab.m2 import config as m2_config
from stocklab.m2 import score as m2_score
from stocklab.m2 import store as m2_store
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-22T16:00:00+08:00"
START = "2026-09-15"
STOCK = "600519"
ACCOUNT = "arm-agent-v1"


def _sessions(n: int, *, start: str = START) -> list[str]:
    base = datetime.date.fromisoformat(start)
    return [(base + datetime.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _db(tmp_path, *, drop: float | None = None, corp: bool = False,
        dq: bool = False, forecasts: bool = True):
    """最小库：一条标的的价格路径 + 可选的机械信号 + 一条已打分的预测。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "bypass.db"
    init_db(path)
    c = connect(path)
    days = _sessions(4)
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES (?,?,'sh','main','stock',?)", (STOCK, "贵州茅台", NOW))
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in days])
    closes = [100.0, 100.0, 100.0 * (1.0 + (drop or 0.0)),
              100.0 * (1.0 + (drop or 0.0)) ** 2]
    rows = []
    for i, (d, v) in enumerate(zip(days, closes)):
        pre = closes[i - 1]
        rows.append((STOCK, d, v, v, v, v, pre, 100, "none", 0, "x", NOW))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, pre_close,"
        " volume, adj_mode, is_suspended, source, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    if corp:
        c.execute("INSERT INTO corp_actions (code, cqr, content, source,"
                  " first_seen, last_seen) VALUES (?,?,?,'x',?,?)",
                  (STOCK, days[2], f"10{m2_signals.CORP_ACTION_TEXT_WHITELIST[0]}3股",
                   NOW, NOW))
    if dq:
        c.execute("INSERT INTO data_quality (date, source, code, issue_type,"
                  " severity, first_seen, last_seen) VALUES (?,?,?,?,'error',?,?)",
                  (days[2], "tencent", STOCK,
                   m2_signals.DATA_QUALITY_TYPE_WHITELIST[0], NOW, NOW))
    for i in range(len(days) - 1):
        m2_store.insert_forecast(
            c, plugin_id="m2_a3", channel="A", account_id=ACCOUNT, asof=days[i],
            code=STOCK,
            payload={"range_80": [0.01, 10000.0],
                     "direction": {"up": 0.6, "flat": 0.2, "down": 0.2},
                     "invalidate_if": None, "na_reasons": [], "schema_version": "1"},
            script_id=1, script_version="s1", input_sha256=f"{i:064d}", now=NOW)
    c.commit()
    if forecasts:
        m2_score.score_all(c, asof=days[-1], now=NOW)
    c.close()
    return path, days


def _conn(path):
    return connect(path)


def _scan(path, *args) -> int:
    return main(["m2", "bypass", "scan", *args, "--db", str(path), "--now", NOW])


def _events(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM system_events WHERE module = ? ORDER BY event_id",
        (m2_signals.BYPASS_MODULE,))]


def _snapshot(conn, tables) -> dict:
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}


# ══════════════════════════════════════════════════════════════════════
# T6 —— 幂等：同信号只触发一次（结构性防线）
# ══════════════════════════════════════════════════════════════════════


def test_t6_the_same_signal_triggers_once_and_returns_already(tmp_path, capsys):
    path, days = _db(tmp_path, drop=m2_signals.SINGLE_DAY_DROP_THRESHOLD)
    assert _scan(path, "--asof", days[2]) == 0
    out = capsys.readouterr().out
    assert '"fired"' in out or "fired" in out
    c = _conn(path)
    try:
        first = _events(c)
    finally:
        c.close()
    assert len(first) >= 1
    assert _scan(path, "--asof", days[2]) == 0
    second_out = capsys.readouterr().out
    c = _conn(path)
    try:
        second = _events(c)
    finally:
        c.close()
    assert len(second) == len(first), "同一信号重放不增行"
    assert m2_config.STATUS_ALREADY in second_out


def test_t6_a_different_asof_fires_again(tmp_path, capsys):
    """不同 `asof` 各触发一次（幂等键含 `asof`，不是全局一次性）。"""
    path, days = _db(tmp_path, drop=m2_signals.SINGLE_DAY_DROP_THRESHOLD)
    _scan(path, "--asof", days[2])
    n1 = len(_events(_conn(path)))
    _scan(path, "--asof", days[-1])
    n2 = len(_events(_conn(path)))
    capsys.readouterr()
    assert n2 > n1


def test_t6_the_fingerprint_is_semantic_not_the_measured_value(tmp_path):
    """同一天同一标的的跌幅**被数据修正**（-6% → -8%）⇒ 指纹不变、不增行。

    指纹里放实测值的话，「同一条信号」会随着每次重采换一个键 —— 幂等就没了
    （ADR-005：幂等键取语义不取呈现）。
    """
    path, days = _db(tmp_path, drop=-0.06)
    _scan(path, "--asof", days[2])
    n1 = len(_events(_conn(path)))
    c = _conn(path)
    try:
        c.execute("UPDATE bars_daily SET close = ? WHERE code = ? AND date = ?",
                  (92.0, STOCK, days[2]))
        c.commit()
    finally:
        c.close()
    _scan(path, "--asof", days[2])
    assert len(_events(_conn(path))) == n1


def test_t6_the_unique_index_is_the_defense(tmp_path):
    """绕接口裸写两条同指纹事件 ⇒ 被**表达式唯一索引**拒（结构防线）。"""
    import sqlite3

    path, days = _db(tmp_path, drop=-0.06)
    c = _conn(path)
    try:
        fp = m2_bypass.signal_fingerprint(
            m2_signals.SIGNAL_STOCK_PCT_CHG, STOCK, days[2], "")
        repo.log_event(c, m2_signals.BYPASS_MODULE, "warn", "第一条",
                       context={"fingerprint": fp}, now=NOW)
        with pytest.raises(sqlite3.IntegrityError):
            repo.log_event(c, m2_signals.BYPASS_MODULE, "warn", "第二条",
                           context={"fingerprint": fp}, now=NOW)
    finally:
        c.close()


def test_t6_the_ledger_keeps_the_signal_text_and_the_step(tmp_path):
    """留痕带**信号原文**与「算到哪一步」，且写在既有台账（不新造事件表）。"""
    path, days = _db(tmp_path, drop=m2_signals.SINGLE_DAY_DROP_THRESHOLD)
    _scan(path, "--asof", days[2])
    c = _conn(path)
    try:
        events = _events(c)
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    import json

    ctx = json.loads(events[0]["context_json"])
    assert ctx["fingerprint"] and ctx["step"] and ctx["signal"]
    assert ctx["threshold"] == m2_signals.SINGLE_DAY_DROP_THRESHOLD
    assert ctx["at_value"] is not None
    assert events[0]["message"].strip()
    assert m2_signals.BYPASS_MODULE in events[0]["module"]
    # 没有新造事件表：留痕就在既有的 system_events 里
    assert "system_events" in tables and "m2_events" not in tables


def test_t8_no_signal_means_no_row_at_all(tmp_path, capsys):
    """没有任何机械信号 ⇒ **一个字节都不写**（不是「触发了个空的」）。"""
    path, days = _db(tmp_path, drop=-0.001)
    assert _scan(path, "--asof", days[2]) == 0
    capsys.readouterr()
    assert _events(_conn(path)) == []


# ══════════════════════════════════════════════════════════════════════
# T7 —— 触发不越权：不改状态、不改账户、不 approve、网页无按钮
# ══════════════════════════════════════════════════════════════════════


def test_t7_the_trigger_changes_no_state_no_account_no_params(tmp_path, capsys):
    """触发前后：插桩/审计/账户/周期/轮次/事件六张表逐行相等。"""
    path, days = _db(tmp_path, drop=m2_signals.SINGLE_DAY_DROP_THRESHOLD, corp=True)
    tables = ("plugin_scripts", "plugin_audit", "paper_accounts",
              "validation_cycles", "validation_rounds", "validation_events",
              "m2_forecasts", "m2_forecast_scores")
    c = _conn(path)
    try:
        before = _snapshot(c, tables)
    finally:
        c.close()
    assert _scan(path, "--asof", days[2]) == 0
    capsys.readouterr()
    c = _conn(path)
    try:
        after = _snapshot(c, tables)
    finally:
        c.close()
    assert after == before, "触发动了不该动的东西"


def test_t7_the_trigger_path_never_calls_approve():
    """静态扫描：触发路径（`m2/bypass.py` / `m2/attribution.py`）不碰执行路径。"""
    for name in ("bypass.py", "attribution.py"):
        source = (ROOT / "stocklab/m2" / name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=name)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not [m for m in imported if "lifecycle" in m or "plugin.store" in m], \
            f"{name} import 了执行/上线路径"
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "approve" not in names, f"{name} 出现了 approve 调用路径"
        assert "INSERT INTO" not in source, f"{name} 自己拼了 SQL 写库"


def test_t7_the_page_has_no_trigger_button_and_only_lists_the_events(tmp_path):
    """网页端**不提供触发按钮**：页面只列出已落库的事件（触发走 CLI）。"""
    path, days = _db(tmp_path, drop=m2_signals.SINGLE_DAY_DROP_THRESHOLD)
    _scan(path, "--asof", days[2])
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[2])
        html = m2_render.m2_page(panel, base="/lab", built_at=NOW)
        text = m2_render.m2_text(panel)
    finally:
        c.close()
    assert panel["bypass"]["n_events"] >= 1
    assert "m2 bypass scan" in html and "m2 bypass scan" in text
    for banned in ("<button", "<form", "<input", 'method="post"'):
        assert banned not in html, f"页面上出现了写入口：{banned}"


def test_t7_the_rescore_uses_the_existing_scorer_and_writes_no_forecast(tmp_path,
                                                                      capsys):
    """「重打分」= 走既有打分器（`m2/score.py`），且**不写预测**。

    预测已经全部打过分的库上触发 ⇒ 新分数 0 行（既有判据：一条预测一行分数）。
    """
    path, days = _db(tmp_path, drop=m2_signals.SINGLE_DAY_DROP_THRESHOLD)
    c = _conn(path)
    try:
        before_scores = c.execute(
            "SELECT COUNT(*) FROM m2_forecast_scores").fetchone()[0]
        before_forecasts = c.execute(
            "SELECT COUNT(*) FROM m2_forecasts").fetchone()[0]
    finally:
        c.close()
    assert _scan(path, "--asof", days[-1]) == 0
    out = capsys.readouterr().out
    c = _conn(path)
    try:
        after_scores = c.execute(
            "SELECT COUNT(*) FROM m2_forecast_scores").fetchone()[0]
        after_forecasts = c.execute(
            "SELECT COUNT(*) FROM m2_forecasts").fetchone()[0]
    finally:
        c.close()
    assert after_scores == before_scores and after_forecasts == before_forecasts
    assert "rescored" in out


def test_t7_an_unscored_forecast_is_scored_by_the_bypass_trigger(tmp_path, capsys):
    """正例：还有未打分的预测时，旁路触发**提前**把它打掉（不等周期结束）。"""
    path, days = _db(tmp_path, drop=m2_signals.SINGLE_DAY_DROP_THRESHOLD,
                     forecasts=False)
    c = _conn(path)
    try:
        assert c.execute("SELECT COUNT(*) FROM m2_forecast_scores").fetchone()[0] == 0
    finally:
        c.close()
    assert _scan(path, "--asof", days[-1]) == 0
    capsys.readouterr()
    c = _conn(path)
    try:
        n = c.execute("SELECT COUNT(*) FROM m2_forecast_scores").fetchone()[0]
    finally:
        c.close()
    assert n > 0, "旁路触发应当用既有打分器把到期的预测补上"
