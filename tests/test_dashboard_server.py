"""P14 / Task 62：看板服务 —— 只绑回环 + 只读 + 路由稳定。

**最关键的一条**：`--host 0.0.0.0` 必须在**代码层**被拒绝（先于 bind）。
靠「记得别写错」保护本机账本不算保护。
"""

from __future__ import annotations

import io
import json

import pytest

from stocklab.dashboard.server import (LOOPBACK_HOSTS, Context, NonLoopbackHost,
                                       assert_loopback, bound_url, make_handler,
                                       make_server, route)

SUMMARY = {
    "schema_version": 1,
    "service": "stocklab-dashboard",
    "asof": "2026-09-14",
    "portfolio": {
        "asof": "2026-09-14", "cash": 11314.91, "total_assets": 19994.91,
        "cash_breakdown": {"net_deposits": 20000.0, "trade_cash": -8685.09,
                           "other_cash": 0.0},
        "market_value_priced": 8680.0, "market_value_missing": 0.0,
        "net_invested": 20000.0, "total_pnl_incl_fee": -5.09,
        "total_return_incl_fee": -0.000255,
        "positions": [{"code": "000333", "name": "美的集团", "qty": 100,
                       "avg_cost_incl_fee": 86.8509, "avg_cost_excl_fee": 86.8,
                       "price": 86.8, "price_source": "bars_daily",
                       "price_asof": "2026-09-14", "market_value": 8680.0,
                       "float_pnl_incl_fee": -5.09,
                       "weight_of_total_assets": 43.41,
                       "status": "ok"}],
        "missing_price_codes": [],
        "discipline": [{"check": "single_position_max_40pct", "subject": "000333",
                        "status": "FAIL", "detail": "超 40% 上限", "numbers": {}}],
        "advisory": [], "warnings": ["[FAIL] 超 40% 上限"],
        "ledger": {"trades": 1, "cash_flows": 1},
        "price_policy": "同日快照 → bars_daily 收盘 → missing_price",
        "cost_policy": "成本默认含费",
    },
    "freshness": {
        "bars_latest_date": "2026-09-14", "bars_latest_by_code": {"000333": "2026-09-14"},
        "codes_without_bars": [],
        "snapshot_latest": {"ts": "20260915150500", "trade_date": "2026-09-15"},
        "snapshots_on_asof": {"trade_date": "2026-09-14", "n_rows": 0, "n_codes": 0,
                              "n_slots": 0, "slots": [], "per_code": {},
                              "codes_expected": [], "codes_no_snapshot": []},
    },
    "accuracy": {
        "window": {"start": "2026-08-04", "end": "2026-09-14", "n_sessions": 30},
        "provenance": {"rule": "「实盘(LIVE)」⟺ created_at 日期 == asof_date",
                       "live": {"n_rows": 0}, "replay": {"n_rows": 30}},
        "live": None,
        "replay": {"n_rows": 30, "n_scorable": 30, "n_unscorable": 0,
                   "effective_n_days": 30, "direction_accuracy_daily": 0.3,
                   "direction_ci95": [0.15, 0.45], "brier_daily": 0.68,
                   "brier_ci95": [0.65, 0.72],
                   "baselines_daily": {"always_up": 0.3},
                   "sample_gate": {"min_days": 120, "meets": False,
                                   "label": "样本不足，仅供观察"}},
        "note": "口径分列",
    },
    "risk": None,
    "alarms": ["[FAIL] 超 40% 上限"],
}


def _ctx() -> Context:
    return Context(summary_provider=lambda: SUMMARY,
                   health_provider=lambda: {"status": "ok", "service": "s"})


class _KeepOpenIO(io.BytesIO):
    """`close()` 不真的关掉 —— `StreamRequestHandler.finish()` 会 close wfile，
    真关了就再也读不出响应体（测试里要断言响应）。"""

    def close(self):  # noqa: D102
        pass


class _FakeConn:
    """够 `StreamRequestHandler` 用的一根假连接。

    `BaseHTTPRequestHandler` 的 `wbufsize = 0` → stdlib 用 `_SocketWriter` 包住
    `connection`，写响应最终落到 `sendall`（**不是** `makefile('wb')`）。
    所以两个出口都指向同一个缓冲区，否则读不到响应体。
    """

    def __init__(self, raw: bytes):
        self.rfile = _KeepOpenIO(raw)
        self.wfile = _KeepOpenIO()

    def makefile(self, mode, *a, **k):
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data):
        self.wfile.write(data)

    def settimeout(self, *a):
        pass

    def setsockopt(self, *a):
        pass

    def getvalue(self) -> bytes:
        return self.wfile.getvalue()


