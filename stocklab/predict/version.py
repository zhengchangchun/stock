"""预测器的**版本常量与策略黑白名单** —— 全项目只此一处（Task 29，P6）。

为什么单独一个模块：`model_version` 是载荷的一部分，也是 `predictions` 唯一键的
一列。它一旦散落在多处，就会出现「报告里写 v1、库里存 v2、落库的是 v3」这种
谁也说不清的局面，而 `docs/experiments/` 的实验台账正是按 `model_version` 归因的。
"""

from __future__ import annotations

#: 载荷与 `predictions.model_version` 的唯一真源。**改这里 = 换模型**，
#: 且必须同时新建一条实验台账（旧记录不可改）。
#:
#: ## 变更史（改了公式就必须在这里加一行，否则「可追溯」是空话）
#:
#: - `pit-rw-v1.0.0`：首个版本。
#: - `pit-rw-v1.0.1`：修 `p_touch` 的**二次标准化**。`z` 已标准化却喂给了
#:   `NormalDist(mu, sigma)`, 尾部概率恒饱和 0/1 —— 真实数据回放时 4 个价位
#:   `p_touch` 全是 `0.0`（阻力位离现价仅 ~1.7%，本该 ≈0.30）。
#:   见 `docs/errors/ERROR_DIARY.md` 2026-09-15。**载荷因此改变，故必须升版本**：
#:   v1.0.0 那两行预测原样留在库里，不被覆盖、不被重算 —— 这正是 append-only
#:   与 `model_version` 唯一键存在的意义。
#: - `pit-rw-v1.0.2`：**公式一字未改**，改的是**输入口径**。此前库里
#:   `corp_actions`/`adj_factors` 各 0 行 ⇒ 因子恒 1 ⇒ `adjust.load_bars_adjusted()`
#:   把「前复权价」返回成**不复权价逐位相同**的序列（静默失真：不报错，报告里
#:   仍写 `adjusted_prices: true`）。复权链补上（411→**417 事件 / 80461 因子行**）后，
#:   同一个 `asof` 算出的载荷必然不同，而 `(code, asof_date, model_version)` 唯一键
#:   与 append-only 触发器**拒绝改写**旧行（错误文案就是「要出新预测请升 model_version」）。
#:   故按项目纪律升版本，而**不是**删行重写：`v1.0.1` = 「复权链为空的时期」的
#:   带版本历史，报告按 `model_version` 分组天然不混算（`verify.report`）。
#:   决策与证据见 `docs/plans/2026-09-21-红线库指纹.md` §7.2/§7.3
#:   （用户 2026-09-21 拍板「X」）；落地记录见
#:   `docs/plans/2026-09-21-MODEL_VERSION-v1.0.2-复权口径升版.md`。
MODEL_VERSION = "pit-rw-v1.0.2"

#: 模型短名（报告/证据块用）。
MODEL_ID = "pit_rw_v1"

#: 模型的公式摘要。`docs/plans/2026-09-15-p6-预测器.md` §3 是它的长版本；
#: 两边不一致 = bug。放在常量里是为了让「这些数字是哪个公式算的」可被程序读出。
MODEL_SPEC = {
    "returns": "r_i = ln(C_i / C_{i-1})，取 <= asof 的最近 WINDOW 个交易日",
    "distribution": "X ~ Normal(mean(r), stdev(r, ddof=1))，X 为次日对数收益",
    "flat_band": "ln(1 ± 0.005)（总纲 §8.2 的三分类阈值 ±0.5%）",
    "range_80": "C * exp(mu ± z80*sigma)，z80 = Phi^-1(0.90)（中心 80% 分位区间）",
    "key_levels": "20 日低点/高点；p_touch 用实测上/下影线均值做日内延展近似",
    "action_size": "size_pct = 100 * (1 - 不利方向的概率质量)，**不含成本/风险预算**",
    "features_used": "无 —— 只用复权 K 线的 OHLC，不读 features_daily 任何一列",
}

#: 已被**样本外**绩效否证的策略：不允许作为方向证据参与预测。
#: 引用是刻意的 —— 「为什么排除它」必须能一路查到实验台账那一行。
FALSIFIED_STRATEGIES: dict[str, str] = {
    "trend_ma": (
        "docs/experiments/2026-09-15-trend_ma-default-walkforward.md："
        "样本外（2751 交易日）扣成本 510.79% vs buy_and_hold 881.75% → 被否证"
    ),
}

#: 只作对照、**不提供次日方向信息**的策略。给它们权重就是「假装有多策略集成」。
BENCHMARK_ONLY_STRATEGIES: tuple[str, ...] = ("buy_and_hold",)

#: **允许作为方向证据参与预测**的策略白名单。当前为**空**。
#:
#: 为什么要有这份名单，而不是「注册了就算数」：`strategy_registry` 证明的是
#: 「这个策略能被评估」，**不是**「它有 edge」。若把「已注册」当成「可参与集成」，
#: 任何新写的策略（甚至别的测试往全局注册表里塞的探针策略）都会**自动**拿到权重，
#: 于是一份「多策略集成」会在没有任何样本外证据的情况下凭空出现 ——
#: 这正是本任务要防的「假装有集成」。
#:
#: 加入条件（缺一不可）：walk-forward 样本外达标 + 已通过 `scripts/verify.sh`
#: + 在 `docs/experiments/` 留下正向实验记录。一次只能加一个，理由写进实验台账。
#: 与之对称的是 `FALSIFIED_STRATEGIES`：**证明无效就立刻移出**。
ACTIVE_STRATEGIES: tuple[str, ...] = ()
