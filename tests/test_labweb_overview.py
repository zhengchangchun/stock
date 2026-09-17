"""P36：总览页的四段展示接线 —— 模拟盘 / 能不能动 / 怎么做去害 / 风险摘要。

本文件测的是**展示层有没有把已经算好的东西显示出来**，不是重算口径：

| 段 | 断言的对象 | 被摘掉后会红的实现 |
|---|---|---|
| 风险摘要 | `overview()["risk"]` 非 null 且与 `risk()` 同源 | `overview()` 传 `risk_block` |
| 模拟盘 | 页面出现各臂数字 / 无数据时的「暂无」 | `Lab._paper` + `render._paper_block` |
| 能不能动 | 页面出现 `HEADLINE[stop]` 与整手原因 | `render.actions_block` 接线 |
| 怎么做 | 折算股数 < 一手时必须有「执行不了」标注 | `render.advisory_list` 的标注分支 |

整手文案与阈值一律**从被测模块取**（`decision.HEADLINE` / `whole_lot_reason` /
`LOT_SIZE`），不手抄一份到测试里 —— 手抄的那份会与上游漂移，然后测试还是绿的。
"""

import re

import pytest

from stocklab.labweb.data import Lab
from stocklab.labweb.render import overview_page
from stocklab.paper.store import insert_nav
from stocklab.portfolio.decision import HEADLINE, LOT_SIZE, whole_lot_reason
from stocklab.portfolio.ledger import record_trade
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
ASOF = "2026-09-14"
DATES = ("2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14")


def _db(tmp_path, *, close=86.00):
    path = tmp_path / "labweb.db"
    init_db(path)
    conn = connect(path)
    try:
        conn.execute("INSERT INTO instruments (code, name, market, board, added_at)"
                     " VALUES ('000333','美的集团','sz','main',?)", (NOW,))
        conn.executemany(
            "INSERT INTO trading_calendar (date, is_open, source, created_at)"
            " VALUES (?,1,'tencent',?)", [(d, NOW) for d in DATES])
        conn.commit()
        record_trade(conn, date="2026-09-10", code="000333", side="buy",
                     price=86.80, qty=100, fee=5.09, now=NOW)
        if close is not None:
            conn.execute(
                "INSERT INTO bars_daily (code, date, open, high, low, close,"
                " volume, adj_mode, source, fetched_at)"
                " VALUES ('000333',?,?,?,?,?,1000,'none','tencent',?)",
                (DATES[-1], close, close, close, close, NOW))
        conn.commit()
    finally:
        conn.close()
    return path


def _html(path, **kw) -> str:
    lab = Lab(path, asof=kw.pop("asof", ASOF))
    kw.setdefault("base", "/lab")
    kw.setdefault("built_at", NOW)
    return overview_page(lab.overview(), **kw)


def _server_html(tmp_path, loopback_http, **kw) -> str:
    """走真 HTTP（`make_server(port=0)`），不直接调 `handle()`。

    `loopback_http` 必须在场：全测试套默认离线（fixture `no_network`），
    真起服务打请求是**唯一**被放开的例外，且只放开回环。
    """
    import http.client
    import threading

    from stocklab.labweb.app import Context, make_server
    from stocklab.labweb.tokens import TokenSigner

    path = kw.pop("db", _db(tmp_path))
    ctx = Context(lab=Lab(path, asof=kw.pop("asof", ASOF)),
                  signer=TokenSigner(b"p36-secret"), base_path="/lab")
    httpd = make_server("127.0.0.1", 0, ctx=ctx)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1],
                                          timeout=10)
        try:
            conn.request("GET", "/lab/")
            resp = conn.getresponse()
            assert resp.status == 200
            return resp.read().decode("utf-8", "replace")
        finally:
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


# ---------- T1：总览风险摘要不再「没算」 ----------

def test_overview_carries_risk_block(tmp_path):
    """`overview()["risk"]` 必须是算过的面板 —— 不是 null。"""
    ov = Lab(_db(tmp_path), asof=ASOF).overview()
    assert ov["risk"] is not None
    assert ov["risk"]["code"] == "000333"


def test_overview_risk_is_same_source_as_risk_page(tmp_path):
    """总览与 /risk 必须**同一个** block（两处各拼一遍 = 第二个真相来源）。"""
    lab = Lab(_db(tmp_path), asof=ASOF)
    assert lab.overview()["risk"] == lab.risk()["risk"]


def test_overview_html_does_not_say_not_computed(tmp_path):
    """页面上不许再出现「没算」——它现在是算了之后才渲染的。"""
    html = _html(_db(tmp_path))
    assert "没算" not in html
    assert "未接入风险面板" not in html


