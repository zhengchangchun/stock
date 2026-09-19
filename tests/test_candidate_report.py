"""Task 14：候选池 Markdown 报告（设计文档 §7 步骤11）。"""

from stocklab.candidate.report import render_report

LOADED = {
    "snapshot": {"snapshot_id": 7, "asof": "2026-09-17", "run_kind": "weekly",
                 "params": {"seed": 20}},
    "members": [
        {"code": "000333", "pool": "short", "raw_score": 80.0,
         "adj_score": 75.0, "reason": "量价好", "risk_json": '["高波动"]',
         "status": "观察中"},
        {"code": "600519", "pool": "mid", "raw_score": 70.0,
         "adj_score": 68.0, "reason": "景气稳", "risk_json": "[]",
         "status": "观察中"},
    ],
    "rejects": [
        {"code": "600690", "stage": "pre_screen", "reason": "st_flag",
         "plugin_id": None},
    ],
}


def test_report_has_all_three_pool_sections():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert "## 短期池" in md
    assert "## 中期池" in md
    assert "## 长期池" in md


def test_report_lists_members_under_correct_pool():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    short = md.split("## 短期池")[1].split("## 中期池")[0]
    assert "000333" in short
    assert "600519" not in short


def test_report_shows_rejects_with_reason():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert "600690" in md
    assert "st_flag" in md


def test_report_states_financial_data_not_connected():
    """本轮的诚实声明：财务因子是桩，必须写在报告里（设计文档 §13.3）。"""
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert "财务" in md
    assert "未接" in md or "留桩" in md


def test_report_states_st_flag_is_not_pit():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert "非 PIT" in md or "非PIT" in md


def test_report_states_scope_is_20_seeds_not_full_market():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert "全标的扫描" in md


def test_report_is_deterministic():
    a = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                      generated_at="2026-09-18T16:00:00+08:00")
    b = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                      generated_at="2026-09-18T16:00:00+08:00")
    assert a == b


def test_report_handles_empty_pool():
    loaded = {"snapshot": {"snapshot_id": 1, "asof": "2026-09-17",
                           "run_kind": "weekly", "params": {}},
              "members": [], "rejects": []}
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=loaded,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert md.count("（空）") == 3
