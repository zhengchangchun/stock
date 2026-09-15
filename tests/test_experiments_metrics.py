"""Task 38：口径冻结 + 配对日差 + 晋级判定（`stocklab/experiments/metrics.py`）。

这一层是「防自欺」真正落地的地方，四条纪律各有一条测试：

  1. **口径版本不一致必须报错**，不许照比（`MetricVersionMismatch`）；
  2. **不可评分的行既不在分子也不在分母**（配对比较的两个计数都要对得上）；
  3. **判定顺序**：validate 不到 `WIN`，test 根本不会被打开 ——
     「用 test 救活一个 validate 输掉的变体」在结构上做不到；
  4. **`selection_split='test'` 直接抛错**（封存段不能当选择依据）。
"""

from __future__ import annotations

import pytest

from stocklab.experiments.metrics import (METRIC_VERSION, MetricVersionMismatch,
                                          TestSetLeak, assert_metric_version,
                                          daily_stats, decide, gate,
                                          paired_daily_delta)
from stocklab.verify.report import _daily, summarize

from tests.test_verify_report import _row, _unscorable


def _rows(kind: str, *, days: int = 20, codes=("000333",), hit: int = 1,
          brier: float = 0.3, delta_hit: int = 0, delta_brier: float = 0.0,
          base_dir=1):
    """造一组基线行；变体行 = 基线行 + `delta_*`。"""
    day_list = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(days)]
    out = []
    for d in day_list:
        for c in codes:
            r = _row(d, code=c, hit=base_dir, brier=brier)
            if kind == "variant":
                r = dict(r)
                r["hit_direction"] = base_dir + delta_hit
                r["notes"] = {**r["notes"], "brier": brier + delta_brier}
            out.append(r)
    return out


# ---------- 1. 口径冻结 ----------

def test_metric_version_mismatch_is_refused_not_compared():
    """不同 `metric_version` 的数字放在一起相减，差值没有任何含义 —— 必须报错。"""
    a = {"metric_version": METRIC_VERSION, "x": 1}
    b = {"metric_version": "p9-metrics-v2", "x": 2}
    with pytest.raises(MetricVersionMismatch, match="口径版本不一致"):
        assert_metric_version(a, b)


def test_metric_version_matches_are_returned():
    a = {"metric_version": METRIC_VERSION}
    assert assert_metric_version(a, dict(a)) == METRIC_VERSION
    assert METRIC_VERSION == "p8-metrics-v1"


def test_missing_metric_version_is_also_a_mismatch():
    """没有版本号的报告**同样**拒绝 —— 「没写」不等于「一样」。"""
    with pytest.raises(MetricVersionMismatch):
        assert_metric_version({"x": 1}, {"metric_version": METRIC_VERSION})


# ---------- 2. 配对日差：不可评分不进分母 ----------

def test_daily_stats_matches_the_report_formula():
    """自己写一遍的按日聚类统计量必须与 P7 的 `report._daily` **逐位相等**。"""
    by_day = {"2026-01-05": [1.0, 0.0], "2026-01-06": [1.0],
              "2026-01-07": [0.0, 0.0, 1.0], "2026-01-08": []}
    assert daily_stats(by_day) == _daily(by_day)


def test_daily_stats_of_nothing_is_all_none():
    assert daily_stats({}) == {"n_days": 0, "mean": None, "sd": None,
                               "se": None, "ci95": None}


def test_paired_delta_excludes_unscorable_from_both_sides():
    """一行的任一侧不可评分 → 该 `(日, 标的)` 不进配对 —— 分子分母都不进。"""
    base = [_row("2026-01-05", code="000333"), _row("2026-01-05", code="600690")]
    var = [_unscorable("2026-01-05", code="000333"),        # 变体侧不可评分
           _row("2026-01-05", code="600690", hit=0)]
    d = paired_daily_delta(base, var)
    assert d["counts"]["n_pairs"] == 1
    assert d["counts"]["unscorable_either"] == 1
    assert d["direction"]["n_days"] == 1
    # 只剩 600690 那一对，差值 = 变体 − 基线 = 0 − 1 = −1
    assert d["direction"]["mean"] == pytest.approx(-1.0)


