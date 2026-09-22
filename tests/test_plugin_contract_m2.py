"""P45：模块2 四支插桩的契约（`m2_a1` / `m2_a2` / `m2_a3` / `m2_b1`）。

需求书：`docs/plans/2026-09-22-模块2-需求书.md`（D-24 唯一上线通道、D-33 命名）。
任务书：`docs/tasks/2026-09-22-p45-模块2-插桩A1A2A3B1契约.md`（§2 契约、§3 必增测试）。

四条 fail-closed 规则（§2，四支共同）：

1. 形状缺字段 / 类型错 / 越界 → **拒绝并把原因写回**，绝不 clamp、绝不补默认值；
2. 输出引用**候选池外**或**账户里不存在**的标的 → 拒绝（不许静默丢弃）；
3. 概率不归一 / 区间 `lo > hi` → 拒绝；
4. 不可算的特征给 `None` + `na_reasons`，**不许给 0.0**。

**口径决定（记录在案，供审计对账）**：任务书 §2 括号里写的字段名
`range_lo/hi` + `p_up/p_flat/p_down` 与代码里的真源不一致 ——
`stocklab/predict/model.py::CONTRACT_FIELDS` 用 `range_80` + `direction{up,flat,down}`
+ `invalidate_if`，`range_lo/range_hi` 只是 `predict/store.py::payload_to_row`
落库时的**列名**。任务书自己写了「字段名可微调」，所以本实现**取真源名**，
并用 `test_forecast_field_names_come_from_the_canonical_payload` 钉住
「不许出现真源之外的字段名」。
"""

import json

import pytest

from stocklab.plugin import contract, runtime
from stocklab.plugin.contract import PluginContractError, validate_return

M2_IDS = ("m2_a1", "m2_a2", "m2_a3", "m2_b1")

SCHEMA_VERSION = "1"

# ── 合法载荷（四支各一份，取自任务书 §2 的输出形状）─────────────────────

GOOD_A1 = {
    "picks": [
        {"code": "000333", "weight_pct": 40.0, "reason": "低估值 + 趋势向上"},
        {"code": "600690", "weight_pct": 30.0, "reason": "景气回升"},
    ],
    "cash_pct": 30.0,
    "schema_version": SCHEMA_VERSION,
}

GOOD_A2 = {
    "orders": [
        {"code": "000333", "side": "sell", "reason": "收盘跌破止损线 82.14"},
    ],
    "schema_version": SCHEMA_VERSION,
}

GOOD_A3 = {
    "range_80": [80.0, 92.0],
    "direction": {"up": 0.5, "flat": 0.3, "down": 0.2},
    "invalidate_if": "收盘跌破 82.14（20日低点）",
    "na_reasons": [],
    "schema_version": SCHEMA_VERSION,
}

#: 不知道就不给数（规则 4）：全 None + 逐条理由。
UNKNOWN_A3 = {
    "range_80": None,
    "direction": None,
    "invalidate_if": None,
    "na_reasons": ["bars_n=0", "financials_not_announced"],
    "schema_version": SCHEMA_VERSION,
}


def _good(plugin_id):
    return {"m2_a1": GOOD_A1, "m2_a2": GOOD_A2,
            "m2_a3": GOOD_A3, "m2_b1": GOOD_A3}[plugin_id]


# ══════════════════════════════════════════════════════════════════════
# T1 —— 四支契约进 SHAPES（不占既有 0–5 编号）
# ══════════════════════════════════════════════════════════════════════

def test_four_module2_shapes_are_registered():
    assert set(M2_IDS) <= set(contract.SHAPES)


def test_builtin_zero_to_five_shapes_are_untouched():
    """D-33：0–5 的编号语义已被模块1 钉住，模块2 不许改它们一个字节。"""
    assert contract.SHAPES["0"] == (("pass_flag", "bool"), ("risk_note", "str_list"))
    assert [name for name, _ in contract.SHAPES["1"]] == [
        "score", "pass_flag", "reason", "risk_list"]
    assert contract.SHAPES["5"] == (("analysis_result", "dict"),
                                    ("bad_case_list", "list"))
    assert "m2_a1" not in contract.KNOWN_PLUGIN_IDS[:6]


