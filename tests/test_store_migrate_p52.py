"""P52 前滚：`paper_agent_decisions` 加三列（只加列，不回填历史行）。

判据分两层：

1. **老行的值一个字节都不改**。P37 的 spec 行在同一张表里，前滚不许碰它们 ——
   回填等于用今天的口径改写当时的事实（与 ADR-014 的 `predictions.origin` 同一条纪律）。
2. **新库与老库前滚后形状一致**。新库由 `schema.sql` 直接建出、老库靠 `ALTER TABLE`，
   两条路必须收敛到同一个 shape，否则「你这台机器上能跑」会变成一句依赖历史的话。
"""

from __future__ import annotations

import sqlite3

from stocklab.store.db import connect
from stocklab.store.migrate import (agent_decisions_need_portfolio_columns,
                                   ensure_schema, init_db, schema_status)
from stocklab.store.migrate import SCHEMA_SQL

#: P37 形状的台账表（= P52 之前 schema.sql 里的那一份）。
OLD_DDL = """
CREATE TABLE paper_agent_decisions (
    decision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    arm              TEXT NOT NULL,
    asof             TEXT NOT NULL,
    agent_kind       TEXT NOT NULL CHECK (agent_kind IN ('manual','llm','random')),
    model_id         TEXT NOT NULL,
    prompt_sha256    TEXT NOT NULL,
    seed             INTEGER NOT NULL DEFAULT 0,
    context_sha256   TEXT NOT NULL,
    spec_before_json TEXT NOT NULL,
    spec_after_json  TEXT NOT NULL,
    n_trials         INTEGER NOT NULL DEFAULT 1,
    rejected_json    TEXT NOT NULL DEFAULT '[]',
    rationale        TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE (arm, asof)
);
CREATE TRIGGER trg_paper_agent_decisions_no_update
BEFORE UPDATE ON paper_agent_decisions
BEGIN SELECT RAISE(ABORT, 'paper_agent_decisions is append-only (改错请再审一版)'); END;
CREATE TRIGGER trg_paper_agent_decisions_no_delete
BEFORE DELETE ON paper_agent_decisions
BEGIN SELECT RAISE(ABORT, 'paper_agent_decisions is append-only'); END;
"""

OLD_ROW = ("arm-agent", "2026-09-16", "manual", "manual", "a" * 64, 0, "b" * 64,
           '{"etf_target_pct":10.0}', '{"etf_target_pct":12.0}', 2,
           '[{"field":"etf_target_pct","value":30,"reason":"越界"}]', "手写一版",
           "2026-09-16T16:00:00+08:00")


