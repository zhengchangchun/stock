"""P72 T2/T4：四条 `ingest … --universe <id>` 的采集范围（**假 fetcher**，全程离线）。

判据（逐条都有对应用例）：

1. **不带 `--universe` 时采集集合与改前逐位相同**（21 只 active `instruments`）；
2. **带 `--universe` 时集合 = 宇宙全部成员**（**含 `active=0`** 的研究池成员）；
3. 文件缺失/坏 ⇒ **exit 2**（F3 fail-closed），且**零写入**；
4. `--code` 不在集合内 ⇒ 报错退出（ERROR_DIARY #44，不静默跳过）；
5. 既有的两条事实不被破坏：`ingest bars` **ETF 也采**；`ingest actions` **ETF 不进复权链**（ADR-008）。

Falsifiability：

- 若 `--universe` 只是「再叠一层 active 过滤」⇒ 800 只里 799 只被丢 ⇒ `len(...) == 800` 红。
- 若默认路径被顺手改成宇宙路径 ⇒ `len(...) == 21` 红。
- 若 `--universe` 回退（fail-open）⇒ 文件缺失用例的 `== 2` 红。
- 若 `--universe` 路径仍调 `upsert_instruments` ⇒ 库里多出 800 行 `active=1`
  （schema 默认）⇒ `test_explicit_universe_does_not_pollute_daily_scope` 红。

真源文件的**联网 build** 是 nanobot 的事（P71 §0.5.1）：这里用假 fetcher 现建到
`tmp_path`，并把 `resolve_universe` 的 `root` 指过去 —— **repo 的
`config/universes/` 一字不碰**。（`csi300-500.*` 已于 2026-09-25 纳管进仓，
`af41b05`；用例不读它，故与仓内有没有该文件无关。）
"""

from __future__ import annotations

import functools
import json
import re

import pytest

import stocklab.cli.main as main_mod
import stocklab.config.universes as U
from stocklab.candidate.seeds import SEED_UNIVERSE
from stocklab.cli.main import (build_parser, cmd_ingest_actions, cmd_ingest_bars,
                               cmd_ingest_moneyflow, cmd_ingest_valuation)
from stocklab.config import paths
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK, Instrument
from stocklab.data.models import Bar
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-25T16:00:00+08:00"
DATES = ["2026-09-10", "2026-09-11", "2026-09-14"]

#: 宇宙里那只**库里是 `active=0`** 的成员（下面 `db` fixture 把它按 inactive 插进库）。
INACTIVE_CODE = "605000"


def _fake_csi(index_type):
    """两页 300 ＋ 500 ⇒ 去重后 800 只。

    代码段 `605xxx` / `003xxx` 是**刻意选的**：① 前缀都在 `_BOARD_BY_PREFIX` 已收录范围内
    （不撞 P73 的 `302` 缺口）；② **与 `SEED_UNIVERSE` 的 21 只零交集** ——
    这样 sync 之后 `instruments` 的增量就正好是 800（判据 4 的 821 ＝ 21 ＋ 800）。
    真名单（沪深300 ∪ 中证500）与 21 只种子**重叠 16 只**，不满足这个前提，
    见任务书 §7.8。
    """
    if index_type == 1:
        return [{"code": f"605{i:03d}", "name": f"沪{i}", "sector": "银行Ⅱ"}
                for i in range(300)]
    return [{"code": f"003{i:03d}", "name": f"深{i}", "sector": None}
            for i in range(500)]


def _bars(code: str) -> list[Bar]:
    return [Bar(code=code, date=d, open=10.0, high=10.8, low=9.9, close=10.5,
                volume=1000, amount=10_500.0, turnover=1.0, source="test")
            for d in DATES]


def _write_universe(root, uid: str, members: tuple[Instrument, ...]) -> None:
    """手写一个宇宙文件（`build_universe` 只认两个 source，这里要混 ETF）。"""
    text = U.canonical_text(U.rows_from_members(members))
    meta = {"source": "test-fixture", "built_at": NOW, "pit": False,
            "n_members": len(members), "members_sha256": U.sha256_of(text)}
    (root / f"{uid}.csv").write_text(text, encoding="utf-8")
    (root / f"{uid}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8")


