"""P85 / T6：复盘台账（`paper/review.py`）与教训回注。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 迁移建表 + 两触发器 + 索引，重入返回 `[]` | 第二次调用重复建 / 建不全 |
| `DELETE` / `UPDATE` 被触发器拒 | 忘了 `_P85_TRIGGERS` |
| `INSERT OR REPLACE` 只在 `recursive_triggers=ON` 下被拦（**反向自检**） | 以为触发器天然管得住隐式删行 |
| 8 类非法载荷各自 exit 2 **且零写入** | 先写再校验 / 校验漏一种指针 |
| 读数差 0.01 ⇒ 拒（「读数不许编」的牙齿） | 只校验字段名、不比对值 |
| 重复 `(arm, asof, kind)` ⇒ exit 1、零写入 | 用 `INSERT OR REPLACE` 覆盖历史 |
| `derive_facts`：1 次不进、2 次（不同 asof）进 | 把单次观察当「已确认」 |
| `text` 不一致 ⇒ `conflict: true` 且两条都列 | 取平均 / 猜一条 |
| >10 条按 K4 截断 ＋ `n_facts_truncated` | 静默截断 |
| 同库两次调用逐字节相同（确定性） | 混进时间戳 / 自增 id |
| `recent_reviews` / `facts` 受 PIT 约束（决策上下文只看**早于**决策日的复盘） | 少了 `asof <` 过滤 / 把当天写的复盘也塞进当天上下文 |
| `own_history` 既有子键逐位不变（K8） | 顺手重排 / 改掉旧子键 |
| `NON_GOALS` 末尾是 K6 的**逐字**文案 | 只改实现、没改「写给它看的禁令」 |
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from stocklab.cli.main import main
from stocklab.paper import agent_context, engine, own_history, review
from stocklab.store.db import connect
from stocklab.store.migrate import (ensure_schema, init_db,
                                    migrate_p85_agent_reviews)

NOW = "2026-09-25T16:00:00+08:00"
D0, D1, D2, D3 = "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"
ARM = "arm-agent"
OTHER = "arm-agent-random"

#: K6 的**逐字**文案（照任务书 §0.5 抄 —— 从实现里取就是同义反复）。
K6_NON_GOAL = (
    "复盘只写文字与读数：教训（facts）是**经验陈述**，不许当成参数改动的载体 ——"
    "五字段白名单、成本口径、整手口径、熔断阈值都不在复盘的触达范围内"
)

#: P84 定下的三条（P85 只许在它们**之后**追加）。
P84_NON_GOALS: tuple[str, ...] = (
    "不动成本口径 / PIT 判据 / 整手口径 / 白名单 / append-only 纪律",
    "不扩大变更空间本身（`SPEC_SCHEMA` 的区间不是可以改的字段）",
)


# ---------- 夹具 ----------

def _seed(path) -> None:
    """夹具库：市场横截面 ＋ 账户 ＋ 净值/成交/决策（够四种指针各核一次）。"""
    init_db(path)
    c = connect(path)
    c.execute("INSERT INTO instruments (code, name, market, board, type, active,"
              " added_at) VALUES ('000333','美的集团','sh','main','stock',1,?)", (NOW,))
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in (D1, D2, D3)])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW)
         # D0 只是给 D1 一个「前一交易日」（`breadth` 的涨跌基准），不是交易日。
         for code, series in {"000333": {D0: 87.0, D1: 86.0, D2: 85.0, D3: 84.0},
                              "600036": {D0: 41.0, D1: 40.0, D2: 39.5, D3: 39.0},
                              "sh000300": {D1: 4450.04, D2: 4400.0, D3: 4439.14},
                              "sh000905": {D1: 7550.0, D2: 7500.0, D3: 7561.59}}
         .items() for d, v in series.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES (?,'deposit',20000.0,'本金',?)", (D1, NOW))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES (?,'000333','buy',86.80,100,5.09,'首笔',?)",
              (D1, NOW))
    c.commit()
    # 五条静态臂 + AI 家族（`init_accounts` 给 AI 臂建账户行，但不落净值行）。
    engine.init_accounts(c, start_date=D1, now=NOW)
    c.commit()
    for d, nav, cash, cum_cost, cum_return in ((D1, 20000.0, 20000.0, 0.0, 0.0),
                                               (D2, 19500.0, 10000.0, 12.5, -0.025),
                                               (D3, 19600.0, 12000.0, 20.0, -0.02)):
        c.execute(
            "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
            " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
            " index_300_level, index_300_asof, created_at)"
            " VALUES (?,?,?,'[]',?,?,0.0,?,?,20043.0,4400.0,?,?)",
            (ARM, d, cash, nav - cash, nav, cum_cost, cum_return, d, NOW))
    for date in (D1, D2, D3):
        c.execute(
            "INSERT INTO paper_trades (account_id, date, code, side, ref_price,"
            " fill_price, qty, commission, stamp_tax, transfer_fee, slippage_cost,"
            " fee_total, asset_class, rule_citation, reason, binding_json,"
            " price_source, price_asof, created_at)"
            " VALUES (?,?,'000333','buy',86.0,86.5,100,5.0,0,0,1.0,6.0,'stock',"
            " '条文','理由','{}','bars',?,?)", (ARM, date, date, NOW))
    # 每条臂每一天各一条决策 —— 四种指针在 D1/D2/D3 都能核到。
    for arm, asof in ((ARM, D1), (ARM, D2), (ARM, D3), (OTHER, D3)):
        c.execute(
            "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
            " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
            " n_trials, rejected_json, rationale, created_at, decision_kind,"
            " payload_json) VALUES (?,?,'llm','m','p',0,?, '{}','{}',1,'[]','r',?,"
            " 'portfolio','{}')", (arm, asof, "a" * 64, NOW))
    c.commit()
    c.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p85.db"
    _seed(path)
    return path


@pytest.fixture
def conn(db):
    c = connect(db)
    yield c
    c.close()


def _nav_values(conn, date, *, arm=ARM) -> dict:
    row = conn.execute("SELECT * FROM paper_nav_daily WHERE account_id = ? AND date = ?",
                       (arm, date)).fetchone()
    return dict(row)


def _breadth(conn, asof) -> dict:
    from stocklab.paper import market_view
    return market_view.market_block(conn, asof=asof)["breadth"]


def _nav_id(conn, asof, *, arm=ARM):
    return int(conn.execute(
        "SELECT decision_id FROM paper_agent_decisions WHERE arm = ? AND asof = ?",
        (arm, asof)).fetchone()[0])


def _trade_id(conn, asof, *, arm=ARM):
    return int(conn.execute(
        "SELECT trade_id FROM paper_trades WHERE account_id = ? AND date = ?",
        (arm, asof)).fetchone()[0])


def _items(conn, asof, *, arm=ARM, metric="cum_return"):
    """四类指针各一条，读数**从库里现读**（不是手抄的字面量）。"""
    values = _nav_values(conn, asof, arm=arm)
    breadth = _breadth(conn, asof)
    return [
        {"claim": f"{asof} 那天有一条决策", "evidence":
            {"kind": "decision", "decision_id": _nav_id(conn, asof, arm=arm)}},
        {"claim": f"{asof} 那天有一笔成交", "evidence":
            {"kind": "trade", "trade_id": _trade_id(conn, asof, arm=arm)}},
        {"claim": f"{asof} 的 {metric}", "evidence":
            {"kind": "metric", "metric": metric, "date": asof,
             "value": float(values[metric])}},
        {"claim": f"{asof} 的涨跌家数", "evidence":
            {"kind": "market", "field": "breadth.up_ratio",
             "date": breadth["asof"], "value": float(breadth["up_ratio"])}},
    ]


def _payload(conn, asof, *, arm=ARM, lessons=(), items=None, metric="cum_return"):
    return {"asof": asof, "arm": arm, "kind": "daily",
            "items": _items(conn, asof, arm=arm, metric=metric) if items is None
                     else items,
            "lessons": list(lessons)}


def _write(conn, asof, *, arm=ARM, lessons=(), sha="c" * 64, kind="daily",
           items=None, metric="cum_return"):
    return review.record_review(
        conn, arm=arm, asof=asof, kind=kind,
        payload=_payload(conn, asof, arm=arm, lessons=lessons, items=items,
                         metric=metric),
        model_id="manual", prompt_sha256="p" * 64, context_sha256=sha, now=NOW)


def _lesson(key, text, kind="fact"):
    return {"key": key, "kind": kind, "text": text}


def _n_rows(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM paper_agent_reviews").fetchone()[0])


# ---------- T1 迁移 ----------

def test_t1_migration_builds_table_triggers_and_index(db):
    c = connect(db)
    try:
        names = {str(r["name"]) for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('trigger','index')"
            " AND tbl_name = 'paper_agent_reviews'")}
        cols = [str(r["name"]) for r in c.execute(
            "PRAGMA table_info(paper_agent_reviews)")]
    finally:
        c.close()
    assert cols == ["review_id", "arm", "asof", "kind", "model_id",
                    "prompt_sha256", "context_sha256", "n_items", "n_lessons",
                    "payload_json", "created_at"]
    assert {"trg_paper_agent_reviews_no_update",
            "trg_paper_agent_reviews_no_delete",
            "idx_paper_agent_reviews_arm_asof"} <= names


def test_t1_second_call_returns_empty(db):
    c = connect(db)
    try:
        assert migrate_p85_agent_reviews(c) == []
        assert migrate_p85_agent_reviews(c) == []
    finally:
        c.close()


def test_t1_a_dropped_trigger_is_repaired(db):
    c = connect(db)
    try:
        c.execute("DROP TRIGGER trg_paper_agent_reviews_no_delete")
        assert migrate_p85_agent_reviews(c) == ["paper_agent_reviews.triggers"]
        assert migrate_p85_agent_reviews(c) == []
    finally:
        c.close()


def test_t1_ensure_schema_is_reentrant(db):
    """写库入口的统一守护：第二次前滚对本表**零动作**（K1 / 判据 1）。"""
    c = connect(db)
    try:
        assert migrate_p85_agent_reviews(c) == []
    finally:
        c.close()
    assert ensure_schema(db) == []
    assert ensure_schema(db) == []


# ---------- T2 校验与写入 ----------

def test_t2_a_valid_review_is_written(conn):
    receipt = _write(conn, D2, lessons=[_lesson("cash_drag", "现金拖累收益")])
    assert receipt["written"] is True and receipt["review_id"] == 1
    assert receipt["n_items"] == 4 and receipt["n_lessons"] == 1
    row = conn.execute("SELECT * FROM paper_agent_reviews").fetchone()
    assert row["arm"] == ARM and row["asof"] == D2 and row["kind"] == "daily"
    assert json.loads(row["payload_json"])["lessons"][0]["key"] == "cash_drag"


@pytest.mark.parametrize("case", [
    "missing_evidence", "bad_decision_id", "bad_trade_id", "metric_asof_future",
    "metric_not_whitelisted", "metric_value_off_by_0.01", "market_field_unknown",
    "unknown_top_key", "unknown_lesson_key", "evidence_missing_key",
    "evidence_extra_key", "bad_lesson_key", "bad_lesson_kind", "empty_claim",
    "lesson_text_too_long", "kind_not_daily", "arm_mismatch", "asof_mismatch",
    "duplicate_lesson_key", "other_arms_decision",
])
def test_t2_illegal_payloads_are_rejected_with_zero_writes(conn, case):
    payload = _payload(conn, D2)
    if case == "missing_evidence":
        del payload["items"][0]["evidence"]
    elif case == "bad_decision_id":
        payload["items"][0]["evidence"]["decision_id"] = 999999
    elif case == "bad_trade_id":
        payload["items"][1]["evidence"]["trade_id"] = 999999
    elif case == "metric_asof_future":
        payload["items"][2]["evidence"]["date"] = "2026-09-30"   # 晚于复盘日
    elif case == "metric_not_whitelisted":
        payload["items"][2]["evidence"]["metric"] = "sharpe"
    elif case == "metric_value_off_by_0.01":
        payload["items"][2]["evidence"]["value"] += 0.01
    elif case == "market_field_unknown":
        payload["items"][3]["evidence"]["field"] = "breadth.up_ratio_typo"
    elif case == "unknown_top_key":
        payload["params"] = {"lot": 50}
    elif case == "unknown_lesson_key":
        payload["lessons"] = [{"key": "a_b_c", "kind": "fact", "text": "x",
                               "severity": 3}]
    elif case == "evidence_missing_key":
        del payload["items"][0]["evidence"]["decision_id"]
    elif case == "evidence_extra_key":
        payload["items"][0]["evidence"]["reason"] = "我在现场"
    elif case == "bad_lesson_key":
        payload["lessons"] = [{"key": "Bad-Key", "kind": "fact", "text": "x"}]
    elif case == "bad_lesson_kind":
        payload["lessons"] = [{"key": "a_b_c", "kind": "hunch", "text": "x"}]
    elif case == "empty_claim":
        payload["items"][0]["claim"] = "  "
    elif case == "lesson_text_too_long":
        payload["lessons"] = [{"key": "a_b_c", "kind": "fact", "text": "x" * 201}]
    elif case == "kind_not_daily":
        payload["kind"] = "weekly"
    elif case == "arm_mismatch":
        payload["arm"] = OTHER
    elif case == "asof_mismatch":
        payload["asof"] = D3
    elif case == "duplicate_lesson_key":
        payload["lessons"] = [_lesson("a_b_c", "第一次"), _lesson("a_b_c", "第二次")]
    elif case == "other_arms_decision":
        # 引用**别的臂**的决策（那条属于 OTHER，asof 也在 PIT 之内）
        payload["items"][0]["evidence"]["decision_id"] = _nav_id(conn, D3, arm=OTHER)
    before = _n_rows(conn)
    with pytest.raises(review.ReviewValidationError):
        review.record_review(conn, arm=ARM, asof=D2, payload=payload,
                             model_id="manual", prompt_sha256="p", now=NOW,
                             context_sha256="c" * 64)
    assert _n_rows(conn) == before, "被拒的载荷必须**零写入**"


def test_t2_a_value_off_by_a_cent_is_named(conn):
    """「读数不许编」的牙齿：差 0.01 要**点名差了多少**，不能只说「不合法」。"""
    payload = _payload(conn, D2)
    payload["items"][2]["evidence"]["value"] += 0.01
    with pytest.raises(review.ReviewValidationError) as exc:
        review.record_review(conn, arm=ARM, asof=D2, payload=payload,
                             model_id="manual", prompt_sha256="p", now=NOW,
                             context_sha256="c" * 64)
    assert exc.value.field == "items[2].value"
    assert "读数不许编" in str(exc.value)


def test_t2_past_asof_rows_of_the_same_decision_would_be_allowed(conn):
    """反向对照：**同一批指针**只要不越界就能写 —— 证明上面拒的是越界，不是「挑刺」。"""
    payload = _payload(conn, D2)
    del payload["items"][2]["evidence"]["date"]   # 缺省 ⇒ <= 复盘日的最近一行
    assert review.validate_review(conn, arm=ARM, asof=D2, payload=payload)


def test_t2_a_duplicate_arm_asof_kind_is_a_conflict_with_zero_writes(conn):
    _write(conn, D2)
    before = _n_rows(conn)
    with pytest.raises(review.ReviewConflict):
        _write(conn, D2)
    assert _n_rows(conn) == before == 1


def _raw_review(conn, asof, *, arm=ARM, kind="daily", payload_json="{}"):
    """直接落一行（**绕过写入口**）—— 用来造 K4/K5 读侧要看的那种行。"""
    conn.execute(
        "INSERT INTO paper_agent_reviews (arm, asof, kind, model_id, prompt_sha256,"
        " context_sha256, n_items, n_lessons, payload_json, created_at)"
        " VALUES (?,?,?,'m','p','c',0,0,?,?)", (arm, asof, kind, payload_json, NOW))


def test_t2_the_same_arm_asof_with_another_kind_is_a_second_row(conn):
    """`kind` 是幂等键的一部分（K1：本期只写 'daily'，DB 留了扩展位）。"""
    _write(conn, D2, kind="daily")
    _raw_review(conn, D2, kind="weekly")
    assert [r["kind"] for r in review.load_reviews(conn, arm=ARM)] == \
        ["daily", "weekly"]


# ---------- T2 append-only 三重防线 ----------

@pytest.mark.parametrize("sql", [
    "UPDATE paper_agent_reviews SET model_id = '改过'",
    "DELETE FROM paper_agent_reviews",
])
def test_t2_triggers_reject_update_and_delete(conn, sql):
    _write(conn, D2)
    with pytest.raises(sqlite3.Error, match="append-only"):
        conn.execute(sql)


#: 不带 `review_id`：冲突落在 `UNIQUE(arm, asof, kind)` 上 ⇒ 解决方式是**隐式删行**。
_REPLACE_SQL = (
    "INSERT OR REPLACE INTO paper_agent_reviews (arm, asof, kind, model_id,"
    " prompt_sha256, context_sha256, n_items, n_lessons, payload_json, created_at)"
    " VALUES (?,?,'daily','伪造','p','c',0,0,'{\"items\":[{\"claim\":\"覆盖\"}]}',?)")


def test_t2_insert_or_replace_is_blocked_with_recursive_triggers(conn):
    _write(conn, D2)
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1, \
        "db.connect 必须打开 recursive_triggers（否则 INSERT OR REPLACE 拦不住）"
    with pytest.raises(sqlite3.Error, match="append-only"):
        conn.execute(_REPLACE_SQL, (ARM, D2, NOW))
    row = conn.execute("SELECT payload_json FROM paper_agent_reviews").fetchone()
    assert "覆盖" not in row["payload_json"], "原行必须原封不动"


def test_t2_without_the_pragma_replace_silently_succeeds(db):
    """**反向自检**（P48 同款）：不开 `recursive_triggers` ⇒ 同一句 OR REPLACE 静默成功。

    这条用例证明那个 PRAGMA 是**承重墙**，而不是「顺手加的一行」。
    """
    c = connect(db)
    try:
        _write(c, D2)
    finally:
        c.close()
    raw = sqlite3.connect(str(db), isolation_level=None)      # 故意绕开 db.connect
    raw.row_factory = sqlite3.Row
    try:
        assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        raw.execute(_REPLACE_SQL, (ARM, D2, NOW))
        row = raw.execute("SELECT model_id, payload_json FROM paper_agent_reviews").fetchone()
        assert row["model_id"] == "伪造"
        assert "覆盖" in row["payload_json"]
        assert raw.execute("SELECT COUNT(*) FROM paper_agent_reviews").fetchone()[0] == 1
    finally:
        raw.close()


# ---------- T2 读出口 ----------

def test_t2_load_reviews_is_ascending_and_limit_takes_the_tail(conn):
    for d, key in ((D1, "a"), (D2, "b"), (D3, "c")):
        _write(conn, d, lessons=[_lesson(f"k_{key}_x", key)])
    assert [r["asof"] for r in review.load_reviews(conn, arm=ARM)] == [D1, D2, D3]
    assert [r["asof"] for r in review.load_reviews(conn, arm=ARM, limit=2)] == [D2, D3]
    assert review.load_reviews(conn, arm=OTHER) == []


def test_t2_load_reviews_serves_asof_and_projects_items_and_lessons(conn):
    _write(conn, D2, lessons=[_lesson("cash_drag", "现金拖累收益")])
    row = review.load_reviews(conn, arm=ARM, asof=D2)[0]
    assert row["items"][0]["claim"].startswith(D2)
    assert row["items"][0]["evidence"]["kind"] == "decision"
    assert row["lessons"] == [{"key": "cash_drag", "kind": "fact",
                               "text": "现金拖累收益"}]
    assert review.load_reviews(conn, arm=ARM, asof=D1) == []


def test_t2_a_broken_payload_json_is_served_not_raised(conn):
    """读出口不替写入口兜底：坏行照出（`payload=None`），**不抛**。"""
    conn.execute(
        "INSERT INTO paper_agent_reviews (arm, asof, kind, model_id, prompt_sha256,"
        " context_sha256, n_items, n_lessons, payload_json, created_at)"
        " VALUES (?,?,'daily','m','p','c',0,0,'{不是 JSON',?)", (ARM, D2, NOW))
    row = review.load_reviews(conn, arm=ARM)[0]
    assert row["payload"] is None and row["items"] == [] and row["lessons"] == []
    assert review.derive_facts(conn, arm=ARM, asof=D3)["n_reviews"] == 1


def test_t2_a_missing_table_gives_empty_reads_without_raising(tmp_path):
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    c.row_factory = sqlite3.Row
    try:
        assert review.load_reviews(c, arm=ARM) == []
        assert review.derive_facts(c, arm=ARM, asof=D3) == {
            "facts": [], "n_facts_truncated": 0, "n_reviews": 0}
    finally:
        c.close()


# ---------- T3 / K4：facts 的 ≥2 条规则 ----------

def test_t3_a_single_occurrence_is_not_a_fact(conn):
    _write(conn, D2, lessons=[_lesson("seen_once", "只说过一次")])
    out = review.derive_facts(conn, arm=ARM, asof=D3)
    assert out["facts"] == [] and out["n_reviews"] == 1


def test_t3_two_occurrences_on_different_asof_are_a_fact(conn):
    _write(conn, D2, lessons=[_lesson("cash_drag", "旧的说法")])
    _write(conn, D3, lessons=[_lesson("cash_drag", "新的说法")])
    fact = review.derive_facts(conn, arm=ARM, asof=D3)["facts"][0]
    assert fact["key"] == "cash_drag"
    assert fact["text"] == "新的说法"            # 取**最新**一条
    assert fact["seen_at"] == [D2, D3]           # 全部 asof 升序
    assert fact["conflict"] is True
    assert fact["texts"] == ["旧的说法", "新的说法"]   # 两条都列，不取平均


def test_t3_the_same_text_twice_is_not_a_conflict(conn):
    _write(conn, D2, lessons=[_lesson("cash_drag", "同一个说法")])
    _write(conn, D3, lessons=[_lesson("cash_drag", "同一个说法")])
    fact = review.derive_facts(conn, arm=ARM, asof=D3)["facts"][0]
    assert fact["conflict"] is False and "texts" not in fact


def test_t3_only_daily_reviews_count(conn):
    """K4 只在 `kind='daily'` 里聚合 —— 别的 kind 一行都不算（本期只写 daily）。"""
    _raw_review(conn, D1, kind="weekly",
                payload_json=json.dumps({"lessons": [{"key": "cash_drag",
                                                      "kind": "fact", "text": "x"}]},
                                        ensure_ascii=False))
    _write(conn, D2, lessons=[_lesson("cash_drag", "y")])
    out = review.derive_facts(conn, arm=ARM, asof=D3)
    assert out["n_reviews"] == 1 and out["facts"] == []


def test_t3_facts_are_pit_bounded(conn):
    _write(conn, D2, lessons=[_lesson("cash_drag", "x")])
    _write(conn, D3, lessons=[_lesson("cash_drag", "y")])
    assert review.derive_facts(conn, arm=ARM, asof=D2)["facts"] == []
    assert review.derive_facts(conn, arm=ARM, asof=D2)["n_reviews"] == 1


def test_t3_only_this_arms_lessons_count(conn):
    _write(conn, D2, lessons=[_lesson("cash_drag", "x")])
    _write(conn, D3, arm=OTHER, lessons=[_lesson("cash_drag", "y")], items=[])
    assert review.derive_facts(conn, arm=ARM, asof=D3)["facts"] == []


def test_t3_more_than_ten_facts_are_truncated_by_key_order(conn):
    """12 个 key 各出现 2 次 ⇒ 12 条 fact，按 `key` 字典序留前 10、截断 2。"""
    for d in (D1, D2):
        _write(conn, d, lessons=[_lesson(f"k{i:02d}", f"第 {d} 次") for i in range(12)])
    out = review.derive_facts(conn, arm=ARM, asof=D3)
    assert len(out["facts"]) == review.FACT_LIMIT == 10
    assert out["n_facts_truncated"] == 2
    assert [f["key"] for f in out["facts"]] == [f"k{i:02d}" for i in range(10)]


def test_t3_truncation_is_ordered_by_seen_at_count_then_key(conn):
    """条数降序优先：出现 3 次的排在只出现 2 次的前面（同名次才按 key）。"""
    for d in (D1, D2):
        _write(conn, d, lessons=[_lesson(f"k{i:02d}", "两次") for i in range(11)]
               + [_lesson("zz_frequent", "三次")])
    _write(conn, D3, lessons=[_lesson("zz_frequent", "三次")])
    out = review.derive_facts(conn, arm=ARM, asof=D3)
    assert len(out["facts"]) == 10
    assert out["n_facts_truncated"] == 2          # 12 个 key（11 个 2 次 + 1 个 3 次）
    assert out["facts"][0]["key"] == "zz_frequent"
    assert [f["key"] for f in out["facts"][1:]] == [f"k{i:02d}" for i in range(9)]


def test_t3_derive_facts_is_byte_identical_across_two_calls(conn):
    for d in (D1, D2):
        _write(conn, d, lessons=[_lesson("cash_drag", "x"), _lesson("lot_floor", "y")])
    first = review.derive_facts(conn, arm=ARM, asof=D3)
    second = review.derive_facts(conn, arm=ARM, asof=D3)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) \
        == json.dumps(second, ensure_ascii=False, sort_keys=True)
    assert "created_at" not in json.dumps(first)


# ---------- T3 / K5+K8：own_history 的两个新子键 ----------

def test_t3_own_history_keeps_its_old_keys_verbatim(conn):
    """K8：既有子键**逐位不变**（名字、顺序、语义）。"""
    _write(conn, D2, lessons=[_lesson("cash_drag", "x")])
    block = own_history.own_history_block(conn, arm=ARM, asof=D3)
    assert list(block)[:11] == [
        "arm", "nav_series", "n_nav_days", "trades", "n_trades", "n_buy",
        "n_sell", "realized_fees_total", "decisions", "n_decisions", "notes"]
    assert list(block)[-2:] == ["recent_reviews", "facts"]
    assert set(block) == set(list(block)[:11]) | {"recent_reviews", "facts"}


def test_t3_recent_reviews_are_capped_and_ascending(conn):
    for i, d in enumerate((D1, D2, D3)):
        _write(conn, d, lessons=[_lesson(f"k{i}_x", d)])
    # P86 修订：决策**日**的复盘不进当天的上下文 ⇒ 补一条更早的，凑出「尾部 3 条」。
    conn.execute(
        "INSERT INTO paper_agent_reviews (arm, asof, kind, model_id,"
        " prompt_sha256, context_sha256, n_items, n_lessons, payload_json,"
        " created_at) VALUES (?,'2026-09-18','daily','m','p','c',0,0,'{}',?)",
        (ARM, NOW))
    conn.commit()
    block = own_history.own_history_block(conn, arm=ARM, asof=D3)
    assert len(block["recent_reviews"]) == own_history.REVIEW_LIMIT == 3
    assert [r["asof"] for r in block["recent_reviews"]] == ["2026-09-18", D1, D2]
    entry = block["recent_reviews"][1]
    assert set(entry) == {"asof", "kind", "context_sha256", "n_items",
                          "n_lessons", "items", "lessons"}
    assert entry["context_sha256"] == "c" * 64
    assert entry["items"][0]["evidence"]["kind"] == "decision"


def test_p86_a_same_day_review_is_invisible_to_that_days_decision_context(conn):
    """P86 修订（生成器「先复盘、再决策」的前提）：当天写的复盘**不进**当天上下文。

    否则 ② 里取的 `context_sha256` 会立即失效 ⇒ `paper agent decide` 的 D-49
    指纹闸门必拒（该现象在 P86 演练里逐条复现过，见任务书 §7）。
    """
    before = agent_context.decision_context_sha256(
        engine.decision_context_for(conn, arm=ARM, asof=D3))
    _write(conn, D3, lessons=[_lesson("cash_drag", "当天写的")])
    after = agent_context.decision_context_sha256(
        engine.decision_context_for(conn, arm=ARM, asof=D3))
    assert after == before                       # 当天写入不改当天上下文
    assert own_history.own_history_block(conn, arm=ARM, asof=D3)["recent_reviews"] == []
    # 次日（「下一轮」）看得见 —— 不是「永远看不见」
    later = own_history.own_history_block(conn, arm=ARM, asof="2026-09-25")
    assert [r["asof"] for r in later["recent_reviews"]] == [D3]


def test_t3_recent_reviews_take_the_latest_three(conn):
    days = ["2026-08-01", "2026-08-04", "2026-08-05", "2026-08-06"]
    c = conn
    c.executemany(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,?,0.0,'[]',0.0,20000.0,0.0,0.0,0.0,20000.0,4400.0,?,?)",
        [(ARM, d, d, NOW) for d in days[1:]])
    for d in days:
        c.execute(
            "INSERT INTO paper_agent_reviews (arm, asof, kind, model_id,"
            " prompt_sha256, context_sha256, n_items, n_lessons, payload_json,"
            " created_at) VALUES (?,?,'daily','m','p','c',0,0,'{}',?)", (ARM, d, NOW))
    c.commit()
    block = own_history.own_history_block(conn, arm=ARM, asof="2026-08-06")
    assert [r["asof"] for r in block["recent_reviews"]] == days[:3]


def test_t3_own_history_facts_are_capped_at_ten(conn):
    for d in (D1, D2):
        _write(conn, d, lessons=[_lesson(f"k{i:02d}", "x") for i in range(12)])
    block = own_history.own_history_block(conn, arm=ARM, asof=D3)
    assert len(block["facts"]) == own_history.FACT_LIMIT == 10


def test_t3_a_review_after_asof_leaves_the_block_and_the_sha_untouched(conn):
    """PIT：在 `asof` **之后**插复盘 ⇒ 块与指纹逐字节不变（K5 的硬判据）。"""
    _write(conn, D1, lessons=[_lesson("cash_drag", "x")])
    before = own_history.own_history_block(conn, arm=ARM, asof=D2)
    before_sha = agent_context.decision_context_sha256(
        engine.decision_context_for(conn, arm=ARM, asof=D2))
    _write(conn, D2, lessons=[_lesson("cash_drag", "后见之明"), _lesson("later", "z")])
    after = own_history.own_history_block(conn, arm=ARM, asof=D2)
    after_sha = agent_context.decision_context_sha256(
        engine.decision_context_for(conn, arm=ARM, asof=D2))
    assert json.dumps(after, ensure_ascii=False, sort_keys=True) \
        == json.dumps(before, ensure_ascii=False, sort_keys=True)
    assert after_sha == before_sha
    # 反向对照：D1/D2 的复盘在 **D3** 的上下文里看得见（P86 修订后＝「早于决策日」）
    later = own_history.own_history_block(conn, arm=ARM, asof=D3)
    assert [r["asof"] for r in later["recent_reviews"]] == [D1, D2]


def test_t3_the_two_blocks_enter_the_fingerprint(conn):
    """两块在 `own_history` 里 ⇒ 已经进指纹（`DECISION_HASHED_KEYS` 零改动）。"""
    base = engine.decision_context_for(conn, arm=ARM, asof=D3)
    sha = agent_context.decision_context_sha256(base)
    _write(conn, D2, lessons=[_lesson("cash_drag", "x")])
    after = engine.decision_context_for(conn, arm=ARM, asof=D3)
    assert agent_context.decision_context_sha256(after) != sha
    assert "own_history" in agent_context.DECISION_HASHED_KEYS
    assert agent_context.DECISION_HASHED_KEYS[-2:] == ("market", "own_history")


# ---------- T4 CLI ----------

def _run(db, *argv, capsys, now=True, with_db=True):
    """跑一条 CLI。`--now` / `--db` 只在子命令真的声明了它们时才拼上去。"""
    extra = (["--db", str(db)] if with_db else []) + (["--now", NOW] if now else [])
    code = main([*argv, *extra])
    out, err = capsys.readouterr()
    return code, out, err


def _review_file(tmp_path, payload, name="r.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_t4_review_writes_and_prints_the_k7_receipt(db, tmp_path, capsys, conn):
    # 指纹取的是**该决策日的上下文**。P86 把复盘窗口收紧成 `asof < 决策日` 之后，
    # 当天写的复盘不进当天上下文 ⇒ 落地前后现算**逐位相同**，回执里那串是可复算的。
    expected = agent_context.decision_context_sha256(
        engine.decision_context_for(conn, arm=ARM, asof=D2))
    payload = _payload(conn, D2, lessons=[_lesson("cash_drag", "x")])
    path = _review_file(tmp_path, payload)
    code, out, err = _run(db, "paper", "agent", "review", "--asof", D2,
                          "--arm", ARM, "--file", str(path), capsys=capsys)
    assert code == 0, err
    receipt = json.loads(out)
    assert set(receipt) == {"written", "review_id", "arm", "asof", "n_items",
                            "n_lessons", "facts", "context_sha256"}
    assert receipt["written"] is True and receipt["n_items"] == 4
    assert receipt["facts"] == 0
    assert receipt["context_sha256"] == expected
    # 写完之后现算 **还是同一份**（当天写入不改当天上下文 ⇒ 指纹可复算）。
    after = agent_context.decision_context_sha256(
        engine.decision_context_for(conn, arm=ARM, asof=D2))
    assert after == expected
    # 反向对照：次日（「下一轮」）它就在上下文里了 —— 复盘照旧是下一轮的输入。
    later = engine.decision_context_for(conn, arm=ARM, asof=D3)
    assert [r["asof"] for r in later["own_history"]["recent_reviews"]] == [D2]


def test_t4_review_prints_one_line_of_json(db, tmp_path, capsys, conn):
    payload = _payload(conn, D2)
    path = _review_file(tmp_path, payload)
    code, out, err = _run(db, "paper", "agent", "review", "--asof", D2,
                          "--arm", ARM, "--file", str(path), capsys=capsys)
    assert code == 0 and out.count("\n") == 1


@pytest.mark.parametrize("mutate,expected", [
    (lambda p: p["items"][2]["evidence"].__setitem__("value",
                                                     p["items"][2]["evidence"]["value"] + 0.01), 2),
    (lambda p: p.__setitem__("params", {"lot": 50}), 2),
])
def test_t4_review_exit_codes(db, tmp_path, capsys, conn, mutate, expected):
    payload = _payload(conn, D2)
    mutate(payload)
    path = _review_file(tmp_path, payload)
    code, out, err = _run(db, "paper", "agent", "review", "--asof", D2,
                          "--arm", ARM, "--file", str(path), capsys=capsys)
    assert code == expected, err
    assert out == ""
    before = _n_rows(conn)
    assert before == 0


def test_t4_review_duplicate_is_exit_1_with_zero_writes(db, tmp_path, capsys, conn):
    path = _review_file(tmp_path, _payload(conn, D2))
    assert _run(db, "paper", "agent", "review", "--asof", D2, "--arm", ARM,
                "--file", str(path), capsys=capsys)[0] == 0
    code, out, err = _run(db, "paper", "agent", "review", "--asof", D2,
                          "--arm", ARM, "--file", str(path), capsys=capsys)
    assert code == 1 and out == ""
    assert json.loads(err)["type"] == "ReviewConflict"
    assert _n_rows(conn) == 1


def test_t4_review_refuses_an_arm_the_decision_loop_does_not_claim(db, tmp_path,
                                                                  capsys, conn):
    # 载荷本身合法（指针指向 ARM 的真行），**臂**不是决策循环认领的那条 ⇒ 拒、零写入。
    path = _review_file(tmp_path, _payload(conn, D2, arm="arm-hold",
                                           items=_items(conn, D2, arm=ARM)))
    code, out, err = _run(db, "paper", "agent", "review", "--asof", D2,
                          "--arm", "arm-hold", "--file", str(path), capsys=capsys)
    assert code == 2 and out == ""
    assert _n_rows(conn) == 0


def test_t4_reviews_and_facts_are_read_only(db, tmp_path, capsys, conn):
    import hashlib
    path = _review_file(tmp_path, _payload(conn, D2, lessons=[_lesson("cash_drag", "x")]))
    assert _run(db, "paper", "agent", "review", "--asof", D2, "--arm", ARM,
                "--file", str(path), capsys=capsys)[0] == 0
    conn.commit()
    blob_before = db.read_bytes()
    code, out, err = _run(db, "paper", "agent", "reviews", "--arm", ARM,
                          capsys=capsys, now=False)
    assert code == 0, err
    listed = json.loads(out)
    assert listed["n_reviews"] == 1 and listed["n_shown"] == 1
    assert listed["reviews"][0]["payload"]["lessons"][0]["key"] == "cash_drag"
    assert set(listed["reviews"][0]) >= {"review_id", "arm", "asof", "kind",
                                        "payload", "payload_json"}
    code, out, err = _run(db, "paper", "agent", "facts", "--arm", ARM, capsys=capsys, now=False)
    assert code == 0, err
    facts = json.loads(out)
    assert set(facts) == {"arm", "asof", "asof_source", "n_reviews", "n_facts",
                          "n_facts_truncated", "facts", "note"}
    assert facts["facts"] == [] and facts["asof_source"] == "latest_review"
    assert hashlib.sha256(db.read_bytes()).hexdigest() \
        == hashlib.sha256(blob_before).hexdigest(), "两个读命令必须零写入"


def test_t4_reviews_limit_keeps_the_latest(db, tmp_path, capsys, conn):
    for d in (D1, D2, D3):
        path = _review_file(tmp_path, _payload(conn, d), name=f"r{d}.json")
        assert _run(db, "paper", "agent", "review", "--asof", d, "--arm", ARM,
                    "--file", str(path), capsys=capsys)[0] == 0
    code, out, err = _run(db, "paper", "agent", "reviews", "--arm", ARM,
                          "--limit", "2", capsys=capsys, now=False)
    assert code == 0, err
    listed = json.loads(out)
    assert [r["asof"] for r in listed["reviews"]] == [D2, D3]
    assert listed["n_reviews"] == 3 and listed["n_shown"] == 2


def test_t4_facts_defaults_to_the_latest_review_and_is_empty_without_any(db, capsys):
    code, out, err = _run(db, "paper", "agent", "facts", "--arm", ARM, capsys=capsys, now=False)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["asof"] is None and payload["asof_source"] is None
    assert payload["facts"] == [] and payload["n_reviews"] == 0


def test_t4_facts_explicit_asof_is_pit_bounded(db, tmp_path, capsys, conn):
    for d in (D2, D3):
        path = _review_file(tmp_path, _payload(conn, d, lessons=[_lesson("cash_drag", "x")]),
                            name=f"r{d}.json")
        assert _run(db, "paper", "agent", "review", "--asof", d, "--arm", ARM,
                    "--file", str(path), capsys=capsys)[0] == 0
    code, out, _ = _run(db, "paper", "agent", "facts", "--arm", ARM,
                        "--asof", D2, capsys=capsys, now=False)
    assert code == 0
    assert json.loads(out)["facts"] == []
    code, out, _ = _run(db, "paper", "agent", "facts", "--arm", ARM,
                        "--asof", D3, capsys=capsys, now=False)
    assert json.loads(out)["facts"][0]["key"] == "cash_drag"


def test_t4_the_three_subcommands_exist_without_touching_older_ones(db, capsys):
    code, out, _ = _run(db, "paper", "agent", "--help", capsys=capsys, now=False,
                      with_db=False)
    assert code == 0
    for name in ("review", "reviews", "facts", "evals", "context", "decide"):
        assert name in out


# ---------- K6：复盘不是参数通路 ----------

def test_k6_non_goals_ends_with_the_verbatim_k6_text():
    assert agent_context.NON_GOALS[-1] == K6_NON_GOAL
    # P84 的两条（以及 K5 的第 1 条）逐字没动，K6 只是**追加**在第 4 位
    assert agent_context.NON_GOALS[1:3] == P84_NON_GOALS
    assert len(agent_context.NON_GOALS) == 4


def test_k6_the_context_carries_the_k6_line(db):
    c = connect(db)
    try:
        ctx = engine.decision_context_for(c, arm=ARM, asof=D2)
    finally:
        c.close()
    assert ctx["non_goals"][-1] == K6_NON_GOAL


def test_k6_the_payload_schema_has_no_parameter_path(conn):
    """「改参数」在**结构上**写不进来：任何带参数味的键都被白名单挡掉。"""
    for key in ("params", "rules", "target_weight_pct", "stop_loss_pct",
                "spec", "formula", "cost_model", "lot"):
        payload = _payload(conn, D2)
        payload[key] = {"lot": 50}
        with pytest.raises(review.ReviewValidationError) as exc:
            review.validate_review(conn, arm=ARM, asof=D2, payload=payload)
        assert exc.value.field == "payload"


def test_k6_facts_reach_the_context_as_text_only(db):
    # P86 修订：上下文只看**早于**决策日的复盘 ⇒ 两条复盘都得排在 D3 之前。
    c = connect(db)
    try:
        _write(c, D1, lessons=[_lesson("cash_drag", "现金拖累收益", kind="habit")])
        _write(c, D2, lessons=[_lesson("cash_drag", "现金拖累收益")])
    finally:
        c.close()
    c = connect(db)
    try:
        ctx = engine.decision_context_for(c, arm=ARM, asof=D3)
    finally:
        c.close()
    fact = ctx["own_history"]["facts"][0]
    assert set(fact) == {"key", "text", "seen_at", "conflict"}
    assert isinstance(fact["text"], str)


# ---------- K1：新表不动既有表 ----------

def test_k1_the_reviews_write_touches_no_other_table(db):
    c = connect(db)
    try:
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("paper_agent_decisions", "paper_nav_daily",
                            "paper_trades")}
        _write(c, D2)
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in before}
    finally:
        c.close()
    assert after == before


# ---------- P105：metric 腿的比对基准＝上下文展示值 ----------
#
# 2026-09-30 18:34 的事故：库真值 cum_return = -0.011548，模型在上下文里看到的是
# -0.0115（`own_history._round4` 截到 4 位小数），载荷照抄 -0.0115 却被「与库值差
# 4.8e-05 > 1e-6」拒收 ⇒ 当日复盘零写入。根因是**写侧舍入**与**校验基准**不自洽，
# 不是模型编数。修法：metric 腿与 market 腿同构，基准改为 `round(库真值, DISPLAY_DP)`，
# 容差 `TOL = 1e-6` 一字不放宽。

#: 一个「小数第 5 位非 0」的库真值 —— 展示值必然是 -0.0115，与真值差 4.8e-05。
INCIDENT_RAW = -0.011548
INCIDENT_SHOWN = -0.0115
INCIDENT_DAY = "2026-09-28"


def _insert_nav(conn, date, *, arm=ARM, cum_return=0.0) -> None:
    """给该臂补一行净值（真值可带 >4 位小数 —— 正是事故的形状）。"""
    conn.execute(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,?,19604.59,'[]',29818.0,49422.59,0.0,44.46,?,50000.0,4400.0,?,?)",
        (arm, date, cum_return, date, NOW))
    conn.commit()


def _metric_payload(asof, value, *, metric="cum_return", date=None, arm=ARM) -> dict:
    ev: dict = {"kind": "metric", "metric": metric, "value": value}
    if date is not None:
        ev["date"] = date
    return {"asof": asof, "arm": arm, "kind": "daily",
            "items": [{"claim": f"{asof} 的 {metric}", "evidence": ev}],
            "lessons": []}


def _record(conn, asof, payload, *, arm=ARM):
    return review.record_review(conn, arm=arm, asof=asof, payload=payload,
                                model_id="manual", prompt_sha256="p" * 64,
                                context_sha256="c" * 64, now=NOW)


def test_p105_incident_replay_the_display_reading_is_accepted(conn):
    """用例 1：库真值 -0.011548 ⇒ 载荷报展示值 -0.0115 **必须通过**（落 1 行）。

    同时点名：这个差值 4.8e-05 远超旧口径的 1e-6 —— 即旧实现必拒（回归的鉴别力在此）。
    """
    _insert_nav(conn, INCIDENT_DAY, cum_return=INCIDENT_RAW)
    assert abs(INCIDENT_SHOWN - INCIDENT_RAW) > review.TOL   # 旧口径下必然被拒
    receipt = _record(conn, INCIDENT_DAY, _metric_payload(INCIDENT_DAY, INCIDENT_SHOWN))
    assert receipt["written"] is True and receipt["review_id"] == 1
    assert _n_rows(conn) == 1


def test_p105_a_fabricated_reading_is_still_rejected_with_zero_writes(conn):
    """用例 2：同一行报 -0.0116（离展示值 -0.0115 差 1e-04）⇒ 拒、**零写入**。"""
    _insert_nav(conn, INCIDENT_DAY, cum_return=INCIDENT_RAW)
    with pytest.raises(review.ReviewValidationError) as exc:
        _record(conn, INCIDENT_DAY, _metric_payload(INCIDENT_DAY, -0.0116))
    assert exc.value.field == "items[0].value"
    assert "读数不许编" in str(exc.value)
    assert _n_rows(conn) == 0


def test_p105_the_message_shows_both_the_displayed_and_the_stored_reading(conn):
    """D3：失败文案要同时给出**展示值**与**库真值**，让人一眼看出怎么改。"""
    _insert_nav(conn, INCIDENT_DAY, cum_return=INCIDENT_RAW)
    with pytest.raises(review.ReviewValidationError) as exc:
        _record(conn, INCIDENT_DAY, _metric_payload(INCIDENT_DAY, -0.0116))
    msg = str(exc.value)
    assert "上下文" in msg and "展示为" in msg and "库真值" in msg
    assert f"{INCIDENT_SHOWN!r}" in msg and f"{INCIDENT_RAW!r}" in msg


#: 真实形态的读数（净值/成交/累计列）＋ 事故值。
_SHAPES = (-0.011548, 0.1905, -4.3872, -34.9256, 49422.59, 19604.59,
           44.46, 50000.0, 0.0, -0.007208, 86.8, 30035.0, 0.123456789)


def test_p105_round_display_is_within_half_a_display_unit():
    """用例 3：按展示值比对的偏差上界＝半个展示单位（对每一列、若干真实取值）。"""
    bound = 0.5 * 10 ** -review.DISPLAY_DP
    for metric in review.METRIC_COLUMNS:
        for x in _SHAPES:
            shown = review.round_display(x)
            assert abs(shown - x) <= bound, (metric, x)
    assert review.DISPLAY_DP == 4      # 精度来源变了就必须重新审视这条上界


def test_p105_round_display_matches_own_history_round4():
    """用例 4：`own_history._round4` 与 `review.round_display` 用的是**同一个 dp**（D2）。"""
    for x in _SHAPES:
        assert own_history._round4(x) == review.round_display(x)
    assert own_history._round4(None) is review.round_display(None) is None


def test_p105_the_market_leg_message_is_untouched(conn):
    """用例 5（回归）：`market` 腿拿的是块里的展示值，本次改动不碰它 —— 文案逐字不变。"""
    payload = _payload(conn, D2)
    payload["items"][3]["evidence"]["value"] += 0.5          # 编一个市场读数
    with pytest.raises(review.ReviewValidationError) as exc:
        _record(conn, D2, payload)
    assert "与当日 market 块的读数不一致" in str(exc.value)
    assert "读数不许编" in str(exc.value)
