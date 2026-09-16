"""P34 / P29：资金流 / 估值作为「次日方向先验」的两个单变量变体。

这里钉住的东西，每一件都对应预注册里的硬约束
（`docs/experiments/2026-09-17-valuation-moneyflow-oos.md`）：

  1. `compute_forecast` 的两个新 `mu_mode`（`mf_sign` / `val_pe_pct`）只换**漂移符号的来源**，
     幅度仍是 |mu_sample|；`None` / 非法值 → `DegenerateInput`（不许静默退化，§1 PIT 约束）；
  2. `main_net = 0 → mu = 0`：与 `zero` 变体同口径（§2 V1）；
  3. `load_mf_sign` / `load_val_pe_pct_sign` 是 **PIT 硬约束**：只读 `date <= asof`、
     **必须精确命中 asof 当天**（缺行 → `None`，绝不「取最近一根」）、NULL → `None`；
  4. 分位用「含等」约定（复用 `rv_percentile`），窗口 = 最近 756 行、阈值 0.30/0.70、
     历史不足 756 行 → `None`（§1 / §2 V2）。
"""

from __future__ import annotations

import inspect

import pytest

from stocklab.data.models import MoneyFlowDaily, ValuationDaily
from stocklab.experiments.variants import load_mf_sign, load_val_pe_pct_sign
from stocklab.predict.model import DegenerateInput, ForecastSpec, compute_forecast
from stocklab.predict.service import PitCache
from stocklab.store import repo
from tests.test_experiments_variants import CODE, _trend_bars
from tests.test_predict_service import NOW, _to_date, _to_ord, bars, seed

D0 = _to_ord("2024-01-01")


def _mf_db(tmp_path, rows):
    """建库并写入 `(date, main_net)` 行。`main_net=None` → NULL（与缺行是两件事）。"""
    conn = seed(tmp_path / "mf.db", {CODE: bars(n=10)})
    data = [(MoneyFlowDaily(code=CODE, date=d, main_net=v), "sha", None)
            for d, v in rows]
    repo.insert_money_flow(conn, data, now=NOW)
    return conn


def _val_db(tmp_path, pe_values, *, name="val.db"):
    """建库并写入 `pe_values` 行（日期从 D0 起连续编号）。"""
    conn = seed(tmp_path / name, {CODE: bars(n=10)})
    data = [(ValuationDaily(code=CODE, date=_to_date(D0 + i), pe_ttm=v),
             "sha", None) for i, v in enumerate(pe_values)]
    repo.insert_valuation(conn, data, now=NOW)
    return conn


# ---------- 1. compute_forecast：只换漂移符号来源 ----------

def test_mf_sign_flips_the_drift_sign_only():
    hist = _trend_bars()
    asof = hist[-1].date
    up = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                          strategy_mix={}, spec=ForecastSpec(mu_mode="mf_sign"),
                          mf_sign=1)
    down = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                            strategy_mix={}, spec=ForecastSpec(mu_mode="mf_sign"),
                            mf_sign=-1)
    assert up["direction"]["up"] > 10 * up["direction"]["down"]
    assert down["direction"]["down"] > 10 * down["direction"]["up"]
    # |mu| 相同 → 两侧镜像（差异只来自标签带不对称）
    assert up["direction"]["up"] == pytest.approx(down["direction"]["down"], abs=0.005)


def test_mf_sign_zero_matches_the_zero_variant():
    """`main_net=0 → mu=0`：预测与 `rw-mu0`（漂移置 0）完全一样。"""
    hist = _trend_bars()
    asof = hist[-1].date
    a = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={}, spec=ForecastSpec(mu_mode="mf_sign"),
                         mf_sign=0)
    b = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={}, spec=ForecastSpec(mu_mode="zero"))
    assert a["direction"] == b["direction"]
    assert a["range_80"] == b["range_80"]


def test_mf_sign_refuses_when_missing():
    hist = _trend_bars()
    with pytest.raises(DegenerateInput, match="mf_sign=None"):
        compute_forecast(code=CODE, asof=hist[-1].date, bars=hist,
                         target_date=hist[-1].date, strategy_mix={},
                         spec=ForecastSpec(mu_mode="mf_sign"), mf_sign=None)


