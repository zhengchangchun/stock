"""T2–T6：趋势状态标签的度量与判定（`stocklab/trend/evaluate.py`）。

要钉住的纪律（对应预注册 §1 的 F1/F2/F3）：

  1. **PIT 行构造**：`t` 日的状态只用 `≤ t` 的收盘；`t+N` 的收盘只用来做**标签**；
     序列末端凑不满 N 天的行**不构成样本**（不是记 0）。
  2. **行归段按实现日**：一行属于 `t+N` 所在的那一段 —— 否则 validate 的标签会
     吃到 test 段的价格（跨段标签泄漏）。
  3. **按日聚类**：同一天的所有标的进**同一簇**。判别式：把同一天的样本复制几份，
     日级均值不变 → 区间必须**一字不变**（按行重采样会让它变窄）。
  4. **F3 样本量门槛**：有效交易日 < 120 → `inconclusive`，且 test **不打开**。
  5. **达标规则**：判据 (a)/(b) 至少一条成立才允许打开 test；两条都不成立 →
     `falsified` 且 test 段**一点都没算过**（用 spy 当测谎仪）。
  6. **复现性**：同种子 → 报告字节相同。
"""

from __future__ import annotations

import json
import math

import pytest

from stocklab.trend import evaluate
from stocklab.trend.evaluate import (PREREG_CODES, evaluate_segment, judge,
                                     load_closes, make_rows, run_evaluation,
                                     write_report)
from stocklab.trend.state import STATE_DOWN, STATE_FLAT, STATE_UP, trend_state
from tests.test_predict_service import _to_date, _to_ord, bars, seed

CODE = "000333"
CODE2 = "600690"
INDEX = "sh000300"

N_BARS = 400
START = "2024-01-01"


def _days(n: int = N_BARS) -> list[str]:
    d0 = _to_ord(START)
    return [_to_date(d0 + i) for i in range(n)]


def _trend_env(tmp_path, *, n=N_BARS, base=10.0, name="trend.db", codes=None):
    """三个预注册标的、各自一条单调序列（UP 状态为主）+ 日历。"""
    codes = codes or (CODE, CODE2, INDEX)
    bars_by_code = {c: bars(code=c, n=n, start=START, base=base + 5 * i,
                            drift=0.002, wave=0.0)
                    for i, c in enumerate(codes)}
    return seed(tmp_path / name, bars_by_code, cal_dates=_days(n))


# ---------- 1. 行构造：PIT 与视界末端 ----------


def test_rows_start_where_ma60_is_ready_and_end_where_horizon_fits():
    series = [(d, 10.0 + 0.1 * i) for i, d in enumerate(_days(100))]
    rows = make_rows(CODE, series, horizon=5)
    assert rows[0].date == series[59][0]              # MA60 从第 60 根起可算
    assert rows[-1].date == series[100 - 5 - 1][0]    # t+5 必须存在
    assert len(rows) == 100 - 60 - 5 + 1


def test_row_state_and_hit_are_the_pure_functions():
    closes = [10.0 + 0.1 * i for i in range(100)]
    series = list(zip(_days(100), closes))
    rows = make_rows(CODE, series, horizon=5)
    assert len(rows) == 36
    for k, r in enumerate(rows):
        i = 59 + k                                  # 第一行对应第 59 根（MA60 就绪）
        assert r.close == closes[i] and r.date == series[i][0]
        assert r.state == trend_state(closes, i)
        assert r.hit == (1 if closes[i + 5] > closes[i] else 0)
        assert r.realization_date == series[i + 5][0]
        assert r.state_next == trend_state(closes, i + 5)
        assert r.same_state == (1 if r.state_next == r.state else 0)


def test_rows_carry_no_lookahead_in_the_state():
    """同一段历史 + 不同的未来 → 前面那些行的 `state` 必须逐字相同。"""
    base = [10.0 + 0.1 * i for i in range(100)]
    spiked = base[:80] + [999.0] * 20
    a = make_rows(CODE, list(zip(_days(100), base)), horizon=5)
    b = make_rows(CODE, list(zip(_days(100), spiked)), horizon=5)
    sa = {r.date: r.state for r in a if r.date <= _days(100)[79]}
    sb = {r.date: r.state for r in b if r.date <= _days(100)[79]}
    assert sa == sb and sa


