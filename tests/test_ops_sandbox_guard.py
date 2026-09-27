"""P93 T5：巡检第五块 `sandbox_guard`（`ops/business.py`）。

要钉死的六件事：

1. **键只增不减**：`business` 由 P92 的四块 → 五块，P92 那四块一字不动；
2. **形状固定**：`{"status","n_events","max_delta_bytes","max_peak_bytes",
   "limits","reason"}`，状态取 `ok` / `high` / `skipped`（走 `patrol` 的词汇表）；
3. **空表 ⇒ `skipped`，不算异常**（真库现在就是空表，空是预期）；
4. **`high` 只展示、不进退出码** —— `fuse` 仍是唯一进退出码的业务读数；
5. **只读**：读 `plugin_resource_events` 不写任何东西；
6. **简报一行**：`brief_text` 多一行 `sandbox_guard`，而 `summary_line()` 不变。

全程离线、只用临时库。
"""

from __future__ import annotations

import sqlite3

from stocklab.config import limits
from stocklab.ops import business, patrol
from stocklab.plugin import store as plugin_store
from stocklab.store.db import connect
from tests.test_ops_patrol import NOW, _db as green_db

GUARD_STATUSES = frozenset({patrol.OK, "high", patrol.SKIPPED})
GUARD_KEYS = frozenset({"status", "n_events", "max_delta_bytes",
                        "max_peak_bytes", "limits", "reason"})


def _event(db, *, outcome: str, delta: int, peak: int, plugin_id: str = "1"):
    c = connect(db)
    try:
        return plugin_store.record_resource_event(
            c, plugin_id=plugin_id, outcome=outcome, rss_delta_bytes=delta,
            rss_peak_bytes=peak, duration_ms=1.0, detail=None, now=NOW)
    finally:
        c.close()


def _guard(db, **kw) -> dict:
    conn = patrol.ro_connect(db)
    try:
        return business.sandbox_guard(conn, **kw)
    finally:
        conn.close()


# ---------- 空表 / 缺表 ----------

def test_empty_table_is_skipped_not_an_anomaly(tmp_path):
    out = _guard(green_db(tmp_path))
    assert out["status"] == patrol.SKIPPED
    assert out["n_events"] == 0
    assert out["max_delta_bytes"] is None
    assert out["max_peak_bytes"] is None
    assert "空" in out["reason"]
    assert "不计异常" in out["reason"]


def test_missing_table_is_skipped(tmp_path):
    """老库未前滚：表不存在 ⇒ 跳过，不炸（沿用 `_one` 的既有纪律）。"""
    c = sqlite3.connect(":memory:")
    try:
        out = business.sandbox_guard(c)
    finally:
        c.close()
    assert out["status"] == patrol.SKIPPED
    assert "不计异常" in out["reason"]


# ---------- 形状与读数 ----------

def test_shape_is_fixed(tmp_path):
    out = _guard(green_db(tmp_path))
    assert set(out) == GUARD_KEYS


def test_status_uses_the_patrol_vocabulary(tmp_path):
    db = green_db(tmp_path)
    assert _guard(db)["status"] in GUARD_STATUSES
    _event(db, outcome="ok", delta=10, peak=20)
    assert _guard(db)["status"] in GUARD_STATUSES
    _event(db, outcome="resource", delta=30, peak=40)
    assert _guard(db)["status"] in GUARD_STATUSES


def test_ok_events_report_the_maxima(tmp_path):
    db = green_db(tmp_path)
    for delta, peak in ((10, 100), (300, 400), (20, 200)):
        _event(db, outcome="ok", delta=delta, peak=peak)
    out = _guard(db)
    assert out["status"] == patrol.OK
    assert out["n_events"] == 3
    assert out["max_delta_bytes"] == 300
    assert out["max_peak_bytes"] == 400


def test_a_resource_event_makes_it_high(tmp_path):
    db = green_db(tmp_path)
    _event(db, outcome="ok", delta=10, peak=20)
    _event(db, outcome="resource", delta=600 * 1024 ** 2, peak=700 * 1024 ** 2)
    out = _guard(db)
    assert out["status"] == "high"
    assert out["n_events"] == 2
    assert "1" in out["reason"]         # 点名有几次越界


