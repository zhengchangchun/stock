"""复权因子链与复权读取层（ADR-001 D-01 / ADR-004）。

**为什么不用数据源的 qfq 序列**（探针实测，见 ADR-004）：
腾讯的「前复权」是**减法式**（原价 − 累计每股派息），000333 的 2013-09-18 因此
变成 **-12.649 元**。负价会让任何比率类特征（`ret_1d`/`ma`/`atr`）跨零点翻转。
本模块改为**乘性**因子链：`factor ∈ (0,1]`、价格恒正，且 as-of 语义天然成立。

三层结构：
  `parse_terms`   源站原文 → (每股现金, 每股送转比例)
  `build_chain`   事件 + 除权前收盘 → `{date: Π_{cqr<=date} k}`
  `load_bars_adjusted`  因子链 + 不复权 K 线 → 复权 K 线（**读取层复权**）

两条**不可妥协**的纪律：
  ① 解析不出条款的事件**不得当成 k=1 静默略过**（那会把假跌幅留在序列里）；
     无法定价的事件改成「链的可用区间下界」，见 `usable_from`。
  ② 缺因子的日期**抛错**，绝不回退到不复权（ERROR_DIARY 2026-09-14）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from math import prod

from stocklab.config.universe import instrument_type
from stocklab.data.models import Bar, CorpAction

#: 源站原文里的条款词（配股需要配股价，本模块不支持 → 必须显式报错）
_RE_CASH = re.compile(r"派(?:发现金)?([0-9.]+)元")
_RE_SONG = re.compile(r"送([0-9.]+)股")
_RE_ZHUAN = re.compile(r"转(?:增)?([0-9.]+)股")
_RE_PEI = re.compile(r"配([0-9.]+)股")

#: 条款词的分母（「10派2元」= 每 10 股派 2 元）
_PER = 10.0


class AdjustError(ValueError):
    """复权链相关的一切「不能算就不算」错误。"""


class UnpriceableTerms(AdjustError):
    """事件**不可定价**（无原文 / 原文里没有可识别条款 / 含配股 / 条款数理上荒谬）。

    最后一类是本档的扩容（P75）：源站偶尔把数量级写错（真库 `600602` 的
    `1992-03-09`「10派100元」，每股现金 10 元 ≥ 除权前收盘 9.35 元 ⇒ `k ≤ 0`）。
    它与前几类**同性质** —— 条款不可信 ⇒ 该事件算不出系数 ⇒ 逐日假跌幅留在序列里，
    于是走同一条路：记进 `chain.unusable`、把 `usable_from` 推后、由 `adjust_bars`
    拒绝跨越它的窗口。**不改口径、不夹紧、不静默丢行**（行原样留在 `corp_actions`）。
    """


class MissingFactor(AdjustError):
    """请求的日期没有因子 —— 禁止回退到不复权。"""


class StaleFactorTable(AdjustError):
    """`adj_factor_blackout` 与复权链的真实缺口不一致（库里的可用性记录过期）。

    读取层**宁可拒绝服务**也不放行：缺口记录一旦与链不同源，一段算不出收益的
    历史就会被当成正常数据算进净值 —— 这是「非 NULL 的错误值」能活到下游的原因。
    """


class EtfChainUnsupported(AdjustError):
    """该标的的复权链**无法被证明完整** → 拒绝服务（ADR-008）。

    场内 ETF 会分红除息（实测：510300 的 qfq 与不复权在 2023-06-01 差 7.4%），
    但当前唯一合法数据源（腾讯 `fqkline`）**不返回 ETF 的除权事件行**
    （四只 ETF 在 2000 根 K 线内 0 条事件）。事件不可见 → 枚举不出 `cqr`
    → 连 `adj_factor_blackout`（它需要 `cqr`）都填不出来 → 链的缺口无法表达。

    此时写 `factor = 1.0` 就是**拿未复权价冒充复权价**：跨界收益会凭空多出
    一段假跌幅，且数值看着完全正常。所以本层拒绝服务，把「算不出来」变成
    **显式异常**，而不是一条看着正常的错误序列。
    """


#: 允许进复权链的标的类型 —— **白名单**：不在其中的一律拒绝。
#: 白名单而非黑名单是刻意的：将来新增标的类型（可转债 / 商品 / 外盘 ETF…）
#: 默认落到「拒绝服务」，不会因为没人记得加判断而默认被放行（ADR-008 §后果）。
ADJUSTABLE_TYPES: frozenset[str] = frozenset({"stock"})


def assert_adjustable(conn: sqlite3.Connection, code: str) -> None:
    """`code` 是否可用于复权链；不可用 → 抛 `EtfChainUnsupported`（含修法）。

    **未登记代码同样拒绝**：把「不知道是什么」当成「是股票」，正是最难查的一类
    错误 —— 下游会拿股票口径去算一个它并不了解的标的。
    """
    kind = instrument_type(conn, code)
    if kind in ADJUSTABLE_TYPES:
        return
    raise EtfChainUnsupported(
        f"{code} 的标的口径是 {kind!r}（不在 {sorted(ADJUSTABLE_TYPES)} 中），"
        f"复权链无法被证明完整 —— 拒绝返回复权价（ADR-008）。"
        f"原因：该类型标的的除权除息事件不在当前数据源里（腾讯 fqkline 对 ETF "
        f"返回 0 条事件行，而 qfq 与不复权确实不同 → 分红真实存在、事件不可见）。"
        f"写 factor=1.0 等于拿未复权价冒充复权价，故本层宁可拒绝服务。"
        f"该标的只能用于**估值/展示**（adj_mode='none'，见 portfolio.prices）。"
        f"修法：拿到可信的事件源 → 落 corp_actions → `adj rebuild` → "
        f"把该类型加进 ADJUSTABLE_TYPES。"
    )


@dataclass(frozen=True)
class Terms:
    """每**股**的条款（已从「每 10 股」换算）。"""

    cash: float          # 每股现金分红（元）
    share_ratio: float   # 每股送转比例（10送3股 → 0.3）

    @property
    def is_noop(self) -> bool:
        return self.cash == 0.0 and self.share_ratio == 0.0


def parse_terms(content: str | None, *, code: str = "", cqr: str = "") -> Terms:
    """把源站原文（如 `"10派20元转15股"`）解析成 `Terms`。

    **原文是唯一可信的条款来源。** 探针实测 `fh_sh` 在两处不可信
    （见 ADR-004）：2014/2015 的事件是**税后值**（20→19、10→9.5），
    送转-only 事件直接为空串（600690 有 9/36 条）—— 现有解析器要求
    `fh_sh` 存在，会把这 9 条**静默丢弃**，其中就有 `10送3股`。

    解析不出条款时**抛 `UnpriceableTerms`**，不做任何猜测。
    """
    text = (content or "").strip()
    where = f"{code} {cqr}".strip()
    if not text:
        raise UnpriceableTerms(f"{where}：事件无条款原文（FHcontent 为空），无法计算复权系数")
    if _RE_PEI.search(text):
        raise UnpriceableTerms(
            f"{where}：含配股（{text!r}），配股价不在事件行里，本模块不支持 —— 拒绝猜测"
        )
    cash = _RE_CASH.search(text)
    song = _RE_SONG.search(text)
    zhuan = _RE_ZHUAN.search(text)
    if not (cash or song or zhuan):
        raise UnpriceableTerms(f"{where}：条款原文 {text!r} 中无「派/送/转」可识别条款")
    return Terms(
        cash=(float(cash.group(1)) / _PER) if cash else 0.0,
        share_ratio=((float(song.group(1)) if song else 0.0)
                     + (float(zhuan.group(1)) if zhuan else 0.0)) / _PER,
    )


def event_factor(pre_close: float, terms: Terms) -> float:
    """单个事件的复权系数 `k`。

    标准除权价公式的等价写法（ADR-001 的规则是它在「纯现金」下的特例）：

        k = (pre_close - cash) / (pre_close * (1 + share_ratio))

    纯现金（`share_ratio == 0`）时退化为 ADR-001 的 `1 - 现金/除权前收盘`。
    送转必须进公式：000333 的 `10派20元转15股` 只扣现金会得到 k=0.957，
    正确值约 0.383（探针实测，见 ADR-004 证据表）。

    返回值必须落在 `(0, 1]`。`<= 0`（每股现金 ≥ 除权前收盘）说明**源站条款荒谬**
    ⇒ 抛 `UnpriceableTerms`（`AdjustError` 子类）：该事件判为不可定价、进 `chain.unusable`，
    **不改口径、不夹紧、不静默丢行**。`k > 1`（负现金 ⇒ 解析 bug）与 `pre_close <= 0`
    则仍抛**裸 `AdjustError`** —— 那两条是数据/代码缺陷，必须炸，不许被容错档吃掉。
    """
    if not pre_close or pre_close <= 0:
        raise AdjustError(f"除权前收盘必须为正，得到 {pre_close!r}")
    k = (pre_close - terms.cash) / (pre_close * (1.0 + terms.share_ratio))
    if k <= 0.0:
        raise UnpriceableTerms(
            f"复权系数越界（条款疑似荒谬，按不可定价事件处理）：pre_close={pre_close} "
            f"cash={terms.cash} share_ratio={terms.share_ratio} → k={k}"
        )
    if k > 1.0:
        raise AdjustError(
            f"复权系数越界（k > 1：负现金 ⇒ 解析 bug，必须炸）：pre_close={pre_close} "
            f"cash={terms.cash} share_ratio={terms.share_ratio} → k={k}"
        )
    return k


@dataclass(frozen=True)
class EventFactor:
    """一个**可定价**的事件：除权日 + 系数 + 计算依据（审计用）。"""

    cqr: str
    prev_date: str      # 除权前最后一个交易日
    pre_close: float
    factor: float
    content: str


@dataclass(frozen=True)
class Unusable:
    """一个**无法定价**的事件：不复权序列在它之后的一段会残留假跌幅。"""

    cqr: str
    reason: str


@dataclass(frozen=True)
class Chain:
    """因子链 + 不可定价事件清单。

    `factors[date] = Π_{priced, cqr <= date} k`（**PIT：只累乘 date 及之前的事件**）。

    无法定价的事件其 `k` **完全不进链**，因此它对任何 `factor(...)` 都无贡献
    —— 精确的后果是：**恰好在它除权日那一天，不复权的假跌幅会原样留下**，
    其余日期的收益不受影响（`multiplier(t2)/multiplier(t1)` 只含 `(t1, t2]` 内的事件）。
    `adjust_bars` 据此拒绝跨越这类事件的窗口；`usable_from`
    （`max(不可定价事件的 cqr)`）是给调用方挪窗口用的下界参考值。
    """

    factors: dict[str, float]
    unusable: tuple[Unusable, ...]
    events: tuple[EventFactor, ...]
    usable_from: str | None

    def at(self, date: str) -> float:
        try:
            return self.factors[date]
        except KeyError:
            raise MissingFactor(
                f"{date} 没有复权因子（不在因子链内）—— 拒绝回退到不复权价"
            ) from None


def build_chain(bars, events, *, code: str = "") -> Chain:
    """由**不复权** K 线与事件构建因子链。

    `bars` 需按日期升序（乱序也可以，本函数内部排序）。
    除权前收盘 = `cqr` 之前最后一个交易日的收盘（不复权）。
    """
    ordered = sorted(bars, key=lambda b: b.date)
    dates = [b.date for b in ordered]
    close_by_date = {b.date: b.close for b in ordered}
    if not dates:
        return Chain({}, (), (), None)

    factors: dict[str, float] = {}
    unusable: list[Unusable] = []
    priced: list[EventFactor] = []

    for ev in sorted(events, key=lambda e: e.cqr):
        prev = [d for d in dates if d < ev.cqr]
        try:
            terms = parse_terms(ev.content, code=code, cqr=ev.cqr)
            if not prev:
                raise UnpriceableTerms(
                    f"除权日 {ev.cqr} 早于K线覆盖首日 {dates[0]}，取不到除权前收盘"
                )
            priced.append(EventFactor(
                cqr=ev.cqr, prev_date=prev[-1], pre_close=close_by_date[prev[-1]],
                factor=event_factor(close_by_date[prev[-1]], terms),
                content=ev.content,
            ))
        except UnpriceableTerms as exc:
            unusable.append(Unusable(cqr=ev.cqr, reason=str(exc)))

    # 逐日累乘：对每个日期只乘 cqr <= 该日 的事件（**PIT 的唯一防线**）。
    # 事件按 cqr 升序、日期升序，指针单向前进 → O(n+m)。
    pending = sorted(priced, key=lambda e: e.cqr)
    idx, running = 0, 1.0
    for d in dates:
        while idx < len(pending) and pending[idx].cqr <= d:
            running *= pending[idx].factor
            idx += 1
        factors[d] = running

    # 可用下界参考值：覆盖区间内最晚的那条不可定价事件的 cqr。
    # 注意它只是「把窗口挪到哪儿」的建议，**不是**复权正确性的判据 ——
    # 判据在 `adjust_bars` 里按「窗口 (t_min, base_date] 内有无不可定价事件」逐次检查。
    skipped = [u.cqr for u in unusable if u.cqr <= dates[-1]]
    return Chain(factors=factors, unusable=tuple(unusable),
                 events=tuple(priced),
                 usable_from=max(skipped) if skipped else None)


# ---------- 复权读取层 ----------

def adjust_bars(bars, chain: Chain, as_of: str, *, code: str = "",
                start: str | None = None) -> list[Bar]:
    """把不复权 K 线按 `as_of` 口径复权（前复权，锚定在 `as_of`）。

    `multiplier(t) = factor(as_of) / factor(t)`：
      - `t == as_of` 时 multiplier = 1（最新价即真实价，与行情软件一致）；
      - `t < as_of` 时 multiplier ≤ 1（历史价按比例缩小）；
      - 全程**乘性**，价格恒正（腾讯减法式 qfq 会变负，见 ADR-004）。

    **拒绝跨越「不可定价事件」的窗口**：被略过的事件其 `k` 不在链里，
    于是它在 `factor(...)` 中既不出现于分子也不出现于分母 ——
    后果是**恰好在它的除权日那一天的收益里，假跌幅原样保留**（其余日期不受影响）。
    因此只要窗口 `(t_min, base_date]` 内存在不可定价事件，本函数就报错，
    而不是交出一条「大部分对、某几天错」的序列。要服务该区间的一部分，
    由调用方显式传 `start`（≥ `chain.usable_from`）把窗口挪到事件之后。

    `Bar.adj_mode` 置 `"qfq"`：这是**刻意的**，`repo.insert_bars` 会拒绝写它，
    所以复权价在物理上不可能被误落进 `bars_daily`（铁律①）。
    """
    usable = [b for b in bars if b.date <= as_of and (start is None or b.date >= start)]
    if not usable:
        return []
    base_date = max(b.date for b in usable)
    t_min = min(b.date for b in usable)
    blocking = [u.cqr for u in chain.unusable if t_min < u.cqr <= base_date]
    if blocking:
        raise MissingFactor(
            f"{code} 窗口 [{t_min}, {base_date}] 跨越了无法定价的除权事件 {blocking}"
            f"（见 chain.unusable）—— 该日的假跌幅无法还原。"
            f"请显式传 start >= {max(blocking)}（chain.usable_from）"
        )
    base = chain.factors[base_date]
    out: list[Bar] = []
    for bar in usable:
        m = base / chain.at(bar.date)
        out.append(Bar(
            code=bar.code, date=bar.date,
            open=bar.open * m, high=bar.high * m,
            low=bar.low * m, close=bar.close * m,
            volume=bar.volume,
            # 成交额/换手率是**真实发生**的现金流与股本口径，不做复权（本层不消费它们）
            amount=bar.amount, turnover=bar.turnover,
            source=bar.source, adj_mode="qfq",
        ))
    return out


def load_chain(conn: sqlite3.Connection, code: str) -> tuple[list[Bar], Chain]:
    """读出该标的的**全部**不复权 K 线与因子链。

    刻意读全量（而不是只读 `<= as_of`）：因子链是逐日累乘的结果，
    只按窗口读会让链的计算依赖调用方传了什么窗口。PIT 由 `build_chain`
    的 `cqr <= date` 过滤器保证，**不依赖调用方裁剪**——
    这正是「as-of 语义」唯一可靠的位置（与 Task 19 的教训一致：
    防线要放在结构上，不能指望每个调用点都记得裁）。

    **非股票标的一律拒绝**（`assert_adjustable`，ADR-008）：ETF 的事件源不可见，
    链无法被证明完整。这个判断放在**唯一的读链入口**上 —— 而不是放在每个调用点，
    否则新增一个调用点就多一条静默放行的路。

    **判序：先看有没有行情，再判口径。** 「库里没有这只标的的 K 线」是比
    「标的口径不可复权」**更具体、也更常见**的诊断（写错代码 / 还没采集），
    必须让它先说话。顺序反了的话，一个尚未采集的代码会得到一长串关于
    ETF 事件源的说明 —— 把人引向完全错误的方向（P17 实测：这正是
    `predict run --code 600690` 的既有断言抓到的）。
    """
    bars = [Bar(code=r["code"], date=r["date"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
                amount=r["amount"], turnover=r["turnover"], source=r["source"],
                adj_mode=r["adj_mode"])
            for r in conn.execute(
                "SELECT * FROM bars_daily WHERE code=? ORDER BY date", (code,))]
    if not bars:
        # 没有行情 → 没有链可建（也无可复权之物）。返回空链而不是抛口径错误。
        return bars, Chain({}, (), (), None)
    assert_adjustable(conn, code)
    events = [CorpAction(code=r["code"], cqr=r["cqr"], djr=r["djr"] or "",
                         content=r["content"] or "", fh_sh=r["fh_sh"],
                         source=r["source"])
              for r in conn.execute(
                  "SELECT * FROM corp_actions WHERE code=? ORDER BY cqr", (code,))]
    return bars, build_chain(bars, events, code=code)


def load_bars_adjusted(conn: sqlite3.Connection, code: str, as_of: str, *,
                       start: str | None = None) -> list[Bar]:
    """从库里读 `code` 的复权 K 线（`date <= as_of`，可选起点 `start`）。

    **as-of 语义**：只使用 `cqr <= as_of` 的事件 —— 由因子链的构造方式保证
    （`build_chain` 只累乘 `cqr <= date` 的事件）。传入未来的事件也**不会**
    影响 `as_of` 之前的价格，这一点由 `test_future_event_does_not_affect_past`
    反证（先让测试真红，再修绿）。

    **默认不缩窗口**：`start` 不传时窗口从库中最早一根 K 线起算，
    一旦跨越不可定价事件就抛 `MissingFactor`（缺陷必须浮出来）。
    要主动放弃那段历史，调用方须**显式**传 `start=chain.usable_from` ——
    缩窗口是一个决定，不能由本函数替调用方默默做掉。

    调用前先核对 `adj_factor_blackout` 与链的缺口**同源**：不一致时抛
    `StaleFactorTable`，绝不放行。这样「库里有一堆非 NULL 的错误因子」这件事
    不可能被静默消费（ADR-004 §无法定价事件）。

    `load_chain` 里还会先过 `assert_adjustable`：ETF 之类的非股票标的连链都不构建，
    直接抛 `EtfChainUnsupported`（ADR-008）。
    """
    bars, chain = load_chain(conn, code)
    assert_blackout_current(conn, code, chain)
    return adjust_bars(bars, chain, as_of, code=code, start=start)


def load_blackouts(conn: sqlite3.Connection, code: str) -> dict[str, str]:
    """读该标的的不可用区间：`{除权日: 原因}`。"""
    return {r["cqr"]: r["reason"] for r in conn.execute(
        "SELECT cqr, reason FROM adj_factor_blackout WHERE code=? ORDER BY cqr",
        (code,))}


def usable_from(conn: sqlite3.Connection, code: str) -> str | None:
    """该标的复权链的可用下界（= 最晚一条黑名单事件的除权日）；无缺口返回 None。

    语义：**早于**该日期的行情不可用于收益计算；`>=` 该日期的窗口可用
    （窗口判据见 `adjust_bars`：`t_min < cqr <= base` 才算跨越）。
    """
    row = conn.execute(
        "SELECT MAX(cqr) AS u FROM adj_factor_blackout WHERE code=?", (code,)
    ).fetchone()
    return row["u"] if row and row["u"] else None


def assert_blackout_current(conn: sqlite3.Connection, code: str, chain: Chain) -> None:
    """库里的缺口记录必须与链的真实缺口**完全一致**（不多、不少）。

    少一条 = 一段算不出收益的历史被当成正常数据（静默假收益）；
    多一条 = 记录过期，可能把可用区间误判为不可用。两者都拒绝服务，
    并给出「怎么修」的指令。

    **只在库里存有该标的因子行时才核对**：`adj_factor_blackout` 是
    `adj_factors` 的可用性记录，没有因子行就无所谓「记录过期」；
    而链本身的窗口校验（`adjust_bars`）在任何情况下都生效 ——
    所以这个「跳过」不会放过任何跨缺口的窗口。
    """
    has_factors = conn.execute(
        "SELECT 1 FROM adj_factors WHERE code=? LIMIT 1", (code,)).fetchone()
    if has_factors is None:
        return
    stored = set(load_blackouts(conn, code))
    declared = {u.cqr for u in chain.unusable}
    if stored != declared:
        missing = sorted(declared - stored)
        extra = sorted(stored - declared)
        raise StaleFactorTable(
            f"{code} 的 adj_factor_blackout 与复权链缺口不一致："
            f"链有而库中缺 {missing}，库中有而链没有 {extra}。"
            "在修复前拒绝返回复权价（跨缺口的收益无法计算）。"
            "修复：跑 `stocklab adj rebuild` 由 bars_daily + corp_actions 重算因子链"
        )


def chain_summary(chain: Chain) -> dict:
    """因子链的可读摘要（CLI / 报告用）。"""
    return {
        "n_events": len(chain.events),
        "n_unusable": len(chain.unusable),
        "unusable": [{"cqr": u.cqr, "reason": u.reason} for u in chain.unusable],
        "usable_from": chain.usable_from,
        "first_date": min(chain.factors) if chain.factors else None,
        "last_date": max(chain.factors) if chain.factors else None,
        "first_factor": chain.factors[min(chain.factors)] if chain.factors else None,
        "cumulative_product": prod(e.factor for e in chain.events),
    }
