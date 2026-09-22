"""P41 模块2 §4：绩效对比指标集（`paper metrics` + `/lab/paper` 新节）。

五组，对应用户需求 02 §4 点名的五个指标，以及**这个任务真正的重点** ——
每个数字都要自带样本量与非显著性标注，否则 6 个交易日的曲线会说谎。

| 组 | 断言 | 被摘掉后会红的实现 |
|---|---|---|
| 1 指标正确性 | 已知 NAV 序列 → 手算五个数逐位一致 | 指标公式与 `backtest/metrics` 漂移 |
| 2 门禁 | `n_sessions<120` → `insufficient` + 「样本不足」；`>=120` → `ok` | 短样本照样给「跑赢」的结论 |
| 3 基准缺失 | 删掉 `sh000300` 行 → 超额 `None` + 原因，不抛异常 | 拿相邻日顶替 / 静默算成 0 |
| 4 页面契约 | 既有节全在 + 新节字段齐 + 无 `None`/`nan` 裸值 | CLI 与页面各算一套（T4） |
| 5 CLI | 假 runner 注入 + `paths.DB_PATH` 指向 tmp，**零副作用** | ERROR_DIARY #55：CLI 用例写进真库 |

数字一律**从被测模块取**，不手抄一份到测试里 —— 手抄的那份会与上游漂移。
"""

import datetime
import json
import subprocess

import pytest

from stocklab.cli.main import main
from stocklab.labweb import paper_data, paper_render
from stocklab.paper import engine as paper_engine
from stocklab.paper import store as paper_store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-22T16:00:00+08:00"
START = "2026-09-15"
ACC = "arm-fixture"

#: 一条手算得出来的净值序列（+10% / −5% / +10% / −10% / +5%）。
#: 期初 1000 → 末日 1086.2775 ⇒ 总收益 8.62775%；峰值 1149.5 → 谷 1034.55 ⇒ 回撤 −10%。
NAVS = [1100.0, 1045.0, 1149.5, 1034.55, 1086.2775]
INITIAL = 1000.0
RETURNS = [0.10, -0.05, 0.10, -0.10, 0.05]
#: 指数：每日 +1% 的单调序列 —— 它**没有亏损日**，盈亏比因此算不出来（不是 0、不是 inf）。
INDEX_LEVELS = [4000.0, 4040.0, 4080.0, 4120.0, 4160.0, 4200.0]


def _dates(n: int, start: str = START) -> list[str]:
    """`start` 之后的 n 个**交易日**（本夹具不需要真实日历，单调递增即可）。"""
    base = datetime.date.fromisoformat(start)
    return [(base + datetime.timedelta(days=i)).isoformat()
            for i in range(1, n + 1)]


