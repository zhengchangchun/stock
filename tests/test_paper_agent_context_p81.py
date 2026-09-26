"""P81 / D1：`tradability` 的「可下手」**结论**（只增键；判据只有一处实现）。

P79 给了「一手多少钱」（`one_lot_cost`）、P80 给了「买得起几手」（`affordable_lots`），
但「**所以今天这只下不下得了手**」仍然要模型自己拿 0 去比：`affordable_lots` 是个数，
结论是个判断。本站把判断也交出去 —— 判据落在 `engine.tradable_verdict`，
**只有这一处**（账户读数的 `account_view.buying_power.by_code` 调的是同一个函数）：
两处各写一份「买不买得起」就是 ERROR_DIARY #70 那类「同一页两个数」的形状。

判据（逐条对应任务书 T1）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | `tradable == (affordable_lots >= 1)` 恒等 | 另写一套「买不买得起」的算法 |
| ② | 两种 `untradable_reason` 文案各自出现、`tradable=True` 时是 `None` | 两种成因混成一句 |
| ③ | `n_tradable + n_untradable == len(by_code)` | 计数与逐只结论各算一份 |
| ④ | 全买不起 ⇒ `min_tradable_weight_pct` / `cheapest` 是 `None` | 猜一个 0 / 猜一只最便宜的 |
| ⑤ | 块内改一个**结论键** ⇒ 指纹变 | 加了键却没进指纹（摆设） |
| ⑥ | 池外 / 无价标的仍不进 `by_code` | 把不可投的标的报进门槛 |

数字一律**从被测模块取**（`engine.tradable_verdict` / `rules.one_lot_cost`），
不手抄一份到测试里。
"""

from __future__ import annotations

import json

import pytest

from stocklab.config.costs import ASSET_ETF, ASSET_STOCK
from stocklab.paper import agent_context, engine
from stocklab.paper.config import PAPER_START_DATE
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.portfolio.prices import Price
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
ARM = "arm-agent"

CAL = ("2026-09-11", "2026-09-14", START)
BARS = {
    "000333": {"2026-09-14": 86.80, START: 87.23},
    "510300": {START: 4.523},
    "510880": {START: 3.382},
    "600519": {START: 1251.24},     # 一手 ¥12.5 万+ —— 「买不起」的那一端
    "sh000300": {START: 4450.04},
}
POOL = ("000333", "510300", "510880", "600519")
#: 池内**没有** PIT 收盘价的那一只（⑥ 用它证明「无价 ⇒ 不进 by_code」）。
POOL_NO_PRICE = "000002"
ASSET = {"000333": ASSET_STOCK, "510300": ASSET_ETF, "510880": ASSET_ETF,
         "600519": ASSET_STOCK}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p81-context.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sh','main',?,?)",
                  [(code, code, ASSET[code], NOW) for code in POOL])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, series in BARS.items()
         for d, v in series.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,"
              " '首笔',?)", (NOW,))
    c.commit()
    c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
              " created_at) VALUES (?,'light','{}',?)", (START, NOW))
    sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
    for code in (*POOL, POOL_NO_PRICE):
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                  " adj_score, reason, risk_json, status, entered_at)"
                  " VALUES (?,?, 'short', 1,1,'夹具','{}','观察中',?)",
                  (sid, code, NOW))
    c.commit()
    engine.init_accounts(c, start_date=START, now=NOW)
    c.close()
    return path


def _marks(**prices) -> dict:
    return {code: Price(code=code, price=price, source="bars",
                        price_asof=START, detail="夹具")
            for code, price in prices.items()}


def _context(db) -> dict:
    c = connect(db)
    try:
        return engine.decision_context_for(c, arm=ARM, asof=START)
    finally:
        c.close()


def _block(**kw) -> dict:
    """一个**独立**的 `tradability` 块夹具（只给钱与价，不碰库）。"""
    prices = kw.pop("prices")
    return agent_context._tradability_block(
        prices=_marks(**prices), asset_classes={c: ASSET.get(c, ASSET_STOCK)
                                                for c in prices}, **kw)


# ---------- ① 判据恒等 ----------

def test_t1_tradable_equals_affordable_lots_at_least_one(db):
    """①：`tradable` 就是 `(affordable_lots >= 1)` —— 恒等，不是「差不多」。

    这条把「另写一套买不买得起的算法」判红：`affordable_lots` 已经由唯一的整手数
    公式（`engine.max_lots_affordable`）算出，结论只能是它的一个阈值。
    """
    block = _block(prices={"600519": 1251.24, "510300": 4.523, "000333": 87.23},
                   total_assets=20037.91, cash=11314.91, net_deposits=20000.0)
    for code, row in block["by_code"].items():
        assert row["tradable"] is (row["affordable_lots"] >= 1), code
    # 现金 0 ⇒ 逐只都下不了手（无论贵贱），且都是**结论**不是缺数据。
    broke = _block(prices={"510300": 4.523, "000333": 87.23}, total_assets=20037.91,
                   cash=0.0, net_deposits=20000.0)
    assert all(row["tradable"] is False for row in broke["by_code"].values())
    assert all(row["affordable_lots"] == 0 for row in broke["by_code"].values())


# ---------- ② 两种理由分开写 ----------