# ---------- 2. 非复权口径必须显式 ----------


def test_load_closes_refuses_a_code_without_unadjusted_bars(tmp_path):
    conn = _trend_env(tmp_path, n=90)
    conn.execute("UPDATE bars_daily SET adj_mode='qfq' WHERE code=?", (CODE,))
    conn.commit()
    with pytest.raises(evaluate.AdjustModeError, match="不复权"):
        load_closes(conn, CODE)


def test_load_closes_returns_ascending_dates_and_closes(tmp_path):
    conn = _trend_env(tmp_path, n=90)
    series = load_closes(conn, CODE)
    assert [d for d, _ in series] == sorted(d for d, _ in series)
    assert len(series) == 90


# ---------- 3. 按日聚类（复制同一天不改变区间） ----------


def _pair_rows():
    """两标的、构造好的状态与命中（直接造行，绕开价格）。"""
    from stocklab.trend.evaluate import Row

    rows = []
    for k, day in enumerate(("d1", "d2", "d3", "d4")):
        for code, st, hit in ((CODE, STATE_UP, 1), (CODE2, STATE_UP, 0)):
            rows.append(Row(code=code, date=day, close=10.0, state=st,
                            realization_date=day, forward_return=0.01,
                            hit=hit, tie=False, state_next=st, same_state=1))
    return rows


def test_duplicating_a_day_does_not_change_the_day_clustered_interval():
    """按日聚类的判别式：同一天再塞 3 个标的（命中一模一样），区间必须不变。"""
    rows = _pair_rows()
    a = evaluate_segment(rows)
    extra = [evaluate.Row(code=f"x{i}", date=r.date, close=10.0, state=r.state,
                          realization_date=r.realization_date,
                          forward_return=0.0, hit=r.hit, tie=False,
                          state_next=r.state_next, same_state=1)
             for i, r in enumerate(rows)]
    b = evaluate_segment(rows + extra)
    assert a["M1"][STATE_UP]["delta"] == b["M1"][STATE_UP]["delta"]
    assert a["M1"][STATE_UP]["delta_ci95"] == b["M1"][STATE_UP]["delta_ci95"]
    assert a["M1"][STATE_UP]["n_days"] == b["M1"][STATE_UP]["n_days"] == 4


def test_one_day_is_one_cluster_not_one_row():
    rows = _pair_rows()
    m = evaluate_segment(rows)["M1"][STATE_UP]
    assert m["n_rows"] == 8 and m["n_days"] == 4


# ---------- 4. M1 的 Δ 是「同日配对」 ----------


def test_delta_is_conditional_minus_same_day_unconditional():
    from stocklab.trend.evaluate import Row

    # d1：UP 全中（1,1），另一标的 FLAT 全不中（0,0）→ 条件 1.0，基线 0.5，Δ=+0.5
    rows = [
        Row(CODE, "d1", 10.0, STATE_UP, "d1", 0.01, 1, False, STATE_UP, 1),
        Row(CODE2, "d1", 10.0, STATE_UP, "d1", 0.01, 1, False, STATE_UP, 1),
        Row(CODE, "d1", 10.0, STATE_FLAT, "d1", -0.01, 0, False, STATE_FLAT, 1),
        Row(CODE2, "d1", 10.0, STATE_FLAT, "d1", -0.01, 0, False, STATE_FLAT, 1),
        # d2：UP 全不中，其余全中 → 条件 0，基线 0.5，Δ=-0.5
        Row(CODE, "d2", 10.0, STATE_UP, "d2", -0.01, 0, False, STATE_UP, 1),
        Row(CODE2, "d2", 10.0, STATE_UP, "d2", -0.01, 0, False, STATE_UP, 1),
        Row(CODE, "d2", 10.0, STATE_FLAT, "d2", 0.01, 1, False, STATE_FLAT, 1),
        Row(CODE2, "d2", 10.0, STATE_FLAT, "d2", 0.01, 1, False, STATE_FLAT, 1),
    ]
    m = evaluate_segment(rows)["M1"][STATE_UP]
    assert m["hit_rate"] == pytest.approx(0.5)
    assert m["baseline_rate"] == pytest.approx(0.5)
    assert m["delta"] == pytest.approx(0.0)      # (+0.5 与 −0.5 的日均)
    assert m["n_days"] == 2