def test_timeout_is_not_high(tmp_path):
    """`high` 只由 `outcome="resource"` 触发 —— 超时是另一条通道（已有退出码语义）。"""
    db = green_db(tmp_path)
    _event(db, outcome="timeout", delta=10, peak=20)
    assert _guard(db)["status"] == patrol.OK


def test_window_keeps_only_the_newest_events(tmp_path):
    db = green_db(tmp_path)
    _event(db, outcome="ok", delta=10_000, peak=999_999)      # 最老的那条
    for i in range(24):
        _event(db, outcome="ok", delta=i, peak=i)
    out = _guard(db, limit=20)
    assert out["n_events"] == 20
    assert out["max_delta_bytes"] == 23          # 最老的 10_000 没进窗口
    assert out["max_peak_bytes"] == 23


def test_limits_come_from_the_locked_constants(tmp_path):
    out = _guard(green_db(tmp_path))
    assert out["limits"] == {
        "call": limits.PLUGIN_CALL_RSS_LIMIT_BYTES,
        "process": limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES,
    }


def test_reason_is_human_readable_and_non_empty(tmp_path):
    db = green_db(tmp_path)
    for outcome in ("ok", "timeout", "resource"):
        _event(db, outcome=outcome, delta=10, peak=20)
    out = _guard(db)
    assert isinstance(out["reason"], str) and out["reason"].strip()
    assert str(out["n_events"]) in out["reason"]


# ---------- 只展示：不进退出码 ----------

def test_high_does_not_change_the_exit_code(tmp_path):
    """`fuse` 是唯一进退出码的业务读数（D6/P92 D2）—— 加一条越界事件也不改。"""
    db = green_db(tmp_path)
    before = patrol.check_db(db, NOW)
    _event(db, outcome="resource", delta=900 * 1024 ** 2, peak=950 * 1024 ** 2)
    after = patrol.check_db(db, NOW)
    assert after["exit_code"] == before["exit_code"]
    assert after["business"]["sandbox_guard"]["status"] == "high"
    assert "sandbox_guard" not in [a.get("kind") for a in after["anomalies"]]


# ---------- 只读 ----------

def test_the_block_writes_nothing(tmp_path):
    db = green_db(tmp_path)
    _event(db, outcome="ok", delta=10, peak=20)

    def counts():
        c = connect(db)
        try:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'")]
            return {n: int(c.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0])
                    for n in names}
        finally:
            c.close()

    before = counts()
    _guard(db)
    assert counts() == before


# ---------- 组装 / 简报 / summary_line ----------

def test_build_business_exposes_five_blocks(tmp_path):
    conn = patrol.ro_connect(green_db(tmp_path))
    try:
        out = business.build_business(conn, latest=None, db_path=None)
    finally:
        conn.close()
    assert tuple(out) == business.BLOCK_ORDER
    assert "sandbox_guard" in business.BLOCK_ORDER
    for name in ("risk_screen", "fuse", "candidate_status", "freshness"):
        assert name in out, "P92 那四块必须一字不动地留着"


def test_brief_text_has_a_sandbox_guard_line(tmp_path):
    payload = {
        "now": NOW, "today": "2026-09-22", "session_day": {"why": "w"},
        "latest_closed_session": {"date": "2026-09-21"}, "exit_code": 0,
        "anomalies": [], "plan": {"skipped": []}, "steps": [],
        "business": {
            "risk_screen": {"status": "ok", "reason": "r"},
            "fuse": {"status": "ok", "reason": "f"},
            "candidate_status": {"status": "ok", "reason": "c"},
            "freshness": {"status": "ok", "reason": "n"},
            "sandbox_guard": {"status": "high", "reason": "越界 1 次"},
        },
    }
    text = business.brief_text(payload)
    assert "`sandbox_guard`：high —— 越界 1 次" in text


def test_summary_line_is_unchanged_by_the_new_block(tmp_path):
    """D7：`summary_line()` 与既有字段一字不动（launchd 日志与回执文案不许变）。"""
    db = green_db(tmp_path)
    payload = patrol.check_db(db, NOW)
    without = dict(payload)
    without["business"] = {k: v for k, v in payload["business"].items()
                           if k != "sandbox_guard"}
    assert patrol.summary_line(payload) == patrol.summary_line(without)
