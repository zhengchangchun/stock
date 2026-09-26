"""沙盒回放引擎（设计文档 `2026-09-20-沙盒真回放-design.md`）。

## 观测单元是**调仓周期**，不是交易日

一个 20 日调仓周期里，20 个日度收益是**同一个持仓**产生的、高度自相关。
把它们当独立样本做 bootstrap 会算出**过窄的置信区间 → 假的 WIN**。所以
每个周期只产生**一个**观测。

代价（设计文档 §2 D1）：门槛从「120 交易日」变成「120 调仓周期」，
严格得多 —— 长期池（60 日）在可预见的历史长度内**永远出不了结论**。
这是算术，不降门槛迁就。

## 下游规则写死

等权、持到下一个调仓日、空池持现金（但计清仓成本）、扣成本、涨跌停
不可成交。**规则不随插桩版本变化** —— 否则「改下游」会成为另一条提分
路径，归因失效。

### 成本口径（ADR-017 D-13 / D-14）

- **按每只名义仓位计**（`POSITION_NOTIONAL`），**不按 1 手**。用 1 手接
  固定 5 元最低佣金，会把成本除成大费率（低价股放大 19 倍）——
  见 `stocklab/config/replay.py` 的注释。
- **费用用 `CostModel.fees()`（参考价）**，不用 `total()`（滑点价）：
  滑点由下面单独计，避免同一笔滑点被算两遍。
- **滑点如实计入**（铁律 4）：每**成交一条腿**收 `slippage_bps × (1/n_hold)`。
  持仓不动不成交、也就不产生滑点；被涨跌停挡住的腿**没成交**，同样不收。

## 本模块在 candidate/ 下，不在 plugin/ 下

回放要调打分内核；`plugin/` 不得 import `candidate/`。所以回放住在这里，
由 `plugin/sandbox.py` 通过**注入**调用（见本模块 `period_returns`）。
"""

from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

from stocklab.backtest.portfolio import BoardUnknown, LIMIT_BY_BOARD, LIMIT_TOLERANCE
from stocklab.config.costs import CostModel
from stocklab.config.replay import POSITION_NOTIONAL, REBALANCE_DAYS
from stocklab.data import adjust
from stocklab.data.models import Bar

#: 空池时的处置：持现金。**不是**「跳过该周期」—— 卖出上一期持仓是要付
#: 成本的，跳过会把那笔成本抹掉。
EMPTY_POOL_IS_CASH: bool = True

#: 收益侧的价格口径（D1）。`raw` = 现状（直读 `bars_daily.close`，全表 `adj_mode='none'`
#: ⇒ **未复权价**）；`adj` = **PIT 复权价**。只作用于周期毛收益的 `p0/p1`（D2）：
#: 成本侧成交价、`_qty_for` 整手、涨跌停判定一律保持未复权（与 P77 / ADR-029 同构）。
PRICE_MODES: tuple[str, ...] = ("raw", "adj")

#: 默认口径 = **现状**。不传 `price_mode` 的调用点（含 `plugin/sandbox.py` 经注入
#: 调用的 `benchmark_excess`）走 `raw`，逐位不变（D1 / T3 用例①）。
DEFAULT_PRICE_MODE = "raw"

#: 「**这个标的的复权价算不出来**」这一类失败（数据侧不可用 ⇒ 回退未复权 + 计数）。
#: 与 `candidate/run.py` / `research/signal.py` 的 `_ADJ_UNAVAILABLE` **同一集合**（三类）。
#: 刻意不是 `AdjustError` 全体：裸 `AdjustError`（`k > 1` = 负现金解析 bug、
#: `pre_close <= 0` = 数据坏）是**我们这边**的缺陷，必须炸出来
#: （ERROR_DIARY #81「数据脏与代码坏要分档」）。D3：不抛异常、也不静默。
_ADJ_UNAVAILABLE = (adjust.EtfChainUnsupported, adjust.MissingFactor,
                    adjust.StaleFactorTable)

#: 涨跌停判定时的比较容差（同 `backtest.portfolio.LIMIT_TOLERANCE`）。
#: `portfolio.py` 使用 1e-6 处理浮点表示误差（如 1.1*10 = 10.999…）。
#: 本模块使用同一常量，语义完全一致；若两处将来发散，需要统一评审后再改。
_LIMIT_SLACK = LIMIT_TOLERANCE


