"""证明「测试套离线」这条约束真的被强制执行，而不是靠自觉（验收标准③）。

conftest 里的 autouse fixture 把 socket 换成不能连接的子类；
本文件从**真实调用方**的角度验证它确实拦得住：裸 socket、requests 都出不去。
"""

import socket
import subprocess
import sys
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WHITELISTED_URL = "https://qt.gtimg.cn/q=sz000333"   # 白名单内，被拦只能是因为离线守卫


def test_raw_socket_connect_is_blocked():
    s = socket.socket()
    try:
        with pytest.raises(AssertionError, match="禁止真实联网"):
            s.connect(("1.1.1.1", 80))
    finally:
        s.close()


def test_create_connection_is_blocked():
    with pytest.raises(AssertionError, match="禁止真实联网"):
        socket.create_connection(("1.1.1.1", 80), timeout=1)


def test_guard_does_not_break_ssl_import_in_fresh_interpreter():
    """守卫必须能在「ssl 尚未导入」的新解释器里生效。

    若把 `socket.socket` 换成函数，`ssl` 在导入时执行 `class SSLSocket(socket)`
    会直接 TypeError（本仓库真的踩过）。当前进程里 ssl 早已被 requests 导入，
    观察不到该缺陷，故必须开子进程复现。
    """
    code = (
        "import socket;"
        "from tests.conftest import _BlockedSocket;"
        "socket.socket = _BlockedSocket;"     # 与 autouse 守卫同样的替换方式
        "import ssl;"                         # 首次导入 ssl
        "assert issubclass(ssl.SSLSocket, _BlockedSocket);"
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_requests_cannot_reach_network():
    """requests 走完整链路也出不去（异常类型由 urllib3 包装，只要求「必然失败」）。"""
    with pytest.raises(Exception) as exc:
        requests.get(WHITELISTED_URL, timeout=5)
    assert "禁止真实联网" in repr(exc.value) + repr(exc.value.__cause__)
