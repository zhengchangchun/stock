"""P37：`/lab/paper` 页面上的「智能体臂」一节（T7）。

页面**不重算**任何数字：这一段全部来自 `paper_data.agent_track`，而它又直接复用
`engine.agent_block`（与 `paper show` 同源）。所以这里的断言分两类：

1. 页面的数字 == 上游的同名字段（页面自己算一套就红）；
2. **`null` 与 `0` 在页面上长得不一样** —— 随机对照臂还没有决策时
   `delta_vs_random` 是「差分不存在」，不是「差分等于 0」。写成 `+0.00%`
   就把「还没出决策」读成了结论。

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
from stocklab.paper import agent_decide, agent_spec, engine as paper_engine
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, ARM_KIND_AGENT,
                                   ARM_KIND_AGENT_RANDOM,
                                   RULE_CITATIONS_AGENT_DECISION)
from stocklab.store.db import connect
from tests.test_labweb_paper import LAST, NEXT, NOW, _fixture_db

SECTION = "智能体臂（P52）：AI 操盘手"


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


def _add_agent_decision(path, *, asof, payload):
    """写一条操盘决策 + 推进该日（`arm-agent` 的成交现在只来自这里）。

    P52 起 `arm-agent` 不跑纪律条文（D-34）：它的成交必须能追溯到
    `paper_agent_decisions` 里当日那一条目标权重，否则一笔都不该有。
    """
    c = connect(path)
    try:
        c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
                  " created_at) VALUES (?,'light','{}',?)", (asof, NOW))
        sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
        for code in sorted(payload["decisions"], key=lambda d: d["code"]):
            c.execute("INSERT INTO candidate_members (snapshot_id, code, pool,"
                      " raw_score, adj_score, reason, risk_json, status, entered_at)"
                      " VALUES (?,?, 'short', 1.0, 1.0, '夹具', '{}', '观察中', ?)",
                      (sid, code["code"], NOW))
        c.commit()
        state = paper_engine.arm_state_for(c, ARM_AGENT, asof)
        pool = {"codes": [d["code"] for d in payload["decisions"]]}
        # 写入口要能定价**候选池里的**标的（不只是当前持仓）——
        # 否则一笔「买入一只新标的」的决策会因为「没有它的价」而被拒。
        marks = {**paper_engine.resolve_marks(c, set(pool["codes"]), asof),
                 **state["marks"]}
        validated = agent_decide.validate_payload(
            asof=asof, payload=payload, pool_codes=set(pool["codes"]),
            held_qty=state["positions"], marks=marks,
            total_assets=state["total_assets"])
        agent_decide.record_portfolio_decision(
            c, arm=ARM_AGENT, asof=asof, payload=validated, pool=pool,
            agent_kind=agent_spec.AGENT_KIND_LLM, model_id="test-model",
            prompt_sha256="p" * 64, seed=0, context_sha256="c" * 64, now=NOW)
        # P56 / D-50：AI 臂的日终由**决策循环**落（`paper step` 已让出它）——
        # 这里原来调 `paper_engine.step`，换成同一个执行者的入口，
        # 否则决策永远不会被执行（这正是 D-50 要挡的那条坑）。
        paper_engine.agent_run(c, asof, now=NOW)
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

def test_trades_by_the_agent_decision_are_counted_without_polluting_unknown(tmp_path):
    """`arm-agent` 的成交按**当日决策**的条文表单列，且不许落进「表外条文」。

    原判据数的是 `n_trades_by_spec`（P37：条文来自 spec 台账）。D-34 之后这条臂
    照着**当日那一条操盘决策**下单，于是计数该落在
    `RULE_CITATIONS_AGENT_DECISION` 上 —— 换的是条文表的来源，不是判据的形状。
    """
    path = _fixture_db(tmp_path)
    _add_agent_decision(path, asof=NEXT, payload={
        "asof": NEXT, "cash_pct": 90.0, "rationale": "夹具：买 10% 的 510300",
        "decisions": [{"code": "510300", "side": "buy", "target_weight_pct": 10.0,
                       "reason": "夹具要造一笔可归因的成交"}]})
    c = connect(path)
    try:
        ev = paper_data.ai_evidence(c, NEXT)
        last = paper_engine.arm_state_for(c, ARM_AGENT, NEXT)
    finally:
        c.close()
    cons = ev["consumption"]
    assert cons["n_trades_by_decision"] > 0, "夹具里 AI 臂应当按当日决策建仓"
    assert set(cons["decision_rules"]) <= set(RULE_CITATIONS_AGENT_DECISION.values())
    assert cons["unknown_rules"] == [], \
        "决策条文是**已登记**的，不许当成「表外（模型/插桩）」"
    assert last["positions"].get("510300"), "决策要真的落到持仓上"
    html = paper_render.ai_block(ev)
    assert "AI 操盘手用上了" in html
    assert "引用模型预测 0 条、引用插桩脚本 0 条" in html
    assert "RULE_CITATIONS_AGENT_DECISION" in html


def test_agent_arms_have_their_own_labels_and_styles(db):
    """AI 家族：按 `params.executor` + 台账分档（P69 §T3 替掉按 `arm` 一刀切）。

    ⚠️ P69 同步改写：`arm-agent` 原来是「AI 操盘手 · 每交易日一条决策」（P52 文案），
    但它**没有预注册** ⇒ 一条决策也拿不到 ⇒ 现文案如实写成「无预注册（内置占位臂）」。
    """
    data = _track(db)
    labels = {a["account_id"]: paper_render.arm_label(a) for a in data["arms"]}
    assert "无预注册" in labels[ARM_AGENT], "占位臂不许再被叫成「每交易日一条决策」"
    assert "智能体 spec 编排" not in labels[ARM_AGENT], "P56 §8.4 那个错标签不许回来"
    assert "随机对照" in labels[ARM_AGENT_RANDOM]
    assert "阶段 3" not in labels[ARM_AGENT_RANDOM], \
        "阶段 3 的说法已经作废（P52 起它真的下单）"
    styles = {a["account_id"]: paper_render.arm_style(a) for a in data["arms"]}
    assert styles[ARM_AGENT] != paper_render._UNKNOWN_STYLE
    assert styles[ARM_AGENT] != styles[ARM_AGENT_RANDOM], \
        "两条臂同色时必须靠线型分开（同色实线会被读成同一条）"
    assert styles[ARM_AGENT][0] == styles[ARM_AGENT_RANDOM][0]
    assert styles[ARM_AGENT_RANDOM][1] != ""      # random = 虚线


def test_agent_family_follows_the_disciplines_in_reading_order(db):
    """家族排在固定五条之后、且**不靠列举 id**（P69 §T3 的 `_ordered` 契约）。"""
    data = _track(db)
    order = [str(a["account_id"]) for a in paper_render._ordered(data["arms"])]
    assert order.index(ARM_AGENT) > order.index("arm-discipline-15")
    assert order.index(ARM_AGENT_RANDOM) == order.index(ARM_AGENT) + 1
    assert ARM_AGENT not in paper_render._DISPLAY_ORDER, \
        "家族不许再列举进 `_DISPLAY_ORDER` —— 列举就等于漏了将来新开的版本账户"
    assert paper_render.in_agent_family("arm-agent-xx-v9"), \
        "家族按前缀纳入 ⇒ 没见过的版本账户自动算家族成员"


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
    assert "AI 操盘手" in seg


# ══════════════════════════════════════════════════════════════════════
# P56 / D-50：「今日无决策」必须看得见，且与「有决策但不动手」不同形
# ══════════════════════════════════════════════════════════════════════

#: 结论句禁词（沿用 P48/P55 的措辞纪律）：新增一行不许把页面措辞带坏。
FORBIDDEN_WORDS = ("跑赢", "跑输", "优于", "劣于", "领先", "胜过")


def _comparison_html(path, asof=LAST) -> str:
    """只渲染**对照臂同轴表** —— 禁词纪律说的是这一段（整页别处另有既有措辞）。"""
    c = connect(path)
    try:
        return paper_render.comparison_block({"comparison": paper_data.track(c, asof)[
            "comparison"]})
    finally:
        c.close()


def test_p56_the_ai_row_says_today_has_no_decision(tmp_path):
    """`/lab/paper` 的 AI 行显式写「今日无决策」，且措辞里没有买卖建议词。"""
    path = _fixture_db(tmp_path)
    assert "今日无决策" in _html(path), "AI 行必须显式写出「今日无决策」（整页）"
    block = _comparison_html(path)
    assert "今日无决策" in block
    assert "没决定" in block and "不是「决定不动手」" in block, \
        "「无决策」与「有决策但不动手」必须**不同形**"
    for word in FORBIDDEN_WORDS:
        assert word not in block, f"对照块出现禁词 {word}"


def test_p56_the_ai_row_says_something_else_when_there_is_a_decision(tmp_path):
    """有决策的那条臂不再写「今日无决策」，而**没决策的那条照旧写**（两形并存）。"""
    path = _fixture_db(tmp_path, steps=False)
    _add_agent_decision(path, asof=LAST, payload={
        "asof": LAST, "cash_pct": 100.0, "rationale": "夹具：全现金也是一个决定",
        "decisions": []})
    c = connect(path)
    try:
        block = paper_data.track(c, LAST)["comparison"]
    finally:
        c.close()
    by_id = {a["id"]: a for a in block["arms"]}
    mine = by_id[ARM_AGENT]["decision"]
    theirs = by_id[ARM_AGENT_RANDOM]["decision"]
    # 两种形态**开头就不一样** —— 这是「不同形」的判据。
    # （有决策的那条会在正文里引用「今日无决策」来对比，所以不能拿子串否定。）
    assert mine["present"] is True and mine["note"].startswith("**今日有决策**")
    assert theirs["present"] is False and theirs["note"].startswith("**今日无决策**")
    # 两种形态在**渲染出来之后**也分得开
    html = _comparison_html(path)
    assert "今日有决策" in html and "今日无决策" in html
