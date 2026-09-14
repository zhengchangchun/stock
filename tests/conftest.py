import socket
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_db(tmp_path):
    """独立的临时 SQLite 数据库路径。"""
    return tmp_path / "test.db"


def _no_network(*args, **kwargs):
    raise AssertionError(
        "测试禁止真实联网（可复现性铁律②）：请改用 fixture / raw_fetch_cache 回放"
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """全测试套默认离线：任何真实 socket 都直接失败。

    数据层测试必须走 fixture 回放（`tests/fixtures/`）或注入的假 session，
    不允许依赖网络。需要真实网络的一次性动作放在 `scripts/`（手工运行，非测试）。
    """
    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
