"""预测服务（Task 31，P6）：取数 → 策略证据 → 模型 → 载荷 → 落库。

## 这个模块负责的三道「不许」的防线

1. **不许看未来**。历史取数走 `adjust.load_bars_adjusted(conn, code, asof)`，
   `asof` 是**字面写在这里**的唯一上限；模型再做一次「最后一根必须是 asof」的校验。
   两道都在，是因为本项目的教训是「防线要放在结构上，不能指望每个调用点都记得裁」
   —— 但**取数层**这道是真正生效的那道（模型那道只挡「拿最近一根冒充今日」）。
2. **不许在复权缺口上回退**。`load_pit_bars` 先查 `usable_from`：
   `asof` 早于可用下界 → 抛 `UnusableWindow`。**没有**「取不到复权价就用不复权价」
   这条分支，因为不复权价在除权日是假跌幅，会让模型算出一个**数字合法、结论全错**
   的预测（ERROR_DIARY 2026-09-14「宽松回退」同款）。
3. **不许假装有集成**。`strategy_evidence` 逐个已注册策略给出**身份**：
   `active` / `excluded_falsified` / `benchmark_only` / `inactive_unproven`。
   权重**只**从 `ACTIVE_STRATEGIES` 白名单（当前为空）里来，
   所以 `weights` 必然是 `{}`、`degenerate=True` 必然写进载荷 ——
   这不是硬编码，而是「注册 ≠ 有 edge」这条规则算出来的结果。

## 历史回放

整条链只依赖「库里 <= asof 的行」，没有任何 `datetime.now()` 参与计算
（`now` 只用于 `created_at` 落库）。所以对任意过去的 `asof` 都能算出**当时该给的**
预测 —— 这是日后测准确率的前提。
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from stocklab.calendar.holidays import HolidayTable, load_holiday_table, weekday_of
from stocklab.calendar.trading_calendar import Calendar
from stocklab.data import adjust
from stocklab.data.models import Bar
from stocklab.predict.model import (DegenerateInput, compute_forecast,
                                    degenerate_strategy_mix, payload_hash)
from stocklab.predict.version import (ACTIVE_STRATEGIES,
                                      BENCHMARK_ONLY_STRATEGIES,
                                      FALSIFIED_STRATEGIES, MODEL_VERSION)
from stocklab.strategies.registry import strategy_registry


class UnusableWindow(RuntimeError):
    """该标的在 `asof` **没有可用的复权窗口**（早于 `adj_factor_blackout` 的可用下界）。

    刻意是异常而不是「返回空列表」：空列表会被调用方当成「这只票今天没数据」，
    而真相是「这只票的这段历史在复权口径下**不可用**」。两者必须能区分
    （ERROR_DIARY 2026-09-15：「这个字段为空时，是『没有』还是『没填』？」）。
    """


class NotASession(RuntimeError):
    """`asof` 不是「日历 ∩ 行情」双重口径下的交易日。"""


# ---------- 交易日判定与 target_date ----------

def session_axis(conn: sqlite3.Connection, codes: Sequence[str]) -> set[str]:
    """行情轴：给定标的在 `bars_daily` 里**真的出现过 K 线**的日期。

    刻意读不复权的 `bars_daily`：日期是否存在与复权口径无关，
    而 `adj_factors` 只覆盖到因子链算得出的日期。
    """
    if not codes:
        return set()
    marks = ",".join("?" * len(codes))
    return {r["date"] for r in conn.execute(
        f"SELECT DISTINCT date FROM bars_daily WHERE code IN ({marks})", tuple(codes))}


def market_axis(conn: sqlite3.Connection) -> set[str]:
    """全市场行情轴：`instruments` 里所有在用标的在 `bars_daily` 出现过的日期。

    用于判「**市场**那天开不开市」。刻意与「请求的标的当天有没有 K 线」分开：
    后者是**单只票停牌**，该跳过的是那只票，不是整批预测。
    把两者混在一个判据里，会让一只停牌票把当天的全部预测打掉。
    """
    return session_axis(conn, [r["code"] for r in conn.execute(
        "SELECT code FROM instruments WHERE active=1 AND type='stock'")])


def assert_session(conn: sqlite3.Connection, calendar: Calendar, asof: str, *,
                   cache: PitCache | None = None) -> None:
    """`asof` 必须同时落在日历与**市场**行情轴上（**取交集**）。

    - 不在日历 → 「那天根本不开市」；
    - 在日历但整个市场当天都没有 K 线 → 「日历说开市、库里却没有那天的行情」，
      多半是采集缺口。此时出预测等于拿旧数据冒充今天，必须拒绝。
    """
    cal_dates = set(calendar.all_dates)
    if asof not in cal_dates:
        raise NotASession(
            f"{asof} 不在 trading_calendar（日历最早 {calendar.all_dates[0]}、"
            f"最晚 {calendar.all_dates[-1]}）—— 非交易日，拒绝出预测"
        )
    axis = cache.market_axis(conn) if cache is not None else market_axis(conn)
    if asof not in axis:
        raise NotASession(
            f"{asof} 在日历里是交易日，但库里没有任何标的当天有 K 线"
            "（采集缺口）—— 拒绝出预测"
        )


def resolve_target_date(calendar: Calendar, asof: str, *,
                        holidays: "HolidayTable | None" = None) -> tuple[str, str]:
    """下一个交易日 = 日历中**严格大于** `asof` 的第一个日期。

    返回 `(date, source)`，`source ∈ {"trading_calendar", "holiday_table",
    "weekday_fallback"}`。

    **为什么不与行情轴取交集**：未来的交易日必然还没有 K 线，取交集恒为空。
    故 `target_date` 只用日历 —— 而日历**今天就是耗尽态**
    （`trading_calendar` 最晚 = `2026-09-16`），此时按**三级**处理：

    1. 日历里有 > asof 的日期 → `trading_calendar`（**最强**，实测交易日）；
    2. 日历耗尽 + 休市表**覆盖**该年（有年度休市安排通知）→ 沿已公告的休市日往后推到
       第一个开市日，`source="holiday_table"`（**用了真实休市公告**，前瞻已知信息，
       PIT 判断见 ADR-013）；
    3. 日历耗尽 + 休市表**覆盖不到**（尚未公告的年份，如 2027 元旦）→ 退回
       `weekday_fallback`：**纯工作日外推**，会算错节假日，是本函数**最弱**的一档。

    第 2/3 档的区别必须留在 `source` 里 —— 载荷与报告会原样带上它，
    下游据此知道「这个 target_date 是查了公告的」还是「是猜的」。
    """
    later = [d for d in calendar.all_dates if d > asof]
    if later:
        return later[0], "trading_calendar"

    cand = _next_weekday(asof)
    if holidays is not None:
        d = cand
        # 覆盖范围内：已公告休市就继续往后推（春节最长连休 7 个工作日，循环必在上限内收敛）
        while holidays.covers(d) and holidays.is_closed(d):
            d = _next_weekday(d)
        if holidays.covers(d):
            return d, "holiday_table"
        # 覆盖不到 → 原样退回工作日外推（语义与 P30 之前**逐字一致**）
        return d, "weekday_fallback"
    return cand, "weekday_fallback"


def _next_weekday(after: str) -> str:
    """`after` 之后的下一个周一~周五（纯日历外推，不知道任何节假日）。"""
    y, m, d = (int(x) for x in after.split("-"))
    o = y * 372 + m * 31 + d
    while True:
        o += 1
        yy, rem = divmod(o, 372)
        mm, dd = divmod(rem, 31)
        if mm < 1 or mm > 12 or dd < 1 or dd > 31:
            continue
        if weekday_of(yy, mm, dd) < 5:
            return f"{yy:04d}-{mm:02d}-{dd:02d}"


# ---------- PIT 取数 ----------

def _target_date_note(source: str, holidays: HolidayTable) -> str:
    """`notes.target_date` 的文案（**按事实取值，不按开关取值** —— ERROR_DIARY #16）。

    `trading_calendar` 那一支的字符串**逐字节保持不变**：回归红线
    (`docs/baselines/redlines.json`) 盯的就是两个 predict 载荷的 sha256，
    而它们都用 `trading_calendar`。改这个分支的文案 = 动红线，必须单独归因。
    """
    if source == "trading_calendar":
        return "target_date 取自 trading_calendar，非外推"
    if source == "holiday_table":
        b = holidays.bounds()
        return (
            f"target_date 由**已公告的休市安排**推出（source=holiday_table）："
            f"trading_calendar 已耗尽，改用 market_holidays（已公告休市 {b[0]}~{b[1]}，"
            "覆盖年份取有**年度通知**者）沿休市日往后推到第一个开市日。"
            "休市安排是交易所提前公告的公开日历信息，PIT 判断见 ADR-013"
        )
    tail = ""
    if holidays.rows:
        tail = ("（休市表里有公告，但**覆盖不到**该日期所在的年份 —— "
                "尚未公告的年份不许假装知道）")
    return (
        "target_date 取自 trading_calendar 的下一交易日；"
        "日历耗尽时为「下一个工作日」外推（source=weekday_fallback）—— "
        "**那是工作日外推，不是交易日历**，它是最弱的一个字段" + tail
    )


class PitCache:
    """批量回放期的**只读记忆化**（默认为 `None`，单日 `predict` 不用它）。

    ## 为什么需要

    `load_chain` 要把该标的**全部** K 线读出来并按事件累乘因子（600690 有 7807 根），
    `market_axis` 要扫全表 DISTINCT 日期，`Calendar.load` 要读整张日历 ——
    这些量**都不随 `asof` 变化**。单日预测各算一次没问题，但历史回放要跑几千天，
    实测单日 0.16 秒里绝大部分花在重算这些不变量上（3000 天 ≈ 8 分钟）。

    ## 为什么它不改变结果

    缓存的只是「输入不变则输出不变」的**只读派生量**（因子链、日历、行情轴、原始 K 线）；
    每次仍然重新 `assert_blackout_current`（它会因库变化而报错，不能缓存判断结果）
    并重新 `adjust_bars(as_of)`。`test_backfill_payloads_match_predict_run`
    逐日比对 `payload_sha256`，把「缓存版 == 非缓存版」钉死。
    """

    def __init__(self) -> None:
        self._chains: dict[str, tuple] = {}
        self._usable: dict[str, str | None] = {}
        self._raw: dict[str, tuple] = {}
        self._calendar: Calendar | None = None
        self._axis: set[str] | None = None
        self._holidays: HolidayTable | None = None
        self._money_flow: dict[str, dict[str, float | None]] = {}
        self._valuation: dict[str, list[tuple[str, float | None]]] = {}

    def chain(self, conn: sqlite3.Connection, code: str) -> tuple:
        if code not in self._chains:
            self._chains[code] = adjust.load_chain(conn, code)
        return self._chains[code]

    def usable_from(self, conn: sqlite3.Connection, code: str) -> str | None:
        if code not in self._usable:
            self._usable[code] = adjust.usable_from(conn, code)
        return self._usable[code]

    def raw_bars(self, conn: sqlite3.Connection, code: str) -> tuple[list[Bar], set[str]]:
        """该标的**全量**不复权 K 线 + 停牌日集合（读一次，之后按日期过滤）。"""
        if code not in self._raw:
            self._raw[code] = _read_raw(conn, code)
        return self._raw[code]

    def calendar(self, conn: sqlite3.Connection) -> Calendar:
        if self._calendar is None:
            self._calendar = Calendar.load(conn)
        return self._calendar

    def market_axis(self, conn: sqlite3.Connection) -> set[str]:
        if self._axis is None:
            self._axis = market_axis(conn)
        return self._axis

    def holidays(self, conn: sqlite3.Connection) -> HolidayTable:
        """已公告休市表（只读派生量；`market_holidays` 是 append-only，回放期不变）。"""
        if self._holidays is None:
            self._holidays = load_holiday_table(conn)
        return self._holidays

    def money_flow(self, conn: sqlite3.Connection, code: str) -> dict[str, float | None]:
        """`code` 的 `main_net` 全序列 `{date: main_net}`（读一次；NULL 原样保留）。"""
        if code not in self._money_flow:
            self._money_flow[code] = _read_money_flow(conn, code)
        return self._money_flow[code]

    def valuation(self, conn: sqlite3.Connection, code: str) -> list[tuple[str, float | None]]:
        """`code` 的 `pe_ttm` 全序列 `[(date, pe_ttm), ...]`（按 date 升序，读一次）。"""
        if code not in self._valuation:
            self._valuation[code] = _read_valuation(conn, code)
        return self._valuation[code]


def _read_raw(conn: sqlite3.Connection, code: str) -> tuple[list[Bar], set[str]]:
    """全量不复权 K 线 + 停牌日集合（`PitCache.raw_bars` 的底层读取）。"""
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, amount, turnover, source,"
        " adj_mode, is_suspended FROM bars_daily WHERE code=? ORDER BY date",
        (code,)).fetchall()
    bars = [Bar(code=code, date=r["date"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
                amount=r["amount"], turnover=r["turnover"], source=r["source"],
                adj_mode=r["adj_mode"]) for r in rows]
    return bars, {r["date"] for r in rows if r["is_suspended"]}


def _read_money_flow(conn: sqlite3.Connection, code: str) -> dict[str, float | None]:
    """`code` 的 `money_flow_daily.main_net` 全序列（**只读**；`date → main_net`）。

    返回**全量**（含未来行）：裁剪是 `load_mf_sign` 自己的第一件事 ——
    「调用方已经裁好了」这种约定一旦有人忘，前视就静默发生了（与 `_read_raw` 同款理由）。
    """
    rows = conn.execute(
        "SELECT date, main_net FROM money_flow_daily WHERE code=? ORDER BY date",
        (code,)).fetchall()
    return {r["date"]: r["main_net"] for r in rows}


def _read_valuation(conn: sqlite3.Connection, code: str) -> list[tuple[str, float | None]]:
    """`code` 的 `valuation_daily.pe_ttm` 全序列（**只读**；`[(date, pe_ttm), ...]` 升序）。"""
    rows = conn.execute(
        "SELECT date, pe_ttm FROM valuation_daily WHERE code=? ORDER BY date",
        (code,)).fetchall()
    return [(r["date"], r["pe_ttm"]) for r in rows]


def load_pit_bars(conn: sqlite3.Connection, code: str, asof: str, *,
                  cache: PitCache | None = None) -> list[Bar]:
    """读 `code` 在 `asof`（含）之前的**复权**日 K。

    复权链不可用的区间 → `UnusableWindow`（**硬拒绝，无回退分支**）。
    """
    floor = (cache.usable_from(conn, code) if cache is not None
             else adjust.usable_from(conn, code))
    if floor is not None and asof < floor:
        raise UnusableWindow(
            f"{code} 在 {asof} 没有可用的复权窗口：该标的复权链的可用下界是 {floor}"
            "（此前有无法定价的除权事件，不复权序列在那一段残留假跌幅）。"
            "**拒绝回退到不复权价** —— 那会算出一个数字合法、结论全错的预测"
        )
    # start=floor：把窗口显式挪到可用下界（None 时表示全历史都可用）
    if cache is None:
        return adjust.load_bars_adjusted(conn, code, asof, start=floor)
    bars, chain = cache.chain(conn, code)
    # 不缓存这个判断：它会在「库里的缺口记录与链不同源」时报错，
    # 那是个**应当被重新发现**的事实，不是不变量。
    adjust.assert_blackout_current(conn, code, chain)
    return adjust.adjust_bars(bars, chain, asof, code=code, start=floor)


# ---------- 策略证据 ----------

def strategy_evidence(asof: str, history_by_code: dict[str, list[Bar]]) -> list[dict]:
    """逐个**已注册**策略给出身份与（若有）当日信号。

    四类身份（**注册 ≠ 有 edge**，见 `version.ACTIVE_STRATEGIES`）：
      - `active`：在 `ACTIVE_STRATEGIES` 白名单里（**当前为空**）→ 唯一的权重来源；
      - `excluded_falsified`：样本外绩效被否证（见 `version.FALSIFIED_STRATEGIES`）；
      - `benchmark_only`：只作对照，不提供次日方向信息；
      - `inactive_unproven`：已注册但**没有任何样本外正向证据**。

    最后一类是本函数的关键：`strategy_registry` 只证明「能被评估」。
    把「已注册」当成「可参与集成」，任何新写的策略都会**自动**拿到权重 ——
    一份「多策略集成」会在零证据下凭空出现。所以**只有白名单里的才 `generate`**，
    未证明的策略连跑都不跑（跑它还可能因状态副作用污染别的标的）。

    信号由 `Strategy.generate` 产出 —— 它内部先 `clip_history` 再派发，
    所以即使传进去的历史含未来行，策略也**读不到**（策略层自己的防线）。
    """
    out: list[dict] = []
    for sid in strategy_registry.ids():
        entry: dict = {"strategy_id": sid, "signal": None, "weight": 0.0}
        if sid in FALSIFIED_STRATEGIES:
            entry.update(status="excluded_falsified",
                         reason=FALSIFIED_STRATEGIES[sid])
        elif sid in BENCHMARK_ONLY_STRATEGIES:
            entry.update(status="benchmark_only",
                         reason="只作对照，不提供次日方向信息；给它权重 = 假装有集成")
        elif sid in ACTIVE_STRATEGIES:
            entry.update(status="active", reason="")
            sigs = strategy_registry.get(sid).generate(asof, history_by_code)
            entry["signal"] = {c: s.action for c, s in sorted(sigs.items())}
        else:
            entry.update(status="inactive_unproven", reason=(
                "已注册但**没有样本外正向证据**：注册只说明「能被评估」，"
                "不说明有 edge。加入 version.ACTIVE_STRATEGIES 才能参与集成"
            ))
        out.append(entry)
    return out


def active_weights(evidence: Sequence[dict]) -> dict[str, float]:
    """`active` 策略的等权权重。

    **当前必然是空 dict**（`ACTIVE_STRATEGIES` 为空：`trend_ma` 被否证、
    `buy_and_hold` 只作对照、其余注册策略无样本外证据）。
    留这个函数是为了让「将来加了新策略会怎样」有一个**唯一**的落点，
    而不是散在组装逻辑里 —— 也为了让 `weights == {}` 这件事来自代码而非硬编码。
    """
    actives = [e["strategy_id"] for e in evidence if e["status"] == "active"]
    if not actives:
        return {}
    w = 1.0 / len(actives)
    return {sid: w for sid in actives}


# ---------- 组装 ----------

def build_predictions(conn: sqlite3.Connection, asof: str,
                      codes: Sequence[str] | None = None, *,
                      cache: PitCache | None = None) -> dict:
    """算出 `asof` 的全部预测载荷（**不写库**；落库由 CLI/调用方决定）。

    返回一份可直接落盘的报告 dict，**不含任何时间戳** ——
    同一 `asof` + 同一 `model_version` 重复运行必须逐字节一致。
    """
    from stocklab.config.universe import Instrument

    if codes is None:
        codes = [r["code"] for r in conn.execute(
            "SELECT code FROM instruments WHERE active=1 AND type='stock'"
            " ORDER BY code")]
    codes = list(codes)
    calendar = cache.calendar(conn) if cache is not None else Calendar.load(conn)
    assert_session(conn, calendar, asof, cache=cache)
    holidays = cache.holidays(conn) if cache is not None else load_holiday_table(conn)
    target_date, td_source = resolve_target_date(calendar, asof, holidays=holidays)

    history: dict[str, list[Bar]] = {}
    skipped: dict[str, str] = {}
    for code in codes:
        try:
            rows = load_pit_bars(conn, code, asof, cache=cache)
        except (adjust.AdjustError, UnusableWindow) as exc:
            skipped[code] = f"{type(exc).__name__}: {exc}"
            continue
        # 显式区分「没有数据」与「有数据但今天这根不在」——两者都不出预测，
        # 但原因必须能读出来（ERROR_DIARY：「为空时是『没有』还是『没填』？」）
        if not rows or rows[-1].date != asof:
            last = rows[-1].date if rows else "无"
            skipped[code] = (
                f"NoBarOnAsof: {code} 在 {asof} 无 K 线（最后一根 {last}）"
                "—— 停牌或采集缺口，拒绝用旧价冒充今日"
            )
            continue
        history[code] = rows

    evidence = strategy_evidence(asof, history)
    weights = active_weights(evidence)
    mix = degenerate_strategy_mix(
        weights=weights,
        excluded={e["strategy_id"]: e["reason"] for e in evidence
                  if e["status"] == "excluded_falsified"},
        benchmark_only=[e["strategy_id"] for e in evidence
                        if e["status"] == "benchmark_only"],
    )

    predictions: list[dict] = []
    per_code_evidence: dict[str, dict] = {}
    for code in sorted(history):
        try:
            p = compute_forecast(code=code, asof=asof, bars=history[code],
                                 target_date=target_date, strategy_mix=mix)
        except DegenerateInput as exc:
            skipped[code] = f"DegenerateInput: {exc}"
            continue
        predictions.append(p)
        ev = dict(p["evidence"])
        ev["target_date_source"] = td_source
        per_code_evidence[code] = ev

    return {
        "asof_date": asof,
        "target_date": target_date,
        "target_date_source": td_source,
        "model_version": MODEL_VERSION,
        "session_check": {"axis": "trading_calendar ∩ bars_daily",
                          "asof": asof, "codes": codes},
        "predictions": predictions,
        "payload_sha256": {p["code"]: payload_hash(p) for p in predictions},
        "evidence": per_code_evidence,
        "strategies": evidence,
        "strategy_weights": weights,
        "skipped": skipped,
        "notes": {
            "target_date": _target_date_note(td_source, holidays),
            "integration": (
                "当前集成是**退化**的："
                + (f"仅 {sorted(weights)} 参与" if weights else
                   "没有任何未被否证的方向性策略参与，方向概率 100% 来自统计模型")
                + "；`buy_and_hold` 只作对照，`trend_ma` 已被样本外绩效否证"
            ),
            "accuracy": (
                "本报告**不含任何准确率数字**：预测准不准要由 P7 的次日验证器按 "
                "§8.2 评分后才可上报，且必须先满足「样本外 + 按日聚类 + 样本量门槛」"
            ),
        },
    }
