"""P69 §T4：「AI 操盘手」专节的三块（操盘记录 / 盈利净值 / 口径史）。

任务书 `docs/tasks/2026-09-24-p69-模拟操盘可见性.md` §T4。

| 判据 | 被摘掉后会红的实现 |
|---|---|
| 台账里每一条决策都在「操盘记录」里逐行出现 | 只画净值不列决策（用户原话：「看不到记录」） |
| 当日成交笔数 == 库里那天该臂的成交行数 | 数成整条臂的成交总数 |
| 每臂净值/累计收益 == `engine.build_report` 的同名字段（逐位） | 页面自己再算一套 |
| `Δ vs 随机` == 两条累计收益相减；随机臂自身写「基准自身」 | 把 `None` 渲染成 `0.00%` |
| 通路 A 的「缺决策」是**不适用**，不是「缺」 | 拿台账判据去量不走台账的臂 |
| 口径史三档都在（LLM 版本 / 通路 A 插桩版本 / spec 逐键差异） | 只说「口径会变」，不写哪天变过 |

夹具复用 `tests/test_p69_agent_arms_scope.py` 的建库与写决策助手（同一套离线口径）。
"""

import json

import pytest

from stocklab.labweb import paper_data, paper_render
from stocklab.paper import agent_spec, engine, store as paper_store
from stocklab.plugin import lifecycle as plugin_lifecycle
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM,
                                   EXECUTOR_AGENT_DECISION, EXECUTOR_CHANNEL_A,
                                   HALTED_LABEL)
from stocklab.store.db import connect
from tests.test_p69_agent_arms_scope import (ARM_A, ARM_B, NOW, START, _decide,
                                             _enroll, _init, _random, _run,
                                             make_db)

MODEL = "deepseek/deepseek-v4-pro"
PROMPT = "b" * 64


@pytest.fixture
def db(tmp_path, capsys):
    """一个跑过 `init` + 两条 LLM 臂 + 随机臂 + 一条通路A 臂 + 一天日终的库。"""
    path = make_db(tmp_path)
    _init(path, capsys)
    for name in (ARM_A, ARM_B):
        _enroll(path, capsys, name)
    for name in (ARM_A, ARM_B):
        _decide(path, capsys, name, START, tmp_path)
    _random(path, capsys, START)
    _add_channel_a(path, "arm-agent-v1")
    _add_plugin_chain(path, "m2_a1")
    _add_spec_row(path)
    _run(path, capsys=capsys)
    return path


def _add_channel_a(path, account_id="arm-agent-v1"):
    c = connect(path)
    try:
        paper_store.insert_account(
            c, account_id=account_id, arm="agent", etf_target_pct=None,
            start_date=START, initial_cash=20000.0, initial_positions=[],
            initial_nav=20000.0,
            params={"initial_capital": 20000.0, "executor": EXECUTOR_CHANNEL_A,
                    "plugin_hooks": ["m2_a1", "m2_a2"],
                    "strategy_version": "v1"}, now=NOW)
        paper_store.insert_nav(
            c, account_id=account_id, date=START, cash=11320.0, positions=[],
            market_value=8680.0, nav=20000.0, drawdown=0.0, cum_cost=0.0,
            cum_return=0.0, net_deposits=20000.0, index_300_level=None,
            index_300_asof=None, now=NOW)
    finally:
        c.close()


def _add_plugin_chain(path, plugin_id="m2_a1"):
    """给通路 A 的插桩塞一条版本链（`plugin_scripts` ＋ `plugin_audit`）。

    离线夹具里没有内置脚本种子 ⇒ 这两张表是空的；真库那边由
    `plugin submit/sandbox/approve` 那条路写。这里直接调 `plugin.store`
    的**写库函数**（不是手敲 SQL），让「页面读得到版本链」这件事可测。
    """
    from stocklab.plugin import store as plugin_store
    c = connect(path)
    try:
        first = plugin_store.insert_script(
            c, plugin_id=plugin_id, version="1.0.0", source_text="def run(ctx): pass",
            note="夹具 v1", now=NOW)
        plugin_store.insert_audit(c, script_id=first, action="submit", actor="fixture",
                                  reason=None, now=NOW)
        plugin_store.insert_audit(c, script_id=first, action="sandbox_pass",
                                  actor="sandbox", reason="fixture", now=NOW)
        plugin_store.insert_audit(c, script_id=first, action="approve", actor="fixture",
                                  reason="fixture", now=NOW)
        second = plugin_store.insert_script(
            c, plugin_id=plugin_id, version="1.0.1",
            source_text="def run(ctx): return {}", note="夹具 v2", now=NOW)
        plugin_store.insert_audit(c, script_id=second, action="submit", actor="fixture",
                                  reason=None, now=NOW)
        return first, second
    finally:
        c.close()


