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


def _blocked_getaddrinfo(*args, **kwargs):
    raise AssertionError(_NET_BLOCKED)


# ---------- 回环放行（P15：真起服务用 http.client 打请求） ----------
#
# `no_network` 是 autouse 的，会连回环一起挡掉。P15 要求「**真起服务**（随机端口）
# 用 `http.client` 打请求」，所以需要一个**只放开回环**的窄口：
# 仍然挡掉任何非回环目标（换回 `_NET_BLOCKED`），否则「测试能连上」就又变成了
# 环境巧合（ERROR_DIARY 2026-09-14 的教训：守卫只拦一半 = 没拦）。
#
# 在模块导入时抓真实实现：conftest 的 autouse fixture 是在**测试时**才替换的，
# 导入时拿到的还是标准库原件。
_REAL_SOCKET = socket.socket
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


class _LoopbackOnlySocket(_REAL_SOCKET):
    """能构造、能 bind/listen，但 `connect` 只允许回环。"""

    def connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise AssertionError(_NET_BLOCKED)
        return super().connect(address, *args, **kwargs)

    def connect_ex(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise AssertionError(_NET_BLOCKED)
        return super().connect_ex(address, *args, **kwargs)


def _loopback_create_connection(address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOOPBACK:
        raise AssertionError(_NET_BLOCKED)
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)


def _loopback_getaddrinfo(host, *args, **kwargs):
    if host is not None and str(host) not in _LOOPBACK:
        raise AssertionError(_NET_BLOCKED)
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture
def loopback_http(monkeypatch):
    """**只**放开回环网络：允许本机起服务并用 `http.client` 打它。"""
    monkeypatch.setattr(socket, "socket", _LoopbackOnlySocket)
    monkeypatch.setattr(socket, "create_connection", _loopback_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _loopback_getaddrinfo)
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """全测试套默认离线：DNS 解析与真实连接都直接失败。

    必须**同时**拦住 getaddrinfo：只拦 socket 的话，在 DNS 不可用的环境里
    urllib3 会先在解析阶段抛出 gaierror，永远走不到我们的守卫，
    「守卫有效」的测试就变成了「本机 DNS 恰好可用」的巧合
    （在 `unshare -n` 无网络命名空间下实测暴露）。见 ERROR_DIARY 2026-09-14。

    数据层测试必须走 fixture 回放（`tests/fixtures/`）或注入的假 session，
    不允许依赖网络。需要真实网络的一次性动作放在 `scripts/`（手工运行，非测试）。
    """
    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_getaddrinfo)
