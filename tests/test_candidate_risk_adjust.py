"""Task 12：步骤8 风险权重修正（调插桩4）。"""

import pytest

from stocklab.candidate import risk_adjust
from stocklab.candidate.score import ScoreOutcome
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
OUT = ScoreOutcome(code="000333", pool="short", raw_score=80.0,
                   pass_flag=True, reason="量价好", risk_list=("高波动",))


def _install(conn, script, version="1.0.0"):
    sid = store.insert_script(conn, plugin_id="4", version=version,
                              source_text=script, note=None, now=NOW)
    lifecycle.record_submit(conn, sid, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, sid, actor="t", reason="ok", now=NOW)
    return sid


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def test_adjust_applies_deduction(conn):
    _install(conn, "def run(ctx):\n"
                   "    return {'final_score': ctx['raw_score'] - 5.0,"
                   " 'risk_out': ctx['risk_list']}\n")
    out = ScoreOutcome(code="000333", pool="short", raw_score=80.0,
                       pass_flag=True, reason="r", risk_list=("高波动",))
    final, risks = risk_adjust.adjust(conn, out, {})
    assert final == 75.0
    assert risks == ["高波动"]


def test_adjust_sees_raw_score_and_risks_in_ctx(conn):
    """插桩4 的输入必须来自 ScoreOutcome，而非调用方 ctx —— 传冲突值才能证伪。"""
    _install(conn, "def run(ctx):\n"
                   "    return {'final_score': ctx['raw_score'],"
                   " 'risk_out': ctx['risk_list']}\n")
    # 调用方传入与 OUT 冲突的值：若实现从 caller ctx 读取，断言将看到 0.0 / []
    final, risks = risk_adjust.adjust(conn, OUT, {"raw_score": 0.0, "risk_list": []})
    assert final == 80.0      # 必须来自 OUT.raw_score，不是调用方的 0.0
    assert risks == ["高波动"]  # 必须来自 OUT.risk_list，不是调用方的 []


def test_adjust_output_is_clamped_by_contract_not_here(conn):
    """插桩4 返回越界分数 → 契约层拒绝，本模块不替它修。"""
    _install(conn, "def run(ctx):\n"
                   "    return {'final_score': 999.0, 'risk_out': []}\n")
    from stocklab.plugin.contract import PluginContractError
    with pytest.raises(PluginContractError):
        risk_adjust.adjust(conn, OUT, {})


def test_missing_active_raises(conn):
    with pytest.raises(lifecycle.NoActivePlugin):
        risk_adjust.adjust(conn, OUT, {})


def test_ctx_passed_through(conn):
    """OutcomeContract 键必须来自 ScoreOutcome；非契约键（如 asof）来自调用方 ctx。"""
    _install(conn, "def run(ctx):\n"
                   "    return {'final_score': 50.0,"
                   " 'risk_out': [ctx['code'] + '@' + ctx['asof']]}\n")
    # 传入与 OUT.code 冲突的值；若实现从 caller ctx 读取 code，将看到 "999999"
    _, risks = risk_adjust.adjust(conn, OUT, {"code": "999999",
                                              "asof": "2026-09-17"})
    assert risks == ["000333@2026-09-17"]  # code 来自 OUT，asof 来自 caller ctx
