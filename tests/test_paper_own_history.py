"""P84 / T3：`own_history` 块（`paper/own_history.py`）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 只含 `arm` 自己（别的臂的行不进） | 忘了按 `account_id` 过滤 |
| 切片 ≤20 / ≤10 / ≤5，且**升序** | 取头部（最老的几行）当「最近」 |
| `n_nav_days` / `n_trades` / `n_decisions` 是**全量** | 拿切片长度冒充历史长度 |
| 塞 `asof` 之后的行 → 块逐字节不变（PIT） | 少了 `date <= asof` |
| `payload_json` 坏掉 ⇒ `n_orders = 0` ＋ 点名，**不抛** | 直接 `json.loads` 炸掉整条链 |
| 空库 / 缺表 ⇒ 各计数 0，**不抛** | 夹具库上 `OperationalError` |
| 块内没有任何前向字段名（K5） | 往里塞 `confidence` / `predicted_*` |

P85 追加：`recent_reviews` / `facts` 两个子键（末尾追加、PIT、尺寸 ≤3 / ≤10，
既有子键逐位不变）。
"""

import json

import pytest

from stocklab.paper import own_history, review
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-25T16:00:00+08:00"
ASOF = "2026-09-24"
ARM = "arm-agent-ds-v2"
OTHER = "arm-agent-ds-v1"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type, active,"
              " added_at) VALUES ('600519','贵州茅台','sh','main','stock',1,?)", (NOW,))
    c.commit()
    yield c
    c.close()


def _nav(conn, date, *, arm=ARM, nav=20000.0, cum_cost=5.0, cum_return=0.0,
         drawdown=-0.01, net_deposits=20043.0):
    conn.execute(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,?,0.0,'[]',0.0,?,?,?,?,?,4400.0,?,?)",
        (arm, date, nav, drawdown, cum_cost, cum_return, net_deposits, date, NOW))


def _trade(conn, date, *, arm=ARM, code="600519", side="buy", qty=100,
           fill_price=1500.0, fee_total=5.0, slippage=1.25, rule="条文：理由",
           reason="因为所以"):
    cur = conn.execute(
        "INSERT INTO paper_trades (account_id, date, code, side, ref_price,"
        " fill_price, qty, commission, stamp_tax, transfer_fee, slippage_cost,"
        " fee_total, asset_class, rule_citation, reason, binding_json,"
        " price_source, price_asof, created_at)"
        " VALUES (?,?,?,?,?,?,?,0.0,0.0,0.0,?,?, 'stock',?,?,'{}','bars',?,?)",
        (arm, date, code, side, fill_price, fill_price, qty, slippage, fee_total,
         rule, reason, date, NOW))
    return int(cur.lastrowid)


def _decision(conn, asof, *, arm=ARM, payload=None, rejected="[]", trials=3,
              kind="portfolio", sha="a" * 64):
    conn.execute(
        "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
        " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
        " n_trials, rejected_json, rationale, created_at, decision_kind,"
        " payload_json) VALUES (?,?,'llm','m','p',0,?,'{}','{}',?,?,'r',?,?,?)",
        (arm, asof, sha, trials, rejected, NOW, kind,
         json.dumps({"decisions": payload} if payload is not None else {})))


# ---------- ① 只含自己 + 全量计数 ----------

def test_t3_the_block_only_contains_this_arm(conn):
    _nav(conn, "2026-09-23")
    _nav(conn, "2026-09-23", arm=OTHER)
    _trade(conn, "2026-09-23")
    _trade(conn, "2026-09-23", arm=OTHER, side="sell")
    _decision(conn, "2026-09-23", payload=[{"code": "600519"}])
    _decision(conn, "2026-09-23", arm=OTHER, payload=[])
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert blk["arm"] == ARM
    assert blk["n_nav_days"] == 1 and blk["n_trades"] == 1 and blk["n_decisions"] == 1
    assert [t["side"] for t in blk["trades"]] == ["buy"]
    assert [d["n_orders"] for d in blk["decisions"]] == [1]


