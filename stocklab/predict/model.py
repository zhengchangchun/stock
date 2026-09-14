"""`pit_rw_v1`：由**当前可用证据**算出预测载荷的纯函数（Task 29，P6）。

## 这个模型是什么，不是什么

它是「**PIT 随机游走 + 实测波动率**」：把最近 `WINDOW` 个交易日的对数收益
当成次日收益分布的样本，用 `Normal(mean, stdev)` 去算三分类概率、区间与触及概率。
**它不预测得准**，它的全部意义是把「今晚该给的预测」写成一个**次日可以证伪**的对象。

明确拒绝的三件事：

1. **凭空常数**。`0.42 / 0.31 / 0.27` 这种「看起来像概率」的写法在本模块不可能出现：
   每个数字都能被 `evidence.inputs` 里的 `mu` / `sigma` / `close` 复算出来
   （`test_probabilities_match_the_documented_formula` 就是手算一遍的对照）。
2. **读全 NULL 的字段**。`features_daily` 的 `regime_label` / `main_net_5d` /
   `pe_pct_3y` 当前全为 NULL。本模块**不收 features 参数**，所以
   「NULL 被当成 0」在结构上不可能发生（不是靠自觉）。
3. **把 `size_pct` 说成可执行建议**。它没有成本、没有风险预算、不知道用户持仓，
   载荷里原样写明（`evidence.notes.size_pct`）。

## 参数不是旋钮

`WINDOW` / `LEVEL_WINDOW` 是**口径**（估计窗有多长），`FLAT_BAND` 由总纲 §8.2 规定。
本任务**不做任何参数搜索** —— 搜了就无法把结果归因到模型本身
（R7 反过拟合红线：一次实验只改一个变量）。
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence

from stocklab.data.models import Bar
from stocklab.predict.version import MODEL_ID, MODEL_SPEC, MODEL_VERSION

#: 波动率/漂移的估计窗（交易日）。与 `trend_ma` 的 `slow=60` 同量级，
#: 但不是同一个东西：这里是**统计估计窗**，那里是均线窗。
WINDOW = 60

#: 关键位（支撑/阻力）的回看窗。
LEVEL_WINDOW = 20

#: 三分类阈值：|次日收益| <= 0.5% 记为 flat。**由总纲 §8.2 规定**，不是本实现的自选。
FLAT_BAND = 0.005

#: `range_80` 的名义覆盖：中心 80% 分位区间。
RANGE_COVERAGE = 0.80

#: 总纲 §8.1 的载荷契约字段。`payload_hash` **只**覆盖这些字段 ——
#: `created_at` / `pred_id` 不参与，所以「同 asof 同 model_version 重复运行
#: 逐字节一致」是能被验证的。
CONTRACT_FIELDS: tuple[str, ...] = (
    "code", "asof_date", "target_date", "direction", "range_80", "key_levels",
    "action", "size_pct", "invalidate_if", "strategy_mix", "model_version",
)


class DegenerateInput(ValueError):
    """输入不足以给出分布 —— **拒绝**，绝不编一个默认预测。

    触发情形（每一种都对应一条已知的数据/口径陷阱）：
      - 当日无 K 线（停牌 / 采集缺口）→ 拿「最近一根」当今日 = 用昨天决定今天；
      - 历史不足 `WINDOW + 1` 根 → 估计量不可计算；
      - `sigma == 0`（价格恒定）→ 分布退化，`range_80` 宽度为 0 是个假区间。
    """


def canonical_json(obj) -> str:
    """规范化 JSON：键排序、紧凑分隔、不转义非 ASCII。

    「可复现」要求**逐字节一致**，所以序列化方式本身必须是契约的一部分 ——
    两处各写一遍 `json.dumps` 迟早会漂移。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def payload_hash(payload: Mapping) -> str:
    """载荷的 sha256（只覆盖 `CONTRACT_FIELDS`，见其 docstring）。"""
    contract = {k: payload[k] for k in CONTRACT_FIELDS}
    return hashlib.sha256(canonical_json(contract).encode("utf-8")).hexdigest()


