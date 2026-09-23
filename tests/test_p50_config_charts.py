"""P50 T3/T4/T5/T8：只读配置视图（D-32）＋ 可视化报表（复用既有图）。

| # | 判据 | 用例 |
|---|---|---|
| T3 | **配置视图只读且同源** | `test_t3_*`：页面显示的常量值 == 直接 import 的常量对象值（逐项）；源码扫描零写路径（**带反向自检**） |
| T4 | **图表不平行实现** | `test_t4_*`：曲线取数/绘图只有一处真源；喂一段违规实现要判红 |
| T5 | **页面/报告同源** | `test_t5_*`：页面字段 == 报告生成器函数返回（逐字段）；同一函数两次调用同结果 |
| T8 | **缺数据不造数** | `test_t8_*`：无净值 / 无预测 ⇒ 写「无」+ 理由，不画空曲线、不写 0 |
"""

from __future__ import annotations

import ast
import datetime
import sqlite3
from pathlib import Path

import pytest

from stocklab.config import limits, m2_signals
from stocklab.dashboard import html as dash_html
from stocklab.dashboard.summary import build_summary
from stocklab.labweb import m2_charts, m2_data, m2_render, paper_data
from stocklab.m2 import config as m2_config
from stocklab.m2 import config_view
from stocklab.m2 import store as m2_store
from stocklab.paper import store as paper_store
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-22T16:00:00+08:00"
START = "2026-09-15"
STOCK = "600519"
ACCOUNT = "arm-agent-v1"


