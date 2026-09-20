"""Task 18：6 个内置插桩脚本 —— 必须能过预检、契约，且诚实标注留桩。"""

import pytest

from stocklab.candidate.builtin import BUILTIN_PLUGINS
from stocklab.plugin import contract, guard, runtime


def _make_bars(n):
    """生成 n 根 K 线，收盘价微幅波动、成交量 > 0，避免除零。"""
    bars = []
    close = 10.0
    for i in range(n):
        close = close * (1 + (0.005 if i % 3 != 0 else -0.003))
        bars.append({
            "date": f"2026-{(i // 20 + 1):02d}-{(i % 20 + 1):02d}",
            "open": close * 0.99, "high": close * 1.01,
            "low": close * 0.98, "close": close,
            "volume": 1000 + i * 10,
            "amount": None, "turnover": None,
        })
    return bars


def _short_ctx():
    return {
        "code": "000333", "name": "美的集团", "asof": "2026-09-17",
        "pool": "mid", "asset_type": "stock", "board": "main",
        "bars": _make_bars(28),
        "raw_score": 60.0, "risk_list": ["高波动"],
    }


def _long_ctx():
    return {
        "code": "000333", "name": "美的集团", "asof": "2026-09-17",
        "pool": "long", "asset_type": "stock", "board": "main",
        "bars": _make_bars(130),
        "raw_score": 60.0, "risk_list": [],
    }


def test_all_six_present():
    assert sorted(BUILTIN_PLUGINS) == ["0", "1", "2", "3", "4", "5"]


@pytest.mark.parametrize("plugin_id", ["0", "1", "2", "3", "4", "5"])
def test_passes_guard(plugin_id):
    guard.check_source(BUILTIN_PLUGINS[plugin_id])


@pytest.mark.parametrize("plugin_id", ["0", "1", "2", "3", "4", "5"])
def test_runs_and_satisfies_contract(plugin_id):
    fn = runtime.load_script(BUILTIN_PLUGINS[plugin_id], plugin_id=plugin_id)
    result = fn(_short_ctx())
    assert isinstance(result, dict)


def test_plugin2_short_circuit_vs_full_body():
    """插桩2 无财报（period=None）走 guard 分支，有财报走真实打分体 —— reason 必须不同。

    Task 11 之前的占位实现（bars 长度守卫）被真实因子实现替换后，
    短路条件改为 features["period"] 是否存在，而非 bars 长度。
    本测试更新为用 features 触发两条路径，验证分支覆盖仍有效。
    """
    fn = runtime.load_script(BUILTIN_PLUGINS["2"], plugin_id="2")
    # 无财报：period=None → 短路
    no_feat_ctx = {**_short_ctx(), "features": {"period": None}}
    # 有财报：构造完整 features
    full_feats = {}
    for k in ("roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"):
        full_feats[k] = 0.5
        full_feats[f"{k}_pct"] = 0.5
        full_feats[f"{k}_n"] = 10
    full_feats.update({"period": "2026Q2", "asof": "2026-09-17",
                       "period_mixed": False, "na_reasons": [],
                       "dupont": None})
    full_feat_ctx = {**_long_ctx(), "features": full_feats}
    short_result = fn(no_feat_ctx)
    long_result = fn(full_feat_ctx)
    assert isinstance(short_result, dict)
    assert isinstance(long_result, dict)
    # 短路 reason ≠ 打分体 reason —— 两条分支都被覆盖
    assert short_result.get("reason") != long_result.get("reason"), (
        f"plugin2 no-feat reason={short_result.get('reason')!r} "
        f"full-feat reason={long_result.get('reason')!r}"
    )


def test_plugin3_short_circuit_vs_full_body():
    """插桩3 无财报（period=None）走 guard 分支，有财报走真实打分体 —— reason 必须不同。

    同 test_plugin2_short_circuit_vs_full_body 的升级说明：
    Task 11 将短路条件从 bars 长度改为 features["period"] 是否存在。
    """
    fn = runtime.load_script(BUILTIN_PLUGINS["3"], plugin_id="3")
    no_feat_ctx = {**_short_ctx(), "features": {"period": None}}
    full_feats = {}
    for k in ("roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"):
        full_feats[k] = 0.5
        full_feats[f"{k}_pct"] = 0.5
        full_feats[f"{k}_n"] = 10
    full_feats.update({"period": "2026Q2", "asof": "2026-09-17",
                       "period_mixed": False, "na_reasons": [],
                       "dupont": None})
    full_feat_ctx = {**_long_ctx(), "features": full_feats}
    short_result = fn(no_feat_ctx)
    long_result = fn(full_feat_ctx)
    assert isinstance(short_result, dict)
    assert isinstance(long_result, dict)
    assert short_result.get("reason") != long_result.get("reason"), (
        f"plugin3 no-feat reason={short_result.get('reason')!r} "
        f"full-feat reason={long_result.get('reason')!r}"
    )


