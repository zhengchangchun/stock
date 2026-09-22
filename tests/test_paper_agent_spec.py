"""P37：智能体动态编排臂的**规格与校验**（`stocklab/paper/agent_spec.py`）。

验收项（设计 §6 第 1 条）在这里钉死：区间端点通过、越界拒绝、未知字段拒绝、
`stop_loss_pct="off"`、`cash_floor_pct` 放宽被拒、`spec_sha256` 对键序不敏感。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 白名单**恰好**是拍板的 5 个字段 | 为了让某个改动通过而偷偷加字段 |
| 越界 / 未知字段 / 类型不符 → `SpecViolation` | 夹紧到边界、静默丢弃 |
| `cash_floor_pct` 只能收紧（下界 = 静态臂现值） | 把它当成普通区间参数 |
| 默认 spec 与 `arm-discipline-10` 逐项同口径 | 另抄一份数字，改配置后漂移 |
| 默认 spec 推出的止损线 == 静态臂写死的那条 | 用设计文档里的近似值 −5.4 |
| `spec_sha256` 对键序不敏感 | 直接 hash `json.dumps(spec)` |
| `apply_spec` 是**增量合并** | 每次都从默认值重来（会静默丢掉上一版的改动） |
| 试错预算 >3 被拒 | 预算可以被智能体自己填大 |

数字一律**从被测模块取**，不手抄一份到测试里。
"""

import pytest

from stocklab.paper import agent_spec
from stocklab.paper.config import (
    AGENT_DEFAULT_ARM,
    DISCIPLINE_PREFIX,
    ETF_TRANCHES,
    HOLD_CODE,
    HOLD_COST_PRICE,
    MAX_TRIALS_PER_REVIEW,
    RULE_CITATIONS,
    RULE_CITATIONS_AGENT,
)
from stocklab.portfolio.discipline import DISCIPLINE, lines_for


# ---------- 白名单就是拍板的那 5 个字段 ----------

def test_schema_is_exactly_the_five_decided_fields():
    """变更空间**恰好**是 D-16 拍板的 5 个字段。多一个就红。"""
    assert set(agent_spec.SPEC_SCHEMA) == {
        "etf_target_pct", "stop_loss_pct", "max_single_pct",
        "rebalance_cadence", "cash_floor_pct"}
    assert set(agent_spec.AGENT_DEFAULT_SPEC) == set(agent_spec.SPEC_SCHEMA)


@pytest.mark.parametrize("name", ["cost_model", "lot", "whitelist", "leverage",
                                  "cash_band_pct", "model_version", "seed"])
def test_changing_ones_own_change_space_is_not_expressible(name):
    """「扩大自己的变更空间」在**字段名**这一层就不可表达（不是靠事后拦）。

    成本口径 / 整手 / 白名单 / 加方向信号，都只能表现为「未知字段被拒」。
    """
    with pytest.raises(agent_spec.SpecViolation) as ei:
        agent_spec.validate_spec({name: 1})
    assert ei.value.field == name
    assert "未知字段" in ei.value.reason


# ---------- 区间与类型 ----------

@pytest.mark.parametrize("name", sorted(agent_spec.SPEC_SCHEMA))
def test_boundaries_are_inclusive(name):
    """区间是**闭**区间：上下界本身必须通过。"""
    f = agent_spec.SPEC_SCHEMA[name]
    if f.choices:
        assert agent_spec.validate_spec({name: f.choices[0]})[name] == f.choices[0]
        return
    assert agent_spec.validate_spec({name: f.lo})[name] == f.lo
    assert agent_spec.validate_spec({name: f.hi})[name] == f.hi


@pytest.mark.parametrize("name", sorted(agent_spec.SPEC_SCHEMA))
def test_out_of_range_is_rejected_not_clamped(name):
    """越界 → **拒绝**，绝不夹紧到边界（夹紧会把「它提了个非法改动」改写成别的）。"""
    f = agent_spec.SPEC_SCHEMA[name]
    if f.choices:
        with pytest.raises(agent_spec.SpecViolation):
            agent_spec.validate_spec({name: 4})         # 4 不在 (3, 5, 10) 里
        with pytest.raises(agent_spec.SpecViolation):
            agent_spec.validate_spec({name: "5"})       # 字符串不算选项
        return
    with pytest.raises(agent_spec.SpecViolation) as lo:
        agent_spec.validate_spec({name: f.lo - 1e-6})
    assert "低于下界" in lo.value.reason
    with pytest.raises(agent_spec.SpecViolation) as hi:
        agent_spec.validate_spec({name: f.hi + 1e-6})
    assert "高于上界" in hi.value.reason


@pytest.mark.parametrize("bad", [True, False, "10", None, [10], {"a": 1}])
def test_type_mismatch_is_rejected(bad):
    """`True` 不是数字 —— `isinstance(True, int)` 为真是 Python 的坑，要显式挡。"""
    with pytest.raises(agent_spec.SpecViolation):
        agent_spec.validate_spec({"etf_target_pct": bad})


