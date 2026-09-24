"""P60 T4：横截面单变量实验（`research xsec-topn`）的可用例。

三组：

1. **红线式等价** —— `hold_override` 是**唯一**的产品参数，缺省 `None` 必须逐位
   走现状；给了 `{日期: [code,…]}` 必须与**改动前**的 `_pools_for_test` 接缝逐位
   相同（黄金向量来自 `git show HEAD:stocklab/candidate/replay.py` 的原始实现）。
2. **成本口径**（硬约束 3）—— 持有集合含 ETF 时逐标的按 `instruments.type` 取
   `CostModel`；未知口径仍抛错。
3. **CLI** —— 三个 exit 2（`--pool mid` / `--start 2014-12-31` / 被篡改的预注册）
   各自**零输出**；零写库（跑前跑后逐表行数相同）；报告里的限定句与三条非 PIT 项。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from stocklab.backtest.portfolio import BoardUnknown
from stocklab.candidate import replay
from stocklab.cli.main import main
from stocklab.config.costs import CostModel
from stocklab.plugin import lifecycle, store
from stocklab.research import xsec
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-24T16:00:00+08:00"
PREREG = Path(__file__).resolve().parents[1] / "docs" / "experiments" / \
    "2026-09-24-xsec-topn.md"

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

#: 跑 CLI 用例时要断言「一行都没多」的表（任务书 T3 判据 3 的那 7 张）。
WATCH_TABLES = ("paper_accounts", "plugin_backtests", "plugin_audit",
                "plugin_scripts", "candidate_snapshots",
                "paper_agent_decisions", "paper_nav_daily")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _dates(n, start="2026-01-05"):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _db_with_bars(tmp_db, prices: dict[str, list[float]], dates: list[str],
                  assets: dict[str, str] | None = None):
    """标的 + 日历 + 逐日收盘价（价格序列与 dates 等长）。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    assets = assets or {}
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main',?,?)",
        [(code, code, assets.get(code, "stock"), NOW) for code in prices])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, p, p, p, p, NOW)
         for code, series in prices.items()
         for d, p in zip(dates, series)])
    c.commit()
    return c


def _seed_pipeline_db(tmp_db, *, n_weekdays=300, n_codes=10):
    """够跑 `score_pipeline` 的最小库：标的 + 日历 + K 线 + 5 个 active 插桩。

    标的数 > `POOL_TOPN['short'] = 6`，否则「前 6」与「全部合格」是同一个集合、
    Δ 恒为 0，跑出来的东西证明不了任何事。
    """
    init_db(tmp_db)
    c = connect(tmp_db)
    from stocklab.candidate.seeds import SEED_UNIVERSE
    codes = [i.code for i in SEED_UNIVERSE if i.is_stock][:n_codes]
    days: list[str] = []
    cur = date(2015, 1, 1)
    while len(days) < n_weekdays:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)

    inst = {i.code: i for i in SEED_UNIVERSE}
    for code in codes:
        i = inst[code]
        c.execute("INSERT INTO instruments (code, name, market, board, type,"
                  " added_at) VALUES (?,?,?,?,?,?)",
                  (i.code, i.name, i.market, i.board, i.asset_type, NOW))
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('sh000300','沪深300','sh','main','index',?)",
              (NOW,))
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'t',?)", [(d, NOW) for d in days])

    rows = []
    for n, code in enumerate(codes):
        px = 10.0 + n
        for k, d in enumerate(days):
            # 每只不同漂移，幅度小到不会连续跌停（避免被 screen 淘汰）
            px *= 1.0 + 0.0004 * ((k % 7) - 3) + 0.0002 * (n - 4)
            rows.append((code, d, px, px, px, px, 1000, "none", "x", NOW))
    px = 3000.0
    for k, d in enumerate(days):
        px *= 1.0 + 0.0002 * ((k % 11) - 5)
        rows.append(("sh000300", d, px, px, px, px, 1000, "none", "x", NOW))
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    for pid, text in PLUGINS.items():
        sid = store.insert_script(c, plugin_id=pid, version="1.0.0",
                                  source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
    c.commit()
    c.close()
    return days


def _write_prereg(path: Path, **over) -> Path:
    """写一份与仓库预注册同形的 md（可覆盖 json 字段，用于篡改用例）。"""
    data = {"experiment": "xsec-topn", "pool": "short", "start": "2015-01-01",
            "topn": 6, "hold_arm": "topn", "hold_control": "all-eligible",
            "min_periods": 120, "bootstrap_n": 2000,
            "bootstrap_seed": 20260918, "benchmark": "sh000300",
            "rule": "WIN = CI 下界 > 0"}
    data.update(over)
    path.write_text("# 夹具预注册\n\n```json\n"
                    + json.dumps(data, ensure_ascii=False) + "\n```\n",
                    encoding="utf-8")
    return path


def _run_cli(db, out, prereg, *extra):
    return main(["research", "xsec-topn", "--pool", "short",
                 "--start", "2015-01-01", "--end", "2016-06-30",
                 "--prereg", str(prereg), "--out", str(out), "--db", str(db),
                 *extra])


def _counts(db) -> dict[str, int]:
    c = connect(db)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in WATCH_TABLES}
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 1. 红线式等价：hold_override 缺省 = 现状；给了 = 与改动前逐位相同
# ---------------------------------------------------------------------------