def test_score_plugins_declare_financial_data_is_stubbed():
    """中期/长期池的脚本必须包含「财务」相关声明及诚实的未验证标注。

    Task 11 之前（占位阶段）：声明「财务因子未接」。
    Task 11 之后（真实因子阶段）：「财务因子未接」声明已删除，
    改为「未经验证」——表明分数是样本内横截面排序，尚未经过 walk-forward 验证。
    本测试随实现升级，验证新的诚实标注已到位。
    """
    for plugin_id in ("2", "3"):
        blob = BUILTIN_PLUGINS[plugin_id]
        assert "财务" in blob and "未经验证" in blob


def test_industry_screen_is_per_industry():
    """插桩0 必须按行业分支 —— 否则「行业特殊排雷」名不副实。"""
    assert "sector" in BUILTIN_PLUGINS["0"] or "行业" in BUILTIN_PLUGINS["0"]


def test_plugin0_ctx_sector_wins_over_name_keyword():
    """ctx["sector"] 与 name-keyword 冲突时，ctx 优先（可证伪，基线 ab1f30e）。

    Task 10 之前的基线（ab1f30e）：插桩0 没有 sector 处理，只用 name 猜行业。
    对 name="招商银行", sector="其他金融Ⅱ" 的 ctx：
      - ab1f30e（旧）：_guess_sector("招商银行") 命中 SECTOR_KEYWORDS["银行"]
                        → risk_note=['银行业：高杠杆经营，通用排雷指标不完全适用']（含"银行"）
      - 当前（新）：   优先读 ctx["sector"]="其他金融Ⅱ"，不触发银行 note
                        → risk_note=[] 或不含"银行"的 note

    实证可证伪：ab1f30e 跑本断言 `not any("银行" in r …)` 会红（实测）。
    上述 ab1f30e 输出由 `git show ab1f30e:…/p0_industry.py` + load_script 实测得到。
    """
    fn = runtime.load_script(BUILTIN_PLUGINS["0"], plugin_id="0")
    # name="招商银行" → old code would match SECTOR_KEYWORDS["银行"] → emit "银行"
    # sector="其他金融Ⅱ"  → not in FINANCIAL_KEYWORDS, so no financial note at all
    ctx = {"code": "600036", "name": "招商银行", "asof": "2026-09-17",
           "pool": "short", "sector": "其他金融Ⅱ", "asset_type": "stock",
           "board": "main", "bars": [], "features": {}, "risk_list": []}
    out = fn(ctx)
    assert out["pass_flag"] is True
    # The new code uses ctx sector "其他金融Ⅱ" — not in FINANCIAL_KEYWORDS,
    # so no financial note at all; old code would have emitted a "银行"-containing note.
    assert not any("银行" in r for r in out["risk_note"]), (
        f"Expected no '银行' note (ctx sector wins), got: {out['risk_note']}"
    )
    # No "name-fallback" note either — sector came from ctx
    assert not any("sector 字段缺失" in r for r in out["risk_note"]), (
        f"Should not emit name-fallback note when ctx sector is present: {out['risk_note']}"
    )


def test_plugin0_falls_back_to_name_when_sector_missing():
    """sector=None 时从 name 推断行业，标注 note，pass_flag 仍为 True。"""
    fn = runtime.load_script(BUILTIN_PLUGINS["0"], plugin_id="0")
    ctx = {"code": "000333", "name": "美的集团", "asof": "2026-09-17",
           "pool": "short", "sector": None, "asset_type": "stock",
           "board": "main", "bars": [], "features": {}, "risk_list": []}
    out = fn(ctx)
    assert out["pass_flag"] is True
    # "集团" matches SECTOR_KEYWORDS["家电"] → name-fallback note fires
    assert any("sector 字段缺失" in r for r in out["risk_note"]), (
        f"Expected name-fallback note, got: {out['risk_note']}"
    )


def test_plugin0_unknown_sector_emits_only_one_note():
    """sector=None かつ name にもキーワードなし → note は 1 つだけ（二重発火なし）。

    Task 10 之前基线（ab1f30e）：sector=None 且 name 无 keyword → 两个 if 都走旧分支，
    仅 emit "行业未能从名称推断…"；不含 "行业未能判定" → assert any("行业未能判定" …) 红。
    当前：elif 结构确保只发一条 "行业未能判定" note，不重复发 "sector 字段缺失"。

    if→elif 改动（round 1）覆盖此路径；ab1f30e 对本测试的两个 assert 均会红（实测）。
    """
    fn = runtime.load_script(BUILTIN_PLUGINS["0"], plugin_id="0")
    ctx = {"code": "888888", "name": "某未知标的XYZ", "asof": "2026-09-17",
           "pool": "short", "sector": None, "asset_type": "stock",
           "board": "main", "bars": [], "features": {}, "risk_list": []}
    out = fn(ctx)
    assert out["pass_flag"] is True
    assert any("行业未能判定" in r for r in out["risk_note"]), (
        f"Expected undetermined-sector note, got: {out['risk_note']}"
    )
    assert not any("sector 字段缺失" in r for r in out["risk_note"]), (
        f"Must not emit name-fallback note when no sector was inferred: {out['risk_note']}"
    )


