"""Task 35：准确率报告聚合（`stocklab/verify/report.py`）。

三条必须被测试钉住的东西：
  1. **有效样本量 = 交易日数**（不是行数）—— 行数更大更像样本量，正是陷阱；
  2. **按日聚类**的标准误（A 股同涨同跌，2 只标的不独立）；
  3. **不可评分不进分母**，`UNDETERMINED` 的失效判定不算成「没失效」。
"""

from __future__ import annotations

import pytest

from stocklab.verify.report import MIN_DAYS, render_markdown, summarize


def _row(day: str, *, code="000333", mv="pit-rw-v1.0.1", hit=1, hit_range=1,
         hit_levels=1, level=1.0, brier=0.3, sim=0.01, bh=0.005, index=0.004,
         scorable=True, reason=None, actual_class="up", invalidated=0,
         undetermined=()) -> dict:
    return {
        "model_version": mv, "target_date": day, "code": code,
        "scorable": scorable, "reason_code": reason,
        "hit_direction": hit, "hit_range": hit_range, "hit_levels": hit_levels,
        "score_level": level, "invalidated": invalidated,
        "notes": {"brier": brier, "sim_ret": sim, "bh_ret": bh, "index_pct": index,
                  "excess_ret": sim - bh, "actual_class": actual_class,
                  "undetermined": list(undetermined)},
    }


def _unscorable(day: str, code="600690") -> dict:
    return {"model_version": "pit-rw-v1.0.1", "target_date": day, "code": code,
            "scorable": False, "reason_code": "NO_BAR_TARGET",
            "hit_direction": None, "hit_range": None, "hit_levels": None,
            "score_level": None, "invalidated": None,
            "notes": {"scorable": False, "reason_code": "NO_BAR_TARGET",
                      "reason": "no bar"}}


# ---------- 1. 有效样本量 ----------

def test_effective_sample_size_is_trading_days_not_rows():
    """2 标的 × 3 天 = 6 行，但有效样本量只能是 **3 个交易日**。"""
    rows = [_row(d, code=c) for d in ("2026-01-05", "2026-01-06", "2026-01-07")
            for c in ("000333", "600690")]
    g = summarize(rows, from_date="2026-01-05", to_date="2026-01-07")
    s = g["model_versions"]["pit-rw-v1.0.1"]
    assert s["n_rows"] == 6
    assert s["effective_n"] == 3
    assert s["rows_by_day_avg"] == pytest.approx(2.0)
    assert s["direction"]["accuracy_daily"]["n_days"] == 3


def test_sample_size_gate_flags_insufficient_history():
    rows = [_row(f"2026-01-{i:02d}") for i in range(1, 11)]     # 10 个交易日
    g = summarize(rows, from_date="2026-01-01", to_date="2026-01-10")
    gate = g["model_versions"]["pit-rw-v1.0.1"]["sample_gate"]
    assert gate["sufficient"] is False
    assert "样本不足" in gate["label"]
    assert MIN_DAYS == 120


def test_sample_gate_passes_at_the_threshold():
    # 用序号造 120 个不同日期（报告只看「有几个不同交易日」）
    days = [f"D{i:04d}" for i in range(MIN_DAYS)]
    g = summarize([_row(d) for d in days], from_date="D0000", to_date="D0119")
    assert g["model_versions"]["pit-rw-v1.0.1"]["sample_gate"]["sufficient"] is True


# ---------- 2. 按日聚类（手算） ----------

def test_daily_clustered_accuracy_matches_hand_computation():
    """三天：日均值 = [1.0, 0.5, 0.0] → 均值 0.5，
    样本标准差 = 0.5，标准误 = 0.5/sqrt(3) = 0.288675。手算，不靠实现自证。"""
    rows = [
        _row("2026-01-05", code="A", hit=1), _row("2026-01-05", code="B", hit=1),
        _row("2026-01-06", code="A", hit=1), _row("2026-01-06", code="B", hit=0),
        _row("2026-01-07", code="A", hit=0), _row("2026-01-07", code="B", hit=0),
    ]
    d = summarize(rows, from_date="2026-01-05",
                  to_date="2026-01-07")["model_versions"]["pit-rw-v1.0.1"]["direction"]
    # 行级 = 3/6 = 0.5；按日 = (1.0 + 0.5 + 0.0)/3 = 0.5
    assert d["accuracy_row"] == pytest.approx(0.5)
    assert d["accuracy_daily"]["mean"] == pytest.approx(0.5)
    assert d["accuracy_daily"]["sd"] == pytest.approx(0.5)
    assert d["accuracy_daily"]["se"] == pytest.approx(0.5 / 3 ** 0.5)
    ci = d["accuracy_daily"]["ci95"]
    assert ci[1] - ci[0] == pytest.approx(2 * 1.96 * 0.5 / 3 ** 0.5)


