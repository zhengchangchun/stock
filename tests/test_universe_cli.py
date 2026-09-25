"""P71 T2/T5：`universe build / sync / doctor` 三条命令的退出码与写路径。

**站内一律不联网、不写真库**：`build` 只用离线 `seed21`（`csi300+csi500` 的代码
路径由 `tests/test_universe_config.py` 用**假 fetcher** 覆盖）；`sync`/`doctor`
只在 `tmp_db`（`tmp_path` 下的独立库）上跑。

Falsifiability：

- `test_sync_is_idempotent`：把 `_sync` 的 `DELETE ... WHERE universe_id=?` 删掉
  ⇒ 第二次 sync 撞主键（或行数翻倍）即红。
- `test_sync_never_touches_existing_columns`：把 `_sync` 改成无条件
  `UPDATE instruments SET sector=?`（或顺手写 name/board）⇒ 那两条断言红。
- `test_doctor_exit_2_on_corrupted_projection_sha`：把 `_doctor` 的 sha 比对删掉
  ⇒ exit 0 即红。
- `test_doctor_exit_2_when_table_has_no_rows`：把「表缺行」当「跳过」⇒ exit 0 即红。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.cli.main import main
from stocklab.config import universes as U
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-24T00:00:00+00:00"


def _seed(db, *, sector=None):
    init_db(db)
    conn = connect(db)
    conn.execute("INSERT INTO instruments (code, name, market, board, type, sector,"
                 " added_at) VALUES ('000333','美的集团','sz','main','stock',?,?)",
                 (sector, NOW))
    conn.commit()
    conn.close()
    return db


def _count_memberships(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM universe_memberships").fetchone()[0]
    finally:
        conn.close()


# ---------- build ----------

def test_build_seed21_writes_both_files(tmp_path, capsys):
    rc = main(["universe", "build", "--source", "seed21", "--out", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "seed21.csv").is_file()
    assert (tmp_path / "seed21.meta.json").is_file()
    assert "source=seed21" in capsys.readouterr().out


def test_build_bad_source_exits_2(tmp_path, capsys):
    rc = main(["universe", "build", "--source", "sz50", "--out", str(tmp_path)])
    assert rc == 2                       # argparse choices 拦下（用法错误）
    assert "sz50" in capsys.readouterr().err


# ---------- sync ----------

def test_sync_projects_members_and_is_idempotent(tmp_db):
    _seed(tmp_db)
    assert main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    assert _count_memberships(tmp_db) == 21
    # 重入：行数不变（按 universe_id 整体重写，不新增）
    assert main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    assert _count_memberships(tmp_db) == 21
    conn = sqlite3.connect(tmp_db)
    try:
        shas = {r[0] for r in conn.execute(
            "SELECT members_sha256 FROM universe_memberships")}
        assert shas == {U.seed21_sha256()}
    finally:
        conn.close()


def test_sync_never_touches_existing_columns(tmp_db):
    """既有 `instruments` 行：只允许**补空列**，name/market/board/type 一个字不许改。"""
    _seed(tmp_db, sector="既有值")
    conn = connect(tmp_db)
    conn.execute("UPDATE instruments SET name='人工改名', board='main'"
                 " WHERE code='000333'")
    conn.commit()
    before = dict(conn.execute("SELECT * FROM instruments WHERE code='000333'")
                  .fetchone())
    conn.close()
    assert main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        after = dict(conn.execute("SELECT * FROM instruments WHERE code='000333'")
                     .fetchone())
    finally:
        conn.close()
    assert after == before, "sync 动了既有行（只许补空 sector）"


def test_sync_adds_missing_instruments_rows(tmp_db):
    _seed(tmp_db)
    assert main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    conn = sqlite3.connect(tmp_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        row = conn.execute("SELECT name, market, board, type FROM instruments"
                           " WHERE code='300750'").fetchone()
    finally:
        conn.close()
    assert n == 21
    assert row == ("宁德时代", "sz", "gem", "stock")


# ---------- P72 T1：新成员落 active=0（研究池不进日更口径，ADR-027） ----------

def test_sync_lands_new_members_inactive_and_keeps_existing_active(tmp_db, capsys):
    """`instruments.active` 是**全局日更口径**开关：sync 只能把研究池写进表，
    **不许**顺手把它们塞进日更口径（F1；否则日链 `ingest *` 从 21 只放大到 821 只）。

    Falsifiability：把 `_sync` 的 INSERT 改回 `active=1` ⇒ 下面 `all(... == 0)` 即红。
    """
    _seed(tmp_db)                       # 000333：既有行，active=1（schema 默认）
    capsys.readouterr()
    assert main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    out = capsys.readouterr().out
    assert "新增 instruments=20 只" in out

    conn = sqlite3.connect(tmp_db)
    try:
        rows = dict(conn.execute("SELECT code, active FROM instruments"))
        ctx_raw = conn.execute(
            "SELECT context_json FROM system_events WHERE module='universe'"
            " ORDER BY event_id DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()

    assert len(rows) == 21
    assert rows["000333"] == 1, "既有行的 active 一个字不许动（只补空列）"
    added = sorted(c for c in rows if c != "000333")
    assert added == sorted(c for c in rows if rows[c] == 0), \
        "新增的 20 只必须**全部**是 active=0（研究池不进日更口径）"
    ctx = json.loads(ctx_raw)
    assert ctx["instruments_added_inactive"] is True
    assert sorted(ctx["instruments_added"]) == added


def test_sync_second_run_adds_nothing_and_keeps_active_zero(tmp_db, capsys):
    """幂等：第二次 sync `added=0`、`instruments` 行数不变、active 分布不变。"""
    _seed(tmp_db)
    main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)])
    capsys.readouterr()
    assert main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    assert "新增 instruments=0 只" in capsys.readouterr().out
    conn = sqlite3.connect(tmp_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        n_active = conn.execute(
            "SELECT COUNT(*) FROM instruments WHERE active=1").fetchone()[0]
    finally:
        conn.close()
    assert (n, n_active) == (21, 1)


def test_sync_missing_universe_file_exits_2_and_writes_nothing(tmp_db):
    """文件缺失 ⇒ exit 2 且零写入。

    注意：`csi300-500` 文件已于 2026-09-25 纳管进仓（`af41b05`），**不能**再当
    「不存在」的样例 —— 这里用一个仓里必然没有的 id，判据本身逐字不变。
    """
    _seed(tmp_db)
    rc = main(["universe", "sync", "--universe", "no-such-universe", "--db", str(tmp_db)])
    assert rc == 2
    conn = sqlite3.connect(tmp_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM universe_memberships").fetchone()[0] == 0
    finally:
        conn.close()


# ---------- doctor ----------

def test_doctor_exit_0_after_sync(tmp_db, capsys):
    _seed(tmp_db)
    main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)])
    capsys.readouterr()
    assert main(["universe", "doctor", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    out = capsys.readouterr().out
    assert "表投影行=21" in out and "sector 非空 0/21" in out


def test_doctor_exit_0_when_members_are_inactive(tmp_db, capsys):
    """P72 T3：`instruments` 覆盖判据**不看 `active`** —— sync 之后成员全是 `active=0`
    也是**一致**（exit 0），且输出把 active 分布逐条报出来。

    Falsifiability：把 `_doctor` 的覆盖判据改成「只数 active=1」⇒ 这里 exit 2 即红。
    """
    _seed(tmp_db)                        # 000333 active=1，其余 20 只由 sync 落 active=0
    main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)])
    capsys.readouterr()
    assert main(["universe", "doctor", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    out = capsys.readouterr().out
    assert "instruments 覆盖 21/21" in out
    assert "成员 active 1 / 非 active 20" in out


def test_doctor_exit_2_on_corrupted_projection_sha(tmp_db, capsys):
    _seed(tmp_db)
    main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)])
    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE universe_memberships SET members_sha256='deadbeef'"
                 " WHERE code='000333'")
    conn.commit()
    conn.close()
    capsys.readouterr()
    rc = main(["universe", "doctor", "--universe", "seed21", "--db", str(tmp_db)])
    assert rc == 2
    assert "表投影 sha" in capsys.readouterr().out


def test_doctor_exit_2_when_table_has_no_rows(tmp_db, capsys):
    """**表缺行也是 exit 2**，不是「跳过」（§9 裁决 4）。"""
    _seed(tmp_db)
    assert main(["universe", "doctor", "--universe", "seed21", "--db", str(tmp_db)]) == 2
    assert "没有 universe_id" in capsys.readouterr().out


def test_doctor_exit_2_when_members_missing_from_instruments(tmp_db, capsys):
    _seed(tmp_db)
    main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)])
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM instruments WHERE code='600519'")   # 不返序列，能删
    conn.commit()
    conn.close()
    capsys.readouterr()
    rc = main(["universe", "doctor", "--universe", "seed21", "--db", str(tmp_db)])
    assert rc == 2
    assert "instruments 缺 1 只" in capsys.readouterr().out


def test_doctor_is_read_only(tmp_db):
    """doctor 跑完，库的字节不许变（只读姿势 `mode=ro&immutable=1`）。"""
    import hashlib
    _seed(tmp_db)
    main(["universe", "sync", "--universe", "seed21", "--db", str(tmp_db)])
    before = hashlib.sha256(tmp_db.read_bytes()).hexdigest()
    assert main(["universe", "doctor", "--universe", "seed21", "--db", str(tmp_db)]) == 0
    assert hashlib.sha256(tmp_db.read_bytes()).hexdigest() == before


def test_doctor_missing_db_exits_2(tmp_path, capsys):
    rc = main(["universe", "doctor", "--universe", "seed21",
               "--db", str(tmp_path / "absent.db")])
    assert rc == 2
    assert "打不开库" in capsys.readouterr().err


def test_universe_group_requires_a_subcommand(capsys):
    assert main(["universe"]) == 2       # argparse required=True ⇒ 用法错误