# ---------------------------------------------------------------------------
# Task 11: 插桩2/3 换成真实因子
# ---------------------------------------------------------------------------

def _feat(**over):
    base = {"period": "2026Q2", "asof": "2026-09-17", "period_mixed": False,
            "na_reasons": []}
    for k in ("roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"):
        base[k] = 0.5
        base[f"{k}_pct"] = 0.5
        base[f"{k}_n"] = 17
    # dupont is carried in the runtime features dict but neither p2 nor p3
    # currently scores on it — present for completeness, not part of scoring contract.
    base["dupont"] = {"net_margin": 0.1, "asset_turnover": 0.8,
                      "equity_multiplier": 2.0}
    base.update(over)
    return base


def _ctx(feats, pool="mid"):
    return {"code": "000333", "name": "美的集团", "asof": "2026-09-17",
            "pool": pool, "sector": "白色家电", "asset_type": "stock",
            "board": "main", "bars": [], "features": feats, "risk_list": []}


@pytest.mark.parametrize("pid,pool", [("2", "mid"), ("3", "long")])
def test_score_rises_with_percentiles(pid, pool):
    fn = runtime.load_script(BUILTIN_PLUGINS[pid], plugin_id=pid)
    low = fn(_ctx(_feat(roe_pct=0.1, gross_margin_pct=0.1, gm_yoy_pp_pct=0.1,
                        inv_days_pct=0.1, fcf_margin_pct=0.1), pool))
    high = fn(_ctx(_feat(roe_pct=0.9, gross_margin_pct=0.9, gm_yoy_pp_pct=0.9,
                         inv_days_pct=0.9, fcf_margin_pct=0.9), pool))
    assert high["score"] > low["score"]


@pytest.mark.parametrize("pid,pool", [("2", "mid"), ("3", "long")])
def test_financial_stock_with_na_still_scores(pid, pool):
    """金融股毛利率/存货周转 NA —— 按可用因子加权，仍能出分，不判死。"""
    feats = _feat(gross_margin=None, gross_margin_pct=None, gross_margin_n=0,
                  inv_days=None, inv_days_pct=None, inv_days_n=0,
                  na_reasons=["gross_margin: operate_cost is NULL",
                              "inv_days: inventory is NULL"])
    fn = runtime.load_script(BUILTIN_PLUGINS[pid], plugin_id=pid)
    out = fn(_ctx(feats, pool))
    assert out["pass_flag"] is True
    assert out["score"] > 0.0


@pytest.mark.parametrize("pid,pool", [("2", "mid"), ("3", "long")])
def test_no_usable_factor_marks_fail(pid, pool):
    """全部 *_pct 为 None 时，可用因子计数分支触发，pass_flag=False，reason 提名缺失因子。

    注意：period 故意保留（"2026Q2"），以确保执行绕过 period 早返回守卫，
    真正落到 usable < MIN_FACTORS 的判据分支。period=None 分支由下方独立测试覆盖。
    """
    feats = _feat(na_reasons=["全部缺失"])
    for k in ("roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"):
        feats[k] = None
        feats[f"{k}_pct"] = None
        feats[f"{k}_n"] = 0
    feats["dupont"] = None
    # period intentionally kept set — we want the factor-count guard, not the period guard
    fn = runtime.load_script(BUILTIN_PLUGINS[pid], plugin_id=pid)
    out = fn(_ctx(feats, pool))
    assert out["pass_flag"] is False
    assert "可用财务因子不足" in out["reason"]


@pytest.mark.parametrize("pid,pool", [("2", "mid"), ("3", "long")])
def test_period_none_marks_fail(pid, pool):
    """period=None 时 period 早返回守卫触发，pass_flag=False，reason 含"期数"。"""
    feats = _feat()
    feats["period"] = None
    fn = runtime.load_script(BUILTIN_PLUGINS[pid], plugin_id=pid)
    out = fn(_ctx(feats, pool))
    assert out["pass_flag"] is False
    assert "期数" in out["reason"]


@pytest.mark.parametrize("pid", ["2", "3"])
def test_source_declares_unvalidated_and_no_longer_claims_stub(pid):
    blob = BUILTIN_PLUGINS[pid]
    assert "未经验证" in blob
    assert "财务因子未接" not in blob, "占位声明必须删掉，它已不成立"
