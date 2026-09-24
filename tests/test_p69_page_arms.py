"""P69 §T3：`/lab/paper` 的账目标签 / 颜色 / 顺序（修 P56 §8.4 的贴错标签）。

任务书 `docs/tasks/2026-09-24-p69-模拟操盘可见性.md` §T3 的判据：

| 判据 | 被摘掉后会红的实现 |
|---|---|
| 用例内**虚构**的 `arm-agent-xx-v9` 自动拿到紫色线 ＋ 人话标签 | 渲染代码里硬编码 4 个 id |
| `arm-agent` / `arm-agent-ds-v1` 显示「历史版本（已停飞）」灰线 | 停飞臂仍按在飞画紫线 |
| 通路 A（`executor=m2_channel_a`）与 LLM 臂、随机臂**靠线型分开** | 同色实线 ⇒ 三条线叠成一条 |
| LLM 臂标签写的是**模型名**，不是「智能体 spec 编排」 | 按 `arm=='agent'` 一刀切（P56 §8.4 的原文） |

夹具复用 `tests/test_labweb_paper.py::_fixture_db`（`paper init` + `step` + 决策循环）。
"""

import pytest

from stocklab.labweb import paper_data, paper_render
from stocklab.paper import store as paper_store
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, ARM_KIND_AGENT,
                                   EXECUTOR_AGENT_DECISION, EXECUTOR_CHANNEL_A,
                                   HALTED_LABEL, LIVE_KEY,
                                   PREREGISTERED_KEY)
from stocklab.store.db import connect
from tests.test_labweb_paper import LAST, NOW, START, _fixture_db

MODEL = "deepseek/deepseek-v4-pro"
PROMPT = "b" * 64
FICTIONAL = "arm-agent-xx-v9"


@pytest.fixture
def db(tmp_path):
    return _fixture_db(tmp_path)


def _track(path, asof=LAST):
    c = connect(path)
    try:
        return paper_data.track(c, asof)
    finally:
        c.close()


def _arm(data, account_id):
    return next(a for a in data["arms"] if a["account_id"] == account_id)


def _add_arm(path, account_id, *, arm=ARM_KIND_AGENT, params, nav=1000.0,
             date="2026-09-21"):
    """加一条只记净值的账户（**只为考渲染分档**，不参与任何口径计算）。"""
    c = connect(path)
    try:
        paper_store.insert_account(
            c, account_id=account_id, arm=arm, etf_target_pct=None,
            start_date=START, initial_cash=nav, initial_positions=[],
            initial_nav=nav, params={"initial_capital": nav, **params}, now=NOW)
        paper_store.insert_nav(
            c, account_id=account_id, date=date, cash=nav, positions=[],
            market_value=0.0, nav=nav, drawdown=0.0, cum_cost=0.0, cum_return=0.0,
            net_deposits=nav, index_300_level=None, index_300_asof=None, now=NOW)
    finally:
        c.close()


def _set_live(path, account_id, value):
    """把 `params.live` 写进 fixture（真库那条路走 `store/migrate` 的迁移）。"""
    c = connect(path)
    try:
        c.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        row = c.execute("SELECT params_json FROM paper_accounts WHERE account_id = ?",
                        (account_id,)).fetchone()
        import json
        params = json.loads(row["params_json"] or "{}")
        params[LIVE_KEY] = value
        c.execute("UPDATE paper_accounts SET params_json = ? WHERE account_id = ?",
                  (json.dumps(params, ensure_ascii=False, sort_keys=True), account_id))
        c.commit()
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# 判据 1：虚构的新版本账户 ⇒ 自动紫色线 + 人话标签（不改渲染代码）
# ══════════════════════════════════════════════════════════════════════

def test_t3_a_fictional_family_member_is_purple_and_in_the_family(db):
    """从没见过的 `arm-agent-xx-v9` ⇒ 紫色线 + 「LLM 操盘臂 · <模型> · xx-v9」。"""
    _add_arm(db, FICTIONAL, params={
        "executor": EXECUTOR_AGENT_DECISION,
        PREREGISTERED_KEY: {"model_id": MODEL, "prompt_sha256": PROMPT},
    })
    data = _track(db)
    arm = _arm(data, FICTIONAL)
    assert paper_render.in_agent_family(FICTIONAL)
    color, dash, width = paper_render.arm_style(arm)
    assert color == paper_render._AGENT_COLOR, "家族成员一律紫"
    assert paper_render.arm_style(arm) == paper_render._AGENT_STYLES["llm"], \
        "有预注册的 LLM 臂 = 实线（家族内的线型区分）"
    label = paper_render.arm_label(arm)
    assert label == f"LLM 操盘臂 · {MODEL} · xx-v9", label
    assert "口径未知" not in label and "spec 编排" not in label
    # 顺序：家族成员自动排进家族（固定五条之后），不靠列举
    order = [str(a["account_id"]) for a in paper_render._ordered(data["arms"])]
    assert order.index(FICTIONAL) > order.index("arm-discipline-15")


def test_t3_the_family_styles_are_three_distinct_line_types(db):
    """LLM 实线 / 随机虚线 / 通路 A 点线 —— **同色不同线型**，三条都认得出。"""
    _add_arm(db, "arm-agent-v1", params={
        "executor": EXECUTOR_CHANNEL_A,
        "plugin_hooks": ["m2_a1", "m2_a2", "m2_a3"],
        "strategy_version": "v1",
    })
    data = _track(db)
    llm = paper_render.arm_style(_arm(data, ARM_AGENT))
    rnd = paper_render.arm_style(_arm(data, ARM_AGENT_RANDOM))
    cha = paper_render.arm_style(_arm(data, "arm-agent-v1"))
    assert {llm[0], rnd[0], cha[0]} == {paper_render._AGENT_COLOR}, "同色"
    assert len({llm[1], rnd[1], cha[1]}) == 3, "线型必须两两不同（含实线的空串）"
    assert llm[1] == "" and rnd[1] != "" and cha[1] != ""


