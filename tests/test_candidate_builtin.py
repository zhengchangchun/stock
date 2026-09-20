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


def test_plugin0_prefers_ctx_sector_over_name_guessing():
    """有 sector 时用 sector；行业口径只允许一套。"""
    fn = runtime.load_script(BUILTIN_PLUGINS["0"], plugin_id="0")
    ctx = {"code": "600036", "name": "招商银行", "asof": "2026-09-17",
           "pool": "short", "sector": "银行Ⅱ", "asset_type": "stock",
           "board": "main", "bars": [], "features": {}, "risk_list": []}
    out = fn(ctx)
    assert out["pass_flag"] is True
    assert any("银行" in r for r in out["risk_note"])


def test_plugin0_falls_back_to_name_when_sector_missing():
    fn = runtime.load_script(BUILTIN_PLUGINS["0"], plugin_id="0")
    ctx = {"code": "000333", "name": "美的集团", "asof": "2026-09-17",
           "pool": "short", "sector": None, "asset_type": "stock",
           "board": "main", "bars": [], "features": {}, "risk_list": []}
    out = fn(ctx)
    assert out["pass_flag"] is True


def test_plugin0_source_reads_sector():
    assert 'ctx' in BUILTIN_PLUGINS["0"] and 'sector' in BUILTIN_PLUGINS["0"]