def test_paired_delta_counts_rows_that_only_exist_on_one_side():
    """变体因指数缺口整天没出预测 → 记进 `keys_baseline_only`，不许静默消失。"""
    base = [_row("2026-01-05"), _row("2026-01-06")]
    var = [_row("2026-01-05")]
    d = paired_daily_delta(base, var)
    assert d["counts"]["keys_baseline_only"] == 1
    assert d["counts"]["keys_variant_only"] == 0
    assert d["counts"]["n_pairs"] == 1
    assert d["direction"]["n_days"] == 1


def test_paired_delta_is_day_clustered_not_row_averaged():
    """2 标的 × 2 天：有效样本量是 **2 个交易日**，不是 4 行。"""
    base = _rows("baseline", days=2, codes=("000333", "600690"))
    var = _rows("variant", days=2, codes=("000333", "600690"), delta_hit=1)
    d = paired_daily_delta(base, var)
    assert d["counts"]["n_pairs"] == 4
    assert d["direction"]["n_days"] == 2


def test_paired_delta_has_the_four_frozen_metrics():
    base = _rows("baseline", days=5)
    var = _rows("variant", days=5, delta_hit=1, delta_brier=-0.1)
    d = paired_daily_delta(base, var)
    for m in ("direction", "brier", "coverage", "excess"):
        assert m in d
    assert d["brier"]["mean"] == pytest.approx(-0.1)


# ---------- 3. gate ----------

def _paired(base_dir=1, *, days=200, d_hit=1, d_brier=-0.05):
    base = _rows("baseline", days=days, base_dir=base_dir)
    var = _rows("variant", days=days, base_dir=base_dir, delta_hit=d_hit,
                delta_brier=d_brier)
    return paired_daily_delta(base, var)


def test_gate_win_needs_both_metrics_to_improve_significantly():
    g = gate(_paired(d_hit=1, d_brier=-0.05))
    assert g["status"] == "WIN"
    assert g["beat_direction"] and g["beat_brier"]


def test_gate_is_lose_when_one_metric_has_the_wrong_sign():
    """方向变好但 Brier 变差 → **LOSE**（前提不成立），不进 FLAT。"""
    g = gate(_paired(d_hit=1, d_brier=+0.05))
    assert g["status"] == "LOSE"


def test_gate_is_lose_when_direction_has_the_wrong_sign():
    g = gate(_paired(d_hit=-1, d_brier=-0.05))
    assert g["status"] == "LOSE"


def test_gate_is_flat_when_right_sign_but_not_significant():
    """「差一点」不是结论：符号对但 CI 跨 0 → `FLAT`，**不许**当赢。"""
    base = _rows("baseline", days=2, base_dir=1)
    var = _rows("variant", days=2, base_dir=1, delta_hit=1, delta_brier=-0.05)
    var[1]["hit_direction"] = 1          # 第二天差值 0
    var[1]["notes"] = {**var[1]["notes"], "brier": 0.3}
    g = gate(paired_daily_delta(base, var), min_days=1)
    assert g["status"] == "FLAT"
    assert not g["beat_direction"]


def test_gate_is_insufficient_below_the_sample_threshold():
    g = gate(_paired(d_hit=1, d_brier=-0.05), min_days=120)
    assert g["status"] == "INSUFFICIENT" or g["n_days"] >= 120
    small = gate(_paired(days=10, d_hit=1, d_brier=-0.05), min_days=120)
    assert small["status"] == "INSUFFICIENT"
    assert "样本不足" in small["reasons"][0]


def test_gate_on_empty_pairs_is_insufficient_not_a_win():
    g = gate(paired_daily_delta([], []), min_days=1)
    assert g["status"] == "INSUFFICIENT"


# ---------- 4. decide：判定顺序就是纪律 ----------

def _win():
    return gate(_paired(d_hit=1, d_brier=-0.05))


def _lose():
    return gate(_paired(d_hit=-1, d_brier=-0.05))


def _flat():
    base = _rows("baseline", days=2, base_dir=1)
    var = _rows("variant", days=2, base_dir=1, delta_hit=1, delta_brier=-0.05)
    var[1]["hit_direction"] = 1
    var[1]["notes"] = {**var[1]["notes"], "brier": 0.3}
    return gate(paired_daily_delta(base, var), min_days=1)


def test_promoted_requires_validate_and_test_to_both_win():
    v = decide(validate_gate=_win(), test_gate=_win(), selection_split="validate",
               test_evaluated=True)
    assert v["status"] == "promoted"


def test_validate_win_but_test_not_win_is_falsified_not_excused():
    v = decide(validate_gate=_win(), test_gate=_flat(), selection_split="validate",
               test_evaluated=True)
    assert v["status"] == "falsified"
    assert "没能复现" in v["reasons"][0]


