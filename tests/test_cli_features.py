"""`features build` 子命令（Task 20）：离线只读 bars_daily，可重复运行。

覆盖：参数解析 / 落库 / 幂等 / 历史不足留痕 / code 过滤 / 行情被修订后的冲突上报。
"""

import json

import pytest

from stocklab.cli.main import (
    _cmd_features_build, build_parser, cmd_features_build, cmd_ingest_actions,
)
from stocklab.config import paths
from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.data.models import Bar, CorpAction
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T20:00:00+08:00"
ASOF = "2026-03-20"


def _bars(code="000333", n=80, base=10.0):
    out = []
    for i in range(n):
        c = base + i * 0.1
        out.append(Bar(code=code, date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                       open=c - 0.05, high=c + 0.15, low=c - 0.15, close=c,
                       volume=1000 + i, amount=(1000 + i) * c, turnover=1.0,
                       source="test"))
    return out


def _seed(tmp_db, codes=("000333",), n=80):
    """只登记有行情的标的 —— active 但无 K 线会额外触发 skipped，干扰断言。"""
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(
        conn, tuple(i for i in DEFAULT_UNIVERSE if i.code in codes), now=NOW)
    for code in codes:
        repo.insert_bars(conn, _bars(code, n), now=NOW)
    conn.close()


# ---------- 参数解析 ----------

def test_features_build_subcommand():
    args = build_parser().parse_args(["features", "build", "--date", "2026-09-14"])
    assert args.command == "features"
    assert args.date == "2026-09-14"
    assert args.code is None
    assert args.func is _cmd_features_build


def test_features_build_requires_date():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["features", "build"])


def test_features_build_accepts_multiple_codes():
    args = build_parser().parse_args(
        ["features", "build", "--date", "2026-09-14", "--code", "000333",
         "--code", "600690"])
    assert args.code == ["000333", "600690"]


def test_features_build_rejects_unknown_action():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["features", "backfill"])


# ---------- 落库与幂等 ----------

def test_features_build_writes_snapshot(tmp_db, monkeypatch, capsys):
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build(ASOF, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"date": ASOF, "written": 1, "identical": 0, "conflicts": [],
                   "skipped": [], "feature_version": "v2"}

    conn = connect(tmp_db)
    row = conn.execute("SELECT code, date, feature_version, payload_hash,"
                       " data_version FROM features_daily").fetchone()
    conn.close()
    assert row["code"] == "000333"
    assert row["date"] == ASOF
    assert row["feature_version"] == "v2"
    assert row["data_version"] == f"bars:{ASOF};adj:0events"
    assert len(row["payload_hash"]) == 64


def test_features_build_is_idempotent(tmp_db, monkeypatch, capsys):
    """同一份数据重复运行：不得重复写入，且必须报告 identical（可安全重试）。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build(ASOF, None) == 0
    first = json.loads(capsys.readouterr().out)
    assert cmd_features_build(ASOF, None) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["written"] == 1 and second["written"] == 0
    assert second["identical"] == 1

    conn = connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0] == 1
    conn.close()


def _revise_last_bar_on(tmp_db, date, delta=5.0):
    """把 `date` 当日的收盘价改掉（模拟源站修订历史行情）。"""
    bars = _bars("000333", 80)
    revised = [b for b in bars if b.date == date]
    assert revised, f"{date} 不在造数范围内"
    old = revised[0]
    conn = connect(tmp_db)
    repo.insert_bars(conn, [Bar(code=old.code, date=old.date, open=old.open,
                                high=old.high + delta, low=old.low,
                                close=old.close + delta, volume=old.volume,
                                amount=old.amount, turnover=old.turnover,
                                source=old.source)], now=NOW)
    conn.close()


def test_features_build_reports_conflict_when_asof_bars_revised(tmp_db, monkeypatch,
                                                                capsys):
    """asof 当日行情被修订 → 同键重算结果不同 → 必须显式上报，而不是静默覆盖。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    cmd_features_build(ASOF, None)
    capsys.readouterr()

    _revise_last_bar_on(tmp_db, ASOF)

    assert cmd_features_build(ASOF, None) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["conflicts"] == ["000333"]
    assert out["written"] == 0

    conn = connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0] == 1
    events = [r["message"] for r in conn.execute(
        "SELECT message FROM system_events WHERE level='warn'")]
    conn.close()
    assert any("不一致" in m for m in events)