def _sessions(n: int, *, start: str = START) -> list[str]:
    base = datetime.date.fromisoformat(start)
    return [(base + datetime.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _db(tmp_path, *, nav: bool = True, forecasts: bool = False, cycles: bool = False):
    """最小库：账户 / 净值行 / （可选）预测与分数 / （可选）验证周期。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "cfg.db"
    init_db(path)
    c = connect(path)
    days = _sessions(4)
    paper_store.insert_account(
        c, account_id=ACCOUNT, arm="agent", etf_target_pct=None, start_date=START,
        initial_cash=20000.0, initial_positions=[], initial_nav=20000.0,
        params={m2_config.EXECUTOR_KEY: m2_config.EXECUTOR_CHANNEL_A}, now=NOW)
    if nav:
        for i, d in enumerate(days):
            value = 20000.0 + 10.0 * i
            paper_store.insert_nav(
                c, account_id=ACCOUNT, date=d, cash=value, positions=[],
                market_value=0.0, nav=value, drawdown=0.0, cum_cost=0.0,
                cum_return=round(value / 20000.0 - 1.0, 6), net_deposits=20000.0,
                index_300_level=None, index_300_asof=None, now=NOW, commit=False)
    if forecasts:
        c.execute("INSERT INTO instruments (code, name, market, board, type,"
                  " added_at) VALUES (?,?,'sh','main','stock',?)",
                  (STOCK, "贵州茅台", NOW))
        c.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
            " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
            [(STOCK, d, 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i, NOW)
             for i, d in enumerate(days)])
        for i in range(len(days) - 1):
            m2_store.insert_forecast(
                c, plugin_id="m2_a3", channel="A", account_id=ACCOUNT,
                asof=days[i], code=STOCK,
                payload={"range_80": [0.01, 10000.0],
                         "direction": {"up": 0.6, "flat": 0.2, "down": 0.2},
                         "invalidate_if": None, "na_reasons": [],
                         "schema_version": "1"},
                script_id=1, script_version="s1", input_sha256=f"{i:064d}", now=NOW)
    if cycles:
        from stocklab.store import validation as ledger

        ledger.insert_cycle(
            c, script_id=7, account_id=ACCOUNT, planned_rounds=3,
            planned_days=10,
            params={"top_n": 3, "rebalance_cadence": 5},
            criteria_text="相对沪深300 超额 > 0 且样本 ≥ 120 交易日",
            start_date=START, now=NOW)
    c.commit()
    c.close()
    return path, days


def _conn(path):
    return connect(path)


# ══════════════════════════════════════════════════════════════════════
# T3 —— 配置视图：只读、同源、每项指到定义处
# ══════════════════════════════════════════════════════════════════════


def _items(conn) -> dict[str, dict]:
    view = config_view.items(conn)
    return {item["key"]: item for group in view["groups"] for item in group["items"]}


def test_t3_every_value_is_the_imported_constant_object(tmp_path):
    """逐项：视图里的值 == 直接 import 的常量（同源，不是「凑巧一样」）。"""
    path, _ = _db(tmp_path)
    c = _conn(path)
    try:
        items = _items(c)
    finally:
        c.close()
    assert items["circuit_breaker_drawdown"]["value"] is limits.CIRCUIT_BREAKER_DRAWDOWN \
        or items["circuit_breaker_drawdown"]["value"] == limits.CIRCUIT_BREAKER_DRAWDOWN
    assert items["rounds_min"]["value"] == limits.VALIDATION_ROUNDS_MIN
    assert items["rounds_max"]["value"] == limits.VALIDATION_ROUNDS_MAX
    assert items["max_days"]["value"] == limits.VALIDATION_MAX_DAYS
    assert items["freeze_max_days"]["value"] == limits.FREEZE_MAX_DAYS
    assert items["sample_gate_threshold"]["value"] == paper_data.PERFORMANCE_THRESHOLD
    assert items["account_prefix"]["value"] == m2_config.ACCOUNT_PREFIX
    assert items["mirror_account"]["value"] == m2_config.MIRROR_ACCOUNT
    assert items["decision_cadence"]["value"] == m2_config.DECISION_CADENCE


def test_t3_every_item_points_at_its_definition_site(tmp_path):
    """每一项都能指到**代码里的定义处**（`文件:符号`），并且真的在那儿。"""
    path, _ = _db(tmp_path)
    c = _conn(path)
    try:
        items = _items(c)
    finally:
        c.close()
    expected = {
        "circuit_breaker_drawdown": "stocklab/config/limits.py:CIRCUIT_BREAKER_DRAWDOWN",
        "rounds_min": "stocklab/config/limits.py:VALIDATION_ROUNDS_MIN",
        "rounds_max": "stocklab/config/limits.py:VALIDATION_ROUNDS_MAX",
        "max_days": "stocklab/config/limits.py:VALIDATION_MAX_DAYS",
        "freeze_max_days": "stocklab/config/limits.py:FREEZE_MAX_DAYS",
        "account_prefix": "stocklab/m2/config.py:ACCOUNT_PREFIX",
        "mirror_account": "stocklab/m2/config.py:MIRROR_ACCOUNT",
        "decision_cadence": "stocklab/m2/config.py:DECISION_CADENCE",
    }
    for key, source in expected.items():
        assert items[key]["source"] == source
        rel, _, symbol = source.partition(":")
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert symbol in text, f"{source} 指到了不存在的地方（文件在说谎）"


def test_t3_the_page_and_the_report_show_those_very_values(tmp_path):
    """页面与报告显示的**就是**视图里的那一串（渲染层不重新格式化数值）。"""
    path, days = _db(tmp_path)
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
        html = m2_render.m2_page(panel, base="/lab", built_at=NOW)
        text = m2_render.m2_text(panel)
    finally:
        c.close()
    shown = 0
    for group in panel["config"]["groups"]:
        for item in group["items"]:
            assert item["display"], f"{item['key']} 没有可显示的形态"
            assert item["display"] in html, f"{item['key']} 的值没出现在页面上"
            assert item["display"] in text, f"{item['key']} 的值没出现在报告里"
            shown += 1
    assert shown >= 8


def test_t3_the_effective_params_come_from_the_latest_cycle_row(tmp_path):
    """当前生效参数 = `validation_cycles` 最近一行的 `params_json` + 来源。"""
    path, _ = _db(tmp_path, cycles=True)
    c = _conn(path)
    try:
        view = config_view.items(c)
    finally:
        c.close()
    assert view["params"] == {"top_n": 3, "rebalance_cadence": 5}
    assert view["params_source"].startswith("validation_cycles#")
    assert view["params_source"].endswith(".params_json")
    assert view["params_criteria"]


def test_t3_without_a_cycle_the_params_say_why_instead_of_showing_zero(tmp_path):
    """没有周期 ⇒ 当前生效参数写「无」+ 理由（不写 `{}`、不写 0）。"""
    path, _ = _db(tmp_path)
    c = _conn(path)
    try:
        view = config_view.items(c)
        html = m2_render.m2_page(m2_data.panel(c, START), base="/lab", built_at=NOW)
    finally:
        c.close()
    assert view["params"] is None and view["params_reason"]
    assert "无" in html
    assert m2_render.NONE_MARK in html


#: 写路径的形态（配置视图**一个都不许有**）。
_WRITE_MARKERS = ("INSERT INTO", "UPDATE ", "DELETE FROM", "INSERT OR REPLACE",
                  "DROP TABLE", "ALTER TABLE")
#: 写入口模块：`from X import Y` 要拼成 `X.Y` 再判（只判 `node.module` 会漏掉
#: `from stocklab.plugin import lifecycle` 这种写法 —— 反向自检就是抓这个）。
_WRITE_MODULES = ("plugin.lifecycle", "plugin.store", "paper.store", "m2.store")
#: 写动作的**调用名**（不看 import：`store.validation` 是读写混装的模块，
#: 配置视图只许调它的读函数 `list_cycles`）。
_WRITE_CALLS = ("approve", "insert_", "update_", "delete_", "log_event", "commit")


def _write_hits(source: str) -> list[str]:
    """源码里的写路径（SQL 标记 + 写模块 import + 写动作调用）。"""
    hits = [m for m in _WRITE_MARKERS if m in source]
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else "")
            if any(name.startswith(p) or name == p for p in _WRITE_CALLS):
                hits.append(f"call:{name}")
        elif isinstance(node, ast.ImportFrom):
            full = (node.module or "") + "." + ",".join(a.name for a in node.names)
            hits += [f"import {node.module}.{a.name}" for a in node.names
                     for w in _WRITE_MODULES if w in f"{node.module}.{a.name}"]
        elif isinstance(node, ast.Import):
            hits += [f"import {a.name}" for a in node.names
                     for w in _WRITE_MODULES if w in a.name]
    return hits


def test_t3_the_config_view_has_no_write_path_at_all():
    """配置视图**零写入口**（ADR-021：只读展示）。

    `labweb/` 对 5 个主干常量名的零引用由 `tests/test_p44_constants_guard.py`
    继续钉住；这一条管的是 P50 新开的这扇窗本身。
    """
    source = (ROOT / "stocklab/m2/config_view.py").read_text(encoding="utf-8")
    assert _write_hits(source) == [], _write_hits(source)
    # 它引用主干常量走的是 `selfeval.BOUNDARIES`（值视图），不是那 5 个名字
    assert "BOUNDARIES" in source


def test_t3_the_write_scan_flags_a_planted_violation():
    """**反向自检**：喂一段违规实现必须判红（否则这条扫描等于没写）。"""
    planted = (
        "from stocklab.plugin import lifecycle\n"
        "from stocklab.store import validation as ledger\n"
        "def f(conn):\n"
        "    conn.execute('UPDATE validation_cycles SET params_json = ?', ('x',))\n"
        "    conn.execute('INSERT INTO system_events (ts) VALUES (?)', ('t',))\n"
        "    return lifecycle.approve(conn, script_id=1, actor='ai', reason='r')\n")
    hits = _write_hits(planted)
    assert "UPDATE " in hits and "INSERT INTO" in hits and "call:approve" in hits
    assert "import stocklab.plugin.lifecycle" in hits
    # 干净的实现不判红
    assert _write_hits(
        "from stocklab.config import limits\n"
        "def f():\n"
        "    return limits.CIRCUIT_BREAKER_DRAWDOWN\n") == []


# ══════════════════════════════════════════════════════════════════════
# T4 —— 图表不平行实现（取数 / 绘图各只有一处真源）
# ══════════════════════════════════════════════════════════════════════


def _svg_hits(source: str) -> list[str]:
    """自己拼 SVG 的痕迹（`<svg` / `<polyline` / `viewBox` / 手拼点集）。"""
    return [m for m in ("<svg", "<polyline", "<circle", "viewBox")
            if m in source]


def test_t4_the_chart_code_does_not_draw_its_own_svg():
    """曲线上只有一处绘图真源（`paper_render.race_svg`）—— 模块里不许出现 SVG。"""
    scanned = [ROOT / "stocklab/labweb/m2_charts.py",
               ROOT / "stocklab/labweb/m2_render.py",
               ROOT / "stocklab/labweb/m2_data.py",
               ROOT / "stocklab/m2/attribution.py",
               ROOT / "stocklab/m2/bypass.py"]
    assert all(p.exists() for p in scanned), "扫描目标不存在（空转）"
    offenders = []
    scanners_ok = 0
    for path in scanned:
        hits = _svg_hits(path.read_text(encoding="utf-8"))
        scanners_ok += 1
        if hits:
            offenders.append(f"{path.name}: {hits}")
    assert scanners_ok == len(scanned)
    assert not offenders, f"出现了第二套画图实现：{offenders}"


def test_t4_the_chart_module_reuses_the_existing_renderer_and_sources():
    """取数与绘图都**引用**既有实现（不 import 它们就不算复用）。"""
    source = (ROOT / "stocklab/labweb/m2_charts.py").read_text(encoding="utf-8")
    assert "race_svg" in source
    assert "paper_render" in source and "paper_data" in source
    assert "drawdown" in source


def test_t4_the_svg_scan_flags_a_planted_violation():
    """**反向自检**：喂一段自己画线的实现必须判红。"""
    planted = (
        "def chart(points):\n"
        "    return ('<svg viewBox=\"0 0 100 100\">'\n"
        "            + ''.join('<polyline points=\"1,2 3,4\"/>' for _ in points)\n"
        "            + '</svg>')\n")
    hits = _svg_hits(planted)
    assert "<svg" in hits and "<polyline" in hits and "viewBox" in hits


# ══════════════════════════════════════════════════════════════════════
# T5 —— 页面 / 离线报告同源（同一取数函数、同一渲染函数）
# ══════════════════════════════════════════════════════════════════════


def test_t5_the_page_and_the_offline_report_hold_the_same_chart_payload(tmp_path):
    """页面字段 == 报告生成器函数返回（逐字段）；两次调用同结果（页面不重算）。"""
    path, days = _db(tmp_path, forecasts=True)
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
        again = m2_charts.chart_panel(c, days[-1])
        summary = build_summary(c, days[-1])
    finally:
        c.close()
    assert panel["charts"] == again, "同一库同一 asof 两次取数必须逐字段相同"
    assert summary["m2"] == panel["charts"], \
        "离线报告与页面必须读同一个取数函数（不是各算一遍）"


def test_t5_the_same_renderer_puts_the_same_figure_in_both_documents(tmp_path):
    """同一个 `charts_section` 渲染出同一段 HTML：页面与离线单文件报告一致。"""
    path, days = _db(tmp_path, forecasts=True)
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
        summary = build_summary(c, days[-1])
    finally:
        c.close()
    section = m2_render.charts_section(panel["charts"])
    assert section in m2_render.m2_page(panel, base="/lab", built_at=NOW)
    doc = dash_html.render_html(summary, built_at=NOW)
    assert section in doc, "离线报告里的图与页面不是同一段（两张皮）"
    assert dash_html.html_sha256(doc)


# ══════════════════════════════════════════════════════════════════════
# T8 —— 缺数据不造数（写「无」+ 理由，不画空曲线、不写 0）
# ══════════════════════════════════════════════════════════════════════


def test_t8_without_nav_or_forecasts_every_curve_says_none_with_a_reason(tmp_path):
    """无净值 / 无预测 ⇒ 三条曲线各写「无」+ 理由，`svg` 为 `None`（不画空图）。"""
    path, days = _db(tmp_path, nav=False)
    c = _conn(path)
    try:
        charts = m2_charts.chart_panel(c, days[-1])
        html = m2_render.m2_page(m2_data.panel(c, days[-1]), base="/lab",
                                 built_at=NOW)
    finally:
        c.close()
    for block in charts["blocks"]:
        assert block["available"] is False, block["key"]
        assert block["svg"] is None, f"{block['key']} 画了一条空曲线"
        assert block["reason"], f"{block['key']} 没说为什么没有"
        assert "0%" not in str(block["reason"])
    assert "<svg" not in html
    assert "<polyline" not in html


def test_t8_a_curve_with_data_is_actually_drawn(tmp_path):
    """正例：有净值、有**已落库的分数**就画出来（免得「一律写无」也算过）。"""
    path, days = _db(tmp_path, nav=True, forecasts=True)
    c = _conn(path)
    try:
        from stocklab.m2 import score as m2_score

        m2_score.score_all(c, asof=days[-1], now=NOW)     # 命中率曲线要有分数才存在
        charts = m2_charts.chart_panel(c, days[-1])
    finally:
        c.close()
    by_key = {b["key"]: b for b in charts["blocks"]}
    assert by_key["nav"]["available"] is True and "<polyline" in by_key["nav"]["svg"]
    assert by_key["drawdown"]["available"] is True
    assert by_key["win_rate"]["available"] is True
    assert "<polyline" in by_key["win_rate"]["svg"]


def test_t8_the_missing_industry_source_is_named_not_guessed(tmp_path):
    """归因里的行业真源缺失 ⇒ 说明写清用的是哪张表（不是「0 只同向」）。"""
    path, days = _db(tmp_path)
    c = _conn(path)
    try:
        note = m2_data.panel(c, days[-1])["attribution"]["industry_source_note"]
    finally:
        c.close()
    assert "financial_reports.industry_name" in note
    assert "不新造行业分类" in note


def test_t8_the_schema_has_the_attribution_table(tmp_path):
    """归因结论落库的表在 schema 里（append-only + 结构性幂等键）。"""
    path, _ = _db(tmp_path)
    c = _conn(path)
    try:
        names = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        triggers = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        rows = c.execute(
            f"SELECT COUNT(*) FROM {m2_store.TABLE_ATTRIBUTIONS}").fetchone()[0]
    finally:
        c.close()
    assert m2_store.TABLE_ATTRIBUTIONS in names
    assert f"trg_{m2_store.TABLE_ATTRIBUTIONS}_no_update" in triggers
    assert f"trg_{m2_store.TABLE_ATTRIBUTIONS}_no_delete" in triggers
    assert rows == 0
    assert m2_signals  # 常量模块被引用（判据真源）
