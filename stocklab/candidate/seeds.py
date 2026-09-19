"""种子标的清单（设计文档 §2 D7）：16 只个股 + 4 只 ETF = 20 只，跨 10 个行业。

## 为什么不是「全市场」

文档 07 要求「全标的扫描」，但那需要全市场财报与行业分类的采集路径，
本轮（骨架）没有 —— 见设计文档 §3「不做」与 §13「已知限制 6」。
20 只种子是为了让**三池分流与行业排雷这两条主干逻辑真的被跑到**：

- 中期池/长期池需要财报 → 4 只 ETF 进不去，必须有够多的**个股**；
- 插桩0 是「**行业特殊**排雷」→ 标的全在一个行业就测不出东西。

## 为什么至少一只创业板

`300750` 是 `board='gem'`（±20% 涨跌停），用来测
`backtest/portfolio.py` 的 `LIMIT_BY_BOARD` 分支。全主板的话那条分支
永远不会被走到。

## 代码来源

全部在 2026-09-18 实测过腾讯行情接口可返回真实数据
（`qt.gtimg.cn/q=...`），不是拍脑袋写的。
"""

from __future__ import annotations

from stocklab.config.universe import ASSET_ETF, ASSET_STOCK, Instrument

#: 20 只种子。**顺序即代码升序**，保证报告与快照的稳定排序。
SEED_UNIVERSE: tuple[Instrument, ...] = (
    # 家电（5）
    Instrument("000333", "美的集团", "sz", "main", ASSET_STOCK),
    Instrument("000651", "格力电器", "sz", "main", ASSET_STOCK),
    Instrument("002032", "苏泊尔", "sz", "main", ASSET_STOCK),
    Instrument("002508", "老板电器", "sz", "main", ASSET_STOCK),
    Instrument("600690", "海尔智家", "sh", "main", ASSET_STOCK),
    # 银行 / 保险 / 通信（4）
    Instrument("600036", "招商银行", "sh", "main", ASSET_STOCK),
    Instrument("600941", "中国移动", "sh", "main", ASSET_STOCK),
    Instrument("601318", "中国平安", "sh", "main", ASSET_STOCK),
    Instrument("601398", "工商银行", "sh", "main", ASSET_STOCK),
    # 能源 / 公用（3）
    Instrument("600028", "中国石化", "sh", "main", ASSET_STOCK),
    Instrument("600900", "长江电力", "sh", "main", ASSET_STOCK),
    Instrument("601088", "中国神华", "sh", "main", ASSET_STOCK),
    # 食品饮料（2）
    Instrument("000858", "五粮液", "sz", "main", ASSET_STOCK),
    Instrument("600519", "贵州茅台", "sh", "main", ASSET_STOCK),
    # 科技制造（2）—— 300750 是唯一的创业板，用于涨跌停分支
    Instrument("002415", "海康威视", "sz", "main", ASSET_STOCK),
    Instrument("300750", "宁德时代", "sz", "gem", ASSET_STOCK),
    # ETF（4）—— 与 paper.config.ETF_WHITELIST 保持一致
    Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF),
    Instrument("510880", "红利ETF", "sh", "main", ASSET_ETF),
    Instrument("512890", "红利低波ETF", "sh", "main", ASSET_ETF),
    Instrument("518880", "黄金ETF", "sh", "main", ASSET_ETF),
)

SEED_CODES: tuple[str, ...] = tuple(i.code for i in SEED_UNIVERSE)
