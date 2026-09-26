"""P81：`/lab/paper` 的「智能体臂」一节出现**意图 vs 落地对账表**与**可下手域**行。

页面**不重算**任何数字：对账表来自 `engine.agent_block.reconciliation`、
可下手域来自 `build_report` 给 AI 臂加的 `tradable_domain`（都由 `paper_data.track`
透传）。所以断言分两类：

1. 页面上**出现**那张表与那一行（否则「AI 想买什么、为什么没买成」在页面上依旧不可见，
   而这是本站的全部目的）；
2. 数字与上游同名字段一致、缺数据时写「取不到」而**不是 0**（页面既有纪律）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| AI 段有「意图腿数 / 成交笔数 / 未成交腿」表头与逐臂的行 | 只显示写死的那条臂 |
| 未成交腿的 code/action/reason 出现在表里 | 把理由留在库里不给页面读 |
| 「池内可下手 X/Y 只」与报告数据块同值 | 渲染层自己再算一遍 |
| 空台账 ⇒ 「取不到」＋ 说明，**不出现「0 只」** | 把「没有决策」渲染成「全都落地了」 |
"""

from __future__ import annotations

import pytest

from stocklab.labweb import paper_data, paper_render
from stocklab.paper import engine as paper_engine
from stocklab.paper.config import ARM_AGENT
from stocklab.store.db import connect
from tests.test_labweb_paper import LAST, NOW, _fixture_db
from tests.test_labweb_paper_agent import _add_agent_decision

#: 池内**买不起一手**的那只（一手 ¥12.5 万+）：用它造一条「说了没做到」的腿。
EXPENSIVE = "600519"
PRICE_600519 = 1251.24


@pytest.fixture
def db(tmp_path):
    return _fixture_db(tmp_path)


def _html(path, asof=LAST) -> str:
    c = connect(path)
    try:
        data = paper_data.track(c, asof)
    finally:
        c.close()
    return paper_render.paper_page(data, base="/lab", built_at=NOW)


def _domain(path, asof=LAST) -> dict:
    c = connect(path)
    try:
        rep = paper_engine.build_report(c, asof)
    finally:
        c.close()
    return next(a for a in rep["accounts"]
                if a["account_id"] == ARM_AGENT)["tradable_domain"]


def _add_expensive_leg(path):
    """给夹具补一只**贵**标的（池内、有价、一手买不起）＋写一条带它的决策。"""
    c = connect(path)
    try:
        c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sh','main','stock',?)", (EXPENSIVE, EXPENSIVE, NOW))
        c.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                  " adj_mode, source, fetched_at)"
                  " VALUES (?,?,'1251.24','1251.24','1251.24','1251.24',100,'none',"
                  " 'x',?)", (EXPENSIVE, LAST, NOW))
        c.commit()
    finally:
        c.close()
    total = None
    c = connect(path)
    try:
        total = paper_engine.arm_state_for(c, ARM_AGENT, LAST)["total_assets"]
    finally:
        c.close()
    legs = [{"code": "510300", "side": "buy",
             "target_weight_pct": round(500.0 / total * 100.0, 2),
             "reason": "夹具：买得起一手（会成交）"},
            {"code": EXPENSIVE, "side": "buy",
             "target_weight_pct": round(500.0 / total * 100.0, 2),
             "reason": "夹具：目标 ≈ 一手以下（说了做不到）"}]
    weights = round(sum(d["target_weight_pct"] for d in legs), 6)
    _add_agent_decision(path, asof=LAST, payload={
        "asof": LAST, "cash_pct": round(100.0 - weights, 6), "rationale": "夹具：P81",
        "decisions": legs})


def test_p81_the_ai_section_shows_the_reconciliation_table(db):
    """有决策 ⇒ 表的表头与逐臂的行都在，未成交腿点名 code 与理由。"""
    _add_expensive_leg(db)
    html = _html(db)
    for head in ("意图腿数", "成交笔数", "未成交腿", "evals 行数"):
        assert head in html, head
    assert ARM_AGENT in html
    assert EXPENSIVE in html, "未成交腿的 code 必须出现在表里"
    assert "一手以下" in html or "说了做不到" in html, "理由要看得见"
    # 可下手域那行 == 报告数据块（同一份投影）。
    domain = _domain(db)
    assert domain["available"] is True
    assert f"池内可下手 {domain['n_tradable']}/{domain['n_pool_codes']} 只" in html
    assert f"买不起：{EXPENSIVE}" in html
    assert domain["n_untradable"] == 1 and domain["n_tradable"] == \
        domain["n_pool_codes"] - 1


def test_p81_the_ai_section_says_unknown_rather_than_zero(db):
    """空台账 ⇒ 「取不到」＋ 说明；**不出现**「池内可下手 0 只」这种伪结论。"""
    html = _html(db)
    assert "意图 vs 落地" in html
    assert "取不到" in html
    assert "没有任何一条" in html
    assert "池内可下手 0" not in html
    # 那一段仍然渲染（不能因为缺数据就把整节吞掉）。
    assert "智能体臂（P52）：AI 操盘手" in html
