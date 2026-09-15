"""看板服务（P14 / Task 62）：stdlib `ThreadingHTTPServer`，**只读**、**只绑回环**。

## 为什么 host 要在**代码层**拒绝

绑定 `0.0.0.0` 会把「我本机的持仓、账本、成本」暴露给整个局域网。部署约定是
nginx 反代（设计文档 §2.6），后端只许绑 `127.0.0.1`。这条不能靠「记得别写错」——
它是一个**先于 bind 的显式检查**，且有单测钉住（`--host 0.0.0.0` 必须报错退出）。

回环白名单只放 `127.0.0.1` / `::1` / `localhost`。**不放** `0.0.0.0`、`::`、
`127.0.0.2`（后者虽在 127/8 内，但「回环」的常见写法就那三个，多放一个就多一个
「以为绑的是本机、实际不是」的机会）。

## 只读

路由表里没有任何写入口：非 GET/HEAD 一律 405。页面每次请求**重算**摘要，
不落任何文件、不写库。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from stocklab.dashboard.html import render_html
from stocklab.dashboard.summary import SCHEMA_VERSION, SERVICE, summary_json

TZ = ZoneInfo("Asia/Shanghai")

#: 允许绑定的 host。**只有回环**。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: 页面路由（`/` 与 `/lab/` 是同一份页面；后者便于挂在 nginx 子路径下）。
PAGE_PATHS = frozenset({"/", "/lab", "/lab/"})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791


class NonLoopbackHost(ValueError):
    """host 不是回环地址 —— 拒绝（不是警告）。"""


def assert_loopback(host: str) -> str:
    """不是回环就抛 `NonLoopbackHost`。返回原值便于链式使用。"""
    if host not in LOOPBACK_HOSTS:
        raise NonLoopbackHost(
            f"--host {host!r} 不是回环地址。看板服务**只允许**绑定回环"
            f"（{'/'.join(sorted(LOOPBACK_HOSTS))}）：绑 0.0.0.0 等于把本机持仓账本"
            f"暴露到局域网。对外访问请走 nginx 反代。")
    return host


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = field(default=())


@dataclass(frozen=True)
class Context:
    """路由需要的两个**纯取数**回调（测试可替换，不必起库）。"""

    summary_provider: Callable[[], Mapping]
    health_provider: Callable[[], Mapping]
    db_path: Path | None = None


def _json_response(payload: Mapping, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    return Response(status, "application/json; charset=utf-8", body)


def route(path: str, ctx: Context, *, now: str | None = None) -> Response:
    """`path` → `Response`。**纯函数**（除调用 ctx 的两个取数回调外不做别的）。

    路由：`/` 与 `/lab/`（同一页面）、`/health`、`/api/summary`，其余 404。
    """
    target = urlsplit(path).path  # 丢掉 query，但**不**做任何解码/重写
    if target in PAGE_PATHS:
        summary = ctx.summary_provider()
        stamp = now or datetime.now(TZ).isoformat(timespec="seconds")
        doc = render_html(summary, built_at=stamp)
        return Response(200, "text/html; charset=utf-8", doc.encode("utf-8"))
    if target == "/health":
        return _json_response(ctx.health_provider())
    if target == "/api/summary":
        return _json_response(ctx.summary_provider())
    return _json_response({"error": "not found", "path": target,
                           "routes": sorted(PAGE_PATHS | {"/health", "/api/summary"})},
                          status=404)


def make_handler() -> type[BaseHTTPRequestHandler]:
    """请求处理器：把 HTTP 层压到最薄，逻辑都在 `route()` 里（可单测）。"""

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "stocklab-dashboard/1.0"
        protocol_version = "HTTP/1.1"

        def do_GET(self):    # noqa: N802（stdlib 命名）
            self._respond(with_body=True)

        def do_HEAD(self):   # noqa: N802（`curl -I` 走这里；必须与 GET 同状态/同头）
            self._respond(with_body=False)

        def do_POST(self):   # noqa: N802
            self._method_not_allowed()

        def do_PUT(self):    # noqa: N802
            self._method_not_allowed()

        def do_DELETE(self):  # noqa: N802
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            resp = _json_response({"error": "read-only service",
                                   "method": self.command}, status=405)
            resp = Response(resp.status, resp.content_type, resp.body,
                            (("Allow", "GET, HEAD"),))
            self._write(resp, with_body=True)

        def _respond(self, *, with_body: bool) -> None:
            resp = route(self.path, self.server.ctx)
            self._write(resp, with_body=with_body)

        def _write(self, resp: Response, *, with_body: bool) -> None:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            for key, value in resp.headers:
                self.send_header(key, value)
            self.end_headers()
            if with_body:
                self.wfile.write(resp.body)

        def log_message(self, fmt, *args):   # noqa: A003（stdlib 命名）
            sys.stderr.write("[dashboard] %s %s\n" % (self.address_string(), fmt % args))

    return DashboardHandler


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                ctx: Context | None = None, *,
                summary_provider: Callable[[], Mapping] | None = None,
                health_provider: Callable[[], Mapping] | None = None,
                db_path: Path | None = None) -> ThreadingHTTPServer:
    """建服务。**先检查 host**，再 bind —— 检查失败时一个 socket 都没建。

    可以只给 `summary_provider`（`health_provider` 缺省时只报服务自身状态）。
    """
    assert_loopback(host)
    if ctx is None:
        if summary_provider is None:
            raise ValueError("必须给 ctx 或 summary_provider")
        if health_provider is None:
            health_provider = summary_health(summary_provider, db_path)
        ctx = Context(summary_provider=summary_provider,
                      health_provider=health_provider, db_path=db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler())
    httpd.daemon_threads = True
    httpd.ctx = ctx          # type: ignore[attr-defined]
    return httpd


def summary_health(summary_provider: Callable[[], Mapping],
                   db_path: Path | None) -> Callable[[], Mapping]:
    """健康检查：报「服务活着」+「库在不在」+「摘要能不能算出来」。

    **故意把摘要能否算出纳入健康判定**：一个只回 `{"status":"ok"}` 的健康检查
    在库损坏时照样是绿的，那种绿是有害的。
    """
    def health() -> Mapping:
        out: dict = {
            "status": "ok",
            "service": SERVICE,
            "schema_version": SCHEMA_VERSION,
            "server_time": datetime.now(TZ).isoformat(timespec="seconds"),
        }
        if db_path is not None:
            out["db"] = str(db_path)
            out["db_exists"] = Path(db_path).exists()
        try:
            summary = summary_provider()
            out["asof"] = summary.get("asof")
            out["bars_latest_date"] = summary["freshness"]["bars_latest_date"]
            out["alarms"] = len(summary.get("alarms") or [])
        except Exception as exc:                     # noqa: BLE001（健康检查必须兜住）
            out["status"] = "degraded"
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    return health


def serve_forever(httpd: ThreadingHTTPServer) -> None:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def bound_url(httpd: ThreadingHTTPServer) -> str:
    """服务实际绑定的地址（`port=0` 时端口由内核分配，必须读回来）。"""
    host, port = httpd.server_address[0], httpd.server_address[1]
    return f"http://{host}:{port}/lab/"


__all__ = ["Context", "DEFAULT_HOST", "DEFAULT_PORT", "LOOPBACK_HOSTS",
           "NonLoopbackHost", "PAGE_PATHS", "Response", "assert_loopback",
           "bound_url", "make_server", "route", "serve_forever", "summary_health",
           "summary_json"]