def rebalance_dates(trading_days: list[str], *, period: int, start: str,
                    end: str) -> list[str]:
    """窗口内每 `period` 个交易日一个调仓日。

    **末尾不足一个周期直接丢弃** —— 不截断成短周期，否则周期长度不一致、
    收益不可比。
    """
    days = [d for d in trading_days if start <= d <= end]
    if not days:
        return []
    out = [days[i] for i in range(0, len(days), period)]
    return [d for d in out if d <= end]


def _instrument(conn: sqlite3.Connection, code: str) -> tuple[str, str]:
    """`(board, asset_type)` —— 一次查询取齐两项口径，避免逐项各查一次。"""
    row = conn.execute("SELECT board, type FROM instruments WHERE code = ?",
                       (code,)).fetchone()
    if row is None:
        raise BoardUnknown(f"{code} 不在 instruments 表里，无法判定板别")
    return str(row["board"]), str(row["type"])


def _board_of(conn: sqlite3.Connection, code: str) -> str:
    return _instrument(conn, code)[0]


def _costs_for(conn: sqlite3.Connection, code: str,
               costs: CostModel | None) -> CostModel:
    """该标的的 `CostModel`。

    调用方**显式**传了 `costs`（含「压零成本」的测试）→ 一律用它，不做口径分支。
    未传（`None`）→ 按 `instruments.type` **逐标的**取口径（ADR-008 / 设计稿 §Q4）。

    为什么必须逐标的：原来无条件用单一 stock 口径，理由是「ETF 只 4/21 且不进
    短期动量池」；横截面实验把持有集合换成「池内全部合格标的」后，ETF 可能真的
    进池，该理由不再成立，多扣的印花税/过户费就不再是「Δ 两端抵消」的中性项。
    `CostModel` 对未知口径抛 `ValueError`（`config/costs.py:69-74`）—— 保留，
    不降级成「默认股票成本」：口径错的方向是让回测好看，必须响。
    """
    if costs is not None:
        return costs
    return CostModel(asset_class=_instrument(conn, code)[1])


def _limit_hit(prev_close: float, close: float, board: str) -> str | None:
    """返回 `'up'` / `'down'` / `None`。涨停买不进、跌停卖不出。"""
    if board not in LIMIT_BY_BOARD or prev_close <= 0:
        raise BoardUnknown(f"板别 {board!r} 不在 {sorted(LIMIT_BY_BOARD)} 中")
    limit = LIMIT_BY_BOARD[board]
    chg = (close - prev_close) / prev_close
    if chg >= limit - _LIMIT_SLACK:
        return "up"
    if chg <= -(limit - _LIMIT_SLACK):
        return "down"
    return None


def _close_dated(conn: sqlite3.Connection, code: str,
                 date: str) -> tuple[str, float] | None:
    """`(该收盘价实际所属的日期, 收盘价)`。取不到返回 `None`。

    返回**真实日期**而不是查询用的日期，是为了让价格侧 PIT 守卫
    （`guard_pit_prices`）能看见「这个价到底属于哪一天」，而不是自证。
    """
    row = conn.execute(
        "SELECT date, close FROM bars_daily WHERE code = ? AND date = ?",
        (code, date)).fetchone()
    if row is None:
        return None
    return str(row["date"]), float(row["close"])


def _close_on(conn: sqlite3.Connection, code: str, date: str) -> float | None:
    got = _close_dated(conn, code, date)
    return None if got is None else got[1]


def guard_pit_prices(*, asof: str,
                     rows: Mapping[str, tuple[str, float] | None]) -> None:
    """价格侧 PIT 守卫：决策日 `asof` 用到的价只能来自 ≤ `asof` 的 bar。

    硬约束 5 / 设计稿 §Q3 的落法：把 `_close_on` 的取值包装成
    `paper.engine.pit_close` 已经在用的那个 `Price(price_asof=...)` 形状，
    再调 `paper.rules.check_no_lookahead` —— **复用同一个函数**，不另写一份守卫
    （`m2/context.py:251` 也是同一个函数在两个入口各跑一遍）。

    这里传的是**bar 的真实日期**（`_close_dated` 的返回），所以守卫不是自证：
    若将来有人把 `_close_dated` 改成取「下一根」或 `>= date`，守卫立刻抛
    `LookaheadError`。缺失（`None`）的标的不进 marks —— 缺价是「不可交易」，
    不是「用了未来价」。
    """
    from stocklab.paper.rules import check_no_lookahead
    from stocklab.portfolio.prices import Price

    marks: dict[str, Price] = {}
    for code, got in rows.items():
        if got is None:
            continue
        d, px = got
        marks[code] = Price(code=code, price=px, source="bars",
                            price_asof=d, detail=d)
    check_no_lookahead(asof, marks)