@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_contract_round_trip_accepts_well_formed(plugin_id):
    out = validate_return(plugin_id, dict(_good(plugin_id)))
    assert out["schema_version"] == SCHEMA_VERSION


@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_unknown_forecast_is_accepted_with_na_reasons(plugin_id):
    """m2_a3 / m2_b1 的「不知道」形态：全 None + 非空 na_reasons（规则 4）。"""
    if plugin_id not in ("m2_a3", "m2_b1"):
        pytest.skip("只有预测类插桩有「不知道」形态")
    assert validate_return(plugin_id, dict(UNKNOWN_A3))["na_reasons"]


def test_m2_a1_normalises_numbers_to_float():
    out = validate_return("m2_a1", {
        "picks": [{"code": "000333", "weight_pct": 100, "reason": "r"}],
        "cash_pct": 0, "schema_version": SCHEMA_VERSION})
    assert out["picks"][0]["weight_pct"] == 100.0
    assert out["cash_pct"] == 0.0


def test_forecast_field_names_come_from_the_canonical_payload():
    """口径唯一（需求书 §5 A5）：m2_a3 / m2_b1 的字段名必须是预测载荷真源的子集。

    真源是 `stocklab/predict/model.py::CONTRACT_FIELDS`（`range_lo/range_hi`
    只是库里 `predictions` 的列名，不是载荷字段名）。这条测试让「另造一套口径」
    变成一条读得懂的失败，而不是靠人去 diff 两份文档。
    """
    from stocklab.predict.model import CONTRACT_FIELDS

    shape_keys = {name for name, _ in contract.SHAPES["m2_a3"]}
    # `na_reasons` 是「不知道」的落点（项目既有口径，见 ctx 的 features），
    # `schema_version` 是形状版本号，两者都不是概率/区间字段。
    payload_keys = shape_keys - {"na_reasons", "schema_version"}
    assert payload_keys == {"range_80", "direction", "invalidate_if"}
    assert payload_keys <= set(CONTRACT_FIELDS), (
        f"用了真源之外的字段名：{sorted(payload_keys - set(CONTRACT_FIELDS))}")


def test_a3_and_b1_shapes_are_identical():
    """同口径才可比（任务书 §2：人工镜像与 AI 模拟必须能对标）。"""
    assert contract.SHAPES["m2_a3"] == contract.SHAPES["m2_b1"]


def test_probe_ctx_carries_the_module2_context_keys():
    """探针 ctx 必须覆盖 m2 脚本会读的键，否则正常脚本被误拒（既有教训）。"""
    for key in ("candidates", "holdings", "cash"):
        assert key in contract.PROBE_CTX, f"PROBE_CTX 缺 {key}"
    assert set(contract.PROBE_CTX["candidates"]) == {"short", "mid", "long"}


# ══════════════════════════════════════════════════════════════════════
# T2 —— fail-closed 四条规则（拒绝，绝不 clamp）
# ══════════════════════════════════════════════════════════════════════

def test_weights_plus_cash_over_100_is_rejected_not_clamped():
    """权重 80 + 现金 30 = 110% → 拒绝；**不许压到 100**。"""
    bad = {"picks": [{"code": "000333", "weight_pct": 80.0, "reason": "r"}],
           "cash_pct": 30.0, "schema_version": SCHEMA_VERSION}
    with pytest.raises(PluginContractError) as e:
        validate_return("m2_a1", dict(bad))
    assert "110" in str(e.value)


def test_weights_plus_cash_under_100_is_also_rejected():
    """不足 100 同样是越界 —— 「少了一条腿」比「多了一条腿」更隐蔽。"""
    bad = {"picks": [{"code": "000333", "weight_pct": 10.0, "reason": "r"}],
           "cash_pct": 10.0, "schema_version": SCHEMA_VERSION}
    with pytest.raises(PluginContractError):
        validate_return("m2_a1", dict(bad))