def test_t3_counts_are_about_the_whole_history_not_the_slice(conn):
    for i in range(1, 31):                       # 30 天净值
        _nav(conn, f"2026-08-{i:02d}", nav=20000.0 + i)
    for i in range(1, 21):                       # 20 笔成交
        _trade(conn, f"2026-08-{i:02d}",
               code="600519", side="buy" if i % 2 else "sell")
    for i in range(1, 13):                       # 12 条决策
        _decision(conn, f"2026-08-{i:02d}", payload=[])
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert blk["n_nav_days"] == 30 and len(blk["nav_series"]) == 20
    assert blk["n_trades"] == 20 and len(blk["trades"]) == 10
    assert blk["n_decisions"] == 12 and len(blk["decisions"]) == 5
    assert blk["n_buy"] == 10 and blk["n_sell"] == 10
    # 升序，且最后一行 = asof 或之前最近一行（不是最老那几行）
    assert [r["date"] for r in blk["nav_series"]] == \
        [f"2026-08-{i:02d}" for i in range(11, 31)]
    assert blk["nav_series"][-1]["date"] == "2026-08-30"
    assert [t["trade_id"] for t in blk["trades"]] == list(range(11, 21))
    assert [d["asof"] for d in blk["decisions"]] == \
        [f"2026-08-{i:02d}" for i in range(8, 13)]


def test_t3_trade_rows_carry_the_instrument_name_and_rounding(conn):
    _trade(conn, "2026-09-23", fill_price=1500.123456, fee_total=5.55555,
           slippage=1.234567, rule="止损：收盘价跌破", reason="破了")
    conn.commit()
    t = own_history.own_history_block(conn, arm=ARM, asof=ASOF)["trades"][0]
    assert t["name"] == "贵州茅台"
    assert t["fill_price"] == 1500.1235           # round 4
    assert t["fee_total"] == 5.5556
    assert t["slippage_cost"] == 1.2346
    assert t["rule_citation"] == "止损：收盘价跌破"
    assert t["reason"] == "破了"
    assert isinstance(t["qty"], int)


def test_t3_an_unknown_code_yields_a_null_name(conn):
    _trade(conn, "2026-09-23", code="999999")
    conn.commit()
    assert own_history.own_history_block(
        conn, arm=ARM, asof=ASOF)["trades"][0]["name"] is None


# ---------- ② realized_fees_total 的两种口径 ----------

def test_t3_realized_fees_come_from_the_latest_nav_row(conn):
    _nav(conn, "2026-09-23", cum_cost=7.5)
    _nav(conn, "2026-09-24", cum_cost=12.25)
    _trade(conn, "2026-09-23", fee_total=5.0)
    conn.commit()
    assert own_history.own_history_block(
        conn, arm=ARM, asof=ASOF)["realized_fees_total"] == 12.25


def test_t3_without_any_nav_row_fees_are_summed_from_trades(conn):
    _trade(conn, "2026-09-23", fee_total=5.0)
    _trade(conn, "2026-09-24", fee_total=6.5)
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert blk["nav_series"] == [] and blk["n_nav_days"] == 0
    assert blk["realized_fees_total"] == 11.5


# ---------- ③ 决策块 ----------

def test_t3_decisions_report_the_full_context_sha_and_the_ledger_counts(conn):
    _decision(conn, "2026-09-23", payload=[{"code": "a"}, {"code": "b"}],
              rejected='[{"k":1},{"k":2}]', trials=4, kind="portfolio",
              sha="c" * 64)
    conn.commit()
    d = own_history.own_history_block(conn, arm=ARM, asof=ASOF)["decisions"][0]
    assert set(d) == {"decision_id", "asof", "decision_kind", "n_orders",
                      "context_sha256", "n_trials", "n_rejected"}
    assert d["n_orders"] == 2 and d["n_trials"] == 4 and d["n_rejected"] == 2
    assert d["context_sha256"] == "c" * 64        # 全 64 位，不截断
    assert d["decision_kind"] == "portfolio"


def test_t3_a_broken_payload_is_named_not_raised(conn):
    conn.execute(
        "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
        " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
        " n_trials, rejected_json, rationale, created_at, decision_kind,"
        " payload_json) VALUES (?,'2026-09-23','llm','m','p',0,?, '{}','{}',1,"
        " '[]','r',?,'portfolio',?)",
        (ARM, "d" * 64, NOW, "{不是 JSON"))
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert blk["decisions"][0]["n_orders"] == 0
    assert any("payload_json" in n for n in blk["notes"])
    assert blk["n_decisions"] == 1


