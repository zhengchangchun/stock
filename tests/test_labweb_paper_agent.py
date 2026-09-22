"""P37：`/lab/paper` 页面上的「智能体臂」一节（T7）。

页面**不重算**任何数字：这一段全部来自 `paper_data.agent_track`，而它又直接复用
`engine.agent_block`（与 `paper show` 同源）。所以这里的断言分两类：

1. 页面的数字 == 上游的同名字段（页面自己算一套就红）；
2. **`null` 与 `0` 在页面上长得不一样** —— 阶段 3 之前 `delta_vs_random` 是
   「差分不存在」，不是「差分等于 0」。写成 `+0.00%` 就把「还没接线」读成了结论。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 页面上有当前 spec / 止损线 / spec 指纹 | 只写「智能体臂在跑」 |
| 差分显示「不存在」且**不出现 `+0.00%`** | 把 `None` 渲染成 0 |
| 台账行渲染出 `decision_id` / 试错 / 被拒 / 理由 | 历史表只列 spec |
| 缺表（老库）→ 段内说明原因，**页面仍 200** | 直接 `sqlite3.OperationalError` 打 500 |
| `n_trades_by_spec` 与库里的成交行数一致 | 数「表外条文」时把 agent 条文也算进去 |
| 两条 agent 臂有各自的标签与线型 | 落入「口径未知」的灰虚线 |
"""

import http.client
import re
import threading

import pytest

from stocklab.labweb import paper_data, paper_render
from stocklab.paper import agent_spec
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, ARM_KIND_AGENT,
                                   ARM_KIND_AGENT_RANDOM, RULE_CITATIONS_AGENT)
from stocklab.store.db import connect
from tests.test_labweb_paper import LAST, NOW, _fixture_db

SECTION = "智能体臂（P37）：条文的数字可改"


@pytest.fixture
def db(tmp_path):
    return _fixture_db(tmp_path)


def _track(path, asof=LAST):
    c = connect(path)
    try:
        return paper_data.track(c, asof)
    finally:
        c.close()


def _html(path, asof=LAST) -> str:
    return paper_render.paper_page(_track(path, asof), base="/lab", built_at=NOW)


def _set_spec(path, *, asof="2026-09-18", patch=None, n_trials=1, rejected=None,
              arm=ARM_AGENT, rationale="手写一版"):
    c = connect(path)
    try:
        agent_spec.record_decision(
            c, arm=arm, asof=asof,
            spec_before=agent_spec.spec_before(c, arm, asof),
            spec_after=agent_spec.apply_spec(
                agent_spec.spec_before(c, arm, asof), patch or {"etf_target_pct": 12.0}),
            agent_kind=agent_spec.AGENT_KIND_MANUAL, model_id="manual",
            prompt_sha256=agent_spec.MANUAL_PROMPT_SHA256, seed=0,
            context_sha256="c" * 64, now=NOW, n_trials=n_trials,
            rejected=rejected, rationale=rationale)
    finally:
        c.close()


def _make_legacy_db(path):
    """把 P37 的两样东西去掉，做一份**「还没前滚到 P37」**的库。

    去掉的正是 P37 新增的部分：台账表、两条 agent 账户，以及这两条臂名下的成交与
    净值行。为什么必须**一起**去掉 —— `paper_accounts` 有 agent 行却没有台账表是
    不可能的（那条臂的参数就是从台账读的），构造它只会测到一个人造态；而留着
    agent 的成交行，又会让人在「旧库」上读到 `n_trades_by_spec > 0`（正文里说
    「spec 条文在跑」，但那本台账已经不存在了）。

    `paper_*` 是 append-only，所以这里要先摘掉那三条 DELETE 触发器：这是测试里
    唯一一次绕过它，目的是让「旧库」这个场景可构造。
    """
    c = connect(path)
    try:
        c.execute("DROP TABLE paper_agent_decisions")
        for name in ("trg_paper_accounts_no_delete", "trg_paper_trades_no_delete",
                     "trg_paper_nav_daily_no_delete"):
            c.execute(f"DROP TRIGGER {name}")
        for table in ("paper_accounts", "paper_trades", "paper_nav_daily"):
            c.execute(f"DELETE FROM {table} WHERE account_id LIKE 'arm-agent%'")
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0] == 5
        assert c.execute("SELECT COUNT(*) FROM paper_trades WHERE account_id = ?",
                         ("arm-agent",)).fetchone()[0] == 0
    finally:
        c.close()