#: 黄金向量：用 **改动前** 的 `period_returns`（`git show HEAD:...replay.py`）
#: 在同一夹具上算出来的。`==` 精确比较，不用 approx —— 这就是「逐位相同」。
GOLDEN_PRE_CHANGE = [0.06961998699869994, -0.021352819605552353,
                     -0.07533407407407407]


def _equivalence_fixture(tmp_db):
    dates = _dates(8)
    c = _db_with_bars(tmp_db, {
        # 000651 在 d2 相对 d1 +10% → d2 涨停（买入腿被挡）
        "000333": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4],
        "000651": [20.0, 20.0, 22.0, 22.5, 22.0, 21.5, 21.0, 20.5],
        # 600690 在 d4 相对 d3 −10% → d4 跌停（卖出腿被挡）
        "600690": [30.0, 30.0, 30.0, 30.0, 27.0, 26.0, 25.0, 24.0],
    }, dates)
    holds = {dates[0]: ["000333", "000651"],
             dates[2]: ["000333", "000651", "600690"],
             dates[4]: ["600690"],
             dates[6]: []}                      # 空池 → 清仓成本
    return c, [dates[0], dates[2], dates[4], dates[6]], holds


def test_hold_override_reproduces_pre_change_vector(tmp_db):
    """`hold_override=…` 与**改动前**的接缝路径逐位相同（黄金向量硬编码）。

    夹具只用**股票**标的：硬约束 3 对 ETF 的口径修正**有意**改变了 ETF 的成本，
    所以「逐位相同」这一条只在股票持有集合上成立（ETF 那条见下一个用例）。
    """
    c, marks, holds = _equivalence_fixture(tmp_db)
    got = replay.period_returns(c, asof_dates=marks, pool="short",
                                hold_override=holds)
    assert got == GOLDEN_PRE_CHANGE
    # 同一份成员走测试接缝，也必须逐位相同 —— 两条路只是「谁提供成员」不同
    seam = replay.period_returns(c, asof_dates=marks, pool="short",
                                 _pools_for_test=holds)
    assert seam == GOLDEN_PRE_CHANGE
    c.close()


def test_hold_override_none_is_the_status_quo(tmp_db):
    """缺省 `None` ⇒ 逐位走现状：与「按 `score_pipeline` 现算成员喂接缝」相同。

    这是「加这个参数没有改变既有行为」的红线：若 `None` 分支被人改成别的
    取成员方式（比如默认走 override 的空表 → 全现金），这里立刻变红。
    """
    from stocklab.candidate.run import score_pipeline

    dates = _seed_pipeline_db(tmp_db, n_weekdays=300)
    marks = replay.rebalance_marks(connect(tmp_db), pool="short",
                                   start=dates[0], end=dates[-1])
    c = connect(tmp_db)
    try:
        expected = replay.period_returns(
            c, asof_dates=marks, pool="short",
            _pools_for_test={
                d: sorted(m.code for m in
                          score_pipeline(c, asof=d).members
                          if m.pool == "short")
                for d in marks})
        got = replay.period_returns(c, asof_dates=marks, pool="short")
        assert got == expected
    finally:
        c.close()


def test_override_and_test_seam_together_are_rejected(tmp_db):
    """两条路都声称决定成员 → 报错，不静默取其一。"""
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    with pytest.raises(ValueError, match="不能同时传"):
        replay.period_returns(c, asof_dates=[dates[0], dates[5]], pool="short",
                              hold_override={dates[0]: ["000333"]},
                              _pools_for_test={dates[0]: ["000333"]})
    c.close()


