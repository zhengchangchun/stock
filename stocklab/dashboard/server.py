"""回环守卫 —— 合并服务后本模块**只剩这一件事**（2026-09-21）。

## 为什么只剩一个函数

P14 在这里实现过一个**只读看板服务**（`route()` / `make_server()` / `/health` /
`/api/summary`），默认端口 `8791` —— 与 `lab serve`（P15 应用）**撞同一个默认端口**。
于是「本机现在跑的是哪一个网页服务」变成了一个只能靠记忆回答的问题，
而两个服务里有一个是只读快照、另一个带写路径，读错一次就是读错一次。

2026-09-21 用户拍板：**只保留一个服务**，`lab serve`（页面更全、且写路径已带
CSRF/幂等纪律）。看板不再有服务入口，只保留**离线单文件产物**
（`dashboard build` → 一个 HTML，可双击、可转发）。

`assert_loopback` 留在本模块而不是搬进 `labweb`：它是 **labweb 也在复用的那一份**
（口径只跟函数走、不跟包走）。搬过去等于制造一次无意义的迁移，
留在原处则「只有一份白名单」这句话继续成立。

## 只绑回环

绑 `0.0.0.0` 等于把本机账本暴露到局域网，所以它是**先于 bind 的显式检查**：
失败时一个 socket 都没建。对外访问走 nginx 反代。
"""

from __future__ import annotations

#: 允许绑定的 host。**只有回环**。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class NonLoopbackHost(ValueError):
    """host 不是回环地址 —— 拒绝（不是警告）。"""


def assert_loopback(host: str) -> str:
    """不是回环就抛 `NonLoopbackHost`。返回原值便于链式使用。"""
    if host not in LOOPBACK_HOSTS:
        raise NonLoopbackHost(
            f"--host {host!r} 不是回环地址。Web 服务**只允许**绑定回环"
            f"（{'/'.join(sorted(LOOPBACK_HOSTS))}）：绑 0.0.0.0 等于把本机账本"
            f"暴露到局域网。对外访问请走 nginx 反代。")
    return host


__all__ = ["LOOPBACK_HOSTS", "NonLoopbackHost", "assert_loopback"]