def test_cash_floor_may_only_tighten():
    """现金下限**只许收紧**：等于静态臂现值可以，放宽一个基点就拒。"""
    f = agent_spec.SPEC_SCHEMA["cash_floor_pct"]
    base = DISCIPLINE["cash_band_pct"][0]
    assert f.lo == base and f.tighten_only is True
    assert agent_spec.validate_spec({"cash_floor_pct": base})["cash_floor_pct"] == base
    assert agent_spec.validate_spec({"cash_floor_pct": base + 1.0})
    with pytest.raises(agent_spec.SpecViolation):
        agent_spec.validate_spec({"cash_floor_pct": base - 0.01})


def test_stop_loss_accepts_the_off_literal_only_there():
    """只有止损允许关；别的字段拿 `"off"` 当值一律拒。"""
    assert agent_spec.validate_spec(
        {"stop_loss_pct": agent_spec.SPEC_OFF})["stop_loss_pct"] == agent_spec.SPEC_OFF
    assert agent_spec.stop_loss_line(
        {"stop_loss_pct": agent_spec.SPEC_OFF}) is None
    with pytest.raises(agent_spec.SpecViolation):
        agent_spec.validate_spec({"etf_target_pct": agent_spec.SPEC_OFF})


def test_unknown_field_error_names_the_rejected_key():
    with pytest.raises(agent_spec.SpecViolation) as ei:
        agent_spec.validate_spec({"etf_target_pct": 12, "kelly_fraction": 0.5})
    assert ei.value.field == "kelly_fraction"
    assert ei.value.value == 0.5


def test_validate_returns_only_the_given_subset():
    """`validate_spec` 不补字段（补字段是 `normalize_spec` 的事）。"""
    assert agent_spec.validate_spec({"etf_target_pct": 12}) == {"etf_target_pct": 12.0}
    assert agent_spec.validate_spec({}) == {}


# ---------- 默认 spec = arm-discipline-10 口径（一处推导，不另抄） ----------

def test_default_etf_target_comes_from_the_named_static_arm():
    suffix = AGENT_DEFAULT_ARM[len(DISCIPLINE_PREFIX):]
    want = next(t for t in ETF_TRANCHES if f"{int(t):02d}" == suffix)
    assert agent_spec.AGENT_DEFAULT_SPEC["etf_target_pct"] == float(want)


def test_default_spec_is_the_static_arm_written_off_by_value():
    """默认 spec 的每一项都能在写死条文里找到同一个数。"""
    d = agent_spec.AGENT_DEFAULT_SPEC
    assert d["max_single_pct"] == DISCIPLINE["single_position_max_pct"]
    assert d["cash_floor_pct"] == DISCIPLINE["cash_band_pct"][0]
    assert d["rebalance_cadence"] in agent_spec.SPEC_SCHEMA[
        "rebalance_cadence"].choices
    assert d["rebalance_cadence"] == 5


def test_default_stop_loss_line_equals_the_written_line():
    """默认 spec 推出的止损线 == 静态臂写死的那条（**逐位相同**）。

    用设计文档里的近似值 −5.4 会让这条断言差几毛钱 —— 于是「默认 spec 与静态臂
    同口径」从一句可验证的话退化成一个大概。
    """
    written = float(lines_for(HOLD_CODE)["stop_loss_close"])
    assert agent_spec.AGENT_DEFAULT_SPEC["stop_loss_pct"] == pytest.approx(
        round((written / HOLD_COST_PRICE - 1.0) * 100.0, 6))
    assert agent_spec.stop_loss_line(agent_spec.AGENT_DEFAULT_SPEC) == written


# ---------- 序列化 ----------

def test_spec_sha256_is_insensitive_to_key_order():
    a = {"etf_target_pct": 12.0, "stop_loss_pct": -6.0}
    b = {"stop_loss_pct": -6.0, "etf_target_pct": 12.0}
    assert agent_spec.spec_sha256(a) == agent_spec.spec_sha256(b)
    # 缺字段按默认补齐之后再 hash ⇒ 只写一个字段与写全等值也同指纹
    assert agent_spec.spec_sha256({"etf_target_pct": 12.0}) == \
        agent_spec.spec_sha256({"etf_target_pct": 12.0, **{
            k: v for k, v in agent_spec.AGENT_DEFAULT_SPEC.items()
            if k != "etf_target_pct"}})
    # 改一个数 → 指纹必须变（否则「同指纹」这条判据是死的）
    assert agent_spec.spec_sha256(a) != agent_spec.spec_sha256(
        {**a, "stop_loss_pct": -6.5})


def test_spec_tag_carries_the_fingerprint_and_the_ledger_day():
    spec = {"etf_target_pct": 12.0}
    tag = agent_spec.spec_tag(spec, source_asof="2026-09-22")
    assert tag == f"spec {agent_spec.spec_sha256(spec)[:12]} @ 2026-09-22"
    assert "2026-09-22" not in agent_spec.spec_tag(spec)


