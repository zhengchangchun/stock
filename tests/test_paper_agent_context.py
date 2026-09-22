"""P37：喂给智能体的 **PIT 上下文**与它的指纹（`stocklab/paper/agent_context.py`）。

「智能体当时看到了什么」与「后来实际跑了什么」在台账里是两列，可以被分开检验
（设计 §5）。所以上下文必须满足两条：

1. **只含 `date <= asof` 的行** —— 喂进 `asof` 之后的任何行，指纹必须不变；
2. **可复算** —— 同库同参数两次调用逐字节一致（不含时间戳、不含自增 id）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 塞进 `asof` 之后的行 → 指纹不变 | 用 `MAX(date)` 取净值 / 取未来的收盘价 |
| 同参数两次调用 → 同指纹 | 指纹里混进 `now()` 或行号 |
| 改 spec / 改台账 → 指纹**必须**变 | 忘记把某个键加进 `HASHED_KEYS` |
| `HASHED_KEYS` 缺键 → 直接报错 | 静默只 hash 存在的键（指纹变哑巴） |
| 注入未来的价 → `LookaheadError` | 只在取库价时判 PIT，注入路径漏判 |
| `change_space` 与 `SPEC_SCHEMA` 同一份 | 上下文里再抄一份区间 |
"""

import json

import pytest

from stocklab.paper import agent_context, agent_spec
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, HOLD_CODE,
                                   RULE_CITATIONS)
from stocklab.paper.rules import LookaheadError
from stocklab.portfolio.prices import Price
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-22T16:00:00+08:00"
ASOF = "2026-09-21"
STAMP = "b" * 64


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    # 两天的收盘价：09-21 是决策日，09-22 是「明天」（PIT 反例要用）
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW)
         for code, series in {"000333": {"2026-09-21": 80.90, "2026-09-22": 81.50},
                              "sh000300": {"2026-09-21": 4539.57,
                                           "2026-09-22": 4550.00}}.items()
         for d, v in series.items()])
    c.commit()
    yield c
    c.close()


def _nav(conn, date, *, nav=19410.0, cash=11320.0, codes=()):
    conn.execute(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ARM_AGENT, date, cash,
         json.dumps([{"code": c, "qty": 100} for c in codes]),
         nav - cash, nav, 0.0, 5.09, -0.0295, 20000.0, 4539.57, date, NOW))
    conn.commit()


def _sha(conn, asof=ASOF):
    return agent_context.context_sha256(
        agent_context.build_context(conn, arm=ARM_AGENT, asof=asof))


# ---------- 确定性 ----------

def test_same_inputs_give_the_same_fingerprint_twice(conn):
    first = _sha(conn)
    second = _sha(conn)
    assert first == second
    ctx = agent_context.build_context(conn, arm=ARM_AGENT, asof=ASOF)
    assert json.dumps(ctx, sort_keys=True, ensure_ascii=False) == \
        json.dumps(agent_context.build_context(conn, arm=ARM_AGENT, asof=ASOF),
                   sort_keys=True, ensure_ascii=False)


def test_fingerprint_has_no_wall_clock(conn):
    """指纹只由库内容决定 —— 本模块根本不读时钟（错误日记 #30：时钟是参数）。"""
    blob = json.dumps(agent_context.build_context(
        conn, arm=ARM_AGENT, asof=ASOF), sort_keys=True, ensure_ascii=False)
    assert "2026-09-22T16" not in blob         # `NOW` 的时分秒
    assert "created_at" not in blob


# ---------- PIT：喂未来行，指纹不许动 ----------

def test_rows_after_asof_do_not_change_the_fingerprint(conn):
    before = _sha(conn)
    _nav(conn, "2026-09-22")                       # 明天的净值行
    conn.execute("UPDATE bars_daily SET close = 999.0 WHERE date = '2026-09-22'")
    conn.execute("DELETE FROM bars_daily WHERE date = '2026-09-22'")
    conn.commit()
    assert _sha(conn) == before, "asof 之后的行影响了指纹 ⇒ PIT 破了"


def test_rows_before_asof_do_change_the_fingerprint(conn):
    """反证：`<= asof` 的行**必须**影响指纹，否则上面那条测试是空的。"""
    before = _sha(conn)
    _nav(conn, ASOF)
    assert _sha(conn) != before


def test_account_snapshot_uses_the_latest_row_at_or_before_asof(conn):
    _nav(conn, "2026-09-18", nav=100.0)
    _nav(conn, ASOF, nav=200.0)
    _nav(conn, "2026-09-22", nav=300.0)
    ctx = agent_context.build_context(conn, arm=ARM_AGENT, asof=ASOF)
    assert ctx["account"]["date"] == ASOF
    assert ctx["account"]["nav"] == 200.0
    # 没有净值行时是 `None`（不是 0、不是拿别臂的顶替）
    empty = agent_context.build_context(conn, arm=ARM_AGENT_RANDOM, asof=ASOF)
    assert empty["account"] is None
    assert empty["arm"] == ARM_AGENT_RANDOM


def test_injected_future_price_raises_lookahead(conn):
    """注入路径与取库路径**同一条 PIT 判据**（否则注入就成了后门）。"""
    with pytest.raises(LookaheadError):
        agent_context.build_context(
            conn, arm=ARM_AGENT, asof=ASOF,
            prices={HOLD_CODE: Price(code=HOLD_CODE, price=81.5, source="inject",
                                     price_asof="2026-09-22", detail="future")})
    # 同日或更早的价照用（停牌时用上一交易日收盘是合法的）
    ok = agent_context.build_context(
        conn, arm=ARM_AGENT, asof=ASOF,
        prices={HOLD_CODE: Price(code=HOLD_CODE, price=80.9, source="inject",
                                 price_asof=ASOF, detail="today")})
    assert ok["marks"][HOLD_CODE]["price"] == 80.9