def _tiny_db(tmp_path, *, navs=NAVS, initial=INITIAL, initial_nav=None,
             with_index=True) -> tuple:
    """一份最小的库：**一个账户** + 一条给定净值序列（不跑 `paper step`）。

    直接插净值行而不是推进流水线，是为了让「手算的五个数」不被引擎的成交逻辑
    搅进来 —— 这一组测的是指标公式，不是下单。

    `initial_nav` 默认等于净入金；给它一个**不同**的值就复现真库起跑日浮盈的形态
    （2026-09-15：`initial_nav=20043` ÷ 净入金 20000），T4 那条断言要的正是这个差。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "metrics.db"
    init_db(path)
    c = connect(path)
    days = _dates(len(navs))
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main','etf',?)",
                  [(code, code, NOW) for code in ("510300", "sh000300")])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in [START, *days]])
    rows = [(code, d, v) for code, v in (("510300", 4.5),) for d in [START, *days]]
    if with_index:
        rows += [("sh000300", d, v) for d, v in zip([START, *days], INDEX_LEVELS)]
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, d, v in rows])
    c.commit()
    paper_store.insert_account(
        c, account_id=ACC, arm="discipline", etf_target_pct=10.0,
        start_date=START, initial_cash=initial, initial_positions=[],
        initial_nav=initial if initial_nav is None else initial_nav,
        params={"initial_capital": initial}, now=NOW)
    for d, nav in zip(days, navs):
        paper_store.insert_nav(
            c, account_id=ACC, date=d, cash=nav, positions=[], market_value=0.0,
            nav=nav, drawdown=0.0, cum_cost=0.0,
            # 与 `paper.engine` 落库时同一个写法（round 到 6 位），不是测试自己发明的
            cum_return=round(nav / initial - 1.0, 6),
            net_deposits=initial, index_300_level=None, index_300_asof=None, now=NOW,
            commit=False)
    c.commit()
    c.close()
    return path, days


def _perf(path, asof):
    c = connect(path)
    try:
        return paper_data.performance(c, asof)
    finally:
        c.close()


def _row(data, account_id=ACC):
    return next(r for r in data["rows"] if r["account_id"] == account_id)


def _index_row(data):
    return _row(data, paper_engine.INDEX_300_SYMBOL)


# ---------- 组 1：指标正确性 ----------

def test_five_metrics_match_a_hand_computed_series(tmp_path):
    """五个数逐个手算对账（总收益 / 年化 / 最大回撤 / 盈亏比 / 胜率）。"""
    path, days = _tiny_db(tmp_path)
    data = _perf(path, days[-1])
    row = _row(data)
    assert row["total_return"] == pytest.approx(1086.2775 / 1000.0 - 1.0)
    assert row["annualized_return"] == pytest.approx(
        (1086.2775 / 1000.0) ** (252 / 5) - 1.0)
    assert row["max_drawdown"] == pytest.approx(-0.10)          # 1034.55 / 1149.5 − 1
    assert row["win_rate"] == pytest.approx(3 / 5)
    assert row["profit_loss_ratio"] == pytest.approx(
        (sum(r for r in RETURNS if r > 0) / 3)
        / abs(sum(r for r in RETURNS if r < 0) / 2))
    assert row["n_sessions"] == 5


def test_total_return_is_the_compounded_product_of_the_daily_returns(tmp_path):
    """总收益 == Π(1+日收益) − 1：指标集内部必须自洽（口径只有一套）。"""
    path, days = _tiny_db(tmp_path)
    row = _row(_perf(path, days[-1]))
    prod = 1.0
    for r in RETURNS:
        prod *= 1.0 + r
    assert row["total_return"] == pytest.approx(prod - 1.0)


def test_the_new_section_matches_the_existing_cum_return_column(tmp_path):
    """T4（ADR-023 修正段 D-37）：新节的「总收益」与既有「累计收益」列**逐位一致**。

    真库的形态是起跑日持仓已有浮盈：`initial_nav`（20043）≠ 净入金（20000）。
    期初若取 `initial_nav`，新节的「总收益」会比既有列低一个**对全部臂相同**的
    常数（实测 0.2155%）—— 同一个页面上两个「总收益」差一个常数，
    读者只会读成「真差异」。所以期初取**净入金**（`paper_nav_daily.net_deposits`）。

    这条断言是防漂的钉子：以后谁把期初改回 `initial_nav`（或另算一个基），
    两列立刻不等，这里当场变红。
    """
    path, days = _tiny_db(tmp_path, initial=1000.0, initial_nav=1005.0)
    c = connect(path)
    try:
        tracked = paper_data.track(c, days[-1])
    finally:
        c.close()
    perf = _row(tracked["performance"])
    arm = next(a for a in tracked["arms"] if a["account_id"] == ACC)

    # 1) 期初确实是净入金：18.62775% 而不是以 initial_nav 为基的 8.08731%
    assert perf["total_return"] == pytest.approx(1086.2775 / 1000.0 - 1.0)
    assert perf["total_return"] != pytest.approx(1086.2775 / 1005.0 - 1.0)

    # 2) 与既有「累计收益」列逐位一致（那边落库时 round 到 6 位，所以这里也对齐它）
    assert round(perf["total_return"], 6) == arm["cum_return"]
    # 3) 页面上**渲染出来的字符串**也必须一模一样 —— 「逐位」是对读者而言的
    assert (paper_render.ratio_pct(perf["total_return"])
            == paper_render.ratio_pct(arm["cum_return"]))


def test_monotonic_benchmark_has_no_profit_loss_ratio_and_says_why(tmp_path):
    """指数每日 +1%：没有亏损日 ⇒ 盈亏比**算不出**（`None` + 原因），不是 0 也不是 inf。"""
    path, days = _tiny_db(tmp_path)
    idx = _index_row(_perf(path, days[-1]))
    assert idx["total_return"] == pytest.approx(4200.0 / 4000.0 - 1.0)
    assert idx["win_rate"] == pytest.approx(1.0)
    assert idx["profit_loss_ratio"] is None
    assert "profit_loss_ratio" in idx["missing"]
    assert idx["max_drawdown"] == pytest.approx(0.0)


def test_metrics_come_from_the_backtest_metrics_true_source(tmp_path):
    """五个数**不是**在展示层另写一份 —— 它必须等于 `backtest.metrics` 的同名量。"""
    from stocklab.backtest import metrics as bt

    path, days = _tiny_db(tmp_path)
    row = _row(_perf(path, days[-1]))
    assert row["annualized_return"] == pytest.approx(
        bt.annualize(row["total_return"], row["n_sessions"]))
    assert row["win_rate"] == pytest.approx(bt.win_rate(RETURNS))
    assert row["profit_loss_ratio"] == pytest.approx(bt.profit_loss_ratio(RETURNS))


def test_arm_hold_is_marked_as_the_frozen_do_nothing_arm(tmp_path):
    """`arm-hold` 是冻结快照 —— 数据里必须能认出它，页面文案才标注得出来。"""
    path, days = _tiny_db(tmp_path)
    c = connect(path)
    try:
        paper_store.insert_account(
            c, account_id="arm-hold", arm="hold", etf_target_pct=None,
            start_date=START, initial_cash=INITIAL, initial_positions=[],
            initial_nav=INITIAL, params={"initial_capital": INITIAL}, now=NOW)
    finally:
        c.close()
    data = _perf(path, days[-1])
    assert _row(data, "arm-hold")["kind"] == "hold"
    assert _row(data)["kind"] == "arm"
    assert _index_row(data)["kind"] == "benchmark"


# ---------- 组 2：样本量与显著性门禁 ----------

def test_six_sessions_is_insufficient_and_says_so(tmp_path):
    """需求 02 的 6 个交易日 → `insufficient`，并且**必须**出现那句提示。"""
    path, days = _tiny_db(tmp_path, navs=NAVS + [1090.0])
    data = _perf(path, days[-1])
    assert data["n_sessions"] == 6
    assert data["sample_gate"]["threshold"] == 120
    assert data["sample_gate"]["gate_status"] == "insufficient"
    assert data["sample_gate"]["meets"] is False
    assert "样本不足，仅供观察" in data["sample_gate"]["label"]
    assert any("样本不足" in n for n in data["notes"])


def test_130_sessions_clears_the_gate(tmp_path):
    path, days = _tiny_db(tmp_path, navs=[1000.0 + i for i in range(130)])
    data = _perf(path, days[-1])
    assert data["n_sessions"] == 130
    assert data["sample_gate"]["gate_status"] == "ok"
    assert data["sample_gate"]["meets"] is True


def test_insufficient_run_never_uses_conclusive_wording(tmp_path):
    """门禁没过时**只给数字**：「跑赢/跑输/优于/劣于」一个都不许出现。"""
    path, days = _tiny_db(tmp_path)
    data = _perf(path, days[-1])
    assert data["sample_gate"]["gate_status"] == "insufficient"
    html = paper_render.performance_block(data)
    for banned in ("跑赢", "跑输", "优于", "劣于", "领先", "胜过"):
        assert banned not in html, f"样本不足却出现了结论性措辞：{banned}"
    json_blob = paper_render.performance_text(data)
    for banned in ("跑赢", "跑输", "优于", "劣于", "领先", "胜过"):
        assert banned not in json_blob


def test_gate_status_flips_with_the_same_series_length(tmp_path):
    """单变量：只有 `n_sessions` 过门槛这一件事在变，没有第二个差异。"""
    short, d1 = _tiny_db(tmp_path / "short", navs=[1000.0 + i for i in range(119)])
    long_, d2 = _tiny_db(tmp_path / "long", navs=[1000.0 + i for i in range(120)])
    assert _perf(short, d1[-1])["sample_gate"]["gate_status"] == "insufficient"
    assert _perf(long_, d2[-1])["sample_gate"]["gate_status"] == "ok"


# ---------- 组 3：基准缺失 ----------

def test_missing_index_rows_yield_none_not_a_substituted_neighbour(tmp_path):
    """删掉 `sh000300` 的行 → 基准五个数全 `None` + 原因；不抛异常、不拿相邻日顶替。"""
    path, days = _tiny_db(tmp_path, with_index=False)
    data = _perf(path, days[-1])
    idx = _index_row(data)
    for key in paper_data.METRIC_KEYS:
        assert idx[key] is None, f"{key} 缺数据时必须为 None"
        assert key in idx["missing"], f"{key} 缺数据时必须给原因"
    assert data["excess_vs_index_300"][ACC] is None
    assert any("sh000300" in r for r in idx["missing"].values())


def test_gap_in_the_middle_also_invalidates_the_benchmark(tmp_path):
    """中间缺一天同样是**没有观测**，不许跨过它连一条收益出来。"""
    path, days = _tiny_db(tmp_path)
    c = connect(path)
    try:
        c.execute("DELETE FROM bars_daily WHERE code='sh000300' AND date=?",
                  (days[2],))
        c.commit()
        data = paper_data.performance(c, days[-1])
    finally:
        c.close()
    idx = _index_row(data)
    assert idx["total_return"] is None
    assert days[2] in idx["missing"]["total_return"]
    assert _row(data)["total_return"] is not None      # 账户那行不受影响


def test_excess_is_a_subtraction_and_absent_when_the_benchmark_is_absent(tmp_path):
    """超额 == 账户收益 − 基准收益（一次减法）；基准缺 → `None`，不是 0。"""
    path, days = _tiny_db(tmp_path)
    data = _perf(path, days[-1])
    assert data["excess_vs_index_300"][ACC] == pytest.approx(
        _row(data)["total_return"] - _index_row(data)["total_return"])
    assert data["excess_vs_index_300"][paper_engine.INDEX_300_SYMBOL] is None


def test_excess_vs_hold_is_absent_until_the_hold_arm_exists(tmp_path):
    """没有「不动」臂时，相对它的差值是 `None`（**不是 0**：那是「一样」的意思）。"""
    path, days = _tiny_db(tmp_path)
    assert _perf(path, days[-1])["excess_vs_hold"][ACC] is None
    c = connect(path)
    try:
        paper_store.insert_account(
            c, account_id="arm-hold", arm="hold", etf_target_pct=None,
            start_date=START, initial_cash=INITIAL, initial_positions=[],
            initial_nav=INITIAL, params={"initial_capital": INITIAL}, now=NOW)
        for d in days:
            paper_store.insert_nav(
                c, account_id="arm-hold", date=d, cash=INITIAL, positions=[],
                market_value=0.0, nav=INITIAL, drawdown=0.0, cum_cost=0.0,
                cum_return=0.0, net_deposits=INITIAL, index_300_level=None,
                index_300_asof=None, now=NOW, commit=False)
        c.commit()
    finally:
        c.close()
    data = _perf(path, days[-1])
    assert data["excess_vs_hold"][ACC] == pytest.approx(
        _row(data)["total_return"] - _row(data, "arm-hold")["total_return"])
    assert data["excess_vs_hold"]["arm-hold"] is None


def test_empty_window_is_an_honest_empty_state(tmp_path):
    """库里一个净值行都没有 → `available=False` + 原因，不是一张全 0 的表。"""
    path = tmp_path / "empty.db"
    init_db(path)
    data = _perf(path, "2026-09-22")
    assert data["available"] is False
    assert data["reason"]
    assert data["rows"] == []
    assert data["sample_gate"]["gate_status"] == "insufficient"


# ---------- 组 4：页面契约（CLI 与页面同源） ----------

def test_page_section_renders_exactly_the_data_function_numbers(tmp_path):
    """T4：页面拿到的 `performance` 就是 CLI 拿到的那个 —— `track()` 不自己算一遍。

    真正的钉子在这里：`track()["performance"]` 必须**等于** `performance()` 的结果。
    只断言「数字出现在 HTML 里」不够 —— 页面完全可以自己再算一份而仍然通过。
    """
    path, days = _tiny_db(tmp_path)
    data = _perf(path, days[-1])
    c = connect(path)
    try:
        tracked = paper_data.track(c, days[-1])["performance"]
    finally:
        c.close()
    assert tracked == data, "页面取数与 performance() 不同源 —— 两处必然走样"
    html = paper_render.performance_block(data)
    for key in paper_data.METRIC_KEYS:
        assert _row(data)[key] is not None
    assert paper_render.ratio_pct(_row(data)["total_return"]) in html
    assert paper_render.ratio_pct(_row(data)["win_rate"]) in html
    assert str(data["sample_gate"]["threshold"]) in html


def test_a_nav_row_on_the_start_date_does_not_duplicate_the_axis(tmp_path):
    """起跑日本身有净值行时，基准的轴**只占一个位置**（多一个会凭空多出 0% 的一天）。"""
    path, days = _tiny_db(tmp_path)
    c = connect(path)
    try:
        paper_store.insert_nav(
            c, account_id=ACC, date=START, cash=INITIAL, positions=[],
            market_value=0.0, nav=INITIAL, drawdown=0.0, cum_cost=0.0,
            cum_return=0.0, net_deposits=INITIAL, index_300_level=None,
            index_300_asof=None, now=NOW)
        data = paper_data.performance(c, days[-1])
    finally:
        c.close()
    idx = _index_row(data)
    # 基准轴 = 起跑日 + 5 行 → 5 个收益。修之前起跑日被放进去两次 ⇒ 6。
    assert idx["n_sessions"] == len(days), "起跑日被算了两遍"
    # 窗口本身确实多了那一天（净值行数从 5 变 6）—— 两件事必须能同时成立
    assert data["n_sessions"] == len(days) + 1
    assert _row(data)["n_sessions"] == len(days) + 1


def test_page_renders_no_bare_none_or_nan(tmp_path):
    """缺值必须是「未知」这类标记，不能是裸的 `None` / `nan`（ERROR_DIARY #50 家族）。"""
    path, days = _tiny_db(tmp_path, with_index=False)
    html = paper_render.performance_block(_perf(path, days[-1]))
    for bad in ("None", "nan", "NaN", "inf"):
        assert bad not in html, f"页面出现了裸值：{bad}"
    assert "未知" in html


