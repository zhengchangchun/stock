"""Task 3：返回结构校验（设计文档 §6.2）—— 越界拒绝，不 clamp。"""

import pytest

from stocklab.plugin.contract import PluginContractError, validate_return

GOOD_SCORE = {"score": 78.5, "pass_flag": True, "reason": "低估值", "risk_list": []}
GOOD_SCREEN = {"pass_flag": True, "risk_note": []}
GOOD_ADJUST = {"final_score": 70.0, "risk_out": ["高波动"]}


def test_plugin1_accepts_well_formed():
    assert validate_return("1", dict(GOOD_SCORE))["score"] == 78.5


def test_plugin0_accepts_well_formed():
    assert validate_return("0", dict(GOOD_SCREEN))["pass_flag"] is True


def test_plugin4_accepts_well_formed():
    assert validate_return("4", dict(GOOD_ADJUST))["final_score"] == 70.0


def test_score_over_100_is_rejected_not_clamped():
    bad = dict(GOOD_SCORE, score=150.0)
    with pytest.raises(PluginContractError) as e:
        validate_return("1", bad)
    assert "150" in str(e.value)


def test_score_below_zero_is_rejected():
    with pytest.raises(PluginContractError):
        validate_return("1", dict(GOOD_SCORE, score=-0.1))


def test_bool_is_not_accepted_as_score():
    """Python 里 True 是 int 的子类 —— 必须显式排除，否则 True 会变成 1.0。"""
    with pytest.raises(PluginContractError):
        validate_return("1", dict(GOOD_SCORE, score=True))


def test_int_score_is_accepted_and_coerced():
    assert validate_return("1", dict(GOOD_SCORE, score=80))["score"] == 80.0


def test_string_score_is_rejected():
    with pytest.raises(PluginContractError):
        validate_return("1", dict(GOOD_SCORE, score="high"))


def test_missing_field_is_rejected():
    bad = {k: v for k, v in GOOD_SCORE.items() if k != "pass_flag"}
    with pytest.raises(PluginContractError) as e:
        validate_return("1", bad)
    assert "pass_flag" in str(e.value)


def test_wrong_type_for_pass_flag_is_rejected():
    with pytest.raises(PluginContractError):
        validate_return("1", dict(GOOD_SCORE, pass_flag="yes"))


def test_risk_list_must_be_list_of_str():
    with pytest.raises(PluginContractError):
        validate_return("1", dict(GOOD_SCORE, risk_list="高波动"))


def test_non_dict_result_is_rejected():
    with pytest.raises(PluginContractError):
        validate_return("1", None)


def test_unknown_plugin_id_is_rejected():
    with pytest.raises(PluginContractError):
        validate_return("9", dict(GOOD_SCORE))


def test_plugin5_shape():
    ok = {"analysis_result": {}, "bad_case_list": []}
    assert validate_return("5", ok)["analysis_result"] == {}


def test_extra_keys_are_dropped_and_input_is_unchanged():
    """validate_return 返回的 dict 不含多余键，且原始输入不被修改。"""
    inp = {**GOOD_SCORE, "injected": "x"}
    out = validate_return("1", inp)
    assert "injected" not in out
    # 原始输入对象必须保持不变
    assert inp["injected"] == "x"
    assert set(out.keys()) == {"score", "pass_flag", "reason", "risk_list"}


def test_probe_ctx_has_complete_feature_shape():
    """探针 ctx 必须让读 features 任意键的脚本都能跑起来（上一轮的坑）。"""
    from stocklab.plugin import contract
    feats = contract.PROBE_CTX["features"]
    for key in contract.FEATURE_KEYS:
        assert key in feats, f"PROBE_CTX['features'] 缺 {key}"
    assert contract.PROBE_CTX["sector"]


def test_probe_ctx_is_json_serializable():
    import json
    from stocklab.plugin import contract
    json.dumps(contract.PROBE_CTX)
