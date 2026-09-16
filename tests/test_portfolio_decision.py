"""P1c：「能不能动」结论层（`portfolio.decision`）—— 三情形 + 全清试算逐分对账。

本文件钉住四件事：

1. **三条线的口径**与 `discipline` 逐字一致（正好落线上不算破）；
2. **全清试算走 `CostModel` 权威入口**，与 `model.fees()` 逐分一致；
3. **整手规则**：手里一手时减仓执行不了，只剩「全清 / 不动」；
4. **人话**：面向用户的文字里不许出现 PASS/FAIL/UNDETERMINED 与英文术语。
"""

import pytest

from stocklab.config.costs import ASSET_STOCK, CostModel
from stocklab.portfolio.decision import (
    LOT_SIZE,
    STATE_HOLD,
    STATE_NO_ADD,
    STATE_STOP,
    STATE_UNKNOWN,
    position_decision,
    sell_to_cash,
    whole_lot_reason,
)
from stocklab.portfolio.discipline import PER_CODE_LINES, lines_for

CODE = "000333"
LINES = PER_CODE_LINES[CODE]

#: 用户真实持仓：100 股，含费成本 8680 元，总资产约 2 万。
QTY = 100
COST_BASIS = 8680.0
TOTAL_ASSETS = 20000.0
CASH = 10000.0


def _decide(close, **kw):
    kw.setdefault("qty", QTY)
    kw.setdefault("cost_basis", COST_BASIS)
    kw.setdefault("avg_cost", COST_BASIS / QTY)
    kw.setdefault("total_assets", TOTAL_ASSETS)
    kw.setdefault("cash", CASH)
    kw.setdefault("close_asof", "2026-09-16")
    kw.setdefault("name", "美的集团")
    return position_decision(CODE, close=close, **kw)


# ---------- 1. 三条线的口径（与 discipline 逐字一致） ----------

def test_lines_come_from_discipline_not_a_second_copy():
    """本模块不许有自己的阈值：三条线只能来自 `discipline.lines_for`。"""
    d = _decide(86.0)
    assert d["lines"]["stop_loss_close"] == LINES["stop_loss_close"] == 85.00
    assert d["lines"]["stop_loss_weekly"] == LINES["stop_loss_weekly"] == 83.25
    assert d["lines"]["no_add_above"] == LINES["no_add_above"] == 87.00


@pytest.mark.parametrize("close,expected", [
    (84.90, STATE_STOP),   # 跌破止损线
    (85.00, STATE_HOLD),   # **正好落线上不算破**（与 check_stop_loss_close 同口径）
    (86.00, STATE_HOLD),   # 带内持有
    (86.99, STATE_HOLD),
    (87.00, STATE_NO_ADD),  # 正好到禁补仓线 → 禁补仓（check_no_add_above 用 >=）
    (87.50, STATE_NO_ADD),
])
def test_three_price_regimes(close, expected):
    assert _decide(close)["state"] == expected


def test_close_at_stop_line_is_not_a_breach():
    """85.00 是「落在线上」，不是「跌破」—— 差一分才破。"""
    assert _decide(85.00)["state"] == STATE_HOLD
    assert _decide(84.99)["state"] == STATE_STOP


def test_headline_is_state_specific_and_human():
    assert _decide(86.0)["headline"] == "持有不动。"
    assert "不能补仓" in _decide(87.5)["headline"]
    assert "全清" in _decide(84.9)["headline"]


# ---------- 2. 全清试算：走 CostModel 权威入口，逐分一致 ----------

@pytest.mark.parametrize("close", [84.90, 85.00, 86.00, 87.00, 87.50, 100.00])
def test_sell_all_matches_cost_model_to_the_cent(close):
    """`fees_priced_against` 是用明细拼出来的费用 与 `model.fees()` 之差，须 ≤ 0.01。"""
    d = _decide(close)
    s = d["sell_all"]
    m = CostModel(asset_class=ASSET_STOCK)
    fill = m.fill_price("sell", close)

    assert abs(s["fees_priced_against"]) <= 0.01
    assert s["fill_price"] == pytest.approx(fill)
    # 到手金额 = 成交额 − 权威费用（逐分）
    assert s["proceeds"] == pytest.approx(
        round(fill * QTY - m.fees("sell", fill, QTY), 2), abs=0.01)
    # 明细三项之和 == 权威费用
    assert (round(s["commission"], 2) + round(s["transfer_fee"], 2)
            + round(s["stamp_tax"], 2)) == pytest.approx(s["fee_total"], abs=0.01)


def test_sell_all_uses_stock_policy_not_etf():
    """股票口径：印花税仅卖出、佣金有 5 元最低、过户费双边、滑点卖出下调。"""
    s = sell_to_cash(85.00, QTY)
    m = CostModel(asset_class=ASSET_STOCK)
    gross = 85.00 * QTY * (1 - m.slippage_bps / 10_000.0)

    assert s["stamp_tax"] > 0                       # ETF 是 0，股票不是
    assert s["commission"] == pytest.approx(max(gross * 0.00025, 5.0), abs=0.01)
    assert s["transfer_fee"] == pytest.approx(gross * 0.00001, abs=0.01)
    assert s["slippage_per_share"] > 0               # 卖出价被下调
    assert s["proceeds"] < s["gross"]                # 到手一定少于成交额