def degenerate_strategy_mix(*, excluded: Mapping[str, str] | None = None,
                            benchmark_only: Sequence[str] = (),
                            weights: Mapping[str, float] | None = None,
                            note: str | None = None) -> dict:
    """构造 `strategy_mix`，**退化情形显式化**。

    总纲 §8.1 的形状是 `{"trend_ma": 0.4, ...}`，但那个平铺形状**表达不了
    「一个有效策略都没有」**。所以本实现包一层：权重仍逐字在 `weights` 里，
    另加 `degenerate` / `excluded` / `benchmark_only`，让读者一眼看到
    「这个预测背后有没有集成」。`schema_note` 记录这处偏离。
    """
    w = dict(weights or {})
    excl = dict(excluded or {})
    return {
        "weights": w,
        "degenerate": len(w) == 0,
        "note": note or (
            "无任何「未被否证」的方向性策略参与 —— 方向概率全部来自 " + MODEL_ID
            + " 统计模型" if not w else "按注册表权重集成"),
        "benchmark_only": sorted(benchmark_only),
        "excluded": excl,
        "schema_note": (
            "总纲 §8.1 的 {id: weight} 平铺形状无法表达退化态，故包一层；"
            "权重仍逐字在 weights 里"
        ),
    }


def compute_forecast(*, code: str, asof: str, bars: Sequence[Bar], target_date: str,
                     strategy_mix: Mapping) -> dict:
    """算出 `code` 在 `asof` 收盘后应给出的次日预测载荷。

    `bars` 必须是**复权**日 K 且**全部 `<= asof`**（调用方负责，见
    `service.load_pit_bars`）；本函数只做最后一道校验：最后一根必须正好是 `asof`。

    返回 §8.1 契约字段 + 一个 `evidence` 块（`evidence` **不**进 `payload_hash`：
    它是解释，不是预测本身；把它算进去会让「同一天同一模型」因为多打印一个
    中间量就变成另一个载荷）。
    """
    usable = [b for b in bars if b.date <= asof]
    if not usable or usable[-1].date != asof:
        raise DegenerateInput(
            f"{code} 在 {asof} 没有 K 线（最后一根是 "
            f"{usable[-1].date if usable else '无'}）—— 停牌或数据缺口，拒绝出预测"
        )
    need = max(WINDOW, LEVEL_WINDOW) + 1
    if len(usable) < need:
        raise DegenerateInput(
            f"{code} 在 {asof} 只有 {len(usable)} 根 K 线，需要 >= {need}"
            f"（WINDOW={WINDOW} / LEVEL_WINDOW={LEVEL_WINDOW}）"
        )

    closes = [b.close for b in usable[-(WINDOW + 1):]]
    rets = [math.log(cur / prev) for prev, cur in zip(closes, closes[1:])]
    mu = statistics.fmean(rets)
    sigma = statistics.stdev(rets)
    if not (sigma > 0.0):
        raise DegenerateInput(
            f"{code} 在 {asof} 的 {WINDOW} 日对数收益标准差为 {sigma} —— "
            "分布退化，给不出有意义的区间与概率（不许编默认值）"
        )

    close = usable[-1].close
    lo_b, hi_b = math.log(1 - FLAT_BAND), math.log(1 + FLAT_BAND)
    nd = statistics.NormalDist(mu, sigma)
    p_down = round(float(nd.cdf(lo_b)), 6)
    p_up = round(1.0 - float(nd.cdf(hi_b)), 6)
    p_flat = round(1.0 - p_up - p_down, 6)      # 减法 → 三者之和**恒等于** 1.0

    z80 = statistics.NormalDist().inv_cdf(0.50 + RANGE_COVERAGE / 2.0)
    range_80 = [round(close * math.exp(mu - z80 * sigma), 2),
                round(close * math.exp(mu + z80 * sigma), 2)]

    win = usable[-LEVEL_WINDOW:]
    support = round(min(b.low for b in win), 2)
    resistance = round(max(b.high for b in win), 2)
    # 日内延展用**实测**上/下影线均值（<= asof），不引入魔数
    ext_win = usable[-WINDOW:]
    up_ext = statistics.fmean((b.high - b.close) / b.close for b in ext_win)
    dn_ext = statistics.fmean((b.close - b.low) / b.close for b in ext_win)

    # 触及概率：把「触及」近似成「收盘价 × 实测日内延展」够到该价位。
    #
    # ⚠️ 这里的 `z` **已经是标准化值**（下面减了 mu 又除了 sigma），所以必须
    # 喂给**标准正态** `std`；喂给 `nd = Normal(mu, sigma)` 会被**二次标准化**
    # —— 尾部概率恒等于 0/1（P6 真实数据回放里 4 个价位全是 `p_touch: 0.0`，
    # 见 ERROR_DIARY 2026-09-15「已在算 z 了就别再喂带 mu/sigma 的分布」）。
    std = statistics.NormalDist()

    def p_touch_of(level: float, ext: float, *, above: bool) -> float:
        ref = close * (1 + ext) if above else close * (1 - ext)
        if ref <= 0:                                   # pragma: no cover - 防御
            return 0.0
        z = (math.log(level / ref) - mu) / sigma
        p = 1.0 - float(std.cdf(z)) if above else float(std.cdf(z))
        return round(min(1.0, max(0.0, p)), 6)

    key_levels = [
        {"price": resistance, "role": "resistance",
         "p_touch": p_touch_of(resistance, up_ext, above=True)},
        {"price": support, "role": "support",
         "p_touch": p_touch_of(support, dn_ext, above=False)},
    ]

    if p_flat >= p_up and p_flat >= p_down:
        action, size_pct = "wait", 0.0
    elif p_up > p_down:
        action, size_pct = "add", round(100.0 * (1.0 - p_down), 2)
    else:
        action, size_pct = "trim", round(100.0 * (1.0 - p_up), 2)

    return {
        "code": code,
        "asof_date": asof,
        "target_date": target_date,
        "direction": {"up": p_up, "flat": p_flat, "down": p_down},
        "range_80": range_80,
        "key_levels": key_levels,
        "action": action,
        "size_pct": size_pct,
        "invalidate_if": (
            f"收盘跌破 {support:.2f}（{LEVEL_WINDOW}日低点）"
            f"或收盘站上 {resistance:.2f}（{LEVEL_WINDOW}日高点）"
        ),
        "strategy_mix": dict(strategy_mix),
        "model_version": MODEL_VERSION,
        "evidence": {
            "model_id": MODEL_ID,
            "model_version": MODEL_VERSION,
            "model_spec": MODEL_SPEC,
            "inputs": {
                "close": close,
                "mu": mu,
                "sigma": sigma,
                "n_returns": len(rets),
                "drift_t": mu / (sigma / math.sqrt(len(rets))),
                "up_ext": up_ext,
                "dn_ext": dn_ext,
                "first_bar": usable[0].date,
                "last_bar": usable[-1].date,
                "n_bars_used": len(usable),
            },
            "features_used": [],
            "notes": {
                "mu": (
                    "漂移项 = 最近 WINDOW 日对数收益均值，**最弱的一项证据**："
                    "其 t 值见 drift_t，|t| 小的时候它基本是噪声"
                ),
                "range_80": (
                    "模型分位数（中心 80%），不是拟合出来的覆盖率 —— "
                    "§8.2 的「长期命中率应 ≈ 80%」对它才是真检验；"
                    "假设对数正态、独立同分布，忽略跳空/涨跌停/日内路径"
                ),
                "p_touch": (
                    "用实测上/下影线均值近似日内延展；忽略日内路径顺序与跳空"
                ),
                "size_pct": (
                    "模型隐含目标仓位（= 1 − 不利方向概率质量）；**不可直接执行** —— "
                    "不含成本、不含风险预算、不知道用户实际持仓"
                ),
                "features_used": (
                    "本模型不读 features_daily 任何一列：regime_label / main_net_5d / "
                    "pe_pct_3y 当前全为 NULL，NULL 不得当 0 或默认值"
                ),
            },
        },
    }