def test_empty_picks_with_full_cash_is_accepted():
    """空仓是合法结论（100% 现金），不是错误 —— 别把 fail-closed 读成「必须买」。"""
    ok = {"picks": [], "cash_pct": 100.0, "schema_version": SCHEMA_VERSION}
    assert validate_return("m2_a1", ok)["picks"] == []


def test_cash_pct_over_100_is_rejected():
    bad = {"picks": [], "cash_pct": 150.0, "schema_version": SCHEMA_VERSION}
    with pytest.raises(PluginContractError) as e:
        validate_return("m2_a1", dict(bad))
    assert "150" in str(e.value)


def test_bool_weight_is_not_accepted_as_a_percentage():
    """`True` 是 `int` 子类 —— 不显式排除就会静默变成 1.0%。"""
    bad = {"picks": [{"code": "000333", "weight_pct": True, "reason": "r"}],
           "cash_pct": 99.0, "schema_version": SCHEMA_VERSION}
    with pytest.raises(PluginContractError):
        validate_return("m2_a1", dict(bad))


def test_missing_field_is_rejected_for_every_module2_shape():
    for plugin_id in M2_IDS:
        for key in _good(plugin_id):
            bad = {k: v for k, v in _good(plugin_id).items() if k != key}
            with pytest.raises(PluginContractError) as e:
                validate_return(plugin_id, bad)
            assert key in str(e.value), (plugin_id, key)


def test_m2_a2_only_allows_the_sell_side():
    """A2 是卖出侧（止盈止损 / 调仓退出）；买入侧不归它管。"""
    bad = {"orders": [{"code": "000333", "side": "buy", "reason": "抄底"}],
           "schema_version": SCHEMA_VERSION}
    with pytest.raises(PluginContractError) as e:
        validate_return("m2_a2", dict(bad))
    assert "sell" in str(e.value)


def test_m2_a2_empty_orders_is_accepted():
    ok = {"orders": [], "schema_version": SCHEMA_VERSION}
    assert validate_return("m2_a2", ok)["orders"] == []


def test_probabilities_that_do_not_sum_to_one_are_rejected():
    bad = dict(GOOD_A3, direction={"up": 0.5, "flat": 0.5, "down": 0.5})
    with pytest.raises(PluginContractError) as e:
        validate_return("m2_a3", dict(bad))
    assert "1.5" in str(e.value)


def test_probability_outside_unit_interval_is_rejected():
    bad = dict(GOOD_A3, direction={"up": 1.5, "flat": -0.3, "down": -0.2})
    with pytest.raises(PluginContractError):
        validate_return("m2_a3", dict(bad))


def test_inverted_range_is_rejected():
    bad = dict(GOOD_A3, range_80=[92.0, 80.0])
    with pytest.raises(PluginContractError) as e:
        validate_return("m2_a3", dict(bad))
    assert "92" in str(e.value) and "80" in str(e.value)


def test_partially_unknown_forecast_is_rejected():
    """规则 4 的 all-or-nothing：不许「半真半假」（有数就给全，无数就全 None）。"""
    bad = dict(GOOD_A3, direction=None,
               na_reasons=["sector_unknown"])
    with pytest.raises(PluginContractError):
        validate_return("m2_b1", dict(bad))


def test_all_none_without_na_reasons_is_rejected():
    """全 None 却不说为什么 = 「不知道」被当成「就这样」。"""
    bad = dict(UNKNOWN_A3, na_reasons=[])
    with pytest.raises(PluginContractError):
        validate_return("m2_a3", dict(bad))


def test_rejection_does_not_rewrite_the_input():
    """越界拒绝时**不许**留下一个被改写过的输出（任务书 §3.2）。"""
    bad = {"picks": [{"code": "000333", "weight_pct": 110.0, "reason": "r"}],
           "cash_pct": 30.0, "schema_version": SCHEMA_VERSION}
    with pytest.raises(PluginContractError):
        validate_return("m2_a1", bad)
    assert bad["picks"][0]["weight_pct"] == 110.0     # 原地未被改
    assert bad["cash_pct"] == 30.0