def test_override_only_wins_over_score_pipeline(tmp_db):
    """给了 `hold_override` 就不再跑 `score_pipeline` —— 用**空**的成员表验证。

    夹具里没有插桩（`score_pipeline` 必然抛 `NoActivePlugin`），若 override 生效
    就该**跑得通**且收益恒为 0（空仓持现金、无换手、无成本）。
    """
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    got = replay.period_returns(
        c, asof_dates=[dates[0], dates[3], dates[5]], pool="short",
        hold_override={d: [] for d in (dates[0], dates[3], dates[5])})
    assert got == [0.0, 0.0]
    c.close()


# ---------------------------------------------------------------------------
# 2. 成本口径（硬约束 3）
# ---------------------------------------------------------------------------

def test_etf_hold_uses_etf_cost_caliber(tmp_db):
    """持有集合含 ETF → 逐标的按 `instruments.type` 取口径（ETF 免印花税/过户费）。

    同一段平坦行情、同一个「持一只→清仓」的周期，唯一差别是标的口径。
    ETF 一腿卖出只付佣金；股票还要付印花税 0.05% + 过户费 0.001%。
    """
    from stocklab.config.replay import POSITION_NOTIONAL

    dates = _dates(6)
    c = _db_with_bars(tmp_db,
                      {"000333": [10.0] * 6, "510300": [10.0] * 6},
                      dates, assets={"510300": "etf"})

    def _period(code: str) -> float:
        r = replay.period_returns(
            c, asof_dates=[dates[0], dates[5]], pool="short",
            hold_override={dates[0]: [code], dates[5]: []})
        return r[0]

    px, qty = 10.0, int(POSITION_NOTIONAL / 10.0)
    fees = {k: CostModel(asset_class=k).fees("sell", px, qty)
            for k in ("stock", "etf")}
    slip = CostModel().slippage_bps / 10_000.0
    assert _period("000333") == pytest.approx(-(fees["stock"] / (px * qty) + slip))
    assert _period("510300") == pytest.approx(-(fees["etf"] / (px * qty) + slip))
    # ETF 一腿便宜 = 印花税 + 过户费那部分；方向必须是「ETF 成本更低」
    assert fees["etf"] < fees["stock"]
    assert _period("510300") > _period("000333")
    c.close()


def test_unknown_asset_caliber_still_raises(tmp_db):
    """`costs.py` 对未知口径抛错的行为**保留**，不许降级成「默认成本」。"""
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates,
                      assets={"000333": "warrant"})
    with pytest.raises(ValueError, match="未知标的口径"):
        replay.period_returns(
            c, asof_dates=[dates[0], dates[5]], pool="short",
            hold_override={dates[0]: ["000333"], dates[5]: []})
    c.close()


def test_missing_instrument_raises_board_unknown(tmp_db):
    """有 K 线但不在 `instruments` 表里的 code → 报错，不静默按股票口径算。"""
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES ('999999',?,10,10,10,10,1000,"
        "'none','x',?)", [(d, NOW) for d in dates])
    c.commit()
    with pytest.raises(BoardUnknown):
        replay.period_returns(
            c, asof_dates=[dates[0], dates[5]], pool="short",
            hold_override={dates[0]: ["000333"], dates[5]: ["999999"]})
    c.close()


def test_explicit_costs_is_not_overridden_by_caliber(tmp_db):
    """调用方**显式**传 `costs` 时不做口径分支 —— 否则「压零成本」会被静默覆盖。"""
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"510300": [10.0] * 6}, dates,
                      assets={"510300": "etf"})
    zero = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0, slippage_bps=0.0)
    got = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=zero,
        hold_override={dates[0]: ["510300"], dates[5]: []})
    assert got == [0.0]
    c.close()


# ---------------------------------------------------------------------------
# 3. CLI：三个 exit 2（零输出）、零写库、报告字符串
# ---------------------------------------------------------------------------

def test_cli_pool_mid_exits_2_with_zero_output(tmp_db, tmp_path):
    out = tmp_path / "out"
    rc = main(["research", "xsec-topn", "--pool", "mid", "--start", "2015-01-01",
               "--prereg", str(PREREG), "--out", str(out), "--db", str(tmp_db)])
    assert rc == 2
    assert not out.exists()


def test_cli_start_before_2015_exits_2_with_zero_output(tmp_db, tmp_path):
    out = tmp_path / "out"
    rc = main(["research", "xsec-topn", "--pool", "short",
               "--start", "2014-12-31", "--prereg", str(PREREG),
               "--out", str(out), "--db", str(tmp_db)])
    assert rc == 2
    assert not out.exists()