# ---------- 增量合并 ----------

def test_apply_spec_merges_incrementally_and_keeps_earlier_changes():
    first = agent_spec.apply_spec(None, {"etf_target_pct": 12.0})
    second = agent_spec.apply_spec(first, {"cash_floor_pct": 50.0})
    assert second["etf_target_pct"] == 12.0, "第二次合并不许把上一版的改动丢掉"
    assert second["cash_floor_pct"] == 50.0
    assert second["max_single_pct"] == agent_spec.AGENT_DEFAULT_SPEC["max_single_pct"]


def test_apply_spec_does_not_revalidate_untouched_fields():
    """`before` 里那些**当时合法、现在越界**的字段不许在本次被打回。

    语义是「在现状上改这一项」。重新校验整份 spec 会让一次只改 `max_single_pct`
    的复审因为旧的 `etf_target_pct` 而失败 —— 那不是校验，是时区穿越。
    """
    legacy = {**agent_spec.AGENT_DEFAULT_SPEC, "etf_target_pct": 99.0}
    out = agent_spec.apply_spec(legacy, {"max_single_pct": 45.0})
    assert out["etf_target_pct"] == 99.0          # 照旧带着，不在本次被拒
    assert out["max_single_pct"] == 45.0
    # 但它自己出现在 patch 里时照样被拒
    with pytest.raises(agent_spec.SpecViolation):
        agent_spec.apply_spec(legacy, {"etf_target_pct": 99.0})


def test_spec_violation_is_renderable():
    exc = agent_spec.SpecViolation("etf_target_pct", 30.0, "高于上界：5 ~ 25")
    assert exc.as_dict() == {"field": "etf_target_pct", "value": 30.0,
                             "reason": "高于上界：5 ~ 25"}


# ---------- 参数化（静态臂与智能体臂共用同一个内核） ----------

def test_params_for_uses_the_agent_rule_book_and_stamps_the_spec():
    spec = {"etf_target_pct": 12.0, "cash_floor_pct": 50.0}
    p = agent_spec.params_for(spec, source_asof="2026-09-22")
    assert p.etf_target_pct == 12.0
    assert p.cash_floor_pct == 50.0
    assert p.stop_loss_line == agent_spec.stop_loss_line(spec)
    assert dict(p.citations) == RULE_CITATIONS_AGENT
    assert p.cite("stop_loss") == RULE_CITATIONS_AGENT["stop_loss"]
    assert p.spec_tag == agent_spec.spec_tag(spec, source_asof="2026-09-22")
    # 智能体臂的条文表与写死条文表**不是同一批文本**（否则数不清谁触发的）
    assert RULE_CITATIONS_AGENT["stop_loss"] != RULE_CITATIONS["stop_loss"]


def test_agent_rule_summaries_do_not_hardcode_numbers():
    """`RULE_CITATIONS_AGENT` 刻意**不带数字**（数字进 `reason`）。

    带了数字之后，一条 50% 的下限会被记成「45% 那条下限」，按规则类型统计就废了。
    对照组：写死条文表里就是带数字的（那才是它的语义）。
    """
    joined = "\n".join(RULE_CITATIONS_AGENT.values())
    for literal in ("45", "40", "5%", "1,000", "1000", "82.14"):
        assert literal not in joined, f"智能体条文里出现了写死的数字：{literal}"
    assert "82.14" in "\n".join(RULE_CITATIONS.values())
    assert all(isinstance(v, str) and v for v in RULE_CITATIONS_AGENT.values())


def test_params_for_with_stop_loss_off_disables_only_that_rule():
    p = agent_spec.params_for({"stop_loss_pct": agent_spec.SPEC_OFF})
    assert p.stop_loss_line is None and p.stop_loss_enabled is False
    assert p.single_position_max_pct == agent_spec.AGENT_DEFAULT_SPEC["max_single_pct"]


# ---------- 试错预算 ----------

def test_trial_budget_bounds():
    assert agent_spec.validate_trial_budget(1, []) is None
    assert agent_spec.validate_trial_budget(MAX_TRIALS_PER_REVIEW, [{"a": 1}]) is None
    with pytest.raises(agent_spec.SpecViolation) as ei:
        agent_spec.validate_trial_budget(MAX_TRIALS_PER_REVIEW + 1, [])
    assert "预算" in ei.value.reason
    for bad in (0, -1, True, 1.5, "3"):
        with pytest.raises(agent_spec.SpecViolation):
            agent_spec.validate_trial_budget(bad, [])


def test_rejected_cannot_outnumber_trials():
    """至少有一版被采纳 ⇒ 被拒条数最多 `n_trials - 1`。计数对不上不许落库。"""
    assert agent_spec.validate_trial_budget(2, [{"x": 1}]) is None
    with pytest.raises(agent_spec.SpecViolation) as ei:
        agent_spec.validate_trial_budget(2, [{"x": 1}, {"y": 2}])
    assert ei.value.field == "rejected"