# ---------- 5. M2 延续率 ----------


def test_persistence_uses_state_at_t_plus_five_and_the_marginal_frequency():
    from stocklab.trend.evaluate import Row

    rows = [
        Row(CODE, "d1", 10.0, STATE_UP, "d1", 0.01, 1, False, STATE_UP, 1),
        Row(CODE2, "d1", 10.0, STATE_UP, "d1", 0.01, 1, False, STATE_DOWN, 0),
        Row(CODE, "d1", 10.0, STATE_FLAT, "d1", 0.01, 1, False, STATE_FLAT, 1),
        Row(CODE2, "d1", 10.0, STATE_FLAT, "d1", 0.01, 1, False, STATE_FLAT, 1),
    ]
    m = evaluate_segment(rows)["M2"][STATE_UP]
    assert m["persistence"] == pytest.approx(0.5)        # 2 个 UP 里 1 个延续
    assert m["baseline_freq"] == pytest.approx(0.5)      # 4 行里 2 行是 UP
    assert m["delta"] == pytest.approx(0.0)


# ---------- 6. 判定：F1/F2/F3 + 达标规则 ----------


def _m(delta, lo, hi):
    return {"delta": delta, "delta_ci95": [lo, hi]}


def _metrics(*, up, down, m2_up, m2_down):
    return {"M1": {STATE_UP: _m(*up), STATE_DOWN: _m(*down)},
            "M2": {STATE_UP: _m(*m2_up), STATE_DOWN: _m(*m2_down)}}


def test_insufficient_days_means_inconclusive_and_no_test():
    v = judge(validate=_metrics(up=(.1, .05, .15), down=(.1, .05, .15),
                                m2_up=(.4, .3, .5), m2_down=(.4, .3, .5)),
              days=119)
    assert v["status"] == "inconclusive" and v["requires_test"] is False


def test_criterion_a_requires_both_sides():
    """F1：「任一」侧的 CI 下界 ≤ 0 → 判据 (a) 不成立。"""
    v = judge(validate=_metrics(up=(.1, .05, .15), down=(.1, -.01, .2),
                                m2_up=(.0, -.1, .1), m2_down=(.0, -.1, .1)),
              days=300)
    assert v["criteria"]["a"] is False and v["criteria"]["b"] is False
    assert v["status"] == "falsified" and v["requires_test"] is False


def test_criterion_b_alone_is_enough_to_open_the_test():
    v = judge(validate=_metrics(up=(.0, -.05, .05), down=(.0, -.05, .05),
                                m2_up=(.4, .3, .5), m2_down=(.4, .3, .5)),
              days=300)
    assert v["criteria"] == {"a": False, "b": True}
    assert v["requires_test"] is True and v["status"] == "pending_test"


def test_test_must_replicate_the_criterion_that_held():
    v = judge(validate=_metrics(up=(.0, -.05, .05), down=(.0, -.05, .05),
                                m2_up=(.4, .3, .5), m2_down=(.4, .3, .5)),
              days=300,
              test=_metrics(up=(.0, -.05, .05), down=(.0, -.05, .05),
                            m2_up=(.01, -.02, .04), m2_down=(.4, .3, .5)),
              test_days=200)
    assert v["status"] == "falsified"       # (b) 在 test 上没复现（UP 侧下界 ≤ 0）
    assert v["test_evaluated"] is True


def test_both_segments_holding_is_a_win():
    good = _metrics(up=(.0, -.05, .05), down=(.0, -.05, .05),
                    m2_up=(.4, .3, .5), m2_down=(.4, .3, .5))
    v = judge(validate=good, days=300, test=good, test_days=200)
    assert v["status"] == "WIN" and v["test_evaluated"] is True


