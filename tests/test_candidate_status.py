"""P91｜候选池「标的状态」流转（B7）：事件表 + 写入口 + 读侧 overlay。

口径（任务书 §0.5 D1–D5）：

- `candidate_status_events` 是 **append-only 事件表**，PIT 锚 = `asof_date`
  （「该状态**从这一天起**生效」）；
- 「某 code 在日期 D 的状态」= `asof_date <= D` 里 `(asof_date, event_id)` 最大那行的
  status；**无事件 ⇒ `观察中`**（与 `MemberRow` 的默认值逐位相同）；
- 同 `(code, asof_date, status)` 只许一行（`UNIQUE` + 幂等命中 ⇒ 零写入）；
- 同日换状态 = **追加新行**；读侧按 `(asof_date, event_id)` 取最后一行 ⇒ 后写赢。

`candidate/status.py` 是**只读**模块（写入口在 CLI）。
"""

import re
import sqlite3
from datetime import date, timedelta

import pytest

from stocklab.candidate import run as candidate_run
from stocklab.candidate import snapshot
from stocklab.candidate import status as cand_status
from stocklab.cli.main import main
from stocklab.config.paths import SCHEMA_SQL
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
ASOF = "2026-09-17"

PLUGINS = {
    "0": "def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n",
    "1": "def run(ctx):\n    return {'score': 80.0, 'pass_flag': True,"
         " 'reason': '量价', 'risk_list': []}\n",
    "2": "def run(ctx):\n    return {'score': 60.0, 'pass_flag': True,"
         " 'reason': '景气', 'risk_list': []}\n",
    "3": "def run(ctx):\n    return {'score': 40.0, 'pass_flag': True,"
         " 'reason': '护城河', 'risk_list': []}\n",
    "4": "def run(ctx):\n    return {'final_score': ctx['raw_score'],"
         " 'risk_out': []}\n",
}


# ---------- 夹具 ----------

def _seed_db(tmp_db, *, n_days=300):
    """够跑完整候选池主流程的最小库（与 `test_candidate_run.py` 同构）。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    codes = ["000333", "600690", "600519"]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, f"标的{code}", NOW) for code in codes])
    days = []
    cur = date(2025, 6, 1)
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'tencent',?)",
                  [(d, NOW) for d in days])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, 10.0, 10.0, 10.0, 10.0, NOW)
         for code in codes for d in days])
    for pid, text in PLUGINS.items():
        sid = store.insert_script(c, plugin_id=pid, version="1.0.0",
                                  source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
    c.commit()
    return c


@pytest.fixture
def conn():
    """内存库：只有 schema，没有业务数据（读侧纯函数用例用它）。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    yield c
    c.close()


def _add_event(c, *, code, asof, status_, reason="人工设置", actor="nanobot",
               created_at=NOW) -> int:
    cur = c.execute(
        "INSERT INTO candidate_status_events (code, asof_date, status, reason,"
        " actor, created_at) VALUES (?,?,?,?,?,?)",
        (code, asof, status_, reason, actor, created_at))
    return int(cur.lastrowid)


def _rows(c) -> int:
    return c.execute("SELECT COUNT(*) FROM candidate_status_events").fetchone()[0]


def _cli(argv: list[str]) -> int:
    return main(argv)


# ---------- D2 读侧纯函数 ----------

def test_latest_status_defaults_when_no_event(conn):
    assert cand_status.latest_status(conn, "000333", "2026-09-24") == (
        "观察中", None)


def test_default_status_matches_member_row():
    """无事件的默认值必须**逐位**等于 `MemberRow.status` 的默认（D2）。"""
    assert cand_status.DEFAULT_STATUS == snapshot.MemberRow(
        code="", pool="", raw_score=0.0, adj_score=0.0, reason="",
        risk_json="").status