@pytest.fixture
def db(tmp_db, tmp_path, monkeypatch):
    """库 / raw_cache / universe 根全指到临时目录（真库与真 `config/universes/` 都不碰）。"""
    init_db(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    monkeypatch.setattr(paths, "RAW_CACHE_DIR", tmp_path / "raw_cache")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(paths, "REPORT_DIR", tmp_path / "reports")
    # 默认集合（不带 --universe）＝库内 active：21 只种子（`upsert_instruments` 用 schema
    # 默认 active=1 登记，与真库现状同形），外加一只**显式 active=0** 的研究池成员。
    conn = connect(tmp_db)
    repo.upsert_instruments(conn, SEED_UNIVERSE, now=NOW)
    conn.execute("INSERT INTO instruments (code, name, market, board, type, active,"
                 " added_at) VALUES (?,?,?,?,?,0,?)",
                 (INACTIVE_CODE, "浦发银行", "sh", "main", ASSET_STOCK, NOW))
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def uni(tmp_path, monkeypatch):
    """假 fetcher 现建 `csi300-500`（800 只）＋一个含 ETF 的 `mixed` 宇宙。"""
    root = tmp_path / "universes"
    U.build_universe("csi300+csi500", out_dir=root, fetcher=_fake_csi)
    _write_universe(root, "mixed", (
        Instrument("600519", "贵州茅台", "sh", "main", ASSET_STOCK),
        Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF),
    ))
    monkeypatch.setattr(main_mod, "resolve_universe",
                        functools.partial(U.resolve_universe, root=root))
    return root


def _spy(monkeypatch, name: str, seen: list[str], *, on_call=None):
    """把 `stocklab.data.fetch.<name>` 换成记录 code 的假 fetcher（绝不联网）。"""
    import stocklab.data.fetch as fetch_mod

    def fake(client, *, code, **kw):
        seen.append(code[-6:])            # 腾讯口径的 code 带市场前缀，取后 6 位
        return on_call(code) if on_call else []

    monkeypatch.setattr(fetch_mod, name, fake)


def _parse(argv):
    return build_parser().parse_args(argv)


def _instruments(db):
    return db.execute("SELECT COUNT(*) FROM instruments").fetchone()[0], \
        db.execute("SELECT COUNT(*) FROM instruments WHERE active=1").fetchone()[0]


# ---------- 1＋2：默认 21 只 vs 宇宙 800 只（逐位对账） ----------

def test_default_bars_scope_is_the_21_active_instruments(db, uni, monkeypatch, capsys):
    seen: list[str] = []
    _spy(monkeypatch, "fetch_daily_bars", seen, on_call=_bars)
    assert cmd_ingest_bars(_parse(["ingest", "bars", "--days", "30"])) == 0
    capsys.readouterr()
    assert len(seen) == 21
    assert set(seen) == {i.code for i in SEED_UNIVERSE}
    assert INACTIVE_CODE not in seen, "默认路径只认 active=1 —— 研究池成员不许被采"


def test_universe_bars_scope_is_all_800_members(db, uni, monkeypatch, capsys):
    seen: list[str] = []
    _spy(monkeypatch, "fetch_daily_bars", seen, on_call=_bars)
    args = _parse(["ingest", "bars", "--days", "30", "--universe", "csi300-500"])
    assert cmd_ingest_bars(args) == 0
    capsys.readouterr()
    assert len(seen) == 800, "显式宇宙 ⇒ **全部成员**，不受 active 过滤"
    assert len(set(seen)) == 800
    assert INACTIVE_CODE in seen, "库里 active=0 的成员也必须被采（F2）"


def test_universe_valuation_and_moneyflow_scope_is_all_800(db, uni, monkeypatch, capsys):
    for cmd, fetcher in ((cmd_ingest_valuation, "fetch_valuation_daily"),
                         (cmd_ingest_moneyflow, "fetch_money_flow_daily")):
        seen: list[str] = []
        _spy(monkeypatch, fetcher, seen)
        sub = "valuation" if cmd is cmd_ingest_valuation else "moneyflow"
        args = _parse(["ingest", sub, "--universe", "csi300-500"])
        assert cmd(args) == 0
        capsys.readouterr()
        assert len(seen) == 800, f"{sub} 显式宇宙 ⇒ 800 只"


def test_universe_actions_scope_is_all_800(db, uni, monkeypatch, capsys):
    seen: list[str] = []
    _spy(monkeypatch, "fetch_corp_actions", seen)
    args = _parse(["ingest", "actions", "--universe", "csi300-500"])
    assert cmd_ingest_actions(args) == 0
    capsys.readouterr()
    assert len(seen) == 800


def test_explicit_universe_does_not_pollute_daily_scope(db, uni, monkeypatch, capsys):
    """F2 的反面：显式宇宙**不许**顺手把研究池写进 `instruments`（那会 `active=1`）。"""
    before = _instruments(db)
    seen: list[str] = []
    _spy(monkeypatch, "fetch_daily_bars", seen, on_call=_bars)
    assert cmd_ingest_bars(
        _parse(["ingest", "bars", "--universe", "csi300-500"])) == 0
    capsys.readouterr()
    assert _instruments(db) == before == (22, 21), \
        "`--universe` 路径不许 upsert：否则 800 行以 schema 默认 active=1 灌进日更口径"


