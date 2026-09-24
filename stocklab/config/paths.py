from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "stocklab.db"
BACKUP_DIR = DATA_DIR / "backups"
RAW_CACHE_DIR = DATA_DIR / "raw_cache"
REPORT_DIR = PROJECT_ROOT / "reports"
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
CONFIG_PATH = PROJECT_ROOT / "config.toml"
#: 宇宙**真源**目录（ADR-026 / D3＝C：repo 文件＝真源、`universe_memberships` 表＝投影）。
#: 目录与文件都进 git —— 「什么时候写进去的」只能由 `git log` 回答。
UNIVERSE_DIR = PROJECT_ROOT / "config" / "universes"

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "store" / "schema.sql"


def ensure_dirs() -> None:
    """创建所有运行时目录（幂等）。"""
    for d in (DATA_DIR, BACKUP_DIR, RAW_CACHE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