def test_agent_track_says_so_when_the_ledger_table_is_missing(tmp_path):
    """老库缺表 → `available: False` + 原因。页面**不外滚 schema**、不 500。"""
    path = _fixture_db(tmp_path)
    _make_legacy_db(path)
    c = connect(path)
    try:
        got = paper_data.agent_track(c, LAST)
        # `build_report` 也要活着：老库上没有 agent 账户 ⇒ 不该去读那张表
        assert paper_data.track(c, LAST)["available"] is True
        assert paper_data.ai_evidence(c, LAST)["consumption"]["n_trades_by_spec"] == 0
    finally:
        c.close()
    assert got["available"] is False
    assert agent_spec.TABLE_DECISIONS in got["reason"]
    assert "P37" in got["reason"]
    assert set(got["change_space"]) == set(agent_spec.SPEC_SCHEMA)
    assert got["default_spec"] == agent_spec.AGENT_DEFAULT_SPEC
    html = _html(path)
    assert SECTION in html                 # 段还在，只是里面写「没法回答」
    assert "这一段没法回答" in html
    assert agent_spec.TABLE_DECISIONS in html
    assert "<svg" in html                  # 别的段照常渲染


# ---------- 取数：与 `paper show` 同源 ----------

def test_agent_track_is_the_engine_block_plus_page_only_fields(db):
    from stocklab.paper.engine import agent_block

    c = connect(db)
    try:
        block = agent_block(c, LAST)
        got = paper_data.agent_track(c, LAST)
    finally:
        c.close()
    shared = set(block) & set(got)
    assert shared, "两边没有共同字段 ⇒ 页面在另算一套"
    for key in shared:
        assert got[key] == block[key], f"页面上的 {key} 与 `paper show` 不一致"
    assert {"available", "default_spec", "counter_arm_n_reviews", "reproducibility",
            "present"} <= set(got)
    assert got["available"] is True and got["present"] is True
    assert got["default_spec"] == agent_spec.AGENT_DEFAULT_SPEC
    assert got["counter_arm_n_reviews"] == 0


# ---------- 首屏：spec、止损线、差分 ----------

def test_page_shows_the_current_spec_and_its_stop_line(db):
    data = _track(db)
    ev = data["agent"]
    html = _html(db)
    assert SECTION in html
    assert "当前 spec：" in html
    assert f'{ev["spec"]["etf_target_pct"]:g}' in html
    assert paper_render.num(ev["stop_loss_line"], 2) in html
    assert agent_spec.spec_sha256(ev["spec"])[:12] in html
    assert f'{ev["n_reviews"]} 次复审' in html
    assert f'每次上限 {ev["max_trials_per_review"]}' in html


def test_page_renders_missing_delta_as_missing_not_as_zero(db):
    """阶段 3 之前：差分**不存在**。写成 `+0.00%` 就是把「没接线」读成了结论。"""
    html = _html(db)
    assert "与随机对照臂的差分" in html
    assert "<b>不存在</b>" in html
    assert "不是 0" in html
    assert ARM_AGENT_RANDOM in html
    # 这一段里不许出现把 null 当成 0 的痕迹
    seg = html.split(SECTION, 1)[1].split("</section>", 1)[0]
    assert "+0.00%" not in seg and "0.00%（" not in seg


def test_ledger_history_is_rendered_with_the_trials_evidence(db):
    html = _html(db)
    assert "台账里还没有任何一行" in html       # 默认 spec ≠ 没有条文
    _set_spec(db, n_trials=2,
              rejected=[{"field": "etf_target_pct", "value": 30.0,
                         "reason": "高于上界"}],
              rationale="把 ETF 目标提到 12%")
    html = _html(db)
    assert "把 ETF 目标提到 12%" in html
    assert "台账里还没有任何一行" not in html
    assert "被拒" in html and "试错" in html
    assert "append-only" in html


def test_change_space_on_the_page_is_the_schema(db):
    html = _html(db)
    for name in agent_spec.SPEC_SCHEMA:
        assert f"<code>{name}</code>" in html
    assert "只许收紧" in html
    assert "不夹紧" in html


def test_reproducibility_says_undecided_until_tested(db):
    html = _html(db)
    assert "复现性：无法判定" in html
    assert "还没被检验" in html
    assert "复现性：可复现" not in html


