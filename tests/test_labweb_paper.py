"""模拟盘对照页（`/paper`）：取数与渲染。

本文件测的是**展示层有没有把已经算好的东西摆出来**，不是重算口径：

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 页面出现我 / AI 三档 / 大盘 / 什么都不做 | `paper_render._ordered` + `compare_table` |
| 各臂累计收益 == `paper.engine.build_report` 的同名数 | `paper_data.track` 自己算一套 |
| 「相对大盘」== 该线累计收益 − 指数累计收益 | 超额列接了别的字段 |
| 起跑日锚点 == `initial_nav / initial_capital − 1` | 锚点写死 0 |
| 指数缺价 → 断线且计数（不插值、不用前值滚动） | 锚点/前值填充 |
| 认不出的账户名原样显示 | 猜成最像的那条臂 |
| 记一笔真成交并 step 后「我」与「什么都不做」分开 | `mirror_equals_hold` 写死 True |
| 某臂当天没净值 → 「当日无净值；最后一行 …」 | 空白 / 拿上一日冒充当日 |
| 「AI 自己编排的东西」一段的每个计数 == 库里的行数 | 页面上写死「用了/没用」 |
| 表外触发理由 → 「已经接了写死条文之外的东西」 | 永远只说「没用上」 |
| 准确率数字 == `rolling_accuracy` 的同名数 | 页面自己算一套命中率 |
| 页面里不出现 `&lt;a`/`&lt;span` 这类被转义的标签 | 把 HTML 喂给 `rich()` |

数字一律**从被测模块取**，不手抄一份到测试里 —— 手抄的那份会与上游漂移。
"""

import http.client
import json
import re
import threading

import pytest

from stocklab.labweb import paper_data, paper_render
from stocklab.labweb.render import NAV_ITEMS
from stocklab.paper import engine as paper_engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import INITIAL_CAPITAL
from stocklab.portfolio.ledger import record_trade
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-21T16:00:00+08:00"
NEXT_NOW = "2026-09-22T16:00:00+08:00"
START = "2026-09-15"
LAST = "2026-09-21"
NEXT = "2026-09-22"
STEP_DAYS = ("2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21")
CAL = ("2026-09-11", "2026-09-14", START, *STEP_DAYS, NEXT)
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23, "2026-09-16": 87.60,
               "2026-09-17": 87.10, "2026-09-18": 86.40, "2026-09-21": 80.90,
               "2026-09-22": 81.50},
    "510300": {"2026-09-15": 4.523, "2026-09-16": 4.550, "2026-09-17": 4.532,
               "2026-09-18": 4.582, "2026-09-21": 4.608, "2026-09-22": 4.620},
    "510880": {"2026-09-15": 3.382, "2026-09-16": 3.390, "2026-09-17": 3.380,
               "2026-09-18": 3.375, "2026-09-21": 3.381, "2026-09-22": 3.385},
    "sh000300": {"2026-09-15": 4450.04, "2026-09-16": 4480.27,
                 "2026-09-17": 4460.16, "2026-09-18": 4507.39,
                 "2026-09-21": 4539.57, "2026-09-22": 4550.00},
}

ARMS = ("arm-hold", "arm-now", "arm-discipline-05", "arm-discipline-10",
        "arm-discipline-15", "arm-agent", "arm-agent-random")


def _fixture_db(tmp_path, *, index_gap: bool = False, steps: bool = True):
    """一份能跑完 `paper init` + `step` 的库（口径与 `test_cli_paper` 同一套）。"""
    path = tmp_path / "paper-page.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main',?,?)",
                  [(code, code, "stock" if code == "000333" else "etf", NOW)
                   for code in ("000333", "510300", "510880")])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    rows = []
    for code, series in BARS.items():
        for d, v in series.items():
            if index_gap and code == "sh000300" and d == "2026-09-17":
                continue                      # 制造一个「那天没开盘」的洞
            rows.append((code, d, v))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, d, v in rows])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES (?, 'deposit', ?, '起跑本金', ?)", (START, INITIAL_CAPITAL, NOW))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES (?, '000333', 'buy', 86.80, 100, 0, '起跑持仓', ?)",
              (START, NOW))
    c.commit()
    paper_engine.init_accounts(c, start_date=START, now=NOW)
    if steps:
        for day in STEP_DAYS:
            paper_engine.step(c, day, now=NOW)
            # P56 / D-50：两条 AI 臂被 `paper step` 让出日终，改由决策循环落
            # （夹具里没有决策 ⇒ 平盘行 + 缺决策异常，不影响本文件的对照读数）。
            paper_engine.agent_run(c, day, now=NOW)
    c.close()
    return path


