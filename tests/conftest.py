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


_NET_BLOCKED = (
    "测试禁止真实联网（可复现性铁律②）：请改用 fixture / raw_fetch_cache 回放"
)


class _BlockedSocket(socket.socket):
    """把 socket 换成「能构造、不能连接」的子类。

    不能直接把 `socket.socket` 替换成函数：`ssl` 模块在导入时执行
    `class SSLSocket(socket)`，函数不能作为基类 → `TypeError: function()
    argument 'code' must be code, not str`。用子类则既可被继承，
    又在 `connect` 时失败（见 ERROR_DIARY 2026-09-14）。
    """

    def connect(self, *args, **kwargs):
        raise AssertionError(_NET_BLOCKED)

    def connect_ex(self, *args, **kwargs):
        raise AssertionError(_NET_BLOCKED)


def _blocked_create_connection(*args, **kwargs):
    raise AssertionError(_NET_BLOCKED)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """全测试套默认离线：任何真实 socket 连接都直接失败。

    数据层测试必须走 fixture 回放（`tests/fixtures/`）或注入的假 session，
    不允许依赖网络。需要真实网络的一次性动作放在 `scripts/`（手工运行，非测试）。
    """
    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)
