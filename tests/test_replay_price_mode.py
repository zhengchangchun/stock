"""P82：`period_returns` 的价格口径开关（`raw` / `adj`）。

覆盖任务书 T3 的五个用例：

① 默认等价 —— 不传 `price_mode` 与传 `"raw"` **逐位相同**；
② 含除权的夹具上 `adj` 与 `raw` 分叉且**方向正确**（除权日 adj 收益 > raw 收益）；
③ 复权不可用 ⇒ 回退未复权 ＋ `n_adj_fallback` 按 `(code, 周期)` 计数；
④ `plugin/sandbox.py` 路径零改动（注入的 `benchmark_excess` 默认口径不变）；
⑤ AS-OF —— 未来的 `cqr` 不污染过去，且取价确实用 `as_of = d1`。

另加一条**等价性钉子**：`_AdjCloses` 的读数必须与
`adjust.load_bars_adjusted(..., as_of=d1, start=usable_from)` 的返回值**逐位相同**
（`replay.py` 把该函数的三步拆开做缓存，这条测试是那次拆分的证据）。
"""

from pathlib import Path

import pytest

from stocklab.candidate import replay
from stocklab.config.costs import CostModel
from stocklab.data import adjust
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-20T16:00:00+08:00"


def _dates(n, start="2026-01-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _flat() -> CostModel:
    """零成本模型：隔离价格口径（收益 = 毛收益，成本项恒 0）。"""
    return CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)


def _seed(tmp_db, *, prices: dict[str, list[float]], dates: list[str],
          kinds: dict[str, str] | None = None,
          actions: list[tuple[str, str, str]] = ()):
    """建库：标的（含口径）+ 日历 + 逐日收盘价 + `corp_actions`。

    `kinds`：`code → instruments.type`（缺省 `stock`）。
    `actions`：`(code, cqr, content)`；`content` 决定该事件可否定价
    （`"10配3股"` 之类不可定价 ⇒ 进 `chain.unusable`）。
    """
    init_db(tmp_db)
    c = connect(tmp_db)
    kinds = kinds or {}
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main',?,?)",
        [(code, code, kinds.get(code, "stock"), NOW) for code in prices])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, p, p, p, p, NOW)
         for code, series in prices.items()
         for d, p in zip(dates, series)])
    if actions:
        c.executemany(
            "INSERT INTO corp_actions (code, cqr, djr, fh_sh, content, source,"
            " first_seen, last_seen) VALUES (?,?,?,?,?,'t',?,?)",
            [(code, cqr, cqr, None, content, NOW, NOW)
             for code, cqr, content in actions])
    c.commit()
    return c


# ---------------------------------------------------------------------------
# ① 默认等价：不传 price_mode == 传 "raw"，逐位相同
# ---------------------------------------------------------------------------

def test_default_mode_is_raw_and_bitwise_identical(tmp_db):
    """不传参 == 传 `"raw"`：**逐位相同**（T3 ①，D1 的落点）。

    同一夹具下两次调用，元素级 `==`（float 逐位）。`price_stats` 不传时
    什么都不写 —— 默认路径连一个新的副作用都没有。
    """
    dates = _dates(11)
    c = _seed(tmp_db, prices={"000001": [10.0 + i * 0.1 for i in range(11)],
                              "000002": [20.0 - i * 0.2 for i in range(11)]},
               dates=dates)
    marks = [dates[0], dates[5], dates[10]]
    pools = {dates[0]: ["000001"], dates[5]: ["000002"],
             dates[10]: ["000001", "000002"]}

    default = replay.period_returns(c, asof_dates=marks, pool="short",
                                    costs=_flat(), _pools_for_test=pools)
    explicit = replay.period_returns(c, asof_dates=marks, pool="short",
                                     costs=_flat(), _pools_for_test=pools,
                                     price_mode="raw")
    assert default == explicit
    assert all(a == b for a, b in zip(default, explicit)), "默认路径必须逐位不变"
    assert len(default) == 2

    # `price_stats` 是**可选的旁路出口**：不传就不产生任何写入。
    stats: dict = {}
    replay.period_returns(c, asof_dates=marks, pool="short", costs=_flat(),
                          _pools_for_test=pools, price_stats=stats)
    assert stats == {"price_mode": "raw", "n_adj_fallback": 0}