def test_features_build_ignores_revision_of_future_bars(tmp_db, monkeypatch,
                                                        capsys):
    """端到端 PIT：修订 asof **之后**的行情，当日快照必须一模一样（identical）。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    cmd_features_build(ASOF, None)
    capsys.readouterr()

    _revise_last_bar_on(tmp_db, "2026-03-24")   # asof 之后

    assert cmd_features_build(ASOF, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"date": ASOF, "written": 0, "identical": 1, "conflicts": [],
                   "skipped": [], "feature_version": "v2"}


# ---------- 缺失与过滤 ----------

def test_features_build_skips_insufficient_history(tmp_db, monkeypatch, capsys):
    """历史不足 → 记 skipped + warn 事件，不写假快照。"""
    _seed(tmp_db, n=10)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build("2026-01-05", None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == ["000333"]
    assert out["written"] == 0

    conn = connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0] == 0
    events = list(conn.execute("SELECT message, context_json FROM system_events"
                               " WHERE module='features' AND level='warn'"))
    conn.close()
    assert len(events) == 1
    assert json.loads(events[0]["context_json"])["last_bar"] == "2026-01-05"


def test_features_build_skips_non_trading_asof_date(tmp_db, monkeypatch, capsys):
    """asof 当日无 K 线（周末/停牌）→ skipped，禁止拿前一日的价格贴当天标签。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    sum_ = _bars("000333", 80)
    assert all(b.date != "2026-03-25" for b in sum_)
    cmd_features_build("2026-03-25", None)
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == ["000333"]