def test_existing_paper_page_sections_survive(tmp_path):
    """既有节的标题**只增不减** —— 新节是加上去的，不是换掉的。"""
    path, days = _tiny_db(tmp_path)
    empty = _perf(path, "2026-09-14")          # 那天之前一行净值都没有
    assert empty["available"] is False
    html = paper_render.paper_page(
        {"available": False, "db_missing": False, "asof": "2026-09-14",
         "date": None, "dates": [], "n_sessions": 0, "arms": [], "index": None,
         "now_account_id": None, "mirror_equals_hold": None, "real_trades": [],
         "real_trades_after_start": None, "paper_trades": [], "ai": {},
         "agent": {}, "disclosure": [], "disclaimer": "", "sample_note": "",
         "start_date": START, "performance": empty},
        base="/lab", built_at=NOW)
    assert "还没有模拟盘净值" in html
    assert "绩效对比（模块2 §4）" in html


def test_metrics_are_rendered_as_a_table_with_the_gate_in_the_header(tmp_path):
    """表头要带 `n_sessions` 与门禁状态 —— 数字离开样本量就没有意义。"""
    path, days = _tiny_db(tmp_path)
    data = _perf(path, days[-1])
    html = paper_render.performance_block(data)
    for col in ("总收益", "年化", "最大回撤", "盈亏比", "胜率"):
        assert col in html
    assert str(data["n_sessions"]) in html
    assert "样本不足" in html
    assert paper_render.INDEX_LABEL in html