def test_t1_the_two_untradable_reasons_name_both_numbers():
    """②：两种成因**分开**写，两种都点名两个数；`tradable=True` 时是 `None`。

    成因分开是刻意的：一种（成本 > 总资产）这个账户无解，另一种（现金不够）
    先卖出别的标的就能凑出来 —— 处置不同，不许混成一句。
    """
    block = _block(prices={"600519": 1251.24, "000002": 33.82, "510300": 4.523},
                   total_assets=20000.0, cash=500.0, net_deposits=20000.0)
    rows = block["by_code"]
    cost_519 = rows["600519"]["one_lot_cost"]
    cost_002 = rows["000002"]["one_lot_cost"]
    assert rows["600519"]["tradable"] is False
    assert rows["600519"]["untradable_reason"] == \
        f"一手 ¥{cost_519:.2f} 已超过总资产 ¥20000.00"
    assert rows["000002"]["tradable"] is False
    assert rows["000002"]["untradable_reason"] == \
        f"现金 ¥500.00 不足一手 ¥{cost_002:.2f}（先卖出其他标的可释放现金）"
    # 买得起的那只：理由必须是 `None`（不是空串、不是省略键）。
    assert rows["510300"]["tradable"] is True
    assert "untradable_reason" in rows["510300"]
    assert rows["510300"]["untradable_reason"] is None


# ---------- ③ 计数与逐只结论同源 ----------

def test_t1_counts_add_up_to_the_by_code_size(db):
    """③：`n_tradable + n_untradable == len(by_code)`（真库形状的上下文）。"""
    block = _context(db)["tradability"]
    assert block["n_tradable"] + block["n_untradable"] == len(block["by_code"])
    assert block["n_tradable"] == sum(1 for r in block["by_code"].values()
                                      if r["tradable"])
    assert block["cheapest"] is None if block["n_tradable"] == 0 else \
        block["cheapest"]["code"] in block["by_code"]
    # 真库那一端：600519 一手 ¥12.5 万+ ⇒ 两万本金的账户上买不起一手。
    assert block["by_code"]["600519"]["tradable"] is False
    assert block["by_code"]["510300"]["tradable"] is True


# ---------- ④ 子集为空 ⇒ 两个读数都是 None ----------

def test_t1_an_empty_tradable_subset_reports_none_not_a_guessed_number():
    """④：一只都买不起 ⇒ `min_tradable_weight_pct` 与 `cheapest` 都是 `None`。

    「没有可下手标的」与「最小可成交权重是 0」是两件事：后者会把「买不到任何东西」
    读成「随便买」。所以这里**不猜数**（与 `min_weight_pct` 的退化口径同款）。
    """
    block = _block(prices={"600519": 1251.24, "510300": 4.523},
                   total_assets=1.0, cash=1.0, net_deposits=20000.0)
    assert block["n_untradable"] == len(block["by_code"]) == 2
    assert block["n_tradable"] == 0
    assert block["min_tradable_weight_pct"] is None
    assert block["cheapest"] is None
    # 逐只的 `min_weight_pct`（既有键）照旧算得出：两个读数的退化条件不同。
    assert block["min_weight_pct"] is not None


# ---------- ⑤ 新键进指纹 ----------

def test_t1_the_new_keys_are_inside_the_fingerprint(db):
    """⑤ 红-绿：改一个**结论键** ⇒ `decision_context_sha256` 变。

    「键加进 `DECISION_HASHED_KEYS` 但块里恒为空」也能让『块进指纹』那条绿 ——
    这一条把那种实现判红：变化的必须是新键本身。
    （真库台账里**既有**行的 sha 一个字符都不改：append-only，见 P79 同款纪律。）
    """
    ctx = _context(db)
    now = agent_context.decision_context_sha256(ctx)
    tampered = dict(ctx)
    block = json.loads(json.dumps(ctx["tradability"]))
    assert "untradable_reason" in block["by_code"]["600519"]
    block["by_code"]["600519"]["untradable_reason"] = "改过"
    tampered["tradability"] = block
    assert agent_context.decision_context_sha256(tampered) != now
    # 顶层的新计数键同样在指纹里。
    tampered2 = dict(ctx)
    block2 = json.loads(json.dumps(ctx["tradability"]))
    block2["n_tradable"] = int(block2["n_tradable"]) + 1
    tampered2["tradability"] = block2
    assert agent_context.decision_context_sha256(tampered2) != now


# ---------- ⑥ 键集仍然是 marks ∩ pool ----------

def test_t1_pool_codes_without_a_price_and_the_index_stay_out(db):
    """⑥：池内**无价**的（`000002`）与池外的（指数）都不进 `by_code`。

    这是 P79/P80 的既有口径（`marks ∩ pool`）—— 新增结论键**不许**把它放宽：
    报一只下不了单的标的，正是这个块要避免的事。
    """
    ctx = _context(db)
    assert POOL_NO_PRICE in ctx["pool"]["codes"]
    assert POOL_NO_PRICE not in ctx["tradability"]["by_code"]
    assert INDEX_300_SYMBOL not in ctx["tradability"]["by_code"]
    assert set(ctx["tradability"]["by_code"]) == \
        ({str(c) for c in ctx["pool"]["codes"]} & set(ctx["marks"]))
