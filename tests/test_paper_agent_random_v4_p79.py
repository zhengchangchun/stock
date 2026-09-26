"""P79 / D4：随机对照臂口径 v4（**可成交抽样**）。

本站要修的第二个故障：v3 的随机臂抽到的敞口**一笔都成交不了** ⇒ 它退化成
`arm-hold` 的第二份拷贝，而 `delta_vs_random` 就是拿它当分母的 ⇒
「AI 跑赢随机」这句话当时**没有对手**（P79 §0.1）。

真库只读探针原文（`paper_agent_decisions` 的 `payload_json` / `rationale`）：

```
2026-09-23  decision_id=1  总敞口 16.43%  分 4 只（¥317 / ¥1,323 / ¥988 / ¥587）
2026-09-24  decision_id=3  总敞口 0.17%   分 2 只（¥19.59 / ¥13.71）
两天 paper_trades / arm-agent-random = 0 笔，净值恒等于 arm-hold
```

判据（逐条对应任务书 T3）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | 同 `(arm, asof, seed)` 重跑逐字节一致 | 重抽用了全局 `random`（不可复现） |
| ② | 抽出来的每只**目标市值 ≥ 一手含费** | 抽完不折算整手（v3 的原样） |
| ③ | `S = ∅` 触发重抽、**上限 8 次**，仍空才退到「一手最便宜的」 | 无上限重抽 / 退路不落地 |
| ④ | **200 种子探针：零成交 == 0**，且 09-23 / 09-24 两天各自 ≥1 笔成交 | 本站在修的那个故障（两日 0 笔） |
| ⑤ | 上界语义未放宽（抽出的敞口 ≤ `cap`） | 为了「成交」把 `cap` 抬掉（那会作废 P68） |
"""

from __future__ import annotations

import random
import re

import pytest

from stocklab.paper import agent_decide
from stocklab.paper.config import (ARM_AGENT_RANDOM, RANDOM_CASH_FLOOR,
                                   RANDOM_N_CODES, RANDOM_RETRY_MAX)
from stocklab.paper.rules import one_lot_cost
from stocklab.portfolio.prices import Price
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

#: 真库 2026-09-23 / 09-24 的**完整候选池与收盘价**（只读探针原文，11 只）。
POOL_0923 = {"000333": 82.47, "000651": 38.36, "002032": 39.10, "002415": 33.11,
             "002508": 16.48, "600036": 40.60, "600519": 1251.24, "600900": 28.08,
             "601318": 53.87, "601398": 8.09, "603868": 31.26}
POOL_0924 = {"000333": 82.65, "000651": 38.50, "002032": 39.23, "002415": 32.63,
             "002508": 16.58, "600036": 40.69, "600519": 1237.00, "600900": 28.36,
             "601318": 53.25, "601398": 8.13, "603868": 31.00}
#: 那两天的账户形态（真库 `paper_nav_daily`）：现金 ¥11,320 ＋ `000333 ×100`。
CASH = 11320.0
HELD = {"000333": 100}


def _marks(closes: dict, asof: str) -> dict:
    return {code: Price(code=code, price=price, source="bars_daily",
                        price_asof=asof, detail="P79 只读探针")
            for code, price in closes.items()}


def _classes(closes: dict) -> dict:
    return {code: "stock" for code in closes}


def _payload(closes: dict, asof: str, *, seed: int = 0, held=None,
             cash=CASH) -> dict:
    held = HELD if held is None else held
    total = round(cash + sum(closes[c] * q for c, q in held.items() if c in closes), 4)
    return agent_decide.random_payload(
        arm=ARM_AGENT_RANDOM, asof=asof, pool_codes=set(closes),
        held_qty=held, marks=_marks(closes, asof), total_assets=total, seed=seed,
        asset_classes=_classes(closes), cash=cash)


def _trades(payload: dict, closes: dict, asof: str, *, cash=CASH, held=None) -> int:
    """把一份载荷走完整条执行链（rebase → execute），返回**成交笔数**。

    这是「零成交」判据的唯一口径：不看载荷里有几条 `decisions`，看**实际下了几单**
    —— v3 的失效恰恰是「载荷合法、但每一单都判 `hold`」。
    """
    held = HELD if held is None else held
    marks = _marks(closes, asof)
    total = round(cash + sum(closes[c] * q for c, q in held.items() if c in closes), 4)
    import tempfile
    from pathlib import Path
    db = Path(tempfile.mkdtemp()) / "exec.db"
    init_db(db)
    c = connect(db)
    try:
        c.executemany("INSERT OR IGNORE INTO instruments (code,name,market,board,type,"
                      "added_at) VALUES (?,?,'sz','main','stock','x')",
                      [(code, code) for code in closes])
        c.commit()
        rebased = agent_decide.rebase_payload(payload, total_assets=total,
                                              marks=marks, positions=dict(held))
        _cash, _pos, orders, _evals = agent_decide.execute_decision(
            c, arm=ARM_AGENT_RANDOM, asof=asof, decision=rebased, cash=cash,
            positions=dict(held), marks=marks, total_assets=total)
        return len(orders)
    finally:
        c.close()


