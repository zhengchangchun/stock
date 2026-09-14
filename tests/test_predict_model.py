"""预测核心（Task 29）：模型数学 + 载荷契约（**纯函数，不碰库**）。

这些测试刻意**不检查预测准不准**（本任务不产出准确率数字）—— 它们检查的是
「每个数字是不是从它声称的那个公式算出来的」以及「这个载荷能不能被次日证伪」。
"""

import math
import statistics

import pytest

from stocklab.data.models import Bar
from stocklab.predict import model as M


def mk_bars(closes, *, start_day=1, code="000333", spread=0.01):
    """按收盘价造 K 线：high/low 在 close 上下 `spread` 处。"""
    out = []
    for i, c in enumerate(closes):
        out.append(Bar(code=code, date=f"2024-{1 + i // 28:02d}-{start_day + i % 28:02d}",
                       open=c, high=c * (1 + spread), low=c * (1 - spread), close=c,
                       volume=1000.0, amount=c * 1000, turnover=1.0,
                       source="test", adj_mode="qfq"))
    return out


def ramp(n=80, start=10.0, step=0.05):
    return [start + i * step for i in range(n)]


# ---------- 三分类阈值与概率 ----------

def test_flat_band_matches_spec_8_2():
    """总纲 §8.2：±0.5% 为 flat。这个数**不是**本实现可以自己定的旋钮。"""
    assert M.FLAT_BAND == 0.005


def test_probabilities_sum_to_one_exactly():
    """三个概率之和必须**精确**为 1.0（flat 用减法得到，不是各自四舍五入）。"""
    bars = mk_bars([10 + math.sin(i / 3) for i in range(80)])
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    d = f["direction"]
    assert d["up"] + d["flat"] + d["down"] == 1.0
    assert all(0.0 <= d[k] <= 1.0 for k in d)


def test_probabilities_match_the_documented_formula():
    """手算一遍：p_up/p_down 必须等于 N(mu, sigma) 在 ln(1±0.005) 处的尾部质量。"""
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    closes = [b.close for b in bars][-(M.WINDOW + 1):]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mu, sigma = statistics.fmean(rets), statistics.stdev(rets)
    nd = statistics.NormalDist(mu, sigma)
    assert f["direction"]["down"] == pytest.approx(nd.cdf(math.log(0.995)), abs=1e-6)
    assert f["direction"]["up"] == pytest.approx(1 - nd.cdf(math.log(1.005)), abs=1e-6)
    assert f["evidence"]["inputs"]["mu"] == pytest.approx(mu, abs=1e-12)
    assert f["evidence"]["inputs"]["sigma"] == pytest.approx(sigma, abs=1e-12)


def test_drift_never_alone_decides_when_volatility_dominates():
    """高波动 + 零漂移 → 上下概率应接近对称，不许凭空造出方向 edge。

    零漂移是**构造**出来的（对数收益严格 ±3% 交替，均值 ≈ −0.00045），
    不用随机数 —— 随机的种子会让「对称」这件事变成运气。
    """
    closes, c = [10.0], 10.0
    for i in range(200):
        c *= 1.03 if i % 2 == 0 else 0.97
        closes.append(c)
    bars = mk_bars(closes)
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    d = f["direction"]
    assert abs(d["up"] - d["down"]) < 0.05


def test_zero_volatility_is_hard_rejected():
    """恒定价格 → sigma=0，给不出分布。**拒绝**，不许编一个 0.33/0.33/0.33。"""
    bars = mk_bars([10.0] * 80)
    with pytest.raises(M.DegenerateInput):
        M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})


def test_insufficient_history_is_hard_rejected():
    bars = mk_bars(ramp(30))
    with pytest.raises(M.DegenerateInput):
        M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})


def test_last_bar_must_be_asof():
    """asof 当日无 K 线（停牌/缺口）→ 拒绝。拿「最近一根」当今日 = 用昨天决定今天。"""
    bars = mk_bars(ramp(80))
    with pytest.raises(M.DegenerateInput):
        M.compute_forecast(code="000333", asof="2030-01-01", bars=bars,
                           target_date="2030-01-02", strategy_mix={"weights": {}})


# ---------- range_80 ----------

