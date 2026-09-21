"""回环守卫（P14 遗留）；**网页服务已合并**（2026-09-21）。

P14 在这里实现过一个只读看板服务（`route()` / `make_server()` / `/health` /
`/api/summary`），默认端口 `8791` —— 与 `lab serve` 撞端口。2026-09-21 用户拍板
**只保留一个服务**：网页入口只剩 `lab serve`（页面更全、且带写路径纪律），
看板只保留离线单文件产物（`dashboard build`）。

本文件因此只钉两件事：

1. 回环白名单本身（两条参数化用例）；
2. **本模块不许再长出一个服务** —— 公开面就是那三个名字。多一个名字，
   就说明「只保留一个服务」这条决定被无声地回退了。
"""

from __future__ import annotations

import pytest

from stocklab.dashboard import server as dash

#: 合并后允许存在的公开名字。
ALLOWED = {"LOOPBACK_HOSTS", "NonLoopbackHost", "assert_loopback"}

#: 曾经存在、已被删除的服务层名字（谁加回来谁负责解释）。
BANNED = ("make_server", "route", "serve_forever", "bound_url", "summary_health",
          "make_handler", "Context", "Response", "PAGE_PATHS", "DEFAULT_PORT")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "10.0.0.1",
                                  "127.0.0.2", "example.com", ""])
def test_non_loopback_host_is_rejected(host):
    with pytest.raises(dash.NonLoopbackHost) as exc:
        dash.assert_loopback(host)
    assert "回环" in str(exc.value)


@pytest.mark.parametrize("host", sorted(dash.LOOPBACK_HOSTS))
def test_loopback_host_is_accepted(host):
    assert dash.assert_loopback(host) == host


def test_module_public_surface_is_just_the_guard():
    """「一个服务」这条决定的可执行版本。"""
    assert set(dash.__all__) == ALLOWED
    for banned in BANNED:
        assert not hasattr(dash, banned), (
            f"{banned} 又回来了：网页服务只允许 `lab serve` 一个"
            f"（2026-09-21 合并，见 docs/decisions/ADR-016）")


def test_labweb_reuses_this_guard():
    """白名单只有一份：labweb 直接用本模块的 `assert_loopback`（不重写、不复制）。"""
    from stocklab.labweb import app as labweb

    assert labweb.assert_loopback is dash.assert_loopback
    assert labweb.LOOPBACK_HOSTS == dash.LOOPBACK_HOSTS
    assert labweb.NonLoopbackHost is dash.NonLoopbackHost
