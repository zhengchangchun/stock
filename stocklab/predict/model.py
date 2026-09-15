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
   `pe_pct_3y` 当前全为 NULL。本模块**不读 `features_daily`**；它只从 `bars` 与
   显式传入的**当日事实**（`index_dir`、P9-a 的 `PitFeatures`）取数，所以
   「NULL 被当成 0」在结构上不可能发生（不是靠自觉）—— `PitFeatures` 的字段是
   `None` 时本模块**抛 `DegenerateInput`** 而不是当 0 用。
3. **把 `size_pct` 说成可执行建议**。它没有成本、没有风险预算、不知道用户持仓，
   载荷里原样写明（`evidence.notes.size_pct`）。

## 参数不是旋钮

`WINDOW` / `LEVEL_WINDOW` 是**口径**（估计窗有多长），`FLAT_BAND` 由总纲 §8.2 规定。
本任务**不做任何参数搜索** —— 搜了就无法把结果归因到模型本身
（R7 反过拟合红线：一次实验只改一个变量）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from stocklab.data.models import Bar
from stocklab.errors import DegenerateInput
from stocklab.features.pit_regime import PitFeatures
from stocklab.predict.residual import ResidualDistribution
from stocklab.predict.version import MODEL_ID, MODEL_SPEC, MODEL_VERSION

# `DegenerateInput` 自 P9-a 起搬到了 `stocklab.errors`（`features.pit_regime` 也要用它
# 硬拒绝退化输入，异常留在本模块会形成 import 环）。上面那行是**原样再导出**，
# 既有的 `from stocklab.predict.model import DegenerateInput` 继续有效 ——
# 有测试钉住它是**同一个对象**（两个长得像的类会让 `except` 静默失效）。
# 刻意**不写 `__all__`**：本模块历史上没有它，加一个会悄悄改变 `import *` 的行为。

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


#: `mu_mode` 的合法取值。**这是唯一的变体旋钮**（P8 单变量实验用）。
#:
#: - `sample_mean`：基线 `pit-rw-v1.0.1` —— `mu = fmean(最近 WINDOW 日对数收益)`；
#: - `zero`：漂移项取 0（其余一律不变）；
#: - `index_sign`：`mu = |mu_sample| × s`，`s` 由**指数在 `asof` 当日**的方向给出
#:   （`index_dir` 作为**数据**传入，不是配置项 —— 见 `ForecastSpec` 的 docstring）。
MU_MODES: tuple[str, ...] = ("sample_mean", "zero", "index_sign")

#: `sigma_mode` 的合法取值。**P9-a 新增的第二条轴**：条件化波动率。
#:
#: 基线的 `sigma` 是最近 `WINDOW` 个对数收益的**无条件**样本标准差 —— 它把
#: 「当前处于高波动还是低波动状态」平均掉了。下面三个取值各自用**一种 PIT 信息**
#: 把 `sigma` 缩放成 `sigma_base × factor`，`factor = SIGMA_SCALE_MIN +
#: (SIGMA_SCALE_MAX − SIGMA_SCALE_MIN) × q`，`q ∈ [0,1]` 是该信息给出的分位：
#:
#: - `const`：基线 `pit-rw-v1.0.1` —— 不缩放（`q` 不参与，`features` 一行都不读）；
#: - `vol_z`：`q = Φ(量能 z)` —— 成交量相对自身近期均值的 z 分数（放量 ↔ 高波动）；
#: - `rv_pct`：`q =` 20 日已实现波动率在**自身**过去 250 期 RV 中的分位；
#: - `index_rv_pct`：同上，但序列换成 `sh000300` 的 PIT 指数 K 线（市场级状态）。
#:   刻意**只取波动率、不取方向** —— 指数方向那条路已由 `index-mom-dir` 在
#:   validate 段否证（`docs/experiments/2026-09-15-index-mom-dir.md`），不重复。
#:
#: 三者是**同一个字段的三个取值**：每个变体相对基线仍然**恰好改 1 个字段**，
#: `assert_single_variable()` 的机械校验没有任何松动。
SIGMA_MODES: tuple[str, ...] = ("const", "vol_z", "rv_pct", "index_rv_pct")

