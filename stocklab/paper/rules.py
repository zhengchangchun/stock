"""模拟盘规则（P19）：**纯函数**，输入数字输出决定，不碰数据库、不读时钟。

## 为什么规则要抽成纯函数

① 规则是这套系统里唯一**可被检验**的东西（模型的 edge ≈ 0）；
② 纯函数让「为什么不动」变成一个**可断言的返回值**，而不是日志里的一句话；
③ 边界（正好 40% / 正好 45%）可以被直接单测，不必构造一整个数据库。

## 三条口径（口径不许改，见 `docs/plans/2026-09-15-p19-模拟盘骨架.md` §2.4）

1. **整手一律向下取整**（复用 `risk/sizing.py` 的口径：宁可不动，也不放大仓位）。
   推论：1 手仓位「想减 10%」→ 可执行量只有 0 或 100 → **取 0（不动）**，
   并把「违规未消除」如实上报（`violation_remaining=True`），**绝不假装已合规**。
2. **单次动用现金 = 每步（每日）累计**（`PER_STEP_CASH_PCT`）——
   按「单笔」解读可被拆单绕过，而风险规则错的方向必须是**少投**。
3. **白名单即分散**：只有 510300 / 510880。非白名单标的**报错**，不静默照买。

## 禁止方向择时

本模块不 import `stocklab.predict` / `stocklab.risk.kelly` ——
模拟盘对照的是纪律与分散，不是模型预测。`test_paper_never_imports_model_or_kelly`
扫描源码钉住这条。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from stocklab.config.costs import ASSET_ETF, CostModel
from stocklab.paper.config import (
    ETF_WHITELIST,
    LOT,
    ORDER_TARGET_AMOUNT,
    PER_STEP_CASH_PCT,
    RULE_CITATIONS,
)
from stocklab.portfolio.discipline import (
    DISCIPLINE,
    check_stop_loss_close,
    lines_for,
)
from stocklab.portfolio.prices import Price

# ---------- 约束代号（进 `binding_constraints`，机器可读、测试可断言） ----------

C_LOT = "lot_100"                        # 整手约束（不足 1 手 → 不动）
C_SINGLE = "single_position_max_40pct"   # 单票 ≤40%
C_CASH_FLOOR = "cash_band_floor_45pct"   # 现金 ≥45%（带的下沿是硬约束）
C_PER_STEP = "cash_per_step_max_5pct"    # 单次（每步）动用现金 ≤5% 总资产
C_ORDER_TARGET = "order_target_1000"     # 单笔金额 ≈1,000 元（摊薄最低佣金）
C_NO_ADD = "no_add_above_87"             # 现价 ≥87.00 禁补仓
C_WHITELIST = "diversifier_whitelist"    # 分散工具白名单
C_NO_PRICE = "missing_price"             # 取不到价格 → 不判定
C_NO_LINES = "no_discipline_lines"       # 该标的没配纪律线 → 不拿别人的线量它
C_STOP_OFF = "stop_loss_off_by_spec"      # 止损按 spec 关闭（只可能出现在 arm-agent*）

_EPS = 1e-9


@dataclass(frozen=True)
class RuleParams:
    """一次决策用到的**全部可参数化条文**。默认值 = 写死条文（静态三臂口径）。

    ## 为什么默认值必须逐字等于现有常量

    这张 dataclass 存在的唯一理由是让 `arm-agent` 与静态臂**共用同一个执行内核**
    （设计 §4.3：不许复制规则代码）。既然共用，静态臂就必须走「默认参数」这条路径 ——
    否则两套参数各跑各的，迟早漂移，而漂移是静默的：净值只是「有点不一样」。

    - 默认值一律引用 `paper/config.py` / `portfolio/discipline.py` 的**现值**，
      不在本模块抄字面量（`test_static_params_are_the_written_rules` 逐字段对表）；
    - `STATIC_PARAMS` 是那个默认实例，也是 `plan_*` 的默认参数；
    - 静态臂「零变化」的最终证据是 `paper show` 的逐字节对拍（任务书 T3）。

    ## 条文表为什么不放进默认值

    `citations` 默认空元组 = 用模块级 `RULE_CITATIONS`。把 dict 塞成默认值会让
    这个 frozen dataclass 不可哈希（`replace()` 与相等比较都要用它）；
    空元组表示「没覆盖」，语义上也更准。
    """

    #: ETF 目标占比。`None` = 本账户不做分散建仓（`arm-hold` / `arm-now`）。
    etf_target_pct: float | None = None
    whitelist: tuple[str, ...] = ETF_WHITELIST
    single_position_max_pct: float = DISCIPLINE["single_position_max_pct"]
    cash_floor_pct: float = DISCIPLINE["cash_band_pct"][0]
    per_step_cash_pct: float = PER_STEP_CASH_PCT
    order_target_amount: float = ORDER_TARGET_AMOUNT
    lot: int = LOT
    #: 止损线（**绝对价**）。`None` ⇒ 用 `lines_for(code)` 的线（静态臂走这条）。
    stop_loss_line: float | None = None
    #: `False` ⇒ 不判止损。只有 spec 的 `stop_loss_pct="off"` 会关掉它。
    stop_loss_enabled: bool = True
    #: 条文表覆盖（`()` ⇒ 用 `RULE_CITATIONS`，即写死条文）。
    citations: tuple[tuple[str, str], ...] = ()
    #: 溯源标签（spec 指纹），只有 `arm-agent*` 非空；为空则 reason 一字不改。
    spec_tag: str = ""

    def cite(self, key: str) -> str:
        """条文原文：有覆盖就用覆盖的，否则用写死条文。"""
        for k, text in self.citations:
            if k == key:
                return text
        return RULE_CITATIONS[key]

    def cash_floor_code(self) -> str:
        """现金下限的约束代号**带着那个百分数**：换了下限就必须换代号。

        硬用 `C_CASH_FLOOR`（`cash_band_floor_45pct`）去标一条 50% 的下限，
        等于在 append-only 的 `binding_json` 里写下一句假话 —— 以后统计
        「45% 那条下限 bind 过几次」就会把 50% 的单子也算进去。
        """
        if self.cash_floor_pct == DISCIPLINE["cash_band_pct"][0]:
            return C_CASH_FLOOR
        return f"cash_floor_min_{self.cash_floor_pct:g}pct"

    def per_step_code(self) -> str:
        if self.per_step_cash_pct == PER_STEP_CASH_PCT:
            return C_PER_STEP
        return f"cash_per_step_max_{self.per_step_cash_pct:g}pct"

    def order_target_code(self) -> str:
        if self.order_target_amount == ORDER_TARGET_AMOUNT:
            return C_ORDER_TARGET
        return f"order_target_{self.order_target_amount:g}"

    def amount_text(self, key: str) -> str:
        """约束项 → 人话（**数字从这里取，不写死**）。"""
        return {
            "leg_gap": "该腿缺口",
            "order_target": f"单笔目标 {self.order_target_amount:,.0f} 元",
            "per_step": f"本步剩余额度 {self.per_step_cash_pct:.0f}%",
            "cash_floor": f"现金下限 {self.cash_floor_pct:.0f}%",
        }[key]


#: 「写死条文」的那一组参数，也是 `plan_*` 的默认值。
STATIC_PARAMS = RuleParams()


def _params(p: RuleParams | None) -> RuleParams:
    return STATIC_PARAMS if p is None else p


class LookaheadError(RuntimeError):
    """用了未来信息（`price_asof > asof`）。

    **报错而不是截断**：截断会静默产出一个「看着像决策」的东西，
    而它其实基于当时还不存在的数据 —— 这类错误在做实验时是致命的
    （策略的漂亮成绩全部来自未来价）。
    """


@dataclass(frozen=True)
class Decision:
    """一次决策。`action == "hold"` 也是决策（**不动的理由同样是结论**）。"""

    action: str                      # 'buy' | 'sell' | 'hold'
    code: str | None
    qty: int
    rule_citation: str
    reason: str
    binding_constraints: tuple[str, ...] = ()
    planned_shares_raw: float = 0.0  # 整手取整**之前**想动的股数（用于解释为什么不动）
    violation_remaining: bool = False  # 硬约束**未消除**（不许悄悄放过）
    ref_price: float | None = None
    fill_price: float | None = None
    amount: float = 0.0              # 现金流出（买，含费）或流入（卖，扣费）
    fees: dict = field(default_factory=dict)
    cash_after: float | None = None
    weight_after_pct: float | None = None
    asset_class: str | None = None
    #: 价格出处（由引擎从 `Price` 填；规则层不认识数据源）
    price_source: str | None = None
    price_asof: str | None = None

    @property
    def is_trade(self) -> bool:
        """**真的能执行**的成交（有方向**且有股数**）。

        `qty > 0` 进这个判据不是修辞：只有方向没有股数的「卖 0 股」曾经一路
        传到写入层，被 `paper_trades` 的 `CHECK (qty > 0)` 拦成一个
        `IntegrityError` —— 而 `step` 是**一个事务**，于是一天的净值全部回滚。
        触发路径很平常：持仓已在昨天止损清完，今天收盘仍在线下，
        规则说「整清 0 股」。见 ERROR_DIARY #49。
        """
        return self.action in ("buy", "sell") and self.qty > 0


def _hold(code: str | None, reason: str, *, rule: str = "",
          constraints: tuple[str, ...] = (), raw: float = 0.0,
          remaining: bool = False, weight_after: float | None = None,
          ref_price: float | None = None) -> Decision:
    return Decision(action="hold", code=code, qty=0, rule_citation=rule,
                    reason=reason, binding_constraints=tuple(sorted(set(constraints))),
                    planned_shares_raw=raw, violation_remaining=remaining,
                    weight_after_pct=weight_after, ref_price=ref_price)


def floor_lot(shares: float, lot: int = LOT) -> int:
    """整手**向下**取整；负数按 0（不能卖负数、也不能买负数）。"""
    if shares <= 0:
        return 0
    return int(shares // lot) * lot


def _clip(pct: float | None, *, nd: int = 4) -> float | None:
    return None if pct is None else round(pct, nd)


# ---------- PIT 守卫 ----------

def check_no_lookahead(asof: str, prices: Mapping[str, Price]) -> None:
    """任何 `price_asof > asof` 一律抛错（验收 ⑤：喂未来价必须报错）。

    `price_asof` 是**价格实际所属的日期**，不一定等于 asof：
    停牌时用上一交易日收盘是合法的（`< asof`），拿明天的价就是不合法。
    """
    for code, p in prices.items():
        if p.price_asof > asof:
            raise LookaheadError(
                f"未来函数：{code} 的价格属于 {p.price_asof}，晚于决策日 {asof}；"
                f"当日决策只能用 ≤ {asof} 的收盘价（PIT）"
            )


# ---------- 止损（收盘价口径） ----------

def plan_stop_loss(*, code: str, close: float | None, qty: int,
                   costs: CostModel | None = None,
                   source: str | None = None,
                   price_asof: str | None = None,
                   params: RuleParams | None = None) -> Decision:
    """收盘价跌破止损线 → **整清**。判据复用 `discipline.check_stop_loss_close`。

    判据复用而不是重写：纪律线的数字只有一处真相，判定语义（「跌破才动，
    正好在线上不算破」）也只有一处。

    止损线从哪来由 `params` 决定：`None` ⇒ 该标的写死的纪律线（静态臂）；
    给了绝对值 ⇒ 用它的（`arm-agent` 按 spec 的 `stop_loss_pct` 推出）。
    """
    p = _params(params)
    if not p.stop_loss_enabled:
        return _hold(
            code,
            f"止损规则按 spec 关闭（`stop_loss_pct=\"off\"`）→ 本步只有「单票上限」"
            f"与「分散建仓」两条在跑。**关闭不等于风险消失**，只是不再由这条规则拦住",
            constraints=(C_STOP_OFF,), ref_price=close)
    line = p.stop_loss_line
    if line is None:
        lines = lines_for(code)
        if lines is None:
            return _hold(code, f"{code} 未配置纪律线（止损线由入场价推出，不是全局常数），"
                               f"不拿别的标的的线去量它", constraints=(C_NO_LINES,))
        line = float(lines["stop_loss_close"])
    check = check_stop_loss_close(code, close, source=source, price_asof=price_asof,
                                  line=line)
    if close is None:
        return _hold(code, f"无收盘价（{C_NO_PRICE}）：持仓与止损都不判定，"
                           f"不拿成本价冒充现价", constraints=(C_NO_PRICE,))
    if check["status"] != "FAIL":
        return _hold(code, f"收盘 {close:.2f} 未触发止损线 {line:.2f}（跌破才动）",
                     rule=p.cite("stop_loss"), ref_price=close)
    if qty <= 0:
        # 规则失效了、但手里没货（典型：昨天已按这条线整清，今天收盘仍在线下）。
        # 这里必须回 `hold` 而不是「卖 0 股」：后者 `is_trade` 会把它当成成交，
        # 一路走到 `paper_trades` 的 `CHECK (qty > 0)` 上，把整个 `step` 事务拖垮。
        return _hold(
            code,
            f"{code} 收盘 {close:.2f} **跌破**止损线 {line:.2f}，但持仓为 0 股"
            f"→ 无可执行动作（不是忘了卖）",
            rule=p.cite("stop_loss"), ref_price=close)
    costs = costs or CostModel()
    fill, fees = costs.total("sell", close, qty)
    fee_parts = _fee_parts(costs, "sell", fill, qty, ref=close)
    return Decision(
        action="sell", code=code, qty=qty,
        rule_citation=p.cite("stop_loss"),
        reason=(f"{code} 收盘 {close:.2f} **跌破**止损线 {line:.2f} → 整清 {qty} 股"
                f"（含费净收 ¥{fill * qty - fees:,.2f}）"),
        ref_price=close, fill_price=round(fill, 4), fees=fee_parts,
        amount=round(fill * qty - fees, 2), cash_after=None,
        weight_after_pct=0.0, asset_class=costs.asset_class,
    )


# ---------- 单票 40% 上限 ----------

def plan_trim(*, code: str, qty: int, close: float, total_assets: float,
              costs: CostModel | None = None, intent_pct: float | None = None,
              params: RuleParams | None = None) -> Decision:
    """单票权重 >上限 → 减到 ≤上限（整手向下取整；不足 1 手 → 不动 + 如实上报）。

    `intent_pct` 给定时用它当减仓意图（用户声明的 `trim_light_pct` = 单次减仓 10%），
    但**仍以「减到 ≤上限 所需」为下界**：上限是硬约束，10% 只是动作偏好，
    两者冲突时硬约束赢。默认（`None`）直接用「减到上限所需」。

    上限的值从 `params.single_position_max_pct` 取（静态臂 = `DISCIPLINE` 的 40%）。

    取整方向：**向下**。向上会把仓位减过头（且可能把一笔 10% 的减仓变成整清），
    向下则最坏是「不动」，此时 `violation_remaining=True` 把违规如实标出来。
    """
    p = _params(params)
    limit = p.single_position_max_pct
    lot = p.lot
    costs = costs or CostModel()
    mv = close * qty
    weight = mv / total_assets * 100.0 if total_assets > 0 else None
    if weight is None:
        return _hold(code, f"总资产不可用，算不出权重（{C_NO_PRICE}）",
                     constraints=(C_NO_PRICE,), ref_price=close)
    if weight <= limit + _EPS:
        return _hold(code, f"{code} 占总资产 {weight:.2f}%，未超 {limit:.0f}% 上限",
                     rule=p.cite("single_max"), ref_price=close,
                     weight_after=_clip(weight))

    needed_value = (weight - limit) / 100.0 * total_assets
    need_shares = needed_value / close
    if intent_pct is None:
        raw = need_shares
    else:
        raw = max(intent_pct / 100.0 * qty, need_shares)
    constraints = [C_SINGLE]

    sell = floor_lot(raw, lot)
    if sell <= 0:
        constraints.append(C_LOT)
        return _hold(
            code,
            f"{code} 占总资产 {weight:.2f}%，超 {limit:.0f}% 上限；"
            f"想减 {raw:.2f} 股 < 1 手（{lot} 股）→ **不动**。"
            f"硬约束**未消除**，如实上报（不假装已合规，也不擅自整清）",
            rule=p.cite("single_max"), constraints=tuple(constraints),
            raw=raw, remaining=True, ref_price=close, weight_after=_clip(weight))

    # 卖出会减少总资产（手续费），故减完要**回代验证**；不够就再加一手，
    # 直到满足或清仓（宁可多减一手，也不留一个「减了但还超限」的状态）。
    while sell < qty:
        if _weight_after_sell(code, close, qty, sell, total_assets, costs) <= limit + _EPS:
            break
        sell += lot
    sell = min(sell, qty)
    remaining = _weight_after_sell(code, close, qty, sell, total_assets, costs) > limit + _EPS
    fill, fees = costs.total("sell", close, sell)
    if sell == qty:
        constraints.append(C_LOT)
    return Decision(
        action="sell", code=code, qty=sell,
        rule_citation=p.cite("single_max"),
        reason=(f"{code} 占总资产 {weight:.2f}%，超 {limit:.0f}% 上限 "
                f"{weight - limit:.2f} 个百分点 → 卖出 {sell} 股"
                f"（想减 {raw:.2f} 股，整手向下取整；卖后权重 "
                f"{_weight_after_sell(code, close, qty, sell, total_assets, costs):.2f}%）"),
        binding_constraints=tuple(sorted(set(constraints))),
        planned_shares_raw=round(raw, 4), violation_remaining=remaining,
        ref_price=close, fill_price=round(fill, 4),
        fees=_fee_parts(costs, "sell", fill, sell, ref=close),
        amount=round(fill * sell - fees, 2),
        weight_after_pct=_clip(_weight_after_sell(code, close, qty, sell,
                                                  total_assets, costs)),
        asset_class=costs.asset_class,
    )


def _weight_after_sell(code: str, close: float, qty: int, sell: int,
                       total_assets: float, costs: CostModel) -> float:
    """卖 `sell` 股之后的权重（%，含费对总资产的影响）。"""
    fill, fees = costs.total("sell", close, sell)
    mv_after = close * (qty - sell)
    total_after = total_assets - fees          # 成交本身不改变总资产，只有费会
    return mv_after / total_after * 100.0 if total_after > 0 else 0.0


# ---------- 分散建仓（白名单 ETF） ----------

def etf_leg_targets(*, etf_target_pct: float, total_assets: float,
                    whitelist: tuple[str, ...] = ETF_WHITELIST) -> dict[str, float]:
    """ETF 目标金额**在两条腿之间均分**。

    均分是刻意的：给 510880/510300 定权重就是「挑一个更看好」，
    而本臂明确**不做方向判断**（模型方向能力 ≈ 0）。没有偏好 = 均分。
    """
    total = total_assets * etf_target_pct / 100.0
    n = len(whitelist)
    return {code: total / n for code in whitelist}


def plan_etf_buy(*, code: str, price: float, cash: float, total_assets: float,
                 leg_gap_value: float, costs: CostModel,
                 deployed_today: float = 0.0,
                 params: RuleParams | None = None) -> Decision:
    """白名单 ETF 的买入计划：可动用额度取四项的**最小值**，并逐条留痕。

    四项：① 该腿缺口 ② 单笔目标 ③ 本步剩余额度（`per_step_cash_pct` 总资产 − 今日已动用）
    ④ 现金下限（现金 − `cash_floor_pct` 总资产）。四个参数全部从 `params` 取，
    默认 = 写死条文。

    `code` 不在白名单 → **抛 ValueError**：换家电股/家电 ETF 不算分散，
    静默照买就是拿「加倍下注同一个行业」冒充分散。
    """
    p = _params(params)
    whitelist, lot = p.whitelist, p.lot
    if code not in whitelist:
        raise ValueError(
            f"{C_WHITELIST}: {code} 不在分散白名单 {list(whitelist)} 中 —— "
            f"换家电股（600690）或家电 ETF 不算分散，不许拿它当分散工具"
        )
    if costs.asset_class != ASSET_ETF:
        raise ValueError(
            f"{code} 是 ETF（ADR-008 标的口径），成本模型给的是 "
            f"{costs.asset_class!r} —— 口径错会让成本算错，且错的方向对策略有利"
        )
    floor_value = p.cash_floor_pct / 100.0 * total_assets
    per_step_cap = p.per_step_cash_pct / 100.0 * total_assets
    remaining_step = max(0.0, per_step_cap - deployed_today)
    available = max(0.0, cash - floor_value)
    cands = {"leg_gap": leg_gap_value, "order_target": p.order_target_amount,
             "per_step": remaining_step, "cash_floor": available}
    codes = {"order_target": p.order_target_code(), "per_step": p.per_step_code(),
             "cash_floor": p.cash_floor_code(), "leg_gap": "leg_gap"}
    budget = min(cands.values())
    # 与最小值相等的都算「binding」（并列时全部列出，不挑一个）
    binding = tuple(sorted({codes[k] for k, v in cands.items()
                            if v <= budget + _EPS}))
    fill = costs.fill_price("buy", price)
    if budget <= 0:
        return _hold(code, f"本次可动用 ¥{budget:,.2f}（"
                           f"{_binding_text(cands, p)}）→ 不动",
                     rule=p.cite("etf_first_build"),
                     constraints=binding, ref_price=price)
    qty = floor_lot(budget / fill, lot)
    if qty <= 0:
        one_lot = fill * lot
        return _hold(
            code,
            f"1 手需 ¥{one_lot:,.2f}，本次可动用 ¥{budget:,.2f}（{_binding_text(cands, p)}）"
            f"→ 不足 1 手，**不动**（不四舍五入买一手：那会放大到超过纪律允许的额度）",
            rule=p.cite("etf_first_build"),
            constraints=tuple(sorted(set(binding) | {C_LOT})), ref_price=price)
    fees = _fee_parts(costs, "buy", fill, qty, ref=price)
    outflow = fill * qty + fees["total"]
    return Decision(
        action="buy", code=code, qty=qty,
        rule_citation=p.cite("etf_first_build"),
        reason=(f"分散建仓：买 {qty} 份 {code} @{fill:.4f}（含滑点），"
                f"支出 ¥{outflow:,.2f}；可动用额度受 {_binding_text(cands, p)} 约束"),
        binding_constraints=binding, ref_price=price, fill_price=round(fill, 4),
        fees=fees, amount=round(outflow, 2), cash_after=round(cash - outflow, 2),
        asset_class=costs.asset_class,
    )


def _binding_text(cands: Mapping[str, float], p: RuleParams) -> str:
    return "、".join(f"{p.amount_text(k)} ¥{v:,.2f}" for k, v in cands.items())


def _fee_parts(costs: CostModel, side: str, price: float, qty: int, *,
               ref: float) -> dict:
    """把成本**拆成明细分列**返回（佣金/印花税/过户费/滑点）。

    分列而不是只给合计：`paper_trades` 要能查到「ETF 免印花税」这条口径
    （ADR-008），只存合计就查不出来了 —— 口径错是静默错误。
    """
    amount = price * qty
    commission = max(amount * costs.commission_rate, costs.min_commission)
    transfer = amount * costs.transfer_fee_rate
    stamp = amount * costs.stamp_tax_rate if side == "sell" else 0.0
    slip = abs(price - ref) * qty
    return {"commission": round(commission, 2), "stamp_tax": round(stamp, 2),
            "transfer_fee": round(transfer, 2), "slippage_cost": round(slip, 2),
            "total": round(commission + transfer + stamp, 2)}
