from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "stocklab.db"
BACKUP_DIR = DATA_DIR / "backups"
RAW_CACHE_DIR = DATA_DIR / "raw_cache"
REPORT_DIR = PROJECT_ROOT / "reports"
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
CONFIG_PATH = PROJECT_ROOT / "config.toml"

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "store" / "schema.sql"


def ensure_dirs() -> None:
    """创建所有运行时目录（幂等）。"""
    for d in (DATA_DIR, BACKUP_DIR, RAW_CACHE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
