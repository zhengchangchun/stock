"""`predict run` 子命令：离线、可回放、**逐字节可复现**。

「可复现」在这里是**文件级**断言（`read_bytes()` 相等），不是「数字看起来一样」——
只有字节相等才能证明「同一 asof + 同一 model_version 重复运行给出同一份载荷」。
"""

import hashlib
import json

import pytest

from stocklab.cli.main import build_parser, main
from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.predict import version as V
from stocklab.predict.service import build_predictions
from stocklab.predict.store import insert_prediction
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T10:00:00+08:00"
CODE = "000333"
N = 200


def _dates():
    out = []
    for i in range(N):
        y, rem = divmod(2024 * 372 + 1 * 31 + 1 + i, 372)
        m, d = divmod(rem, 31)
        out.append(f"{y:04d}-{m:02d}-{d:02d}")
    return out


DATES = _dates()
ASOF = DATES[-1]


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "db.sqlite"
    init_db(db)
    conn = connect(db)
    repo.upsert_instruments(conn, [Instrument(CODE, "美的集团", "sz", "main")], now=NOW)
    repo.insert_bars(conn, [
        Bar(code=CODE, date=d, open=10 + i * 0.01, high=10 + i * 0.01 + 0.1,
            low=10 + i * 0.01 - 0.1, close=10 + i * 0.01, volume=1000,
            amount=1000.0, turnover=1.0, source="test", adj_mode="none")
        for i, d in enumerate(DATES)], now=NOW)
    conn.executemany(
        "INSERT OR IGNORE INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'test',?)", [(d, NOW) for d in DATES])
    conn.close()
    return {"db": db, "reports": tmp_path / "reports", "dir": tmp_path}


def run(env, *extra):
    return main(["predict", "run", "--asof", ASOF, "--db", str(env["db"]),
                 "--report-dir", str(env["reports"]), *extra])


def test_subcommand_is_registered():
    a = build_parser().parse_args(["predict", "run", "--asof", ASOF])
    assert a.func is not None and a.asof == ASOF
    assert a.code is None


def test_predict_run_is_byte_for_byte_reproducible(env, capsys):
    r1, r2 = env["dir"] / "r1.json", env["dir"] / "r2.json"
    assert run(env, "--report", str(r1)) == 0
    out1 = capsys.readouterr().out
    assert run(env, "--report", str(r2)) == 0
    out2 = capsys.readouterr().out

    assert r1.read_bytes() == r2.read_bytes()          # 载荷逐字节一致
    h1 = hashlib.sha256(r1.read_bytes()).hexdigest()
    h2 = hashlib.sha256(r2.read_bytes()).hexdigest()
    assert h1 == h2
    assert h1 in out1 and h1 in out2                   # 两次运行的 hash 都打印出来了

    rep = json.loads(r1.read_text(encoding="utf-8"))
    assert rep["asof_date"] == ASOF
    assert len(rep["predictions"]) == 1
    p = rep["predictions"][0]
    assert p["model_version"] == V.MODEL_VERSION
    assert abs(sum(p["direction"].values()) - 1.0) < 1e-12
    assert p["strategy_mix"]["degenerate"] is True


def test_predict_run_persists_and_second_run_is_identical(env, capsys):
    assert run(env) == 0
    capsys.readouterr()
    assert run(env) == 0
    assert "identical" in capsys.readouterr().out
    conn = connect(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM predictions").fetchone()
    assert row["code"] == CODE and row["asof_date"] == ASOF
    assert json.loads(row["strategy_mix_json"])["degenerate"] is True
    assert row["regime_label"] is None and row["feature_snapshot_id"] is None
    conn.close()


def test_predict_run_rejects_a_non_session_date(env, capsys):
    assert main(["predict", "run", "--asof", "2019-01-02", "--db", str(env["db"])]) == 2
    assert "日历" in capsys.readouterr().err


def test_predict_run_exits_nonzero_on_conflict_without_overwriting(env, capsys):
    """同键但载荷不同 → 退出码 1，且**原行一个字节都不变**。

    冲突用「跑之前先手工塞一条同键、不同 action 的预测」构造 ——
    这正是将来「改了模型却忘了升 model_version」时的真实现场。
    """
    conn = connect(env["db"])
    rep = build_predictions(conn, ASOF, [CODE])
    bad = dict(rep["predictions"][0], action="trim", size_pct=12.34)
    assert insert_prediction(conn, bad, now=NOW)[0] == "inserted"
    conn.close()

    assert main(["predict", "run", "--asof", ASOF, "--db", str(env["db"])]) == 1
    assert "冲突" in capsys.readouterr().err

    conn = connect(env["db"])
    row = conn.execute("SELECT action, size_pct FROM predictions").fetchone()
    assert (row["action"], row["size_pct"]) == ("trim", 12.34)   # 未被覆盖
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    conn.close()


def test_predict_run_skips_a_code_with_no_bars_on_asof(env, capsys):
    """请求了库里没有行情的标的 → 该标的**显式跳过**并给出原因，不是静默消失。"""
    assert run(env, "--code", "600690") == 2
    err = capsys.readouterr().err
    assert "600690" in err and "无 K 线" in err
