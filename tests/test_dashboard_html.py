"""P14 / Task 61：单文件 HTML —— 零外部依赖 + 诚实性版面。

「无外部引用」不是审美要求：看板要能在**离线**机器上双击打开，还要能挂在
nginx 子路径下。任何一条 CDN / 外部字体 / JS 框架都会让这两个前提同时失效。
"""

from __future__ import annotations

import re

import pytest

from stocklab.dashboard.html import (HARD_FACTS, esc, frac_pct, num, pct100,
                                     render_html)
from stocklab.dashboard.summary import build_summary

# 从 summary 的测试里复用同一套真实库（fixture 跨模块导入是 pytest 支持的用法）。
from tests.test_dashboard_summary import conn  # noqa: F401

BUILT_AT = "2026-09-15T16:00:00+08:00"


@pytest.fixture
def summary(conn):  # noqa: F811
    return build_summary(conn, "2026-09-14")


@pytest.fixture
def doc(summary):
    return render_html(summary, built_at=BUILT_AT)


# ---------- 零外部依赖 ----------

def test_html_has_no_external_reference(doc):
    """正则扫描三类外部引用，一个都不许有（含 SVG 的 xmlns —— 它含 `http://`）。"""
    assert "http://" not in doc
    assert "https://" not in doc
    assert "<script" not in doc.lower()
    assert "<link" not in doc.lower()
    assert "<iframe" not in doc.lower()
    assert not re.search(r"\bsrc\s*=", doc, re.I)      # 没有外链资源
    assert not re.search(r"@import|url\(", doc, re.I)  # CSS 里也没有外链


def test_html_is_a_complete_document(doc):
    assert doc.startswith("<!DOCTYPE html>")
    assert doc.rstrip().endswith("</html>")
    assert '<meta charset="utf-8">' in doc
    assert "<style>" in doc
    assert "<svg" in doc                                # 图形是 inline SVG


def test_html_is_deterministic_for_same_input(summary):
    a = render_html(summary, built_at=BUILT_AT)
    b = render_html(summary, built_at=BUILT_AT)
    assert a == b


def test_html_has_no_absolute_paths(summary):
    """页面内不出现绝对 URL/路径（`/api/summary` 这类硬编码会让子路径部署失效）。"""
    doc = render_html(summary, built_at=BUILT_AT)
    assert "127.0.0.1" not in doc
    assert "localhost" not in doc


# ---------- 诚实性版面 ----------

def test_no_bet_verdict_is_above_the_portfolio_section(summary):
    """`NO_BET` 与理由必须在**最显眼处**（组合明细之前）。"""
    block = {"verdict": "NO_BET", "verdict_label": "不下注（edge 不显著）",
             "verdict_reason": "p_used 0.3120 ≤ p_be 0.4880：保守胜率打不过盈亏平衡"}
    doc = render_html({**summary, "risk": block}, built_at=BUILT_AT)
    assert "NO_BET" in doc
    # 用**段落标题**做锚点：告警文本里也可能出现「组合视图」字样
    # （「组合视图的现价判定用的是…」），拿裸词比位置会假绿/假红。
    assert doc.index("NO_BET") < doc.index("<h2>组合视图</h2>")
    assert "edge 不显著" in doc
    assert "p_be" in doc


def test_risk_section_distinguishes_null_from_zero(summary):
    """`risk=None`（未接入）不许渲染成「风险为零」。"""
    doc = render_html(summary, built_at=BUILT_AT)
    assert "未接入" in doc
    assert "风险为零" in doc          # 原文里明确否掉这种读法


def test_html_declares_that_state_does_not_predict_direction(doc):
    assert "不预测方向" in doc
    assert "38.14%" in doc
    assert "样本不足，仅供观察" in doc or "样本不足" in doc


def test_html_marks_live_empty_explicitly(summary):
    """LIVE 桶为空时，页面必须说「没有实盘记录」而不是显示一个 0%。"""
    doc = render_html(summary, built_at=BUILT_AT)
    assert "一条实盘记录都没有" in doc


def test_html_shows_the_provenance_rule(doc):
    assert "created_at" in doc          # 判据原文进页面，供审计者逐字复核


def test_html_lists_alarms(summary):
    doc = render_html(summary, built_at=BUILT_AT)
    if summary["alarms"]:
        assert "需要人来看" in doc
    else:                                # 没告警就不该有告警框
        assert "需要人来看" not in doc


# ---------- 显示口径 ----------

def test_none_is_never_rendered_as_zero():
    assert num(None) == "—"
    assert frac_pct(None) == "—"
    assert pct100(None) == "—"
    # 真正的 0 该显示 0 —— 只是**不能**与「没有这个数」长得一样。
    assert num(0) == "0"          # int = 计数（股数/行数），无小数
    assert num(0.0) == "0.00"     # float = 金额/价格，两位小数
    assert frac_pct(0.0) == "0.00%"


def test_percent_helpers_do_not_mix_scales():
    """`weight` 存的是 43.41（已是百分数），`ci` 存的是 0.38（小数）—— 差 100 倍。"""
    assert pct100(43.41) == "43.41%"
    assert frac_pct(0.3814) == "38.14%"


def test_esc_escapes_html():
    assert esc("<b>&") == "&lt;b&gt;&amp;"
    assert esc(None) == ""


def test_hard_facts_are_present_in_the_page(doc):
    for fact in HARD_FACTS:
        assert fact[:20] in doc
