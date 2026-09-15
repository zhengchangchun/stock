"""P15：持仓管理 Web 应用 —— **真起服务** + `http.client` 打请求。

这里的测试刻意**不**直接调 `handle()`：路由表自测只能证明「函数返回了 200」，
证明不了「HTTP 层真的把它发出去了」。所以每个 fixture 都真起
`ThreadingHTTPServer`（`port=0` 随机端口，避免撞端口），用 `http.client` 走网络栈。

网络守卫见 `conftest.loopback_http`：只放开回环，别的目标照旧抛 `_NET_BLOCKED`。
"""

import html
import json
import http.client
import re
import sqlite3
from urllib.parse import urlencode

import pytest

from stocklab.labweb.app import (Context, assert_loopback, make_server,
                                 normalize_base_path)
from stocklab.labweb.data import Lab
from stocklab.labweb.tokens import TokenSigner
from stocklab.portfolio.ledger import record_trade
from stocklab.portfolio.positions import replay_trades
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
ASOF = "2026-09-14"
SECRET = b"test-secret-not-a-real-key"
CAL_DATES = ("2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14")


@pytest.fixture
def db(tmp_db):
    """一个**有持仓**的小库：09-10 买入 000333 100 股。"""
    init_db(tmp_db)
    conn = connect(tmp_db)
    conn.execute("INSERT INTO instruments (code, name, market, board, added_at)"
                 " VALUES ('000333','美的集团','sz','main',?)", (NOW,))
    conn.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL_DATES])
    conn.commit()
    record_trade(conn, date="2026-09-10", code="000333", side="buy",
                 price=86.80, qty=100, fee=5.09, now=NOW)
    conn.close()
    return tmp_db


@pytest.fixture
def ctx(db):
    return Context(lab=Lab(db, asof=ASOF), signer=TokenSigner(SECRET),
                   base_path="/lab")


@pytest.fixture
def server(ctx, loopback_http):
    httpd = make_server("127.0.0.1", 0, ctx=ctx)
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


@pytest.fixture
def base(ctx):
    return ctx.base_path