def test_range_80_is_the_model_central_80pct_interval():
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    mu = f["evidence"]["inputs"]["mu"]
    sigma = f["evidence"]["inputs"]["sigma"]
    close = f["evidence"]["inputs"]["close"]
    z = statistics.NormalDist().inv_cdf(0.90)
    assert f["range_80"][0] == pytest.approx(round(close * math.exp(mu - z * sigma), 2))
    assert f["range_80"][1] == pytest.approx(round(close * math.exp(mu + z * sigma), 2))


def test_range_80_widens_with_volatility():
    calm = mk_bars([10.0 * (1 + 0.001 * (i % 2)) for i in range(80)])
    wild = mk_bars([10.0 * (1 + 0.05 * (i % 2)) for i in range(80)])
    a = M.compute_forecast(code="c", asof=calm[-1].date, bars=calm,
                           target_date="t", strategy_mix={"weights": {}})
    b = M.compute_forecast(code="c", asof=wild[-1].date, bars=wild,
                           target_date="t", strategy_mix={"weights": {}})
    span = lambda f: f["range_80"][1] - f["range_80"][0]  # noqa: E731
    assert span(b) > span(a) * 10


def test_range_80_brackets_the_model_median():
    """区间必须夹住模型中位数 `close*exp(mu)`（这是它的定义，不是巧合）。

    刻意**不**断言「夹住当前收盘价」：单边趋势里 mu 会让整个区间落在收盘价之上，
    那是模型的正确行为，不是 bug —— 断言它会逼实现去凑一个好看的区间。
    """
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    median = f["evidence"]["inputs"]["close"] * math.exp(f["evidence"]["inputs"]["mu"])
    assert f["range_80"][0] <= median <= f["range_80"][1]


# ---------- key_levels / invalidate_if ----------

def test_key_levels_are_measured_from_the_window():
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    win = bars[-M.LEVEL_WINDOW:]
    roles = {k["role"]: k for k in f["key_levels"]}
    assert roles["support"]["price"] == pytest.approx(round(min(b.low for b in win), 2))
    assert roles["resistance"]["price"] == pytest.approx(
        round(max(b.high for b in win), 2))
    for k in f["key_levels"]:
        assert 0.0 <= k["p_touch"] <= 1.0


def test_p_touch_matches_the_standard_normal_reference():
    """`p_touch` 必须取到**中间值**，且等于「标准化后取标准正态尾部」的手算结果。

    回归自一次真实数据回放：000333 / 600690 的 4 个价位 `p_touch` **全是 0.0**，
    而阻力位离现价只有 ~1.7%。根因是 `z` 已经标准化过，却又喂进了
    `NormalDist(mu, sigma).cdf` → 被**二次标准化** → 尾部概率恒为 0/1
    （ERROR_DIARY 2026-09-15）。「严格落在 (0,1) 内」这条断言就是钉死这个饱和：
    一个恒 0 的触及概率**看起来**像「不会触及」，是典型的合法数字、错误结论。
    """
    # 振幅要**够大**（±5%）：波动太小则价位在标准化尺度上离现价很远，
    # 尾部概率本来就该是 ~0，那样「饱和」就不是 bug 而是正确结果，
    # 测试也就抓不到二次标准化（第一次写这个 fixture 用 ±1% 就踩了这个坑）。
    bars = mk_bars([10.0 * (1 + 0.05 * math.sin(i / 2)) for i in range(80)])
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    i = f["evidence"]["inputs"]
    sn = statistics.NormalDist()
    for k in f["key_levels"]:
        above = k["role"] == "resistance"
        ext = i["up_ext"] if above else i["dn_ext"]
        ref = i["close"] * (1 + ext) if above else i["close"] * (1 - ext)
        z = (math.log(k["price"] / ref) - i["mu"]) / i["sigma"]
        expected = 1 - sn.cdf(z) if above else sn.cdf(z)
        assert k["p_touch"] == pytest.approx(round(expected, 6), abs=1e-6)
    # 饱和守卫**只要求至少一个价位不饱和**，不是每个都要求：支撑位在标准化
    # 尺度上离现价很远时，`p_touch ≈ 0` 是**正确**结论，断言它非 0 等于逼模型
    # 编一个「够得着」。而二次标准化会让**所有**价位一起塌成 0/1 → `any` 变红。
    assert any(0.01 < k["p_touch"] < 0.99 for k in f["key_levels"]), f["key_levels"]