def _add_spec_row(path, account_id=ARM_AGENT, asof="2026-09-15"):
    """写一条 **spec** 台账行（`decision_kind='spec'`）—— 口径史第三档的靶子。"""
    c = connect(path)
    try:
        before = agent_spec.spec_before(c, account_id, asof)
        agent_spec.record_decision(
            c, arm=account_id, asof=asof, spec_before=before,
            spec_after=agent_spec.apply_spec(before, {"etf_target_pct": 12.0}),
            agent_kind=agent_spec.AGENT_KIND_MANUAL, model_id="manual",
            prompt_sha256=agent_spec.MANUAL_PROMPT_SHA256, seed=0,
            context_sha256="c" * 64, now=NOW, n_trials=1, rejected=[],
            rationale="手写一版：ETF 目标改 12%")
    finally:
        c.close()


def _ops(path, asof=START):
    c = connect(path)
    try:
        data = paper_data.track(c, asof)
        report = engine.build_report(c, asof)
        perf = paper_data.performance(c, asof)
        navs = {aid: paper_store.load_nav(c, aid, asof=asof)
                for aid in [a["account_id"] for a in data["arms"]]}
        trades = conn_count(c, "paper_trades")
        return data["agent_ops"], data, report, perf, navs, trades
    finally:
        c.close()


def conn_count(conn, table):
    """`(account_id, date) → 笔数`（`Counter` 的缺键即 0）。"""
    from collections import Counter
    if table == "paper_trades":
        return Counter((str(r["account_id"]), str(r["date"]))
                       for r in conn.execute("SELECT account_id, date FROM paper_trades"))
    return Counter()


# ══════════════════════════════════════════════════════════════════════
# 第 1 块：操盘记录（逐日一行）
# ══════════════════════════════════════════════════════════════════════

def test_t4_every_ledger_decision_shows_up_as_a_record_row(db):
    """台账里**每一条**决策都在记录表里 —— 这是用户说的「看不到操盘记录」。"""
    ops, *_ = _ops(db)
    c = connect(db)
    try:
        ledger = [(str(r["arm"]), str(r["asof"]))
                  for r in c.execute("SELECT arm, asof FROM paper_agent_decisions"
                                     " WHERE decision_kind = 'portfolio'")]
    finally:
        c.close()
    assert ledger, "夹具要先有决策"
    got = {(r["account_id"], r["date"]) for r in ops["records"]}
    assert set(ledger) <= got, f"台账有、记录表没有：{sorted(set(ledger) - got)}"


def test_t4_a_record_row_carries_the_decision_summary(db):
    """决策摘要：方向 ＋ 标的 ＋ 目标权重%（载荷里怎么写的，页面上就怎么显示）。"""
    ops, *_ = _ops(db)
    row = next(r for r in ops["records"]
               if r["account_id"] == ARM_A and r["date"] == START)
    assert row["producer"] == MODEL, "产出者是台账里的 model_id 原样字面量"
    assert row["decision_id"] is not None
    assert row["weights"], "决策摘要不能是空的"
    w = row["weights"][0]
    assert set(w) == {"code", "side", "target_weight_pct", "reason"}
    assert w["code"] == "510300" and w["side"] == "buy"
    assert w["target_weight_pct"] == 10.0
    c = connect(db)
    try:
        payload = json.loads(c.execute(
            "SELECT payload_json FROM paper_agent_decisions"
            " WHERE arm = ? AND asof = ?", (ARM_A, START)).fetchone()["payload_json"])
    finally:
        c.close()
    assert row["weights"][0]["target_weight_pct"] == \
        payload["decisions"][0]["target_weight_pct"], "摘要的值必须原样来自载荷"


def test_t4_the_trade_count_of_a_row_matches_the_ledger(db):
    """当日成交笔数 == `paper_trades` 里那天那条臂的行数（不是整条臂的总数）。"""
    ops, _, _, _, _, trades = _ops(db)
    for r in ops["records"]:
        assert r["n_trades"] == trades.get((r["account_id"], r["date"]), 0), \
            f"{r['date']}/{r['account_id']} 的成交笔数对不上"


def test_t4_a_record_row_carries_the_nav_columns_verbatim(db):
    """当日净值 / 累计收益 == `paper_nav_daily` 的同名列（**读库，不重算**）。"""
    ops, _, _, _, navs, _ = _ops(db)
    for r in ops["records"]:
        row = next((n for n in navs[r["account_id"]] if str(n["date"]) == r["date"]), None)
        assert row is not None, f"{r['date']}/{r['account_id']} 没有净值行却出现在记录表里"
        assert (r["nav"], r["cum_return"]) == (row["nav"], row["cum_return"])


