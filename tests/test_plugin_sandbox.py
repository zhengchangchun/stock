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
    assert v.n_periods == 0
    assert v.baseline_script_id is None
    assert "baseline" in v.note or "首版" in v.note


def test_short_window_below_120_periods_is_inconclusive(conn):
    """不足 120 个有效调仓周期 → 不出结论（CLAUDE.md 度量纪律③）。

    注意：本测试通过是因为 deps=None（无注入回放）时 fail-closed 保持
    空序列（n_periods=0），而非真的根据 window_start/window_end 日期范围计算了
    周期数。周期数门槛本身由 test_sandbox_replay_wiring.py 覆盖。
    """
    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=1,
                            pool="short", window_start="2026-08-01",
                            window_end="2026-09-17", now="2026-09-18T16:00:00+08:00")
    assert v.verdict == "INCONCLUSIVE"
    assert v.n_periods < sandbox.MIN_VALID_PERIODS
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
# overfit_flag 单元测试
# 判据（Task 6）：train_gap = train_mean − validate_mean > OVERFIT_TRAIN_GAP
# ---------------------------------------------------------------------------

def test_overfit_flag_returns_suspected_when_heuristic_fires():
    """训练段比验证段好看超过阈值 → 'suspected'。"""
    # train_mean=0.02, validate_mean=0.001 → gap=0.019 > 0.005 → 触发
    result = sandbox.overfit_flag(train_mean=0.02, validate_mean=0.001)
    assert result == "suspected"


def test_overfit_flag_returns_none_when_heuristic_does_not_fire():
    """gap 不超阈值 → None（无标记）。"""
    # train_mean=0.006, validate_mean=0.005 → gap=0.001 < 0.005 → 不触发
    result = sandbox.overfit_flag(train_mean=0.006, validate_mean=0.005)
    assert result is None


def test_overfit_flag_returns_none_when_inputs_are_none():
    """任一输入为 None → None（无证据，无标记）。"""
    assert sandbox.overfit_flag(None, None) is None
    assert sandbox.overfit_flag(0.01, None) is None


def test_overfit_flag_returns_none_when_ci_does_not_cross_zero():
    """验证段优于训练段（validate > train）→ None，不是过拟合的形态。"""
    # validate_mean > train_mean → gap < 0 → 不触发
    result = sandbox.overfit_flag(train_mean=0.001, validate_mean=0.02)
    assert result is None