def test_min_commission_bites_on_small_tickets():
    """小额单笔：佣金被 5 元最低托住（0.025% 算出来不到 5 元）。"""
    s = sell_to_cash(10.00, 100)
    assert s["commission"] == pytest.approx(5.0, abs=0.01)


def test_sell_all_without_total_assets_is_none():
    """总资产不可用 → 不算全清试算（不拿半个数冒充）。"""
    d = _decide(84.9, total_assets=None, cash=None)
    assert d["sell_all"] is None
    # 但结论本身仍然给得出（止损判定不依赖总资产）
    assert d["state"] == STATE_STOP


def test_sell_all_carries_cash_and_total_after():
    d = _decide(84.9)
    s = d["sell_all"]
    assert s["cash_before"] == pytest.approx(CASH, abs=0.01)
    assert s["cash_after"] == pytest.approx(CASH + s["proceeds"], abs=0.01)
    assert s["total_after"] == pytest.approx(TOTAL_ASSETS - s["fee_total"], abs=0.01)


# ---------- 3. 整手规则：一手时只剩「全清 / 不动」 ----------

def test_whole_lot_reason_for_one_lot():
    reason = whole_lot_reason(QTY)
    assert reason is not None
    assert "100" in reason


def test_whole_lot_reason_none_when_divisible():
    assert whole_lot_reason(300) is None


def test_stop_offers_only_two_options():
    d = _decide(84.9)
    assert len(d["options"]) == 2
    joined = " ".join(d["options"])
    assert "不动" in joined and "全清" in joined
    assert "100 股" in joined


def test_hold_says_trim_cannot_be_executed():
    """100 股时「减一点仓」执行不了，必须明说，而不是给出一个做不到的建议。"""
    d = _decide(86.0)
    assert any("执行不了" in b for b in d["because"])


def test_whole_lot_rule_appears_in_details():
    d = _decide(86.0)
    assert any(str(LOT_SIZE) in r for r in d["rules"])


# ---------- 4. 人话：无状态机术语、无英文黑话 ----------

BANNED = ("PASS", "FAIL", "UNDETERMINED", "WARN", "state", "None")


@pytest.mark.parametrize("close", [84.90, 86.00, 87.50])
def test_user_facing_text_has_no_jargon(close):
    d = _decide(close)
    text = " ".join([d["headline"], *d["because"], *d["options"]])
    for word in BANNED:
        assert word not in text, f"首屏文字出现术语 {word!r}"


@pytest.mark.parametrize("close", [84.90, 86.00, 87.50])
def test_options_never_recommend(close):
    """只摆选项、不推荐 —— 系统在没样本外 edge 之前没资格替人做决定。"""
    for opt in _decide(close)["options"]:
        assert not any(w in opt for w in ("建议", "推荐", "应该"))


def test_stop_because_says_it_does_not_choose_for_you():
    d = _decide(84.9)
    assert any("不推荐" in b or "不替你选" in b for b in d["because"])


# ---------- 5. 判不了的时候要说清为什么（不是「没事」） ----------

def test_unknown_when_no_position():
    d = position_decision(CODE, close=86.0, qty=0)
    assert d["state"] == STATE_UNKNOWN
    assert d["because"]


def test_unknown_when_no_lines_configured():
    d = position_decision("600519", close=86.0, qty=QTY,
                          cost_basis=COST_BASIS, total_assets=TOTAL_ASSETS)
    assert d["state"] == STATE_UNKNOWN
    assert any("纪律线" in b for b in d["because"])


def test_unknown_when_close_missing():
    d = _decide(None)
    assert d["state"] == STATE_UNKNOWN
    assert any("收盘价" in b for b in d["because"])
    assert d["sell_all"] is None


def test_unknown_headline_is_not_reassuring():
    """拿不到价格 ≠ 没事：结论句必须说「判不了」，不能默认成「持有不动」。"""
    d = _decide(None)
    assert "判不了" in d["headline"]


# ---------- 6. 价格口径：只有日收盘价，不看盘中价 ----------

def test_disclosure_separates_close_from_intraday_price():
    d = _decide(86.0, price=85.50, price_asof="2026-09-16 14:30",
                price_source="盘中快照")
    text = " ".join(d["disclosure"])
    assert "日收盘价" in text
    assert "bars_daily" in text
    # 盘中价只能出现在说明里，且必须写明「结论不看它」
    assert any("结论不看" in x for x in d["disclosure"])


def test_price_does_not_affect_state():
    """同样的收盘价，盘中价怎么变都不改结论。"""
    a = _decide(84.9, price=99.0)
    b = _decide(84.9, price=1.0)
    assert a["state"] == b["state"] == STATE_STOP


def test_money_block_reports_pnl():
    d = _decide(86.0)
    m = d["money"]
    assert m["market_value"] == pytest.approx(8600.0, abs=0.01)
    assert m["float_pnl"] == pytest.approx(8600.0 - COST_BASIS, abs=0.01)
    assert m["avg_cost"] == pytest.approx(86.80, abs=0.001)


def test_weight_over_limit_is_stated_in_plain_words():
    d = _decide(86.0)
    assert d["weight"]["pct"] == pytest.approx(43.0, abs=0.01)
    assert any("多出" in b and "个百分点" in b for b in d["because"])
