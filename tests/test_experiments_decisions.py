"""Task 43：实验决策落库（`experiment_decisions`，append-only）。

这一层要钉住四件事，每一件都对应一条纪律：

  - **可追溯**：跑完一场实验，决策**自动**落库，「哪个变体在哪段是 WIN/LOSE」可 SQL 查；
  - **只落决策、不落预测**：`predictions` / `verifications` 的**行数一行都不变**
    （P8 §1.1「变体预测不落库」这条架构决策没有任何松动）；
  - **append-only**：UPDATE / DELETE 被触发器拒绝，结论被推翻时追加新行；
  - **幂等语义写清楚**：同一份报告重跑 → `identical` 不重复写；
    换了区间/配置 → 新 sha256 → 追加；同键内容不同 → 抛 `DecisionConflict`，不覆盖。
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import main
from stocklab.experiments.decisions import (DecisionConflict,
                                            decisions_for,
                                            decisions_from_report,
                                            record_decisions)
from stocklab.store.db import connect
from tests.test_experiments_runner import CODE, N, _days, _env, _run

#: 「只落决策不落预测」的判据表 —— 沿用 runner 那套（预测/验证/特征/成交）。
_PRED_TABLES = ("predictions", "verifications", "features_daily", "sim_trades")


def _counts(conn, tables=_PRED_TABLES) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in tables}


def _report(tmp_path, name="a.db", variant="rw-mu0"):
    conn = _env(tmp_path, name=name)
    try:
        return _run(conn, variant=variant)
    finally:
        conn.close()


def _conn(tmp_path, name="a.db"):
    """连库（不存在就先建 —— 前滚走的是 `migrate.init_db` 的同一条 schema）。"""
    from stocklab.store.migrate import init_db

    path = tmp_path / name
    if not path.exists():
        init_db(path)
    return connect(path)


# ---------- 1. DDL / append-only ----------

def test_decisions_table_is_append_only(tmp_path):
    conn = _conn(tmp_path)
    try:
        conn.execute(
            "INSERT INTO experiment_decisions (variant_id, split, metric,"
            " metric_version, delta, ci_low, ci_high, gate_status, decision,"
            " report_sha256, created_at) VALUES ('v','validate','direction',"
            " 'm', 0.1, 0.0, 0.2, 'WIN', 'promoted', 'sha', 'now')")
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE experiment_decisions SET delta = 0.9")
        with pytest.raises(Exception, match="append-only"):
            conn.execute("DELETE FROM experiment_decisions")
        # 触发器 ABORT 之后连接必须仍可用（ERROR_DIARY 2026-09-14 的教训）
        assert conn.execute("SELECT COUNT(*) c FROM experiment_decisions"
                            ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_decisions_table_rejects_unknown_split_metric_and_status(tmp_path):
    """CHECK 约束是**结构**防线：写错枚举值进不去，而不是靠调用方自觉。"""
    conn = _conn(tmp_path)
    base = ("INSERT INTO experiment_decisions (variant_id, split, metric,"
            " metric_version, delta, ci_low, ci_high, gate_status, decision,"
            " report_sha256, created_at) VALUES"
            " ('v','{split}','{metric}','m',0.1,0.0,0.2,'{gate}','{dec}','sha','now')")
    try:
        with pytest.raises(Exception):
            conn.execute(base.format(split="holdout", metric="direction",
                                     gate="WIN", dec="promoted"))
        with pytest.raises(Exception):
            conn.execute(base.format(split="validate", metric="accuracy",
                                     gate="WIN", dec="promoted"))
        with pytest.raises(Exception):
            conn.execute(base.format(split="validate", metric="direction",
                                     gate="MAYBE", dec="promoted"))
        with pytest.raises(Exception):
            conn.execute(base.format(split="validate", metric="direction",
                                     gate="WIN", dec="maybe"))
    finally:
        conn.close()


# ---------- 2. 从报告抽决策 ----------

def test_decisions_from_report_extracts_two_rows_per_split(tmp_path):
    rep = _report(tmp_path)
    rows = decisions_from_report(rep, report_sha256="deadbeef")
    splits = sorted(rep["splits"])
    assert len(rows) == 2 * len(splits)
    assert {r["split"] for r in rows} == set(splits)
    assert {r["metric"] for r in rows} == {"direction", "brier"}
    for r in rows:
        assert r["variant_id"] == rep["variant"]["name"]
        assert r["metric_version"] == rep["metric_version"]
        assert r["decision"] == rep["verdict"]["status"]
        assert r["gate_status"] == rep["splits"][r["split"]]["gate"]["status"]
        assert r["report_sha256"] == "deadbeef"
        stat = rep["splits"][r["split"]]["paired"][r["metric"]]
        assert r["delta"] == stat["mean"]
        assert r["ci_low"] == stat["ci95"][0]
        assert r["ci_high"] == stat["ci95"][1]


def test_gate_status_is_shared_by_the_two_metric_rows_of_a_split(tmp_path):
    """`gate` 同时看两个指标才给结论 —— 所以同段两行共享一个 `gate_status`，
    各自的 `delta` / CI 独立。这条钉住的是「不要把 gate 误当成 per-metric 的量」。"""
    rep = _report(tmp_path)
    rows = decisions_from_report(rep, report_sha256="x")
    for split in rep["splits"]:
        two = [r for r in rows if r["split"] == split]
        assert len({r["gate_status"] for r in two}) == 1
        assert len({r["metric"] for r in two}) == 2


# ---------- 3. 幂等 / 追加 / 冲突 ----------

def test_record_decisions_is_idempotent_for_the_same_report(tmp_path):
    rep = _report(tmp_path)
    rows = decisions_from_report(rep, report_sha256="sha-A")
    conn = _conn(tmp_path)
    try:
        first = record_decisions(conn, rows, now="T1")
        assert first == {"inserted": len(rows), "identical": 0}
        again = record_decisions(conn, rows, now="T2")
        assert again == {"inserted": 0, "identical": len(rows)}
        assert len(decisions_for(conn)) == len(rows)
    finally:
        conn.close()


def test_a_changed_report_appends_new_rows_instead_of_rewriting(tmp_path):
    """换了区间/配置 → 新 sha256 → **追加**。旧结论原样保留（append-only 的本意）。"""
    rep = _report(tmp_path)
    conn = _conn(tmp_path)
    try:
        record_decisions(conn, decisions_from_report(rep, report_sha256="sha-A"))
        n1 = len(decisions_for(conn))
        record_decisions(conn, decisions_from_report(rep, report_sha256="sha-B"))
        n2 = len(decisions_for(conn))
        assert n2 == 2 * n1
        assert {r["report_sha256"] for r in decisions_for(conn)} == {"sha-A", "sha-B"}
    finally:
        conn.close()


def test_same_key_with_different_content_is_refused_not_overwritten(tmp_path):
    """幂等键相同、内容不同 → 抛错。这是「哈希撞了 / 有人绕过本模块」的信号，
    静默覆盖会让台账失去可信度。"""
    rep = _report(tmp_path)
    rows = decisions_from_report(rep, report_sha256="sha-A")
    conn = _conn(tmp_path)
    try:
        record_decisions(conn, rows)
        tampered = [dict(r) for r in rows]
        tampered[0]["delta"] = (tampered[0]["delta"] or 0.0) + 1.0
        with pytest.raises(DecisionConflict):
            record_decisions(conn, tampered)
        # 事务整体回滚：原有行数不变，被篡改的那一行也没有进去
        assert len(decisions_for(conn)) == len(rows)
    finally:
        conn.close()


def test_conflict_rolls_back_the_whole_batch(tmp_path):
    """部分写入的台账比不写更糟（会让人以为「只判了这两段」）—— 要么全写要么不写。"""
    rep = _report(tmp_path)
    rows = decisions_from_report(rep, report_sha256="sha-A")
    conn = _conn(tmp_path)
    try:
        record_decisions(conn, rows)
        fresh = decisions_from_report(rep, report_sha256="sha-B")
        fresh[-1] = {**fresh[-1], "metric_version": fresh[0]["metric_version"]}
        # 让最后一行与已有行撞键（同 sha-A、同 split/metric/version）但内容不同
        fresh[-1]["report_sha256"] = "sha-A"
        fresh[-1]["gate_status"] = ("LOSE" if fresh[-1]["gate_status"] != "LOSE"
                                    else "WIN")
        before = len(decisions_for(conn))
        with pytest.raises(DecisionConflict):
            record_decisions(conn, fresh)
        assert len(decisions_for(conn)) == before       # 前面几行也没写进去
    finally:
        conn.close()


# ---------- 4. 只落决策、不落预测 ----------

def test_recording_decisions_writes_no_prediction_rows(tmp_path):
    """P8 §1.1 继续有效：变体**预测**一行都不落库，落库的只有**决策**。"""
    conn = _env(tmp_path)
    try:
        before = _counts(conn)
        rep = _run(conn)
        after_run = _counts(conn)
        record_decisions(conn, decisions_from_report(rep, report_sha256="s"))
        after_record = _counts(conn)
    finally:
        conn.close()
    assert after_run == before == after_record
    assert all(v == 0 for v in after_record.values())


# ---------- 5. 可查 ----------

def test_decisions_are_queryable_by_variant_split_and_gate(tmp_path):
    """「哪个变体在哪段是 WIN/LOSE」从此可 SQL 查 —— 这是本表的**存在理由**。"""
    rep = _report(tmp_path)
    conn = _conn(tmp_path)
    try:
        record_decisions(conn, decisions_from_report(rep, report_sha256="s"))
        allrows = decisions_for(conn)
        assert len(allrows) == 2 * len(rep["splits"])

        only_validate = decisions_for(conn, variant_id="rw-mu0", split="validate")
        assert {r["split"] for r in only_validate} == {"validate"}
        assert len(only_validate) == 2

        wins = decisions_for(conn, gate_status="WIN")
        for r in wins:
            assert r["gate_status"] == "WIN"
        expected = sum(1 for r in allrows if r["gate_status"] == "WIN")
        assert len(wins) == expected

        assert decisions_for(conn, variant_id="nope") == []
    finally:
        conn.close()


def test_rows_survive_and_are_readable_after_reconnect(tmp_path):
    """落库是可查询的**库里**的状态，不是内存里的对象。"""
    rep = _report(tmp_path)
    conn = _conn(tmp_path)
    record_decisions(conn, decisions_from_report(rep, report_sha256="s"))
    conn.close()
    conn2 = _conn(tmp_path)
    try:
        rows = decisions_for(conn2)
        assert rows and all(r["created_at"] for r in rows)
        assert all(isinstance(r["delta"], float) or r["delta"] is None
                   for r in rows)
    finally:
        conn2.close()


# ---------- 6. CLI：`experiment run` 结束后**自动**写入 ----------

def _argv(tmp_path, **over):
    days = _days()
    a = {"--variant": "rw-mu0", "--from": days[len(days) // 3],
         "--to": days[-1], "--db": str(tmp_path / "a.db"), "--code": CODE}
    a.update(over)
    argv = ["experiment", "run"]
    for k, v in a.items():
        argv += [k, str(v)]
    return argv


def test_cli_experiment_run_records_decisions_automatically(tmp_path, capsys):
    _env(tmp_path).close()
    out = tmp_path / "exp" / "r.md"
    assert main(_argv(tmp_path, **{"--report": str(out)})) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decisions_recorded"]["inserted"] > 0
    assert payload["decisions_recorded"]["identical"] == 0

    conn = _conn(tmp_path)
    try:
        rows = decisions_for(conn)
    finally:
        conn.close()
    assert rows
    # 落库的决策与 stdout 报告同源：sha256 对得上
    assert {r["report_sha256"] for r in rows} == {payload["sha256_json"]}
    assert {r["decision"] for r in rows} == {payload["verdict"]}


def test_cli_rerun_records_identical_decisions_not_duplicates(tmp_path, capsys):
    _env(tmp_path).close()
    counts = []
    for name in ("r1", "r2"):
        assert main(_argv(tmp_path, **{"--report": str(tmp_path / name / "r.md")})) == 0
        payload = json.loads(capsys.readouterr().out)
        counts.append(payload["decisions_recorded"])
    assert counts[0]["inserted"] > 0
    assert counts[1] == {"inserted": 0, "identical": counts[0]["inserted"]}

    conn = _conn(tmp_path)
    try:
        assert len(decisions_for(conn)) == counts[0]["inserted"]
    finally:
        conn.close()


def test_cli_gives_an_actionable_error_when_the_db_is_not_migrated(tmp_path, capsys):
    """没前滚的库 → 明确让人去跑 `db init`，而不是抛个 sqlite3 栈。"""
    from tests.test_predict_service import _to_date, _to_ord, bars, seed

    days = _days()
    bars_by_code = {CODE: bars(code=CODE, n=N, base=10.0)}
    seed(tmp_path / "a.db", bars_by_code, cal_dates=days)
    conn = connect(tmp_path / "a.db")
    conn.execute("DROP TABLE experiment_decisions")     # 模拟未前滚
    conn.close()
    rc = main(_argv(tmp_path, **{"--report": str(tmp_path / "r.md")}))
    err = capsys.readouterr().err
    assert rc == 2
    assert "db init" in err
    assert _to_date(_to_ord(days[0])) == days[0]        # 用一下导入，防 lint 抱怨
