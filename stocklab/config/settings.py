import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from stocklab.config import paths
from stocklab.config.costs import CostModel


@dataclass(frozen=True)
class Settings:
    timezone: str = "Asia/Shanghai"
    backfill_days: int = 1200          # 日K 回补目标长度
    http_timeout: float = 10.0
    http_min_interval: float = 0.35    # 同域请求最小间隔（防限流）
    retry_attempts: int = 4
    retry_base_delay: float = 0.8
    retry_max_delay: float = 8.0
    cache_enabled: bool = True         # 命中 raw_cache 则不联网
    costs: CostModel = field(default_factory=CostModel)


def load_settings(path: Path | None = None) -> Settings:
    """从 TOML 加载配置；文件不存在时返回默认值。"""
    p = path or paths.CONFIG_PATH
    if not p.exists():
        return Settings()
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    flat = {k: v for k, v in raw.items() if not isinstance(v, dict)}
    costs_raw = raw.get("costs", {})
    costs = CostModel(**costs_raw) if costs_raw else CostModel()
    return Settings(**flat, costs=costs)