def _request(method: str, path: str) -> tuple[int, dict, bytes]:
    """把一次真实 HTTP 请求喂给处理器，返回 `(状态码, 头, 体)`。"""

    class _Server:
        ctx = _ctx()

    raw = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
    sock = _FakeConn(raw)
    make_handler()(sock, ("127.0.0.1", 12345), _Server())
    out = sock.getvalue()
    head, _, body = out.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


# ---------- host 白名单 ----------

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "10.0.0.1",
                                  "127.0.0.2", "example.com", ""])
def test_non_loopback_host_is_rejected(host):
    with pytest.raises(NonLoopbackHost) as exc:
        assert_loopback(host)
    assert "回环" in str(exc.value)


@pytest.mark.parametrize("host", sorted(LOOPBACK_HOSTS))
def test_loopback_host_is_accepted(host):
    assert assert_loopback(host) == host


def test_make_server_refuses_0_0_0_0_before_binding():
    """拒绝发生在 **bind 之前** —— 失败时不该留下任何监听端口。"""
    with pytest.raises(NonLoopbackHost):
        make_server("0.0.0.0", 0, _ctx())


def test_make_server_binds_loopback_and_reports_url():
    httpd = make_server("127.0.0.1", 0, _ctx())
    try:
        assert httpd.server_address[0] == "127.0.0.1"
        assert httpd.server_address[1] > 0            # 内核分配的端口
        assert bound_url(httpd).startswith("http://127.0.0.1:")
        assert bound_url(httpd).endswith("/lab/")
    finally:
        httpd.server_close()


# ---------- 路由（纯函数层） ----------

@pytest.mark.parametrize("path", ["/", "/lab", "/lab/"])
def test_page_routes_serve_the_same_html(path):
    resp = route(path, _ctx(), now="2026-09-15T16:00:00+08:00")
    assert resp.status == 200
    assert resp.content_type.startswith("text/html")
    assert b"stock-lab \xe7\x9c\x8b\xe6\x9d\xbf" in resp.body   # 「stock-lab 看板」


def test_health_is_json():
    resp = route("/health", _ctx())
    assert resp.status == 200
    assert resp.content_type.startswith("application/json")
    assert json.loads(resp.body)["status"] == "ok"


def test_api_summary_is_the_stable_json():
    resp = route("/api/summary", _ctx())
    payload = json.loads(resp.body)
    assert payload["asof"] == "2026-09-14"
    assert payload["accuracy"]["live"] is None      # null 不许被写成 0


def test_query_string_is_ignored_for_routing():
    assert route("/api/summary?x=1", _ctx()).status == 200
    assert route("/lab/?t=1", _ctx()).status == 200


def test_unknown_path_is_404_json():
    resp = route("/etc/passwd", _ctx())
    assert resp.status == 404
    assert json.loads(resp.body)["error"] == "not found"


# ---------- 路由（真实 HTTP 层，含 HEAD） ----------

def test_get_lab_returns_200_html():
    status, headers, body = _request("GET", "/lab/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert int(headers["content-length"]) == len(body)
    assert b"<svg" in body


def test_head_lab_has_same_status_and_no_body():
    """`curl -I` 走 HEAD：状态与头必须与 GET 一致，但**不发正文**。"""
    get_status, get_headers, get_body = _request("GET", "/lab/")
    status, headers, body = _request("HEAD", "/lab/")
    assert status == get_status == 200
    assert headers["content-type"] == get_headers["content-type"]
    assert int(headers["content-length"]) == len(get_body)   # 长度仍是 GET 的长度
    assert body == b""


def test_health_over_http():
    status, headers, body = _request("GET", "/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_write_methods_are_rejected():
    """只读服务：任何写动词一律 405（路由表里根本没有写入口）。"""
    for method in ("POST", "PUT", "DELETE"):
        status, headers, body = _request(method, "/api/summary")
        assert status == 405, method
        assert headers["allow"] == "GET, HEAD"
        assert json.loads(body)["error"] == "read-only service"