def test_t3_a_negative_or_broken_rejected_json_counts_as_zero(conn):
    """`rejected_json` 不是数组 / 解析不出来 ⇒ 记 0（**不抛**）。"""
    for i, bad in enumerate(("{不是数组", '{"k":1}', "[]")):
        conn.execute(
            "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
            " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
            " n_trials, rejected_json, rationale, created_at, decision_kind,"
            " payload_json) VALUES (?,?,'llm','m','p',0,?, '{}','{}',1,"
            " ?, 'r',?,'portfolio','{}')",
            (ARM, f"2026-09-2{i + 1}", "e" * 64, bad, NOW))
    conn.commit()
    ds = own_history.own_history_block(conn, arm=ARM, asof=ASOF)["decisions"]
    assert [d["n_rejected"] for d in ds] == [0, 0, 0]
    assert all(d["n_orders"] == 0 for d in ds)


# ---------- ④ PIT ----------

def test_t3_rows_after_asof_do_not_change_the_block(conn):
    _nav(conn, "2026-09-23", cum_cost=3.0)
    _trade(conn, "2026-09-23")
    _decision(conn, "2026-09-23", payload=[])
    conn.commit()
    before = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    _nav(conn, "2026-09-25", cum_cost=99.0)
    _trade(conn, "2026-09-25")
    _decision(conn, "2026-09-25", payload=[{"code": "z"}])
    conn.commit()
    after = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert json.dumps(after, ensure_ascii=False, sort_keys=True) \
        == json.dumps(before, ensure_ascii=False, sort_keys=True)


# ---------- ⑤ 空 / 缺表 ----------