def test_non_dict_result_is_rejected_for_every_module2_shape():
    for plugin_id in M2_IDS:
        with pytest.raises(PluginContractError):
            validate_return(plugin_id, None)


# ---------- 规则 2：引用池外 / 账户里没有的标的 ----------

def test_reference_outside_the_candidate_pool_is_rejected():
    with pytest.raises(PluginContractError) as e:
        contract.validate_references(
            "m2_a1", dict(GOOD_A1), allowed_codes={"000333"})
    assert "600690" in str(e.value)


def test_reference_missing_from_the_account_is_rejected():
    with pytest.raises(PluginContractError) as e:
        contract.validate_references(
            "m2_a2", dict(GOOD_A2), allowed_codes={"600690"})
    assert "000333" in str(e.value)


def test_references_all_inside_the_allowed_set_pass():
    out = contract.validate_references(
        "m2_a1", dict(GOOD_A1), allowed_codes={"000333", "600690"})
    assert len(out["picks"]) == 2


def test_empty_positions_reference_nothing_and_pass():
    ok = {"orders": [], "schema_version": SCHEMA_VERSION}
    assert contract.validate_references(
        "m2_a2", ok, allowed_codes={"000333"})["orders"] == []


def test_reference_check_refuses_a_shape_only_plugin():
    """对不引用标的的插桩调用引用校验是调用方的编程错误，不是脚本 bug。"""
    with pytest.raises(ValueError):
        contract.validate_references("m2_a3", dict(GOOD_A3),
                                     allowed_codes={"000333"})


def test_reference_check_is_not_silently_skippable():
    """fail-closed：**没有**允许集合就不能算通过 —— 传空集时全判越界。"""
    with pytest.raises(PluginContractError):
        contract.validate_references("m2_a1", dict(GOOD_A1), allowed_codes=set())


# ══════════════════════════════════════════════════════════════════════
# T3 —— 沙盒探针 / PIT 守卫复用 / approve 未放宽
# ══════════════════════════════════════════════════════════════════════

#: 四支的探针脚本：读 ctx 时**必须**容忍「不知道」（规则 4）。
PROBE_SCRIPTS = {
    "m2_a1": (
        "def run(ctx):\n"
        "    codes = [c for pool in ('short', 'mid', 'long')\n"
        "             for c in ctx['candidates'].get(pool, [])]\n"
        "    if not codes:\n"
        "        return {'picks': [], 'cash_pct': 100.0,"
        " 'schema_version': '1'}\n"
        "    w = 100.0 / len(codes)\n"
        "    return {'picks': [{'code': c, 'weight_pct': w,"
        " 'reason': 'probe'} for c in codes],\n"
        "            'cash_pct': 0.0, 'schema_version': '1'}\n"
    ),
    "m2_a2": (
        "def run(ctx):\n"
        "    return {'orders': [], 'schema_version': '1'}\n"
    ),
    "m2_a3": (
        "def run(ctx):\n"
        "    f = ctx['features']\n"
        "    if f['roe'] is None:\n"          # 「不知道」不许折算成 0.0
        "        return {'range_80': None, 'direction': None,"
        " 'invalidate_if': None,\n"
        "                'na_reasons': ['roe_unknown'],"
        " 'schema_version': '1'}\n"
        "    return {'range_80': [80.0, 92.0],\n"
        "            'direction': {'up': 0.5, 'flat': 0.3, 'down': 0.2},\n"
        "            'invalidate_if': '收盘跌破 82.14', 'na_reasons': [],\n"
        "            'schema_version': '1'}\n"
    ),
    "m2_b1": (
        "def run(ctx):\n"
        "    f = ctx['features']\n"
        "    if f['roe'] is None:\n"
        "        return {'range_80': None, 'direction': None,"
        " 'invalidate_if': None,\n"
        "                'na_reasons': ['roe_unknown'],"
        " 'schema_version': '1'}\n"
        "    return {'range_80': [80.0, 92.0],\n"
        "            'direction': {'up': 0.5, 'flat': 0.3, 'down': 0.2},\n"
        "            'invalidate_if': '收盘跌破 82.14', 'na_reasons': [],\n"
        "            'schema_version': '1'}\n"
    ),
}


