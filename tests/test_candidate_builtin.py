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
    """插桩2 短 ctx 走 guard 分支，长 ctx 走真实打分体 —— reason 必须不同。"""
    fn = runtime.load_script(BUILTIN_PLUGINS["2"], plugin_id="2")
    short_result = fn(_short_ctx())
    long_result = fn(_long_ctx())
    assert isinstance(short_result, dict)
    assert isinstance(long_result, dict)
    # 短 ctx 命中长度守卫，长 ctx 进入打分体，两者 reason 不同证明分支都覆盖到
    assert short_result.get("reason") != long_result.get("reason"), (
        f"plugin2 short reason={short_result.get('reason')!r} "
        f"long reason={long_result.get('reason')!r}"
    )


def test_plugin3_short_circuit_vs_full_body():
    """插桩3 短 ctx 走 guard 分支，长 ctx 走真实打分体 —— reason 必须不同。"""
    fn = runtime.load_script(BUILTIN_PLUGINS["3"], plugin_id="3")
    short_result = fn(_short_ctx())
    long_result = fn(_long_ctx())
    assert isinstance(short_result, dict)
    assert isinstance(long_result, dict)
    assert short_result.get("reason") != long_result.get("reason"), (
        f"plugin3 short reason={short_result.get('reason')!r} "
        f"long reason={long_result.get('reason')!r}"
    )


def test_score_plugins_declare_financial_data_is_stubbed():
    """中期/长期池的脚本必须自己声明「财务因子未接」——
    这是报告之外的第二道诚实防线（脚本的 risk_list 会进候选池记录）。"""
    for plugin_id in ("2", "3"):
        blob = BUILTIN_PLUGINS[plugin_id]
        assert "财务" in blob and ("未接" in blob or "留桩" in blob)


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
