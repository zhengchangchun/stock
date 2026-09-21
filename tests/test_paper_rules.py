"""P19：模拟盘纯函数（`stocklab/paper/rules.py`）。

这些测试**不碰数据库** —— 规则是纯函数，输入是数字，输出是决定。
验收项 ③（40% 上限 / 45–60% 现金带边界）与 ④（整手约束）在这里钉死。
"""

import pytest

from stocklab.config.costs import CostModel
from stocklab.portfolio.prices import Price
from stocklab.paper.rules import (
    C_CASH_FLOOR,
    C_LOT,
    C_ORDER_TARGET,
    C_PER_STEP,
    C_SINGLE,
    C_WHITELIST,
    Decision,
    LookaheadError,
    check_no_lookahead,
    etf_leg_targets,
    floor_lot,
    plan_etf_buy,
    plan_stop_loss,
    plan_trim,
)
from stocklab.paper.config import ETF_WHITELIST, LOT

ETF = CostModel(asset_class="etf")
STOCK = CostModel(asset_class="stock")


def _price(code, price, asof):
    return Price(code=code, price=price, source="bars", price_asof=asof,
                 detail=asof)


# ---------- 整手（验收 ④ 的基础） ----------

@pytest.mark.parametrize("raw,expected", [
    (0.0, 0), (8.11, 0), (10.0, 0), (99.99, 0), (100.0, 100),
    (150.0, 100), (221.5, 200), (1000.0, 1000),
])
def test_floor_lot_rounds_down_never_up(raw, expected):
    """整手一律**向下**取整 —— 向上取整会放大仓位（同 `risk/sizing.py` 口径）。"""
    assert floor_lot(raw, LOT) == expected


def test_floor_lot_negative_is_zero_not_negative():
    assert floor_lot(-5.0, LOT) == 0


# ---------- 禁未来函数（验收 ⑤） ----------

def test_future_price_asof_raises():
    with pytest.raises(LookaheadError) as e:
        check_no_lookahead("2026-09-15", {"000333": _price("000333", 87.23, "2026-09-16")})
    msg = str(e.value)
    assert "2026-09-16" in msg and "2026-09-15" in msg


def test_same_day_price_is_allowed():
    check_no_lookahead("2026-09-15", {"000333": _price("000333", 87.23, "2026-09-15")})


def test_stale_price_is_allowed_and_reported_by_caller():
    """停牌时用上一交易日收盘是**合法**的（价格自带 price_asof 供审计）。"""
    check_no_lookahead("2026-09-15", {"000333": _price("000333", 86.80, "2026-09-14")})


# ---------- 止损（收盘价口径） ----------

def test_stop_loss_triggers_below_line_and_sells_everything():
    d = plan_stop_loss(code="000333", close=82.13, qty=100)
    assert d.action == "sell" and d.qty == 100
    assert "82.14" in d.rule_citation


def test_stop_loss_exactly_on_line_does_not_trigger():
    """**跌破**才动，正好在线上不算破（与 `discipline.check_stop_loss_close` 同口径）。"""
    d = plan_stop_loss(code="000333", close=82.14, qty=100)
    assert d.action == "hold"
    assert "未触发" in d.reason


def test_stop_loss_with_no_position_is_a_hold_not_a_zero_share_sell():
    """跌破但手里没货（昨天已整清）→ `hold`，**不是**「卖 0 股」。

    这条钉住 ERROR_DIARY #49：曾经返回 `action="sell", qty=0`，
    `is_trade` 为真 → 一路走到 `paper_trades` 的 `CHECK (qty > 0)`，
    而 `step` 是一个事务 ⇒ 当天**五个账户的净值全部回滚**，且第二天照旧。
    """
    d = plan_stop_loss(code="000333", close=82.00, qty=0)
    assert d.action == "hold" and d.qty == 0
    assert not d.is_trade
    assert "跌破" in d.reason and "0 股" in d.reason
    assert "无关" not in d.reason          # 不是「不判定」，是「判定过了、没动作」


def test_is_trade_needs_a_direction_and_a_positive_qty():
    """`is_trade` 是「**真的能执行**」，所以只带方向不算成交。"""
    assert Decision(action="sell", code="000333", qty=0,
                    rule_citation="", reason="").is_trade is False
    assert Decision(action="buy", code="510300", qty=0,
                    rule_citation="", reason="").is_trade is False
    assert Decision(action="hold", code="000333", qty=100,
                    rule_citation="", reason="").is_trade is False
    assert Decision(action="sell", code="000333", qty=100,
                    rule_citation="", reason="").is_trade is True


# ---------- 单票 40% 上限（验收 ③） ----------

def test_weight_exactly_40pct_is_pass_no_trim():
    """40.00% 不超限（判据是 `> 40`），**不许**把边界当成违规。"""
    d = plan_trim(code="000333", qty=400, close=20.0, total_assets=20000.0)
    assert d.action == "hold"
    assert d.qty == 0
    assert C_SINGLE not in d.binding_constraints


def test_weight_above_40pct_triggers_trim():
    """40.01% 就该减 —— 边界另一侧必须真的报警。"""
    d = plan_trim(code="000333", qty=401, close=20.0, total_assets=20000.0)
    # 401 股 @20 = 8,020 / 20,000 = 40.1% > 40%
    assert C_SINGLE in d.binding_constraints
    assert d.qty == 0                      # 要减 0.1% = 0.4 股 → 不足 1 手 → 不动
    assert d.action == "hold"
    assert d.violation_remaining is True    # 违规**没被消除**，必须如实上报


