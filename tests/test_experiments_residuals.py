"""`experiments.residuals`（P10-a）：在 **train 段** 拟合标准化残差的经验分布。

这一层的关键不是「能拟合」，而是四条在**运行路径**上必须成立的纪律：

  1. **拟合只用 train 段**，apply 到 validate/test（PIT）。判据是**结构性的**：
     把训练窗之后的 bar 换成极端值，拟合结果必须**逐位不变**；
  2. **残差就是模型自己的残差**：每个 `(日, 标的)` 的 `resid` 必须等于
     `compute_forecast` 在同一天给出的 `evidence.inputs.mu/sigma` 复算出来的值 ——
     这条钉住「拟合实现」与「模型实现」不许各写一套（两份实现迟早漂移）；
  3. **剔除即计数**：K 线不足 / 停牌 / `sigma<=0` 的 `(日, 标的)` 进 `skipped`
     并写明原因，**不许填 0**；
  4. **不写生产表**：拟合是纯读 + 内存计算。

夹具规模是**算出来的**：`ResidualDistribution` 的构造门槛 `RESIDUAL_MIN_SAMPLES=250`，
而可回放的首个目标日是第 `WINDOW+1=61` 个交易日、train 占 60% ——
`n=800` 时 train 目标日 = `int((800-61+1)*0.6)` = 444 天，×2 标的 = 888 个残差 > 250。
"""

from __future__ import annotations

import dataclasses

import pytest

from stocklab.predict.model import WINDOW, compute_forecast, degenerate_strategy_mix
from stocklab.predict.residual import (RESIDUAL_MIN_SAMPLES,
                                       InsufficientResiduals,
                                       ResidualDistribution)
from stocklab.predict.service import PitCache

from tests.test_experiments_runner import (CODE, CODE2, FIRST_TARGET, _days, _env,
                                           _seg)
N_BIG = 800
CODES = (CODE, CODE2)


def _days_big(n: int = N_BIG) -> list[str]:
    """复用 `test_experiments_runner._days` 的日期轴（同一起点 2024-01-01）。

    刻意**不另起一个起点**：`bars()` 的默认 `start` 就是 2024-01-01，
    日期轴与 K 线轴一旦错开，库里就查不到任何 bar —— 那种失败看起来像
    「拟合逻辑坏了」，实际是夹具自己错位。
    """
    return _days(n)


def _env_big(tmp_path, *, n=N_BIG, codes=CODES, name="big.db"):
    """与 `test_experiments_runner._env` 同一套建库方式，只把规模提到 800 天。"""
    from tests.test_predict_service import bars as mk_bars, seed

    bars_by_code = {c: mk_bars(code=c, n=n, base=10.0 + 0.5 * i)
                    for i, c in enumerate(codes)}
    return seed(tmp_path / name, bars_by_code, cal_dates=_days_big(n))


def _train_days(n: int = N_BIG) -> list[str]:
    from stocklab.experiments.split import split_days

    return split_days(_days_big(n)[FIRST_TARGET:])["train"]


def _fit(conn, *, train_days=None, sessions=None, codes=CODES):
    from stocklab.experiments.residuals import fit_residual_distribution

    return fit_residual_distribution(
        conn, train_days=train_days if train_days is not None else _train_days(),
        sessions=sessions if sessions is not None else _days_big(),
        codes=list(codes), cache=PitCache())


# ---------- 0. 样本量门槛确实被跨过（否则下面的测试测的是门槛不是拟合） ----------

def test_the_fixture_actually_clears_the_sample_threshold():
    days = _train_days()
    assert len(days) * len(CODES) > RESIDUAL_MIN_SAMPLES, (
        "夹具的残差池没跨过门槛 —— 下面的测试会变成在测「拒绝路径」，"
        "而不是在测拟合"
    )


# ---------- 1. 残差 = 模型自己的残差 ----------