def test_unknown_price_mode_raises(tmp_db):
    """取值只许 `raw | adj` —— `both` 是 CLI 层的事，不是本函数的取值。"""
    dates = _dates(3)
    c = _seed(tmp_db, prices={"000001": [10.0] * 3}, dates=dates)
    with pytest.raises(ValueError, match="price_mode"):
        replay.period_returns(c, asof_dates=[dates[0], dates[2]], pool="short",
                              costs=_flat(), _pools_for_test={},
                              price_mode="both")


# ---------------------------------------------------------------------------
# ② 含除权：adj 与 raw 分叉且方向正确
# ---------------------------------------------------------------------------

def test_adj_beats_raw_across_cash_dividend(tmp_db):
    """除权日 `adj` 收益 > `raw` 收益（T3 ②，D2 的方向）。

    夹具：`000001` 在 `d1` 除权，「10派2元」⇒ 每股 0.2 元现金。
    真实价格序列是**除权后的**：10.0 → 9.8（跳空 −2%），`cqr = d1`。

    - `raw`：`9.8/10 - 1 = -2.0%`（除权跳空被算成亏损 —— 这就是本站要量的口径污染）
    - `adj`：`F(d0)=1`、`F(d1)=(10-0.2)/10=0.98` ⇒ 复权价 `9.8 → 9.8` ⇒ `0.0%`

    零成本模型隔离价格项；两期持有同一只 ⇒ 无调仓账单。
    """
    dates = _dates(6)
    c = _seed(tmp_db, prices={"000001": [10.0] * 5 + [9.8]}, dates=dates,
              actions=[("000001", dates[5], "10派2元")])
    marks = [dates[0], dates[5]]
    pools = {dates[0]: ["000001"], dates[5]: ["000001"]}

    raw = replay.period_returns(c, asof_dates=marks, pool="short",
                               costs=_flat(), _pools_for_test=pools,
                               price_mode="raw")[0]
    stats: dict = {}
    adj = replay.period_returns(c, asof_dates=marks, pool="short",
                                costs=_flat(), _pools_for_test=pools,
                                price_mode="adj", price_stats=stats)[0]

    assert raw == pytest.approx(-0.02), "raw 侧应把除权跳空算成 -2%"
    assert adj == pytest.approx(0.0), "adj 侧应把这次除权还原掉"
    assert adj > raw, "除权日 adj 收益必须**高于** raw（假跌幅被还原）"
    assert stats == {"price_mode": "adj", "n_adj_fallback": 0}

    # 成本侧**没有**跟着变：零成本模型下两期持有相同 ⇒ 两侧都是「纯价格项」。
    # 若成本侧误用了复权价，`_qty_for` 的分母会变、断言会漂（此处近乎不可能，
    # 但口径哨兵要留一条 —— 见 test_cost_side_stays_raw）。
    assert stats["n_adj_fallback"] == 0