def test_latest_status_is_point_in_time(conn):
    """PIT：09-24 的事件不泄漏到 09-23。"""
    _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    assert cand_status.latest_status(conn, "000333", "2026-09-23")[0] == "观察中"
    assert cand_status.latest_status(conn, "000333", "2026-09-24")[0] == "已建仓"
    # asof 当天的事件「从这一天起」生效 ⇒ 之后的日期也看得到
    assert cand_status.latest_status(conn, "000333", "2026-10-01")[0] == "已建仓"


def test_latest_status_same_day_later_event_wins(conn):
    """同日两条 ⇒ `(asof_date, event_id)` 最大者赢（后写赢）。"""
    e1 = _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    e2 = _add_event(conn, code="000333", asof="2026-09-24", status_="等待买点")
    assert e2 > e1
    value, row = cand_status.latest_status(conn, "000333", "2026-09-24")
    assert value == "等待买点"
    assert row["event_id"] == e2


def test_latest_status_newer_asof_date_beats_later_event_id(conn):
    """跨日：`asof_date` 优先于 `event_id`（新一天的事件赢过旧一天后写的）。"""
    _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    _add_event(conn, code="000333", asof="2026-09-24", status_="等待买点")
    _add_event(conn, code="000333", asof="2026-09-25", status_="已建仓")
    assert cand_status.latest_status(conn, "000333", "2026-09-25")[0] == "已建仓"


def test_latest_status_is_per_code(conn):
    _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    assert cand_status.latest_status(conn, "000651", "2026-09-24") == (
        "观察中", None)


def test_status_map_batch_and_defaults(conn):
    _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    got = cand_status.status_map(conn, ["000333", "000651"], "2026-09-24")
    assert got == {"000333": "已建仓", "000651": "观察中"}


def test_status_map_empty_codes_does_no_query(conn):
    assert cand_status.status_map(conn, [], "2026-09-24") == {}


def test_history_is_desc_and_filterable(conn):
    _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓",
               reason="手动建仓")
    _add_event(conn, code="000333", asof="2026-09-25", status_="观察中")
    _add_event(conn, code="600690", asof="2026-09-25", status_="等待买点")
    rows = cand_status.history(conn, limit=10)
    assert [(r["code"], r["asof_date"], r["status"]) for r in rows] == [
        ("600690", "2026-09-25", "等待买点"),
        ("000333", "2026-09-25", "观察中"),
        ("000333", "2026-09-24", "已建仓"),
    ]
    # 行形状（页面要用）
    assert set(rows[0]) >= {"event_id", "code", "asof_date", "status", "reason",
                            "actor", "created_at"}
    # 按 code 过滤
    one = cand_status.history(conn, code="000333", limit=10)
    assert [r["status"] for r in one] == ["观察中", "已建仓"]
    # limit 截最新 N 条
    assert len(cand_status.history(conn, limit=2)) == 2
    # asof 过滤 = PIT（只看 <= asof 的）
    pit = cand_status.history(conn, asof="2026-09-24", limit=10)
    assert [r["asof_date"] for r in pit] == ["2026-09-24"]


def test_status_module_is_read_only():
    """D2：`status.py` 里不许有 INSERT/UPDATE/DELETE（写入口只在 CLI）。

    扫的是**代码里的字符串字面量**（docstring 除外）—— 注释里提一句写语句不算违规，
    真的拼出写 SQL 才算。
    """
    import ast

    src = (SCHEMA_SQL.parent.parent / "candidate" / "status.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    literals = " ".join(
        c.value for c in ast.walk(tree)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
        and c.value not in docstrings)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", literals,
                         re.IGNORECASE), "status.py 出现了写 SQL"


# ---------- D1 schema：枚举一致 + append-only ----------

def test_schema_enum_matches_snapshot_statuses():
    """G7：`schema.sql` 的 CHECK 枚举 == `candidate/snapshot.py::STATUSES`。"""
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    start = text.index("CREATE TABLE IF NOT EXISTS candidate_status_events")
    block = text[start:text.index(");", start)]
    m = re.search(r"CHECK \(status IN \(([^)]*)\)\)", block)
    assert m is not None, "candidate_status_events 的 status CHECK 没找到"
    assert set(re.findall(r"'([^']+)'", m.group(1))) == set(snapshot.STATUSES)