def test_mf_sign_refuses_invalid_value():
    hist = _trend_bars()
    with pytest.raises(DegenerateInput, match="mf_sign"):
        compute_forecast(code=CODE, asof=hist[-1].date, bars=hist,
                         target_date=hist[-1].date, strategy_mix={},
                         spec=ForecastSpec(mu_mode="mf_sign"), mf_sign=2)


def test_val_pe_pct_flips_the_drift_sign_only():
    hist = _trend_bars()
    asof = hist[-1].date
    up = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                          strategy_mix={}, spec=ForecastSpec(mu_mode="val_pe_pct"),
                          val_sign=1)
    down = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                            strategy_mix={}, spec=ForecastSpec(mu_mode="val_pe_pct"),
                            val_sign=-1)
    assert up["direction"]["up"] > 10 * up["direction"]["down"]
    assert down["direction"]["down"] > 10 * down["direction"]["up"]
    assert up["direction"]["up"] == pytest.approx(down["direction"]["down"], abs=0.005)


def test_val_pe_pct_zero_matches_the_zero_variant():
    hist = _trend_bars()
    asof = hist[-1].date
    a = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={}, spec=ForecastSpec(mu_mode="val_pe_pct"),
                         val_sign=0)
    b = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={}, spec=ForecastSpec(mu_mode="zero"))
    assert a["direction"] == b["direction"]
    assert a["range_80"] == b["range_80"]


def test_val_pe_pct_refuses_when_missing():
    hist = _trend_bars()
    with pytest.raises(DegenerateInput, match="val_sign=None"):
        compute_forecast(code=CODE, asof=hist[-1].date, bars=hist,
                         target_date=hist[-1].date, strategy_mix={},
                         spec=ForecastSpec(mu_mode="val_pe_pct"), val_sign=None)


def test_val_pe_pct_refuses_invalid_value():
    hist = _trend_bars()
    with pytest.raises(DegenerateInput, match="val_sign"):
        compute_forecast(code=CODE, asof=hist[-1].date, bars=hist,
                         target_date=hist[-1].date, strategy_mix={},
                         spec=ForecastSpec(mu_mode="val_pe_pct"), val_sign=-2)


def test_mf_sign_records_mf_sign_in_evidence_only():
    hist = _trend_bars()
    asof = hist[-1].date
    base = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                            strategy_mix={})
    var = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                           strategy_mix={}, spec=ForecastSpec(mu_mode="mf_sign"),
                           mf_sign=1)
    assert "mf_sign" not in base["evidence"]["inputs"]
    assert var["evidence"]["inputs"]["mf_sign"] == 1


def test_val_pe_pct_records_val_sign_in_evidence_only():
    hist = _trend_bars()
    asof = hist[-1].date
    base = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                            strategy_mix={})
    var = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                           strategy_mix={}, spec=ForecastSpec(mu_mode="val_pe_pct"),
                           val_sign=-1)
    assert "val_sign" not in base["evidence"]["inputs"]
    assert var["evidence"]["inputs"]["val_sign"] == -1


# ---------- 2. load_mf_sign：资金流符号的 PIT 硬约束 ----------

def test_load_mf_sign_reads_the_sign_of_main_net(tmp_path):
    dates = [_to_date(D0 + i) for i in range(5)]
    conn = _mf_db(tmp_path, [(dates[0], 1e6), (dates[1], -2e5),
                             (dates[2], 0.0), (dates[3], None)])
    try:
        assert load_mf_sign(conn, CODE, dates[0]) == 1
        assert load_mf_sign(conn, CODE, dates[1]) == -1
        assert load_mf_sign(conn, CODE, dates[2]) == 0
        assert load_mf_sign(conn, CODE, dates[3]) is None       # NULL main_net
        assert load_mf_sign(conn, CODE, dates[4]) is None       # 缺行
    finally:
        conn.close()


def test_load_mf_sign_missing_asof_row_is_none_not_the_nearest(tmp_path):
    dates = [_to_date(D0 + i) for i in range(3)]
    conn = _mf_db(tmp_path, [(dates[0], 1e6)])
    try:
        gap = _to_date(D0 + 5)                       # 中间缺了几天
        assert load_mf_sign(conn, CODE, gap) is None
    finally:
        conn.close()