@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_probe_script_runs_on_the_contract_probe_ctx(plugin_id):
    """四支脚本能被沙盒真实执行（`runtime.load_script` + 契约探针 ctx）。

    未被计算的 ctx 键在探针里是**空形状**，脚本必须走 None 分支而不是炸
    （规则 4：不可算给 None + na_reasons）。
    """
    fn = runtime.load_script(PROBE_SCRIPTS[plugin_id], plugin_id=plugin_id)
    out = fn(contract.PROBE_CTX)
    assert out["schema_version"] == "1"


def test_probe_ctx_uses_none_not_zero_for_unknown_features():
    """规则 4 的口径在**入参**上：不可算的特征是 None，不是 0.0。"""
    feats = contract.PROBE_CTX["features"]
    for key in ("roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"):
        assert feats[key] is None, f"{key} 被填成了 {feats[key]!r}（不知道≠最差）"
    assert isinstance(feats["na_reasons"], list)


def test_probe_ctx_is_json_serializable_with_module2_keys():
    json.dumps(contract.PROBE_CTX)


@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_module2_scripts_pass_the_real_submit_sandbox(tmp_db, plugin_id):
    """D-24：四支走的是**既有插件通道**（契约预检 → `sandbox.run_sandbox` → 人工闸门）。

    这条打的是 `cli/plugin.py::_run_sandbox` 本身，不是它的替身 ——
    「插桩不是主干硬编码逻辑」这句话的可执行证据就在这里。
    """
    from stocklab.cli import plugin as cli_plugin
    from stocklab.plugin import store
    from tests.test_candidate_run import _seed_db

    now = "2026-09-22T16:00:00+08:00"
    conn = _seed_db(tmp_db)
    sid = store.insert_script(conn, plugin_id=plugin_id, version="1.0.0",
                              source_text=PROBE_SCRIPTS[plugin_id], note=None,
                              now=now)
    passed, reason = cli_plugin._run_sandbox(
        conn, script_id=sid, plugin_id=plugin_id,
        source_text=PROBE_SCRIPTS[plugin_id], now=now)
    conn.close()
    assert passed, reason


# ---------- PIT：复用既有守卫（`candidate.build_ctx` 的裁剪），不新写一套 ----------

def test_plugin_never_sees_bars_after_asof_via_the_existing_pit_guard(tmp_db):
    """PIT 由 `candidate/score.py::build_ctx` 的裁剪保证；插桩拿到的是裁剪后的 ctx。

    插桩自己**不做**裁剪（它是被裁剪的消费者）—— 所以这条测试钉的是
    「既有守卫覆盖了插桩路径」，而不是另写一个守卫。
    """
    from stocklab.candidate import score as candidate_score
    from stocklab.config.universe import Instrument
    from stocklab.data.models import Bar
    from stocklab.store.db import connect
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    conn = connect(tmp_db)
    conn.execute("INSERT INTO instruments (code, name, market, board, type,"
                 " sector, added_at) VALUES ('000333','美的','sz','main',"
                 "'stock','家用电器','2026-01-01')")
    conn.commit()

    asof = "2026-09-17"
    bars = [
        Bar(code="000333", date="2026-09-16", open=10.0, high=10.0, low=10.0,
            close=10.0, volume=1.0, amount=None, turnover=None, source="x",
            adj_mode="none"),
        Bar(code="000333", date=asof, open=10.0, high=10.0, low=10.0,
            close=10.0, volume=1.0, amount=None, turnover=None, source="x",
            adj_mode="none"),
        # 未来两根（> asof）：插桩**不许**看见
        Bar(code="000333", date="2026-09-18", open=99.0, high=99.0, low=99.0,
            close=99.0, volume=1.0, amount=None, turnover=None, source="x",
            adj_mode="none"),
        Bar(code="000333", date="2026-09-21", open=99.0, high=99.0, low=99.0,
            close=99.0, volume=1.0, amount=None, turnover=None, source="x",
            adj_mode="none"),
    ]
    inst = Instrument(code="000333", name="美的", market="sz", board="main")
    ctx = candidate_score.build_ctx(conn, inst, "short", bars, asof=asof)
    conn.close()

    assert [b["date"] for b in ctx["bars"]] == ["2026-09-16", asof]

    # 插桩侧可观测的同一事实：脚本把「它看到的最新一根」回吐出来
    fn = runtime.load_script(
        "def run(ctx):\n"
        "    seen = [b['date'] for b in ctx['bars']]\n"
        "    latest = seen[len(seen) - 1] if seen else 'none'\n"
        "    return {'picks': [{'code': ctx['code'], 'weight_pct': 100.0,\n"
        "                       'reason': latest}],\n"
        "            'cash_pct': 0.0, 'schema_version': '1'}\n",
        plugin_id="m2_a1")
    out = fn(ctx)
    assert out["picks"][0]["reason"] == asof
    assert "2026-09-18" not in str(out)