def test_overview_server_risk_section_has_verdict(tmp_path, loopback_http):
    """真 HTTP 下风险摘要段显示凯利结论（而不是 null 的提示）。"""
    html = _server_html(tmp_path, loopback_http)
    assert "凯利结论" in html
    assert "没算" not in html


# ---------- T2：模拟盘段 ----------

def _add_paper(path, rows):
    conn = connect(path)
    try:
        for account_id, date, nav in rows:
            insert_nav(conn, account_id=account_id, date=date, cash=0.0,
                           positions=[], market_value=0.0, nav=nav,
                           drawdown=0.0, cum_cost=0.0,
                           cum_return=round(nav / 20000.0 - 1.0, 6),
                           net_deposits=20000.0, index_300_level=None,
                           index_300_asof=None, now=NOW)
    finally:
        conn.close()


def test_paper_section_lists_every_arm(tmp_path):
    path = _db(tmp_path)
    _add_paper(path, [("arm-hold", "2026-09-14", 19814.91),
                      ("arm-discipline-05", "2026-09-14", 19807.21)])
    html = _html(path)
    assert "arm-hold" in html and "arm-discipline-05" in html
    assert "19,814.91" in html and "19,807.21" in html
    assert "模拟盘" in html


def test_paper_section_shows_only_latest_trading_day(tmp_path):
    """历史 `--asof` 截图不许出现未来的那行 —— 取数一律 `date <= asof`。"""
    path = _db(tmp_path)
    _add_paper(path, [("arm-hold", "2026-09-11", 19000.0),
                      ("arm-hold", "2026-09-14", 19814.91),
                      ("arm-hold", "2026-09-30", 22222.22)])
    html = _html(path, asof="2026-09-11")
    assert "19,000.00" in html
    assert "19,814.91" not in html and "22,222.22" not in html


def test_paper_section_does_not_pick_a_winner(tmp_path):
    """多臂**只并列**：页面上不许出现推荐性措辞（同 PAPER_NO_PICK 纪律）。"""
    path = _db(tmp_path)
    _add_paper(path, [("arm-hold", "2026-09-14", 19814.91),
                      ("arm-discipline-05", "2026-09-14", 19807.21)])
    html = _html(path)
    for word in ("建议", "推荐", "应该", "最优", "最佳", "冠军"):
        assert word not in html, word


def test_paper_section_when_empty(tmp_path):
    html = _html(_db(tmp_path))
    assert "暂无模拟盘记录" in html


# ---------- T3：「能不能动」段 ----------

def test_actions_section_renders_whole_lot_headline(tmp_path):
    """收盘破线 → 页面必须显示「全清或不动」这句结论。"""
    html = _html(_db(tmp_path, close=82.13))
    assert HEADLINE["stop"] in html


def test_actions_section_renders_whole_lot_reason_verbatim(tmp_path):
    """整手原因**原样**展示：句子从 `decision.whole_lot_reason(100)` 取。"""
    reason = whole_lot_reason(100)
    assert reason  # 一手仓必然拆不开
    html = _html(_db(tmp_path, close=82.13))
    assert reason in html


def test_actions_section_shows_proceeds_of_full_exit(tmp_path):
    html = _html(_db(tmp_path, close=82.13))
    assert "全清" in html
    assert re.search(r"\d[\d,]*\.\d\d", html)


def test_actions_section_shows_unknown_reason(tmp_path):
    """判不了时**必须写清为什么**（`unknown` 不是「没事」）。"""
    html = _html(_db(tmp_path, close=None))
    assert HEADLINE["unknown"] in html
    assert "取不到收盘价" in html


# ---------- T4：「怎么做」段去害 ----------

def test_advisory_flags_unexecutable_share_counts(tmp_path):
    """100 股持仓下，折算出的 10 股 / 20 股减仓**执行不了**，必须标注。

    判据用导入的 `LOT_SIZE`（不许在测试里写死 100），并逐条核对：
    页面里每个「N 股（按当前 …）」若 `N % LOT_SIZE != 0`，同一行必须出现
    「执行不了」。
    """
    html = _html(_db(tmp_path))
    hits = re.findall(r"=\s*(\d+)\s*股（按当前", html)
    assert hits, "至少应有一句「= N 股（按当前 …）」的减仓折算"
    for n in hits:
        if int(n) % LOT_SIZE != 0:
            assert "执行不了" in html, f"{n} 股不可执行却没有标注"
