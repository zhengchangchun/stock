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


def test_predict_run_rolls_forward_missing_origin_column(env, capsys):
    """写库入口在「缺 origin 列的旧库」上自动前滚，而不是 OperationalError（P33 根因）。

    反向构造：把 env（已 init_db 的新 shape 库）的 `origin` 列 DROP 掉，模拟
    P32 迁移**没跑到**的真库现场。接线前这里会在 `insert_prediction` 撞
    `sqlite3.OperationalError: table predictions has no column named origin`；
    接线后 `ensure_schema` 先补列，写入口照常成功。
    """
    conn = connect(env["db"])
    conn.execute("ALTER TABLE predictions DROP COLUMN origin")
    conn.commit()
    conn.close()

    assert run(env) == 0
    out = capsys.readouterr().out
    assert "inserted:1" in out                    # 新行已落库（不再是 OperationalError）

    conn = connect(env["db"])
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    assert "origin" in cols                       # 自动前滚补回了列
    row = conn.execute("SELECT origin FROM predictions").fetchone()
    assert row["origin"] == "live"                # 写入口按 live 来源落库
    conn.close()


def test_predict_run_exits_nonzero_on_conflict_without_overwriting(env, capsys):
    """同键但载荷不同 → 退出码 1，且**原行一个字节都不变**。

    冲突用「跑之前先手工塞一条同键、不同 action 的预测」构造 ——
    这正是将来「改了模型却忘了升 model_version」时的真实现场。
    """
    conn = connect(env["db"])
    rep = build_predictions(conn, ASOF, [CODE])
    bad = dict(rep["predictions"][0], action="trim", size_pct=12.34)
    assert insert_prediction(conn, bad, now=NOW, origin="replay")[0] == "inserted"
    conn.close()

    assert main(["predict", "run", "--asof", ASOF, "--db", str(env["db"]),
                 "--report-dir", str(env["reports"])]) == 1
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


# ---------- T3：`--asof 今天` 的「当日数据未定型」闸门（P46） ----------
#
# 背景（实测，见 `docs/errors/ERROR_DIARY.md` #60）：patrol 的 15:00 槽在
# 2026-09-22 补跑了 `predict run --asof 2026-09-22`，而当天的 K 线还是 12:06 那根
# **盘中半截 bar**（`ingest bars` 是 15:30 收盘链的第一步）。17 条 LIVE 预测写进
# append-only 的 `predictions` 后退不回来，15:30 收盘链重算必然全撞冲突。
#
# 闸门判据是 `bars_daily.fetched_at ≥ 当日 15:00`（`session.close.bars_finalized_on`）。
# 它**不是**墙上的钟：`--asof` 是历史日时完全不判（回放不受影响）。

def _today_is(monkeypatch, day: str) -> None:
    """把 CLI 的「今天」注入成 `day`（时钟是参数不是环境，ERROR_DIARY #31）。"""
    monkeypatch.setattr("stocklab.cli.main._today", lambda: day)


def _set_today_fetched_at(env, fetched_at: str) -> None:
    conn = connect(env["db"])
    conn.execute("UPDATE bars_daily SET fetched_at=? WHERE date=?", (fetched_at, ASOF))
    conn.commit()
    conn.close()


def _predictions_count(env) -> int:
    conn = connect(env["db"])
    n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    conn.close()
    return n


def test_asof_today_with_an_intraday_bar_is_refused_before_writing_anything(
        env, monkeypatch, capsys):
    """事故复现：今天的 K 线是盘中采的（`fetched_at` = 12:06）→ **exit 2、一行不写**。"""
    _today_is(monkeypatch, ASOF)
    _set_today_fetched_at(env, f"{ASOF}T12:06:00+08:00")

    assert run(env) == 2
    err = capsys.readouterr().err
    assert "未定型" in err and "12:06" in err and "15:00" in err
    assert _predictions_count(env) == 0          # fail-closed：一行都没写

    report = env["reports"] / f"{ASOF}-predict-{ASOF}.json"
    assert not report.exists()                   # 报告也不落盘（没有可复现的产物）


def test_asof_today_with_a_final_bar_is_allowed(env, monkeypatch, capsys):
    """收盘链的形状：`ingest bars` 已把当天 K 线刷成终值 → 照常出预测。"""
    _today_is(monkeypatch, ASOF)
    _set_today_fetched_at(env, f"{ASOF}T15:30:03+08:00")

    assert run(env) == 0
    assert _predictions_count(env) == 1
    assert "payload_sha256" in capsys.readouterr().out


def test_asof_today_without_any_bar_row_is_refused(env, monkeypatch, capsys):
    """当天连一根 K 线都没有 → 证不出定型，同样拒绝（fail-closed）。"""
    _today_is(monkeypatch, ASOF)
    conn = connect(env["db"])
    conn.execute("DELETE FROM bars_daily WHERE date=?", (ASOF,))
    conn.commit()
    conn.close()

    assert run(env) == 2
    assert "未定型" in capsys.readouterr().err
    assert _predictions_count(env) == 0


def test_a_historical_asof_is_never_gated(env, monkeypatch, capsys):
    """回放不受影响：`--asof` 是历史日时闸门**完全不参与**，哪怕它当天 fetched_at 很旧。

    这条是闸门的边界 —— 判据只回答「今天能不能出预测」，不该顺手把
    「历史日复算」这条整个预测体系赖以存在的路也堵上。
    """
    _today_is(monkeypatch, "2026-09-22")
    _set_today_fetched_at(env, f"{ASOF}T12:06:00+08:00")     # ASOF 是历史日

    assert run(env) == 0
    assert _predictions_count(env) == 1
    capsys.readouterr()


def test_the_finality_judgement_never_reads_a_clock(env):
    """判据是**数据**（`bars_daily.fetched_at`）不是**时刻** —— 签名里就没有「现在几点」。

    节假日 / 临时休市 / 机器睡醒补跑都会骗过「时间 ≥ 15:30」这类判据（P46 §T3 明令
    不许拿它当唯一判据）。这条断言把「不读时钟」变成结构性的：想加时钟进来就得先改
    签名，而改签名会当场红。
    """
    import inspect

    from stocklab.session import close as close_mod

    assert list(inspect.signature(close_mod.bars_finalized_on).parameters) == [
        "conn", "trade_date"]
