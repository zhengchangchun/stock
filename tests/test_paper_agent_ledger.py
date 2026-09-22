"""P37：智能体动态编排臂的决定台账（`paper_agent_decisions`）。

这张表是「智能体改过什么」的唯一档案，所以它必须是 **append-only**：
试错次数、变更前后原文、喂进去的 PIT 指纹 —— 少了任何一列，「编排带来了信息」
与「多试几次的好运」就再也分不开了（设计 §11）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| UPDATE / DELETE / `INSERT OR REPLACE` 三条都被触发器拒 | 少一条触发器（REPLACE 走隐式 DELETE，见 P6） |
| `UNIQUE(arm, asof)` 挡住同一天两版 | 靠「先查再写」，竞态下会写进两版 |
| `current_spec(asof)` 用 `<=`（复审日当天那份生效） | 改成 `==`（非复审日直接回落默认，静默改口径） |
| `spec_before(asof)` 用严格 `<` | 抄 `current_spec`（于是「改之前是什么」永远等于「改之后」） |
| 两臂的台账互不串台 | 漏写 `arm` 过滤 |
| `reproducibility` 无重复组 → `None` + 「无法判定」 | 读成 `True`（把「没检验」当成「通过」） |
| 重复组给出不同 spec → `False` + 逐条列出 | 静默平均 |
| 缺任一指纹 / `agent_kind` 非法 → 拒写 | 留个空串占位，事后查不出是谁写的 |
"""

import sqlite3

import pytest

from stocklab.paper import agent_spec
from stocklab.paper.config import ARM_AGENT, ARM_AGENT_RANDOM
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-22T16:00:00+08:00"
STAMP = "a" * 64


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _record(conn, *, arm=ARM_AGENT, asof="2026-09-22", patch=None,
            before=None, n_trials=1, rejected=None, kind=agent_spec.AGENT_KIND_MANUAL,
            model_id="manual", prompt=STAMP, ctx=STAMP, seed=0, rationale="",
            commit=True):
    before = before or agent_spec.AGENT_DEFAULT_SPEC
    return agent_spec.record_decision(
        conn, arm=arm, asof=asof, spec_before=before,
        spec_after=agent_spec.apply_spec(before, patch or {"etf_target_pct": 12.0}),
        agent_kind=kind, model_id=model_id, prompt_sha256=prompt, seed=seed,
        context_sha256=ctx, now=NOW, n_trials=n_trials,
        rejected=rejected, rationale=rationale, commit=commit)


# ---------- append-only ----------

def test_agent_decision_triggers_exist(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"trg_paper_agent_decisions_no_update",
            "trg_paper_agent_decisions_no_delete"} <= names