def test_cli_tampered_prereg_exits_2_with_zero_output(tmp_db, tmp_path):
    """预注册被改（`topn` 6→7）→ 拒跑。这也是「先提交预注册再跑」的守门人。"""
    _seed_pipeline_db(tmp_db, n_weekdays=300)
    out = tmp_path / "out"
    bad = _write_prereg(tmp_path / "bad.md", topn=7)
    rc = _run_cli(tmp_db, out, bad)
    assert rc == 2
    assert not out.exists()


@pytest.mark.parametrize("field,value", [
    ("pool", "mid"), ("start", "2019-01-01"), ("min_periods", 30),
    ("bootstrap_seed", 1), ("benchmark", "sh000905"),
    ("hold_control", "top-half"),
])
def test_cli_each_prereg_field_mismatch_exits_2(tmp_db, tmp_path, field, value):
    """逐字段 fail-closed：改**任何一个**被校验的字段都拒跑。"""
    _seed_pipeline_db(tmp_db, n_weekdays=300)
    out = tmp_path / "out"
    bad = _write_prereg(tmp_path / "bad.md", **{field: value})
    assert _run_cli(tmp_db, out, bad) == 2
    assert not out.exists()


def test_cli_missing_prereg_file_exits_2(tmp_db, tmp_path):
    out = tmp_path / "out"
    rc = _run_cli(tmp_db, out, tmp_path / "nope.md")
    assert rc == 2
    assert not out.exists()


def test_cli_prereg_without_json_block_exits_2(tmp_db, tmp_path):
    out = tmp_path / "out"
    p = tmp_path / "nojson.md"
    p.write_text("# 只有散文，没有 json 块\n", encoding="utf-8")
    assert _run_cli(tmp_db, out, p) == 2
    assert not out.exists()


def test_cli_missing_db_exits_2(tmp_path):
    out = tmp_path / "out"
    rc = _run_cli(tmp_path / "absent.db", out, PREREG)
    assert rc == 2
    assert not out.exists()


def test_cli_end_to_end_writes_reports_and_no_table(tmp_db, tmp_path):
    """exit 0 + 两个报告文件都在 + **七张表逐表行数不变**（零写库）。"""
    days = _seed_pipeline_db(tmp_db, n_weekdays=300)
    before = _counts(tmp_db)
    out = tmp_path / "out"
    rc = _run_cli(tmp_db, out, PREREG)
    assert rc == 0

    json_p = out / "2016-06-30-xsec-topn.json"
    md_p = out / "2016-06-30-xsec-topn.md"
    assert json_p.is_file() and md_p.is_file()
    assert _counts(tmp_db) == before, "本命令不得写任何表"

    report = json.loads(json_p.read_text(encoding="utf-8"))
    assert report["pool"] == "short" and report["start"] == "2015-01-01"
    assert report["topn"] == 6
    assert report["prereg_sha256"] == \
        __import__("hashlib").sha256(PREREG.read_bytes()).hexdigest()
    assert report["n_periods"] == report["n_marks"] - 1
    assert set(report["arms"]) == {"topn", "all"}
    assert report["delta"]["n_periods"] == report["n_periods"]
    assert report["delta"]["verdict"] in ("WIN", "LOSE", "INCONCLUSIVE")
    # 两臂的周期数必须对齐，否则 Δ 是「错位相减」，读数无意义
    assert len(report["arms"]["all"]["period_returns"]) == report["n_periods"]
    assert len(report["arms"]["topn"]["period_returns"]) == report["n_periods"]
    d = report["delta"]
    assert d["n_train"] + d["n_validate"] == d["n_periods"]
    assert report["elapsed_s"] >= 0.0 and days[0] == "2015-01-01"


def test_report_carries_required_caliber_strings(tmp_db, tmp_path):
    """硬约束 6：三条非 PIT 项、成本口径偏差、选择偏差限定句必须进报告。"""
    _seed_pipeline_db(tmp_db, n_weekdays=300)
    out = tmp_path / "out"
    assert _run_cli(tmp_db, out, PREREG) == 0
    md = (out / "2016-06-30-xsec-topn.md").read_text(encoding="utf-8")
    for s in xsec.NON_PIT_ITEMS:
        assert s in md
    assert xsec.COST_CALIBER_NOTE in md
    assert "在这 21 只、这段历史上成立" in md
    assert "ST 判定" in md and "行业分类" in md and "事后挑选" in md
    # nanobot 直接贴给用户的那一行，必须是**最后一行**
    report = json.loads(
        (out / "2016-06-30-xsec-topn.json").read_text(encoding="utf-8"))
    last = md.rstrip().splitlines()[-1]
    assert last.startswith("summary: xsec-topn pool=short")
    assert report["delta"]["verdict"] in last


