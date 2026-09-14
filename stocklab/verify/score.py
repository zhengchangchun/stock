"""打分口径（Task 32，P7）：`predictions` 的一行 + 实际复权 K 线 → `verifications` 的一行。

## 这是纯函数

没有 `datetime.now()`、没有 sqlite、没有联网。同一输入两次调用**逐键相等** ——
`verifications` 的幂等判定（内容全等 → `identical`）就建立在这个性质上。
取数与归因在 `service.py`。

## 逐条口径（总纲 §8.2；每条都能被 `tests/test_verify_score.py` 里的手算钉住）

| 项 | 怎么算 | 为什么是这个 |
|----|--------|--------------|
| 实际收益 | `adj_close(target)/adj_close(asof) - 1`，复权锚定在 `as_of=target_date` | 比值与锚点无关；`<= target_date` 是硬上限（无未来信息） |
| 方向 | `> +FLAT_BAND` up / `< -FLAT_BAND` down / 其余 flat | §8.2「±0.5% 为 flat」；`FLAT_BAND` **从 `predict.model` 导入**，不在这里写第二遍 |
| 预测方向 | 三概率 `argmax`，平手 `flat > up > down` | 与模型 `action` 的平手判据同序，否则同一份概率会有两个结论 |
| Brier | 三分类 `Σ(p_i - onehot_i)^2`（值域 0–2 存进 `notes`） | §8.2「Brier score 校验概率校准」 |
| `score_direction` | `1 - Brier/2` → 0–1 | 该映射**可逆**：Brier 能从库里反算出来，不必另开一列 |
| `score_range` | 收盘 ∈ `[lo, hi]` → 1 | §8.2 原文是「实际**收盘**是否落入」（盘中冲高不算） |
| 关键位 | resistance 用 `high >= price`、support 用 `low <= price` 判「盘中够到」；`p_touch >= 0.5` 记为「预测会触及」 | 位是「盘中能否够到」；0.5 是决策边界不是拟合参数 |
| `score_action` | `sim_ret > bh_ret` → 1，否则 0 | **参数无关**。不发明「2% 半宽」那种标尺（与 `model.py` 同纪律）；原始超额在 `notes.excess_ret` |
| 动作模拟 | 目标仓位 `size_pct/100`，`asof` 收盘买、`target` 收盘卖，整手 100 股，本金 100 万，`CostModel` 双边 | 与总纲 §11 模拟盘同口径；本金常量只影响 5 元最低佣金，已在报告里声明 |
| 基准 | `buy_and_hold` **同日、同成本、同进出** | 与 `action` 直接可比；index_300 另存 `notes.index_pct`（**无成本**，指数不可交易） |
| `invalidated` | 从 `invalidate_if` **原文**正则取「跌破 X / 站上 Y」，`close < X or close > Y` | 契约是那句话本身；解析不出来就写 `None`（UNDETERMINED），**不猜**。注意 `verifications.invalidated` 是 `NOT NULL`，落库时 0 是类型所迫、真值在 `notes.undetermined` |
| 归因 | 不可评分 → `DATA`；其余一律 `UNDETERMINED` | §9 + 硬约束：`SIGNAL/STRATEGY/MODEL/NOISE` 代码判不了，硬判 = 造假归因 |

## 不可评分（`scorable=False`）

`NO_BAR_TARGET` / `SUSPENDED` / `NO_BAR_ASOF` / `NOT_ADJUSTABLE` 四种。
**不抛异常、返回 `scorable=False` + 原因**，且所有结果列（`hit_*` / `score_*` / `total_score`）
一律 `None` —— 这样统计层**没有**办法把它当成 0 分混进分母（0 分会被平均，
`None` 会显式报错或计数，不会静默变成「预测错了」）。

`NOT_ADJUSTABLE` 是关键的一条：复权序列取不到时**绝不**拿不复权价顶替 ——
不复权序列在除权日是**假跌幅**，会给出一份方向完全相反、且没有任何报错的评分
（ERROR_DIARY 2026-09-14「宽松回退」同款）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from stocklab.config.costs import CostModel
from stocklab.data.models import Bar
from stocklab.predict.model import FLAT_BAND, canonical_json

#: `verifications` 落库与幂等 hash 覆盖的字段（`notes` 是 dict，落库时规范化序列化）。
VERIFICATION_FIELDS: tuple[str, ...] = (
    "pred_id", "target_date", "scorable", "reason_code", "actual_close",
    "actual_pct", "benchmark_pct", "hit_direction", "hit_range", "hit_levels",
    "sim_pnl", "score_direction", "score_range", "score_level", "score_action",
    "total_score", "invalidated", "attribution_auto", "notes",
)

#: §8.2 的四项权重（原文默认值）。刻意放在常量里：**它是被声明的聚合权重**，
#: 不是拟合出来的，报告里要连同这条声明一起展示。
TOTAL_WEIGHTS: dict[str, float] = {
    "direction": 0.3, "range": 0.2, "level": 0.2, "action": 0.3,
}

#: 「预测会触及」的判定阈值。0.5 是决策边界本身，不是调出来的参数。
TOUCH_THRESHOLD = 0.5

#: 模拟本金（元）。只影响 5 元最低佣金与整手取整的相对大小，已在报告披露。
CAPITAL = 1_000_000.0

#: A 股买入最小单位（股）。
LOT = 100

#: 归因取值（**只有这两个**由程序写）。
ATTRIBUTION_DATA = "DATA"
ATTRIBUTION_UNDETERMINED = "UNDETERMINED"

#: 代码**永远不许**自己写出来的四类归因（人工标注列 `attribution_manual` 专属）。
HUMAN_ONLY_ATTRIBUTIONS = ("SIGNAL", "STRATEGY", "MODEL", "NOISE")

#: `invalidate_if` 的模板（由 `model.compute_forecast` 生成）里的两个条件。
_RE_BREAK = re.compile(r"跌破\s*([0-9]+(?:\.[0-9]+)?)")
_RE_RECLAIM = re.compile(r"站上\s*([0-9]+(?:\.[0-9]+)?)")

_DIRECTIONS = ("up", "flat", "down")

#: 平手时的优先序，**与模型 `action` 的判据同序**（`p_flat >= p_up and p_flat >= p_down` → wait）。
#: 刻意不写成 `_DIRECTIONS` 的顺序：那份顺序是「算 Brier 的固定枚举序」，
#: 两者混用会让「平手算 flat」这条在重构时被静默改掉。
_TIE_PRIORITY = ("flat", "up", "down")


def verification_hash_fields(payload: Mapping) -> str:
    """载荷的内容 sha256（只覆盖 `VERIFICATION_FIELDS`）。

    与预测侧 `payload_hash` 同一套规范化规则（`predict.model.canonical_json`）
    —— 两处各写一遍 `json.dumps` 迟早会漂移，那会让「内容全等」这个判据失真。
    """
    contract = {k: payload[k] for k in VERIFICATION_FIELDS}
    return hashlib.sha256(
        canonical_json(contract).encode("utf-8")).hexdigest()


class Unscorable(Exception):
    """数据层无法给出可打分的实际行情。

    打分器本身**不抛**它（返回 `scorable=False` 更便于批量统计），
    它留给「必须立刻停下」的调用方（例如未来把验证接进日循环时，
    想要「今天有预测却一根 bar 都没有」直接炸掉而不是静默少算一天）。
    """

    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(f"{reason_code}: {reason}")
        self.reason_code = reason_code


def _by_date(bars: Sequence[Bar]) -> dict[str, Bar]:
    return {b.date: b for b in bars}


def _actual_class(pct: float) -> str:
    if pct > FLAT_BAND:
        return "up"
    if pct < -FLAT_BAND:
        return "down"
    return "flat"


def _predicted_class(direction: Mapping[str, float]) -> str:
    """三概率 argmax，平手按 `flat > up > down`（与模型 action 判据同序）。"""
    return max(_TIE_PRIORITY, key=lambda c: (direction[c], -_TIE_PRIORITY.index(c)))


def _round_trip_pnl(close_a: float, close_t: float, *, weight: float,
                    costs: CostModel, capital: float) -> float:
    """`asof` 收盘买入 `weight` 仓位、`target` 收盘全部卖出后的净盈亏（元）。

    整手取整（100 股）、买卖各扣一次 `CostModel` 费用（含最低佣金 5 元与印花税）。
    目标仓位为 0 时**不产生任何费用** —— 空仓就是空仓，不该被收一笔钱。
    """
    if weight <= 0:
        return 0.0
    buy_fill = costs.fill_price("buy", close_a)
    qty = int((capital * weight) // (buy_fill * LOT)) * LOT
    if qty <= 0:
        return 0.0
    sell_fill = costs.fill_price("sell", close_t)
    gross = qty * (sell_fill - buy_fill)
    fees = costs.fees("buy", buy_fill, qty) + costs.fees("sell", sell_fill, qty)
    return round(gross - fees, 2)


def _unscorable(pred: Mapping, reason_code: str, reason: str) -> dict:
    """不可评分的载荷：结果列全 `None`，归因 `DATA`。"""
    out: dict[str, Any] = {k: None for k in VERIFICATION_FIELDS}
    out["pred_id"] = pred["pred_id"]
    out["target_date"] = pred["target_date"]
    out["scorable"] = False
    out["reason_code"] = reason_code
    out["attribution_auto"] = ATTRIBUTION_DATA
    out["notes"] = {"scorable": False, "reason_code": reason_code,
                    "reason": reason}
    return out


def score_prediction(pred: Mapping, *, bars: Sequence[Bar], raw_bars: Sequence[Bar],
                     suspended: Collection[str] = (), costs: CostModel | None = None,
                     capital: float = CAPITAL,
                     index_pct: float | None = None,
                     adjust_error: str | None = None) -> dict:
    """给一条预测打分。

    - `bars`：**复权**日 K（`as_of=target_date` 口径，只含 `<= target_date` 的行）。
      调用方（`service.load_scoring_bars`）负责裁剪；本函数的兜底校验是
      「`asof_date` 与 `target_date` 两根都必须**正好**在序列里」——
      `>=` / 「取最近一根」这类宽松查找会拿**别的交易日**冒充目标日，
      那是最危险的一类错（数字合法、结论全错）。
    - `raw_bars`：**不复权**日 K，只用于判「这根 bar 到底存不存在」。
    - `suspended`：该标的停牌的日期集合（`bars_daily.is_suspended=1`）。
    - `index_pct`：基准指数当日涨跌（可缺，缺了不影响可评分性）。
    """
    costs = costs or CostModel()
    asof, target = pred["asof_date"], pred["target_date"]
    raw = _by_date(raw_bars)

    if target not in raw:
        return _unscorable(pred, "NO_BAR_TARGET",
                           f"{pred['code']} 在目标日 {target} 没有 K 线（停牌/未产生/采集缺口）")
    if target in set(suspended):
        return _unscorable(pred, "SUSPENDED",
                           f"{pred['code']} 在目标日 {target} 停牌 —— 无成交，不可评分")
    if asof not in raw:
        return _unscorable(pred, "NO_BAR_ASOF",
                           f"{pred['code']} 的基准日 {asof} 没有 K 线，算不出当日收益")

    adj = _by_date(bars)
    if asof not in adj or target not in adj:
        return _unscorable(
            pred, "NOT_ADJUSTABLE",
            f"{pred['code']} 在 [{asof}, {target}] 取不到可用的**复权**价"
            "（复权链缺口）。**拒绝用不复权价代替** —— 除权日的假跌幅会让方向反过来"
            + (f"；底层报错：{adjust_error}" if adjust_error else ""))

    close_a = float(adj[asof].close)
    bar_t = adj[target]
    close_t = float(bar_t.close)
    actual_pct = close_t / close_a - 1.0

    actual_class = _actual_class(actual_pct)
    pred_class = _predicted_class(pred["direction"])
    p = {c: float(pred["direction"][c]) for c in _DIRECTIONS}
    # 三分类 Brier：Σ(p_i - onehot_i)^2，值域 0–2
    brier = sum((p[c] - (1.0 if c == actual_class else 0.0)) ** 2 for c in _DIRECTIONS)
    score_direction = round(1.0 - brier / 2.0, 6)

    lo, hi = (float(x) for x in pred["range_80"])
    hit_range = int(lo <= close_t <= hi)

    levels = list(pred.get("key_levels") or [])
    realized: list[int] = []
    level_detail: list[dict] = []
    for lv in levels:
        price = float(lv["price"])
        role = lv["role"]
        touched = (float(bar_t.high) >= price if role == "resistance"
                   else float(bar_t.low) <= price)
        predicted_touch = float(lv["p_touch"]) >= TOUCH_THRESHOLD
        realized.append(int(touched == predicted_touch))
        level_detail.append({"price": price, "role": role,
                             "p_touch": float(lv["p_touch"]),
                             "predicted_touch": predicted_touch,
                             "touched": touched,
                             "realized": int(touched == predicted_touch)})
    if levels:
        hit_levels: int | None = int(all(realized))
        score_level: float | None = round(sum(realized) / len(realized), 6)
    else:                                        # pragma: no cover - 模型恒给两个位
        hit_levels, score_level = None, None

    weight = float(pred["size_pct"]) / 100.0
    sim_pnl = _round_trip_pnl(close_a, close_t, weight=weight, costs=costs,
                              capital=capital)
    bh_pnl = _round_trip_pnl(close_a, close_t, weight=1.0, costs=costs,
                             capital=capital)
    sim_ret = sim_pnl / capital
    bh_ret = bh_pnl / capital
    excess_ret = sim_ret - bh_ret
    # 参数无关：严格跑赢同日 buy_and_hold 才算 1。平手算 0（不给自己送分）。
    score_action: float | None = 1.0 if excess_ret > 0 else 0.0

    invalidated, undetermined = _invalidated(pred.get("invalidate_if"), close_t)

    if score_level is None:                      # pragma: no cover
        total: float | None = None
    else:
        total = round(TOTAL_WEIGHTS["direction"] * score_direction
                      + TOTAL_WEIGHTS["range"] * hit_range
                      + TOTAL_WEIGHTS["level"] * score_level
                      + TOTAL_WEIGHTS["action"] * score_action, 6)

    return {
        "pred_id": pred["pred_id"],
        "target_date": target,
        "scorable": True,
        "reason_code": None,
        "actual_close": round(close_t, 4),
        "actual_pct": round(actual_pct, 6),
        "benchmark_pct": round(bh_ret, 8),
        "hit_direction": int(pred_class == actual_class),
        "hit_range": hit_range,
        "hit_levels": hit_levels,
        "sim_pnl": sim_pnl,
        "score_direction": score_direction,
        "score_range": float(hit_range),
        "score_level": score_level,
        "score_action": score_action,
        "total_score": total,
        "invalidated": invalidated,
        "attribution_auto": ATTRIBUTION_UNDETERMINED,
        "notes": {
            "scorable": True,
            "brier": round(brier, 8),
            "pred_class": pred_class,
            "actual_class": actual_class,
            "probabilities": p,
            # 复权锚点与两根实际收盘：审计「这个 actual_pct 是怎么来的」用
            "adj_anchor": target,
            "close_asof": round(close_a, 4),
            "close_target": round(close_t, 4),
            "levels": level_detail,
            "sim_ret": round(sim_ret, 8),
            "bh_ret": round(bh_ret, 8),
            "excess_ret": round(excess_ret, 8),
            "index_pct": None if index_pct is None else round(index_pct, 8),
            "capital": capital,
            "size_pct": float(pred["size_pct"]),
            "total_weights": dict(TOTAL_WEIGHTS),
            "undetermined": undetermined,
        },
    }


def parse_invalidate_bounds(text: str | None) -> tuple[float, float] | None:
    """从 `invalidate_if` **原文**解析出 `(下界, 上界)`；解析不出返回 `None`。

    刻意解析原文而不是复用 `key_levels`：契约是给用户看的那句话本身。
    `service` 侧有一条测试交叉核对「原文里的两个数与 `key_levels` 一致」，
    两者漂移时测试会红，而不是由某个函数替它圆场。
    """
    if not text:
        return None
    break_at = _RE_BREAK.search(text)
    reclaim_at = _RE_RECLAIM.search(text)
    if not break_at or not reclaim_at:
        return None
    return float(break_at.group(1)), float(reclaim_at.group(1))


def _invalidated(text: str | None, close_t: float) -> tuple[int | None, list[str]]:
    bounds = parse_invalidate_bounds(text)
    if bounds is None:
        return None, ["invalidate_if"]
    lo, hi = bounds
    return int(close_t < lo or close_t > hi), []