def test_reproducibility_passes_once_the_same_input_gives_the_same_spec(db):
    _set_spec(db, asof="2026-09-17", patch={"etf_target_pct": 12.0})
    _set_spec(db, asof="2026-09-18", patch={"etf_target_pct": 12.0})
    c = connect(db)
    try:
        rep = agent_spec.reproducibility(c, ARM_AGENT)
    finally:
        c.close()
    assert rep["reproducible"] is True
    assert "复现性：1 组同指纹的复审给出了同一个 spec —— 通过。" in _html(db)


# ---------- 消费明细：spec 条文 vs 表外条文 ----------

def test_trades_by_the_agent_rule_book_are_counted_without_polluting_unknown(db):
    c = connect(db)
    try:
        ev = paper_data.ai_evidence(c, LAST)
    finally:
        c.close()
    cons = ev["consumption"]
    assert cons["n_trades_by_spec"] > 0, "夹具里 arm-agent 起跑日应当建仓"
    assert set(cons["spec_rules"]) <= set(RULE_CITATIONS_AGENT.values())
    assert cons["unknown_rules"] == [], \
        "spec 条文是**已登记**的，不许当成「表外（模型/插桩）」"
    html = paper_render.ai_block(ev)
    assert "智能体 spec 臂用上了" in html
    assert "引用模型预测 0 条、引用插桩脚本 0 条" in html
    assert "落在 <code>paper/config.RULE_CITATIONS_AGENT</code> 内（智能体 spec）" \
        in html


# ---------- 标签 / 线型 / 顺序 ----------

def test_agent_arms_have_their_own_labels_and_styles(db):
    data = _track(db)
    labels = {a["account_id"]: paper_render.arm_label(a) for a in data["arms"]}
    assert labels[ARM_AGENT] == "AI 智能体臂 · spec 台账"
    assert "随机对照" in labels[ARM_AGENT_RANDOM]
    styles = {a["account_id"]: paper_render.arm_style(a) for a in data["arms"]}
    assert styles[ARM_AGENT] != paper_render._UNKNOWN_STYLE
    assert styles[ARM_AGENT] != styles[ARM_AGENT_RANDOM], \
        "两条臂同色时必须靠线型分开（同色实线会被读成同一条）"
    assert styles[ARM_AGENT][0] == styles[ARM_AGENT_RANDOM][0]
    assert styles[ARM_AGENT_RANDOM][1] != ""      # random = 虚线
    order = paper_render._DISPLAY_ORDER
    assert order.index(ARM_AGENT) > order.index("arm-discipline-15")
    assert order.index(ARM_AGENT_RANDOM) == order.index(ARM_AGENT) + 1


def test_arm_kind_fallback_still_labels_unknown_ids(db):
    """认不出的 id → 按 kind 给一句人话；两个 kind 都不许掉进「口径未知」。"""
    for kind in (ARM_KIND_AGENT, ARM_KIND_AGENT_RANDOM):
        lab = paper_render.arm_label({"account_id": "arm-agent-x", "arm": kind})
        assert "口径未知" not in lab
    assert "口径未知" in paper_render.arm_label(
        {"account_id": "arm-discipline-99", "arm": "discipline"})


# ---------- 空库分支 & 路由 ----------

def test_empty_store_still_renders_the_agent_section(tmp_path):
    """没有净值 ≠ 不告诉用户台账状态（T7 验收项 2）。"""
    path = _fixture_db(tmp_path, steps=False)
    html = paper_render.paper_page(_track(path), base="/lab", built_at=NOW)
    assert "还没有模拟盘净值" in html
    assert SECTION in html
    assert "当前 spec：" in html


def test_route_serves_the_agent_section(tmp_path, loopback_http):
    from stocklab.labweb import app as labapp

    path = _fixture_db(tmp_path)
    server = labapp.make_server(host="127.0.0.1", port=0, db_path=path)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/lab/paper")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        assert res.status == 200
        assert SECTION in body
        assert ARM_AGENT in body and ARM_AGENT_RANDOM in body
        # 页面里不得出现被二次转义的标签（ERROR_DIARY #50）
        for needle in ("&lt;a href", "&lt;span", "&lt;code&gt;"):
            assert needle not in body
    finally:
        conn.close()
        server.shutdown()
        server.server_close()


def test_agent_section_has_no_ranking_words(db):
    html = _html(db)
    for banned in ("推荐", "最优", "冠军", "第一名", "应该买"):
        assert banned not in html
    seg = html.split(SECTION, 1)[1].split("</section>", 1)[0]
    assert re.search(r"不做排名|不排名|并列", html), "「不做排名」这句不能丢"
    assert "不是「AI 会操盘」" in seg