def test_t3_the_channel_a_arm_is_labelled_by_its_hooks_and_version(db):
    """通路 A 的标签写**插桩脚本名 + 策略版本**，不写「LLM」。"""
    _add_arm(db, "arm-agent-v1", params={
        "executor": EXECUTOR_CHANNEL_A,
        "plugin_hooks": ["m2_a1", "m2_a2", "m2_a3"],
        "strategy_version": "v1",
    })
    label = paper_render.arm_label(_arm(_track(db), "arm-agent-v1"))
    assert label == "通路A · 插桩脚本（m2_a1、m2_a2、m2_a3）· v1", label
    assert "LLM" not in label


def test_t3_an_llm_arm_is_not_labelled_as_the_spec_ledger(db):
    """P56 §8.4 的原文缺陷：`arm-agent-ds-*` 的 `arm` 也是 `agent`，不许被叫成 spec 编排。"""
    _add_arm(db, "arm-agent-ds-v9", params={
        "executor": EXECUTOR_AGENT_DECISION,
        PREREGISTERED_KEY: {"model_id": MODEL, "prompt_sha256": PROMPT},
    })
    label = paper_render.arm_label(_arm(_track(db), "arm-agent-ds-v9"))
    assert "智能体 spec 编排" not in label
    assert MODEL in label and label.endswith("ds-v9")


# ══════════════════════════════════════════════════════════════════════
# 判据 2：停飞臂 ⇒ 灰线 + 「历史版本（已停飞）」
# ══════════════════════════════════════════════════════════════════════

def test_t3_a_halted_arm_is_grey_and_says_so(db):
    """T2 要停飞的两条：灰线 + `HALTED_LABEL`（与 `paper agent show` 同一句）。"""
    _add_arm(db, "arm-agent-ds-v1", params={
        "executor": EXECUTOR_AGENT_DECISION,
        PREREGISTERED_KEY: {"model_id": MODEL, "prompt_sha256": PROMPT},
    })
    for aid in (ARM_AGENT, "arm-agent-ds-v1"):
        _set_live(db, aid, False)
    data = _track(db)
    for aid in (ARM_AGENT, "arm-agent-ds-v1"):
        arm = _arm(data, aid)
        assert paper_render.arm_style(arm) == paper_render._HALTED_STYLE, \
            f"{aid} 停飞后必须是灰线"
        assert HALTED_LABEL in paper_render.arm_label(arm)
    assert paper_render._HALTED_STYLE != paper_render._UNKNOWN_STYLE, \
        "「已停飞」是已知状态、「认不出」是未知状态 —— 两件事不许画成一样"


def test_t3_the_halted_arms_sink_below_the_live_ones(db):
    """阅读顺序：同一家族里**在飞的排在前**、停飞的排在后（再按 id）。"""
    _add_arm(db, "arm-agent-ds-v1", params={
        "executor": EXECUTOR_AGENT_DECISION,
        PREREGISTERED_KEY: {"model_id": MODEL, "prompt_sha256": PROMPT},
    })
    _set_live(db, "arm-agent-ds-v1", False)
    data = _track(db)
    order = [str(a["account_id"]) for a in paper_render._ordered(data["arms"])]
    assert order.index("arm-agent-ds-v1") > order.index(ARM_AGENT_RANDOM), \
        "停飞臂不许排在在飞臂前面"


def test_t3_a_halted_arm_is_not_read_as_missing_a_decision(db):
    """对照臂同轴表：停飞写「历史版本（已停飞）」，**不写「今日无决策」**。"""
    before = _cmp(_track(db))[ARM_AGENT]
    assert "今日无决策" in before["decision"]["note"]
    _set_live(db, ARM_AGENT, False)
    after = _cmp(_track(db))[ARM_AGENT]
    assert after["decision"]["halted"] is True
    assert HALTED_LABEL in after["decision"]["note"]
    assert "今日无决策" not in after["decision"]["note"]
    assert HALTED_LABEL in after["label"], "同轴表那一行的口径也要跟着改"


def _cmp(data):
    return {str(a["id"]): a for a in (data["comparison"].get("arms") or [])}


# ══════════════════════════════════════════════════════════════════════
# 判据 3：纪律臂与静态线的标签/颜色没有被顺手改掉
# ══════════════════════════════════════════════════════════════════════

def test_t3_the_existing_five_lines_keep_their_labels_and_styles(db):
    data = _track(db)
    assert paper_render.arm_label(_arm(data, "arm-now")) == "我 · 实盘账本镜像"
    assert paper_render.arm_label(_arm(data, "arm-hold")) == \
        "什么都不做 · 起跑日冻结快照"
    assert paper_render.arm_label(_arm(data, "arm-discipline-10")) == \
        "AI 纪律臂 · ETF 目标 10%"
    for aid in ("arm-now", "arm-hold", "arm-discipline-05",
                "arm-discipline-10", "arm-discipline-15"):
        assert paper_render.arm_style(_arm(data, aid)) == \
            paper_render._STYLES[aid], f"{aid} 的线型不该被动过"
    assert paper_render._UNKNOWN_STYLE == ("#5a6672", "4 3", 1.6), \
        "「认不出」那条兜底线型不许被顺手改掉"