def test_single_day_has_zero_width_interval_and_is_not_a_denominator_trick():
    """只有 1 个交易日时标准误无定义 → 记 0（区间宽度 0），但 `n_days=1` 是明写的，"""
    rows = [_row("2026-01-05"), _row("2026-01-05", code="600690")]
    d = summarize(rows, from_date="2026-01-05",
                  to_date="2026-01-05")["model_versions"]["pit-rw-v1.0.1"]["direction"]
    assert d["accuracy_daily"]["n_days"] == 1
    assert d["accuracy_daily"]["se"] == 0.0


# ---------- 3. 不可评分不进分母；UNDETERMINED 不算「没失效」 ----------

def test_unscorable_rows_are_counted_but_never_scored():
    rows = [_row("2026-01-05"), _unscorable("2026-01-06"), _unscorable("2026-01-07",
                                                                       code="000333")]
    s = summarize(rows, from_date="2026-01-05",
                  to_date="2026-01-07")["model_versions"]["pit-rw-v1.0.1"]
    assert (s["n_scorable"], s["n_unscorable"]) == (1, 2)
    assert s["unscorable_reasons"] == {"NO_BAR_TARGET": 2}
    assert s["n_rows"] == 1 and s["effective_n"] == 1
    # 若不可评分被当成 0 分，行级准确率会变成 1/3 而不是 1/1
    assert s["direction"]["accuracy_row"] == pytest.approx(1.0)


def test_undetermined_invalidate_is_excluded_from_the_rate():
    """`invalidated` 列是 NOT NULL，UNDETERMINED 被迫写 0 —— 不排除就会被算成「没失效」。"""
    rows = [_row("2026-01-05", invalidated=1),
            _row("2026-01-06", invalidated=0, undetermined=("invalidate_if",))]
    inv = summarize(rows, from_date="2026-01-05",
                    to_date="2026-01-06")["model_versions"]["pit-rw-v1.0.1"]["invalidated"]
    assert inv["n_known"] == 1 and inv["n_undetermined"] == 1
    assert inv["rate"] == pytest.approx(1.0)


def test_groups_are_split_by_model_version():
    rows = [_row("2026-01-05", mv="pit-rw-v1.0.0", hit=0),
            _row("2026-01-05", mv="pit-rw-v1.0.1", hit=1)]
    g = summarize(rows, from_date="2026-01-05", to_date="2026-01-05")
    assert set(g["model_versions"]) == {"pit-rw-v1.0.0", "pit-rw-v1.0.1"}
    assert g["model_versions"]["pit-rw-v1.0.0"]["direction"]["accuracy_row"] == 0.0
    assert g["model_versions"]["pit-rw-v1.0.1"]["direction"]["accuracy_row"] == 1.0


# ---------- 4. 基准对照与呈现 ----------

def test_baselines_include_all_four_references():
    rows = [_row("2026-01-05", actual_class="up"),
            _row("2026-01-06", actual_class="flat"),
            _row("2026-01-07", actual_class="down")]
    b = summarize(rows, from_date="2026-01-05",
                  to_date="2026-01-07")["model_versions"]["pit-rw-v1.0.1"]["baselines"]
    assert set(b) == {"always_flat", "always_up", "always_down", "index_300"}
    assert b["always_up"]["accuracy_row"] == pytest.approx(1 / 3)
    assert b["always_flat"]["accuracy_row"] == pytest.approx(1 / 3)


def test_index_baseline_excludes_rows_without_index_data():
    rows = [_row("2026-01-05", index=0.01, actual_class="up"),
            _row("2026-01-06", index=None, actual_class="up")]
    b = summarize(rows, from_date="2026-01-05",
                  to_date="2026-01-06")["model_versions"]["pit-rw-v1.0.1"]["baselines"]
    assert b["index_300"]["n_rows"] == 1
    assert b["index_300"]["accuracy_row"] == pytest.approx(1.0)


def test_index_baseline_is_labelled_contemporaneous_and_untradable():
    """钉住口径诚实性标注：`index_300` 是**同窗口、含未来信息、不可交易**的参照，
    不是可比的预测对手。这段文字被删/被改回去，等于把同期共动当成领先信号。"""
    rows = [_row("2026-01-05", index=0.01, actual_class="up")]
    g = summarize(rows, from_date="2026-01-05", to_date="2026-01-05")
    b = g["model_versions"]["pit-rw-v1.0.1"]["baselines"]["index_300"]

    note = b["note"]
    assert "同窗口" in note
    assert "含未来信息" in note
    assert "不可交易" in note
    assert "不作为可比的预测对手" in note
    # 口径描述必须说清是「同期（asof→target）」，不能只说「当日」
    assert "同期" in note
    assert "asof" in note and "target" in note

    pit = b["pit_comparable"]
    assert "index-mom-dir" in pit          # PIT 口径的可比对手
    assert "always_up" in pit and "always_down" in pit
    assert "不在此列" in pit               # index_300 被显式排除在可比对手之外

    md = render_markdown(g)
    assert "含未来信息" in md and "不可交易" in md
    assert "不作为可比的预测对手" in md
    assert "index-mom-dir" in md


