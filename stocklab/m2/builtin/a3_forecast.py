"""`m2_a3`（AI 持仓收益预测）与 `m2_b1`（人工持仓收益预测）**共用的源文本**。

## 一份实现、两份源文本（D-30 的可比性）

A3 与 B1 必须**同口径**才可比：同一条通路 A 的模拟持仓、一条通路 B 的人工持仓，
两份预测放进同一张表（`m2_forecasts`）与同一个校验口径里。所以逻辑只写一遍
（`_BODY`），两份源文本由 `source_for()` 渲染，**只差版本串**；「逻辑部分逐字
相同」由 `tests/test_m2_builtin.py::test_t6_*` 归一后比对钉住（并带反向自检，
防止归一函数宽到什么都拦不住）。

`b1_forecast.py` 从本模块取 `source_for` —— 漂移只可能发生在一处。

## 口径（沿用模块1，`predict/model.py`）

| 量 | 怎么算 |
|---|---|
| `mu` / `sigma` | 估计窗内收盘价对数收益的**均值与样本标准差**（`n-1`） |
| `range_80` | `close·exp(mu ± Z80·sigma)`，`Z80 = 1.2816`（覆盖 80%） |
| `direction` | **同一个** `Normal(mu, sigma)` 的 CDF：`p_down=Φ(ln 0.995)`、`p_up=1−Φ(ln 1.005)`、`p_flat=Φ(ln 1.005)−Φ(ln 0.995)` |
| `invalidate_if` | 一句可读的失效条件（跌破 80% 区间下沿） |

`p_flat` 的关键字是**用减法收尾**（不做第二次分布计算、更不归一化）：契约对
「三概率之和」的判据是 ±1e-6，且**拒绝归一化**。但减的**两个数**必须挑对 ——
`p_flat = Φ(hi_b) − Φ(lo_b)` 与 `1 − p_up − p_down` 代数上完全等价，差别只在浮点：
后者的 `1 − (1−ε)` 在带子远离均值时会舍成 `−6.995e-27`（契约拒绝任何 `<0`
的概率，实测见 `test_t6_probabilities_stay_in_range_across_a_mu_sigma_grid` 与
P57 任务书 §实施记录），前者是两个 CDF 值相减、恒 ≥ 0 且三者之和恒为 1.0。
**这一处偏离了任务书 §1.5 的字面写法**（「必须用 `1 − p_up − p_down` 收尾」），
理由与实测证据写在上面的注释与实施记录里 —— 不是换个口径，是同一个量的等价算法。

## 估计窗：20 个交易日的**收盘价**（19 个对数收益）

任务书 §1.5 的原话是「取最近 20 个交易日的收盘价对数收益」＋「样本不足 20 根 ⇒
给 None」。两处必须用同一个单位，否则 20 根 K 线的 ctx 会落进「够/不够」之间的洞：
若按「20 个收益」算就需要 21 根收盘价，而守卫写的是 20 根 ⇒ 20 根时无输出但也不报
`na_reasons`。所以取**最近 20 根收盘价**（19 个收益），守卫 `MIN_BARS = 20` 与它同单位。
（模块1 的 `WINDOW = 60` 同理：`usable[-(WINDOW+1):]` 是 61 根收盘价 / 60 个收益。）

## 沙盒里没有 `math`

`plugin/runtime.py` 的受限命名空间只给 22 个内建（没有 `math`、没有 `statistics`），
所以脚本自带三个纯算术替代：`_exp`（`E ** x`）、`_ln`（atanh 级数，先缩放到
`[0.5, 2)` 再乘回 `k·ln2`）、`_phi`（标准正态 CDF，Abramowitz & Stegun 7.1.26，
绝对误差 ≤ 1.5e-7）。三者都只依赖 IEEE-754 双精度，与调用次数无关 —— 确定性
由 T3 钉住。
"""

from __future__ import annotations

#: 载荷形状版本。A3/B1 **同版本**：形状与语义逐字相同才有可比性。
SCHEMA_VERSION: str = "1.0.0"