#: `sigma` 缩放因子的上下界。**先验固定，不是搜出来的**：`P9-a` 不做任何参数搜索，
#: 改这两个数就是第二个变量。±50% 是「同一个 `sigma` 估计量在不同波动状态下
#: 合理的不确定带宽」，不是拟合值。
SIGMA_SCALE_MIN = 0.5
SIGMA_SCALE_MAX = 1.5

#: `dist_mode` 的合法取值。**P10-a 新增的第三条轴**：预测分布的**形状来源**。
#:
#: 基线把次日收盘收益假设成 `Normal(mu, sigma)`，`range_80` 与三分类概率全是解析分位。
#: 这个**形状假设**从未被检验过：
#:
#: - `gaussian`：基线 `pit-rw-v1.0.1` —— 解析分位，`residuals` 一行都不读；
#: - `resid_emp`：形状取**训练窗内**标准化残差 `(r_t − mu_hat_t)/sigma_hat_t` 的
#:   经验分布（`predict.residual.ResidualDistribution`，**只在 train 段拟合**）。
#:   `direction` 的 CDF 与 `range_80` 的分位由**同一份**残差池给出 —— 它们是
#:   同一个分布的两个泛函，不能各换一半（那不是一个分布，而且主指标会恒等于基线）。
#:
#: 与 `sigma_mode` 的**缩放**轴刻意区分：那条轴已被三个变体一致否证
#: （`docs/experiments/2026-09-15-sigma-*.md`），本条轴不动
#: `SIGMA_SCALE_MIN/MAX`、不换 `WINDOW`、`sigma` 仍是 60 日无条件样本标准差，
#: 改的只是「形状从哪里来」。
#:
#: **不在本条轴定义域内**：`p_touch`（日内路径量的近似）保持解析式，逐行不动。
DIST_MODES: tuple[str, ...] = ("gaussian", "resid_emp")

#: 基线口径的 spec。**`compute_forecast(spec=None)` 与 `spec=BASELINE_SPEC` 必须逐字节等价**，
#: 这条由 `tests/test_predict_model.py` 钉住。
BASELINE_SPEC: "ForecastSpec"