def test_test_segment_below_threshold_is_inconclusive():
    good = _metrics(up=(.0, -.05, .05), down=(.0, -.05, .05),
                    m2_up=(.4, .3, .5), m2_down=(.4, .3, .5))
    v = judge(validate=good, days=300, test=good, test_days=60)
    assert v["status"] == "inconclusive"


# ---------- 7. 端到端：test 段默认封存 ----------


def _force_judge(monkeypatch, *, requires_test: bool):
    """把判定换成「已知答案」，用来单独测**封存结构**（判定逻辑另有专测）。"""
    real = evaluate.judge

    def fake(**kw):
        v = real(**kw)
        return {**v, "requires_test": requires_test,
                "status": "pending_test" if requires_test else v["status"]}

    monkeypatch.setattr(evaluate, "judge", fake)


def test_run_evaluation_keeps_the_test_segment_sealed_when_validate_fails(
        tmp_path, monkeypatch):
    conn = _trend_env(tmp_path, n=N_BARS)
    _force_judge(monkeypatch, requires_test=False)
    calls: list[str] = []
    real = evaluate.evaluate_segment

    def spy(rows, **kw):
        calls.append(kw.get("label", "?"))
        return real(rows, **kw)

    monkeypatch.setattr(evaluate, "evaluate_segment", spy)
    rep = run_evaluation(conn, min_days=5)
    assert rep["test_evaluated"] is False
    assert "test" not in rep["splits"]
    assert "test" not in calls          # 封存段**一点都没算过**
    assert calls.count("validate") == 1
    assert rep["test_not_evaluated_reason"]


def test_test_segment_is_opened_exactly_once_when_validate_passes(tmp_path, monkeypatch):
    conn = _trend_env(tmp_path, n=N_BARS)
    _force_judge(monkeypatch, requires_test=True)
    calls: list[str] = []
    real = evaluate.evaluate_segment

    def spy(rows, **kw):
        calls.append(kw.get("label", "?"))
        return real(rows, **kw)

    monkeypatch.setattr(evaluate, "evaluate_segment", spy)
    rep = run_evaluation(conn, min_days=5)
    assert calls.count("test") == 1
    assert rep["test_evaluated"] is True
    assert "test" in rep["splits"]


def test_keep_test_sealed_overrides_even_a_passing_validate(tmp_path, monkeypatch):
    conn = _trend_env(tmp_path, n=N_BARS)
    _force_judge(monkeypatch, requires_test=True)
    rep = run_evaluation(conn, min_days=5, keep_test_sealed=True)
    assert rep["test_evaluated"] is False
    assert rep["verdict"]["status"] == "inconclusive"
    assert "封存" in " ".join(rep["verdict"]["reasons"])


def test_run_evaluation_writes_nothing_to_production_tables(tmp_path):
    conn = _trend_env(tmp_path, n=N_BARS)
    before = _table_counts(conn)
    run_evaluation(conn, min_days=5)
    assert _table_counts(conn) == before


def _table_counts(conn):
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: conn.execute(f"SELECT COUNT(*) c FROM {n}").fetchone()["c"]
            for n in names}


# ---------- 7b. 报告口径缺陷（P21）：计数语义与理由文字 ----------


def test_counts_test_rows_is_a_row_count_like_its_siblings(tmp_path, monkeypatch):
    """P21-D1：`counts.test_rows` 必须是 test 段的**样本行数**（与 `train_rows`/
    `validate_rows`/`rows_used` 同语义），**不是** test 段指标字典的键数。

    缺陷现场：写的是 `len(splits["test"])` → 报 7（M1/M2… 的键数），
    而 md §4 写的是 1962 行 —— 同一个量两个数。
    """
    conn = _trend_env(tmp_path, n=N_BARS)
    _force_judge(monkeypatch, requires_test=True)
    rep = run_evaluation(conn, min_days=5)
    c = rep["counts"]
    assert rep["test_evaluated"] is True
    assert c["test_rows"] == rep["splits"]["test"]["n_rows"]
    assert c["test_rows"] != len(rep["splits"]["test"])   # 不是「键数」
    assert c["test_rows"] != 7
    # 四个 *_rows 同一把尺子：加起来必须回到全样本
    assert (c["test_rows"] + c["train_rows"] + c["validate_rows"]) <= c["rows_used"]