# ---------- 3：`--universe` fail-closed ----------

def test_missing_universe_file_exits_2_without_writing(db, uni, monkeypatch, capsys):
    before = _instruments(db)
    called: list[str] = []
    _spy(monkeypatch, "fetch_daily_bars", called)
    assert cmd_ingest_bars(_parse(["ingest", "bars", "--universe", "nope"])) == 2
    assert "nope" in capsys.readouterr().err
    assert called == [], "用法错误不许走到抓取"
    assert _instruments(db) == before
    assert db.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_missing_universe_file_exits_2_for_every_series(db, uni, monkeypatch, capsys):
    for cmd, sub in ((cmd_ingest_valuation, "valuation"),
                     (cmd_ingest_moneyflow, "moneyflow"),
                     (cmd_ingest_actions, "actions")):
        capsys.readouterr()
        assert cmd(_parse(["ingest", sub, "--universe", "nope"])) == 2, sub
        assert "nope" in capsys.readouterr().err


# ---------- 4：`--code` 语义不变（ERROR_DIARY #44） ----------

def test_code_outside_the_explicit_universe_is_rejected(db, uni, monkeypatch, capsys):
    called: list[str] = []
    _spy(monkeypatch, "fetch_daily_bars", called, on_call=_bars)
    args = _parse(["ingest", "bars", "--universe", "csi300-500", "--code", "999999"])
    assert cmd_ingest_bars(args) == 1
    err = capsys.readouterr().err
    assert "999999" in err and "拒绝静默跳过" in err
    assert called == []


def test_code_outside_the_explicit_universe_is_rejected_for_series(db, uni, capsys):
    for cmd, sub in ((cmd_ingest_valuation, "valuation"),
                     (cmd_ingest_moneyflow, "moneyflow"),
                     (cmd_ingest_actions, "actions")):
        capsys.readouterr()
        args = _parse(["ingest", sub, "--universe", "csi300-500", "--code", "999999"])
        assert cmd(args) == 1, sub
        assert "999999" in capsys.readouterr().err


def test_code_inside_the_explicit_universe_narrows_the_set(db, uni, monkeypatch, capsys):
    seen: list[str] = []
    _spy(monkeypatch, "fetch_daily_bars", seen, on_call=_bars)
    args = _parse(["ingest", "bars", "--universe", "csi300-500",
                   "--code", INACTIVE_CODE])
    assert cmd_ingest_bars(args) == 0
    capsys.readouterr()
    assert seen == [INACTIVE_CODE], "--code 在宇宙集合内 ⇒ 求交后只采它一只"


# ---------- 5：既有两条事实（ETF 也采 / ETF 不进复权链） ----------

def test_explicit_universe_keeps_etf_in_bars_but_out_of_actions(db, uni, monkeypatch,
                                                              capsys):
    bars_seen: list[str] = []
    _spy(monkeypatch, "fetch_daily_bars", bars_seen, on_call=_bars)
    assert cmd_ingest_bars(_parse(["ingest", "bars", "--universe", "mixed"])) == 0
    capsys.readouterr()
    assert sorted(bars_seen) == ["510300", "600519"], "bars **ETF 也采**（既有事实）"

    act_seen: list[str] = []
    _spy(monkeypatch, "fetch_corp_actions", act_seen)
    assert cmd_ingest_actions(_parse(["ingest", "actions", "--universe", "mixed"])) == 0
    capsys.readouterr()
    assert act_seen == ["600519"], "actions **ETF 不进复权链**（ADR-008，既有事实）"


# ---------- 形状：五个 ingest 的 `--universe` 同一文案 ----------

@pytest.mark.parametrize("sub", ["bars", "actions", "valuation", "moneyflow",
                                 "financials"])
def test_all_ingest_subcommands_share_the_same_universe_flag_wording(sub, capsys):
    from stocklab.cli.main import UNIVERSE_SCOPE_HELP

    assert _parse(["ingest", sub, "--universe", "seed21"]).universe == "seed21"
    with pytest.raises(SystemExit) as exc:
        _parse(["ingest", sub, "--help"])
    assert exc.value.code == 0
    # argparse 会按宽度折行 ⇒ 先去空白再比（文案本身逐字相同）。
    flat = re.sub(r"\s+", "", capsys.readouterr().out)
    assert re.sub(r"\s+", "", UNIVERSE_SCOPE_HELP) in flat, \
        "四条新加 + financials 必须共用 UNIVERSE_SCOPE_HELP 一份文案"