@dataclass(frozen=True)
class ForecastSpec:
    """一次预测的**显式**输入配置（P8 单变量实验的载体）。

    ## 为什么要有它，而不是给 `compute_forecast` 加几个布尔开关

    实验流水线的整套纪律建立在「**一次只改一个变量**」上。若变体靠模块级开关
    （环境变量 / 全局配置）注入，那么「这次跑的是哪个口径」就变成**隐式**的了：
    忘了复位就静默串味，而且 `payload_hash` 里看不出来。把口径做成一个 frozen
    dataclass、**按调用显式传入**，才有两件结构性的保证：

    1. `assert_single_variable` 能**数出**它相对基线改了几个字段（必须恰好 1 个）；
    2. 同一个进程里可以同时算基线与变体，互不污染。

    ## 为什么「指数方向」不是本类的字段

    `index_dir` 是**当日的事实**（`sh000300` 在 `asof` 的涨跌方向），不是「模型配置」。
    把它塞进 spec 会让「改了一个变量」的计数变成 2（`mu_mode` 与 `index_dir`），
    「单变量」这条纪律就没法机械校验了。所以它作为**数据**参数传进
    `compute_forecast`，与 `bars` 同级。

    ## 字段只有 `mu_mode` / `sigma_mode` / `dist_mode` 三个

    标签带（`FLAT_BAND`）、窗口（`WINDOW` / `LEVEL_WINDOW`）、关键位口径**不在本类里**，
    因此「用某个变体把标签带改成 ±1%」在结构上不可能发生 ——
    口径冻结靠的是「没有那个旋钮」，不是靠自觉（见 `docs/plans/2026-09-15-p8-实验流水线.md` §1.5）。

    `sigma_mode` 是 P9-a 新增的**第二条轴**（条件化波动率，取值见 `SIGMA_MODES`）；
    `dist_mode` 是 P10-a 新增的**第三条轴**（预测分布的形状来源，取值见 `DIST_MODES`）。
    两者都是**按调用显式传入**的 frozen 字段：没有环境变量、没有模块级开关，
    「这次跑的是哪个口径」永远能从参数读出来。三个字段的默认值就是基线口径，
    所以 `ForecastSpec()` 与 `spec=None` 仍然逐字节等价。

    ## 为什么 `dist_mode` 不是两个字段（`range_mode` + 概率口径）

    `range_80` 与 `direction` 是**同一个预测分布**的分位数与 CDF。拆成两个字段会允许
    「经验分位 + 高斯 CDF」这种组合 —— 它不是一个分布，两个输出互相矛盾；
    而且只换 `range_80` 会让 Brier 恒等于基线（Δ≡0），实验在结构上不可能赢。
    所以形状**只能整体换**，一个字段一条轴。
    """

    mu_mode: str = "sample_mean"
    sigma_mode: str = "const"
    dist_mode: str = "gaussian"

    def __post_init__(self) -> None:
        if self.mu_mode not in MU_MODES:
            raise ValueError(
                f"未知 mu_mode={self.mu_mode!r}；合法取值 {MU_MODES}。"
                "拒绝静默回退到基线口径 —— 那会让一次实验跑出「看起来是变体、其实是基线」的数字"
            )
        if self.sigma_mode not in SIGMA_MODES:
            raise ValueError(
                f"未知 sigma_mode={self.sigma_mode!r}；合法取值 {SIGMA_MODES}。"
                "拒绝静默回退到基线口径（同上）"
            )
        if self.dist_mode not in DIST_MODES:
            raise ValueError(
                f"未知 dist_mode={self.dist_mode!r}；合法取值 {DIST_MODES}。"
                "拒绝静默回退到基线口径（同上）"
            )

    @property
    def is_baseline(self) -> bool:
        """**所有**字段都在基线上 —— 不是「`mu_mode` 是基线值」。

        写成 `not self.changed_fields()` 而不是 `self.mu_mode == "sample_mean"`：
        后者在新增第二条轴后会漏判（一个只改了 `sigma_mode` 的 spec 会被当成基线，
        于是 `compute_forecast` 走基线证据分支、`evidence` 里少打印中间量）。
        """
        return not self.changed_fields()

    def changed_fields(self) -> tuple[str, ...]:
        """相对基线**被改掉的字段名**（供单变量校验）。"""
        return tuple(f.name for f in dataclasses.fields(self)
                     if getattr(self, f.name) != getattr(BASELINE_SPEC, f.name))