def test_duplicate_triple_is_rejected_by_unique(conn):
    """结构性幂等：同 `(code, asof_date, status)` 撞 UNIQUE。"""
    _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    with pytest.raises(sqlite3.IntegrityError):
        _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    assert _rows(conn) == 1


def test_invalid_status_is_rejected_by_check(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _add_event(conn, code="000333", asof="2026-09-24", status_="随便写的")


def test_append_only_triggers_reject_update_delete_and_replace(conn):
    """G6 同型：UPDATE / DELETE / `INSERT OR REPLACE` 三条都必须被拒。"""
    eid = _add_event(conn, code="000333", asof="2026-09-24", status_="已建仓")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE candidate_status_events SET status='观察中'"
                     " WHERE event_id=?", (eid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM candidate_status_events WHERE event_id=?",
                     (eid,))
    # 隐式 DELETE 只在 recursive_triggers=ON 时才触发（`store/db.connect` 已打开；
    # 内存夹具没有，这里显式开一次 —— 与 P48/P90 同型）。
    conn.execute("PRAGMA recursive_triggers = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO candidate_status_events (event_id, code,"
            " asof_date, status, reason, actor, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (eid, "000333", "2026-09-24", "观察中", "r", "a", NOW))
    assert _rows(conn) == 1


# ---------- D3 CLI ----------

def test_cli_set_then_show_and_idempotent(tmp_db, capsys):
    rc = _cli(["candidate", "status", "set", "--code", "000333",
               "--status", "已建仓", "--asof", "2026-09-24",
               "--reason", "手动建仓", "--db", str(tmp_db)])
    assert rc == 0
    assert "event_id=1" in capsys.readouterr().out
    c = connect(tmp_db)
    assert _rows(c) == 1
    assert c.execute("SELECT reason, actor FROM candidate_status_events"
                     ).fetchone()["reason"] == "手动建仓"
    c.close()

    # 幂等：同命令重跑 ⇒ 0、打印 already、零写入
    rc = _cli(["candidate", "status", "set", "--code", "000333",
               "--status", "已建仓", "--asof", "2026-09-24",
               "--reason", "手动建仓", "--db", str(tmp_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already" in out and "event_id=1" in out
    c = connect(tmp_db)
    assert _rows(c) == 1
    c.close()

    # show：一行一条，最新在前
    rc = _cli(["candidate", "status", "show", "--code", "000333",
               "--db", str(tmp_db)])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1 and "已建仓" in lines[0] and "2026-09-24" in lines[0]


def test_cli_same_day_new_status_appends_and_show_latest_first(tmp_db, capsys):
    _cli(["candidate", "status", "set", "--code", "000333", "--status", "已建仓",
          "--asof", "2026-09-24", "--db", str(tmp_db)])
    _cli(["candidate", "status", "set", "--code", "000333", "--status", "观察中",
          "--asof", "2026-09-24", "--db", str(tmp_db)])
    c = connect(tmp_db)
    assert _rows(c) == 2
    c.close()
    capsys.readouterr()
    rc = _cli(["candidate", "status", "show", "--code", "000333",
               "--db", str(tmp_db)])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert "观察中" in lines[0]


def test_cli_defaults_reason_and_actor(tmp_db):
    _cli(["candidate", "status", "set", "--code", "000333", "--status", "等待买点",
          "--asof", "2026-09-24", "--db", str(tmp_db)])
    c = connect(tmp_db)
    row = c.execute("SELECT reason, actor FROM candidate_status_events").fetchone()
    assert row["reason"] == "人工设置" and row["reason"] != ""
    assert row["actor"] == "nanobot"
    c.close()


def test_cli_show_filters_by_code_and_limit(tmp_db, capsys):
    for code in ("000333", "000651"):
        _cli(["candidate", "status", "set", "--code", code, "--status", "已建仓",
              "--asof", "2026-09-24", "--db", str(tmp_db)])
    capsys.readouterr()
    rc = _cli(["candidate", "status", "show", "--limit", "1", "--db", str(tmp_db)])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1


@pytest.mark.parametrize("bad", ["已建仓 ", "观察", "", "建仓"])
def test_cli_invalid_status_is_exit_2_and_writes_nothing(tmp_path, bad):
    """非法状态由 argparse choices 拒（exit 2），且**库文件都不建**。"""
    ghost = tmp_path / "ghost.db"
    rc = _cli(["candidate", "status", "set", "--code", "000333",
               "--status", bad, "--asof", "2026-09-24", "--db", str(ghost)])
    assert rc == 2
    assert not ghost.exists()


@pytest.mark.parametrize("flag,value", [("--reason", " "), ("--actor", "")])
def test_cli_blank_reason_or_actor_is_exit_2(tmp_path, flag, value):
    """`--reason` 不许空串（D3）；`--actor` 同理 —— 空着就没人知道谁改的。"""
    ghost = tmp_path / "ghost.db"
    rc = _cli(["candidate", "status", "set", "--code", "000333",
               "--status", "已建仓", "--asof", "2026-09-24", flag, value,
               "--db", str(ghost)])
    assert rc == 2
    assert not ghost.exists()


@pytest.mark.parametrize("code", ["333", "abcdef", "0003333", "00033a"])
def test_cli_bad_code_is_exit_2_before_ensure_schema(tmp_path, code):
    """用法错误必须**先于** `ensure_schema` 拒掉：库文件一个字节都不许建。"""
    ghost = tmp_path / "ghost.db"
    rc = _cli(["candidate", "status", "set", "--code", code, "--status", "已建仓",
               "--asof", "2026-09-24", "--db", str(ghost)])
    assert rc == 2
    assert not ghost.exists()


def test_cli_bad_asof_is_exit_2_before_ensure_schema(tmp_path):
    ghost = tmp_path / "ghost.db"
    rc = _cli(["candidate", "status", "set", "--code", "000333",
               "--status", "已建仓", "--asof", "2026-13-99", "--db", str(ghost)])
    assert rc == 2
    assert not ghost.exists()


def test_cli_show_on_missing_db_is_exit_2_and_does_not_create_it(tmp_path):
    """读命令**不建库**（与 `candidate review` 同款）：敲错路径不该凭空造一个空库。"""
    ghost = tmp_path / "ghost.db"
    rc = _cli(["candidate", "status", "show", "--db", str(ghost)])
    assert rc == 2
    assert not ghost.exists()


def test_cli_has_no_delete_or_update_subcommand(tmp_db):
    """append-only：写错了只能再追加一行，没有 delete/update 入口。"""
    for verb in ("delete", "update"):
        rc = _cli(["candidate", "status", verb, "--code", "000333",
                   "--db", str(tmp_db)])
        assert rc == 2, verb


# ---------- D4 `candidate run` 用 overlay ----------

def test_run_candidate_uses_overlay_status(tmp_db):
    c = _seed_db(tmp_db)
    _add_event(c, code="000333", asof=ASOF, status_="已建仓")
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    by_code = {m.code: m.status for m in result.members}
    assert "000333" in by_code, "夹具里 000333 应当入池"
    assert by_code["000333"] == "已建仓"
    assert by_code["600690"] == "观察中"
    stored = {r[0] for r in c.execute(
        "SELECT status FROM candidate_members WHERE code='000333'")}
    assert stored == {"已建仓"}
    c.close()


def test_run_candidate_overlay_respects_pit(tmp_db):
    """事件发生在 asof **之后** ⇒ 不许进这一轮快照。"""
    c = _seed_db(tmp_db)
    _add_event(c, code="000333", asof="2026-10-01", status_="已建仓")
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert {m.status for m in result.members} == {"观察中"}
    c.close()


def test_run_candidate_without_events_is_unchanged(tmp_db):
    """无事件 ⇒ 逐位等于默认值（既有用例不改一行也必须绿）。"""
    c = _seed_db(tmp_db)
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert {m.status for m in result.members} == {"观察中"}
    c.close()