def test_counts_test_rows_is_zero_when_the_test_segment_is_sealed(tmp_path, monkeypatch):
    """封存段**没算过** → 行数写 0，不许写成「段里有多少行」。"""
    conn = _trend_env(tmp_path, n=N_BARS)
    _force_judge(monkeypatch, requires_test=False)
    rep = run_evaluation(conn, min_days=5)
    assert rep["test_evaluated"] is False
    assert rep["counts"]["test_rows"] == 0


def _lower_bounds(seg, metric):
    return {s: seg[metric][s]["delta_ci95"][0] for s in (STATE_UP, STATE_DOWN)}


def test_criterion_reason_text_quotes_the_real_ci_lower_bounds():
    """P21-D2：理由文字里的下界值必须是报告里**实际**的 CI 下界（不得硬编码）。"""
    seg = _metrics(up=(.0, -.0681, .0014), down=(.0, -.0081, .0336),
                   m2_up=(.4, .2770, .3976), m2_down=(.4, .2809, .3567))
    v = judge(validate=seg, days=300)
    assert v["criteria"] == {"a": False, "b": True}
    for key, metric in (("a", "M1"), ("b", "M2")):
        text, lows = v["criteria_reasons"][key], _lower_bounds(seg, metric)
        for lo in lows.values():
            assert f"{lo:+.4f}" in text, (key, lo, text)


def test_criterion_reason_never_contradicts_the_verdict():
    """判据不成立时，文字里不许出现「故 (a) 成立」这种反向措辞。"""
    seg = _metrics(up=(.0, -.0681, .0014), down=(.0, -.0081, .0336),
                   m2_up=(.4, .2770, .3976), m2_down=(.4, .2809, .3567))
    v = judge(validate=seg, days=300)
    a_text, b_text = v["criteria_reasons"]["a"], v["criteria_reasons"]["b"]
    assert "故 (a) 不成立" in a_text and "故 (a) 成立" not in a_text
    assert "故 (b) 成立" in b_text and "故 (b) 不成立" not in b_text
    # 成立/不成立 的开头结论也必须与 criteria 一致
    assert a_text.startswith("判据 (a) 不成立") and b_text.startswith("判据 (b) 成立")


def test_criterion_reason_says_so_when_a_side_has_no_interval():
    """算不出区间的侧不许被说成「均 > 0」（CI=None → 判不成立）。"""
    v = judge(validate={"M1": {STATE_UP: {"delta": .1, "delta_ci95": None},
                               STATE_DOWN: _m(.1, .05, .15)},
                        "M2": {STATE_UP: _m(.4, .3, .5),
                               STATE_DOWN: _m(.4, .3, .5)}},
              days=300)
    text = v["criteria_reasons"]["a"]
    assert v["criteria"]["a"] is False
    assert "均 > 0，故 (a) 成立" not in text
    assert "算不出 CI" in text and "故 (a) 不成立" in text


def test_markdown_criterion_lines_come_from_the_generated_reasons(tmp_path, monkeypatch):
    """md §3 的两行判据必须**逐字**等于 verdict 里生成的文字（render 不许另写一套）。"""
    conn = _trend_env(tmp_path, n=N_BARS)
    rep = run_evaluation(conn, min_days=5)
    md = evaluate.render_markdown(rep)
    for key in ("a", "b"):
        assert f"- {rep['verdict']['criteria_reasons'][key]}" in md


