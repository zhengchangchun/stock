"""Task 11：步骤7 打分（调插桩 0/1/2/3）。"""

import pytest
import json

from stocklab.candidate import score
from stocklab.config.universe import ASSET_STOCK, Instrument
from stocklab.data.models import Bar
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
STOCK = Instrument("000333", "美的集团", "sz", "main", ASSET_STOCK)


def _bar(date, close):
    return Bar(code="000333", date=date, open=close, high=close, low=close,
               close=close, volume=1000, amount=None, turnover=None,
               source="t", adj_mode="none")


BARS = [_bar(f"2026-09-{d:02d}", 10.0 + d) for d in range(1, 11)]

PLUGINS = {
    "0": ("def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n"),
    "1": ("def run(ctx):\n    return {'score': 80.0, 'pass_flag': True,"
          " 'reason': '量价好', 'risk_list': ['高波动']}\n"),
    "2": ("def run(ctx):\n    return {'score': 60.0, 'pass_flag': True,"
          " 'reason': '景气平', 'risk_list': []}\n"),
    "3": ("def run(ctx):\n    return {'score': 40.0, 'pass_flag': False,"
          " 'reason': '护城河弱', 'risk_list': []}\n"),
}


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    for pid, text in PLUGINS.items():
        sid = store.insert_script(c, plugin_id=pid, version="1.0.0",
                                  source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
    yield c
    c.close()


def test_build_ctx_contains_required_keys(conn):
    ctx = score.build_ctx(conn, STOCK, "short", BARS, asof="2026-09-10")
    assert ctx["code"] == "000333"
    assert ctx["name"] == "美的集团"
    assert ctx["asof"] == "2026-09-10"
    assert ctx["pool"] == "short"
    assert ctx["asset_type"] == "stock"
    assert len(ctx["bars"]) == 10


def test_build_ctx_is_pit(conn):
    """asof 之后的行不得进 ctx。"""
    ctx = score.build_ctx(conn, STOCK, "short", BARS, asof="2026-09-05")
    assert all(b["date"] <= "2026-09-05" for b in ctx["bars"])


def test_build_ctx_bars_are_json_safe_plain_dicts(conn):
    ctx = score.build_ctx(conn, STOCK, "short", BARS, asof="2026-09-10")
    json.dumps(ctx)                      # 不抛 = 可序列化


def test_industry_screen_passes(conn):
    ctx = score.build_ctx(conn, STOCK, "short", BARS, asof="2026-09-10")
    r = score.industry_screen(conn, STOCK, ctx)
    assert r["pass_flag"] is True


def test_score_pool_short(conn):
    ctx = score.build_ctx(conn, STOCK, "short", BARS, asof="2026-09-10")
    out = score.score_pool(conn, STOCK, "short", ctx)
    assert out.raw_score == 80.0
    assert out.pass_flag is True
    assert out.risk_list == ("高波动",)


def test_score_pool_mid(conn):
    ctx = score.build_ctx(conn, STOCK, "mid", BARS, asof="2026-09-10")
    assert score.score_pool(conn, STOCK, "mid", ctx).raw_score == 60.0


def test_score_pool_long_fails_flag(conn):
    ctx = score.build_ctx(conn, STOCK, "long", BARS, asof="2026-09-10")
    out = score.score_pool(conn, STOCK, "long", ctx)
    assert out.raw_score == 40.0
    assert out.pass_flag is False


def test_missing_active_plugin_raises(tmp_db):
    """没有任何 active 版本时，主流程必须明确报错，不兜底。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    ctx = score.build_ctx(c, STOCK, "short", BARS, asof="2026-09-10")
    with pytest.raises(lifecycle.NoActivePlugin):
        score.score_pool(c, STOCK, "short", ctx)
    c.close()


# ---------- Task 9：features 与 sector ----------

FEATURE_KEYS = (
    "period", "roe", "roe_pct", "roe_n", "gross_margin", "gross_margin_pct",
    "gross_margin_n", "gm_yoy_pp", "gm_yoy_pp_pct", "gm_yoy_pp_n",
    "inv_days", "inv_days_pct", "inv_days_n", "fcf_margin", "fcf_margin_pct",
    "fcf_margin_n", "dupont", "na_reasons", "period_mixed", "asof",
)


@pytest.fixture
def fin_db(tmp_db):
    """一个只有标的、没有财报的库。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type, sector,"
              " added_at) VALUES ('000333','美的集团','sz','main','stock',"
              " '白色家电', ?)", (NOW,))
    c.commit()
    yield c
    c.close()