def request(server, method, path, body=None, *, headers=None):
    """真 HTTP 请求。返回 `(status, text, headers_dict)`。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                      timeout=10)
    try:
        payload = urlencode(body).encode() if isinstance(body, dict) else body
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, raw, dict(resp.getheaders())
    finally:
        conn.close()


def rows(db, table):
    conn = connect(db)
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


@pytest.fixture
def token(ctx):
    return ctx.signer.mint(ctx.lab.asof)


def trade_form(token, form_id="fid-1", **over):
    """一份合法成交表单；`over` 覆盖单字段（非法用例就靠它）。"""
    form = {"_token": token, "_form_id": form_id, "date": "2026-09-14",
            "code": "000333", "side": "buy", "price": "30.00", "qty": "100",
            "fee": "5.00", "note": ""}
    form.update(over)
    return form


# ---------- 路由 ----------

READ_PAGES = ("/", "/trades", "/cash", "/risk", "/data")


@pytest.mark.parametrize("page", READ_PAGES)
def test_read_pages_are_200(server, base, page):
    status, text, hdrs = request(server, "GET", base + page)
    assert status == 200, text[:400]
    assert hdrs["Content-Type"].startswith("text/html")


def test_trade_detail_is_200(server, base, db):
    tid = rows(db, "real_trades")[0]["trade_id"]
    status, text, _ = request(server, "GET", f"{base}/trades/{tid}")
    assert status == 200
    assert "000333" in text


def test_health_is_json(server, base):
    status, text, hdrs = request(server, "GET", base + "/health")
    assert status == 200
    assert hdrs["Content-Type"].startswith("application/json")
    assert '"status"' in text and '"db_exists": true' in text


def test_unknown_page_is_404(server, base):
    status, text, _ = request(server, "GET", base + "/nope")
    assert status == 404
    assert "没有这个页面" in text


def test_unknown_trade_id_is_404(server, base):
    status, _, _ = request(server, "GET", f"{base}/trades/999999")
    assert status == 404


def test_root_redirects_into_the_sub_path(server, base):
    status, _, hdrs = request(server, "GET", "/")
    assert status == 303
    assert hdrs["Location"] == base + "/"


def test_path_outside_base_path_is_404(server):
    """挂在 /lab 时 /trades 就是**不存在**的 —— 不能悄悄也给 200。"""
    status, _, _ = request(server, "GET", "/trades")
    assert status == 404


@pytest.mark.parametrize("method", ("PUT", "DELETE"))
def test_unsupported_method_is_405(server, base, method):
    status, _, hdrs = request(server, method, base + "/trades")
    assert status == 405
    assert hdrs["Allow"] == "GET, POST"


def test_head_has_no_body(server, base):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                      timeout=10)
    conn.request("HEAD", base + "/")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.read() == b""
    conn.close()


# ---------- 站内链接与外部引用 ----------

def test_every_in_site_link_carries_the_base_path(server, base):
    """正则扫 HTML：不许出现不带前缀的站内绝对路径。"""
    pattern = re.compile(r'(?:href|action|src)="(/[^/][^"]*)"')
    for page in READ_PAGES:
        status, text, _ = request(server, "GET", base + page)
        assert status == 200
        for ref in pattern.findall(text):
            assert ref.startswith(base + "/") or ref == base, \
                f"{page} 出现无前缀站内路径 {ref!r}"


#: 允许出现的站外协议前缀（一个都不许有）
EXTERNAL = ("http://", "https://", "//")


def test_html_has_no_external_references(server, base):
    """零依赖 + 离线可用：不许有 CDN / 外链字体 / 外部脚本。

    P16 起改成**按 URL 判定**而不是按标签判定。原来的 `assert "<link" not in text`
    是标签级的，本意对（不许外链）但判据错：自托管的
    `<link rel="stylesheet" href="/lab/static/app.css">` 会被它误杀，
    而 `<link href="//evil.example/x.css">` 这种协议相对外链它根本查不出来。
    现在的判据对每个 `href`/`src` 都要求是 base-path 前缀的站内路径 ——
    比原来**更严**，不是更松。
    """
    ref = re.compile(r'(?:href|src)="([^"]*)"')
    for page in READ_PAGES:
        _, text, _ = request(server, "GET", base + page)
        for bad in EXTERNAL:
            assert bad not in text, f"{page} 出现外部引用前缀 {bad!r}"
        for url in ref.findall(text):
            if url.startswith("#"):
                continue
            assert url == base or url.startswith(base + "/"), \
                f"{page} 出现非站内引用 {url!r}"


def test_html_references_the_two_static_assets(server, base):
    """全站**只有**两个外部资源，都是自己的静态路由。"""
    for page in READ_PAGES:
        _, text, _ = request(server, "GET", base + page)
        assert f'href="{base}/static/app.css"' in text, page
        assert f'src="{base}/static/app.js"' in text, page


@pytest.mark.parametrize("asset,ctype", [
    ("/static/app.css", "text/css"),
    ("/static/app.js", "text/javascript"),
])
def test_static_assets_are_served_from_the_app_itself(server, base, asset, ctype):
    status, text, hdrs = request(server, "GET", base + asset)
    assert status == 200
    assert hdrs["Content-Type"].startswith(ctype)
    assert len(text) > 500
    etag = hdrs["ETag"]
    # 再要一次：带 If-None-Match 应当拿 304（省掉重复下载）
    status2, body2, _ = request(server, "GET", base + asset,
                                headers={"If-None-Match": etag})
    assert status2 == 304 and body2 == ""


def test_unknown_static_path_is_404(server, base):
    """静态目录是白名单，不是文件服务器。"""
    assert request(server, "GET", base + "/static/../app.py")[0] in (403, 404)
    assert request(server, "GET", base + "/static/secrets.css")[0] == 404


# ---------- 局部更新（fetch 片段） ----------

FRAG = {"X-Lab-Fragment": "1"}


def test_fragment_post_returns_json_blocks_and_writes(db, server, base, token):
    """写成功后回 JSON：回执 + 流水表片段。**库照写**，只是不整页刷新。"""
    status, text, hdrs = request(server, "POST", base + "/trades",
                                 trade_form(token, form_id="frag-1"),
                                 headers=FRAG)
    assert status == 200, text[:400]
    assert hdrs["Content-Type"].startswith("application/json")
    data = json.loads(text)
    assert data["ok"] is True
    assert "已写入" in data["receipt_html"]
    assert 'id="trades-pane"' not in text          # 片段不带整页外壳
    assert "000333" in data["pane_html"]
    assert data["url"].startswith(base + "/trades?receipt=trade:")
    assert len(rows(db, "real_trades")) == 2       # 真的写进去了


def test_fragment_post_is_idempotent_like_the_html_path(db, server, base, token):
    """同一份表单发两次（双击）：第二回是幂等命中，**不写第二行**。"""
    form = trade_form(token, form_id="frag-dup")
    request(server, "POST", base + "/trades", form, headers=FRAG)
    _, text, _ = request(server, "POST", base + "/trades", form, headers=FRAG)
    data = json.loads(text)
    assert data["ok"] is True and "幂等命中" in data["receipt_html"]
    assert len(rows(db, "real_trades")) == 2


def test_fragment_error_is_json_with_field_and_db_unchanged(db, server, base, token):
    """校验失败 → 400 + JSON，且**带字段名**（页面才能标在那一格旁边）。"""
    status, text, _ = request(server, "POST", base + "/trades",
                              trade_form(token, form_id="frag-bad", price="0"),
                              headers=FRAG)
    assert status == 400
    data = json.loads(text)
    assert data["ok"] is False and data["field"] == "price"
    assert "0" in data["error"]
    assert len(rows(db, "real_trades")) == 1


def test_field_level_error_is_marked_next_to_the_input(db, server, base, token):
    """整页路径同样把错误标到字段旁（`field bad` + 格内文案）。"""
    _, text, _ = request(server, "POST", base + "/trades",
                         trade_form(token, form_id="bad-2", qty=""))
    assert 'data-field="qty"' in text
    assert 'class="field w-qty bad"' in text


def test_no_duplicate_confirmation_still_falls_back_to_a_page(db, server, base, token):
    """疑似重复**不**走片段：它需要一个整页让人勾选确认。"""
    request(server, "POST", base + "/trades",
            trade_form(token, form_id="real-1"), headers=FRAG)
    status, text, _ = request(server, "POST", base + "/trades",
                              trade_form(token, form_id="real-2"), headers=FRAG)
    assert status == 409
    assert "<!doctype html>" in text and "疑似重复" in text
    assert len(rows(db, "real_trades")) == 2       # 没有写第三行


# ---------- 显示筛选与富文本 ----------

def test_trades_can_be_filtered_by_code(db, server, base):
    _, text, _ = request(server, "GET", base + "/trades?code=000333")
    assert "只看 000333" in text
    assert "显示全部" in text


def test_filtered_away_code_shows_an_empty_list(db, server, base):
    _, text, _ = request(server, "GET", base + "/trades?code=600519")
    assert "还没有任何成交" in text


def test_double_asterisk_is_rendered_as_bold_not_shown_literally(db, server, base):
    """账本 detail 里的 `**超**` 必须变成加粗 —— P16 之前是原样显示 `**超**` 的。"""
    _, text, _ = request(server, "GET", base + "/")
    assert "**" not in text, "页面里还在原样显示 Markdown 星号"
    assert "<b>" in text


# ---------- _token ----------

def test_missing_token_is_403_and_db_unchanged(server, base, db, token):
    before = rows(db, "real_trades")
    form = trade_form(token)
    del form["_token"]
    status, text, _ = request(server, "POST", base + "/trades", form)
    assert status == 403
    assert "账本没有任何改动" in text
    assert rows(db, "real_trades") == before


def test_wrong_token_is_403_and_db_unchanged(server, base, db, token):
    before = rows(db, "real_trades")
    status, text, _ = request(server, "POST", base + "/trades",
                              trade_form(token, _token="0" * 32))
    assert status == 403
    assert "账本没有任何改动" in text
    assert rows(db, "real_trades") == before


def test_token_from_another_secret_is_403(server, base, db, token):
    before = rows(db, "real_trades")
    foreign = TokenSigner(b"a-different-secret").mint(ASOF)
    status, _, _ = request(server, "POST", base + "/trades",
                           trade_form(token, _token=foreign))
    assert status == 403
    assert rows(db, "real_trades") == before


# ---------- 写入：合法路径 ----------

def test_legal_buy_writes_row_and_redirects_with_receipt(server, base, db, token):
    before = rows(db, "real_trades")
    status, _, hdrs = request(server, "POST", base + "/trades",
                              trade_form(token))
    assert status == 303, hdrs
    loc = hdrs["Location"]
    assert loc.startswith(base + "/trades?receipt=trade:")
    after = rows(db, "real_trades")
    assert len(after) == len(before) + 1
    new = after[-1]
    assert (new["date"], new["code"], new["side"], new["price"], new["qty"]) == \
        ("2026-09-14", "000333", "buy", 30.0, 100)

    # 回执要真的渲染出来，而且是**那一笔**
    status, text, _ = request(server, "GET", loc)
    assert status == 200
    assert f"#{new['trade_id']}" in text


def test_handcrafted_receipt_for_unknown_id_is_not_rendered(server, base):
    """手编 `?receipt=trade:999999` 不能在页面上造出一条不存在的回执。"""
    status, text, _ = request(server, "GET",
                              f"{base}/trades?receipt=trade:999999")
    assert status == 200
    assert "已记录" not in text and "回执" not in text.split("</nav>")[-1]


def test_legal_cash_deposit_redirects(server, base, db, token):
    before = rows(db, "cash_flows")
    form = {"_token": token, "_form_id": "cf-1", "date": "2026-09-14",
            "kind": "deposit", "amount": "20000", "note": "追加本金"}
    status, _, hdrs = request(server, "POST", base + "/cash", form)
    assert status == 303
    assert hdrs["Location"].startswith(base + "/cash?receipt=cash:")
    after = rows(db, "cash_flows")
    assert len(after) == len(before) + 1
    assert after[-1]["amount"] == 20000.0


# ---------- 写入：非法矩阵（每条校验一例，且**库未变**） ----------

BAD_TRADES = (
    ("side 必须是", {"side": "hold"}),
    ("qty 必须 > 0", {"qty": "0"}),
    ("整手", {"qty": "150"}),
    ("price 必须 > 0", {"price": "0"}),
    ("price 必须 > 0", {"price": "-1"}),
    ("fee 必须 ≥ 0", {"fee": "-1"}),
    ("未在 instruments 登记", {"code": "999999"}),
    ("不在 trading_calendar 里", {"date": "2026-09-12"}),
    ("禁录未来成交", {"date": "2026-12-31"}),
    ("date 格式必须是 YYYY-MM-DD", {"date": "2026/09/14"}),
    ("超过当时持仓", {"side": "sell", "qty": "200"}),
    ("price 必须是数字", {"price": "abc"}),
    ("qty 必须是整数", {"qty": "1.5"}),
    ("price 不能为空", {"price": ""}),
    ("fee 必须是数字", {"fee": "x"}),
)


@pytest.mark.parametrize("fragment,over", BAD_TRADES,
                         ids=[f"{k}:{list(v)[0]}" for k, v in BAD_TRADES])
def test_bad_trade_is_400_with_reason_and_db_unchanged(server, base, db, token,
                                                      fragment, over):
    before = rows(db, "real_trades")
    status, text, _ = request(server, "POST", base + "/trades",
                              trade_form(token, **over))
    assert status == 400, f"{fragment} → {status}"
    # 页面是 HTML：`>` `<` 会被 esc() 转义，所以按转义后的样子比对
    assert html.escape(fragment) in text, f"页面没回显「{fragment}」"
    assert rows(db, "real_trades") == before


def test_bad_cash_is_400_and_db_unchanged(server, base, db, token):
    before = rows(db, "cash_flows")
    for fragment, over in (
        ("kind 必须是", {"kind": "bonus"}),
        ("必须 > 0", {"kind": "deposit", "amount": "-1"}),
        ("必须 < 0", {"kind": "withdraw", "amount": "1"}),
        ("amount 不能为 0", {"kind": "other", "amount": "0"}),
        ("amount 不能为空", {"kind": "deposit", "amount": ""}),
    ):
        form = {"_token": token, "_form_id": "cf-bad", "date": "2026-09-14",
                "kind": "deposit", "amount": "1", "note": ""}
        form.update(over)
        status, text, _ = request(server, "POST", base + "/cash", form)
        assert status == 400, f"{fragment} → {status}"
        assert html.escape(fragment) in text, f"页面没回显「{fragment}」"
    assert rows(db, "cash_flows") == before


# ---------- 幂等 ----------

def test_same_form_twice_is_idempotent(server, base, db, token):
    """重试（同一个 `_form_id`）→ **不写第二行**。"""
    form = trade_form(token, _form_id="retry-me")
    assert request(server, "POST", base + "/trades", form)[0] == 303
    n = len(rows(db, "real_trades"))
    status, _, hdrs = request(server, "POST", base + "/trades", form)
    assert status == 303
    assert "state=identical" in hdrs["Location"]
    assert len(rows(db, "real_trades")) == n


def test_identical_new_form_asks_for_confirmation(server, base, db, token):
    """**重新敲**一笔一模一样的 → 疑似重复确认页，且**此时还没写**。"""
    before = rows(db, "real_trades")
    seeded = before[0]
    form = trade_form(token, _form_id="fresh-1", date=seeded["date"],
                      price=f"{seeded['price']:.2f}", qty=str(seeded["qty"]),
                      fee=f"{seeded['fee']:.2f}")
    status, text, _ = request(server, "POST", base + "/trades", form)
    assert status == 409
    assert "疑似重复" in text
    assert "另一笔真实成交" in text
    assert rows(db, "real_trades") == before, "确认页之前就写了库"


def test_confirmation_writes_the_second_genuine_trade(server, base, db, token):
    before = rows(db, "real_trades")
    seeded = before[0]
    form = trade_form(token, _form_id="fresh-2", date=seeded["date"],
                      price=f"{seeded['price']:.2f}", qty=str(seeded["qty"]),
                      fee=f"{seeded['fee']:.2f}")
    assert request(server, "POST", base + "/trades", form)[0] == 409
    form["confirm_duplicate"] = "1"
    status, _, hdrs = request(server, "POST", base + "/trades", form)
    assert status == 303, hdrs
    assert len(rows(db, "real_trades")) == len(before) + 1


# ---------- 冲正 ----------

def test_reverse_keeps_original_row_and_rolls_back_holdings(server, base, db,
                                                            token):
    before = rows(db, "real_trades")
    tid = before[0]["trade_id"]
    status, _, hdrs = request(server, "POST", f"{base}/trades/{tid}/reverse",
                              {"_token": token, "_form_id": "rev-1",
                               "reason": "录错了价格"})
    assert status == 303, hdrs
    after = rows(db, "real_trades")
    # append-only：原行**一动不动**，只多了一笔反向的
    assert len(after) == len(before) + 1
    assert after[0] == before[0]
    rev = after[-1]
    assert rev["side"] == "sell" and rev["qty"] == before[0]["qty"]
    assert f"冲正 #{tid}" in rev["note"] and "录错了价格" in rev["note"]

    with connect(db) as conn:
        held = replay_trades([dict(r) for r in conn.execute(
            "SELECT trade_id, date, code, side, price, qty, fee"
            " FROM real_trades")])
    pos = held.get("000333")            # 清仓后仍会返回，qty == 0
    assert pos is not None and pos.qty == 0


def test_reverse_requires_a_reason(server, base, db, token):
    tid = rows(db, "real_trades")[0]["trade_id"]
    before = rows(db, "real_trades")
    status, text, _ = request(server, "POST", f"{base}/trades/{tid}/reverse",
                              {"_token": token, "_form_id": "rev-2",
                               "reason": "  "})
    assert status == 400
    assert "reason" in text
    assert rows(db, "real_trades") == before


def test_reverse_unknown_id_is_404(server, base, token):
    status, _, _ = request(server, "POST", f"{base}/trades/999999/reverse",
                           {"_token": token, "_form_id": "rev-3",
                            "reason": "x"})
    assert status == 404


def test_reverse_without_token_is_403_and_db_unchanged(server, base, db):
    tid = rows(db, "real_trades")[0]["trade_id"]
    before = rows(db, "real_trades")
    status, _, _ = request(server, "POST", f"{base}/trades/{tid}/reverse",
                           {"_form_id": "rev-4", "reason": "x"})
    assert status == 403
    assert rows(db, "real_trades") == before


# ---------- 启动守卫 ----------

def test_non_loopback_host_is_refused(tmp_db):
    for host in ("0.0.0.0", "::", "192.168.1.5", "example.com", ""):
        with pytest.raises(Exception) as exc:
            make_server(host, 0, db_path=tmp_db)
        assert "回环" in str(exc.value)


@pytest.mark.parametrize("good", ("127.0.0.1", "::1", "localhost"))
def test_loopback_hosts_are_accepted(good):
    assert_loopback(good)


def test_assert_loopback_is_the_dashboard_one():
    """复用同一份白名单 —— 两处各写一遍迟早会漂移。"""
    import stocklab.dashboard.server as dash
    assert assert_loopback is dash.assert_loopback


def test_non_loopback_never_binds_a_socket(monkeypatch):
    """拒绝必须发生在 bind **之前**：失败进程不该占着端口。"""
    import socket as _socket
    calls = []
    monkeypatch.setattr(_socket, "socket", lambda *a, **k: calls.append(a))
    with pytest.raises(Exception):
        make_server("0.0.0.0", 0, db_path="unused.db")
    assert calls == []


@pytest.mark.parametrize("raw,expected", (
    ("/lab", "/lab"), ("/lab/", "/lab"), ("/", ""), ("", ""),
    ("/a/b", "/a/b"),
))
def test_normalize_base_path_ok(raw, expected):
    assert normalize_base_path(raw) == expected


@pytest.mark.parametrize("bad", ("lab", "/a b", "/a?b", "/a#b", "/../etc", None))
def test_normalize_base_path_rejects(bad):
    from stocklab.labweb.app import BadBasePath
    with pytest.raises(BadBasePath):
        normalize_base_path(bad)


def test_root_mount_serves_pages_without_prefix(db, loopback_http):
    """`--base-path /` 也要能用（挂在根，别把重定向成环）。"""
    ctx = Context(lab=Lab(db, asof=ASOF), signer=TokenSigner(SECRET),
                  base_path="")
    httpd = make_server("127.0.0.1", 0, ctx=ctx)
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        status, text, _ = request(httpd, "GET", "/")
        assert status == 200 and "美的集团" in text
        assert request(httpd, "GET", "/trades")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_db_is_opened_read_only_for_reads(server, base, db):
    """只读页不许留下任何写痕迹：读完之后库里还是原来的行数。"""
    before = (rows(db, "real_trades"), rows(db, "cash_flows"))
    for page in READ_PAGES:
        request(server, "GET", base + page)
    assert (rows(db, "real_trades"), rows(db, "cash_flows")) == before


def test_sqlite_errors_do_not_leak_as_500(server, base, tmp_path, loopback_http):
    """库文件不存在 → 页面可读地报错，而不是把 sqlite 栈打到 stderr。"""
    missing = tmp_path / "missing.db"
    ctx = Context(lab=Lab(missing, asof=ASOF), signer=TokenSigner(SECRET))
    httpd = make_server("127.0.0.1", 0, ctx=ctx)
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        status, text, _ = request(httpd, "GET", "/lab/health")
        assert status == 200                       # health 自报 degraded
        assert '"db_exists": false' in text
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_original_ledger_row_is_never_updated(db):
    """老规矩不能因为多了个 Web 层就松动：UPDATE 仍被触发器挡住。"""
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE real_trades SET qty = 1")
    finally:
        conn.close()