def test_features_build_respects_code_filter(tmp_db, monkeypatch, capsys):
    _seed(tmp_db, codes=("000333", "600690"))
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build(ASOF, ["600690"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["written"] == 1

    conn = connect(tmp_db)
    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM features_daily ORDER BY code")]
    conn.close()
    assert codes == ["600690"]


def test_features_build_missing_db_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "nope.db")
    assert cmd_features_build(ASOF, None) == 2
    assert "db init" in json.loads(capsys.readouterr().out)["error"]


# ---------- ADR-004：特征在**复权价**上计算（v2） ----------

def _seed_with_dividend(tmp_db):
    """造一根除权日：前收 10.00，10 派 2 元 → 理论除权价 9.80，行情正好开在 9.80。

    于是**真实收益为 0**；不复权序列看到的是 -2.00% 的假跌幅。
    """
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(
        conn, tuple(i for i in DEFAULT_UNIVERSE if i.code == "000333"), now=NOW)
    bars = _bars("000333", 76, base=8.0)      # 末根 = 2026-03-20（除权前一日）
    ex_close = bars[-1].close - 0.2           # 理论除权价 = 前收 - 每股派息 0.2
    bars.append(Bar(code="000333", date="2026-03-21", open=ex_close,
                    high=ex_close * 1.001, low=ex_close * 0.999, close=ex_close,
                    volume=1000, amount=None, turnover=None, source="test"))
    repo.insert_bars(conn, bars, now=NOW)
    repo.insert_corp_actions(conn, [CorpAction(
        code="000333", cqr="2026-03-21", djr="2026-03-20",
        content="10派2元", fh_sh=2.0)], now=NOW)
    conn.close()
    return bars


def test_features_use_adjusted_prices_on_ex_dividend_day(tmp_db, monkeypatch, capsys):
    """除权日快照的 `ret_1d` 必须是复权后的收益，不是 -2% 的假跌幅。

    与 `test_adjust.py::test_ex_dividend_day_has_no_fake_drop` 是同一失效模式，
    但这一条走**完整 CLI 路径**（读库 → 建链 → 复权 → 算特征 → 落快照），
    证明「特征层真的切换到复权价了」，而不只是 adjust 模块本身正确。
    """
    bars = _seed_with_dividend(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build("2026-03-21", None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["written"] == 1

    conn = connect(tmp_db)
    row = conn.execute(
        "SELECT feature_version, ret_1d, close FROM features_daily").fetchone()
    conn.close()
    assert row["feature_version"] == "v2"
    # 不复权：9.80/10.00 - 1 = -2.00%；复权后：9.80/(10.00-0.20) - 1 = 0
    assert row["ret_1d"] == pytest.approx(0.0, abs=1e-9)
    assert row["close"] == pytest.approx(bars[-1].close)   # asof 当日锚定真实价


def test_features_skip_when_chain_is_unusable(tmp_db, monkeypatch, capsys):
    """链不可用 → 记 skipped 并留痕，**绝不退回不复权价写快照**。

    窗口跨越不可定价事件时，`adjust_bars` 报错（该日假跌幅无法还原），
    CLI 必须把它记成**缺失**而不是退回不复权价。
    """
    _seed_with_dividend(tmp_db)
    conn = connect(tmp_db)
    repo.insert_corp_actions(conn, [CorpAction(
        code="000333", cqr="2026-03-19", djr="2026-03-18",
        content="", fh_sh=None)], now=NOW)                 # 无原文 → 不可定价
    conn.close()
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build("2026-03-21", None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["written"] == 0 and out["skipped"] == ["000333"]
    conn = connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0] == 1
    conn.close()


def test_ingest_actions_subcommand():
    args = build_parser().parse_args(["ingest", "actions", "--code", "000333"])
    assert args.command == "ingest" and args.ingest_target == "actions"
    assert args.code == ["000333"] and args.start == "1990-01-01"
    assert args.func is cmd_ingest_actions


# ---------- P76：`load_chain` 必须与 `adjust_bars` 同一个保护边界 ----------

def test_p76_features_build_etf_does_not_abort_the_batch(tmp_db, monkeypatch, capsys):
    """真库实测：`active=1` 的 21 只里有 4 只 ETF，`load_chain` 对它们一律抛
    `EtfChainUnsupported`（ADR-008，设计如此）⇒ 边界留在 try 外时，每次都在
    **第 8 只**（300750 之后就是 510300）整轮 abort。

    改后：那只进 `skipped` + warn 事件、其余照跑、**返回 0**（无 conflicts）。
    这里用真 ETF（`510300`，`instruments.type='etf'`）走**真口径**，不打桩。
    """
    _seed(tmp_db, codes=("000333", "510300"), n=80)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build(ASOF, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == ["510300"]
    assert out["written"] == 1 and out["conflicts"] == []

    conn = connect(tmp_db)
    try:
        assert [r["code"] for r in conn.execute(
            "SELECT code FROM features_daily")] == ["000333"]
        ev = conn.execute("SELECT level, message, context_json FROM system_events"
                          " ORDER BY event_id").fetchall()
    finally:
        conn.close()
    assert [r["level"] for r in ev] == ["warn"]
    assert "510300" in ev[0]["message"] and "复权链不可用" in ev[0]["message"]
    ctx = json.loads(ev[0]["context_json"])
    # `510300` 排在 `000333` 之后：若循环开头没把 chain 清成 None，这里读到的是
    # **上一轮**的链 ⇒ 下面的 None 断言会红（脏值是隐形的，未绑定变量反而会 NameError）
    assert ctx["usable_from"] is None and ctx["n_unusable"] is None


def test_p76_features_build_bare_adjust_error_does_not_abort_the_batch(
        tmp_db, monkeypatch, capsys):
    """裸 `AdjustError`（P75 刻意保留的 `k > 1` / `pre_close ≤ 0` 档）同样只判这一只：
    记 `skipped` + warn、**不中断**、返回 0（`skipped` 不是失败，是既有契约）。"""
    _seed(tmp_db, codes=("000333", "600690"), n=80)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)

    from stocklab.data import adjust as adjust_mod
    real = adjust_mod.load_chain

    def _bad(c, code):
        if code == "600690":
            raise adjust_mod.AdjustError(
                "复权系数越界：pre_close=1.0 cash=2.0 share_ratio=0.0 → k=1.5")
        return real(c, code)

    monkeypatch.setattr(adjust_mod, "load_chain", _bad)
    assert cmd_features_build(ASOF, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == ["600690"]
    assert out["written"] == 1 and out["conflicts"] == []