def test_features_keys_always_complete_without_data(fin_db):
    """没有财报时 features 的**键仍须齐全**，值为 None —— 否则插桩里
    ctx['features']['roe_pct'] 会 KeyError，被沙盒探针判成脚本 bug。"""
    ctx = score.build_ctx(fin_db, STOCK, "mid", BARS, asof="2026-09-17")
    assert set(ctx["features"]) == set(FEATURE_KEYS)
    assert ctx["features"]["roe"] is None
    assert ctx["features"]["roe_pct"] is None
    assert ctx["features"]["na_reasons"]


def test_sector_comes_from_instruments(fin_db):
    ctx = score.build_ctx(fin_db, STOCK, "mid", BARS, asof="2026-09-17")
    assert ctx["sector"] == "白色家电"


def test_ctx_is_json_serializable_with_features(fin_db):
    import json
    ctx = score.build_ctx(fin_db, STOCK, "mid", BARS, asof="2026-09-17")
    json.dumps(ctx)


def test_pit_excludes_unannounced_periods(fin_db):
    """notice_date > asof 的期绝不进 features（设计 §7.1）。"""
    fin_db.execute(
        "INSERT INTO financial_reports (code, report_date, notice_date,"
        " notice_date_source, report_type, total_assets, parent_equity,"
        " total_equity, total_liabilities, total_operate_income,"
        " parent_netprofit, source, fetched_at, created_at, raw_refs_json)"
        " VALUES ('000333','2026-06-30','2026-08-29','f10','中报',"
        " 1e11, 4e10, 4.4e10, 5.6e10, 5e10, 5e9, 'x', ?, ?, '[]')", (NOW, NOW))
    early = score.build_ctx(fin_db, STOCK, "mid", BARS, asof="2026-08-01")
    late = score.build_ctx(fin_db, STOCK, "mid", BARS, asof="2026-09-17")
    assert early["features"]["period"] is None, "公告日之前的期不许可见"
    assert late["features"]["period"] == "2026Q2"


def test_features_keys_complete_when_cross_section_omits_dupont(fin_db):
    """cross_section 不含 dupont 时，build_ctx 本地保证该键仍在 features 中。

    旧代码：只有 setdefault 循环（_pct/_n/period_mixed），dupont 完全依赖
    indicators.factors() 带进来。若 cross_section 覆写整个 feats（update 路径）
    且省略了 dupont，旧代码不会补它；新代码在循环后显式 setdefault("dupont", None)。

    这个测试会对旧代码的 dupont 缺失 KeyError 路径失败（falsifiable）。
    """
    from stocklab.plugin.contract import FEATURE_KEYS
    # cross_section dict 故意不含 dupont、roe、gross_margin 等，模拟来源数据残缺
    incomplete_xsec = {
        STOCK.code: {
            "roe_pct": 75.0, "roe_n": 10,
            "period_mixed": False,
        }
    }
    ctx = score.build_ctx(
        fin_db, STOCK, "mid", BARS, asof="2026-09-17",
        cross_section=incomplete_xsec,
    )
    missing = [k for k in FEATURE_KEYS if k not in ctx["features"]]
    assert missing == [], f"features 缺失键: {missing}"
    # dupont 是本次 fix 专门保证的键
    assert "dupont" in ctx["features"]