def test_t4_a_missing_decision_is_marked_and_a_present_one_is_not(db):
    """有没有决策：有 ⇒ `missing_decision is False`；没有且该有 ⇒ `True`。"""
    ops, *_ = _ops(db)
    for r in ops["records"]:
        if r["decision_id"] is not None:
            assert r["missing_decision"] is False
        elif r["ledger_driven"]:
            assert isinstance(r["missing_decision"], bool)
        else:
            assert r["missing_decision"] is None, "不走台账的臂：不适用，不是「缺」"


def test_t4_a_channel_a_arm_is_not_judged_by_the_ledger(db):
    """通路 A 的「缺决策」= **不适用**（它的决策不在 `paper_agent_decisions` 里）。"""
    ops, *_ = _ops(db)
    chan = [r for r in ops["records"] if r["account_id"] == "arm-agent-v1"]
    assert chan, "夹具里的通路A 臂要有净值行"
    for r in chan:
        assert r["ledger_driven"] is False
        assert r["missing_decision"] is None


# ══════════════════════════════════════════════════════════════════════
# 第 2 块：盈利 / 净值（含 Δ vs 随机）
# ══════════════════════════════════════════════════════════════════════

def test_t4_the_nav_table_matches_build_report_field_by_field(db):
    """每臂的净值 / 累计收益 / 回撤 / 成本 / 持仓 == `build_report` 的同名字段。"""
    ops, _, report, perf, _, _ = _ops(db)
    by_id = {str(a["account_id"]): a for a in report["accounts"]}
    for row in ops["arms"]:
        src = by_id[row["account_id"]]
        assert row["nav"] == src["nav"]
        assert row["cum_return"] == src["cum_return"]
        assert row["max_drawdown"] == src["max_drawdown"]
        assert row["cum_cost"] == src["cum_cost"]
        assert row["n_positions"] == len(src["positions"])
        assert row["excess_vs_index_300"] == src["excess_vs_index_300"]


def test_t4_excess_vs_hold_comes_from_the_performance_block(db):
    """「相对不动」== `paper_data.performance` 的同名字段（同一个减法，不重算）。"""
    ops, _, _, perf, _, _ = _ops(db)
    for row in ops["arms"]:
        assert row["excess_vs_hold"] == (perf["excess_vs_hold"] or {}).get(
            row["account_id"])


def test_t4_delta_vs_random_is_a_subtraction_and_is_null_for_the_random_arm(db):
    """`Δ(AI − 随机)` = 两条累计收益相减；随机臂自身 `None` + 「基准自身」。"""
    ops, *_ = _ops(db)
    by_id = {a["account_id"]: a for a in ops["arms"]}
    random_ret = by_id[ARM_AGENT_RANDOM]["cum_return"]
    assert by_id[ARM_AGENT_RANDOM]["delta_vs_random"] is None
    assert "基准自身" in by_id[ARM_AGENT_RANDOM]["delta_vs_random_note"]
    for aid in (ARM_A, ARM_AGENT):
        row = by_id[aid]
        if row["cum_return"] is None or random_ret is None:
            assert row["delta_vs_random"] is None
            assert "不存在" in row["delta_vs_random_note"]
        else:
            assert row["delta_vs_random"] == round(row["cum_return"] - random_ret, 6)


def test_t4_a_halted_arm_stays_in_the_nav_table_but_is_marked(db):
    """停飞臂**仍列出**（历史口径保留），只是标成「已停飞」+ 不画成在飞。"""
    c = connect(db)
    try:
        c.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        row = c.execute("SELECT params_json FROM paper_accounts WHERE account_id = ?",
                        (ARM_AGENT,)).fetchone()
        params = json.loads(row["params_json"] or "{}")
        params["live"] = False
        c.execute("UPDATE paper_accounts SET params_json = ? WHERE account_id = ?",
                  (json.dumps(params, ensure_ascii=False, sort_keys=True), ARM_AGENT))
        c.commit()
    finally:
        c.close()
    ops, *_ = _ops(db)
    row = next(a for a in ops["arms"] if a["account_id"] == ARM_AGENT)
    assert row["live"] is False
    assert row["nav"] is not None, "停飞不等于从表里消失"


# ══════════════════════════════════════════════════════════════════════
# 第 3 块：口径史（三档并列不合并）
# ══════════════════════════════════════════════════════════════════════