BASELINE_SPEC = ForecastSpec()




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
                     strategy_mix: Mapping, spec: ForecastSpec | None = None,
                     index_dir: int | None = None,
                     features: PitFeatures | None = None,
                     residuals: ResidualDistribution | None = None) -> dict:
    """算出 `code` 在 `asof` 收盘后应给出的次日预测载荷。

    `bars` 必须是**复权**日 K 且**全部 `<= asof`**（调用方负责，见
    `service.load_pit_bars`）；本函数只做最后一道校验：最后一根必须正好是 `asof`。

    `spec` 为 `None`（默认）时走**基线口径 `pit-rw-v1.0.1`**，代码路径与 P6 逐字节一致。
    传入非基线 spec 即得到一个**单变量变体**（P8 实验用）。

    `index_dir` 只在 `spec.mu_mode == "index_sign"` 时被读取，是**数据**不是配置：
    `sh000300` 在 `asof` 当日的方向（`+1` / `-1` / `0`）。**缺失即拒绝** ——
    不允许静默当成 0（那是把「不知道」写成「没有」，ERROR_DIARY 2026-09-14）。

    `features`（P9-a）同理：只有 `spec.sigma_mode != "const"` 时被读取，是**数据**，
    承载量能 z / 自身 RV 分位 / 指数 RV 分位三个 PIT 量（见 `features.pit_regime`）。
    需要哪个由 `PitFeatures.required_for(sigma_mode)` 决定；**需要的那个是 `None`
    即拒绝**（`DegenerateInput`）—— 不许静默回退成基线 `sigma`，
    那会让变体报告里混进基线口径的行而没人知道。

    `residuals`（P10-a）同理：只有 `spec.dist_mode != "gaussian"` 时被读取，
    是**数据**（形状的来源），由 `experiments.residuals` 在 **train 段** 拟合。
    `spec.dist_mode != "gaussian"` 而 `residuals is None` → `DegenerateInput`，
    **不许**静默回退成高斯分位。反过来，口径是 `gaussian` 时**即使传进来也不读** ——
    「读什么」只由 spec 决定，不由调用方传了什么决定。

    返回 §8.1 契约字段 + 一个 `evidence` 块（`evidence` **不**进 `payload_hash`：
    它是解释，不是预测本身；把它算进去会让「同一天同一模型」因为多打印一个
    中间量就变成另一个载荷）。
    """
    spec = spec or BASELINE_SPEC
    mode = spec.mu_mode
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
    mu_sample = statistics.fmean(rets)
    # ---- 漂移项：`mu_mode` 是唯一的变体旋钮（默认路径下 `mu = mu_sample`，逐字节不变）----
    if mode == "sample_mean":
        mu = mu_sample
    elif mode == "zero":
        mu = 0.0
    elif mode == "index_sign":
        if index_dir is None:
            raise DegenerateInput(
                f"{code} 在 {asof} 缺 `sh000300` 的当日方向（index_dir=None）—— "
                "`index_sign` 变体**拒绝静默退化成 mu=0 或基线**：那是把「不知道」写成「没有」，"
                "会让报告里的样本量悄悄变少而没人知道"
            )
        if index_dir not in (-1, 0, 1):
            raise DegenerateInput(
                f"index_dir={index_dir!r} 非法（只接受 -1 / 0 / +1）—— 拒绝猜"
            )
        # 只换**方向的来源**（指数前一日方向），漂移的**幅度**仍是基线那个 |mu_sample|。
        # 直接指数收益当漂移会连尺度一起改掉，那就不是一个变量了（见 P8 计划 §1.3）。
        mu = abs(mu_sample) * index_dir
    else:                                        # pragma: no cover - 构造期已挡
        raise DegenerateInput(f"未知 mu_mode={mode!r}")
    sigma_base = statistics.stdev(rets)
    if not (sigma_base > 0.0):
        raise DegenerateInput(
            f"{code} 在 {asof} 的 {WINDOW} 日对数收益标准差为 {sigma_base} —— "
            "分布退化，给不出有意义的区间与概率（不许编默认值）"
        )
    # ---- 波动率：`sigma_mode` 是第二条轴（默认路径下 `sigma = sigma_base`，逐字节不变）----
    sigma_quantile: float | None = None
    sigma_scale = 1.0
    if spec.sigma_mode != "const":
        if features is None:
            raise DegenerateInput(
                f"{code} 在 {asof} 的 sigma_mode={spec.sigma_mode!r} 需要 PIT 特征，"
                "但 features=None —— 拒绝静默回退到基线 sigma"
            )
        sigma_quantile = features.quantile_for(spec.sigma_mode)
        if sigma_quantile is None:
            missing = sorted(PitFeatures.required_for(spec.sigma_mode))
            raise DegenerateInput(
                f"{code} 在 {asof} 的 {missing}（sigma_mode={spec.sigma_mode!r} 所需）"
                "算不出来 —— 历史不足或数据缺口。**拒绝静默当成 0/1 或回退到基线 sigma**："
                "那会让这一行看起来是变体口径，实际是另一个东西"
            )
        if not (0.0 <= sigma_quantile <= 1.0):
            raise DegenerateInput(
                f"{code} 在 {asof} 的 sigma 分位={sigma_quantile!r} 不在 [0,1] —— "
                "特征层坏了，拒绝猜"
            )
        sigma_scale = SIGMA_SCALE_MIN + (SIGMA_SCALE_MAX - SIGMA_SCALE_MIN) * sigma_quantile
    sigma = sigma_base * sigma_scale

    close = usable[-1].close
    lo_b, hi_b = math.log(1 - FLAT_BAND), math.log(1 + FLAT_BAND)
    # ---- 形状来源：`dist_mode` 是第三条轴（默认路径下逐字节不变）----
    # `range_80` 与三分类概率是**同一个分布**的分位与 CDF，必须同源：
    # 拆开换一半会得到两个互相矛盾的输出（见 `ForecastSpec` 的 docstring）。
    if spec.dist_mode == "gaussian":
        nd = statistics.NormalDist(mu, sigma)
        p_down = round(float(nd.cdf(lo_b)), 6)
        p_up = round(1.0 - float(nd.cdf(hi_b)), 6)
        z80 = statistics.NormalDist().inv_cdf(0.50 + RANGE_COVERAGE / 2.0)
        lo_shape, hi_shape = -z80, z80
    elif spec.dist_mode == "resid_emp":
        if residuals is None:
            raise DegenerateInput(
                f"{code} 在 {asof} 的 dist_mode='resid_emp' 需要训练窗残差分布，"
                "但 residuals=None —— **拒绝静默回退到高斯分位**："
                "那会让这一行看起来是变体口径，实际是基线"
            )
        r_lo = (lo_b - mu) / sigma
        r_hi = (hi_b - mu) / sigma
        p_down = round(residuals.cdf(r_lo), 6)
        p_up = round(1.0 - residuals.cdf(r_hi), 6)
        tail = (1.0 - RANGE_COVERAGE) / 2.0
        # 非对称：左右分位各自取值 —— 这正是「形状」要与高斯比的东西
        lo_shape, hi_shape = residuals.quantile(tail), residuals.quantile(1.0 - tail)
    else:                                        # pragma: no cover - 构造期已挡
        raise DegenerateInput(f"未知 dist_mode={spec.dist_mode!r}")
    p_flat = round(1.0 - p_up - p_down, 6)      # 减法 → 三者之和**恒等于** 1.0

    range_80 = [round(close * math.exp(mu + sigma * lo_shape), 2),
                round(close * math.exp(mu + sigma * hi_shape), 2)]

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

    evidence_inputs = {
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
    }
    if not spec.is_baseline:
        # **只在变体口径下**追加这些键：基线的 evidence 必须逐字节不变
        # （`reports/2026-09-15-predict-2026-09-14.json` 的 sha256 是回归红线）。
        # 注意 `is_baseline` 是「**所有**字段都在基线上」—— 所以新增 `sigma_mode`
        # 之后，一个只改 sigma 的 spec 也会走到这里（旧写法 `mode != "sample_mean"`
        # 会漏判它，于是证据块里查不到变体改了什么）。
        evidence_inputs["mu_sample"] = mu_sample
        evidence_inputs["mu_mode"] = mode
        evidence_inputs["index_dir"] = index_dir
    if spec.sigma_mode != "const":
        # 只改 sigma 的那三个变体在这里留痕；`mu_mode` 那两条变体的 evidence
        # 因此**逐字节不变**（既有报告 sha256 仍是红线）。
        evidence_inputs["sigma_base"] = sigma_base
        evidence_inputs["sigma_mode"] = spec.sigma_mode
        evidence_inputs["sigma_quantile"] = sigma_quantile
        evidence_inputs["sigma_scale"] = sigma_scale
    if spec.dist_mode != "gaussian":
        # 只改形状的那条变体在这里留痕（基线走不到这一支，sha256 红线不受影响）。
        # 残差池的**溯源**必须落进证据块：审计者要能回答「这个形状是拿多少样本、
        # 哪一段、哪几个标的拟合的」，并且能拿这些分位自己复算每个数字。
        evidence_inputs["dist_mode"] = spec.dist_mode
        evidence_inputs["residuals"] = residuals.as_evidence()

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
            "inputs": evidence_inputs,
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
