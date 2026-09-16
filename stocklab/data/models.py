"""领域对象。单位在类型层固定为「股 / 元」（评审 C3 / 铁律④）。

适配器（`sources/`）负责把数据源的「手 / 万元 / 亿元」换算到这里的单位；
出了适配器就不该再出现别的单位。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    """一根日K。`adj_mode` 标明这行价格的复权口径。

    铁律①：落 `bars_raw` 的必须是 `adj_mode == "none"`（不复权）；
    复权因子由消费方按 `cqr <= T` 自行累乘（ADR-001 D-01）。
    """

    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int            # 股
    amount: float | None   # 元
    turnover: float | None # 换手率 %
    source: str
    adj_mode: str = "none"


@dataclass(frozen=True)
class Quote:
    """实时快照（某时刻的截面，不是历史序列）。"""

    code: str
    name: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: int            # 股
    amount: float          # 元
    turnover: float | None # %
    pe_ttm: float | None
    float_mv: float | None # 元
    total_mv: float | None # 元
    pb: float | None
    ts: str                # 数据源时间戳 YYYYMMDDHHMMSS


@dataclass(frozen=True)
class CorpAction:
    """除权除息事件（ADR-001 D-01 的自建因子链输入）。

    `cqr` 是除权日 —— 消费方只允许使用 `cqr <= T` 的事件（point-in-time）。

    `fh_sh`（每 10 股派息，元）**可为 None，且不可作为条款真源**：探针实测
    它有两处不可信（ADR-004）—— 2014/2015 的事件是**税后值**（20→19、10→9.5），
    送转-only 事件直接是空串。条款一律以 `content` 原文解析，
    `fh_sh` 只用于交叉校验并留痕（见 `adjust.parse_terms`）。
    """

    code: str
    cqr: str                     # 除权日
    djr: str                     # 股权登记日
    content: str                 # 源站原文，如 "10派20元转15股"（条款真源）
    fh_sh: float | None = None   # 每 10 股派息（元）；不可信，仅交叉校验
    source: str = "tencent"


@dataclass(frozen=True)
class ValuationDaily:
    """一行估值（东财 datacenter RPT_VALUEANALYSIS_DET）。

    单位：`total_mv`/`close_price` = 元，`total_shares` = 股，`change_rate` = %。
    **非 PIT 风险**：东财按最新股本重算整条历史 PE/PB（P28 计划 §4）—— 对策是
    落库侧「首写保留」，重采值变化只留痕不覆盖。
    """

    code: str
    date: str                    # TRADE_DATE 已裁剪为 YYYY-MM-DD
    pe_ttm: float | None = None
    pb: float | None = None      # PB_MRQ
    ps_ttm: float | None = None
    total_mv: float | None = None
    total_shares: float | None = None
    close_price: float | None = None
    change_rate: float | None = None
    source: str = "eastmoney-datacenter"


@dataclass(frozen=True)
class MoneyFlowDaily:
    """一行资金流（新浪 MoneyFlow.ssl_qsfx_zjlrqs）。

    单位：`close` = 元，`change_ratio` = **小数**（非 %），`turnover` = **百分数值 ×100**，
    `main_net`/`xl_net`/`ratio_amount` = 元（净额，可负；单位**未独立确证**，按源原值存）。

    ⚠️ `turnover` 与 `bars_daily.turnover`（%）的关系（P28b 实测，见
    `docs/diagnostics/2026-09-17-p28-实测证据.md` §e）：
      - **同口径确证**：两源都是「流通股换手率」，单位 %。独立反推（用腾讯
        `float_mv`/`total_mv`/`amount` 自算）两源同时吻合流通股分母、同时不符总股本分母。
      - **量纲差 100 确证**：新浪存的是百分数值 ×100。
      - **精确倍数未确证**：实测比值 97.15–101.08（n=6，仅 3 个交易日两侧有值），
        不是精确 100；残余 1–3% 未归因。**消费方不得按「/100 即可对齐」处理。**
    """

    code: str
    date: str                    # opendate（新浪原生 YYYY-MM-DD）
    close: float | None = None   # trade
    change_ratio: float | None = None
    turnover: float | None = None
    main_net: float | None = None   # netamount 主力净额
    xl_net: float | None = None     # r0_net 超大单净额
    ratio_amount: float | None = None
    source: str = "sina"
