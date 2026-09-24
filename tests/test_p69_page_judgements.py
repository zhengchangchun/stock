"""P69 §T5：页面判据（自跑，不采信实现者自报）。

任务书 `docs/tasks/2026-09-24-p69-模拟操盘可见性.md` §T5：

| 判据 | 被摘掉后会红的实现 |
|---|---|
| `GET /lab/paper` HTTP 200 | 新节抛异常 ⇒ 整页 500 |
| 台账里的决策行**全在**页面上（逐条点名） | 只画汇总不列逐行 |
| 每条在飞臂的净值/收益数字 == `build_report` 的同名字段（逐位） | 页面自己格式化错了 / 又算了一套 |
| 「今日无决策/缺决策」与「有决策」**开头就不同形** | 合成一句话 |
| 停飞臂显示「历史版本（已停飞）」且**不算缺决策** | 停飞被读成「今天没决定」 |
| 页面上不出现买卖建议 / 排名措辞 | 加了「推荐」这类词 |

夹具复用 `tests/test_p69_ai_operator_section.py` 的那份（两条 LLM 臂 ＋ 随机臂 ＋
通路 A 臂 ＋ 一天日终 ＋ spec 台账）。
"""

import http.client
import json
import threading

from stocklab.labweb import paper_data, paper_render
from stocklab.labweb.render import money, ratio_pct
from stocklab.paper import engine
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, HALTED_LABEL,
                                   LIVE_KEY)
from stocklab.store.db import connect
from tests.test_p69_agent_arms_scope import ARM_A, NOW, START
from tests.test_p69_ai_operator_section import db  # noqa: F401  （复用夹具）

SECTION = "AI 操盘手"
#: 结论句禁词（沿用 P48/P55 的措辞纪律，与 `test_agent_section_has_no_ranking_words` 同源）。
BANNED = ("推荐", "最优", "冠军", "第一名", "应该买", "建议买", "建议卖", "更值得")


def _track(path, asof=START):
    c = connect(path)
    try:
        return paper_data.track(c, asof)
    finally:
        c.close()


def _html(path, asof=START):
    return paper_render.paper_page(_track(path, asof), base="/lab", built_at=NOW)


# ══════════════════════════════════════════════════════════════════════
# HTTP 200
# ══════════════════════════════════════════════════════════════════════

def test_t5_the_page_serves_http_200(db, loopback_http):  # noqa: F811
    from stocklab.labweb import app as labapp

    server = labapp.make_server(host="127.0.0.1", port=0, db_path=db)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/lab/paper")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        assert res.status == 200
        for probe in ("AI 操盘手", "操盘记录（逐日", "盈利 / 净值（每臂一行）",
                      "策略调整史"):
            assert probe in body, probe
    finally:
        conn.close()
        server.shutdown()
        server.server_close()


# ══════════════════════════════════════════════════════════════════════
# 台账行全在页面上（逐条点名）
# ══════════════════════════════════════════════════════════════════════

def test_t5_every_ledger_row_is_named_on_the_page(db):  # noqa: F811
    c = connect(db)
    try:
        ledger = [tuple(r) for r in c.execute(
            "SELECT arm, asof FROM paper_agent_decisions"
            " WHERE decision_kind = 'portfolio' ORDER BY asof, arm")]
    finally:
        c.close()
    assert len(ledger) >= 3, f"夹具要有台账行，实测 {ledger}"
    html = _html(db)
    for arm, asof in ledger:
        assert arm in html, f"台账行 {arm}/{asof} 的账户名不在页面上"
        assert asof in html, f"台账行 {arm}/{asof} 的日期不在页面上"


# ══════════════════════════════════════════════════════════════════════
# 逐位对齐：页面上的数 == `build_report` 的同名字段
# ══════════════════════════════════════════════════════════════════════

def test_t5_live_arm_numbers_match_build_report_character_for_character(db):  # noqa: F811
    """每条在飞 AI 臂：页面上的净值 / 累计收益 == `build_report` 的同一字段。"""
    c = connect(db)
    try:
        data = paper_data.track(c, START)
        report = engine.build_report(c, START)
    finally:
        c.close()
    html = paper_render.paper_page(data, base="/lab", built_at=NOW)
    by_id = {str(a["account_id"]): a for a in report["accounts"]}
    live = [a for a in data["agent_ops"]["arms"] if a.get("live", True)]
    assert live, "夹具里要有在飞臂"
    for row in live:
        src = by_id[row["account_id"]]
        assert row["nav"] == src["nav"] and row["cum_return"] == src["cum_return"]
        assert money(row["nav"]) in html, f"{row['account_id']} 的净值不在页面上"
        assert ratio_pct(row["cum_return"]) in html, \
            f"{row['account_id']} 的累计收益不在页面上"


