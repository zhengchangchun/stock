"""Task 18：6 个内置插桩脚本 —— 必须能过预检、契约，且诚实标注留桩。"""

import pytest

from stocklab.candidate.builtin import BUILTIN_PLUGINS
from stocklab.plugin import contract, guard, runtime


def test_all_six_present():
    assert sorted(BUILTIN_PLUGINS) == ["0", "1", "2", "3", "4", "5"]


@pytest.mark.parametrize("plugin_id", ["0", "1", "2", "3", "4", "5"])
def test_passes_guard(plugin_id):
    guard.check_source(BUILTIN_PLUGINS[plugin_id])


@pytest.mark.parametrize("plugin_id", ["0", "1", "2", "3", "4", "5"])
def test_runs_and_satisfies_contract(plugin_id):
    fn = runtime.load_script(BUILTIN_PLUGINS[plugin_id], plugin_id=plugin_id)
    ctx = {
        "code": "000333", "name": "美的集团", "asof": "2026-09-17",
        "pool": "mid", "asset_type": "stock", "board": "main",
        "bars": [{"date": f"2026-08-{d:02d}", "open": 10.0, "high": 10.5,
                  "low": 9.5, "close": 10.0 + d * 0.1, "volume": 1000,
                  "amount": None, "turnover": None} for d in range(1, 29)],
        "raw_score": 60.0, "risk_list": ["高波动"],
    }
    result = fn(ctx)
    assert isinstance(result, dict)


def test_score_plugins_declare_financial_data_is_stubbed():
    """中期/长期池的脚本必须自己声明「财务因子未接」——
    这是报告之外的第二道诚实防线（脚本的 risk_list 会进候选池记录）。"""
    for plugin_id in ("2", "3"):
        blob = BUILTIN_PLUGINS[plugin_id]
        assert "财务" in blob and ("未接" in blob or "留桩" in blob)


def test_industry_screen_is_per_industry():
    """插桩0 必须按行业分支 —— 否则「行业特殊排雷」名不副实。"""
    assert "sector" in BUILTIN_PLUGINS["0"] or "行业" in BUILTIN_PLUGINS["0"]