def test_trim_min_multiple_of_lot_and_actually_cures_breach():
    """多手仓位：减仓后权重必须真的 ≤40%，且股数是整手。"""
    d = plan_trim(code="000333", qty=2000, close=10.0, total_assets=23000.0,
                  costs=STOCK)
    assert d.action == "sell"
    assert d.qty == 1100                    # 1000 股卖后仍 43.5% > 40% → 再加一手
    assert d.qty % LOT == 0
    assert d.violation_remaining is False
    assert d.weight_after_pct <= 40.0
    assert C_SINGLE in d.binding_constraints


def test_trim_10pct_intent_on_one_lot_only_has_two_options():
    """验收 ④：100 股仓位「想减 10%」→ 可执行量只有 0 或 100。

    本实现取 **0（不动）**，与「减到 ≤40% 所需 8.11 股」的解读**给出同一答案** ——
    所以这条测试对两种解读都不敏感（计划 §2.4）。
    """
    for intent_pct in (10.0, None):        # None = 按「减到 40% 所需」解读
        d = plan_trim(code="000333", qty=100, close=87.23, total_assets=20037.91,
                      intent_pct=intent_pct, costs=STOCK)
        assert d.action == "hold", f"intent={intent_pct}"
        assert d.planned_shares_raw < LOT     # 想减的股数不足 1 手
        assert C_LOT in d.binding_constraints
        assert d.violation_remaining is True


# ---------- 分散白名单 ----------

def test_forbidden_diversifier_is_rejected():
    """换家电股不算分散：600690 不在白名单 → 必须报错，不许静默照买。"""
    with pytest.raises(ValueError) as e:
        plan_etf_buy(code="600690", price=21.17, cash=11314.91,
                     total_assets=20037.91, leg_gap_value=1001.90,
                     costs=CostModel(asset_class="stock"))
    assert C_WHITELIST in str(e.value)


def test_whitelist_is_exactly_red_dividend_and_hs300():
    assert set(ETF_WHITELIST) == {"510880", "510300"}


# ---------- ETF 建仓：单次上限 / 现金下限 / 1,000 元摊薄 ----------

def test_etf_buy_sized_near_1000_and_under_per_step_cap():
    d = plan_etf_buy(code="510300", price=4.523, cash=11314.91,
                     total_assets=20037.91, leg_gap_value=1001.90, costs=ETF)
    assert d.action == "buy"
    assert d.qty == 200                     # 1001.90/4.5253 = 221 → floor 200
    assert d.qty % LOT == 0
    assert d.amount <= 0.05 * 20037.91 + 1e-9        # 单次动用 ≤5% 总资产
    assert C_PER_STEP in d.binding_constraints or C_ORDER_TARGET in d.binding_constraints


def test_per_step_cap_binds_when_already_deployed_today():
    """同一 step 内已动用过现金 → 剩余额度把本次订单**压小**（单次 = 每步累计）。"""
    d = plan_etf_buy(code="510300", price=4.523, cash=11314.91,
                     total_assets=20037.91, leg_gap_value=1001.90, costs=ETF,
                     deployed_today=100.0)
    assert d.action == "buy"
    assert d.qty == 100                     # 只剩 ¥901.90 可用 → 1 手
    assert d.amount <= 0.05 * 20037.91 - 100.0 + 1e-9


def test_cash_floor_45pct_binds_and_yields_no_trade():
    """现金下限 45% 是硬约束：可动用现金不足 1 手时**不动**，不许把现金打穿。"""
    d = plan_etf_buy(code="510300", price=4.523, cash=9400.0, total_assets=20000.0,
                     leg_gap_value=1001.90, costs=ETF)
    assert d.action == "hold"
    assert C_CASH_FLOOR in d.binding_constraints
    assert "45" in d.binding_constraints[0] + d.reason


def test_cash_floor_exactly_at_boundary_allows_buy():
    """正好留 45% 是允许的（约束是 `>= 45%`），边界另一侧才拦。"""
    d = plan_etf_buy(code="510300", price=4.523, cash=10000.0, total_assets=20000.0,
                     leg_gap_value=1001.90, costs=ETF)
    assert d.action == "buy"
    assert d.cash_after >= 0.45 * 20000.0 - 1e-9


def test_insufficient_for_one_lot_reports_lot_constraint():
    d = plan_etf_buy(code="510300", price=4.523, cash=11314.91,
                     total_assets=20037.91, leg_gap_value=100.0, costs=ETF)
    assert d.action == "hold"
    assert C_ORDER_TARGET in d.binding_constraints or C_LOT in d.binding_constraints


def test_etf_buy_has_no_stamp_tax_but_stock_sell_does():
    """验收 ②：成本按**标的口径**取 —— ETF 卖出免印花税，股票卖出要收。"""
    etf = plan_etf_buy(code="510880", price=3.382, cash=11314.91,
                       total_assets=20037.91, leg_gap_value=1001.90, costs=ETF)
    assert etf.fees["stamp_tax"] == 0.0
    stk = plan_stop_loss(code="000333", close=82.00, qty=100, costs=STOCK)
    assert stk.fees["stamp_tax"] > 0.0


# ---------- 目标腿均分 ----------

def test_etf_leg_targets_split_evenly_across_whitelist():
    t = etf_leg_targets(etf_target_pct=10.0, total_assets=20037.91)
    assert set(t) == set(ETF_WHITELIST)
    assert t["510300"] == pytest.approx(t["510880"])
    assert sum(t.values()) == pytest.approx(2003.791)
