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


@dataclass(frozen=True)
class Instrument:
    code: str          # 6 位代码，如 "000333"
    name: str
    market: str        # "sz" | "sh"
    board: str         # "main" | "gem" | "star" | "bse" —— 决定涨跌停幅度

    @property
    def secid(self) -> str:
        """东财 secid：1.=沪 0.=深。"""
        return f"{'1' if self.market == 'sh' else '0'}.{self.code}"

    @property
    def tencent_code(self) -> str:
        return f"{self.market}{self.code}"


DEFAULT_UNIVERSE: tuple[Instrument, ...] = (
    Instrument("000333", "美的集团", "sz", "main"),
    Instrument("600690", "海尔智家", "sh", "main"),
)