# ---------- ① 逐字节可复现 ----------

def test_t3_same_arm_asof_seed_is_byte_identical():
    """①：同 `(arm, asof, seed)` 两遍必须同 `canonical_payload`（重抽也走同一 rng）。"""
    for asof, closes in (("2026-09-23", POOL_0923), ("2026-09-24", POOL_0924)):
        for seed in (0, 7, 97):
            first = agent_decide.canonical_payload(_payload(closes, asof, seed=seed))
            second = agent_decide.canonical_payload(_payload(closes, asof, seed=seed))
            assert first == second, (asof, seed)


# ---------- ② 每只都真的买得起一手 ----------

def test_t3_every_kept_leg_is_at_least_one_lot():
    """②：抽出来的每只，目标市值 ≥ 一手含费成本。

    这是「可成交」的定义式 —— 不是「看起来金额不小」，而是「执行层折算得出 ≥1 手」。
    """
    for asof, closes in (("2026-09-23", POOL_0923), ("2026-09-24", POOL_0924)):
        for seed in range(40):
            payload = _payload(closes, asof, seed=seed)
            total = round(CASH + closes["000333"] * 100, 4)
            for d in payload["decisions"]:
                cost = one_lot_cost(price=closes[d["code"]], asset_class="stock")
                assert total * d["target_weight_pct"] / 100.0 >= cost - 1e-9, \
                    (asof, seed, d["code"], d["target_weight_pct"], cost)


# ---------- ③ 重抽与退路 ----------

def test_t3_the_retry_path_is_bounded_and_then_falls_back():
    """③：`S = ∅` ⇒ 重抽 `exposure`，**上限 `RANDOM_RETRY_MAX` 次**；仍空 ⇒ 退路。

    构造：存量 `000333 ×100` 占总资产 87.23% ⇒ `cap = 2.77%` ⇒ 抽出来的敞口
    永远买不起任何一手（最便宜的 510880 也要 3.43%）⇒ 必然走到退路。
    退路取「一手最便宜的**未持有**池内标的」给 1 手（现金够）。
    """
    closes = {"000333": 87.23, "510880": 3.382, "600519": 1251.24}
    marks = _marks(closes, "2026-09-23")
    payload = agent_decide.random_payload(
        arm=ARM_AGENT_RANDOM, asof="2026-09-23",
        pool_codes={"510880", "600519"}, held_qty=HELD, marks=marks,
        total_assets=10000.0, seed=0, asset_classes=_classes(closes), cash=1277.0)
    assert f"重抽 {RANDOM_RETRY_MAX} 次" in payload["rationale"], payload["rationale"]
    assert "退路" in payload["rationale"]
    assert [d["code"] for d in payload["decisions"]] == ["510880"], payload
    weight = payload["decisions"][0]["target_weight_pct"]
    cost = one_lot_cost(price=closes["510880"], asset_class="stock")
    assert 10000.0 * weight / 100.0 >= cost, "退路给出的一手必须真的够一手"


def test_t3_the_fallback_refuses_when_cash_cannot_cover_one_lot():
    """③（续）：现金不够一手 ⇒ **全现金**并在 `rationale` 里点名（不硬凑）。"""
    closes = {"000333": 87.23, "600519": 1251.24}
    marks = _marks(closes, "2026-09-23")
    payload = agent_decide.random_payload(
        arm=ARM_AGENT_RANDOM, asof="2026-09-23", pool_codes={"600519"},
        held_qty=HELD, marks=marks, total_assets=10000.0, seed=0,
        asset_classes=_classes(closes), cash=1277.0)
    assert payload["decisions"] == []
    assert payload["cash_pct"] == 100.0
    assert "600519" in payload["rationale"] and "全现金" in payload["rationale"]


# ---------- ④ 零成交 == 0（含真库那两天） ----------

def test_t3_the_two_real_zero_trade_days_now_trade():
    """④（本站的靶子）：真库 09-23 / 09-24 两天的形态，口径 v4 下**各自 ≥1 笔成交**。

    旧口径（v3）这两天的读数（真库只读探针原文）：
    09-23 抽到 16.43% 分 4 只 ⇒ 每只目标 ¥317–1,323、**全部 < 一手**；
    09-24 抽到 0.17% 分 2 只 ⇒ ¥19.59 / ¥13.71、**全部 < 一手** ⇒ 两天 0 笔成交。
    """
    report = {}
    for asof, closes in (("2026-09-23", POOL_0923), ("2026-09-24", POOL_0924)):
        payload = _payload(closes, asof, seed=0)
        n = _trades(payload, closes, asof)
        report[asof] = (n, [d["code"] for d in payload["decisions"]])
        assert n >= 1, (asof, payload["rationale"])
    assert set(report) == {"2026-09-23", "2026-09-24"}
    assert all(n >= 1 for n, _ in report.values()), report