def test_load_mf_sign_ignores_future_rows(tmp_path):
    dates = [_to_date(D0 + i) for i in range(3)]
    conn = _mf_db(tmp_path, [(dates[0], 1e6), (dates[1], -1e6)])
    try:
        # 未来（dates[2] 之后）再塞一个暴涨，也不许影响 dates[0] 的符号
        assert load_mf_sign(conn, CODE, dates[0]) == 1
    finally:
        conn.close()


def test_load_mf_sign_is_cache_consistent(tmp_path):
    dates = [_to_date(D0 + i) for i in range(2)]
    conn = _mf_db(tmp_path, [(dates[0], 5.0), (dates[1], -5.0)])
    cache = PitCache()
    try:
        assert load_mf_sign(conn, CODE, dates[1], cache=cache) == -1
        assert load_mf_sign(conn, CODE, dates[1], cache=cache) == -1
    finally:
        conn.close()


# ---------- 3. load_val_pe_pct_sign：估值分位（756 / 0.30 / 0.70）----------

def test_load_val_pe_pct_sign_low_percentile_is_positive(tmp_path):
    conn = _val_db(tmp_path, [1000.0] * 755 + [1.0])     # 当前是最小值 → q≈1/756
    try:
        assert load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 755)) == 1
    finally:
        conn.close()


def test_load_val_pe_pct_sign_high_percentile_is_negative(tmp_path):
    conn = _val_db(tmp_path, [1.0] * 755 + [1000.0])     # 当前是最大值 → q=1.0
    try:
        assert load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 755)) == -1
    finally:
        conn.close()


def test_load_val_pe_pct_sign_mid_percentile_is_zero(tmp_path):
    conn = _val_db(tmp_path, [1.0] * 400 + [3.0] * 355 + [2.0])   # q≈401/756
    try:
        assert load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 755)) == 0
    finally:
        conn.close()


def test_load_val_pe_pct_sign_boundaries_at_030_070(tmp_path):
    """含等分位的边界：`# ≤ 当前` 的整数阈值 226 / 530 钉死 0.30 / 0.70。"""

    def sign_for(k):
        vals = list(range(1, 756)) + [k]          # 前 755 行 1..755，末行 = k
        conn = _val_db(tmp_path, [float(v) for v in vals], name=f"val{k}.db")
        try:
            return load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 755))
        finally:
            conn.close()

    assert sign_for(226) == 1      # 226/756 = 0.2989 ≤ 0.30
    assert sign_for(227) == 0      # 227/756 = 0.3003 > 0.30
    assert sign_for(529) == 0      # 529/756 = 0.6997 < 0.70
    assert sign_for(530) == -1     # 530/756 = 0.7010 ≥ 0.70


def test_load_val_pe_pct_sign_requires_the_full_756_window(tmp_path):
    conn = _val_db(tmp_path, [float(i) for i in range(100)])   # 历史不足 756
    try:
        assert load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 99)) is None
    finally:
        conn.close()


def test_load_val_pe_pct_sign_missing_asof_row_is_none(tmp_path):
    conn = _val_db(tmp_path, [1000.0] * 756)
    try:
        assert load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 800)) is None
    finally:
        conn.close()


def test_load_val_pe_pct_sign_null_pe_ttm_is_none(tmp_path):
    conn = _val_db(tmp_path, [1000.0] * 755 + [None])
    try:
        assert load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 755)) is None
    finally:
        conn.close()


def test_load_val_pe_pct_sign_ignores_future_rows(tmp_path):
    """未来多一行低估值也不许污染 asof 的分位。"""
    conn = _val_db(tmp_path, [1.0] * 755 + [1000.0])       # asof 处 q=1.0 → -1
    data = [(ValuationDaily(code=CODE, date=_to_date(D0 + 756), pe_ttm=1.0),
             "sha", None)]
    repo.insert_valuation(conn, data, now=NOW)             # 未来一行 pe=1
    try:
        assert load_val_pe_pct_sign(conn, CODE, _to_date(D0 + 755)) == -1
    finally:
        conn.close()


def test_val_pe_pct_sign_thresholds_are_frozen():
    sig = inspect.signature(load_val_pe_pct_sign)
    assert sig.parameters["window"].default == 756
    assert sig.parameters["lo"].default == 0.30
    assert sig.parameters["hi"].default == 0.70