def test_existing_pit_guard_is_the_only_clip(tmp_db):
    """反向自检：把未来那两根去掉后，`build_ctx` 的输出必须**逐字段不变**。

    若哪天有人在插桩侧另加一道裁剪，这条不会红 —— 但它与上一条一起，
    把「裁剪发生在 build_ctx」这个事实钉在可观测的输出上（ctx["bars"] 的日期集）。
    """
    from stocklab.candidate import score as candidate_score
    from stocklab.config.universe import Instrument
    from stocklab.data.models import Bar
    from stocklab.store.db import connect
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    conn = connect(tmp_db)
    conn.execute("INSERT INTO instruments (code, name, market, board, type,"
                 " sector, added_at) VALUES ('000333','美的','sz','main',"
                 "'stock','家用电器','2026-01-01')")
    conn.commit()
    inst = Instrument(code="000333", name="美的", market="sz", board="main")
    asof = "2026-09-17"
    past = [Bar(code="000333", date="2026-09-16", open=10.0, high=10.0, low=10.0,
                close=10.0, volume=1.0, amount=None, turnover=None, source="x",
                adj_mode="none")]
    future = [Bar(code="000333", date="2026-09-21", open=99.0, high=99.0,
                  low=99.0, close=99.0, volume=1.0, amount=None, turnover=None,
                  source="x", adj_mode="none")]
    with_future = candidate_score.build_ctx(conn, inst, "short", past + future,
                                            asof=asof)
    without = candidate_score.build_ctx(conn, inst, "short", past, asof=asof)
    conn.close()
    assert with_future == without


# ---------- approve 闸门未放宽 ----------

def test_module2_plugins_cannot_skip_the_human_approval_gate(tmp_db):
    """四支走的是**同一道**闸门：沙盒没跑完 → 不许 approve（D-24）。"""
    from stocklab.plugin import lifecycle, store
    from stocklab.store.db import connect
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    conn = connect(tmp_db)
    for plugin_id in M2_IDS:
        sid = store.insert_script(conn, plugin_id=plugin_id, version="1.0.0",
                                  source_text=PROBE_SCRIPTS[plugin_id],
                                  note=None, now="2026-09-22T16:00:00+08:00")
        with pytest.raises(lifecycle.PluginStateError):
            lifecycle.approve(conn, sid, actor="ai",
                              reason="AI 自己批自己",
                              now="2026-09-22T16:00:00+08:00")
    conn.close()


def test_approve_pre_states_do_not_mention_any_module2_plugin_id():
    """`approve` 的前置状态里**不许**按插件编号开口子。"""
    from stocklab.plugin import lifecycle
    src = lifecycle.approve.__doc__ or ""
    assert "m2_" not in src
    assert "m2_" not in json.dumps(lifecycle._EVENT_PRE_STATES)
