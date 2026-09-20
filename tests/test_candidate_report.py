"""Task 14：候选池 Markdown 报告（设计文档 §7 步骤11）。"""

from stocklab.candidate.report import render_report

LOADED = {
    "snapshot": {"snapshot_id": 7, "asof": "2026-09-17", "run_kind": "weekly",
                 "params": {"seed_count": 20}},
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


def test_report_states_financial_factors_are_real_and_unvalidated():
    """Task 12 后：财务因子已接入真实数据，但须注明「横截面分位排序、样本内、未经验证」。

    原测试 test_report_states_financial_data_not_connected 断言「未接」或「留桩」，
    那是 Task 11 之前的实情；Task 12 替换了桩后继续断言同样的词是合法倒退守卫，
    故此测试做两件事：
      (a) 新真相：「横截面分位排序」「样本内」「未经验证」同时出现；
      (b) 旧假话：「未接」「留桩」**不**出现（防止代码回退）。
    这是合理反转：我们不是放松检查，而是把守卫从「必须声明未做」
    换成「必须声明已做但未验证」，两者都是如实声明的正向要求。
    """
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    # (a) 新真相：因子真实，但注明样本内、未经验证
    assert "横截面分位排序" in md
    assert "样本内" in md
    assert "未经验证" in md
    # (b) 旧假话必须消失
    assert "财务数据未接" not in md
    assert "留桩" not in md


def test_report_states_st_flag_is_not_pit():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert "非 PIT" in md or "非PIT" in md


def test_report_states_scope_is_20_seeds_not_full_market():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    assert "全标的扫描" in md


def test_report_metadata_line_shows_seed_count():
    md = render_report(asof="2026-09-17", run_kind="weekly", loaded=LOADED,
                       generated_at="2026-09-18T16:00:00+08:00")
    # 元信息行必须包含真实的 seed_count 值（20），而不是占位符 "?"
    seed_line = next(l for l in md.splitlines() if "种子范围" in l)
    assert "20" in seed_line


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
    # 空淘汰清单：淘汰清单段落内必须出现「无」
    rejects_section = md.split("## 淘汰清单")[1].split("##")[0]
    assert "无" in rejects_section