def test_cost_side_stays_raw_under_adj_mode(tmp_db):
    """D2 的钉子：`adj` 档下**成本侧仍按未复权价**计（成交价 9.8、整手 qty）。

    夹具：`d0 → d1` 价格 10.0 → 9.8（除权），且 `d1` 清仓（`nxt` 为空）
    ⇒ 本期末尾有一笔卖出账单。

    期望返回值 = `adj_gross(0.0) − fee_ratio(px=9.8, qty=int(N/9.8)) − 滑点`。

    ⚠️ **这条钉的是复合读数，不是「成本侧用了哪一档价」**：`as_of = d1` 时复权价
    在 `d1` 恰等于未复权价（multiplier=1），所以「成本侧误用复权价」在数值上
    **不可观测**（变异实验确认过：把卖出腿的 `p1` 换成 adj 价，本用例仍绿）。
    D2 的落法是**代码结构** —— `adj` 只出现在 `gains` 循环里，成本循环读的是
    `p1`（未复权）。这条测试守住的是「两者加起来的结果没被改坏」。
    """
    from stocklab.config.replay import POSITION_NOTIONAL

    dates = _dates(6)
    c = _seed(tmp_db, prices={"000001": [10.0] * 5 + [9.8]}, dates=dates,
              actions=[("000001", dates[5], "10派2元")])
    costs = CostModel(commission_rate=0.00025, min_commission=5.0,
                      transfer_fee_rate=0.00001, stamp_tax_rate=0.0005,
                      slippage_bps=5.0)
    px1 = 9.8
    qty = int(POSITION_NOTIONAL / px1)      # 未复权价 → 整手分母
    expected_fee = costs.fees("sell", px1, qty) / (px1 * qty)
    expected = 0.0 - expected_fee - costs.slippage_bps / 10_000.0

    got = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=costs,
        _pools_for_test={dates[0]: ["000001"], dates[5]: []},
        price_mode="adj")[0]
    assert got == pytest.approx(expected), (
        f"adj 档的收益侧应为 0.0、成本侧按**未复权** 9.8 计；实际 {got}")


# ---------------------------------------------------------------------------
# ③ 复权不可用 ⇒ 回退 ＋ (code, 周期) 计数
# ---------------------------------------------------------------------------

def test_etf_falls_back_to_raw_and_is_counted_per_code_period(tmp_db):
    """ETF（`EtfChainUnsupported`）⇒ 回退未复权、不抛异常，按 `(code, 周期)` 计数。

    夹具：`510300`（`type='etf'`）＋ `000001`（正常股票、无事件）。
    三个调仓边界 ⇒ 两个周期，两个周期里两只都在池 ⇒
    `n_adj_fallback == 2`（ETF 每期一次），股票一次都不计
    （无 `corp_actions` ⇒ 因子恒 1 ⇒ 复权价逐位等于未复权价，不是回退）。

    同时钉住：回退后**读数与 raw 侧逐位相同**（回退 = 用未复权价，不是丢该只）。
    """
    dates = _dates(16)
    c = _seed(tmp_db,
              prices={"510300": [10.0] * 16, "000001": [20.0] * 16},
              dates=dates,
              kinds={"510300": "etf"})
    marks = [dates[0], dates[5], dates[10], dates[15]]
    pools = {d: ["510300", "000001"] for d in marks}

    raw = replay.period_returns(c, asof_dates=marks, pool="short",
                                costs=_flat(), _pools_for_test=pools,
                                price_mode="raw")
    stats: dict = {}
    adj = replay.period_returns(c, asof_dates=marks, pool="short",
                                costs=_flat(), _pools_for_test=pools,
                                price_mode="adj", price_stats=stats)

    assert len(raw) == 3
    assert stats["n_adj_fallback"] == 3, (
        f"ETF 出现在 3 个周期里 ⇒ 应计 3 次，实际 {stats['n_adj_fallback']}")
    assert adj == raw, "回退 = 用未复权价 ⇒ 读数必须与 raw 逐位相同"


