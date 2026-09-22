from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse

from stocklab.data.errors import HostNotAllowed

ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "qt.gtimg.cn",
        "web.ifzq.gtimg.cn",
        "push2.eastmoney.com",
        "push2his.eastmoney.com",
        "datacenter-web.eastmoney.com",       # P28：估值（RPT_VALUEANALYSIS_DET）
        "vip.stock.finance.sina.com.cn",      # P28：资金流（MoneyFlow）
        "www.sse.com.cn",                     # P30：休市安排公告（年度通知 + 单节公告）
        "fund.eastmoney.com",                 # P52：基金日净值（pingzhongdata，**非官方**接口）
    }
)


def assert_host_allowed(url: str) -> None:
    """校验 URL 主机名在白名单内，且不是 IP 直连（R13）。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise HostNotAllowed(f"无法解析主机名: {url!r}")
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise HostNotAllowed(f"禁止 IP 直连: {host}")
    if host not in ALLOWED_HOSTS:
        raise HostNotAllowed(f"域名不在白名单: {host}")


#: 标的类型（落 `instruments.type`）。**白名单语义**：不在其中的一律按「口径未知」处理，
#: 由各消费方显式拒绝（见 `data.adjust.ADJUSTABLE_TYPES`）。
ASSET_STOCK = "stock"
ASSET_ETF = "etf"


@dataclass(frozen=True)
class Instrument:
    code: str          # 6 位代码，如 "000333"
    name: str
    market: str        # "sz" | "sh"
    board: str         # "main" | "gem" | "star" | "bse" —— 决定涨跌停幅度
    #: 标的口径（P17）。默认 `stock` 是为了不破坏既有调用点；ETF 必须显式给。
    asset_type: str = ASSET_STOCK
    #: 财报机构类型（Task 5）。决定东财 F10 报表名（G=通用 / B=银行 / I=保险）。
    #: 填错会静默拿不到财报 —— 必须与 seeds.py 的标注一致。默认「通用」是为了
    #: 不破坏既有调用点；银行/保险必须显式给。
    org_type: str = "通用"

    @property
    def secid(self) -> str:
        """东财 secid：1.=沪 0.=深。"""
        return f"{'1' if self.market == 'sh' else '0'}.{self.code}"

    @property
    def tencent_code(self) -> str:
        return f"{self.market}{self.code}"

    @property
    def is_stock(self) -> bool:
        return self.asset_type == ASSET_STOCK

    @property
    def is_etf(self) -> bool:
        return self.asset_type == ASSET_ETF

    @property
    def lot(self) -> int:
        """最小交易单位。个股 1 手 = 100 股；**场内 ETF 1 手 = 100 份**（同为 100）。"""
        return 100


#: ⚠️ **语义变更（2026-09-18，模块1 骨架）**：本清单从「系统标的池」降级为
#: 「**首次建库的种子清单**」。真正的标的池来源是候选池
#: （`stocklab/candidate/seeds.py` 提供 21 只种子，候选池筛选在其上运行）。
#: 保留此处不删是为了不打破既有调用点与已入库数据。
#:
#: 首次建库的种子标的池（P17 起含 ETF）。
#:
#: ETF 的 `board` 一律 `main`：`board` 在本项目里的语义是**涨跌停幅度**
#: （见 `backtest/portfolio.py` 的 `LIMIT_BY_BOARD`），四只 ETF 都是 ±10%，与主板一致。
#: 刻意**不**把 'etf' 塞进 `board` —— 那要改 schema 的 CHECK 约束（= 重建表 = 改写历史），
#: 且新老库会分叉。「是不是 ETF」由 `asset_type` 表达。
DEFAULT_UNIVERSE: tuple[Instrument, ...] = (
    Instrument("000333", "美的集团", "sz", "main"),
    Instrument("600690", "海尔智家", "sh", "main"),
    Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF),
    Instrument("510880", "红利ETF", "sh", "main", ASSET_ETF),
    Instrument("512890", "红利低波ETF", "sh", "main", ASSET_ETF),
    Instrument("518880", "黄金ETF", "sh", "main", ASSET_ETF),
)


def instrument_type(conn, code: str) -> str | None:
    """读 `instruments.type`（标的**口径**的唯一真相来源）。

    未登记 → `None`。**不默认成 `stock`**：把「不知道」当成「是股票」，
    正是本类错误里最难查的一种（下游会拿股票口径去算一个它不了解的标的）。
    """
    row = conn.execute("SELECT type FROM instruments WHERE code = ?",
                       (code,)).fetchone()
    return None if row is None else str(row["type"])