def test_selfcheck_explains_the_two_row_sets_from_computed_numbers(tmp_path):
    """P21-D3：9403 与 13972 各自是哪套行集、差从哪来 —— 由代码算出并写明不得互引。"""
    conn = _trend_env(tmp_path, n=N_BARS)
    rep = run_evaluation(conn, min_days=5)
    rs = rep["selfcheck"]["row_sets"]
    used = rep["counts"]["rows_used"]
    outside = rep["data_snapshot"]["unmapped_rows_outside_axis"]
    assert rs["this_report_full_sample"]["n_rows"] == used
    assert rs["prereg_explore"]["n_rows"] == evaluate.PREREG_EXPLORE["n_rows"]
    assert rs["difference"]["calendar_outside_rows"] == outside
    assert rs["difference"]["rows_total"] == used + outside
    assert rs["difference"]["this_minus_prereg"] == used - rs["prereg_explore"]["n_rows"]
    # 差值文字里出现的数字必须是上面算出来的，不是手写的
    why = rs["difference"]["why"]
    for n in (used, outside, used + outside, rs["prereg_explore"]["n_rows"]):
        assert str(n) in why, (n, why)
    assert "不得互引" in rs["no_cross_citation"]


def test_markdown_section6_states_both_row_sets_and_forbids_cross_citation(tmp_path):
    conn = _trend_env(tmp_path, n=N_BARS)
    rep = run_evaluation(conn, min_days=5)
    md = evaluate.render_markdown(rep)
    sec6 = md.split("## 6.")[1].split("## 7.")[0]
    assert "不得互引" in sec6
    assert str(rep["counts"]["rows_used"]) in sec6
    assert str(evaluate.PREREG_EXPLORE["n_rows"]) in sec6
    assert str(rep["data_snapshot"]["unmapped_rows_outside_axis"]) in sec6


def test_reproduction_command_carries_the_effective_window(tmp_path):
    """P21 同型：报告的「复现」命令必须带上**实际生效**的窗口参数。

    默认窗口 = 当下全轴 —— 库里新增一根 bar 就会「复现」出另一套数字。
    """
    conn = _trend_env(tmp_path, n=N_BARS)
    rep = run_evaluation(conn, min_days=5, to_date="2025-01-10")
    md = evaluate.render_markdown(rep)
    assert "--to 2025-01-10 " in md and "stocklab trend evaluate --to 2025-01-10 " in md
    # 没传的窗口参数不许凭空出现
    assert "--from" not in md.split("## 9.")[1]
    rep2 = run_evaluation(conn, min_days=5)
    assert "--to" not in evaluate.render_markdown(rep2).split("## 9.")[1]


# ---------- 8. 复现性与报告内容 ----------


def test_same_seed_produces_a_byte_identical_report(tmp_path):
    conn = _trend_env(tmp_path, n=N_BARS)
    a = run_evaluation(conn, min_days=5)
    b = run_evaluation(conn, min_days=5)
    wa = write_report(a, tmp_path / "a.md")
    wb = write_report(b, tmp_path / "b.md")
    assert wa["sha256_md"] == wb["sha256_md"]
    assert wa["sha256_json"] == wb["sha256_json"]


def test_report_records_seed_snapshot_hash_and_boundaries(tmp_path):
    conn = _trend_env(tmp_path, n=N_BARS)
    rep = run_evaluation(conn, min_days=5)
    assert rep["bootstrap"]["n_boot"] == evaluate.BOOTSTRAP_N
    assert rep["bootstrap"]["seed"] == evaluate.BOOTSTRAP_SEED
    assert rep["bootstrap"]["method"] == "day_clustered_percentile_bootstrap"
    assert rep["prereg_doc"] == "docs/experiments/2026-09-15-trend-state-hit-rate.md"
    assert rep["universe"] == list(PREREG_CODES)
    assert len(rep["data_snapshot"]["bars_sha256"]) == 64
    for name in ("train", "validate", "test"):
        b = rep["split_boundaries"][name]
        assert b["first_date"] <= b["last_date"] and b["n_days"] > 0
    assert rep["horizon"] == 5 and rep["ma"] == {"short": 20, "long": 60}


