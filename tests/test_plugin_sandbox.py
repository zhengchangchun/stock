"""Task 16：沙盒对比回测（设计文档 §8）。「口径守铁律」的落点。"""

import pytest

from stocklab.plugin import sandbox

from tests.test_candidate_run import _seed_db          # 复用同一套夹具


@pytest.fixture
def conn(tmp_db):
    c = _seed_db(tmp_db)
    yield c
    c.close()


def test_first_version_has_no_baseline_so_inconclusive(conn):
    """首版没有 baseline → 一律 INCONCLUSIVE（设计文档 §8.5）。"""
    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=None,
                            pool="short", window_start="2025-06-01",
                            window_end="2026-09-17", now="2026-09-18T16:00:00+08:00")
    assert v.verdict == "INCONCLUSIVE"
    assert v.n_days == 0
    assert v.baseline_script_id is None
    assert "baseline" in v.note or "首版" in v.note


def test_short_window_below_120_periods_is_inconclusive(conn):
    """不足 120 个有效调仓周期 → 不出结论（CLAUDE.md 度量纪律③）。

    注意：本测试通过是因为 deps=None（无注入回放）时 fail-closed 保持
    空序列（n_days=0），而非真的根据 window_start/window_end 日期范围计算了
    周期数。周期数门槛本身由 test_sandbox_replay_wiring.py 覆盖。
    """
    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=1,
                            pool="short", window_start="2026-08-01",
                            window_end="2026-09-17", now="2026-09-18T16:00:00+08:00")
    assert v.verdict == "INCONCLUSIVE"
    assert v.n_days < sandbox.MIN_VALID_PERIODS
    assert "样本不足" in v.note


def test_identical_versions_produce_lose_or_inconclusive(conn):
    """同一份脚本跟自己比，Δ 必然为 0 —— 绝不允许判 WIN。"""
    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=1,
                            pool="short", window_start="2024-01-01",
                            window_end="2026-09-17", now="2026-09-18T16:00:00+08:00")
    assert v.verdict in ("LOSE", "INCONCLUSIVE")
    if v.verdict == "LOSE":
        assert v.delta == pytest.approx(0.0, abs=1e-9)


def test_rebalance_days_match_doc():
    assert sandbox.REBALANCE_DAYS == {"short": 5, "mid": 20, "long": 60}


def test_min_valid_periods_is_120():
    assert sandbox.MIN_VALID_PERIODS == 120


def test_verdict_is_json_serializable(conn):
    import json
    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=None,
                            pool="short", window_start="2025-06-01",
                            window_end="2026-09-17", now="2026-09-18T16:00:00+08:00")
    json.dumps(v.as_metrics())


def test_unknown_pool_raises(conn):
    with pytest.raises(ValueError):
        sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=None,
                            pool="nope", window_start="2025-06-01",
                            window_end="2026-09-17", now="2026-09-18T16:00:00+08:00")


# ---------------------------------------------------------------------------
# overfit_flag 启发式直接单元测试
# 判据：ci_low < 0 < ci_high 且 (ci_high - ci_low) > 2 * |delta|
# ---------------------------------------------------------------------------

def test_overfit_flag_returns_suspected_when_heuristic_fires():
    """CI 跨 0 且区间宽度大于 |delta| 的 2 倍 → 'suspected'。"""
    # delta=0.01, ci_low=-0.05, ci_high=0.07
    # 宽度 = 0.12, 2*|delta| = 0.02  →  0.12 > 0.02  → 触发
    result = sandbox.overfit_flag(delta=0.01, ci_low=-0.05, ci_high=0.07)
    assert result == "suspected"


def test_overfit_flag_returns_none_when_heuristic_does_not_fire():
    """CI 跨 0 但区间宽度不超过 2 倍 |delta| → None（无标记）。"""
    # delta=0.04, ci_low=-0.01, ci_high=0.06
    # 宽度 = 0.07, 2*|delta| = 0.08  →  0.07 < 0.08  → 不触发
    result = sandbox.overfit_flag(delta=0.04, ci_low=-0.01, ci_high=0.06)
    assert result is None


def test_overfit_flag_returns_none_when_inputs_are_none():
    """任一输入为 None → None（无证据，无标记）。"""
    assert sandbox.overfit_flag(None, None, None) is None
    assert sandbox.overfit_flag(0.01, None, 0.05) is None


def test_overfit_flag_returns_none_when_ci_does_not_cross_zero():
    """CI 不跨 0（全正）→ None，哪怕区间宽。"""
    # ci_low > 0：区间不跨零
    result = sandbox.overfit_flag(delta=0.01, ci_low=0.005, ci_high=0.10)
    assert result is None