def test_validate_win_but_test_lose_is_falsified():
    v = decide(validate_gate=_win(), test_gate=_lose(), selection_split="validate",
               test_evaluated=True)
    assert v["status"] == "falsified"


def test_validate_lose_is_falsified_and_test_was_never_opened():
    v = decide(validate_gate=_lose(), test_gate=None, selection_split="validate",
               test_evaluated=False)
    assert v["status"] == "falsified"
    assert v["test_evaluated"] is False
    assert v["test_gate"] is None


def test_validate_flat_is_inconclusive():
    v = decide(validate_gate=_flat(), test_gate=None, selection_split="validate",
               test_evaluated=False)
    assert v["status"] == "inconclusive"


def test_validate_win_without_opening_test_is_a_programming_error():
    """validate 赢了却忘了打开 test → 抛错。

    否则会打出一个**没有任何样本外证据支持**的 `promoted`
    ——「晋级评审读一次封存段」这条规则就白写了。
    """
    with pytest.raises(RuntimeError, match="晋级评审必须一次性读取封存段"):
        decide(validate_gate=_win(), test_gate=None, selection_split="validate",
               test_evaluated=False)


def test_insufficient_sample_overrides_everything():
    small = gate(_paired(days=10, d_hit=1, d_brier=-0.05), min_days=120)
    v = decide(validate_gate=small, test_gate=_win(), selection_split="validate",
               test_evaluated=True)
    assert v["status"] == "inconclusive"


def test_selection_on_the_test_split_is_refused():
    with pytest.raises(TestSetLeak, match="封存段"):
        decide(validate_gate=_win(), test_gate=_win(), selection_split="test",
               test_evaluated=True)


def test_selection_on_train_can_never_promote():
    """train 是调试区：在它上面赢**不构成**样本外证据，不得据此晋级。"""
    v = decide(validate_gate=_win(), test_gate=_win(), selection_split="train",
               test_evaluated=True)
    assert v["status"] == "inconclusive"
    assert "调试区" in v["reasons"][0]


def test_unknown_selection_split_is_refused():
    with pytest.raises(ValueError, match="未知 selection_split"):
        decide(validate_gate=_win(), test_gate=None, selection_split="holdout",
               test_evaluated=False)


def test_verdict_records_the_evidence_it_was_based_on():
    v = decide(validate_gate=_win(), test_gate=_win(), selection_split="validate",
               test_evaluated=True)
    assert v["selection_split"] == "validate"
    assert v["test_evaluated"] is True
    assert v["validate_gate"]["status"] == "WIN"
    assert v["test_gate"]["status"] == "WIN"


# ---------- P9-a：`test` 按预注册封存（多变量比较轮次） ----------

def test_validate_win_with_policy_sealed_test_is_inconclusive_not_promoted():
    """validate `WIN` 但预注册声明封存 test → `inconclusive`，**不是** `promoted`。

    这是 `test_validate_win_without_opening_test_is_a_programming_error` 的**有意例外**：
    那里抛错是因为「忘了读」，这里不抛错是因为「声明了不读」。两种情形都绝不能
    产出 `promoted` —— 没有封存段证据就不叫晋升。
    """
    v = decide(validate_gate=_win(), test_gate=None, selection_split="validate",
               test_evaluated=False, test_sealed_by_policy=True)
    assert v["status"] == "inconclusive"
    assert "WIN" in v["reasons"][0]
    assert "留给下一轮" in v["reasons"][0]


def test_policy_sealed_contradicted_by_an_actual_test_gate_is_refused():
    """声明封存却拿到了 test_gate —— 自相矛盾，拒绝合成一个说不清来源的结论。"""
    with pytest.raises(ValueError, match="自相矛盾"):
        decide(validate_gate=_win(), test_gate=_win(), selection_split="validate",
               test_evaluated=True, test_sealed_by_policy=True)


def test_policy_sealing_does_not_rescue_a_losing_validate():
    """封存开关只会**更保守**：validate 输了它救不回来，也变不出 test 证据。"""
    v = decide(validate_gate=_lose(), test_gate=None, selection_split="validate",
               test_evaluated=False, test_sealed_by_policy=True)
    assert v["status"] == "falsified"
    v2 = decide(validate_gate=_flat(), test_gate=None, selection_split="validate",
                test_evaluated=False, test_sealed_by_policy=True)
    assert v2["status"] == "inconclusive"