def test_invalidate_if_is_derived_from_computed_levels():
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    roles = {k["role"]: k for k in f["key_levels"]}
    assert f"{roles['support']['price']:.2f}" in f["invalidate_if"]
    assert f"{roles['resistance']['price']:.2f}" in f["invalidate_if"]


# ---------- action / size_pct ----------

def test_action_and_size_follow_the_documented_rule():
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    d = f["direction"]
    if d["flat"] >= d["up"] and d["flat"] >= d["down"]:
        assert f["action"] == "wait" and f["size_pct"] == 0.0
    elif d["up"] > d["down"]:
        assert f["action"] == "add"
        assert f["size_pct"] == pytest.approx(round(100 * (1 - d["down"]), 2))
    else:
        assert f["action"] == "trim"
        assert f["size_pct"] == pytest.approx(round(100 * (1 - d["up"]), 2))


def test_size_pct_is_disclosed_as_not_executable():
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    assert "不可直接执行" in f["evidence"]["notes"]["size_pct"]


# ---------- 载荷契约 ----------

def test_payload_has_exactly_the_spec_8_1_contract_fields():
    bars = mk_bars(ramp(80))
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    assert set(M.CONTRACT_FIELDS) <= set(f)
    assert f["code"] == "000333"
    assert f["asof_date"] == bars[-1].date
    assert f["target_date"] == "2024-12-31"
    assert f["model_version"] == M.MODEL_VERSION
    assert f["action"] in ("hold", "trim", "add", "exit", "wait")


def test_payload_hash_is_stable_and_order_independent():
    bars = mk_bars(ramp(80))
    kw = dict(code="000333", asof=bars[-1].date, bars=bars, target_date="2024-12-31")
    a = M.compute_forecast(strategy_mix={"weights": {}, "degenerate": True}, **kw)
    b = M.compute_forecast(strategy_mix={"degenerate": True, "weights": {}}, **kw)
    assert M.payload_hash(a) == M.payload_hash(b)
    assert len(M.payload_hash(a)) == 64


def test_payload_hash_changes_when_any_contract_field_changes():
    bars = mk_bars(ramp(80))
    a = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix={"weights": {}})
    b = dict(a, target_date="2024-12-30")
    assert M.payload_hash(a) != M.payload_hash(b)


# ---------- strategy_mix 退化态 ----------

def test_degenerate_strategy_mix_is_carried_into_the_payload():
    bars = mk_bars(ramp(80))
    mix = M.degenerate_strategy_mix(excluded={"trend_ma": "被否证"}, benchmark_only=["buy_and_hold"])
    f = M.compute_forecast(code="000333", asof=bars[-1].date, bars=bars,
                           target_date="2024-12-31", strategy_mix=mix)
    assert f["strategy_mix"]["weights"] == {}
    assert f["strategy_mix"]["degenerate"] is True
    assert f["strategy_mix"]["excluded"]["trend_ma"] == "被否证"
    assert f["strategy_mix"]["benchmark_only"] == ["buy_and_hold"]


def test_model_ignores_bars_after_asof():
    """模型自己的那道裁剪：喂进含**未来** K 线的序列，载荷必须与只喂历史时一致。

    与 `test_future_bars_do_not_change_the_payload`（服务层）是**两层不同的防线**：
    服务层保证「取数不越界」，本测试保证「就算越界了，模型自己也不看」。
    两层都要有 —— 本项目在 Task 19 学到的：防线被上游挡住时，
    变异测不出来，但那不代表这一层可以省。
    """
    hist = mk_bars(ramp(80))
    asof = hist[-1].date
    kw = dict(code="000333", asof=asof, target_date="2024-12-31",
              strategy_mix={"weights": {}})
    clean = M.compute_forecast(bars=hist, **kw)

    # 「未来」的 K 线必须**真的排在 asof 之后**：mk_bars 的日期由 start_day + i%28
    # 生成，单独造一段会绕回 1 月（`2024-01-34` 字典序在 `2024-03-24` 之前），
    # 那不是未来，是中间 —— 造完必须核对首末日期（ERROR_DIARY 2026-09-15 #4）。
    full = mk_bars(ramp(80) + [99.0] * 10)
    assert full[-1].date > asof and full[80].date > asof
    dirty = M.compute_forecast(bars=full, **kw)
    assert M.payload_hash(clean) == M.payload_hash(dirty)
    assert clean == dirty