# ---------- 指纹覆盖了哪些键 ----------

def test_changing_the_spec_changes_the_fingerprint(conn):
    before = _sha(conn)
    agent_spec.record_decision(
        conn, arm=ARM_AGENT, asof=ASOF,
        spec_before=agent_spec.AGENT_DEFAULT_SPEC,
        spec_after=agent_spec.apply_spec(None, {"etf_target_pct": 20.0}),
        agent_kind=agent_spec.AGENT_KIND_MANUAL, model_id="manual",
        prompt_sha256=STAMP, seed=0, context_sha256=STAMP, now=NOW)
    assert _sha(conn) != before, "spec 不参与指纹 ⇒ 「同输入同输出」这条判据是假的"
    assert _sha(conn) == _sha(conn)


def test_hashed_keys_must_all_be_present(conn):
    ctx = agent_context.build_context(conn, arm=ARM_AGENT, asof=ASOF)
    assert set(agent_context.HASHED_KEYS) <= set(ctx)
    want = agent_context.context_sha256(ctx)
    for key in agent_context.HASHED_KEYS:
        broken = {k: v for k, v in ctx.items() if k != key}
        with pytest.raises(KeyError):
            agent_context.context_sha256(broken)
    # 多出来的键**不进**指纹（否则加个展示字段就会让历史指纹全部失效）
    assert agent_context.context_sha256({**ctx, "extra": 1}) == want


def test_every_hashed_key_actually_moves_the_fingerprint(conn):
    """逐个改动 `HASHED_KEYS` 里的键 → 指纹必须变。

    这是「文档说指纹覆盖了 N 个键」与「代码真的覆盖了」之间的对拍：
    少一个键，就会出现「上下文变了但指纹没变」这种最危险的失效。
    """
    _nav(conn, ASOF, codes=(HOLD_CODE,))
    ctx = agent_context.build_context(conn, arm=ARM_AGENT, asof=ASOF)
    base = agent_context.context_sha256(ctx)
    mutated = {
        "arm": ARM_AGENT_RANDOM,
        "asof": "2026-09-20",
        "spec": agent_spec.apply_spec(ctx["spec"], {"etf_target_pct": 20.0}),
        "spec_sha256": "deadbeef",
        "ledger": {**ctx["ledger"], "n_reviews": ctx["ledger"]["n_reviews"] + 1},
        "account": {**ctx["account"], "nav": ctx["account"]["nav"] + 1},
        "marks": {HOLD_CODE: {**ctx["marks"][HOLD_CODE], "price": -1.0}},
        "index_300": {**ctx["index_300"], "level": -1.0},
        "change_space": {"etf_target_pct": "改过了"},
        "disclosure": ["改过了"],
        "non_goals": ["改过了"],
        "citation_book": {"stop_loss": "改过了"},
        "counter_arm": "别的臂",
    }
    assert set(mutated) == set(agent_context.HASHED_KEYS)
    for key, value in mutated.items():
        assert agent_context.context_sha256({**ctx, key: value}) != base, \
            f"HASHED_KEYS 里的 {key} 改了却换不来新指纹 —— 它其实没进指纹"


# ---------- 上下文里写了什么 ----------

def test_context_shape_matches_the_schema_and_the_rule_books(conn):
    ctx = agent_context.build_context(conn, arm=ARM_AGENT, asof=ASOF)
    # 变更空间只有一份真相：直接来自 `SPEC_SCHEMA`
    assert set(ctx["change_space"]) == set(agent_spec.SPEC_SCHEMA)
    assert ctx["change_space"] == {n: f.range_text()
                                   for n, f in agent_spec.SPEC_SCHEMA.items()}
    assert ctx["citation_book"] == RULE_CITATIONS
    assert ctx["counter_arm"] == ARM_AGENT_RANDOM
    assert ctx["non_goals"] == list(agent_context.NON_GOALS)
    assert any("方向择时" in g for g in ctx["non_goals"])
    assert ctx["spec"] == agent_spec.AGENT_DEFAULT_SPEC
    assert ctx["spec_sha256"] == agent_spec.spec_sha256(ctx["spec"])
    assert ctx["ledger"]["arm"] == ARM_AGENT


def test_context_has_no_direction_prediction_fields(conn):
    """「不许加涨跌预测类字段」这条禁令要能被机器检查（它写在 `NON_GOALS` 里）。"""
    blob = json.dumps(agent_context.build_context(
        conn, arm=ARM_AGENT, asof=ASOF), ensure_ascii=False).lower()
    for banned in ("direction", "prob_up", "predict", "kelly", "model_version",
                   "signal", "momentum"):
        assert banned not in blob, f"上下文里出现了方向类字段：{banned}"


def test_context_summary_is_a_projection_not_a_recomputation(conn):
    ctx = agent_context.build_context(conn, arm=ARM_AGENT, asof=ASOF)
    s = agent_context.context_summary(ctx)
    assert s["context_sha256"] == agent_context.context_sha256(ctx)
    assert s["spec_sha256"] == ctx["spec_sha256"]
    assert s["asof"] == ASOF
    assert set(s) == {"arm", "asof", "spec_sha256", "n_reviews", "account_date",
                      "nav", "context_sha256"}