def test_t3_empty_tables_give_zeros_without_raising(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    try:
        blk = own_history.own_history_block(c, arm=ARM, asof=ASOF)
    finally:
        c.close()
    assert blk["nav_series"] == [] and blk["trades"] == [] and blk["decisions"] == []
    assert (blk["n_nav_days"], blk["n_trades"], blk["n_buy"], blk["n_sell"],
            blk["n_decisions"]) == (0, 0, 0, 0, 0)
    assert blk["realized_fees_total"] == 0.0


def test_t3_a_bare_database_without_any_table_is_also_fine(tmp_db):
    import sqlite3
    c = sqlite3.connect(tmp_db)
    try:
        blk = own_history.own_history_block(c, arm=ARM, asof=ASOF)
    finally:
        c.close()
    assert blk["n_nav_days"] == 0 and blk["n_trades"] == 0 and blk["n_decisions"] == 0


# ---------- ⑥ 只描述不预测 ----------

_FORWARD_KEYS = ("predict", "forecast", "target", "prob", "signal", "score",
                 "rating", "advice", "recommend", "kelly", "momentum",
                 "direction", "expected", "confidence")


def _all_keys(obj) -> list[str]:
    if isinstance(obj, dict):
        return [k for k in obj] + [x for v in obj.values() for x in _all_keys(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _all_keys(v)]
    return []


def test_t3_the_block_has_no_forward_looking_field_names(conn):
    _nav(conn, "2026-09-23")
    _trade(conn, "2026-09-23")
    _decision(conn, "2026-09-23", payload=[])
    conn.commit()
    for key in _all_keys(own_history.own_history_block(conn, arm=ARM, asof=ASOF)):
        for banned in _FORWARD_KEYS:
            assert banned not in key.lower(), f"自身历史块里出现了前向字段：{key}"


# ---------- ⑦ P85 / K5：recent_reviews 与 facts（只增两个子键） ----------

def _review(conn, asof, *, arm=ARM, lessons=()):
    """落一条复盘（走写入口 —— 读侧看到的就是它写下的那份）。"""
    return review.record_review(
        conn, arm=arm, asof=asof,
        payload={"asof": asof, "arm": arm, "kind": "daily", "items": [],
                 "lessons": list(lessons)},
        model_id="manual", prompt_sha256="p" * 64, context_sha256="e" * 64,
        now=NOW)


def test_p85_the_block_gains_exactly_two_new_keys_at_the_end(conn):
    """K8：既有子键**逐位不变**，两个新子键追加在末尾。"""
    _nav(conn, "2026-09-23")
    _trade(conn, "2026-09-23")
    _decision(conn, "2026-09-23", payload=[])
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert list(blk) == [
        "arm", "nav_series", "n_nav_days", "trades", "n_trades", "n_buy",
        "n_sell", "realized_fees_total", "decisions", "n_decisions", "notes",
        "recent_reviews", "facts"]
    # 旧子键的值也逐位不变（不是「键还在、内容换了」）
    assert blk["arm"] == ARM and blk["n_nav_days"] == 1 and blk["n_trades"] == 1
    assert blk["decisions"][0]["n_orders"] == 0
    assert len(blk["notes"]) == 3


def test_p85_recent_reviews_are_capped_at_three_and_ascending(conn):
    for i in range(1, 6):                        # 5 条复盘（09-21 … 09-25）
        _review(conn, f"2026-09-2{i}", lessons=[{"key": f"k{i}_x", "kind": "fact",
                                                 "text": "x"}])
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)   # ASOF = 09-24
    assert own_history.REVIEW_LIMIT == 3
    # 「最近 3 条」是在 `asof <= 09-24` 的四条里取尾部（09-25 那条看不见）
    assert [r["asof"] for r in blk["recent_reviews"]] == \
        ["2026-09-22", "2026-09-23", "2026-09-24"]
    assert set(blk["recent_reviews"][0]) == {
        "asof", "kind", "context_sha256", "n_items", "n_lessons",
        "items", "lessons"}


def test_p85_facts_are_capped_at_ten_and_only_for_the_own_arm(conn):
    """facts 是**逐臂**的：别的臂的 12 个 key 一个都不该出现在这条臂里。"""
    for d in ("2026-09-23", "2026-09-24"):
        _review(conn, d, arm=OTHER,
                lessons=[{"key": f"k{i:02d}", "kind": "fact", "text": "别的臂"}
                         for i in range(12)])
    conn.commit()
    assert own_history.FACT_LIMIT == 10
    assert own_history.own_history_block(conn, arm=ARM, asof=ASOF)["facts"] == []
    other = own_history.own_history_block(conn, arm=OTHER, asof=ASOF)
    assert len(other["facts"]) == 10 and other["recent_reviews"] != []


def test_p85_a_review_after_asof_does_not_change_the_block(conn):
    """PIT（K5）：在 `asof` **之后**插复盘 ⇒ 整块逐字节不变。"""
    _review(conn, "2026-09-23", lessons=[{"key": "cash_drag", "kind": "fact",
                                         "text": "旧"}])
    conn.commit()
    before = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert [r["asof"] for r in before["recent_reviews"]] == ["2026-09-23"]
    _review(conn, "2026-09-25", lessons=[{"key": "cash_drag", "kind": "fact",
                                         "text": "后见之明"},
                                        {"key": "later_only", "kind": "fact",
                                         "text": "z"}])
    conn.commit()
    after = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert json.dumps(after, ensure_ascii=False, sort_keys=True) \
        == json.dumps(before, ensure_ascii=False, sort_keys=True)
    # 反向对照：对 09-25 来说它是看得见的（不是「两条路都看不见」）
    later = own_history.own_history_block(conn, arm=ARM, asof="2026-09-25")
    assert [r["asof"] for r in later["recent_reviews"]] == \
        ["2026-09-23", "2026-09-25"]


def test_p85_an_empty_review_table_is_fine(conn):
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert blk["recent_reviews"] == [] and blk["facts"] == []


def test_p85_the_block_signature_gained_no_required_parameter(conn):
    """T3：`own_history_block` 只许**增加可选参数** —— 老调用点一字不改仍成立。"""
    import inspect
    sig = inspect.signature(own_history.own_history_block)
    required = [n for n, p in sig.parameters.items()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                and n != "conn"]
    assert required == ["arm", "asof"]


def test_p85_the_new_subkeys_carry_no_forward_looking_field_names(conn):
    _review(conn, "2026-09-23", lessons=[{"key": "cash_drag", "kind": "fact",
                                         "text": "现金拖累收益"}])
    _review(conn, "2026-09-24", lessons=[{"key": "cash_drag", "kind": "fact",
                                          "text": "现金拖累收益"}])
    conn.commit()
    blk = own_history.own_history_block(conn, arm=ARM, asof=ASOF)
    assert blk["facts"]                                     # 确实有东西可扫
    for key in _all_keys({"recent_reviews": blk["recent_reviews"],
                          "facts": blk["facts"]}):
        for banned in _FORWARD_KEYS:
            assert banned not in key.lower(), f"复盘块里出现了前向字段：{key}"