def _add_late_arm(path, *, account_id="arm-discipline-99", target=None,
                  date="2026-09-18", nav=1000.0):
    """加一条**从某天起才记净值**的臂。

    这是本系统里唯一可能出现的「认不出的账户」：`paper_accounts.arm` 的 CHECK 只认
    `'hold'`/`'now'`/`'discipline'`/`'agent'`/`'agent_random'`，所以「未知」只能是没见过的
    `account_id`，或 `discipline` 但 `etf_target_pct` 为空。
    """
    c = connect(path)
    try:
        paper_store.insert_account(
            c, account_id=account_id, arm="discipline", etf_target_pct=target,
            start_date=START, initial_cash=nav, initial_positions=[],
            initial_nav=nav, params={"initial_capital": nav}, now=NOW)
        paper_store.insert_nav(
            c, account_id=account_id, date=date, cash=nav, positions=[],
            market_value=0.0, nav=nav, drawdown=0.0, cum_cost=0.0, cum_return=0.0,
            net_deposits=nav, index_300_level=None, index_300_asof=None, now=NOW)
    finally:
        c.close()


@pytest.fixture
def db(tmp_path):
    return _fixture_db(tmp_path)


def _track(path, asof=LAST):
    c = connect(path)
    try:
        return paper_data.track(c, asof)
    finally:
        c.close()


def _html(path, asof=LAST) -> str:
    return paper_render.paper_page(_track(path, asof), base="/lab", built_at=NOW)


def _arm(data, account_id):
    return next(a for a in data["arms"] if a["account_id"] == account_id)


# ---------- 取数 ----------

def test_track_matches_build_report_bit_for_bit(db):
    """页面上的净值/收益/回撤/成本/超额**必须**与 `paper show` 同源。"""
    data = _track(db)
    c = connect(db)
    try:
        report = paper_engine.build_report(c, data["date"])
    finally:
        c.close()
    by_id = {a["account_id"]: a for a in data["arms"]}
    assert set(by_id) == set(ARMS)
    assert report["accounts"], "夹具没建起臂 —— 下面全是空断言"
    for entry in report["accounts"]:
        arm = by_id[entry["account_id"]]
        for key in ("nav", "cum_return", "max_drawdown", "cum_cost",
                    "excess_vs_index_300"):
            assert arm[key] == entry[key], f"{entry['account_id']}.{key} 与报告不一致"
    assert data["index"]["return_since_start"] == \
        report["index_300"]["return_since_start"]


def test_excess_columns_are_subtractions_not_new_metrics(db):
    """「相对大盘」「相对我」各是**一次减法**，用上游三个数当场验。"""
    data = _track(db)
    idx_ret = data["index"]["return_since_start"]
    now = _arm(data, "arm-now")
    for arm in data["arms"]:
        assert arm["excess_vs_index_300"] == \
            pytest.approx(arm["cum_return"] - idx_ret)
        if arm is now:
            assert arm["excess_vs_now"] is None      # 基准自身不给「相对我」
        else:
            assert arm["excess_vs_now"] == \
                pytest.approx(arm["cum_return"] - now["cum_return"])


def test_index_series_uses_start_date_as_base(db):
    """指数那条线的起点是起跑日收盘（与 `return_since_start` 同一个基）。"""
    data = _track(db)
    idx = data["index"]
    assert idx["base_date"] == data["start_date"]
    assert idx["base_level"] == BARS["sh000300"][START]
    assert idx["points"][0] == 0.0
    assert idx["points"][-1] == pytest.approx(idx["return_since_start"])
    assert data["dates"][0] == data["start_date"]