def _prev_close(conn: sqlite3.Connection, code: str, date: str) -> float | None:
    row = conn.execute(
        "SELECT close FROM bars_daily WHERE code = ? AND date < ?"
        " ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
    return None if row is None else float(row["close"])


def _qty_for(px: float) -> int:
    """每只的名义仓位换算成股数：`max(1, int(POSITION_NOTIONAL / px))`。

    保留 `int()` 的**整股截断**（不是四舍五入）—— 真实下单也不能买半股。
    代价是实际名义额 `px × qty` 可能**略小于** `POSITION_NOTIONAL`（最多差 1 股），
    所以「最低佣金不生效」的充分条件要落在 **`px × qty ≥ 20000`** 上，
    而不是设定的名义额上（见设计文档 §3）。
    """
    return max(1, int(POSITION_NOTIONAL / px))


class _AdjCloses:
    """`(code, d0, d1)` → 该期收益侧要用的 PIT 复权收盘价对，惰性载入并缓存。

    ## 读法（D4，锁定）

    `adjust.load_bars_adjusted(conn, code, as_of=d1, start=chain.usable_from)`：
      - `as_of = d1` ⇒ 只累乘 `cqr <= d1` 的事件，**无未来函数**；
      - `start = chain.usable_from` ⇒ 主动放弃「跨越不可定价事件」的那段历史。
        缩窗口是**显式决定**（`adjust_bars` 的契约要求调用方显式传，不许本层
        替调用方默默做掉）；落在这个段里的 `d0` 一律**回退未复权**并计数。

    ## 为什么把 `load_bars_adjusted` 拆开

    该函数里 `as_of` **无关**的部分（读该只全量 K 线 ＋ 建链 ＋ 核对缺口表）才是
    大头 —— 真库单只 8500 根 K 线实测 **32 ms/次**，而它会被逐 `(code, 周期)`
    重复几万次。所以这里把它的**全部三步**按 `code` 缓存前两步：

        bars, chain = adjust.load_chain(conn, code)
        adjust.assert_blackout_current(conn, code, chain)
        adjust.adjust_bars(bars, chain, as_of=d1, code=code, start=chain.usable_from)

    —— 没有第二套复权口径。唯一的差别是第三步只传**该周期要用的两根 bar**：
    `adjust_bars` 只用到 `usable` 的 `max`（= `base_date`）与 `chain.at(bar.date)`，
    传 `[bar(d0), bar(d1)]` 时 `base_date` 仍是 `d1`、`multiplier(d0) = F(d1)/F(d0)`
    逐位相同。可定价性检查在 `d0 >= chain.usable_from` 时**必为空**（不可定价事件的
    `cqr` 全 `<= usable_from`），所以 `_load` 先把这条前提显式守住。
    等价性由 `tests/test_replay_price_mode.py::test_adj_pair_matches_load_bars_adjusted`
    拿 `load_bars_adjusted` 的返回值直接钉住。

    复权不可用（三类）／`d0` 落在不可用段／该日无 bar ⇒ `pair()` 返回 `None`，
    调用方回退未复权价并按 `(code, 周期)` 计数 `n_fallback`（D3）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._by_date: dict[str, dict[str, Bar]] = {}
        self._chain: dict[str, adjust.Chain] = {}
        self._cache: dict[tuple[str, str, str], tuple[float, float] | None] = {}
        #: 回退计数：**每次 `pair()` 返回 `None` 计一次**，而调用方每 `(code, 周期)`
        #: 只问一次 ⇒ 天然满足 D3 的「按 (code, 周期) 去重」。
        self.n_fallback = 0

    def _bars_and_chain(self, code: str) -> tuple[dict[str, Bar], adjust.Chain]:
        if code not in self._chain:
            bars, chain = adjust.load_chain(self._conn, code)
            adjust.assert_blackout_current(self._conn, code, chain)
            self._by_date[code] = {b.date: b for b in bars}
            self._chain[code] = chain
        return self._by_date[code], self._chain[code]

    def _load(self, code: str, d0: str,
              d1: str) -> tuple[float, float] | None:
        try:
            by_date, chain = self._bars_and_chain(code)
            if chain.usable_from is not None and d0 < chain.usable_from:
                return None          # D4 的缩窗口：d0 在不可用段内 ⇒ 该期回退
            b0, b1 = by_date.get(d0), by_date.get(d1)
            if b0 is None or b1 is None:
                return None          # 该日无 bar（raw 侧同样取不到 ⇒ 本就不进 gross）
            out = adjust.adjust_bars([b0, b1], chain, d1, code=code,
                                     start=chain.usable_from)
        except _ADJ_UNAVAILABLE:
            return None
        closes = {b.date: b.close for b in out}
        p0, p1 = closes.get(d0), closes.get(d1)
        if not p0 or not p1:
            return None
        return p0, p1

    def pair(self, code: str, d0: str, d1: str) -> tuple[float, float] | None:
        key = (code, d0, d1)
        if key not in self._cache:
            self._cache[key] = self._load(code, d0, d1)
        got = self._cache[key]
        if got is None:
            self.n_fallback += 1
        return got


def period_returns(conn: sqlite3.Connection, *, asof_dates: list[str],
                   pool: str, plugin_overrides: dict[str, int] | None = None,
                   costs: CostModel | None = None,
                   hold_override: Mapping[str, Sequence[str]] | None = None,
                   _pools_for_test: dict[str, list[str]] | None = None,
                   price_mode: str = DEFAULT_PRICE_MODE,
                   price_stats: dict | None = None
                   ) -> list[float]:
    """逐周期收益序列（每个调仓周期一个观测）。

    `asof_dates`：调仓日序列，两两之间构成一个周期（长度 = len-1）。
    返回长度 = `len(asof_dates) - 1`。

    ## 语义（**无未来函数**）

    在调仓日 `d0`：观察 → 决策 → 以 `d0` 收盘价成交建立本期持仓 `hold`。
    在下一调仓日 `d1`：把 `hold` 转成 `nxt`（同样在 `d1` 观察决策）。

    - **周期 `[d0, d1]` 的毛收益** = 「在 `d0` 决定的」`hold` 池的等权收益，
      按 `p1/p0 - 1` 计。**不是** `nxt`（`nxt` 要 `d1` 才知道，用它算
      `[d0, d1]` 收益 = 未来函数，本次修复的核心）。
    - **本期成本** = 在 `d1` 执行的调仓账单（把 `hold` 转成 `nxt`）：
      - 卖出 `hold - nxt`（按 `d1` 价格与 `d1` 涨跌停判定）
      - 买入 `nxt - hold`（按 `d1` 价格与 `d1` 涨跌停判定）
      按 `n_hold` 归一化，每只的名义仓位见 `_qty_for`（ADR-017 D-13）。
      首期无起点建仓成本；末期不再计后续新买入。
      另收**滑点**：每成交一条腿 `slippage_bps × (1/n_hold)`（ADR-017 D-14）。
    - **涨跌停（Finding 2）**：`d0` 涨停买不进 → 本期不持有该只 → 不算入
      gross（原代码仅挡了 fee 侧、漏挡收益侧，是本次修复的一部分）。
      `d1` 涨/跌停挡住的仅是**本期末**的调仓账单。
    - **空池 `hold == []`**：`gross = 0`（持现金），但若上期非空则本期末仍
      会有卖出账单——「持有→空」的正常清仓成本。

    `_pools_for_test`：**测试接缝**，`{调仓日: [code, ...]}`。生产路径不传，
    此时池成员由 `score_pipeline` 现算。接缝只控制**池成员**；调仓日序列
    一律由 `asof_dates` 参数传入，两者职责不混。

    ## `hold_override`（P60 横截面实验的**唯一**产品参数）

    `{调仓日: [code, ...]}` —— **显式指定**每个调仓日的持有集合，给了就**只**用它
    （不再调 `score_pipeline`）。缺省 `None` ⇒ **逐位走现状**（与加这个参数之前
    完全相同的代码路径），所以既有调用方的结果逐位不变。

    为什么是 `{日期: [code,...]}` 而不是一个扁平的 `list[str]`：对照臂是「该池
    **全部合格标的**」，这个集合**逐调仓日不同**（合格与否取决于当日已公告的
    财报与 K 线）。扁平列表无法表达「d0 的合格集 ≠ d1 的合格集」，而两者不等
    正是成本项（调仓账单）的来源 —— 传同一个集合会让两期持仓恒等 ⇒ 换手恒为 0
    ⇒ **成本被悄悄抹掉**。

    与 `_pools_for_test` **同时**传 → 抛 `ValueError`：两条路都声称自己决定池成员，
    静默取其一就会得到一个「说不清用哪套成员」的读数。生产路径两个都不传。

    ## `price_mode`（P82 · D1/D2）

    `"raw"`（默认 = **现状**）｜`"adj"`。**只换收益侧的 `p0 / p1`**：
    `adj` 下换成 PIT 复权收盘价（`_AdjCloses`，as-of = `d1`）；成本侧成交价、
    `_qty_for` 整手、涨跌停判定、`guard_pit_prices` 守卫**一律不动** ——
    与 P77/ADR-029 同构（收益用复权、交易约束用真实价）。

    `price_stats`：**旁路统计出口**（D5）。传了就在其中写入 `price_mode` 与
    `n_adj_fallback`（复权不可用 ⇒ 回退未复权的 `(code, 周期)` 数）。默认 `None`
    ⇒ 什么都不写，返回值**逐位不变**（T3 用例①钉住的就是这条）。
    """
    if price_mode not in PRICE_MODES:
        raise ValueError(
            f"未知 price_mode {price_mode!r}；取值只许 {list(PRICE_MODES)}"
            f"（`both` 是 CLI 层的一次扫描出两套读数，不是本函数的取值）")
    if hold_override is not None and _pools_for_test is not None:
        raise ValueError(
            "hold_override 与 _pools_for_test 不能同时传 —— 两者都决定持有集合，"
            "静默取其一会让读数说不清用的是哪一套成员")
    effective_dates = list(asof_dates)

    if len(effective_dates) < 2:
        if price_stats is not None:
            price_stats["price_mode"] = price_mode
            price_stats["n_adj_fallback"] = 0
        return []

    # ADR-008 记：ETF 与 stock 印花税/过户费口径不同，混用会算错成本方向。
    #
    # P60 之前本函数无条件用**单一 stock 口径**（`CostModel()`），当时的理由之三是
    # 「SEED_UNIVERSE 里 ETF 只 4/21，且都不参与短期动量池的实际选中」。
    # 横截面实验（持有集合 = 池内**全部合格标的**）把这条理由作废了 ⇒ 默认改为
    # **逐标的按 `instruments.type` 取口径**（`_costs_for`）。前两条理由（Δ 两端
    # 同口径 ⇒ 绝对水平抵消；方向保守）在新口径下**依然成立**，只是不再需要它们
    # 兜底：两臂仍用同一套口径，所以 Δ 仍是单变量比较。
    #
    # 调用方显式传 `costs`（如「压零成本」的测试）时不做口径分支，一律用它 ——
    # 否则「显式指定」会被静默覆盖成按标的取。
    base_slippage_bps = (costs.slippage_bps if costs is not None
                         else CostModel().slippage_bps)

    from stocklab.candidate.run import score_pipeline  # 延迟 import，避免环

    def _members_on(day: str) -> list[str]:
        if hold_override is not None:
            return sorted(set(hold_override.get(day, ())))
        if _pools_for_test is not None:
            return list(_pools_for_test.get(day, []))
        res = score_pipeline(conn, asof=day, plugin_overrides=plugin_overrides)
        return sorted(m.code for m in res.members if m.pool == pool)

    out: list[float] = []
    #: 复权读层：`price_mode == "raw"` 时**根本不构造** ⇒ 默认路径连一次
    #: `load_chain` 都不会发生（不只数值不变，开销也不变）。
    adj = _AdjCloses(conn) if price_mode == "adj" else None
    for i in range(len(effective_dates) - 1):
        d0, d1 = effective_dates[i], effective_dates[i + 1]
        hold = _members_on(d0)   # 本期实际持仓（d0 决定，[d0,d1] 持有）
        nxt = _members_on(d1)    # 下期持仓（d1 决定），仅用于本期末的调仓账单

        # 收益侧：iterate `hold`（**不是** `nxt`）。
        # `nxt` 是 d1 才能知道的池，用它算 [d0,d1] 的收益 = 未来函数。
        # 每只 c ∈ hold 需要通过 d0 的可交易性检查：涨停买不进的标的等价于
        # 「无法在 d0 建仓 → [d0,d1] 期间不持有它 → 不产生该只的收益」。
        # `hold - tradable_at_d0` 的部分等价于持现金（该只贡献 0，但仍摊薄 n）。
        codes = set(hold) | set(nxt)
        p0_rows = {c: _close_dated(conn, c, d0) for c in codes}
        p0 = {c: (g[1] if g is not None else None) for c, g in p0_rows.items()}
        p1 = {c: _close_on(conn, c, d1) for c in codes}
        # 价格侧 PIT 守卫（硬约束 5）：**决策日** d0 用的价只能属于 ≤ d0 的 bar。
        # d1 的价属于结算日，本身合法，所以只在 d0 这一侧设卡。
        guard_pit_prices(asof=d0, rows=p0_rows)

        # 计算 d0 的可交易过滤（涨停不可买 → 该只从 hold 剔除，不进 gross）
        tradable_hold: list[str] = []
        for c in hold:
            px = p0.get(c)
            if px is None:
                continue  # 无 d0 价 → 无法建仓，不进 gross（与「无价」不可交易一致）
            board = _board_of(conn, c)
            prev = _prev_close(conn, c, d0)
            if _limit_hit(prev if prev is not None else px, px, board) == "up":
                continue  # 涨停 d0 买不进 → 期间不持有 → 不进 gross（Finding 2）
            tradable_hold.append(c)

        gains: list[float] = []
        for c in tradable_hold:
            q0, q1 = p0.get(c), p1.get(c)
            if not (q0 and q1):
                continue
            if adj is not None:
                # D2：**只**换收益分子/分母。复权不可用 ⇒ 回退本期的未复权对
                # （D3），同时由 `pair()` 计入 `n_fallback`（不抛、不静默）。
                pair = adj.pair(c, d0, d1)
                if pair is not None:
                    q0, q1 = pair
            gains.append(q1 / q0 - 1.0)
        # 等权：分母是 hold 的**目标持仓数**（含无法买入的部分——它们占权重但收益 0）。
        # 空 hold → 无持仓收益（持现金）；仅在下方产生清仓成本。
        n_hold = max(len(hold), 1)
        gross = (sum(gains) / n_hold) if hold else 0.0

        # 成本：本期**末**（d1）执行调仓账单——把 hold 转成 nxt。
        # 卖出 = hold - nxt（在 d1 卖），买入 = nxt - hold（在 d1 买）。
        # 使用 **d1 价格与 d1 涨跌停**（trades 实际发生在 d1）。
        # 归一化用 `n_hold`——本期的等权分母；空 hold 时用 1（此时唯一可能的 fee
        # 是「新一期买入」，但那属于下一期的持仓建立而非本期成本；本模型把它
        # 也计入本期，与「空池→现金→下一期新建仓」的算账语义一致）。
        entering = [c for c in hold if c not in nxt]   # d1 卖出
        new = [c for c in nxt if c not in hold]        # d1 买入
        fee_ratio = 0.0
        #: 成交的腿数（滑点在下面按它计）。被涨跌停挡住的腿**没成交**，
        #: 所以既不收费也不收滑点 —— 与费用严格同形。
        n_filled = 0

        for c in entering:
            px = p1.get(c)
            if px is None:
                continue
            board = _board_of(conn, c)
            prev = _prev_close(conn, c, d1)
            if _limit_hit(prev if prev is not None else px, px, board) == "down":
                continue  # d1 跌停卖不出（免费用）
            qty = _qty_for(px)
            fee_ratio += (_costs_for(conn, c, costs).fees("sell", px, qty)
                          / (px * qty) / n_hold)
            n_filled += 1

        for c in new:
            px = p1.get(c)
            if px is None:
                continue
            board = _board_of(conn, c)
            prev = _prev_close(conn, c, d1)
            if _limit_hit(prev if prev is not None else px, px, board) == "up":
                continue  # d1 涨停买不进（免费用，且该只不会进下一期 gross）
            qty = _qty_for(px)
            fee_ratio += (_costs_for(conn, c, costs).fees("buy", px, qty)
                          / (px * qty) / n_hold)
            n_filled += 1

        # 滑点（ADR-017 D-14）：铁律 4 要求滑点必须进净值曲线。
        # 等权下每只占 1/n_hold，所以一条腿的滑点 = slippage_bps × (1/n_hold)。
        # 旧实现把 `costs.total()` 的第一个返回值（滑点价）丢掉 → **等于没算滑点**：
        # 滑点只让费用基数大了 5bp，对 0.025% 的佣金可忽略。
        # 滑点按**调用方口径**取（stock / ETF 的滑点都是 5 bps，逐标的取也一样）。
        slippage_ratio = base_slippage_bps / 10_000.0 * n_filled / n_hold

        out.append(gross - fee_ratio - slippage_ratio)
    if price_stats is not None:
        # D5：只**新增**键，既有键的名字与语义一个都不动。
        price_stats["price_mode"] = price_mode
        price_stats["n_adj_fallback"] = adj.n_fallback if adj is not None else 0
    return out


# ---------------------------------------------------------------------------
# Task 4：Δ 序列与训练/验证切分
# ---------------------------------------------------------------------------

#: 训练段占比。按**周期序号**切，不按日历 —— 保证两段周期长度相同。
SPLIT_TRAIN_RATIO: float = 0.7


def split_train_validate(deltas: list[float]) -> tuple[list[float], list[float]]:
    """按周期序号切训练段（前 70%）与验证段（后 30%）。

    两段**周期长度相同**（都来自同一套 `REBALANCE_DAYS`），所以 Δ 可比。

    边界情况：
    - 空列表 → ([], [])
    - 长度 1 → 整个列表放训练段，验证段为空（不满足 len > 1 的前提，不做切分）
    - 长度 2 → ([首], [尾])，保证验证段至少有一条
    """
    if not deltas:
        return [], []
    n_train = int(len(deltas) * SPLIT_TRAIN_RATIO)
    # 长度 > 1 时：至少 1 训练 + 1 验证；长度 == 1 时：整个归训练
    n_train = max(1, min(n_train, len(deltas) - 1)) if len(deltas) > 1 else 1
    return deltas[:n_train], deltas[n_train:]


def replay_period_deltas(conn: sqlite3.Connection, *, candidate_script_id: int,
                         baseline_script_id: int, pool: str,
                         window_start: str, window_end: str,
                         costs: CostModel | None = None,
                         trading_days: list[str] | None = None,
                         _pools_for: dict | None = None
                         ) -> tuple[list[float], list[float]]:
    """回放两个版本，返回 `(训练段 Δ, 验证段 Δ)`。

    Δ = 候选版本周期收益 − 基线版本周期收益。

    **单变量**：只有该 `plugin_id` 的两个版本不同；其余插件由
    `score_pipeline` 解析 active 版本（见 `period_returns`）。

    `_pools_for`：测试接缝，`{"cand": {日期: [code,...]}, "base": {...}}`，
    直接指定两个版本各自的成员，绕开 `score_pipeline` 调用。生产路径不传。

    注：`from stocklab.candidate.run import SEED_UNIVERSE` 在本函数里**不需要**。
    `score_pipeline`（在 `period_returns` 里延迟 import）自身已从 `candidate.run`
    导入 `SEED_UNIVERSE`，调用时 SEED_UNIVERSE 必然已在内存中。
    若不传 `_pools_for`，`period_returns` 会调 `score_pipeline`；
    若传了 `_pools_for`，直接走接缝，`score_pipeline` 不被调用。
    两种路径都不需要在这里额外 import SEED_UNIVERSE。
    """
    # 单变量检查：两个版本必须属于同一个 plugin_id。
    # 无论两个 script_id 是否相同，都查库拿 plugin_id。
    # 这确保「pin X」语义：score_pipeline 收到 {pid: X}，不会静默解析成 active 版本。
    pid = _plugin_id_of(conn, candidate_script_id)
    base_pid = _plugin_id_of(conn, baseline_script_id)
    if pid != base_pid:
        raise ValueError(
            f"两个版本属于不同插件（{pid!r} vs {base_pid!r}）—— 单变量原则"
            "要求只换同一个 plugin_id 的版本")
    eff_pid = pid

    period = REBALANCE_DAYS[pool]
    days = trading_days or _trading_days(conn, window_start, window_end)
    marks = rebalance_dates(days, period=period, start=window_start,
                            end=window_end)

    cand_overrides = {eff_pid: candidate_script_id}
    base_overrides = {eff_pid: baseline_script_id}

    cand = period_returns(
        conn, asof_dates=marks, pool=pool,
        plugin_overrides=cand_overrides, costs=costs,
        _pools_for_test=(_pools_for or {}).get("cand"))
    base = period_returns(
        conn, asof_dates=marks, pool=pool,
        plugin_overrides=base_overrides, costs=costs,
        _pools_for_test=(_pools_for or {}).get("base"))

    deltas = [c - b for c, b in zip(cand, base)]
    return split_train_validate(deltas)


# ---------------------------------------------------------------------------
# Task 7：基准超额与调仓日公开薄封装（供 sandbox 经注入调用）
# ---------------------------------------------------------------------------

#: 基准指数代码（腾讯口径，见 ADR-003 的指数源）。
BENCHMARK_CODE = "sh000300"


def benchmark_excess(conn: sqlite3.Connection, *, asof_dates: list[str],
                     pool: str, plugin_overrides: dict[str, int] | None = None,
                     benchmark: str = BENCHMARK_CODE,
                     costs: CostModel | None = None,
                     hold_override: Mapping[str, Sequence[str]] | None = None
                     ) -> float:
    """候选池相对基准指数的**超额收益**（同区间、同调仓日）。

    铁律要求「任何策略必须与 index_300 比较，跑不赢就明说」—— 所以这个
    数与版本 Δ **并列报告**，不是替代。

    ## 缺失基准 bar 的处理（Finding 3，显式记账）

    某个调仓边界 `d0`/`d1` 在 `bars_daily` 里查不到基准收盘价时（例如
    历史扩充覆盖不全、或指数于该日无行情），本函数**跳过该周期**——
    池收益侧与基准侧同步跳过，保证「同区间对齐」。这与旧实现「零填充」
    的差别：零填充会把「缺数据的周期」当成「基准零涨跌」参与均值，
    人为压低基准均值 → 虚增超额。跳过是更保守的选择：宁少一期，也
    不把无观测当零。

    该数仅进入报告 `detail`（打印给用户看的行），**不进 verdict**——
    verdict 只看 Δ 序列。所以此处的口径选择不会撬动结论。

    `hold_override`：**透传**给内部的 `period_returns`（同一个变量，不是第二个
    变量）。没有它，对照臂（持有集合 = 全部合格标的）就没法算与它同口径的超额 ——
    要么另写一份对齐逻辑（= 第二份口径），要么把对照臂的超额报成现状臂的。
    """
    if len(asof_dates) < 2:
        return 0.0
    pool_r_all = period_returns(conn, asof_dates=asof_dates, pool=pool,
                                plugin_overrides=plugin_overrides, costs=costs,
                                hold_override=hold_override)
    # 同区间对齐：pool_r_all 的第 i 项对应 (asof_dates[i], asof_dates[i+1])。
    # 缺基准 bar 的周期，池收益侧与基准侧同步跳过（不零填充）。
    aligned_pool: list[float] = []
    aligned_bench: list[float] = []
    for i, (d0, d1) in enumerate(zip(asof_dates, asof_dates[1:])):
        a, b = _close_on(conn, benchmark, d0), _close_on(conn, benchmark, d1)
        if not (a and b):
            continue  # 缺基准 bar：整个周期都不参与均值
        aligned_bench.append(b / a - 1.0)
        aligned_pool.append(pool_r_all[i])
    if not aligned_pool:
        return 0.0
    return (sum(aligned_pool) / len(aligned_pool)
            - sum(aligned_bench) / len(aligned_bench))


def rebalance_marks(conn: sqlite3.Connection, *, pool: str, start: str,
                    end: str) -> list[str]:
    """窗口内的调仓日序列。供 `sandbox` 经注入调用（它不能 import 本模块）。"""
    days = _trading_days(conn, start, end)
    return rebalance_dates(days, period=REBALANCE_DAYS[pool],
                           start=start, end=end)


def _plugin_id_of(conn: sqlite3.Connection, script_id: int) -> str:
    from stocklab.plugin import store
    row = store.get_script(conn, script_id)
    if row is None:
        raise LookupError(f"脚本 {script_id} 不存在")
    return str(row["plugin_id"])


def _trading_days(conn: sqlite3.Connection, start: str,
                  end: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT date FROM trading_calendar WHERE date BETWEEN ? AND ?"
        " ORDER BY date", (start, end))]
