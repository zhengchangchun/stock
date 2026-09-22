"""P51 T6：报告 §5（实验台账）/ §6（数据缺口）搬上 `/data` 页。

判据是**同源**，不是「页面上有这段字」：

| 断言 | 被摘掉后会红的实现 |
|---|---|
| `Lab.data()` 的这两节 == 报告 `build_review` 的同名两节 | 页面自己再查一遍库（两份数字开始漂） |
| 页面渲染出的每个数字都能在取数结果里找到 | 渲染层重算（例如自己 `COUNT(*)` 一遍） |
| 台账为空 / 快照为空 → 「无」，不是空表 | 空表被读成「有这一节但没渲染出来」 |
| 报告里 §5/§6 仍在 | 「搬到页面」被做成「从报告删掉」 |

数字一律**从被测模块取**，不手抄一份到测试里。
"""

from __future__ import annotations

from stocklab.labweb.data import Lab
from stocklab.labweb.render import data_page, rich
from stocklab.session.review import build_review
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-22T16:00:00+08:00"
ASOF = "2026-09-22"


def _db(tmp_path, *, bars=3, amount_null=2, snapshots=0, experiments=0):
    """一份最小库：只塞这两节要数的东西。"""
    path = tmp_path / "data.db"
    init_db(path)
    c = connect(path)
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('000333','美的集团','sz','main','stock',?)", (NOW,))
    c.execute("INSERT INTO trading_calendar (date, is_open, source, created_at)"
              " VALUES ('2026-09-22',1,'tencent',?)", (NOW,))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, amount,"
        " adj_mode, source, fetched_at) VALUES ('000333',?,10,10,10,10,100,?,"
        "'none','x',?)",
        [("2026-09-2%d" % (i + 1), None if i < amount_null else 100.0, NOW)
         for i in range(bars)])
    for i in range(snapshots):
        c.execute("INSERT INTO quote_snapshots (code, trade_date, ts, price, volume,"
                  " source, fetched_at) VALUES ('000333','2026-09-22',?,10.0,1,"
                  "'tencent',?)", ("20260922103%02d" % i, NOW))
    for i in range(experiments):
        c.execute(
            "INSERT INTO experiment_decisions (variant_id, split, metric,"
            " metric_version, gate_status, decision, report_sha256, created_at)"
            " VALUES (?,'validate','direction','pit-rw-v1.0.2','WIN','promoted',"
            "'x',?)", (f"fin-prior-{i}", NOW))
    c.commit()
    c.close()
    return path


def _lab_data(path):
    return Lab(path, asof=ASOF).data()


def _report(path):
    c = connect(path)
    try:
        return build_review(c, ASOF)
    finally:
        c.close()


def _html(data) -> str:
    return data_page(data, base="/lab", built_at=NOW)


# ---------- 同源 ----------

def test_page_sections_are_the_reports_own_numbers(tmp_path):
    """页面这两节与报告 §5/§6 **逐字同源**：同一个取数函数、同一次结果。"""
    path = _db(tmp_path, experiments=2)
    data = _lab_data(path)
    rep = _report(path)
    assert data["experiments"] == rep["experiments"]
    assert data["gaps"] == rep["gaps"]


def test_rendered_page_carries_the_same_numbers(tmp_path):
    """渲染出来的每个数字都要能在取数结果里找到 —— 页面不重算。"""
    path = _db(tmp_path, experiments=1)
    data = _lab_data(path)
    html = _html(data)
    exp, gaps = data["experiments"], data["gaps"]
    assert f'{exp["rows"]}' in html
    for vid, info in exp["latest_by_variant"].items():
        assert vid in html
        assert str(info["last_decision_id"]) in html
        assert info["gate_status"] in html
    assert str(gaps["bars_daily_rows"]) in html
    assert str(gaps["amount_non_null"]) in html
    assert str(gaps["turnover_non_null"]) in html
    # 语义那句话随数字一起显示（页面上 `**`/`` ` `` 会被 `rich()` 转成真标记，
    # 所以按渲染后的形态比对 —— 比的是同一句话，不是同一串字节）
    assert rich(gaps["note"]) in html


def test_both_sections_are_titled_as_the_reports_sections(tmp_path):
    """标题里点明对应报告的哪一节 —— 读者要能拿页面去核对报告。"""
    html = _html(_lab_data(_db(tmp_path)))
    assert "实验台账" in html and "报告 §5" in html
    assert "数据缺口" in html and "报告 §6" in html


# ---------- 空数据 ----------

def test_empty_ledger_says_none_not_an_empty_table(tmp_path):
    """台账一行都没有 → 「无」，而不是一张只有表头的表。"""
    path = _db(tmp_path, experiments=0)
    data = _lab_data(path)
    assert data["experiments"]["rows"] == 0
    assert data["experiments"]["latest_by_variant"] == {}
    html = _html(data)
    # 判「无」而不是「页面上随便有个无字」——那一节自己的那句话必须在
    assert "无 —— 台账一行都没有。" in html
    assert "变体" not in html             # 空表不渲染（只有表头也是空表）


def test_never_collected_gap_shows_none_not_the_word_none(tmp_path):
    """从未有值的字段（首日 `None`）显示「无」，不许把 `None` 原样漏到页面上。"""
    path = _db(tmp_path, amount_null=3, snapshots=0)
    data = _lab_data(path)
    assert data["gaps"]["amount_first_date"] is None
    assert data["gaps"]["quote_snapshots"]["rows"] == 0
    html = _html(data)
    assert "None" not in html
    assert "首日 无" in html


# ---------- 报告仍保留那两节（搬上去 ≠ 从报告搬走） ----------

def test_the_report_keeps_its_own_sections(tmp_path):
    path = _db(tmp_path, experiments=1)
    from stocklab.session.review import render_markdown

    md = render_markdown(_report(path))
    assert "## 5. 实验台账现状" in md
    assert "## 6. 数据缺口清单" in md
    assert str(_lab_data(path)["gaps"]["bars_daily_rows"]) in md


def test_the_page_reads_no_new_write_path(tmp_path):
    """页面**只读**：取数前后库里各表行数逐表不变（不是「看起来没变」）。"""
    path = _db(tmp_path, experiments=1)
    tables = ("experiment_decisions", "bars_daily", "quote_snapshots",
              "system_events", "trading_calendar")

    def counts():
        c = connect(path)
        try:
            return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in tables}
        finally:
            c.close()

    before = counts()
    _html(_lab_data(path))
    assert counts() == before