def test_anchor_point_comes_from_initial_nav(tmp_path):
    """起跑日锚点 = `initial_nav / initial_capital − 1`，不是写死的 0。"""
    path = _fixture_db(tmp_path)
    data = _track(path)
    c = connect(path)
    try:
        accounts = {a["account_id"]: a for a in paper_store.load_accounts(c)}
    finally:
        c.close()
    for arm in data["arms"]:
        row = accounts[arm["account_id"]]
        capital = float(json.loads(row["params_json"])["initial_capital"])
        expect = round(float(row["initial_nav"]) / capital - 1.0, 6)
        assert arm["anchor_cum_return"] == expect
        assert arm["points"][0] == expect
    # 起跑日收盘 87.23 > 成本 86.80 → 锚点必然是正的（证明它不是 0）
    assert _arm(data, "arm-now")["points"][0] > 0


def test_index_gap_breaks_the_line_instead_of_interpolating(tmp_path):
    """大盘某天没有收盘价 → 那个点是 `None`（断线），并**计数**。"""
    path = _fixture_db(tmp_path, index_gap=True)
    data = _track(path)
    idx = data["index"]
    assert idx["n_missing"] == 1
    assert idx["points"][data["dates"].index("2026-09-17")] is None
    svg = paper_render.race_svg(data["dates"], paper_render._races(data))
    # 指数线被切成两段（其余四条账户线各一段）→ 至少 5 条 polyline
    assert svg.count("<polyline") >= 5


def test_real_trade_separates_me_from_do_nothing_after_a_step(db):
    """账本记一笔真成交 → 下一次 `step` 起「我」与「什么都不做」分开。"""
    assert _track(db)["mirror_equals_hold"] is True
    c = connect(db)
    try:
        record_trade(c, date=NEXT, code="000333", side="buy",
                     price=80.90, qty=100, fee=5.0, now=NEXT_NOW)
    finally:
        c.close()
    # 只记成交、不 step：已落库的净值行**不会**变 —— 页面只读事实，不按账本重算
    assert _track(db, NEXT)["mirror_equals_hold"] is True
    c = connect(db)
    try:
        paper_engine.step(c, NEXT, now=NEXT_NOW)
    finally:
        c.close()
    data = _track(db, NEXT)
    assert data["date"] == NEXT
    assert data["mirror_equals_hold"] is False
    assert data["real_trades_after_start"] == 1
    assert _arm(data, "arm-now")["nav"] != _arm(data, "arm-hold")["nav"]
    # 页面要把重合的解释换成「已经分开」，不许还留着那句
    assert "完全重合" not in _html(db, NEXT)


def test_no_nav_rows_yet_is_an_honest_empty_state(tmp_path):
    """有账户、没有净值行 → `available=False`，页面明说「还没有」，不画线。"""
    path = _fixture_db(tmp_path, steps=False)
    data = _track(path)
    assert data["available"] is False and data["dates"] == []
    html = paper_render.paper_page(data, base="/lab", built_at=NOW)
    assert "还没有模拟盘净值" in html
    assert "<svg" not in html


# ---------- 渲染 ----------

def test_page_shows_every_line_and_the_labels_that_matter(db):
    html = _html(db)
    for arm_id in ARMS:
        assert arm_id in html
    assert paper_render.arm_label({"account_id": "arm-now", "arm": "now"}) in html
    assert "沪深300" in html and "sh000300" in html
    assert "AI 纪律臂 · ETF 目标 5%" in html
    # 「我 − 大盘」这个口径要在首屏出现，并且与上游一致
    data = _track(db)
    assert paper_render.ratio_pct(_arm(data, "arm-now")["excess_vs_index_300"]) in html


def test_unrecognised_arm_label_is_shown_verbatim(db):
    """认不出的账户 → 原样显示 id + 「口径未知」，不猜成最像的那条臂。"""
    _add_late_arm(db, account_id="arm-discipline-99", target=None)
    html = _html(db)
    assert "arm-discipline-99（口径未知）" in html
    assert paper_render.arm_style({"account_id": "arm-discipline-99"}) == \
        paper_render._UNKNOWN_STYLE
    # 同一张表里，认得出的新档位照样按 etf_target_pct 拼出人话
    _add_late_arm(db, account_id="arm-discipline-20", target=20.0)
    data = _track(db)
    assert paper_render.arm_label(_arm(data, "arm-discipline-20")) == \
        "AI 纪律臂 · ETF 目标 20%"


