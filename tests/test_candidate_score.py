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


def test_build_ctx_contains_required_keys():
    ctx = score.build_ctx(STOCK, "short", BARS, asof="2026-09-10")
    assert ctx["code"] == "000333"
    assert ctx["name"] == "美的集团"
    assert ctx["asof"] == "2026-09-10"
    assert ctx["pool"] == "short"
    assert ctx["asset_type"] == "stock"
    assert len(ctx["bars"]) == 10


def test_build_ctx_is_pit():
    """asof 之后的行不得进 ctx。"""
    ctx = score.build_ctx(STOCK, "short", BARS, asof="2026-09-05")
    assert all(b["date"] <= "2026-09-05" for b in ctx["bars"])


def test_build_ctx_bars_are_json_safe_plain_dicts():
    ctx = score.build_ctx(STOCK, "short", BARS, asof="2026-09-10")
    json.dumps(ctx)                      # 不抛 = 可序列化


def test_industry_screen_passes(conn):
    ctx = score.build_ctx(STOCK, "short", BARS, asof="2026-09-10")
    r = score.industry_screen(conn, STOCK, ctx)
    assert r["pass_flag"] is True


def test_score_pool_short(conn):
    ctx = score.build_ctx(STOCK, "short", BARS, asof="2026-09-10")
    out = score.score_pool(conn, STOCK, "short", ctx)
    assert out.raw_score == 80.0
    assert out.pass_flag is True
    assert out.risk_list == ("高波动",)


def test_score_pool_mid(conn):
    ctx = score.build_ctx(STOCK, "mid", BARS, asof="2026-09-10")
    assert score.score_pool(conn, STOCK, "mid", ctx).raw_score == 60.0


def test_score_pool_long_fails_flag(conn):
    ctx = score.build_ctx(STOCK, "long", BARS, asof="2026-09-10")
    out = score.score_pool(conn, STOCK, "long", ctx)
    assert out.raw_score == 40.0
    assert out.pass_flag is False


def test_missing_active_plugin_raises(tmp_db):
    """没有任何 active 版本时，主流程必须明确报错，不兜底。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    ctx = score.build_ctx(STOCK, "short", BARS, asof="2026-09-10")
    with pytest.raises(lifecycle.NoActivePlugin):
        score.score_pool(c, STOCK, "short", ctx)
    c.close()