def test_markdown_is_deterministic_and_declares_provenance():
    rows = [_row("2026-01-05"), _unscorable("2026-01-06")]
    g = summarize(rows, from_date="2026-01-05", to_date="2026-01-06")
    md1, md2 = render_markdown(g), render_markdown(g)
    assert md1 == md2
    assert "历史回放" in md1 and "不是实盘记录" in md1
    assert "交易日" in md1                       # 样本量口径写在报告里
    assert "样本不足" in md1
    # markdown 里不许出现「生成时间」这类会让 sha256 每次不同的东西
    assert "生成时间" not in md1


def test_action_excess_is_reported_with_daily_clustering():
    rows = [_row("2026-01-05", sim=0.02, bh=0.01),
            _row("2026-01-06", sim=0.00, bh=0.01)]
    a = summarize(rows, from_date="2026-01-05",
                  to_date="2026-01-06")["model_versions"]["pit-rw-v1.0.1"]["action"]
    assert a["excess_mean"] == pytest.approx((0.01 - 0.01) / 2)
    assert a["excess_win_rate"] == pytest.approx(0.5)
    assert a["excess_daily"]["n_days"] == 2


# ---------- 5. `invalidated` 子群的口径诚实性标注（P24） ----------

def _golden_rows() -> list[dict]:
    """固定 fixture：两日、两标的、含不可评分与 UNDETERMINED 各一条。

    它同时是 T2 数字投影的**唯一输入**，所以必须是确定性的（不含时钟、不含随机）。
    """
    return [
        _row("2026-01-05", code="000333", hit=1, hit_range=1, hit_levels=1,
             level=1.0, brier=0.2, sim=0.012, bh=0.004, index=0.006,
             actual_class="up", invalidated=0),
        _row("2026-01-05", code="600690", hit=0, hit_range=1, hit_levels=0,
             level=0.0, brier=0.8, sim=-0.006, bh=0.004, index=0.006,
             actual_class="up", invalidated=1),
        _row("2026-01-06", code="000333", hit=1, hit_range=0, hit_levels=1,
             level=1.0, brier=0.3, sim=0.002, bh=-0.003, index=0.001,
             actual_class="flat", invalidated=1,
             undetermined=("invalidate_if",)),
        _row("2026-01-07"),          # 第三条有效日，让 CI 不至退化
        _unscorable("2026-01-08"),
    ]


def _numbers_only(obj):
    """递归摘掉全部**文字叶子**，只留数字 / 布尔 / None / 结构。

    用途：机械证明「新增纯文字字段不改动既有数字」—— 若投影变了，说明某个数字动了。
    文字叶子按定义被丢弃，因此新增文字字段**不进**投影（这正是我们要的性质）。
    """
    if isinstance(obj, dict):
        return {k: _numbers_only(v) for k, v in obj.items()
                if not isinstance(v, str)}
    if isinstance(obj, list):
        return [_numbers_only(v) for v in obj]
    return obj