def test_d0_before_usable_from_falls_back(tmp_db):
    """D4 的缩窗口：`d0` 落在 `chain.usable_from` 之前 ⇒ 该期回退。

    夹具：`000001` 有一条**不可定价**的事件（`"10配3股"` ⇒ `UnpriceableTerms`）
    落在 `dates[2]` ⇒ `chain.usable_from = dates[2]`。

    四个调仓边界 ⇒ 三个周期：
    - `[dates[0], dates[1]]` 与 `[dates[1], dates[2]]`：`d0 < usable_from` ⇒ 回退（计 2 次）
    - `[dates[2], dates[3]]`：`d0 == usable_from` ⇒ 正常走复权
      （该事件不进链 ⇒ 因子恒 1 ⇒ 数值与 raw 相同，但**不是回退**）

    这条同时是「不可定价事件不被静默当成 k=1」的守卫：它把窗口**挪后**，
    而不是把假跌幅留在序列里。
    """
    dates = _dates(6)
    c = _seed(tmp_db, prices={"000001": [10.0] * 6}, dates=dates,
              actions=[("000001", dates[2], "10配3股")])
    marks = [dates[0], dates[1], dates[2], dates[3]]
    pools = {d: ["000001"] for d in marks}

    _bars, chain = adjust.load_chain(c, "000001")
    assert chain.usable_from == dates[2], "夹具前提：不可定价事件把下界推到 dates[2]"

    stats: dict = {}
    got = replay.period_returns(c, asof_dates=marks, pool="short",
                                costs=_flat(), _pools_for_test=pools,
                                price_mode="adj", price_stats=stats)
    assert len(got) == 3
    assert stats["n_adj_fallback"] == 2, (
        f"前两期的 d0 早于 usable_from，第三期不早于；实际 "
        f"{stats['n_adj_fallback']}")
    assert got == [0.0, 0.0, 0.0]        # 价格全平 ⇒ 回退与否都不影响数值


# ---------------------------------------------------------------------------
# ⑤ AS-OF：未来的 cqr 不污染过去
# ---------------------------------------------------------------------------

def test_future_event_does_not_affect_past_and_asof_is_d1(tmp_db):
    """AS-OF 反证（T3 ⑤，D4）：未来事件不进本期的因子，且取价确实用 `as_of=d1`。

    夹具：`000001` 在 `dates[5]` 除权（该日在 `d1 = dates[2]` **之后**）。
    价格序列因此是「除权前 10.0 ... 除权后 9.8」。

    三条断言：
      1. 本期 `adj` 读数 == 同夹具**去掉该事件**后的 `adj` 读数（未来事件不回流）；
      2. `_AdjCloses` 的价对 == `load_bars_adjusted(as_of=d1, start=usable_from)`
         的同日取值 ⇒ 读数确实是 **d1 口径**的那一组（不是「取窗口末尾」的）；
      3. 该价对 **≠** `load_bars_adjusted(as_of=dates[5])` 的取值 ⇒ 断言 2 不空。

    注：`_AdjCloses` 传给 `adjust_bars` 的 `bars` 只含该周期的两根，`base_date`
    因此恒为 `d1` ⇒ 把 `as_of` 参数改大**不会**改变读数（比例里那个因子会约掉）。
    所以断言 2/3 钉的是「价对 = d1 口径」，而不是「代码里那个实参恰好写着 d1」；
    真正会漂的是「改用全量 bars + 别的 as_of 直接调 `load_bars_adjusted`」那种写法。
    """
    dates = _dates(6)
    actions = [("000001", dates[5], "10派2元")]
    prices = {"000001": [10.0, 10.0, 10.0, 10.0, 10.0, 9.8]}
    c = _seed(tmp_db, prices=prices, dates=dates, actions=actions)
    c_plain = _seed(Path(str(tmp_db) + ".plain"), prices=prices, dates=dates)
    marks = [dates[0], dates[2]]
    pools = {dates[0]: ["000001"], dates[2]: ["000001"]}

    with_event = replay.period_returns(c, asof_dates=marks, pool="short",
                                       costs=_flat(), _pools_for_test=pools,
                                       price_mode="adj")
    without = replay.period_returns(c_plain, asof_dates=marks, pool="short",
                                    costs=_flat(), _pools_for_test=pools,
                                    price_mode="adj")
    assert with_event == without, (
        "未来的 cqr 事件影响了过去的读数 —— as-of 语义被打穿")

    # 断言 2/3：直接对照 `load_bars_adjusted` 的取值。
    adj = replay._AdjCloses(c)
    pair = adj.pair("000001", dates[0], dates[2])
    at_d1 = {b.date: b.close
             for b in adjust.load_bars_adjusted(c, "000001", dates[2],
                                                start=None)}
    at_later = {b.date: b.close
                for b in adjust.load_bars_adjusted(c, "000001", dates[5],
                                                   start=None)}
    assert pair == (at_d1[dates[0]], at_d1[dates[2]])
    assert pair != (at_later[dates[0]], at_later[dates[2]]), (
        "取价没有按 as_of=d1 走（未来事件已经进了价格）—— 本条不该相同")


