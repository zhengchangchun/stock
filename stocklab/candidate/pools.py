"""步骤6：候选池分流路由（设计文档 §7.2）。

## ETF 只能进短期池

中期池的定义是「景气 & 财务打分」，长期池是「护城河打分」—— 两者都
需要财报（杜邦、毛利率弹性、ROE、自由现金流）。ETF 是一篮子，**没有
财报**，硬塞进去只会得到一堆 NULL 计算出来的假分数。

## TOPN 写死在代码里

它是候选池的**规模定义**，不是策略参数。放配置文件里会让人以为调它
是「优化」，实际上只会稀释信号密度。

## 同分按 code 升序

排序必须完全确定 —— 否则两次 `candidate run` 会对同分标的排出不同顺序，
报告也就无法逐字节复现（设计文档 §11.2 的可复现性要求）。
"""

from __future__ import annotations

from stocklab.config.universe import Instrument

POOL_SHORT = "short"
POOL_MID = "mid"
POOL_LONG = "long"

ALL_POOLS: tuple[str, ...] = (POOL_SHORT, POOL_MID, POOL_LONG)

#: 各池取前 N（文档 01 §候选池数量建议的上限）。
POOL_TOPN: dict[str, int] = {POOL_SHORT: 6, POOL_MID: 8, POOL_LONG: 5}


def eligible_pools(inst: Instrument) -> tuple[str, ...]:
    """该标的可以进哪些池。ETF 只有短期池。"""
    if inst.asset_type == "etf":
        return (POOL_SHORT,)
    return ALL_POOLS


def select_top(scored: list[dict], pool: str,
               *, topn: int | None = None) -> list[dict]:
    """按 `adj_score` 降序取前 N；同分按 `code` 升序。"""
    if pool not in POOL_TOPN:
        raise ValueError(f"未知池 {pool!r}；已知：{list(ALL_POOLS)}")
    limit = POOL_TOPN[pool] if topn is None else topn
    ordered = sorted(scored, key=lambda r: (-r["adj_score"], r["code"]))
    return ordered[:limit]