#: 改前（Task 41 之后的实现）在 `_golden_rows()` 上的数字投影。
#: 由 `tests/test_verify_report.py::_numbers_only(summarize(_golden_rows(), ...))`
#: 在**改动 `report.py` 之前**跑出来，见 P24 计划 §4 T2。
_GOLDEN_NUMBERS: dict = {
    "min_days": 120,
    "notes": {},
    "model_versions": {"pit-rw-v1.0.1": {
        "action": {"bh_ret_sum": 0.01,
                   "excess_daily": {"ci95": [-0.0009199999999999994,
                                             0.006920000000000001],
                                    "mean": 0.0030000000000000005, "n_days": 3,
                                    "sd": 0.0034641016151377548, "se": 0.002},
                   "excess_mean": 0.002, "excess_win_rate": 0.75,
                   "index_ret_sum": 0.017,
                   "sim_ret_sum": 0.018000000000000002},
        "baselines": {
            "always_down": {"accuracy_daily": {"ci95": [0.0, 0.0], "mean": 0.0,
                                               "n_days": 3, "sd": 0.0, "se": 0.0},
                            "accuracy_row": 0.0},
            "always_flat": {"accuracy_daily": {"ci95": [-0.32,
                                                         0.9866666666666666],
                                               "mean": 0.3333333333333333,
                                               "n_days": 3,
                                               "sd": 0.5773502691896257,
                                               "se": 0.3333333333333333},
                            "accuracy_row": 0.25},
            "always_up": {"accuracy_daily": {"ci95": [0.013333333333333308,
                                                      1.3199999999999998],
                                             "mean": 0.6666666666666666,
                                             "n_days": 3,
                                             "sd": 0.5773502691896257,
                                             "se": 0.3333333333333333},
                          "accuracy_row": 0.75},
            "index_300": {"accuracy_daily": {"ci95": [0.013333333333333308,
                                                      1.3199999999999998],
                                             "mean": 0.6666666666666666,
                                             "n_days": 3,
                                             "sd": 0.5773502691896257,
                                             "se": 0.3333333333333333},
                          "accuracy_row": 0.75, "n_rows": 4}},
        "direction": {"accuracy_daily": {"ci95": [0.5066666666666667,
                                                  1.1600000000000001],
                                         "mean": 0.8333333333333334, "n_days": 3,
                                         "sd": 0.28867513459481287,
                                         "se": 0.16666666666666666},
                      "accuracy_row": 0.75,
                      "brier_daily": {"ci95": [0.23600000000000002,
                                               0.4973333333333334],
                                      "mean": 0.3666666666666667, "n_days": 3,
                                      "sd": 0.11547005383792516,
                                      "se": 0.06666666666666668},
                      "brier_row": 0.4},
        "effective_n": 3,
        "invalidated": {"n_known": 3, "n_undetermined": 1,
                        "rate": 0.3333333333333333},
        "levels": {"hit_all_row": 0.75, "level_realized_row": 0.75},
        "n_predictions": 5, "n_rows": 4, "n_scorable": 4, "n_unscorable": 1,
        "range": {"coverage_daily": {"ci95": [0.013333333333333308,
                                              1.3199999999999998],
                                     "mean": 0.6666666666666666, "n_days": 3,
                                     "sd": 0.5773502691896257,
                                     "se": 0.3333333333333333},
                  "coverage_row": 0.75, "nominal": 0.8},
        "rows_by_day_avg": 1.3333333333333333,
        "sample_gate": {"effective_n": 3, "min_days": 120,
                        "sufficient": False},
        "unscorable_reasons": {"NO_BAR_TARGET": 1}}},
}


def test_invalidated_labels_do_not_change_any_number():
    """机械证明：`invalidated` 的两条纯文字标注**不参与任何计算**。

    判据 = 数字投影（摘掉全部文字叶子后的整份 summary）与改前金标准**逐位相等**，
    且 `invalidated` 块里新增的键全部是 `str` 值。整条链的最终证明是 F1 全量回放
    逐字段 diff（P24 验收 (a)）；本测试是它的离线替身，防止本地改坏而不自知。
    """
    g = summarize(_golden_rows(), from_date="2026-01-05", to_date="2026-01-08")
    assert _numbers_only(g) == _GOLDEN_NUMBERS

    inv = g["model_versions"]["pit-rw-v1.0.1"]["invalidated"]
    assert set(inv) == {"n_known", "rate", "n_undetermined", "note",
                        "pit_comparable"}
    for key in ("note", "pit_comparable"):
        assert isinstance(inv[key], str) and inv[key].strip()


def test_invalidated_subgroup_is_labelled_outcome_conditioned_and_untradable():
    """钉住口径：`invalidated` 子群的分组键是**次日收盘**，是「结果条件、不可交易」，
    **不得作为模型能力证据**。这段文字被删，等于把「事后纯度」当成「样本外能力」
    （ERROR_DIARY #15；诊断 `docs/diagnostics/2026-09-15-invalidated-subgroup.md`）。
    """
    g = summarize(_golden_rows(), from_date="2026-01-05", to_date="2026-01-08")
    inv = g["model_versions"]["pit-rw-v1.0.1"]["invalidated"]

    note = inv["note"]
    assert "次日收盘" in note          # 分组键 = 次日收盘（不是预测时点可知的量）
    assert "结果条件" in note          # 三要素 ①
    assert "不可交易" in note          # 三要素 ②
    assert "不得作为模型能力证据" in note   # 三要素 ③

    pit = inv["pit_comparable"]
    assert "变体假设" in pit           # 禁止作为变体假设来源（预注册 §5.1-2）
    assert "不分" in pit or "无" in pit

    md = render_markdown(g)
    for token in ("次日收盘", "结果条件", "不可交易", "不得作为模型能力证据"):
        assert token in md
    # 断言「内容」而非「非空」：不得把这条标注写成褒义/中性的能力描述
    assert "模型能力" in md