def test_drawdown_is_shown_with_the_pages_positive_convention(tmp_path):
    """`paper_data` 保留 `backtest/metrics` 的负号；显示沿用既有列的**正值**口径。

    符号是展示约定、不是第二套口径 —— 但同一页面上同一个事实只能有一种写法。
    """
    path, days = _tiny_db(tmp_path)
    data = _perf(path, days[-1])
    assert _row(data)["max_drawdown"] == pytest.approx(-0.10)      # 真源：负值
    html = paper_render.performance_block(data)
    assert paper_render.ratio_pct(-_row(data)["max_drawdown"]) in html
    assert paper_render.ratio_pct(_row(data)["max_drawdown"]) not in html


# ---------- 组 5：CLI（ERROR_DIARY #55：绝不真起子进程） ----------

@pytest.fixture
def no_subprocess(monkeypatch):
    """任何子进程一开就报错 —— #55 的判据是「起子进程数 == 0」，不是「结果看起来对」。"""
    def _boom(*a, **k):
        raise AssertionError("ERROR_DIARY #55：这个用例不许起子进程")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    return _boom


@pytest.fixture
def fake_real_db(tmp_path, monkeypatch):
    """把 `paths.DB_PATH` 也指到 tmp —— 「参数看起来指到临时目标」不等于真的指到了。"""
    from stocklab.config import paths

    fake = tmp_path / "REAL-DB-MUST-NOT-BE-TOUCHED.db"
    fake.write_bytes(b"")
    monkeypatch.setattr(paths, "DB_PATH", fake)
    return fake