def test_t4_the_llm_version_chain_is_read_from_the_account_rows(db):
    """LLM 档：每条版本账户的 `model_id` / `prompt_sha256`（换模型 = 开新账户）。"""
    ops, *_ = _ops(db)
    rows = {r["account_id"]: r for r in ops["caliber"]["llm_versions"]}
    assert set(rows) == {ARM_A, ARM_B}, "无预注册的臂不进这一档"
    for aid in (ARM_A, ARM_B):
        assert rows[aid]["model_id"] == MODEL
        assert rows[aid]["prompt_sha256"] == PROMPT
        assert rows[aid]["live"] is True
    assert rows[ARM_A]["n_decisions"] == 1
    assert rows[ARM_A]["first_asof"] == START


def test_t4_the_channel_a_version_chain_is_read_from_the_plugin_tables(db):
    """通路 A 档：按账户的 `plugin_hooks` 逐个列版本链 + 审核事件。"""
    ops, *_ = _ops(db)
    chains = ops["caliber"]["channel_a_versions"]
    assert {c["hook"] for c in chains} == {"m2_a1", "m2_a2"}
    assert {c["account_id"] for c in chains} == {"arm-agent-v1"}
    m1 = next(c for c in chains if c["hook"] == "m2_a1")
    assert m1["strategy_version"] == "v1"
    assert [v["version"] for v in m1["versions"]] == ["1.0.0", "1.0.1"], \
        "版本链按 `script_id` 升序列出（哪版先入库）"
    for v in m1["versions"]:
        assert set(v) >= {"script_id", "version", "state", "created_at", "events"}
    first = m1["versions"][0]
    assert [e["action"] for e in first["events"]] == ["submit", "sandbox_pass", "approve"]
    # 状态一律走 `plugin.lifecycle.script_state`（与打分内核同一个函数）——
    # 用例不硬编码状态名，而是拿同一个函数对拍，页面自己判状态就红。
    conn = connect(db)
    try:
        assert first["state"] == plugin_lifecycle.script_state(conn, first["script_id"])
    finally:
        conn.close()
    # `m2_a2` 在夹具里没有脚本 ⇒ 空链也要**如实给出来**（不是省略这一个 hook）
    m2 = next(c for c in chains if c["hook"] == "m2_a2")
    assert m2["versions"] == []


def test_t4_the_spec_history_is_a_key_by_key_diff(db):
    """spec 档：`spec_before → spec_after` 的**逐键**差异（只列变了的键）。"""
    ops, *_ = _ops(db)
    rows = ops["caliber"]["spec_diffs"]
    assert len(rows) == 1
    assert rows[0]["account_id"] == ARM_AGENT
    changes = {c["key"]: (c["before"], c["after"]) for c in rows[0]["changes"]}
    assert changes == {"etf_target_pct": (10.0, 12.0)}, changes
    assert "12" in rows[0]["rationale"]


def test_t4_a_portfolio_decision_does_not_appear_in_the_spec_history(db):
    """`decision_kind='portfolio'` 的操盘决策**不是** spec 变更（两档不许混）。"""
    ops, *_ = _ops(db)
    assert all(r["account_id"] != ARM_A for r in ops["caliber"]["spec_diffs"])


# ══════════════════════════════════════════════════════════════════════
# 渲染：三块都在页面上
# ══════════════════════════════════════════════════════════════════════

def test_t4_the_page_renders_the_three_blocks(db):
    c = connect(db)
    try:
        html = paper_render.paper_page(paper_data.track(c, START),
                                       base="/lab", built_at=NOW)
    finally:
        c.close()
    for probe in ("AI 操盘手", "操盘记录（逐日", "盈利 / 净值（每臂一行）",
                  "策略调整史", "Δ vs 随机", "随机对照"):
        assert probe in html, probe
    # 决策原文可展开：理由与逐条权重都在（折叠区里也算「在」）
    assert "夹具" in html or "分散" in html
    assert "510300" in html
    # 口径史三档的细节进 `more()`
    assert "LLM 版本" in html and "插桩版本" in html and "spec 台账" in html
    # 通路 A 那一档不许被写成「缺决策」
    assert "不适用" in html and "该臂的决策不走台账" in html


def test_t4_the_page_never_ranks_or_recommends(db):
    """本页只并列：不排名、不出现买卖建议词。"""
    c = connect(db)
    try:
        html = paper_render.paper_page(paper_data.track(c, START),
                                       base="/lab", built_at=NOW)
    finally:
        c.close()
    for banned in ("建议买", "建议卖", "推荐", "更值得", "最优臂", "应当买入"):
        assert banned not in html, f"页面上出现了买卖建议/排名措辞：{banned}"