def test_adj_pair_matches_load_bars_adjusted(tmp_db):
    """**等价性钉子**（`replay.py` 对 `load_bars_adjusted` 的拆分是等价的）。

    `_AdjCloses` 为了缓存把 `load_bars_adjusted` 的三步拆开、并把第三步的
    `bars` 收窄成该周期的两根。这条测试直接拿**原函数的返回值**对照，
    覆盖三种形态：① 无事件（因子恒 1）② 有可定价事件 ③ 事件在窗口外。
    """
    dates = _dates(6)
    c = _seed(tmp_db,
              prices={"000001": [10.0] * 5 + [9.8],
                      "000002": [7.0] * 6,
                      "000003": [10.0, 10.0, 9.0, 9.0, 9.0, 9.0]},
              dates=dates,
              actions=[("000001", dates[5], "10派2元"),
                       ("000003", dates[2], "10送3股")])
    adj = replay._AdjCloses(c)
    for code in ("000001", "000002", "000003"):
        uf = adj._chain[code].usable_from if code in adj._chain else None
        for d0, d1 in ((dates[0], dates[2]), (dates[2], dates[4]),
                       (dates[4], dates[5])):
            pair = adj.pair(code, d0, d1)
            want = {b.date: b.close
                    for b in adjust.load_bars_adjusted(c, code, d1, start=uf)}
            assert pair == (want[d0], want[d1]), (
                f"{code} [{d0},{d1}]：缓存拆分后的读数与 load_bars_adjusted 不一致")


# ---------------------------------------------------------------------------
# ④ plugin/sandbox.py 路径零改动
# ---------------------------------------------------------------------------

def test_sandbox_injected_path_is_untouched(tmp_db):
    """T3 ④：`plugin/sandbox.py` 注入的两个函数**默认口径不变**。

    沙盒只经注入拿 `benchmark_excess` / `rebalance_marks`（它不 import
    `candidate/`），而 `benchmark_excess` 内部的 `period_returns` **不传**
    `price_mode` ⇒ 自动走 raw。两件事都钉住：
      ① 源码里不出现 `price_mode`（调用点零改动是**结构**，不是纪律）；
      ② `benchmark_excess` 不传参 == 传 `"raw"`。
    """
    import stocklab.plugin.sandbox as sandbox_mod

    src = Path(sandbox_mod.__file__).read_text(encoding="utf-8")
    assert "price_mode" not in src, (
        "plugin/sandbox.py 出现了 price_mode —— D1 要求该调用点一个字都不改")

    dates = _dates(6)
    c = _seed(tmp_db, prices={"000001": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]},
              dates=dates)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('sh000300','沪深300','sh','main','index',?)",
              (NOW,))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES ('sh000300',?,?,?,?,?,1000,"
        "'none','x',?)",
        [(d, 100.0, 100.0, 100.0, 100.0, NOW) for d in dates])
    c.commit()
    marks = [dates[0], dates[5]]
    pools = {dates[0]: ["000001"], dates[5]: ["000001"]}

    a = replay.benchmark_excess(c, asof_dates=marks, pool="short",
                                hold_override=pools)
    b = replay.benchmark_excess(c, asof_dates=marks, pool="short",
                                hold_override=pools)
    assert a == b
    assert a == pytest.approx(0.5 - 0.0), "池 +50%、基准持平 ⇒ 超额 +50%"