def test_non_pit_items_are_the_three_from_the_design_doc():
    """三条非 PIT 项就是设计稿 §Q3 点名的三条（防被静默改写/删条）。"""
    assert len(xsec.NON_PIT_ITEMS) == 3
    assert xsec.NON_PIT_ITEMS[0].startswith("非 PIT ①") \
        and "ST" in xsec.NON_PIT_ITEMS[0]
    assert xsec.NON_PIT_ITEMS[1].startswith("非 PIT ②") \
        and "sector" in xsec.NON_PIT_ITEMS[1]
    assert xsec.NON_PIT_ITEMS[2].startswith("非 PIT ③") \
        and "事后挑选" in xsec.NON_PIT_ITEMS[2]


def test_repo_prereg_is_the_one_the_task_book_pinned():
    """仓库里那份预注册的 json 必须逐字段等于任务书 T1 钉死的值。

    预注册一旦提交就**不许改**（要改只能追加新实验）；这个用例就是那条纪律的
    守门人 —— 有人顺手改了 `start` / `topn`，这里会红。
    """
    data, sha = xsec.load_prereg(PREREG)
    assert data == {
        "experiment": "xsec-topn", "pool": "short", "start": "2015-01-01",
        "topn": 6, "hold_arm": "topn", "hold_control": "all-eligible",
        "min_periods": 120, "bootstrap_n": 2000,
        "bootstrap_seed": 20260918, "benchmark": "sh000300",
        "rule": "WIN = CI 下界 > 0；CI 跨 0 ⇒ 如实写「无可测的选股增量」"}
    assert len(sha) == 64
    # topn 必须是**代码读出来的**那个值，不是手抄的巧合
    from stocklab.candidate.pools import POOL_TOPN
    assert data["topn"] == POOL_TOPN["short"] == 6


def test_cli_arm_single_reports_no_delta(tmp_db, tmp_path):
    """`--arm topn` 只跑单臂：没有 Δ，也就没有 verdict（不假装有对照）。"""
    _seed_pipeline_db(tmp_db, n_weekdays=300)
    out = tmp_path / "out"
    assert _run_cli(tmp_db, out, PREREG, "--arm", "topn") == 0
    report = json.loads(
        (out / "2016-06-30-xsec-topn.json").read_text(encoding="utf-8"))
    assert set(report["arms"]) == {"topn"}
    assert report["delta"] is None
    assert "单臂" in xsec.summary_line(report)


def test_prereg_out_dir_default_is_under_reports(tmp_path, monkeypatch):
    """默认产物目录 = `reports/research/`，且**调用时**读 `paths.REPORT_DIR`。"""
    from stocklab.config import paths
    monkeypatch.setattr(paths, "REPORT_DIR", tmp_path / "reports")
    assert xsec.default_out_dir() == tmp_path / "reports" / "research"


def test_tampering_with_prereg_after_the_fact_is_detected(tmp_path):
    """改一个字节 ⇒ sha256 变 ⇒ 报告里的 `prereg_sha256` 对不上原文件。"""
    p = _write_prereg(tmp_path / "p.md")
    _, sha1 = xsec.load_prereg(p)
    body = p.read_text(encoding="utf-8")
    p.write_text(body.replace('"topn": 6', '"topn": 6 '), encoding="utf-8")
    _, sha2 = xsec.load_prereg(p)
    assert sha1 != sha2


def test_research_module_does_not_borrow_paper_or_m2_calibers():
    """T2 的分层要求：新模块只依赖 `candidate` / `plugin` / `config`。

    价格侧 PIT 守卫落在 `candidate/replay.py`（`_close_on` 所在的那一侧，设计稿
    §Q3「给回放侧补一个结构位」），所以研究模块**不需要**也不许 import
    `paper` / `m2` 的内部来借口径。
    """
    src = (Path(__file__).resolve().parents[1] / "stocklab" / "research"
           / "xsec.py").read_text(encoding="utf-8")
    assert "stocklab.paper" not in src
    assert "stocklab.m2" not in src
    # 判定常量与 CI 必须**import** 自 plugin/sandbox.py，不许在本模块抄一遍
    assert "MIN_VALID_PERIODS = 120" not in src
    assert "2000" not in src and "20260918" not in src