def test_report_markdown_states_the_sealed_test_and_the_criteria(tmp_path):
    conn = _trend_env(tmp_path, n=N_BARS)
    rep = run_evaluation(conn, min_days=5)
    written = write_report(rep, tmp_path / "r.md")
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "test_evaluated" in md and "判据" in md
    payload = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert payload["verdict"]["status"] == rep["verdict"]["status"]
    assert written["sha256_md"] == rep["report_sha256"]


def test_robustness_appendix_is_separate_from_the_verdict(tmp_path):
    conn = _trend_env(tmp_path, n=N_BARS)
    rep = run_evaluation(conn, min_days=5)
    assert rep["verdict"]["criteria"] == evaluate.criteria_of(rep["splits"]["validate"])
    variants = {v["label"] for v in rep["robustness"]}
    assert {"N=1", "N=10", "N=20", "MA(10,30)", "MA(5,20)"} <= variants
    for v in rep["robustness"]:
        assert "改判据" in v["note"]


# ---------- 9. 度量层的两处交叉核对 ----------


def test_boot_ci_matches_the_framework_estimator_on_the_same_daily_values():
    """本模块的日聚类 bootstrap 必须与 `metrics.bootstrap_daily_ci` 同一个估计量。

    构造：每天一个标的，「变体 − 基线」= 该天的 Δ → 配对日差序列与
    `_delta_block` 的日级序列逐点相同，两条路径给出的区间必须逐位相等
    （同重采样次数、同种子、同分位下标约定）。两份实现不等 = 有一份悄悄改了口径。
    """
    from stocklab.experiments import metrics
    from stocklab.trend.evaluate import _boot_ci

    # 日期键**零填充**：`metrics` 内部按 `sorted(by_day)`（字典序）取日序列，
    # 不填充时 "d10" 会排在 "d2" 前面 —— 那样比的就是两个不同的日序列，
    # 不是同一估计量的两个实现。
    daily = [0.01 * (i % 7) - 0.02 for i in range(40)]
    key = lambda i: f"d{i:03d}"
    base = [{"target_date": key(i), "code": CODE, "scorable": True,
             "hit_direction": 0.0} for i in range(len(daily))]
    var = [{"target_date": key(i), "code": CODE, "scorable": True,
            "hit_direction": v} for i, v in enumerate(daily)]
    ref = metrics.bootstrap_daily_ci(base, var, "direction")
    assert ref["mean"] == pytest.approx(sum(daily) / len(daily))
    assert _boot_ci(daily) == ref["ci95"]


def test_rows_outside_the_trading_axis_are_counted_not_silently_dropped(tmp_path):
    """日历比行情短 → 落在轴外的行必须**显式计数**（本仓库 600690 真有这种行）。"""
    from tests.test_predict_service import _to_date, _to_ord

    codes = (CODE, CODE2, INDEX)
    bars_by_code = {c: bars(code=c, n=N_BARS, start=START, base=10.0 + 5 * i,
                            drift=0.002) for i, c in enumerate(codes)}
    short_cal = [_to_date(_to_ord(START) + i) for i in range(N_BARS - 40)]
    conn = seed(tmp_path / "short.db", bars_by_code, cal_dates=short_cal)
    rep = run_evaluation(conn, min_days=5)
    assert rep["data_snapshot"]["unmapped_rows_outside_axis"] > 0
    assert rep["counts"]["rows_used"] < rep["counts"]["rows_total"]


def test_adding_a_non_prereg_code_is_refused(tmp_path):
    """扩大样本量 = 改设计：预注册三标的之外一律拒收（含 P17 新增的 ETF）。"""
    conn = _trend_env(tmp_path, n=N_BARS)
    with pytest.raises(evaluate.PreregViolation, match="预注册"):
        run_evaluation(conn, codes=("000333", "600690", "sh000300", "510300"))
    with pytest.raises(evaluate.PreregViolation, match="预注册"):
        run_evaluation(conn, codes=("510300",))
    # 预注册标的的**子集**是允许的（样本更少，不是改设计）
    rep = run_evaluation(conn, codes=("000333",), min_days=5)
    assert rep["universe"] == ["000333"]