def _legacy_db(tmp_path):
    """一份**只有 P37 形状**的库：老台账 + 一行历史 spec。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(OLD_DDL)
    conn.execute(
        "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
        " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
        " n_trials, rejected_json, rationale, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", OLD_ROW)
    conn.commit()
    conn.close()
    return path


def _old_row(conn):
    return tuple(conn.execute(
        "SELECT arm, asof, agent_kind, model_id, prompt_sha256, seed,"
        " context_sha256, spec_before_json, spec_after_json, n_trials,"
        " rejected_json, rationale, created_at FROM paper_agent_decisions").fetchone())


def test_legacy_table_is_detected_as_needing_the_migration(tmp_path):
    """只读探测：老形状要报「缺列」，新库不报（doctor 靠它给告警）。"""
    legacy = _legacy_db(tmp_path)
    conn = connect(legacy)
    try:
        assert agent_decisions_need_portfolio_columns(conn) is True
        status = schema_status(conn)
        assert status["markers"]["p52_agent_decisions_portfolio"]["present"] is False
        assert status["ok"] is False
    finally:
        conn.close()

    fresh = tmp_path / "fresh.db"
    init_db(fresh)
    conn = connect(fresh)
    try:
        assert agent_decisions_need_portfolio_columns(conn) is False
        assert schema_status(conn)["markers"]["p52_agent_decisions_portfolio"][
            "present"] is True
    finally:
        conn.close()


def test_migration_adds_columns_and_leaves_the_old_row_byte_identical(tmp_path):
    """前滚加三列；老行**逐字段一个字节都不变**，且默认值让它自解释（`spec`）。"""
    path = _legacy_db(tmp_path)
    conn = connect(path)
    try:
        before = _old_row(conn)
        changed = ensure_schema(path)
        assert any("decision_kind" in c for c in changed), changed
        assert _old_row(conn) == before, "前滚改了历史行 —— 那是改写事实"
        row = dict(conn.execute("SELECT * FROM paper_agent_decisions").fetchone())
        assert row["decision_kind"] == "spec"
        assert row["payload_json"] == "{}" and row["pool_json"] == "{}"
        # 前滚之后再探测应当已经是「不缺列」
        assert agent_decisions_need_portfolio_columns(conn) is False
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path):
    """再跑一次 → 零变更（前滚必须幂等，否则每次写库入口都会改结构）。"""
    path = _legacy_db(tmp_path)
    ensure_schema(path)
    conn = connect(path)
    try:
        before = _old_row(conn)
    finally:
        conn.close()
    assert ensure_schema(path) == []
    conn = connect(path)
    try:
        assert _old_row(conn) == before
    finally:
        conn.close()


def test_old_and_new_databases_converge_on_the_same_shape(tmp_path):
    """老库前滚后与新库的**列集合完全相同**（两条路收敛到同一个 shape）。"""
    legacy = _legacy_db(tmp_path)
    ensure_schema(legacy)
    fresh = tmp_path / "fresh2.db"
    init_db(fresh)

    def cols(path):
        conn = connect(path)
        try:
            return {r["name"] for r in conn.execute(
                "PRAGMA table_info(paper_agent_decisions)")}
        finally:
            conn.close()

    assert cols(legacy) == cols(fresh)
    assert {"decision_kind", "payload_json", "pool_json"} <= cols(fresh)


def test_migrated_legacy_accepts_a_portfolio_decision(tmp_path):
    """前滚之后老库能直接写操盘决策（写库入口自动前滚，人不该被要求先手动迁移）。"""
    from stocklab.paper import agent_decide

    path = _legacy_db(tmp_path)
    # 前滚由**写库入口**负责（`ensure_schema`）；`connect` 本身不迁移。
    ensure_schema(path)
    conn = connect(path)
    try:
        agent_decide.record_portfolio_decision(
            conn, arm="arm-agent", asof="2026-09-17",
            payload={"asof": "2026-09-17", "decisions": [], "cash_pct": 100.0,
                     "rationale": "空仓"},
            pool={"codes": []}, agent_kind="llm", model_id="m",
            prompt_sha256="p" * 64, seed=0, context_sha256="c" * 64,
            now="2026-09-17T16:00:00+08:00")
        row = agent_decide.decision_on(conn, "arm-agent", "2026-09-17")
        assert row["decision_kind"] == "portfolio"
        assert row["payload"]["cash_pct"] == 100.0
        # 同一张表里两段历史并存
        kinds = [r["decision_kind"] for r in agent_decide.load_decisions(
            conn, "arm-agent")]
        assert kinds == ["spec", "portfolio"]
    finally:
        conn.close()


def test_the_check_constraint_is_enforced_on_a_migrated_database(tmp_path):
    """`ALTER TABLE ADD COLUMN` 带的 CHECK 在老库上**真的生效**（不是只写在 schema.sql 里）。"""
    import pytest

    path = _legacy_db(tmp_path)
    ensure_schema(path)
    conn = connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
                " prompt_sha256, seed, context_sha256, spec_before_json,"
                " spec_after_json, created_at, decision_kind)"
                " VALUES ('arm-agent','2026-09-18','llm','m','p',0,'c','{}','{}','t',"
                " 'bogus')")
    finally:
        conn.close()


def test_schema_sql_is_the_new_shape(tmp_path):
    """新库由 `schema.sql` 直接建出 —— 三列必须在 CREATE TABLE 里（不只是迁移补）。"""
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    seg = sql.split("CREATE TABLE IF NOT EXISTS paper_agent_decisions", 1)[1]
    seg = seg.split(");", 1)[0]
    for col in ("decision_kind", "payload_json", "pool_json"):
        assert col in seg, f"{col} 不在 schema.sql 的 CREATE TABLE 里"
    assert "fund_nav_daily" in sql, "基金净值表必须在新库 schema 里"
