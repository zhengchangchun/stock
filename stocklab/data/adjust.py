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
    """事件的条款解析不出来（无原文 / 原文里没有可识别条款 / 含配股）。"""


class MissingFactor(AdjustError):
    """请求的日期没有因子 —— 禁止回退到不复权。"""


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

    返回值必须落在 `(0, 1]`：`<= 0` 说明输入荒谬（不改口径、不夹紧，直接报错）。
    """
    if not pre_close or pre_close <= 0:
        raise AdjustError(f"除权前收盘必须为正，得到 {pre_close!r}")
    k = (pre_close - terms.cash) / (pre_close * (1.0 + terms.share_ratio))
    if not (0.0 < k <= 1.0):
        raise AdjustError(
            f"复权系数越界：pre_close={pre_close} cash={terms.cash} "
            f"share_ratio={terms.share_ratio} → k={k}"
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
    """
    bars = [Bar(code=r["code"], date=r["date"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
                amount=r["amount"], turnover=r["turnover"], source=r["source"],
                adj_mode=r["adj_mode"])
            for r in conn.execute(
                "SELECT * FROM bars_daily WHERE code=? ORDER BY date", (code,))]
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
    """
    bars, chain = load_chain(conn, code)
    return adjust_bars(bars, chain, as_of, code=code, start=start)


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