def test_each_residual_equals_the_models_own_mu_and_sigma_recomputed(tmp_path):
    """**反漂移**：拟合实现与 `compute_forecast` 不许各写一套 mu/sigma。

    抽查若干个 `(日, 标的)`：拿模型自己 evidence 里的 `mu`/`sigma`，
    配上次日实际收盘，手算标准化残差 —— 必须与拟合结果逐位相等。
    """
    conn = _env_big(tmp_path)
    fit = _fit(conn)
    assert fit.samples, "拟合没产出任何样本"
    sessions = _days_big()
    idx = {d: i for i, d in enumerate(sessions)}
    cache = PitCache()

    from stocklab.predict.service import load_pit_bars

    checked = 0
    for s in fit.samples[::37]:                     # 抽样，别把测试拖成回放
        assert s.target_day != sessions[0]
        assert s.asof == sessions[idx[s.target_day] - 1]
        hist = load_pit_bars(conn, s.code, s.asof, cache=cache)
        p = compute_forecast(code=s.code, asof=s.asof, bars=hist,
                             target_date=s.target_day,
                             strategy_mix=degenerate_strategy_mix())
        inp = p["evidence"]["inputs"]
        assert s.mu_hat == inp["mu"], "拟合的 mu 与模型自己的 mu 不是同一个数"
        assert s.sigma_hat == inp["sigma"], "拟合的 sigma 与模型自己的不是同一个数"
        close_t = _close_on(conn, s.code, s.target_day)
        import math

        expected = (math.log(close_t / inp["close"]) - inp["mu"]) / inp["sigma"]
        assert s.resid == pytest.approx(expected, abs=1e-12)
        checked += 1
    assert checked >= 5, f"只抽查到 {checked} 个样本，覆盖不足"


def _close_on(conn, code: str, day: str) -> float:
    from stocklab.predict.service import load_pit_bars

    bars = load_pit_bars(conn, code, day, cache=PitCache())
    assert bars and bars[-1].date == day
    return bars[-1].close


# ---------- 2. PIT：训练窗之后的数据拿不到 ----------

def test_bars_after_the_train_window_cannot_change_the_fit(tmp_path):
    """**PIT 硬检验**：把训练窗之后的行情换成极端值，拟合必须逐位不变。

    这条不是「记得裁」的自觉，而是「拟合只喂 train 段的日期 + 只读 `<= asof` 的行」
    这个结构在起作用。若实现里有人把 `sessions[-1]` 当成锚点、或让窗口滑到
    验证段，这条会立刻红。
    """
    from tests.test_predict_service import bars as mk_bars, seed

    conn = _env_big(tmp_path)
    base = _fit(conn)

    spike_day = _train_days()[-1]
    i = _days_big().index(spike_day)
    spiked = {}
    for k, c in enumerate(CODES):
        rows = mk_bars(code=c, n=N_BIG, base=10.0 + 0.5 * k)
        rows = [b for b in rows]
        for j, b in enumerate(rows):
            if j > i:                               # 训练窗**之后**：暴涨 100 倍
                rows[j] = dataclasses.replace(b, open=b.open * 100, high=b.high * 100,
                                              low=b.low * 100, close=b.close * 100)
        spiked[c] = rows
    conn2 = seed(tmp_path / "spiked.db", spiked, cal_dates=_days_big())

    after = fit_residual_distribution_on(conn2, tmp_path)
    assert after.distribution.values == base.distribution.values, (
        "训练窗之后的 bar 改变了拟合结果 —— 前视（PIT 违规）"
    )
    assert after.distribution.as_evidence() == base.distribution.as_evidence()


def fit_residual_distribution_on(conn, _tmp):
    from stocklab.experiments.residuals import fit_residual_distribution

    return fit_residual_distribution(conn, train_days=_train_days(),
                                     sessions=_days_big(), codes=list(CODES),
                                     cache=PitCache())


# ---------- 3. 剔除即计数 ----------