def test_update_is_rejected(conn):
    _record(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE paper_agent_decisions SET rationale = '改一下'")


def test_delete_is_rejected(conn):
    _record(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM paper_agent_decisions")


def test_insert_or_replace_is_rejected(conn):
    """`INSERT OR REPLACE` 靠**隐式 DELETE** 解唯一冲突 —— 那条 DELETE 不触发触发器。

    打开 `PRAGMA recursive_triggers = ON`（`store.db.connect` 里做）之后它才被拒。
    没有这一条，同 `(arm, asof)` 的一行会被静默覆盖，而「append-only」就成了空话。
    """
    _record(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO paper_agent_decisions (arm, asof, agent_kind,"
            " model_id, prompt_sha256, seed, context_sha256, spec_before_json,"
            " spec_after_json, n_trials, rejected_json, rationale, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ARM_AGENT, "2026-09-22", "manual", "manual", STAMP, 0, STAMP,
             "{}", "{}", 1, "[]", "覆盖试试", NOW))
    assert agent_spec.load_decisions(conn, ARM_AGENT)[0]["rationale"] == ""


def test_unique_arm_and_asof(conn):
    _record(conn)
    with pytest.raises(agent_spec.DecisionConflict) as ei:
        _record(conn)
    assert ei.value.arm == ARM_AGENT and ei.value.asof == "2026-09-22"
    assert ei.value.existing["decision_id"] == \
        agent_spec.load_decisions(conn, ARM_AGENT)[0]["decision_id"]


def test_same_day_on_another_arm_is_a_different_row(conn):
    """两臂的台账**互不串台**（幂等键是 `(arm, asof)`，不是 `asof`）。"""
    a = _record(conn, arm=ARM_AGENT, patch={"etf_target_pct": 12.0})
    b = _record(conn, arm=ARM_AGENT_RANDOM, patch={"etf_target_pct": 8.0})
    assert a != b
    assert agent_spec.current_spec(conn, ARM_AGENT, "2026-09-22")["etf_target_pct"] == 12.0
    assert agent_spec.current_spec(
        conn, ARM_AGENT_RANDOM, "2026-09-22")["etf_target_pct"] == 8.0


# ---------- 缺字段 / 非法取值一律拒写 ----------

@pytest.mark.parametrize("kwarg,field", [
    ("model_id", "model_id"),
    ("prompt", "prompt_sha256"),
    ("ctx", "context_sha256")])
def test_blank_fingerprints_are_rejected(conn, kwarg, field):
    """换模型 / 换提示词 / 换输入快照 = 换口径，必须留痕 —— 空串不算留痕。"""
    with pytest.raises(agent_spec.SpecViolation) as ei:
        _record(conn, **{kwarg: ""})
    assert ei.value.field == field
    assert agent_spec.load_decisions(conn, ARM_AGENT) == []


def test_unknown_agent_kind_is_rejected(conn):
    with pytest.raises(agent_spec.SpecViolation) as ei:
        _record(conn, kind="script")
    assert ei.value.field == "agent_kind"
    assert agent_spec.load_decisions(conn, ARM_AGENT) == []


def test_trial_budget_is_enforced_at_write_time(conn):
    with pytest.raises(agent_spec.SpecViolation):
        _record(conn, n_trials=agent_spec.MAX_TRIALS_PER_REVIEW + 1)
    assert agent_spec.load_decisions(conn, ARM_AGENT) == []


def test_rejected_trials_round_trip(conn):
    rejected = [{"field": "etf_target_pct", "value": 30.0, "reason": "高于上界"}]
    _record(conn, n_trials=2, rejected=rejected, rationale="试了两版")
    row = agent_spec.load_decisions(conn, ARM_AGENT)[0]
    assert row["rejected"] == rejected
    assert row["n_trials"] == 2
    assert row["spec_before"] == agent_spec.normalize_spec(
        agent_spec.AGENT_DEFAULT_SPEC)
    assert row["spec_after"]["etf_target_pct"] == 12.0


# ---------- 生效边界：`<=` 与 `<` ----------

def test_current_spec_is_the_latest_row_at_or_before_asof(conn):
    _record(conn, asof="2026-09-20", patch={"etf_target_pct": 12.0})
    _record(conn, asof="2026-09-25", patch={"etf_target_pct": 20.0})
    # 复审日**当天**生效（`<=`）—— 改成 `==` 会让非复审日全部回落默认值
    assert agent_spec.current_spec(
        conn, ARM_AGENT, "2026-09-25")["etf_target_pct"] == 20.0
    assert agent_spec.current_spec(
        conn, ARM_AGENT, "2026-09-24")["etf_target_pct"] == 12.0
    # 台账里一行都还没有 → 默认 spec（= arm-discipline-10 口径），不是「没有条文」
    assert agent_spec.current_spec(
        conn, ARM_AGENT, "2026-09-19") == agent_spec.AGENT_DEFAULT_SPEC
    assert agent_spec.current_spec(
        conn, ARM_AGENT, "2026-09-19") is not agent_spec.AGENT_DEFAULT_SPEC, \
        "必须返回副本 —— 调用方改它不许污染模块级默认值"


def test_spec_before_is_strictly_earlier(conn):
    """写下这一版的人要的是「我改之前是什么」⇒ 严格 `<`，不是 `<=`。"""
    _record(conn, asof="2026-09-20", patch={"etf_target_pct": 12.0})
    _record(conn, asof="2026-09-25", patch={"etf_target_pct": 20.0})
    assert agent_spec.spec_before(
        conn, ARM_AGENT, "2026-09-25")["etf_target_pct"] == 12.0
    assert agent_spec.spec_before(
        conn, ARM_AGENT, "2026-09-20") == agent_spec.AGENT_DEFAULT_SPEC
    assert agent_spec.spec_before(
        conn, ARM_AGENT, "2026-09-20") is not agent_spec.AGENT_DEFAULT_SPEC


def test_decision_on_is_an_exact_cell_not_the_latest(conn):
    _record(conn, asof="2026-09-20")
    assert agent_spec.decision_on(conn, ARM_AGENT, "2026-09-21") is None
    assert agent_spec.decision_on(
        conn, ARM_AGENT, "2026-09-20")["asof"] == "2026-09-20"


# ---------- 台账统计（报告里必须出现的两个数） ----------

def test_ledger_summary_counts_and_cadence(conn):
    assert agent_spec.ledger_summary(conn, ARM_AGENT, "2026-09-30") == {
        "arm": ARM_AGENT, "asof": "2026-09-30", "n_reviews": 0,
        "n_trials_total": 0, "n_rejected": 0,
        "max_trials_per_review": agent_spec.MAX_TRIALS_PER_REVIEW,
        "n_trials_last_review": 0,
        "cadence": agent_spec.AGENT_DEFAULT_SPEC["rebalance_cadence"],
        "first_asof": None, "last_asof": None, "last_decision_id": None}

    _record(conn, asof="2026-09-22", n_trials=3,
            rejected=[{"a": 1}, {"b": 2}], patch={"rebalance_cadence": 10})
    _record(conn, asof="2026-09-29", n_trials=2, rejected=[{"c": 3}],
            before=agent_spec.current_spec(conn, ARM_AGENT, "2026-09-29"))
    s = agent_spec.ledger_summary(conn, ARM_AGENT, "2026-09-30")
    assert (s["n_reviews"], s["n_trials_total"], s["n_rejected"]) == (2, 5, 3)
    assert s["first_asof"] == "2026-09-22" and s["last_asof"] == "2026-09-29"
    assert s["cadence"] == 10                      # 最近一版 spec 的复审节奏
    # `<= asof` 的过滤：把 asof 退到第一版之前，统计必须跟着退
    assert agent_spec.ledger_summary(conn, ARM_AGENT, "2026-09-21")["n_reviews"] == 0
    assert agent_spec.ledger_summary(conn, ARM_AGENT, "2026-09-22")["n_reviews"] == 1


# ---------- 复现性（同输入同输出） ----------

def test_reproducibility_is_none_when_never_tested(conn):
    """**没有一组四元组重复出现过** ⇒ 无法判定，不是「可复现」。"""
    _record(conn, asof="2026-09-22")
    rep = agent_spec.reproducibility(conn, ARM_AGENT)
    assert rep["n_groups_tested"] == 0
    assert rep["reproducible"] is None
    assert "无法判定" in rep["verdict"]
    assert rep["violations"] == []


def test_reproducibility_passes_when_same_input_gives_same_spec(conn):
    _record(conn, asof="2026-09-22", ctx=STAMP, patch={"etf_target_pct": 12.0})
    _record(conn, asof="2026-09-23", ctx=STAMP, patch={"etf_target_pct": 12.0})
    rep = agent_spec.reproducibility(conn, ARM_AGENT)
    assert rep["n_groups_tested"] == 1 and rep["reproducible"] is True
    assert rep["violations"] == []


def test_reproducibility_flags_divergence_without_averaging(conn):
    _record(conn, asof="2026-09-22", ctx=STAMP, patch={"etf_target_pct": 12.0})
    _record(conn, asof="2026-09-23", ctx=STAMP, patch={"etf_target_pct": 20.0})
    rep = agent_spec.reproducibility(conn, ARM_AGENT)
    assert rep["reproducible"] is False
    assert rep["n_groups_tested"] == 1
    (v,) = rep["violations"]
    assert v["key"]["context_sha256"] == STAMP
    assert v["asof"] == ["2026-09-22", "2026-09-23"]
    assert v["n_distinct_specs"] == 2
    assert "不可复现" in rep["verdict"]


def test_reproducibility_groups_by_the_whole_quadruple(conn):
    """换了模型或种子就是**另一组**，不是同一组的反例。"""
    _record(conn, asof="2026-09-22", ctx=STAMP, seed=0,
            patch={"etf_target_pct": 12.0})
    _record(conn, asof="2026-09-23", ctx=STAMP, seed=1,
            patch={"etf_target_pct": 20.0})
    rep = agent_spec.reproducibility(conn, ARM_AGENT)
    assert rep["n_groups_tested"] == 0 and rep["reproducible"] is None
    assert rep["n_rows"] == 2