# ══════════════════════════════════════════════════════════════════════
# 「缺决策」/「有决策」不同形
# ══════════════════════════════════════════════════════════════════════

def test_t5_a_missing_decision_and_a_present_one_do_not_share_a_shape(db):  # noqa: F811
    """「今日无决策」与「今日有决策」**开头就不同形**（P56 §2 的既有纪律）。

    用同轴表那一档来钉：它在 `note` 的**开头**就分岔，合成一句话就红。
    """
    data = _track(db)
    notes = {str(a["id"]): (a.get("decision") or {}).get("note")
             for a in data["comparison"]["arms"] if a.get("decision")}
    assert notes, "同轴表里要有 AI 臂的决策档"
    has = [n for n in notes.values() if n and n.startswith("**今日有决策**")]
    none = [n for n in notes.values() if n and n.startswith("**今日无决策**")]
    assert has, "夹具里当天那两条 LLM 臂有决策"
    assert none, "没决策的臂要写明「今日无决策」（不是「决定不动手」）"
    assert not set(n[:8] for n in has) & set(n[:8] for n in none), \
        "两种形态开头不许相同"


def test_t5_the_records_table_distinguishes_the_four_states(db):  # noqa: F811
    """四种状态各自成词：有决策 / 缺决策 / 无决策 / 不适用。"""
    ops = _track(db)["agent_ops"]
    states = set()
    for r in ops["records"]:
        if r["ledger_driven"] is False:
            states.add("na")
        elif r["decision_id"] is not None:
            states.add("has")
        elif r["missing_decision"]:
            states.add("missing")
        else:
            states.add("none")
    assert "has" in states, "夹具要有决策行"
    assert states <= {"has", "missing", "none", "na"}
    html = _html(db)
    assert "不适用" in html and "该臂的决策不走台账" in html


# ══════════════════════════════════════════════════════════════════════
# 停飞臂：显示「历史版本（已停飞）」且不算缺决策
# ══════════════════════════════════════════════════════════════════════

def test_t5_a_halted_arm_reads_as_halted_and_not_as_missing(db):  # noqa: F811
    c = connect(db)
    try:
        c.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        row = c.execute("SELECT params_json FROM paper_accounts WHERE account_id = ?",
                        (ARM_AGENT,)).fetchone()
        params = json.loads(row["params_json"] or "{}")
        params[LIVE_KEY] = False
        c.execute("UPDATE paper_accounts SET params_json = ? WHERE account_id = ?",
                  (json.dumps(params, ensure_ascii=False, sort_keys=True), ARM_AGENT))
        c.commit()
    finally:
        c.close()

    assert HALTED_LABEL in _html(db), "停飞臂要在页面上标出来"
    ops = _track(db)["agent_ops"]
    halted = [a for a in ops["arms"] if not a.get("live", True)]
    assert [a["account_id"] for a in halted] == [ARM_AGENT]
    for r in ops["records"]:
        if r["account_id"] == ARM_AGENT:
            assert r["missing_decision"] is not True, \
                "停飞臂不许被算成「缺决策」—— 它不是今天没决定"
    # 同轴表那一行的口径也要跟着改
    cmp_arm = next(a for a in _track(db)["comparison"]["arms"]
                   if str(a["id"]) == ARM_AGENT)
    assert HALTED_LABEL in cmp_arm["label"]
    assert cmp_arm["decision"]["halted"] is True
    assert "今日无决策" not in cmp_arm["decision"]["note"]


# ══════════════════════════════════════════════════════════════════════
# 禁令词：新节一起扫
# ══════════════════════════════════════════════════════════════════════

def test_t5_the_new_section_carries_no_ranking_or_advice_words(db):  # noqa: F811
    html = _html(db)
    for banned in BANNED:
        assert banned not in html, f"页面上出现了买卖建议/排名措辞：{banned}"
    # 「不做排名」这句要在，且整页仍然只并列
    assert "并行对照，不排名" in html
    ops_html = html.split(SECTION, 1)[1]
    assert "不做排名" in html or "不排名" in html
    for banned in BANNED:
        assert banned not in ops_html


def test_t5_the_new_section_does_not_promise_a_conclusion(db):  # noqa: F811
    """样本远不足 120 交易日 ⇒ 这一段必须带着门禁措辞，不许写成结论。"""
    html = _html(db)
    assert "样本远不足 120 交易日" in html
    assert "只是读数" in html