def test_arm_without_todays_nav_says_so_instead_of_leaving_a_blank(db):
    """某臂当天没净值 → 说明「当日无净值」并给出它最后一行。"""
    _add_late_arm(db, account_id="arm-discipline-99", target=20.0,
                  date="2026-09-18")
    data = _track(db)
    arm = _arm(data, "arm-discipline-99")
    assert arm["has_nav_on_display_date"] is False
    assert arm["latest_nav_date"] == "2026-09-18"
    assert arm["nav"] is None                      # 当日没有就是没有，不拿上一日冒充
    html = paper_render.compare_table(data)
    assert "当日无净值；最后一行 2026-09-18" in html
    assert re.search(r"s-unknown\">未知<", html)


def test_page_never_ranks_and_states_the_limits(db):
    """不做排名：不出现「推荐/最优/冠军」这类词；样本与 LIVE=0 明写在页上。"""
    html = _html(db)
    for banned in ("推荐", "最优", "冠军", "第一名"):
        assert banned not in html
    assert "不做排名" in html
    assert "LIVE 样本仍为 0" in html
    assert "并行对照，不排名" in html


def test_page_does_not_render_none_as_zero(db):
    """指数没有成本/回撤 —— 写「未知」，不写 0.00。"""
    html = _html(db)
    idx_row = html.split('class="mut"><td class="l"')[1].split("</tr>")[0]
    assert "未知" in idx_row
    assert "0.00" not in idx_row


def test_nav_rail_links_the_page():
    assert ("/paper", "模拟盘对照") in NAV_ITEMS


def test_route_serves_the_page(tmp_path, loopback_http):
    """真起服务打一次 `GET /lab/paper`（路由接线在 `app._get` 里）。"""
    from stocklab.labweb import app as labapp

    path = _fixture_db(tmp_path)
    server = labapp.make_server(host="127.0.0.1", port=0, db_path=path)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/lab/paper")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        assert res.status == 200
        assert "模拟盘对照" in body
        assert "arm-discipline-15" in body
        # 首页的导航条要能点到它
        conn.request("GET", "/lab/")
        assert 'href="/lab/paper"' in conn.getresponse().read().decode("utf-8")
    finally:
        conn.close()
        server.shutdown()
        server.server_close()


def test_chart_needs_two_sessions():
    """只有一个点时**不画线**，也不拿起点值铺一条平线充数。"""
    svg = paper_render.race_svg(["2026-09-21"],
                                [{"color": "#000", "dash": "", "width": 2,
                                  "points": [0.01]}])
    assert "<svg" not in svg
    assert "不画线" in svg


# ---------- 「AI 自己编排的东西，用上了没有」 ----------

def _add_active_script(path, *, plugin_id="1", version="9.9.9") -> int:
    """夹具里放一版 active 插桩脚本（走 submit → sandbox_pass → approve）。"""
    from stocklab.plugin import store as plugin_store

    c = connect(path)
    try:
        sid = plugin_store.insert_script(
            c, plugin_id=plugin_id, version=version,
            source_text="def score(ctx):\n    return 0.0\n", note="测试用", now=NOW)
        for action in ("submit", "sandbox_pass", "approve"):
            plugin_store.insert_audit(c, script_id=sid, action=action,
                                      actor="test", reason=None, now=NOW)
    finally:
        c.close()
    return sid


def test_ai_evidence_counts_are_read_from_the_db(tmp_path):
    """每个计数都是库里的行数；池 → plugin 的映射从打分内核**反查**。"""
    from stocklab.candidate import score

    path = _fixture_db(tmp_path)
    sid = _add_active_script(path)
    c = connect(path)
    try:
        ev = paper_data.ai_evidence(c, LAST)
        n_pred = c.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    finally:
        c.close()
    assert ev["counts"]["predictions"] == n_pred
    assert ev["counts"]["plugin_scripts"] == 1
    assert ev["by_state"] == {"active": 1}
    assert [s["script_id"] for s in ev["scripts"]] == [sid]
    assert {r["plugin_id"] for r in ev["routing"]} == \
        {score.INDUSTRY_SCREEN_PLUGIN, *score.POOL_PLUGIN.values()}
    routed = [r["label"] for r in ev["routing"]
              if r["active_script_id"] is not None]
    assert routed == ["short 池打分"], "夹具只给 plugin 1 配了 active 版本"
    assert next(r for r in ev["routing"] if r["label"] == "short 池打分")[
        "active_script_id"] == sid


