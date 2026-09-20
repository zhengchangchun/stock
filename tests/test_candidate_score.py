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


def test_features_keys_complete_when_factors_omits_dupont(fin_db, monkeypatch):
    """indicators.factors() 不含 dupont 时，build_ctx 本地保证该键仍在 features 中。

    falsifiability：monkeypatch 掉 score.indicators.factors，令其返回一个不含
    dupont 的 dict（其余 base keys 保留，因 setdefault 循环只补 _pct/_n，不补 base）。
    update() 只加/覆写键、不删键；cross_section 也不会带 dupont 回来。
    唯一能把 dupont 补回来的只有 `feats.setdefault("dupont", None)`（fix round 1 那行）。
    预修复代码缺少该行 → dupont 不在 feats → 断言失败；
    修复后代码有该行 → dupont 在 feats → 断言通过。
    """
    from stocklab.plugin.contract import FEATURE_KEYS

    # 构造一个缺少 dupont（以及 fcf_margin 作为第二个 missing key）的 factors 返回值。
    # 注意：base keys（roe, gross_margin 等）不被 setdefault 循环覆盖，所以必须保留；
    # 只省略那些确实由 fix 的 setdefault("dupont", None) 负责的键。
    def _factors_missing_dupont(_reports):
        return {
            "period": None,
            "roe": None,
            "gross_margin": None,
            "gm_yoy_pp": None,
            "inv_days": None,
            "fcf_margin": None,
            # dupont 故意省略 —— 这是本测试验证的 fix 点
            "na_reasons": ["no data"],
        }

    monkeypatch.setattr(score.indicators, "factors", _factors_missing_dupont)

    ctx = score.build_ctx(fin_db, STOCK, "mid", BARS, asof="2026-09-17")
    missing = [k for k in FEATURE_KEYS if k not in ctx["features"]]
    assert missing == [], f"features 缺失键: {missing}"
    # dupont 是本次 fix 专门保证的键
    assert ctx["features"]["dupont"] is None