def test_no_price_mode_kwarg_removed_from_existing_signature():
    """`price_mode` / `price_stats` 都是**带默认值的新增关键字**（D1/D5）。

    既有调用方（`replay_period_deltas`、`benchmark_excess`、`sandbox` 的注入
    路径、全部老用例）一个都不用改 —— 这条把「新增而不是替换」写成断言。
    """
    import inspect

    sig = inspect.signature(replay.period_returns)
    for name, default in (("price_mode", replay.DEFAULT_PRICE_MODE),
                          ("price_stats", None)):
        assert name in sig.parameters, f"{name} 不是关键字参数"
        p = sig.parameters[name]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is default
    assert replay.DEFAULT_PRICE_MODE == "raw"
    assert replay.PRICE_MODES == ("raw", "adj")


# ---------------------------------------------------------------------------
# xsec 层：三档读数、产物名、不覆盖既有 raw 产物（T2 / D6）
# ---------------------------------------------------------------------------

def _xsec_seed(tmp_db, tmp_path):
    """建一次夹具（可跑通 `score_pipeline` 的最小库 ＋ 同形预注册），返回预注册路径。

    夹具直接复用 `tests/test_research_xsec.py` 的，**不另造一份** —— 否则
    「raw 档逐字段不变」这条就变成在另一套夹具上自证。
    """
    from tests.test_research_xsec import _seed_pipeline_db, _write_prereg

    tmp_path.mkdir(parents=True, exist_ok=True)
    _seed_pipeline_db(tmp_db)
    return _write_prereg(tmp_path / "prereg.md")


def _xsec_run(tmp_db, prereg, out, *extra):
    from tests.test_research_xsec import _run_cli
    return _run_cli(tmp_db, out, prereg, *extra)


def test_xsec_raw_mode_matches_default_and_only_adds_keys(tmp_db, tmp_path):
    """T2/D5：`--price-mode raw` 与**不传**时逐字段一致（只多三个新键）。

    新键在**两档都出现**（`price_mode` 要能自证这次跑的是哪个口径）；
    `arms_adj` / `delta_adj` 只在 adj/both 档出现 —— raw 档不许有。
    """
    import json

    prereg = _xsec_seed(tmp_db, tmp_path)
    rc_a = _xsec_run(tmp_db, prereg, tmp_path / "a")
    rc_b = _xsec_run(tmp_db, prereg, tmp_path / "b", "--price-mode", "raw")
    assert (rc_a, rc_b) == (0, 0)

    ja = json.loads((tmp_path / "a" / "2016-06-30-xsec-topn-seed21.json")
                    .read_text())
    jb = json.loads((tmp_path / "b" / "2016-06-30-xsec-topn-seed21.json")
                    .read_text())
    assert {"price_mode", "n_adj_fallback", "price_mode_note"} <= set(jb)
    # 新键只在 adj/both 档出现 —— raw 档连 `arms_adj` 都不该有。
    assert "arms_adj" not in jb and "delta_adj" not in jb
    assert jb["price_mode"] == "raw" and jb["n_adj_fallback"] == 0
    # 其余字段**逐字段相同**（含两臂序列与 Δ）—— `elapsed_*` 除外。
    # 这是「不传 == 传 raw == 现状」的机器判据（与 HEAD 产物的全窗比对是 §6 的事）。
    volatile = {"elapsed_s", "scan_s", "replay_s"}
    assert {k: v for k, v in ja.items() if k not in volatile} == \
           {k: v for k, v in jb.items() if k not in volatile}