#: 逻辑体（共享）。`__SCHEMA_VERSION__` 是唯一的版本串占位符 ——
#: 渲染后两份源文本除它以外逐字相同。
_BODY: str = '''# m2_a3 / m2_b1 —— 持仓收益预测（A3：AI 模拟持仓；B1：人工镜像持仓）
#
# 纯函数：只读 ctx。被预测的标的是 ctx["focus"]["code"]，它的明细在
# ctx["holdings"] 里（返回里**没有** code 字段 —— 形状是「一条预测」，不是「一份表」）。
#
# 沙盒没有 math / statistics，所以 exp 用幂运算、ln 用 atanh 级数、
# Φ 用 A&S 7.1.26 有理逼近（|误差| <= 1.5e-7）。全部只依赖双精度浮点。
MIN_BARS = 20          # 估计窗：最近 20 个交易日的收盘价（不足 ⇒ None + 原因）
FLAT_BAND = 0.005      # |对数收益| <= 0.5% 记 flat（模块1 总纲 §8.2 的口径）
Z80 = 1.2816           # 80% 覆盖的正态分位（模块1 RANGE_COVERAGE=0.80 同款）
E = 2.718281828459045
LN2 = 0.6931471805599453


def _ln(x):
    # ln(x) = 2*atanh((x-1)/(x+1))，参数先缩放到 [0.5, 2) 再加回 k*ln2。
    k = 0
    while x >= 2.0:
        x = x / 2.0
        k = k + 1
    while x < 0.5:
        x = x * 2.0
        k = k - 1
    z = (x - 1.0) / (x + 1.0)
    z2 = z * z
    total = 0.0
    term = z
    n = 1
    while n <= 25:
        total = total + term / n
        term = term * z2
        n = n + 2
    return 2.0 * total + k * LN2


def _exp(x):
    return E ** x


def _phi(z):
    # 标准正态 CDF（A&S 7.1.26）。z<0 走 P(Z<=z)，z>=0 走 1-P(Z<=-z)。
    t = 1.0 / (1.0 + 0.2316419 * abs(z))
    d = 0.3989422804014327 * _exp(-0.5 * z * z)
    p = d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
        + t * (-1.821255978 + t * 1.330274429))))
    if z > 0.0:
        return 1.0 - p
    return p


def _na(reason):
    # 「不知道」的落点：三个数值字段**一起**给 None 并逐条说明为什么。
    # 契约拒绝「半真半假」（有 None 却没 na_reasons，或有 na_reasons 却给了数）。
    return {"range_80": None, "direction": None, "invalidate_if": None,
            "na_reasons": [reason], "schema_version": __SCHEMA_VERSION__}


def run(ctx):
    focus = ctx.get("focus") or {}
    code = focus.get("code")
    if code is None:
        return _na("ctx['focus'] 没给被预测的标的（focus=None 或缺 code）——"
                   "预测载荷里没有 code 字段，缺了它就无法确定在预测谁")
    closes = []
    for h in ctx["holdings"]:
        if str(h["code"]) == str(code):
            for b in (h.get("bars") or []):
                if b.get("close") is not None:
                    closes.append(float(b["close"]))
            break
    if len(closes) < MIN_BARS:
        return _na("%s 在 %s 只有 %d 根可用收盘价（< %d）—— 分位区间与方向都"
                   "算不出来（「不知道」不给 0.0 冒充最差）"
                   % (code, ctx.get("asof"), len(closes), MIN_BARS))
    window = closes[-MIN_BARS:]
    rets = []
    for i in range(1, len(window)):
        rets.append(_ln(window[i] / window[i - 1]))
    n = len(rets)
    mu = sum(rets) / n
    var = 0.0
    for r in rets:
        var = var + (r - mu) * (r - mu)
    sigma = (var / (n - 1)) ** 0.5
    if sigma <= 0.0:
        # 与模块1 同一条口径：分布退化就明说（predict/model.py 对 stdev==0
        # 直接拒绝，而不是给一个宽度为 0 的区间）。
        return _na("%s 在 %s 的 %d 日对数收益样本标准差为 0 —— 分布退化，"
                   "给不出有意义的区间与概率（不许编默认值）"
                   % (code, ctx.get("asof"), MIN_BARS))
    close = window[-1]
    lo = round(close * _exp(mu - Z80 * sigma), 2)
    hi = round(close * _exp(mu + Z80 * sigma), 2)
    lo_b = _ln(1.0 - FLAT_BAND)
    hi_b = _ln(1.0 + FLAT_BAND)
    c_lo = _phi((lo_b - mu) / sigma)
    c_hi = _phi((hi_b - mu) / sigma)
    p_down = c_lo
    p_up = 1.0 - c_hi
    # p_flat 就是 `1 − p_up − p_down`（因为 p_up=1−c_hi、p_down=c_lo），
    # 但**按 CDF 差值算**：两个「1 附近的数」相减在带子远离均值时会因 1−ε 的
    # 舍入给出负数（实测 mu=0.05/sigma=0.005 时 flat=-6.995e-27，契约拒绝任何
    # <0 的概率）；两个 CDF 值相减恒 ≥ 0（c_hi >= c_lo），且三者之和仍恒为 1.0。
    p_flat = c_hi - c_lo
    return {"range_80": [lo, hi],
            "direction": {"up": p_up, "flat": p_flat, "down": p_down},
            "invalidate_if": "目标日收盘价 < %s（80%% 区间下沿）" % lo,
            "na_reasons": [], "schema_version": __SCHEMA_VERSION__}
'''


def source_for(schema_version: str) -> str:
    """渲染源文本。`schema_version` 是 A3/B1 之间**唯一**被允许的差异。"""
    return _BODY.replace("__SCHEMA_VERSION__", repr(str(schema_version)))


#: `m2_a3` 的初始版本源文本。
SOURCE: str = source_for(SCHEMA_VERSION)
