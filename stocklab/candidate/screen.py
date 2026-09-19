"""步骤3：固定前置通用排雷（设计文档 §7.1）。

## 这是主干，AI 不可改

候选池的其它环节都是插桩（可替换），**唯独本模块写死**。理由（文档 00
全局约束 1）：如果「什么样的标的算暴雷」也能被 AI 改，那 AI 只要把排雷
条件放宽就能让自己的策略看起来更好 —— 那是最容易也最隐蔽的作弊路径。

## ST 判据不是 PIT（已知近似）

判定用 `Instrument.name`，即**当前**名称。历史某日的 ST 状态取不到
（腾讯行情只给当下快照）。所以 `detail` 里必须显式写明这是近似，
报告里也不得把它当作严格的历史事实。

## ETF 不做 ST 判定

ST 是个股的退市风险标记，ETF 没有这个概念。对 ETF 跑名称匹配只会
误伤（比如某些 ETF 简称里恰好含字母组合）。
"""

from __future__ import annotations

from dataclasses import dataclass

from stocklab.config.universe import Instrument
from stocklab.data.models import Bar

#: 最少需要的历史交易日数。
MIN_HISTORY_DAYS: int = 250
#: 连续跌停多少日算暴雷。
MAX_CONSECUTIVE_LIMIT_DOWN: int = 3
#: 停牌检查窗口（最近多少个交易日）。
SUSPEND_WINDOW: int = 20
#: 窗口内允许的无成交日数上限（超过即判停牌）。
SUSPEND_MAX_GAP: int = 5

#: 各板块涨跌停幅度。
LIMIT_PCT: dict[str, float] = {
    "main": 0.10, "gem": 0.20, "star": 0.20, "bse": 0.30,
}

#: 浮点误差余量：9.999% 之类的舍入不该被当成跌停。
_LIMIT_EPS = 0.002


@dataclass(frozen=True)
class ScreenResult:
    passed: bool
    reason: str | None          # 淘汰原因标签（None = 通过）
    detail: str                 # 人话说明（写进 candidate_rejects.reason）


def _limit_pct(inst: Instrument) -> float:
    return LIMIT_PCT.get(inst.board, 0.10)


def _is_limit_down(prev_close: float, close: float, pct: float) -> bool:
    if prev_close <= 0:
        return False
    return (close - prev_close) / prev_close <= -(pct - _LIMIT_EPS)


def screen(inst: Instrument, bars: list[Bar], *, asof: str) -> ScreenResult:
    """对单只标的做前置排雷。`bars` 必须已按 date 升序。

    **PIT**：只使用 `date <= asof` 的行；未来行不算数（调用方即使传进来
    也不参与判定）。
    """
    usable = [b for b in bars if b.date <= asof]
    usable.sort(key=lambda b: b.date)

    if len(usable) < MIN_HISTORY_DAYS:
        return ScreenResult(
            False, "insufficient_history",
            f"有效 K 线仅 {len(usable)} 个交易日，不足 {MIN_HISTORY_DAYS} 日")

    # ST check comes before the limit-down scan intentionally.
    # Rule order is deliberate: when an instrument fails multiple checks, the
    # *first* matching rule supplies the reason tag.  A stock that is both
    # ST-flagged and in a consecutive limit-down streak will be labelled
    # "st_flag", not "consecutive_limit_down".  Do not reorder the checks.
    if inst.asset_type != "etf" and _looks_like_st(inst.name):
        return ScreenResult(
            False, "st_flag",
            f"名称 {inst.name!r} 含 ST 标记（**非 PIT**：取自当前名称字段，"
            "历史某日的 ST 状态无法还原，仅为近似）")

    pct = _limit_pct(inst)
    streak = 0
    for prev, cur in zip(usable, usable[1:]):
        if _is_limit_down(prev.close, cur.close, pct):
            streak += 1
            if streak >= MAX_CONSECUTIVE_LIMIT_DOWN:
                return ScreenResult(
                    False, "consecutive_limit_down",
                    f"连续 {streak} 日跌停（板块 {inst.board}，"
                    f"阈值 {pct:.0%}），截至 {cur.date}")
        else:
            streak = 0

    window = usable[-SUSPEND_WINDOW:]
    gaps = sum(1 for b in window if b.volume <= 0)
    if gaps > SUSPEND_MAX_GAP:
        return ScreenResult(
            False, "suspended",
            f"最近 {len(window)} 个交易日内有 {gaps} 日无成交"
            f"（上限 {SUSPEND_MAX_GAP} 日）")

    return ScreenResult(True, None, "")


def _looks_like_st(name: str) -> bool:
    """`ST` / `*ST` 前缀判定（去掉全角空格与半角空格）。"""
    normalized = name.replace(" ", "").replace("　", "").upper()
    return normalized.startswith("ST") or normalized.startswith("*ST")