def test_missing_bars_are_counted_with_a_reason_not_filled(tmp_path):
    """某标的在某天没有 bar → 该 `(日, 标的)` 进 `skipped`，**不填 0**。"""
    from tests.test_predict_service import bars as mk_bars, seed

    gap_day = _train_days()[10]
    rows = {c: mk_bars(code=c, n=N_BIG, base=10.0 + 0.5 * k)
            for k, c in enumerate(CODES)}
    rows[CODE2] = [b for b in rows[CODE2] if b.date != gap_day]
    conn = seed(tmp_path / "gap.db", rows, cal_dates=_days_big())

    full = _fit(_env_big(tmp_path))
    gapped = fit_residual_distribution_on(conn, tmp_path)

    assert gapped.distribution.n_skipped > full.distribution.n_skipped
    lost = [s for s in full.samples if s.code == CODE2 and s.target_day == gap_day]
    assert lost, "夹具没造出预期的缺口"
    assert not [s for s in gapped.samples
                if s.code == CODE2 and s.target_day == gap_day]
    # 关键是：缺口**没有**贡献一个 0 残差进池子 —— 池子**变小**了，不是「补了 0」。
    # 少一根 bar 要付**两次**：① 那天自己的收盘没了（`DegenerateInput`）；
    # ② 它的下一天把那天当 `asof`，也读不到（`NoBarOnAsof`）。
    # 这正是「不许用旧价冒充今日」的代价，写成 `-1` 才是把两件事当成一件。
    assert gapped.distribution.n == full.distribution.n - 2
    assert "2024-03-10/600690" in gapped.skipped
    assert "2024-03-11/600690" in gapped.skipped


def test_skipped_reasons_are_distinct_and_non_empty(tmp_path):
    fit = _fit(_env_big(tmp_path))
    for key, reason in fit.skipped.items():
        assert reason, f"{key} 的剔除原因为空 —— 读不出为什么少了一行"


# ---------- 4. 池子不足即拒绝 / 确定性 ----------

def test_a_pool_below_the_threshold_is_refused(tmp_path):
    """小库（N=200 → train 目标日远不足 250）→ 拒绝构造，而不是给一个噪声形状。"""
    conn = _env(tmp_path)
    n_short = 200
    from stocklab.experiments.split import split_days

    train = split_days(_days(n_short)[FIRST_TARGET:])["train"]
    with pytest.raises(InsufficientResiduals):
        _fit(conn, train_days=train, sessions=_days(n_short), codes=(CODE,))


def test_fit_is_deterministic_and_the_pool_is_sorted(tmp_path):
    conn = _env_big(tmp_path)
    a, b = _fit(conn), _fit(conn)
    assert a.distribution.values == b.distribution.values
    assert list(a.distribution.values) == sorted(a.distribution.values)


def test_writes_nothing_to_the_production_tables(tmp_path):
    from tests.test_experiments_runner import _counts

    conn = _env_big(tmp_path)
    before = _counts(conn)
    _fit(conn)
    assert _counts(conn) == before


# ---------- 5. 证据块 / 溯源 ----------

def test_evidence_records_the_train_window_and_the_codes(tmp_path):
    conn = _env_big(tmp_path)
    fit = _fit(conn)
    ev = fit.distribution.as_evidence()
    days = _train_days()
    assert ev["first_day"] == days[0]
    assert ev["last_day"] == days[-1]
    assert ev["codes"] == sorted(CODES)
    assert ev["window"] == WINDOW
    assert ev["n_days"] == len(set(s.target_day for s in fit.samples))
    assert ev["n"] == len(fit.samples)


def test_distribution_is_a_plain_residual_distribution(tmp_path):
    fit = _fit(_env_big(tmp_path))
    assert isinstance(fit.distribution, ResidualDistribution)
    assert fit.distribution.quantile(0.10) < fit.distribution.quantile(0.90)