def test_t3_two_hundred_seeds_have_zero_zero_trade_payloads():
    """④（探针）：夹具账户上 200 个种子，**零成交的种子数 == 0**。

    与 P68 的 200 种子探针同款：判据钉在**读数**上（0/200），不是「好多了」。
    """
    closes = POOL_0923
    asof = "2026-09-23"
    zero: list[int] = []
    from pathlib import Path
    import tempfile
    db = Path(tempfile.mkdtemp()) / "probe.db"
    init_db(db)
    c = connect(db)
    try:
        c.executemany("INSERT OR IGNORE INTO instruments (code,name,market,board,type,"
                      "added_at) VALUES (?,?,'sz','main','stock','x')",
                      [(code, code) for code in closes])
        c.commit()
        marks = _marks(closes, asof)
        total = round(CASH + closes["000333"] * 100, 4)
        for seed in range(200):
            payload = _payload(closes, asof, seed=seed)
            rebased = agent_decide.rebase_payload(payload, total_assets=total,
                                                  marks=marks, positions=dict(HELD))
            _cash, _pos, orders, _evals = agent_decide.execute_decision(
                c, arm=ARM_AGENT_RANDOM, asof=asof, decision=rebased, cash=CASH,
                positions=dict(HELD), marks=marks, total_assets=total)
            if not orders:
                zero.append(seed)
        assert zero == [], f"口径 v4 之后不该再有零成交的种子：{zero}"
    finally:
        c.close()


# ---------- ⑤ 上界语义未放宽 ----------

def test_t3_the_exposure_upper_bound_is_unchanged():
    """⑤：抽出的敞口仍 ∈ [0, cap]，`cap = (100 − 现金下限) − 全部存量占比`。

    「为了成交把 cap 抬掉」是**不能接受的修法** —— 那会把 P68 的读数作废
    （ERROR_DIARY #73 / #77 的同型：修一处顺手改另一处的口径）。
    """
    closes = POOL_0923
    asof = "2026-09-23"
    reserved = round(closes["000333"] * 100 / round(CASH + closes["000333"] * 100, 4)
                     * 100.0, 4)
    cap = round((100.0 - RANDOM_CASH_FLOOR) - reserved, 2)
    drawn: set[float] = set()
    for seed in range(50):
        payload = _payload(closes, asof, seed=seed)
        m = re.search(r"cap=([0-9.]+)%", payload["rationale"])
        assert m and abs(float(m.group(1)) - cap) <= 0.01, payload["rationale"]
        exposure = round(100.0 - float(payload["cash_pct"]), 2)
        assert 0.0 <= exposure <= cap + 1e-9, (seed, exposure, cap)
        drawn.add(exposure)
    assert len(drawn) > 1, "50 个种子的敞口全一样 ⇒ 抽取区间没在被使用"


def test_t3_the_seed_material_and_draw_prefix_are_untouched():
    """⑤（续）：种子材料与**抽取序列的前五步**逐位不变（v4 只在之后追加一步）。

    这里在测试里**独立重放** v3 的那五步（`_seed_material` → `randint` →
    `sample` → `uniform` → `random`），证明 v4 抽到的标的**必是**v3 抽出来的那批
    的子集、抽出的只数也相等 —— 「抽取序列不许破」这句话的可断言形式。
    """
    closes = POOL_0923
    asof = "2026-09-23"
    total = round(CASH + closes["000333"] * 100, 4)
    usable = sorted(closes)
    total_w = sum(closes.values())
    for seed in range(30):
        payload = _payload(closes, asof, seed=seed)
        rng = random.Random(agent_decide._seed_material(ARM_AGENT_RANDOM, asof, seed))
        lo, hi = RANDOM_N_CODES
        k = max(1, min(len(usable), rng.randint(lo, hi)))
        picks = sorted(rng.sample(usable, k))
        m = re.search(r"抽出 (\d+) 只", payload["rationale"])
        assert int(m.group(1)) == k, (seed, payload["rationale"])
        got = [d["code"] for d in payload["decisions"]]
        if "退路" in payload["rationale"]:
            # 退路（S 重抽 8 次仍为空）是 D4 明确规定的**例外**：它取的是
            # 「一手最便宜的池内标的」，可以不是抽出来的那批。如实单列。
            assert set(got) <= set(usable), (seed, got)
        else:
            assert set(got) <= set(picks), (seed, got, picks)
        assert total_w > 0 and total > 0
