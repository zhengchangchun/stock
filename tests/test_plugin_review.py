"""P58 护栏：插桩5「定期复盘分析」的接线（`candidate review --asof`）。

要钉死的主张（每条都对应一个会很贵的错）：

1. **桩不许被包装成结论**：现役 v1.0.0 返回 `{"status": "not_implemented"}`，
   命令必须 exit 0、台账照记、报告**第 1 行**自报「尚未实现」；
2. **没有在役版本是显式失败**：exit 2 ＋点名「插桩5」＋**零写入**（不兜底跑空）；
3. **幂等且 append-only**：同 `(asof, script_id)` 重跑不增行、不覆盖；直接
   `UPDATE`/`DELETE` 台账被触发器拒绝；
4. **PIT**：`asof` 之后才落库的快照与行情既不进样本、也不进报告；
5. **报告落点不撞 session review**：`ops patrol` ⑤ 的判据（`<L>-review.md`）
   一行不改，也不受 `reports/plugin-review/` 影响；
6. **源文本合规**：过静态预检、过探针上下文、过返回结构校验，且逐字节确定。

夹具一律 tmp 库 ＋ conftest 把 `paths.REPORT_DIR` 指到 `tmp_path/reports`
（ERROR_DIARY #51：夹具不许往仓库 `reports/` 写真文件）。全程不起子进程、不联网。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from stocklab.candidate.builtin import review as review_builtin
from stocklab.cli.main import main
from stocklab.config import paths
from stocklab.ops import patrol
from stocklab.plugin import contract, guard, lifecycle, runtime
from stocklab.plugin import store as plugin_store
from stocklab.plugin_review import inputs, report_path, service, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from tests.test_ops_patrol import L as PATROL_L
from tests.test_ops_patrol import NOW as PATROL_NOW
from tests.test_ops_patrol import _db as _patrol_db

NOW = "2026-09-23T20:00:00+08:00"

#: 复盘的**目标日**。T0 之后正好还有 5 个交易日（见 CAL），所以 T1 = 目标日本身。
TARGET = "2026-09-21"
T0 = "2026-09-14"
T1 = "2026-09-21"

#: 交易日历：T0 之后 5 个交易日 = 15/16/17/18/21（22 号也有 —— 用于 PIT 反证）。
CAL = ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14",
       "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21",
       "2026-09-22")

#: 六个候选：四个涨、两个跌 ⇒ 样本足够（MIN_SAMPLES=5）且胜率 = 4/6。
GAINERS = ("000333", "600000", "600519", "000001")
LOSERS = ("002415", "601318")
CODES = GAINERS + LOSERS

#: 现役桩的返回（与真库 v1.0.0 同形）。
STUB_SOURCE = ('def run(ctx):\n'
               '    return {"analysis_result": {"status": "not_implemented"},'
               ' "bad_case_list": []}\n')

_HIGH = 11.0      # 涨：10.0 → 11.0（+10%）
_LOW = 9.5        # 跌：10.0 → 9.5（-5%）
_AFTER = 99.0     # 目标日**之后**那根 bar：谁用了它，读数就会离谱到肉眼可见


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _bars(c, code: str, close: float, *, after: float | None = None) -> None:
    """`code` 的日线：T0 及之前一律 10.0，T0 之后一律 `close`。

    `after` 给目标日**之后**的那根 bar（PIT 反证用）。
    """
    rows = []
    for d in CAL:
        if after is not None and d > TARGET:
            px = after
        else:
            px = 10.0 if d <= T0 else close
        rows.append((code, d, px, px, px, px, 1000, None, "none", "x", NOW))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, amount,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def _snapshot(c, asof: str, codes) -> int:
    cur = c.execute(
        "INSERT INTO candidate_snapshots (asof, run_kind, params_json, created_at)"
        " VALUES (?,'weekly','{}',?)", (asof, NOW))
    sid = int(cur.lastrowid)
    c.executemany(
        "INSERT INTO candidate_members (snapshot_id, code, pool, raw_score, adj_score,"
        " reason, risk_json, status, entered_at) VALUES (?,?,'short',50.0,?,'r','{}',"
        "'观察中',?)",
        [(sid, code, 60.0 + i, NOW) for i, code in enumerate(codes)])
    return sid


def _stub_script(conn, *, version: str = "v1.0.0", approve: bool = True) -> int:
    """插一条插桩5 的脚本并走完审计链：`approve=False` ⇒ 停在 `pending_review`。"""
    sid = plugin_store.insert_script(conn, plugin_id="5", version=version,
                                     source_text=STUB_SOURCE, note="夹具",
                                     now=NOW)
    lifecycle.record_submit(conn, sid, actor="human", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="夹具", now=NOW)
    if approve:
        lifecycle.approve(conn, sid, actor="human", reason="夹具上线", now=NOW)
    return sid


def _db(tmp_path, *, active: bool = True, bars: bool = True,
        snapshots: bool = True, backtests: bool = True,
        future_rows: bool = False) -> Path:
    """一份能跑出「有样本」的夹具库。每个测试只关掉自己要测的那块。"""
    path = tmp_path / "review.db"
    init_db(path)
    c = connect(path)
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)", [(code, code, NOW) for code in CODES])
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'x',?)", [(d, NOW) for d in CAL])
    if bars:
        for code in GAINERS:
            _bars(c, code, _HIGH, after=_AFTER if future_rows else None)
        for code in LOSERS:
            _bars(c, code, _LOW, after=_AFTER if future_rows else None)
    if snapshots:
        _snapshot(c, T0, CODES)
        # 目标日**之后**才形成的快照（PIT：它整个都不该进样本）
        if future_rows:
            _snapshot(c, "2026-09-22", ("601318",))
    if backtests:
        c.execute(
            "INSERT INTO plugin_backtests (candidate_script_id, baseline_script_id,"
            " pool, window_start, window_end, metrics_json, verdict, overfit_flag,"
            " report_sha256, created_at) VALUES (1,NULL,'short','2026-01-01',"
            "'2026-09-21','{\"sharpe\": 1.0}','WIN',NULL,'h',?)", (NOW,))
        c.execute(
            "INSERT INTO plugin_backtests (candidate_script_id, baseline_script_id,"
            " pool, window_start, window_end, metrics_json, verdict, overfit_flag,"
            " report_sha256, created_at) VALUES (1,NULL,'short','2026-01-01',"
            "'2026-09-21','{\"sharpe\": 0.2}','LOSE','suspected','h',?)", (NOW,))
        c.execute(
            "INSERT INTO plugin_backtests (candidate_script_id, baseline_script_id,"
            " pool, window_start, window_end, metrics_json, verdict, overfit_flag,"
            " report_sha256, created_at) VALUES (1,NULL,'mid','2026-01-01',"
            "'2026-09-21','{\"sharpe\": 0.1}','WIN',NULL,'h',?)", (NOW,))
    _stub_script(c, approve=active)
    c.commit()
    c.close()
    return path


def _run(db: Path, *extra: str) -> tuple[int, str, str]:
    """跑 CLI，返回 `(exit_code, stdout, stderr)`。"""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["candidate", "review", "--asof", TARGET, "--db", str(db),
                     "--now", NOW, *extra])
    return code, out.getvalue(), err.getvalue()


def _rows(db: Path) -> list[dict]:
    conn = connect(db)
    try:
        return store.load_reviews(conn, asof=TARGET)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# T1：形状与退出码（现役是桩 ⇒ exit 0）
# --------------------------------------------------------------------------

def test_t1_stub_run_exits_zero_and_prints_the_shape(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path)
    code, out, err = _run(db)
    assert code == 0, err
    assert f"asof={TARGET}" in out
    assert "script_id=1" in out
    assert "script_version=v1.0.0" in out
    assert "review_id=1" in out
    assert "report_path=" in out
    report = Path(out.split("report_path=", 1)[1].splitlines()[0])
    assert report.exists()
    assert report == report_path(TARGET)
    # 报告落点：**不是** session review 那一份
    assert report.parent.name == "plugin-review"
    assert report.name == f"{TARGET}.md"
    assert not (paths.REPORT_DIR / f"{TARGET}-review.md").exists()

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert (row["asof"], row["script_id"], row["script_version"]) == (
        TARGET, 1, "v1.0.0")
    assert row["plugin_id"] == "5"
    assert row["status"] == "not_implemented"
    # 输入侧事实也落了库（不是只写结论）
    assert row["inputs"]["n_candidates"] == len(CODES)
    assert row["inputs"]["n_samples"] == len(CODES)


def test_t1_rejects_a_bad_asof_before_touching_anything(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path)
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(err):
        code = main(["candidate", "review", "--asof", "2026/09/21", "--db", str(db),
                     "--now", NOW])
    assert code == 2
    assert "YYYY-MM-DD" in err.getvalue()
    assert _rows(db) == []


# --------------------------------------------------------------------------
# T2：没有 active 版本 ⇒ 点名报错、零写入
# --------------------------------------------------------------------------

def test_t2_no_active_version_is_named_and_writes_nothing(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path, active=False)
    code, out, err = _run(db)
    assert code == 2
    assert "插桩5" in err
    assert "active" in err
    # 零写入：没有台账行、没有报告文件
    assert _rows(db) == []
    assert not (paths.REPORT_DIR / "plugin-review").exists()


def test_t2_empty_plugin_table_is_the_same_refusal(tmp_path, report_dir_is_tmp):
    """空表夹具（一条脚本都没有）走的是同一条拒绝路径。"""
    path = tmp_path / "empty.db"
    init_db(path)
    code, out, err = _run(path)
    assert code == 2
    assert "插桩5" in err
    conn = connect(path)
    try:
        assert store.load_reviews(conn) == []
    finally:
        conn.close()


# --------------------------------------------------------------------------
# T3：桩不伪装
# --------------------------------------------------------------------------

def test_t3_stub_report_says_so_on_the_first_line(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path)
    code, out, _ = _run(db)
    assert code == 0
    report = Path(out.split("report_path=", 1)[1].splitlines()[0])
    first = report.read_text(encoding="utf-8").splitlines()[0]
    assert "尚未实现" in first
    assert "script_id=1" in first
    # 台账里的 analysis_result 是**原文**，没有被重写成结论
    assert _rows(db)[0]["analysis"] == {"status": "not_implemented"}
    assert _rows(db)[0]["bad_cases"] == []
    assert "not_implemented" in report.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# T4：幂等 ＋ append-only
# --------------------------------------------------------------------------

def test_t4_same_key_twice_adds_no_row(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path)
    assert _run(db)[0] == 0
    assert len(_rows(db)) == 1
    code, out, err = _run(db)
    assert code == 0, err
    assert "已存在同键行，未新增" in out
    assert len(_rows(db)) == 1


def test_t4_existing_row_is_never_overwritten(tmp_path, report_dir_is_tmp):
    """同键已有行时**一个字节都不改**（append-only：改错只能换 `script_id` 重跑）。"""
    db = _db(tmp_path)
    conn = connect(db)
    try:
        store.insert_review(
            conn, asof=TARGET, plugin_id="5", script_id=1, script_version="v1.0.0",
            source_sha256="x", status="not_implemented",
            analysis={"status": "not_implemented", "marker": "手写的那一份"},
            bad_cases=[], inputs={"marker": "手写"}, report_path="/nowhere.md",
            report_sha256="y", now=NOW)
    finally:
        conn.close()
    assert _run(db)[0] == 0
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["analysis"]["marker"] == "手写的那一份"
    assert rows[0]["inputs"] == {"marker": "手写"}
    assert rows[0]["report_path"] == "/nowhere.md"


def test_t4_a_new_script_version_gets_its_own_row(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path)
    assert _run(db)[0] == 0
    conn = connect(db)
    try:
        sid = plugin_store.insert_script(conn, plugin_id="5", version="v1.1.0",
                                         source_text=review_builtin.SOURCE,
                                         note="候选", now=NOW)
        lifecycle.record_submit(conn, sid, actor="human", now=NOW)
        lifecycle.record_sandbox(conn, sid, passed=True, reason="夹具", now=NOW)
        lifecycle.approve(conn, sid, actor="human", reason="夹具上线", now=NOW)
    finally:
        conn.close()
    code, out, err = _run(db)
    assert code == 0, err
    assert f"script_id={sid}" in out
    rows = _rows(db)
    assert [r["script_id"] for r in rows] == [1, sid]
    # 真脚本这一版不再自报「尚未实现」，且真算出了胜率（4/6 涨）
    real = [r for r in rows if r["script_id"] == sid][0]
    assert real["status"] == "ok"
    assert real["analysis"]["n_samples"] == len(CODES)
    assert real["analysis"]["overall"]["win_rate"] == round(4 / 6, 4)
    assert real["analysis"]["backtests"]["win_rate"] == round(2 / 3, 4)


# --------------------------------------------------------------------------
# T5：触发器拦 UPDATE / DELETE
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "UPDATE plugin_reviews SET status = 'ok'",
    "DELETE FROM plugin_reviews",
])
def test_t5_append_only_triggers_reject_update_and_delete(tmp_path, sql):
    db = _db(tmp_path)
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1, \
            "连接必须打开 recursive_triggers（否则 INSERT OR REPLACE 拦不住）"
        store.insert_review(
            conn, asof=TARGET, plugin_id="5", script_id=1, script_version="v1.0.0",
            source_sha256="x", status="not_implemented", analysis={}, bad_cases=[],
            inputs={}, report_path="/nowhere.md", report_sha256="y", now=NOW)
        with pytest.raises(sqlite3.Error, match="append-only"):
            conn.execute(sql)
    finally:
        conn.close()


def test_t5_insert_or_replace_cannot_bypass_the_trigger(tmp_path):
    """`INSERT OR REPLACE` 解决唯一冲突靠隐式删行 —— 那条 DELETE 也必须被拦住
    （ERROR_DIARY #6：SQLite 默认 `recursive_triggers=OFF` 时会静默覆盖成功）。"""
    db = _db(tmp_path)
    conn = connect(db)
    try:
        store.insert_review(
            conn, asof=TARGET, plugin_id="5", script_id=1, script_version="v1.0.0",
            source_sha256="x", status="not_implemented",
            analysis={"marker": "原行"}, bad_cases=[], inputs={},
            report_path="/原.md", report_sha256="y", now=NOW)
        with pytest.raises(sqlite3.Error, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO plugin_reviews (review_id, asof, plugin_id,"
                " script_id, script_version, source_sha256, status, analysis_json,"
                " bad_case_json, inputs_json, report_path, report_sha256, created_at)"
                " VALUES (1,?, '5', 1, 'v9.9.9', 'x', 'ok', '{\"marker\": \"覆盖\"}',"
                " '[]', '{}', '/新.md', 'z', ?)", (TARGET, NOW))
        row = conn.execute("SELECT script_version, report_path FROM plugin_reviews"
                           " WHERE asof = ? AND script_id = 1", (TARGET,)).fetchone()
        assert tuple(row) == ("v1.0.0", "/原.md"), "原行必须原封不动"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# T6：PIT —— 目标日之后的东西既不进样本、也不进报告
# --------------------------------------------------------------------------

def test_t6_future_rows_do_not_leak_into_the_readings(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path, future_rows=True)
    conn = connect(db)
    try:
        ctx = inputs.build_ctx(conn, TARGET, script_id=1, script_version="v1.0.0")
    finally:
        conn.close()
    # 目标日之后的快照整条不进候选
    assert ctx["n_candidates"] == len(CODES)
    assert {s["asof"] for s in ctx["samples"]} == {T0}
    # 目标日之后那根 bar（99.0）没被用来算收益
    rets = {s["code"]: s["ret_pct"] for s in ctx["samples"]}
    assert rets["000333"] == pytest.approx(10.0)
    assert max(rets.values()) < 50.0

    code, out, err = _run(db)
    assert code == 0, err
    report = Path(out.split("report_path=", 1)[1].splitlines()[0])
    body = report.read_text(encoding="utf-8")
    assert "99.0" not in body


def test_t6_unfinished_window_is_dropped_with_a_reason(tmp_path, report_dir_is_tmp):
    """窗口走不完的样本整条剔除（**不是**拿半个窗口算个数），且原因留痕。"""
    db = _db(tmp_path)
    conn = connect(db)
    try:
        _snapshot(conn, "2026-09-18", ("600519",))     # 09-18 之后只剩 2 个交易日
        conn.commit()
        ctx = inputs.build_ctx(conn, TARGET, script_id=1, script_version="v1.0.0")
    finally:
        conn.close()
    assert ctx["n_candidates"] == len(CODES) + 1
    assert len(ctx["samples"]) == len(CODES)          # 新那条没进样本
    dropped = [d for d in ctx["sample_drops"] if d["asof"] == "2026-09-18"]
    assert len(dropped) == 1
    assert "窗口未走完" in dropped[0]["reason"]


# --------------------------------------------------------------------------
# T7：报告落点不撞 session review（patrol ⑤ 的判据一行不改）
# --------------------------------------------------------------------------

def test_t7_patrol_keeps_reading_the_session_review_file(tmp_path):
    rd = tmp_path / "reports"
    db = _patrol_db(tmp_path, report_dir=rd)           # 默认全绿：已写 <L>-review.md
    (rd / "plugin-review").mkdir(parents=True, exist_ok=True)
    (rd / "plugin-review" / f"{PATROL_L}.md").write_text("# 桩\n", encoding="utf-8")

    snap = patrol.check_db(db, PATROL_NOW, report_dir=rd)
    assert snap["checks"]["review"]["status"] == "ok"
    assert snap["checks"]["review"]["required"] == PATROL_L
    assert snap["checks"]["review"]["path"] == str(rd / f"{PATROL_L}-review.md")

    # 把 session review 那份拿掉 ⇒ 仍判 missing（新目录**不能**顶替它）
    (rd / f"{PATROL_L}-review.md").unlink()
    snap = patrol.check_db(db, PATROL_NOW, report_dir=rd)
    assert snap["checks"]["review"]["status"] == "missing"
    assert "plugin-review" not in snap["checks"]["review"]["path"]


# --------------------------------------------------------------------------
# T8：v1.1.0 源文本
# --------------------------------------------------------------------------

def test_t8_source_passes_guard_and_the_probe_context():
    source = review_builtin.SOURCE
    guard.check_source(source)
    fn = runtime.load_script(source, plugin_id="5")
    out = fn(dict(contract.PROBE_CTX))              # 探针：结构完整但无数据
    assert sorted(out) == ["analysis_result", "bad_case_list"]
    # 探针下没有样本 ⇒ 一律 None ＋ 原因，**不许给 0.0 冒充胜率 0%**
    assert out["analysis_result"]["overall"]["win_rate"] is None
    assert out["analysis_result"]["na_reasons"]
    assert out["bad_case_list"] == []


@pytest.mark.parametrize("banned", ["random", "time", "datetime", "open("])
def test_t8_source_has_no_banned_constructs(banned):
    assert banned not in review_builtin.SOURCE


def test_t8_source_is_byte_deterministic_on_the_same_ctx():
    fn = runtime.load_script(review_builtin.SOURCE, plugin_id="5")
    ctx = _sample_ctx()
    a = json.dumps(fn(dict(ctx)), ensure_ascii=False, sort_keys=True)
    b = json.dumps(fn(dict(ctx)), ensure_ascii=False, sort_keys=True)
    assert a == b
    # 深拷贝一份重跑也一样（脚本不许改 ctx、不许留模块级状态）
    again = json.loads(json.dumps(ctx))
    assert json.dumps(fn(again), ensure_ascii=False, sort_keys=True) == a


def test_t8_source_computes_real_readings():
    fn = runtime.load_script(review_builtin.SOURCE, plugin_id="5")
    out = fn(_sample_ctx())
    res = out["analysis_result"]
    assert res["n_samples"] == 6
    assert res["overall"]["win_rate"] == round(4 / 6, 4)
    assert res["by_pool"]["short"]["n"] == 6
    assert res["by_pool"]["mid"]["win_rate"] is None          # 没样本 ⇒ None
    assert any("mid" in r for r in res["na_reasons"])
    # 分桶：`[20,40)` 与 `[60,80)` 各 3 条（见 `_sample_ctx` 的两组分数）
    assert sum(b["n"] for b in res["by_score_bucket"].values()) == 6
    assert res["by_score_bucket"]["[20,40)"]["n"] == 3
    assert res["by_score_bucket"]["[60,80)"]["n"] == 3
    assert res["by_score_bucket"]["[0,20)"]["win_rate"] is None
    assert res["backtests"]["n"] == 3
    assert res["backtests"]["win_rate"] == round(2 / 3, 4)
    assert res["backtests"]["n_overfit_suspected"] == 1
    assert res["backtests"]["window"] == ["2026-01-01", TARGET]
    assert len(out["bad_case_list"]) == 1
    assert out["bad_case_list"][0]["attribution"] is None      # D-31 的结构位原样带过来


def _sample_ctx() -> dict:
    """一份手工构造的 `ctx`（形状与服务层装配的一致）。"""
    scores = (20.0, 22.0, 24.0, 60.0, 62.0, 64.0)
    rets = (2.0, 3.0, -1.0, 0.5, -2.0, 1.5)          # 四个 > 0
    samples = [{"code": "00000%d" % i, "pool": "short", "adj_score": scores[i],
                "asof": T0, "target": T1, "ret_pct": rets[i]} for i in range(6)]
    return {
        "asof": TARGET, "plugin_id": "5", "script_id": 7, "script_version": "v1.1.0",
        "n_days": 5, "samples": samples, "n_candidates": 6, "sample_drops": [],
        "calendar_error": None,
        "backtests": [
            {"pool": "short", "verdict": "WIN", "overfit_flag": None,
             "window_start": "2026-01-01", "window_end": TARGET, "metrics": {}},
            {"pool": "short", "verdict": "LOSE", "overfit_flag": "suspected",
             "window_start": "2026-01-01", "window_end": TARGET, "metrics": {}},
            {"pool": "mid", "verdict": "WIN", "overfit_flag": None,
             "window_start": "2026-01-01", "window_end": TARGET, "metrics": {}},
        ],
        "bad_cases": {"n_cases": 1, "n_miss_total": 3, "n_scored": 9, "limit": 50,
                      "empty_reason": None,
                      "cases": [{"code": "000001", "asof_date": T0,
                                 "target_date": T1, "plugin_id": "m2_a3",
                                 "script_version": "v1", "predicted_class": "up",
                                 "actual_class": "down", "attribution": None}]},
    }


# --------------------------------------------------------------------------
# 台账接口本身
# --------------------------------------------------------------------------

def test_review_store_only_offers_insert_and_select():
    """接口层先不提供 UPDATE/DELETE（想改一行的人不该在 IDE 里找到方法）。"""
    for name in dir(store):
        assert name not in ("update_review", "delete_review"), name


def test_service_report_path_is_not_the_session_review_one(tmp_path):
    assert report_path(TARGET, report_dir=tmp_path).name == f"{TARGET}.md"
    assert report_path(TARGET, report_dir=tmp_path).parent.name == "plugin-review"
    assert service.PLUGIN_ID == "5"


def test_summary_is_compact_and_counts_drops(tmp_path, report_dir_is_tmp):
    db = _db(tmp_path)
    conn = connect(db)
    try:
        ctx = inputs.build_ctx(conn, TARGET, script_id=1, script_version="v1.0.0")
    finally:
        conn.close()
    summary = inputs.summarize(ctx)
    assert summary["n_samples"] == len(CODES)
    assert summary["sample_asof_range"] == [T0, T0]
    assert summary["n_backtests"] == 3
    assert summary["bad_cases"]["limit"] == 50
    assert "samples" not in summary            # 台账不存整份样本序列
