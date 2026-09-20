"""Task 5：sandbox 接受注入的 replay；verdict 只看验证段。

注：brief 中 tests 原始代码使用 `replay=fake_replay` 直接传参；
controller 预检裁决收成一个 `SandboxDeps` dataclass，
因此测试用 `deps=sandbox.SandboxDeps(replay=...)` 形式传入。
"""

import pytest

from stocklab.plugin import sandbox, store
from stocklab.plugin.sandbox import SandboxDeps
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-20T16:00:00+08:00"
SCRIPT = ("def run(ctx):\n"
          "    return {'score': 50.0, 'pass_flag': True, 'reason': 'r',"
          " 'risk_list': []}\n")


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    for pid in ("0", "1", "2", "3", "4"):
        sid = store.insert_script(c, plugin_id=pid, version="1.0.0",
                                  source_text=SCRIPT, note=None, now=NOW)
        from stocklab.plugin import lifecycle
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
    yield c
    c.close()


def test_constant_renamed(conn):
    assert sandbox.MIN_VALID_PERIODS == 120
    assert not hasattr(sandbox, "MIN_VALID_DAYS"), \
        "旧名不保留别名：留着会让「120 日」与「120 周期」两种读法并存"


def test_no_replay_stays_fail_closed(conn):
    """不注入 replay 时保持现状：样本 0 → INCONCLUSIVE。"""
    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2026-01-01",
                            window_end="2026-09-18", now=NOW)
    assert v.verdict == "INCONCLUSIVE"
    assert v.n_periods == 0


def test_verdict_uses_validate_segment_only(conn):
    """验证段全正、训练段全负 → verdict 必须是 WIN（只看验证段）。"""
    def fake_replay(conn, *, candidate_script_id, baseline_script_id, pool,
                    window_start, window_end, **kw):
        return ([-0.01] * 80, [0.01] * 130)

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            deps=SandboxDeps(replay=fake_replay))
    assert v.verdict == "WIN"
    assert v.n_periods == 130, "n_periods 必须是验证段周期数"


def test_insufficient_validate_periods_is_inconclusive(conn):
    def fake_replay(conn, *, candidate_script_id, baseline_script_id, pool,
                    window_start, window_end, **kw):
        return ([0.01] * 80, [0.01] * 119)

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            deps=SandboxDeps(replay=fake_replay))
    assert v.verdict == "INCONCLUSIVE"
    assert "样本不足" in v.note


def test_first_version_still_inconclusive(conn):
    """首版无 baseline —— 即使注入了 replay 也不出结论。"""
    def boom(*a, **kw):
        raise AssertionError("首版不应调用 replay")

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=None,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            deps=SandboxDeps(replay=boom))
    assert v.verdict == "INCONCLUSIVE"


def test_train_segment_is_reported_in_detail(conn):
    """训练段的 Δ 均值要进 detail —— 过拟合标记要用它。"""
    def fake_replay(conn, *, candidate_script_id, baseline_script_id, pool,
                    window_start, window_end, **kw):
        return ([0.02] * 80, [0.001] * 130)

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            deps=SandboxDeps(replay=fake_replay))
    assert v.detail["train_mean"] == pytest.approx(0.02)
    assert v.detail["validate_mean"] == pytest.approx(0.001)
    assert v.detail["train_n"] == 80
    assert v.detail["validate_n"] == 130


# ---------- Task 6：train_gap 驱动 overfit ----------

def test_train_gap_threshold_constant():
    assert sandbox.OVERFIT_TRAIN_GAP == 0.005


def test_train_better_than_validate_is_suspected():
    """过拟合的形态：训练段比验证段好看。"""
    assert sandbox.overfit_flag(0.02, 0.001) == "suspected"


def test_validate_better_than_train_is_not_flagged():
    assert sandbox.overfit_flag(0.001, 0.02) is None


def test_small_gap_is_not_flagged():
    assert sandbox.overfit_flag(0.006, 0.005) is None      # gap=0.001 < 0.005


def test_gap_exactly_at_threshold_is_not_flagged():
    assert sandbox.overfit_flag(0.01, 0.005) is None       # gap=0.005


def test_missing_inputs_give_none():
    assert sandbox.overfit_flag(None, 0.01) is None
    assert sandbox.overfit_flag(0.01, None) is None
    assert sandbox.overfit_flag(None, None) is None


def test_old_ci_width_heuristic_is_gone():
    """旧的 CI 宽度启发式必须删除 —— 留着会让两种判据打架。"""
    import inspect
    src = inspect.getsource(sandbox)
    assert "ci_high - ci_low" not in src
    assert "2 * abs(delta)" not in src


def test_verdict_carries_overfit_flag(conn):
    def fake_replay(conn, *, candidate_script_id, baseline_script_id, pool,
                    window_start, window_end, **kw):
        return ([0.05] * 80, [0.001] * 130)

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            deps=SandboxDeps(replay=fake_replay))
    assert v.detail["overfit_flag"] == "suspected"


def test_no_baseline_detail_carries_overfit_flag_none(conn):
    """无 baseline 路径 detail 必须含 overfit_flag 键（值 None），
    使调用方能区分「没有标记」与「detail 完全缺失」。"""
    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=None,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW)
    assert "overfit_flag" in v.detail
    assert v.detail["overfit_flag"] is None
    assert v.detail.get("reason") == "no_baseline"


# ---------- Task 7：双超额与 script_id 留痕 ----------

def test_verdict_records_both_benchmark_excess(conn):
    """两版相对 index_300 的超额都要在 detail 里 —— 铁律要求「跑不赢就明说」。"""
    def fake_replay(conn, *, candidate_script_id, baseline_script_id, pool,
                    window_start, window_end, **kw):
        return ([0.01] * 80, [0.01] * 130)

    def fake_excess(conn, *, asof_dates, pool, plugin_overrides, **kw):
        return 0.05 if plugin_overrides else -0.05

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            replay=fake_replay, benchmark_excess=fake_excess)
    assert "candidate_excess_index300" in v.detail
    assert "baseline_excess_index300" in v.detail


def test_verdict_records_script_ids(conn):
    """回放时各插件的 script_id 要留痕 —— 否则同一份回放重跑对不上。"""
    def fake_replay(conn, *, candidate_script_id, baseline_script_id, pool,
                    window_start, window_end, **kw):
        return ([0.01] * 80, [0.01] * 130)

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            replay=fake_replay)
    assert v.detail["scripts"]["candidate"] == 1
    assert v.detail["scripts"]["baseline"] == 2
    assert v.detail["scripts"]["active"], "其余插件的 active 版本也要记"


def test_win_verdict_can_still_report_negative_excess(conn):
    """「新版跑赢旧版、但两者都跑输指数」必须能同时看见。"""
    def fake_replay(conn, *, candidate_script_id, baseline_script_id, pool,
                    window_start, window_end, **kw):
        return ([0.02] * 80, [0.01] * 130)

    def neg_excess(conn, *, asof_dates, pool, plugin_overrides, **kw):
        return -0.03

    v = sandbox.run_sandbox(conn, candidate_script_id=1, baseline_script_id=2,
                            pool="short", window_start="2015-01-01",
                            window_end="2026-09-18", now=NOW,
                            replay=fake_replay, benchmark_excess=neg_excess)
    assert v.verdict == "WIN"
    assert v.detail["candidate_excess_index300"] < 0
    assert v.detail["baseline_excess_index300"] < 0