def test_cli_metrics_json_is_the_same_payload_as_the_page(tmp_path, capsys,
                                                         no_subprocess, fake_real_db):
    path, days = _tiny_db(tmp_path)
    code = main(["paper", "metrics", "--db", str(path), "--asof", days[-1],
                 "--json"])
    out, err = capsys.readouterr()
    assert code == 0, err
    payload = json.loads(out)
    assert payload["metric_keys"] == list(paper_data.METRIC_KEYS)
    assert payload["sample_gate"]["gate_status"] == "insufficient"
    assert _row(payload)["total_return"] == pytest.approx(1086.2775 / 1000.0 - 1.0)
    # 真库那份**一个字节都没变**（#55：判据落在具体资源上，不是 git status）
    assert fake_real_db.read_bytes() == b""


def test_cli_metrics_writes_the_report_and_exits_zero(tmp_path, capsys,
                                                     no_subprocess, fake_real_db):
    path, days = _tiny_db(tmp_path)
    out_path = tmp_path / "metrics.md"
    code = main(["paper", "metrics", "--db", str(path), "--asof", days[-1],
                 "--out", str(out_path)])
    out, err = capsys.readouterr()
    assert code == 0, err
    md = out_path.read_text(encoding="utf-8")
    assert "总收益" in md and "样本不足" in md
    assert "跑赢" not in md and "跑输" not in md
    assert fake_real_db.read_bytes() == b""


def test_cli_metrics_does_not_advance_the_paper_account(tmp_path, capsys,
                                                        no_subprocess, fake_real_db):
    """只读命令：跑完前后库的**行数逐表不变**（不是「看起来没变」）。"""
    path, days = _tiny_db(tmp_path)
    c = connect(path)
    try:
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("paper_accounts", "paper_nav_daily", "paper_trades")}
    finally:
        c.close()
    main(["paper", "metrics", "--db", str(path), "--asof", days[-1], "--json"])
    capsys.readouterr()
    c = connect(path)
    try:
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("paper_accounts", "paper_nav_daily", "paper_trades")}
    finally:
        c.close()
    assert before == after


def test_cli_metrics_on_a_missing_db_exits_two_with_a_reason(tmp_path, capsys,
                                                            no_subprocess):
    code = main(["paper", "metrics", "--db", str(tmp_path / "nope.db"),
                 "--asof", START, "--json"])
    _, err = capsys.readouterr()
    assert code == 2
    assert "db not found" in err
