class StocklabError(Exception):
    """项目所有异常的基类。"""


class FetchError(StocklabError):
    """抓取失败（网络、超时、非 200）。"""


class HostNotAllowed(FetchError):
    """目标域名不在白名单内（含 IP 直连 / 数字子域）。"""


class RateLimited(FetchError):
    """被数据源限流（空响应 / 429 / 频繁失败）。"""


class DataQualityError(StocklabError):
    """数据未通过质量校验。"""


class CacheCorrupt(StocklabError):
    """原始响应缓存与记录的 SHA256 不一致（磁盘损坏 / 被手改）。"""
