"""P55：中证500（`sh000905`）接入第二基准。

口径：澄清录 §8.3 **D-43**（`sh000905` 只在「用既有采集路径落进 `bars_daily`、
同一 `adj_mode='none'` 口径」时才加）＋ §8.5 **D-47**（先只读探针证明拿得到；
接入时采集与配置翻转**成对做**；真库首次长窗采集由 nanobot 执行）。

全部离线：抓取被替换成假 `HttpClient`/夹具，`conftest` 的 autouse 守卫挡真实联网。
真源实测（一次性手工只读探针）记在任务书 §实施记录，**不是** pytest 用例。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json

import pytest

import stocklab.data.http as http_mod
from stocklab.cli.main import cmd_ingest_index, build_parser
from stocklab.config import paths
from stocklab.data.fetch import fetch_index_daily
from stocklab.data.sources import tencent
from stocklab.labweb import m2_data, m2_render, paper_data
from stocklab.m2 import config as m2_config
from stocklab.ops import chain, schedule
from stocklab.paper import store as paper_store
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

INDEX_500 = "sh000905"
NOW = "2026-09-23T16:00:00+08:00"

#: 三个交易日（其中两天故意跨月，让「日历前滚」有可观察的增量）
DATES_300 = ["2026-07-30", "2026-07-31", "2026-08-03"]
DATES_500 = ["2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]


def _kline_payload(code: str, dates: list[str], base: float) -> str:
    """腾讯 `fqkline` 响应：`data[code]["day"]` 的行是 [日期, 开, 收, 高, 低, 量]。"""
    rows = [[d, str(base + i), str(base + i + 1), str(base + i + 2),
             str(base + i - 1), "12345"] for i, d in enumerate(dates)]
    return json.dumps({"code": 0, "msg": "",
                       "data": {code: {"day": rows, "qt": {}}}})


class FakeClient:
    """假 `HttpClient`：按 code 返回固定响应，并记录每次请求的 URL。"""

    def __init__(self, pages: dict[str, str]):
        self.pages = dict(pages)
        self.urls: list[str] = []

    def get_text(self, url, **_kw):
        self.urls.append(url)
        for code, payload in self.pages.items():
            if f"param={code}," in url:
                return payload
        # 没有该 code 的页：返回空 day（模拟源站不给这个符号的行）
        return json.dumps({"code": 0, "msg": "", "data": {}})


# ══════════════════════════════════════════════════════════════════════
# T1 —— 只读探针：拿得到，且是同一口径（`adj_mode='none'`、源站符号）
# ══════════════════════════════════════════════════════════════════════


def test_t1_fetch_index_daily_returns_sh000905_with_none_adj_mode():
    """`fetch_index_daily("sh000905")` 走的是**既有**指数路径，不是新抓取器。

    真源实测（任务书 §实施记录 T1）用真 `HttpClient` 跑过一次；
    这里把它钉成离线判据：假源固定响应 ⇒ 行数、首末日、`adj_mode`、`Bar.code`。
    """
    client = FakeClient({INDEX_500: _kline_payload(INDEX_500, DATES_500, 7700.0)})
    bars = fetch_index_daily(client, symbol=INDEX_500,
                             start=DATES_500[0], end=DATES_500[-1])
    assert len(bars) == len(DATES_500)
    assert bars[0].date == DATES_500[0] and bars[-1].date == DATES_500[-1]
    assert {b.adj_mode for b in bars} == {"none"}
    # `code` 是**源站符号**：6 位数字会与基金键空间撞键（见 fetch.py 的 `_INDEX_SYMBOL`）
    assert {b.code for b in bars} == {INDEX_500}
    assert [b.close for b in bars] == [7701.0, 7702.0, 7703.0, 7704.0]


def test_t1_a_six_digit_symbol_is_still_rejected():
    """反向自检：探针**不是**靠放宽符号形状拿到数据的（`000905` 必须仍被拒）。"""
    client = FakeClient({INDEX_500: _kline_payload(INDEX_500, DATES_500, 7700.0)})
    with pytest.raises(ValueError):
        fetch_index_daily(client, symbol="000905", start="2026-08-04",
                          end="2026-08-07")


# ══════════════════════════════════════════════════════════════════════
# T2 —— 落库口径（fixture 库上跑真 CLI，不碰真库）
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db(tmp_db, tmp_path, monkeypatch):
    """把库与 raw_cache 都指到临时目录（绝不碰 `data/stocklab.db`）。"""
    init_db(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    monkeypatch.setattr(paths, "RAW_CACHE_DIR", tmp_path / "raw_cache")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(paths, "REPORT_DIR", tmp_path / "reports")
    conn = connect(tmp_db)
    yield conn
    conn.close()


def _run_ingest(monkeypatch, payloads: dict[str, str], *argv: str) -> dict:
    """跑 `ingest index <argv>`：**只**把 `HttpClient` 换成假源，其余全是真的。

    刻意**不**替换 `fetch_index_daily` / `fetch_daily_bars`（那会把判据变成
    「我的替身能不能用」）：这里是「用**既有采集路径**把 `sh000905` 落进
    `bars_daily`」这句话本身 —— 翻页、上限守卫、解析、`adj_mode` 全部走真实现。
    """
    monkeypatch.setattr(http_mod, "HttpClient",
                        lambda *a, **k: FakeClient(payloads))
    args = build_parser().parse_args(["ingest", "index", *argv])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cmd_ingest_index(args)
    assert code == 0, buf.getvalue()
    return json.loads(buf.getvalue())


def test_t2_ingest_index_lands_sh000905_as_the_source_symbol(db, monkeypatch):
    """采集后 `bars_daily` 出现 `sh000905`，`adj_mode='none'`，`code` 是源站符号。"""
    out = _run_ingest(monkeypatch, {INDEX_500: _kline_payload(
        INDEX_500, DATES_500, 7700.0)},
        "--symbol", INDEX_500, "--start", DATES_500[0], "--end", DATES_500[-1])
    assert out["symbol"] == INDEX_500 and out["bars"] == len(DATES_500)
    assert out["adj_mode"] == "none"

    rows = db.execute(
        "SELECT code, date, close, adj_mode FROM bars_daily WHERE code = ?"
        " ORDER BY date", (INDEX_500,)).fetchall()
    assert len(rows) == len(DATES_500)
    assert {r["adj_mode"] for r in rows} == {"none"}
    assert [r["date"] for r in rows] == DATES_500
    # 没有 6 位数字的 `000905` 混进来（那会与基金共用一个键空间）
    assert db.execute("SELECT COUNT(*) FROM bars_daily WHERE code = '000905'"
                      ).fetchone()[0] == 0


# ══════════════════════════════════════════════════════════════════════
# T3 —— 日历只增不减；`sh000300` 逐行不变
# ══════════════════════════════════════════════════════════════════════


def test_t3_the_calendar_only_grows_and_sh000300_is_untouched(db, monkeypatch):
    """第二个符号顺带前滚日历（`INSERT OR IGNORE`）⇒ 日期集合**不小于**之前。

    `ingest index` 是交易日历的**唯一**来源（ADR-001 B4）：加第二个符号时若把
    日历搞坏（删行/改行），回测与 `verify pending` 会一起被带偏 —— 所以要断言
    「只增不减」，而不只是「跑完了没报错」。
    """
    payloads = {INDEX_300_SYMBOL: _kline_payload(INDEX_300_SYMBOL, DATES_300, 4000.0),
                INDEX_500: _kline_payload(INDEX_500, DATES_500, 7700.0)}
    _run_ingest(monkeypatch, payloads, "--symbol", INDEX_300_SYMBOL,
                "--start", DATES_300[0], "--end", DATES_300[-1])
    before_cal = {r["date"] for r in db.execute(
        "SELECT date FROM trading_calendar")}
    before_300 = [tuple(r) for r in db.execute(
        "SELECT code, date, open, close, adj_mode FROM bars_daily"
        " WHERE code = ? ORDER BY date", (INDEX_300_SYMBOL,))]
    assert before_cal == set(DATES_300) and len(before_300) == len(DATES_300)

    _run_ingest(monkeypatch, payloads, "--symbol", INDEX_500,
                "--start", DATES_500[0], "--end", DATES_500[-1])
    after_cal = {r["date"] for r in db.execute(
        "SELECT date FROM trading_calendar")}
    after_300 = [tuple(r) for r in db.execute(
        "SELECT code, date, open, close, adj_mode FROM bars_daily"
        " WHERE code = ? ORDER BY date", (INDEX_300_SYMBOL,))]

    assert before_cal <= after_cal, "日历被第二个符号**删行**了"
    assert after_cal == before_cal | set(DATES_500)
    assert after_300 == before_300, "`sh000300` 的行情被第二个符号改动了"


# ══════════════════════════════════════════════════════════════════════
# T4 —— 配置翻转（`BENCHMARKS` / `DEFERRED_BENCHMARKS`）+ 第二个基准行
# ══════════════════════════════════════════════════════════════════════


def test_t4_the_config_flips_to_two_benchmarks_and_no_deferred():
    """接入 = 改配置两处：`sh000905` 进 `BENCHMARKS`、`DEFERRED_BENCHMARKS` 清空。

    `DEFERRED_BENCHMARKS` 清空**不是**「悄悄少一个基准」：它清空的前提正是
    那个基准已经真的接上了 —— 判据在下面那条（表里出现两行基准）。
    """
    assert m2_config.BENCHMARKS == (INDEX_300_SYMBOL, INDEX_500)
    assert m2_config.DEFERRED_BENCHMARKS == ()
    assert m2_config.INDEX_500_SYMBOL == INDEX_500


def test_t4_the_panel_shows_one_row_per_benchmark(tmp_path):
    """两个基准**各一行**，且都带「不可直接交易/无成本 ⇒ 偏乐观」的标注。

    反目标：不许把两个基准相减/取平均/合成一个「综合基准」—— 所以断言的是
    **两行**（各自一行），不是一个合起来的数。
    """
    payload = panel_payload(tmp_path)
    sides = payload["three_way"]["sides"]
    bench = [s for s in sides if s["role"] == m2_data.SIDE_BENCHMARK]
    assert [s["account_id"] for s in bench] == [INDEX_300_SYMBOL, INDEX_500]
    assert all(s["available"] for s in bench)
    assert len({s["label"] for s in bench}) == 2, "两行基准必须有各自的标签"
    caveat = payload["three_way"]["benchmark_caveat"]
    assert "不可直接交易" in caveat and "无成本" in caveat
    assert INDEX_300_SYMBOL in caveat and INDEX_500 in caveat
    # 两个基准行都**不**参与「相对基准」那一列（它是 P41 的口径，不是新加的比较）
    assert all(s["excess_vs_index_300"] is None for s in bench)


def test_t4_the_second_benchmark_row_is_field_identical_to_the_p41_payload(tmp_path):
    """同源判据：第二个基准行的五个值 == P41 同行的五个值（逐字段，不重算）。"""
    from stocklab.labweb import paper_data

    conn, asof = p55_db(tmp_path)
    try:
        p41 = paper_data.performance(conn, asof,
                                     benchmarks=m2_config.BENCHMARKS)
        payload = m2_data.panel(conn, asof)
    finally:
        conn.close()
    base = {r["account_id"]: r for r in p41["rows"]}[INDEX_500]
    side = next(s for s in payload["three_way"]["sides"]
                if s["account_id"] == INDEX_500)
    for key in m2_data.METRIC_KEYS:
        assert side[key] == base[key], f"{key} 与 P41 不同源"
    assert side["n_sessions"] == base["n_sessions"]
    assert payload["three_way"]["sample_gate"] == p41["sample_gate"]


def test_t4_the_scope_names_both_benchmarks(tmp_path):
    """`scope` 那行不再写「本轮只对沪深300」——它现在对两个都成立。"""
    payload = panel_payload(tmp_path)
    scope = payload["three_way"]["scope"]
    assert INDEX_300_SYMBOL in scope and INDEX_500 in scope
    assert "本轮基准只对" not in scope
    assert payload["three_way"]["deferred"] == []
    assert payload["three_way"]["deferred_blocked"] == []


def test_t4_the_default_call_is_still_single_benchmark_for_other_pages(tmp_path):
    """反向自检：**不传** `benchmarks` 的调用方（`/lab/paper`）载荷逐位不变。

    第二个基准是模块2 的口径（`m2_config.BENCHMARKS`），不是 P41 的默认 ——
    否则 `/lab/paper` 会跟着多出一行，而那一页根本没人要求看中证500。
    """
    from stocklab.labweb import paper_data

    conn, asof = p55_db(tmp_path)
    try:
        perf = paper_data.performance(conn, asof)          # 不传 benchmarks
    finally:
        conn.close()
    bench = [r for r in perf["rows"] if r["kind"] == "benchmark"]
    assert [r["account_id"] for r in bench] == [INDEX_300_SYMBOL]
    assert INDEX_500 not in {r["account_id"] for r in perf["rows"]}


# ══════════════════════════════════════════════════════════════════════
# T6 —— 链上多一步（顺序 + argv + plist 逐字节不变）
# ══════════════════════════════════════════════════════════════════════


def test_t6_close_runs_ingest_index_500_right_after_ingest_index():
    """日链：`ingest index --symbol sh000905` **紧跟** `ingest index`。

    位置不是排版偏好：日历由 `ingest index` 前滚，第二个符号必须在同一轮里
    紧跟其后 —— 否则「当天的交易日历」与「两个指数的点位」会来自不同轮次。
    """
    names = list(chain.CLOSE_STEP_ORDER)
    i = names.index("ingest_index")
    assert names[i + 1] == "ingest_index_500"
    assert names.count("ingest_index_500") == 1
    step = chain.CLOSE_STEP_BY_NAME["ingest_index_500"]
    assert step.args == ("ingest", "index", "--symbol", INDEX_500)
    # 跨库守卫沿用：采集不接受 `--db`（与 `ingest index` 同族）
    assert step.supports_db is False


def test_t6_monthly_backfills_the_same_symbol_long_window():
    """月度链：同一步进长窗回补批（与 `ingest bars --days 12000` 相邻）。

    日链走 `ingest index` 的默认窗口（4000 天）；长窗回补是月度的事 ——
    与 `ingest_bars_long` 同一条理由（日链不做长窗）。
    """
    names = list(chain.MONTHLY_STEP_ORDER)
    i = names.index("ingest_bars_long")
    assert names[i + 1] == "ingest_index_500"
    step = chain.MONTHLY_STEP_BY_NAME["ingest_index_500"]
    assert step.args == ("ingest", "index", "--symbol", INDEX_500,
                        "--days", "12000")
    assert step.supports_db is False


def test_t6_the_plists_are_still_byte_identical():
    """`ops schedule` 的产物**逐字节不变**（反目标：不改时点与槽数）。

    三份 plist 都**不含步骤表** —— 链上多一步不该动它一个字节。基准 sha 与
    P54 的那一份相同（`test_m2_daily.py::test_t1_the_plists_are_byte_identical_
    to_the_pre_wiring_ones` 里的 golden 值）。
    """
    golden = {
        "close": "8d33c0257bb7eebae320d3546546fc057f6000e70d5580dcf33115e54de8490c",
        "patrol": "4fd47ca89100ddf20f2e9f1c27d736aa02dc8b54fa0b5fdba9951105412a7c3c",
        "monthly": "e97ba0143c9d7bf2afda2e126d1205b33ce5214504f1548e2e5a5c5b2e7bdcbe",
    }
    for job in schedule.JOBS:
        blob = schedule.render_plist(job, project_root="/proj", python="/py",
                                     logs="/logs")
        assert hashlib.sha256(blob).hexdigest() == golden[job.name], job.name
        assert b"steps" not in blob and b"CLOSE_STEPS" not in blob


# ══════════════════════════════════════════════════════════════════════
# 禁词纪律照旧（P48 §1 硬判据 2）—— 新增一行不许把措辞带坏
# ══════════════════════════════════════════════════════════════════════

FORBIDDEN = ("跑赢", "跑输", "优于", "劣于", "领先", "胜过")


def test_the_forbidden_words_are_still_absent_with_two_benchmarks(tmp_path):
    payload = panel_payload(tmp_path)
    html = m2_render.three_way_block(payload["three_way"])
    text = m2_render.three_way_text(payload["three_way"])
    for word in FORBIDDEN:
        assert word not in html, f"页面出现禁词 {word}"
        assert word not in text, f"报告出现禁词 {word}"


# ══════════════════════════════════════════════════════════════════════
# 夹具：一份「两个基准都在库里」的最小载荷
# ══════════════════════════════════════════════════════════════════════


def p55_db(tmp_path) -> tuple[object, str]:
    """(conn, asof)：`sh000300` 与 `sh000905` **都**有完整窗口内的收盘价。"""
    path = tmp_path / "p55.db"
    init_db(path)
    conn = connect(path)
    dates = DATES_300 + DATES_500
    conn.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sh','main',?,?)",
        [(INDEX_300_SYMBOL, "沪深300", "index", NOW),
         (INDEX_500, "中证500", "index", NOW)])
    conn.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in dates])
    conn.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(INDEX_300_SYMBOL, d, 4000.0 + 8 * i, 4000.0 + 8 * i, 4000.0 + 8 * i,
          4000.0 + 8 * i, NOW) for i, d in enumerate(dates)]
        + [(INDEX_500, d, 7700.0 + 5 * i, 7700.0 + 5 * i, 7700.0 + 5 * i,
            7700.0 + 5 * i, NOW) for i, d in enumerate(dates)])
    paper_store.insert_account(
        conn, account_id="arm-now", arm="now", etf_target_pct=None,
        start_date=dates[0], initial_cash=20000.0, initial_positions=[],
        initial_nav=20000.0, params={"initial_capital": 20000.0}, now=NOW)
    for i, d in enumerate(dates):
        nav = 20000.0 + 20.0 * i
        paper_store.insert_nav(
            conn, account_id="arm-now", date=d, cash=nav, positions=[],
            market_value=0.0, nav=nav, drawdown=0.0, cum_cost=0.0,
            cum_return=round(nav / 20000.0 - 1.0, 6), net_deposits=20000.0,
            index_300_level=None, index_300_asof=None, now=NOW, commit=False)
    conn.commit()
    return conn, dates[-1]


def panel_payload(tmp_path) -> dict:
    conn, asof = p55_db(tmp_path)
    try:
        return m2_data.panel(conn, asof)
    finally:
        conn.close()
