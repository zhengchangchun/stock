"""`ops patrol` 的**业务巡检块**（P92 / B5）护栏。

五条主张要在这里被钉死：

1. **形状固定、只增不减**：`business` 只有五个子键，每个都有 `reason`（人话）
   （P92 四块 ＋ P93 的 `sandbox_guard`）；
2. **熔断只读**：只用 `m2.selfeval.fuse_verdict`，**绝不**调 `m2 cycle fuse-check`
   —— 那条命令会往 append-only 台账追加事件，30 分钟一轮会把台账写花；
3. **候选池状态默认只算不写**：`--update-status` 没开时 `candidate_status_events`
   一行都不许多；
4. **简报非阻断**：写失败记 `brief_error`，退出码一个字不改；
5. **只有熔断计入退出码**：其余三块只展示（盘中每 30 分钟报一次假警＝噪音）。

测试全程离线、不联网；子进程一律用假执行器（`FakeRunner`）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from stocklab.ops import business, patrol
from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from tests.test_ops_patrol import NOW, FakeRunner, _db as green_db

L = "2026-09-21"
TODAY = "2026-09-22"

BLOCK_KEYS = ("risk_screen", "fuse", "candidate_status", "freshness",
              "sandbox_guard")            # P93 追加第五块（键只增不减）
STATUSES = frozenset({patrol.OK, patrol.MISSING, patrol.STALE, patrol.SKIPPED,
                      patrol.UNKNOWN, patrol.LAG, patrol.TRIPPED, patrol.RISKY,
                      patrol.HIGH})


# ---------- 夹具 ----------

def _db(tmp_path) -> Path:
    """一张**空壳**库：建好 schema，不塞任何业务行。"""
    path = tmp_path / "business.db"
    init_db(path)
    return path


def _snapshot(path: Path, *, asof: str = L, run_kind: str = "light",
              members=(("000333", "short"),), status: str = "观察中") -> int:
    c = connect(path)
    try:
        cur = c.execute(
            "INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
            " created_at) VALUES (?,?,'{}',?)", (asof, run_kind, NOW))
        sid = int(cur.lastrowid)
        for code, pool in members:
            c.execute(
                "INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                " adj_score, reason, risk_json, status, entered_at)"
                " VALUES (?,?,?,1.0,1.0,'r','[]',?,?)",
                (sid, code, pool, status, NOW))
        c.commit()
        return sid
    finally:
        c.close()


def _account(path: Path, account_id: str = "arm-hold", *, live: bool = True,
             holdings=(), date: str = L, nav: bool = True) -> None:
    positions = [{"code": code, "qty": qty, "cost_price": 10.0}
                 for code, qty in holdings]
    c = connect(path)
    try:
        c.execute(
            "INSERT INTO paper_accounts (account_id, arm, start_date, initial_cash,"
            " initial_positions_json, initial_nav, params_json, created_at)"
            " VALUES (?,'hold',?,10000.0,'[]',10000.0,?,?)",
            (account_id, date, json.dumps({"live": live}), NOW))
        if nav:                                 # 净值行 append-only ⇒ 给了 _navs 就别再插
            c.execute(
                "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
                " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
                " created_at) VALUES (?,?,10000.0,?,10000.0,10000.0,0.0,0.0,0.0,"
                "10000.0,?)", (account_id, date, json.dumps(positions), NOW))
        c.commit()
    finally:
        c.close()


def _cycle(path: Path, *, account_id: str = "arm-hold", start: str = "2026-09-01",
           ended: bool = False) -> int:
    """一轮未收尾的验证周期（默认起点早于净值序列，让整段净值都进熔断窗口）。"""
    c = connect(path)
    try:
        cur = c.execute(
            "INSERT INTO validation_cycles (script_id, account_id, planned_rounds,"
            " planned_days, params_json, criteria_text, start_date, created_at)"
            " VALUES (1,?,2,5,'{}','判据原文',?,?)", (account_id, start, NOW))
        cid = int(cur.lastrowid)
        if ended:
            c.execute(
                "INSERT INTO validation_events (cycle_id, script_id, kind, at_value,"
                " threshold, criteria_text, reason, created_at)"
                " VALUES (1,1,'validation_end',NULL,NULL,'判据原文','收尾',?)", (NOW,))
        c.commit()
        return cid
    finally:
        c.close()


def _navs(path: Path, navs, *, account_id: str = "arm-hold") -> None:
    """按给定净值序列补净值行，**末行落在 L**（窗口必须整段 <= 最新已收盘交易日）。"""
    last = date.fromisoformat(L)
    c = connect(path)
    try:
        for i, nav in enumerate(navs):
            day = (last - timedelta(days=len(navs) - 1 - i)).isoformat()
            c.execute(
                "INSERT OR REPLACE INTO paper_nav_daily (account_id, date, cash,"
                " positions_json, market_value, nav, drawdown, cum_cost, cum_return,"
                " net_deposits, created_at) VALUES (?,?,0.0,'[]',?,?,0.0,0.0,0.0,"
                "0.0,?)", (account_id, day, nav, nav, NOW))
        c.commit()
    finally:
        c.close()


def _announcement(path: Path, *, notice_date: str, code: str = "000333") -> None:
    c = connect(path)
    try:
        c.execute(
            "INSERT INTO announcements (code, art_code, notice_date, title, source,"
            " fetched_at, created_at, resp_sha256) VALUES (?,?,?,'t','sina',?,?,?)",
            (code, f"AN{notice_date}", notice_date, NOW, NOW, "x"))
        c.commit()
    finally:
        c.close()


def _northbound(path: Path, *, trade_date: str, code: str = "000333") -> None:
    c = connect(path)
    try:
        c.execute(
            "INSERT INTO northbound_holdings (code, trade_date, frequency, source,"
            " fetched_at, created_at, resp_sha256)"
            " VALUES (?,?,'quarterly','eastmoney',?,?,?)",
            (code, trade_date, NOW, NOW, "x"))
        c.commit()
    finally:
        c.close()


def _read(path: Path, *, latest: str | None = L, **kw) -> dict:
    conn = patrol.ro_connect(path)
    try:
        return business.build_business(conn, latest=latest, db_path=path, **kw)
    finally:
        conn.close()


def _rows(path: Path, table: str) -> int:
    c = connect(path)
    try:
        return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        c.close()


def _tables(path: Path) -> dict[str, int]:
    c = connect(path)
    try:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%'")]
        return {n: int(c.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0])
                for n in names}
    finally:
        c.close()


# ---------- 主张 1：形状固定 ----------

def test_business_has_exactly_the_five_blocks(tmp_path):
    out = _read(_db(tmp_path))
    assert tuple(out) == BLOCK_KEYS


def test_every_block_has_a_status_from_the_patrol_vocabulary(tmp_path):
    out = _read(_db(tmp_path))
    for name in BLOCK_KEYS:
        assert out[name]["status"] in STATUSES, name


def test_every_block_has_a_non_empty_human_reason(tmp_path):
    out = _read(_db(tmp_path))
    for name in BLOCK_KEYS:
        reason = out[name]["reason"]
        assert isinstance(reason, str) and reason.strip(), name


def test_shapes_are_fixed_key_sets(tmp_path):
    """键**只增不减**：这五个键集是页面与简报的契约。"""
    out = _read(_db(tmp_path))
    assert set(out["risk_screen"]) == {
        "status", "n_members", "n_checked", "n_risky", "risky", "reason"}
    assert set(out["fuse"]) == {
        "status", "cycle_id", "at_value", "threshold", "tripped", "window", "reason"}
    assert set(out["candidate_status"]) == {
        "status", "counts", "would_set", "applied", "reason"}
    assert set(out["freshness"]) == {
        "status", "announcements", "northbound", "reason"}
    assert set(out["sandbox_guard"]) == {
        "status", "n_events", "max_delta_bytes", "max_peak_bytes", "limits",
        "reason"}


def test_risky_members_are_plain_dicts_with_code_and_note(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    out = _read(db, screen_fn=lambda *a, **k: "银行：毛利率无意义")
    assert out["risk_screen"]["risky"] == [
        {"code": "000333", "note": "银行：毛利率无意义"}]


# ---------- 主张 1b：risk_screen（插桩0 排雷） ----------

def test_risk_screen_without_a_snapshot_is_skipped(tmp_path):
    block = _read(_db(tmp_path))["risk_screen"]
    assert block["status"] == patrol.SKIPPED
    assert block["n_members"] == 0 and block["risky"] == []
    assert "快照" in block["reason"]


def test_risk_screen_checks_every_member_once(tmp_path):
    db = _db(tmp_path)
    _snapshot(db, members=(("000333", "short"), ("000651", "mid"),
                           ("000333", "long")))       # 同一只在两个池 → 只查一次
    seen: list[str] = []

    def fake(conn, code, *, asof, xsec):
        seen.append(code)
        return None

    block = _read(db, screen_fn=fake)["risk_screen"]
    assert seen == ["000333", "000651"]
    assert block["n_members"] == 2 and block["n_checked"] == 2
    assert block["n_risky"] == 0 and block["status"] == patrol.OK


def test_risk_screen_counts_the_hits(tmp_path):
    db = _db(tmp_path)
    _snapshot(db, members=(("000333", "short"), ("600036", "mid")))
    block = _read(db, screen_fn=lambda conn, code, **k:
                  "保险：不适用" if code == "600036" else None)["risk_screen"]
    assert block["status"] == patrol.RISKY
    assert block["n_risky"] == 1 and block["n_checked"] == 2
    assert "1/2" in block["reason"]


def test_risk_screen_without_a_pit_asof_is_skipped(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    block = _read(db, latest=None)["risk_screen"]
    assert block["status"] == patrol.SKIPPED
    assert "判不出" in block["reason"]


def test_risk_screen_without_an_active_plugin_is_unknown_not_ok(tmp_path):
    """插桩0 没有 active 版本 ⇒ **判不了**，不许静默当成「都干净」。"""
    db = _db(tmp_path)
    _snapshot(db)
    c = connect(db)
    try:
        c.execute("INSERT INTO instruments (code, name, market, board, type,"
                  " added_at) VALUES ('000333','美的','sz','main','stock',?)", (NOW,))
        c.commit()
    finally:
        c.close()
    block = _read(db)["risk_screen"]
    assert block["status"] == patrol.UNKNOWN
    assert "插桩0" in block["reason"]


def test_risk_screen_failure_does_not_take_the_patrol_down(tmp_path):
    """排雷抛错 ⇒ 记进 reason，**不**让整块炸掉（巡检是闹钟，不是业务主流程）。"""
    db = _db(tmp_path)
    _snapshot(db)

    def boom(conn, code, *, asof, xsec):
        raise RuntimeError("沙盒炸了")

    block = _read(db, screen_fn=boom)["risk_screen"]
    assert block["status"] == patrol.UNKNOWN
    assert "RuntimeError" in block["reason"] and "沙盒炸了" in block["reason"]


# ---------- 主张 2：熔断只读 ----------

def test_fuse_without_a_cycle_is_skipped(tmp_path):
    block = _read(_db(tmp_path))["fuse"]
    assert block["status"] == patrol.SKIPPED
    assert block["tripped"] is False and block["cycle_id"] is None
    assert "没有进行中的验证周期" in block["reason"]


def test_an_ended_cycle_is_not_in_progress(tmp_path):
    db = _db(tmp_path)
    _cycle(db, ended=True)
    assert _read(db)["fuse"]["status"] == patrol.SKIPPED


def test_fuse_reports_the_measured_value_and_the_threshold_separately(tmp_path):
    db = _db(tmp_path)
    _account(db, nav=False)
    cid = _cycle(db)
    _navs(db, [100.0, 105.0, 103.0])                 # 最深回撤 1.9%
    block = _read(db)["fuse"]
    assert block["status"] == patrol.OK and block["tripped"] is False
    assert block["cycle_id"] == cid
    assert block["at_value"] == pytest.approx(0.019048, abs=1e-6)
    assert block["threshold"] == pytest.approx(0.10)
    assert block["window"] == "2026-09-19 ~ 2026-09-21"
    assert "未触发" in block["reason"]


def test_fuse_trips_when_the_drawdown_reaches_the_threshold(tmp_path):
    db = _db(tmp_path)
    _account(db, nav=False)
    cid = _cycle(db)
    _navs(db, [100.0, 100.0, 80.0])                  # 20% 回撤 > 10%
    block = _read(db)["fuse"]
    assert block["status"] == patrol.TRIPPED and block["tripped"] is True
    assert block["at_value"] >= block["threshold"]
    assert block["cycle_id"] == cid
    assert "触发熔断" in block["reason"]


def test_fuse_without_nav_rows_does_not_guess(tmp_path):
    db = _db(tmp_path)
    _cycle(db)
    block = _read(db)["fuse"]
    assert block["tripped"] is False and block["at_value"] is None
    assert "判不了" in block["reason"]


def test_fuse_only_sees_nav_from_the_cycle_start(tmp_path):
    """周期开始**之前**的净值不算进窗口（与 `m2 cycle fuse-check` 同一口径）。"""
    db = _db(tmp_path)
    _account(db, date="2026-09-01")                  # 这条净值在周期开始之前
    _cycle(db, start=L)
    block = _read(db)["fuse"]
    assert block["at_value"] is None and block["tripped"] is False


def test_fuse_never_writes_the_ledger(tmp_path):
    """**只读**：跑一轮熔断检查，validation_events 一行都不许多。"""
    db = _db(tmp_path)
    _account(db, nav=False)
    _cycle(db)
    _navs(db, [100.0, 80.0])
    before = _rows(db, "validation_events")
    block = _read(db)["fuse"]
    assert block["tripped"] is True                  # 就算真触发也不写
    assert _rows(db, "validation_events") == before


def test_fuse_does_not_touch_the_module_that_writes_events(tmp_path, monkeypatch):
    """结构保证：本模块**没有**把 `m2.cycle.fuse_check` 接进来。"""
    import stocklab.m2.cycle as cycle_mod

    def boom(*a, **k):
        raise AssertionError("巡检不许调 m2 cycle fuse-check（会写 append-only 台账）")

    monkeypatch.setattr(cycle_mod, "fuse_check", boom)
    db = _db(tmp_path)
    _account(db, nav=False)
    _cycle(db)
    _navs(db, [100.0, 80.0])
    assert _read(db)["fuse"]["tripped"] is True


# ---------- 主张 3：候选池状态（默认只算不写） ----------

def test_candidate_status_without_a_snapshot_is_skipped(tmp_path):
    block = _read(_db(tmp_path))["candidate_status"]
    assert block["status"] == patrol.SKIPPED
    assert block["would_set"] == [] and block["applied"] == []
    assert "快照" in block["reason"]


def test_candidate_status_counts_every_known_status(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    counts = _read(db)["candidate_status"]["counts"]
    assert set(counts) == set(("观察中", "等待买点", "已建仓", "逻辑证伪移出"))
    assert counts["观察中"] == 1 and sum(counts.values()) == 1


def test_a_held_member_is_a_would_set_candidate(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, holdings=(("000333", 100),))
    block = _read(db)["candidate_status"]
    assert block["status"] == patrol.OK
    assert [w["code"] for w in block["would_set"]] == ["000333"]
    assert block["would_set"][0]["status"] == "已建仓"
    assert block["would_set"][0]["asof"] == L
    assert "arm-hold" in block["would_set"][0]["reason"]


def test_the_default_is_dry_read(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, holdings=(("000333", 100),))
    before = _rows(db, "candidate_status_events")
    block = _read(db)["candidate_status"]
    assert block["applied"] == []
    assert _rows(db, "candidate_status_events") == before == 0
    assert "只算不写" in block["reason"]


def test_a_flat_account_creates_no_difference(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, holdings=())                       # 现金账户
    block = _read(db)["candidate_status"]
    assert block["status"] == patrol.SKIPPED and block["would_set"] == []


def test_a_halted_account_does_not_count_as_holding(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, live=False, holdings=(("000333", 100),))
    assert _read(db)["candidate_status"]["would_set"] == []


def test_a_member_already_marked_built_is_not_rewritten(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, holdings=(("000333", 100),))
    c = connect(db)
    try:
        c.execute("INSERT INTO candidate_status_events (code, asof_date, status,"
                  " reason, actor, created_at) VALUES ('000333',?,'已建仓','人工',"
                  "'nanobot',?)", (L, NOW))
        c.commit()
    finally:
        c.close()
    block = _read(db)["candidate_status"]
    assert block["would_set"] == [] and block["counts"]["已建仓"] == 1


def test_update_status_calls_the_p91_cli_by_subprocess(tmp_path):
    """打开开关 ⇒ 逐条走**子进程**调 `candidate status set`（不 import 业务函数）。"""
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, holdings=(("000333", 100),))
    fake = FakeRunner()
    conn = connect(db)
    try:
        block = business.build_business(conn, latest=L, db_path=db,
                                        update_status=True,
                                        runner=fake)["candidate_status"]
    finally:
        conn.close()
    assert len(fake.calls) == 1
    argv = fake.calls[0]["argv"]
    assert argv[:5] == [argv[0], "-m", "stocklab.cli.main", "candidate", "status"]
    assert argv[5] == "set"
    for flag, value in (("--code", "000333"), ("--asof", L),
                        ("--status", "已建仓"), ("--actor", "patrol"),
                        ("--db", str(db))):
        assert argv[argv.index(flag) + 1] == value
    assert [a["code"] for a in block["applied"]] == ["000333"]
    assert block["applied"][0]["exit_code"] == 0


def test_update_status_records_a_failing_subprocess_without_changing_the_verdict(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, holdings=(("000333", 100),))
    fake = FakeRunner({"candidate_status_set": 1})
    conn = connect(db)
    try:
        block = business.build_business(conn, latest=L, db_path=db,
                                        update_status=True,
                                        runner=fake)["candidate_status"]
    finally:
        conn.close()
    assert block["applied"][0]["exit_code"] == 1
    assert block["applied"][0]["detail"]


def test_update_status_off_means_the_runner_is_never_called(tmp_path):
    db = _db(tmp_path)
    _snapshot(db)
    _account(db, holdings=(("000333", 100),))
    fake = FakeRunner()
    conn = connect(db)
    try:
        business.build_business(conn, latest=L, db_path=db, runner=fake)
    finally:
        conn.close()
    assert fake.calls == []


# ---------- 主张 4b：新鲜度（只读两张 P88 表） ----------

def test_freshness_of_two_empty_tables_is_skipped(tmp_path):
    block = _read(_db(tmp_path))["freshness"]
    assert block["status"] == patrol.SKIPPED
    assert block["announcements"]["rows"] == 0
    assert block["northbound"]["rows"] == 0
    assert "空" in block["reason"]


def test_freshness_reports_the_max_date_and_rows(tmp_path):
    db = _db(tmp_path)
    _announcement(db, notice_date=L)
    _announcement(db, notice_date=L, code="600036")
    _northbound(db, trade_date="2026-06-30")
    block = _read(db)["freshness"]
    assert {k: v for k, v in block["announcements"].items() if k != "reason"} == {
        "rows": 2, "max_date": L, "status": patrol.OK}
    assert block["northbound"]["rows"] == 1
    assert block["northbound"]["max_date"] == "2026-06-30"
    assert block["status"] == patrol.STALE        # 北向落后（季度频率，展示用）


def test_freshness_names_the_expected_cadence(tmp_path):
    db = _db(tmp_path)
    _northbound(db, trade_date="2026-06-30")
    reason = _read(db)["freshness"]["reason"]
    assert "季度" in reason and "不计异常" in reason


def test_freshness_behind_is_display_only(tmp_path):
    """新鲜度落后**不**进退出码（只有熔断进）。"""
    db = _db(tmp_path)
    _announcement(db, notice_date="2026-09-01")
    snap = {"latest_closed_session": {"date": L},
            "checks": {n: {"status": patrol.OK} for n in patrol.CHECK_ORDER},
            "calendar": {"status": patrol.OK},
            "business": _read(db)}
    assert _read(db)["freshness"]["status"] == patrol.STALE
    assert patrol.verdict(snap)["exit_code"] == 0


# ---------- 主张 5：只有熔断计入退出码 ----------

def _snap_with(business_block: dict) -> dict:
    return {"latest_closed_session": {"date": L},
            "checks": {n: {"status": patrol.OK} for n in patrol.CHECK_ORDER},
            "calendar": {"status": patrol.OK},
            "business": business_block}


def test_a_tripped_fuse_is_an_anomaly(tmp_path):
    snap = _snap_with({"fuse": {"tripped": True, "reason": "回撤 20% > 阈值 10%"}})
    v = patrol.verdict(snap)
    assert v["exit_code"] == 1
    kinds = [a["kind"] for a in patrol._anomalies(snap, v)]
    assert "business_fuse" in kinds


def test_an_untripped_fuse_changes_nothing(tmp_path):
    snap = _snap_with({"fuse": {"tripped": False, "reason": "未触发"}})
    assert patrol.verdict(snap)["exit_code"] == 0
    assert patrol._anomalies(snap, patrol.verdict(snap)) == []


def test_a_payload_without_a_business_block_still_verdicts(tmp_path):
    """老载荷（没有 business 键）照旧判 —— 只增键不等于消费者必须给。"""
    snap = {"latest_closed_session": {"date": L},
            "checks": {n: {"status": patrol.OK} for n in patrol.CHECK_ORDER},
            "calendar": {"status": patrol.OK}}
    assert patrol.verdict(snap)["exit_code"] == 0


# ---------- 主张 4：巡检简报 ----------

def test_brief_lists_the_four_business_readings(tmp_path):
    payload = {"now": NOW, "today": TODAY, "exit_code": 1,
               "latest_closed_session": {"date": L},
               "anomalies": [{"kind": "check_review", "detail": "报告不存在"}],
               "plan": {"steps": ["review_daily"], "skipped": []},
               "steps": [{"name": "review_daily"}],
               "business": _read(_db(tmp_path))}
    text = business.brief_text(payload)
    assert f"# {TODAY} 巡检" in text
    assert "退出码：1（exit=1）" in text
    for name in BLOCK_KEYS:
        assert f"`{name}`" in text
    assert "review_daily" in text
    assert text.endswith("\n")


def test_brief_derives_everything_from_the_payload_alone(tmp_path):
    """简报**不现场再查库**：给一个手搭的 payload，内容必须完全跟着它走。"""
    payload = {"now": "2026-09-22T11:00:00+08:00", "today": TODAY,
               "exit_code": 0, "latest_closed_session": {"date": L},
               "anomalies": [], "plan": {"steps": [], "skipped": []},
               "steps": [], "business": _read(_db(tmp_path))}
    text = business.brief_text(payload)
    assert "2026-09-22T11:00:00+08:00" in text
    assert "异常：无" in text


def test_brief_is_appended_per_day_and_latest_is_overwritten(tmp_path):
    payload = {"now": NOW, "today": TODAY, "exit_code": 0,
               "latest_closed_session": {"date": L}, "anomalies": [],
               "plan": {"steps": [], "skipped": []}, "steps": [],
               "business": _read(_db(tmp_path))}
    business.write_brief(payload, report_dir=tmp_path)
    payload2 = {**payload, "now": "2026-09-22T11:00:00+08:00"}
    business.write_brief(payload2, report_dir=tmp_path)

    day = tmp_path / "ops" / "brief" / f"{TODAY}.md"
    latest = tmp_path / "ops" / "latest-patrol-brief.md"
    assert day.exists() and latest.exists()
    assert day.read_text(encoding="utf-8").count(f"# {TODAY} 巡检") == 2
    assert latest.read_text(encoding="utf-8").count(f"# {TODAY} 巡检") == 1
    assert "2026-09-22T11:00:00+08:00" in latest.read_text(encoding="utf-8")


def test_brief_failure_does_not_change_the_exit_code(tmp_path, monkeypatch):
    import stocklab.ops.journal as journal_mod

    def boom(*a, **k):
        raise OSError("只读目录")

    monkeypatch.setattr(journal_mod, "report_dir_of", boom)
    payload = {"now": NOW, "today": TODAY, "exit_code": 1,
               "latest_closed_session": {"date": L}, "anomalies": [],
               "plan": {"steps": [], "skipped": []}, "steps": [], "business": {}}
    out = business.write_brief(payload, report_dir=tmp_path)
    assert "只读目录" in out["error"]


# ---------- 端到端：run_patrol 的载荷 ----------

def test_run_patrol_adds_only_the_business_key(tmp_path):
    """G3：既有载荷一个字不动，只多一个 `business` 顶层键。"""
    import stocklab.ops.patrol as p

    db = green_db(tmp_path)
    rd = tmp_path / "reports"
    payload = p.run_patrol(db_path=db, now=NOW, report_dir=rd)
    assert "business" in payload
    assert tuple(payload["business"]) == BLOCK_KEYS
    assert "brief_error" not in payload
    assert payload["exit_code"] == 0
    # 既有 7 项与摘要行**逐字不变**（同一张全绿库）
    assert p.summary_line(payload) == (
        "patrol: exit=0 latest_closed=2026-09-21 calendar=ok"
        " ①ok ②ok ③ok ④ok ⑤ok ⑥ok ⑦ok fixed=- anomalies=0")


def test_run_patrol_writes_both_brief_files(tmp_path):
    db = green_db(tmp_path)
    rd = tmp_path / "reports"
    patrol.run_patrol(db_path=db, now=NOW, report_dir=rd)
    assert (rd / "ops" / "brief" / f"{TODAY}.md").exists()
    assert (rd / "ops" / "latest-patrol-brief.md").exists()


def test_run_patrol_defaults_to_dry_read_for_the_candidate_status(tmp_path):
    db = green_db(tmp_path)
    rd = tmp_path / "reports"
    payload = patrol.run_patrol(db_path=db, now=NOW, report_dir=rd)
    assert payload["business"]["candidate_status"]["applied"] == []
    assert _rows(db, "candidate_status_events") == 0


def test_run_patrol_with_a_tripped_cycle_exits_one_and_names_it(tmp_path):
    """G4：触发熔断 ⇒ 退出码 1 + `kind="business_fuse"`。"""
    db = green_db(tmp_path)
    _account(db, account_id="arm-fuse", nav=False)
    _cycle(db, account_id="arm-fuse")
    _navs(db, [100.0, 100.0, 80.0], account_id="arm-fuse")
    payload = patrol.run_patrol(db_path=db, now=NOW,
                                report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 1
    assert payload["business"]["fuse"]["tripped"] is True
    assert "business_fuse" in [a["kind"] for a in payload["anomalies"]]


def test_run_patrol_business_blocks_never_write_business_tables(tmp_path):
    """主张 3：业务块跑完，除 `job_runs` 一行外零新增行。"""
    db = green_db(tmp_path)
    _snapshot(db)
    _account(db, account_id="arm-biz", holdings=(("000333", 100),))
    before = _tables(db)
    patrol.run_patrol(db_path=db, now=NOW, report_dir=tmp_path / "reports")
    after = _tables(db)
    skip = lambda d: {k: v for k, v in d.items() if k != "job_runs"}  # noqa: E731
    assert skip(after) == skip(before)
    assert after["job_runs"] == before["job_runs"] + 1