def test_xsec_both_mode_emits_two_readings_without_touching_raw_product(
        tmp_db, tmp_path):
    """T4/D6：`both` 一次扫描出两套读数，且 **不覆盖** 既有 raw 产物。"""
    import json

    prereg = _xsec_seed(tmp_db, tmp_path)
    out = tmp_path / "out"
    assert _xsec_run(tmp_db, prereg, out) == 0
    raw_json = out / "2016-06-30-xsec-topn-seed21.json"
    before = raw_json.read_text(encoding="utf-8")

    # 第二次跑 `both`，同一个 out 目录。
    assert _xsec_run(tmp_db, prereg, out, "--price-mode", "both") == 0

    assert raw_json.read_text(encoding="utf-8") == before, \
        "both 档盖掉了既有的 raw 产物（D6 明确禁止）"
    adj_json = out / "2016-06-30-xsec-topn-seed21-adj.json"
    assert adj_json.is_file(), "both 档的产物名必须带 -adj 后缀"

    r_raw = json.loads(before)
    r = json.loads(adj_json.read_text(encoding="utf-8"))
    assert r["price_mode"] == "both"
    assert set(r["arms_adj"]) == {"topn", "all"}
    assert r["delta_adj"] is not None
    # `arms` 保持 raw（`both` 是对照档，锚点不能动）。
    assert r["arms"] == r_raw["arms"], "both 档的 arms 必须仍是 raw 读数"
    assert r["delta"]["mean_validate"] == r_raw["delta"]["mean_validate"]


def test_xsec_adj_mode_replaces_arms_and_has_no_arms_adj(tmp_db, tmp_path):
    """`adj` 档：`arms` / `delta` 就是复权读数；不另出 `arms_adj`。"""
    import json

    prereg = _xsec_seed(tmp_db, tmp_path)
    out = tmp_path / "out"
    assert _xsec_run(tmp_db, prereg, out, "--price-mode", "adj") == 0
    r = json.loads(
        (out / "2016-06-30-xsec-topn-seed21-adj.json").read_text())
    assert r["price_mode"] == "adj"
    assert "arms_adj" not in r and "delta_adj" not in r
    assert set(r["arms"]) == {"topn", "all"}
    assert all("n_adj_fallback" in a for a in r["arms"].values())
    assert r["n_adj_fallback"] == sum(
        a["n_adj_fallback"] for a in r["arms"].values())


def test_xsec_both_single_arm_keeps_adj_readings(tmp_db, tmp_path):
    """单臂 + `both`：**adj 读数必须在**（「Δ 为 None」≠「读数不存在」）。

    这是一个真实存在过的坑：若用 `delta_adj is not None` 作为「要不要落
    `arms_adj`」的条件，单臂跑（Δ 恒为 None）会把整份 adj 读数丢掉。
    """
    import json

    prereg = _xsec_seed(tmp_db, tmp_path)
    out = tmp_path / "out"
    rc = _xsec_run(tmp_db, prereg, out, "--arm", "topn", "--price-mode", "both")
    assert rc == 0
    r = json.loads(
        (out / "2016-06-30-xsec-topn-seed21-adj.json").read_text())
    assert set(r["arms"]) == {"topn"} and set(r["arms_adj"]) == {"topn"}
    assert r["delta"] is None and r["delta_adj"] is None
    assert r["arms_adj"]["topn"]["period_returns"]
    md = (out / "2016-06-30-xsec-topn-seed21-adj.md").read_text()
    assert "价格口径对照" in md and "没有 Δ" in md


def test_xsec_unknown_price_mode_is_rejected(tmp_path):
    """`--price-mode` 的取值由 argparse 的 `choices` 兜住（返回码 2）。

    `main()` 把 argparse 的 `SystemExit` 转成了返回码（便于调用方判断），
    所以这里断言的是返回码，不是异常。
    """
    from stocklab.cli import main as cli_main
    rc = cli_main.main(["research", "xsec-topn", "--pool", "short",
                        "--start", "2015-01-01", "--prereg", "x.md",
                        "--price-mode", "adjusted"])
    assert rc == 2