def test_ai_evidence_reports_zero_consumption_when_it_consumes_nothing(db):
    """成交理由全在写死条文里、参数里没有 model/plugin 键 ⇒ 消费计数为 0。"""
    c = connect(db)
    try:
        ev = paper_data.ai_evidence(c, LAST)
    finally:
        c.close()
    cons = ev["consumption"]
    assert cons["n_accounts"] == len(ARMS)
    assert cons["n_trades"] > 0, "夹具没产生成交 —— 下面全是空断言"
    assert cons["unknown_rules"] == []
    assert cons["param_refs"] == []
    assert not [k for k in cons["param_keys"] if "plugin" in k or "model" in k]


def test_ai_evidence_flags_a_citation_outside_the_rule_book(db):
    """表外的触发理由必须被点名 —— 真接了模型/插桩，这一段会自己变非空。"""
    from stocklab.paper.config import RULE_CITATIONS

    novel = "模型臂：涨概率 > 0.55 → 买入"
    c = connect(db)
    try:
        c.execute(
            "INSERT INTO paper_trades (account_id, date, code, side, ref_price,"
            " fill_price, qty, commission, stamp_tax, transfer_fee, slippage_cost,"
            " fee_total, asset_class, rule_citation, reason, binding_json,"
            " price_source, price_asof, created_at)"
            " VALUES ('arm-now','2026-09-17','510300','buy',4.532,4.532,100,"
            " 5.0,0.0,0.0,0.0,5.0,'etf',?,'测试','[]','bars','2026-09-17',?)",
            (novel, NOW))
        c.commit()
        ev = paper_data.ai_evidence(c, LAST)
    finally:
        c.close()
    assert novel not in "\n".join(RULE_CITATIONS.values()), "该条文得是真表外的"
    assert ev["consumption"]["unknown_rules"] == [novel]
    html = paper_render.ai_block(ev)
    assert "已经接了已登记条文之外的东西" in html
    assert "模型与插桩没用上" not in html


def test_page_shows_the_ai_evidence_section(db):
    html = _html(db)
    assert "AI 自己编排的东西，用上了没有" in html
    assert "AI 的准确率" in html
    assert "模型与插桩没用上" in html
    assert "引用模型预测 0 条、引用插桩脚本 0 条" in html


def test_page_reports_ai_accuracy_from_rolling_accuracy(db):
    """页面上的准确率与 `rolling_accuracy` 同源（不重算、不手写）。"""
    from stocklab.session.review import rolling_accuracy

    c = connect(db)
    try:
        acc = rolling_accuracy(c, end_date=LAST)
    finally:
        c.close()
    html = _html(db)
    replay = acc["replay"]
    if replay is None:                      # 夹具里没有验证行
        assert "不是「准确率是 0」，是没有样本" in html
    else:
        assert f'{replay["direction_accuracy_daily"]:.4f}' in html
        assert f'{replay["brier_daily"]:.4f}' in html
    assert "LIVE 0 行" in html


def test_page_never_shows_escaped_markup_as_text(db):
    """`rich()` 先转义再替换 —— 页面里不得出现被转义成文本的标签（真踩过）。"""
    html = _html(db)
    for needle in ("&lt;a href", "&lt;span", "&lt;b&gt;", "&lt;code&gt;"):
        assert needle not in html, f"页面出现了被转义成文本的标签：{needle}"


def test_overlap_note_keeps_its_link_clickable(db):
    """两支重合时那句解释里的链接必须**可点**（曾经整句被转义成文本）。"""
    html = _html(db)
    assert "完全重合" in html
    assert 'href="/lab/trades"' in html
    assert "<code>arm-now</code>" in html


def test_index_warning_line_is_markup_not_text():
    data = {"index": {"base_level_missing": True}, "date": LAST,
            "now_account_id": "arm-now"}
    html = paper_render.glance_html(paper_render._why(data))
    assert '<span class="s-warn">' in html
    assert "&lt;span" not in html
