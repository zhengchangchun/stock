"""P84 / T5：页面上不再有认不出的账户 id（`arm_names` 接进渲染层）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 页面上出现「AI操盘手·三版」这类显示名 | 渲染层硬编码旧标签 |
| `_LABELS` / `_DISPLAY_ORDER` 来自 `arm_names`（唯一真源） | 两份列表各自维护 |
| `paper show` **stdout 的 JSON 键集零变更** | 顺手给 stdout 加了 `display` |
| `paper show` 的**人类摘要**（stderr）带显示名 | 终端里认不出是哪个账户 |
| 认不出的账户仍原样显示 id + 口径未知（不 500） | 新版本账户让页面炸掉 |

「CLI stdout 的 JSON 键集逐键不变」那条用的是 **HEAD 实测冻结的键集**
（`git stash push -- stocklab/cli/main.py` 后跑 `paper show` 抄下来的），
不是从现役实现里取 —— 后者是同义反复。
"""

import json

import pytest

from stocklab.cli.main import main
from stocklab.labweb import m2_data, paper_data, paper_render
from stocklab.paper import arm_names, store as paper_store
from stocklab.paper.config import ARM_KIND_AGENT, ARM_AGENT
from stocklab.store.db import connect
from tests.test_labweb_paper import LAST, NOW, START, _fixture_db

#: HEAD（`d934fde` 前的 `paper show` 实现）实测的 stdout 键集，逐键抄下来。
HEAD_SHOW_TOP_KEYS = ("accounts", "agent", "asof", "asof_source", "comparison",
                      "disclosure", "index_300", "latest_nav_date")
HEAD_SHOW_ACCOUNT_KEYS = ("account_id", "agent_decision", "arm", "cash", "cum_cost",
                          "cum_return", "date", "decisions", "discipline",
                          "drawdown", "etf_target_pct", "evaluation", "live",
                          "market_value", "marks", "nav", "nav_history_points",
                          "net_deposits", "positions", "profit_cny")


@pytest.fixture
def db(tmp_path):
    return _fixture_db(tmp_path)


def _add_arm(path, account_id, *, nav=1000.0, date="2026-09-21"):
    c = connect(path)
    try:
        paper_store.insert_account(
            c, account_id=account_id, arm=ARM_KIND_AGENT, etf_target_pct=None,
            start_date=START, initial_cash=nav, initial_positions=[],
            initial_nav=nav, params={"initial_capital": nav}, now=NOW)
        paper_store.insert_nav(
            c, account_id=account_id, date=date, cash=nav, positions=[],
            market_value=0.0, nav=nav, drawdown=0.0, cum_cost=0.0, cum_return=0.0,
            net_deposits=nav, index_300_level=None, index_300_asof=None, now=NOW)
    finally:
        c.close()


def _page(path, asof=LAST):
    from stocklab.store.db import connect
    c = connect(path)
    try:
        data = paper_data.track(c, asof)
    finally:
        c.close()
    return paper_render.paper_page(data, base="", built_at=NOW)


# ---------- ① 名字只有一份真源 ----------

def test_t5_the_render_tables_come_from_the_single_source():
    assert paper_render._LABELS == {
        "arm-now": arm_names.display_name("arm-now"),
        "arm-hold": arm_names.display_name("arm-hold")}
    assert paper_render._DISPLAY_ORDER == arm_names.DISPLAY_ORDER


def test_t5_known_accounts_render_as_display_names():
    for aid, row in arm_names.ARM_NAMES.items():
        label = paper_render.arm_label(
            {"account_id": aid, "arm": "agent", "model_id": "m/x",
             "executor": "agent_decision"})
        assert aid not in label or row["name"] in label, (aid, label)
    # 家族新版本（映射表里没有）也不掉进「口径未知」
    assert "口径未知" not in paper_render.arm_label(
        {"account_id": "arm-agent-ds-v9", "arm": "agent"})


def test_t5_the_page_shows_the_chinese_display_name(db):
    _add_arm(db, "arm-agent-ds-v3")
    html = _page(db)
    assert "AI操盘手·三版" in html
    # 机器名仍在（`<code>` 里，对账要用）—— 身份与显示名是两件事
    assert "<code>arm-agent-ds-v3</code>" in html


def test_t5_m2_data_labels_use_the_display_name():
    assert m2_data.SIDE_LABELS[m2_data.SIDE_AI] == "AI 模拟（AI操盘手·<策略版本>）"
    assert m2_data.SIDE_LABELS[m2_data.SIDE_MIRROR] == "人工镜像（你的实盘镜像）"
    # 基准那两条本来就没有裸臂 id（它们是指数），一字不动
    assert m2_data.benchmark_label("sh000300") == "市场基准（沪深300 指数）"


# ---------- ② CLI：stdout 键集零变更、stderr 带显示名 ----------

def _run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def test_t5_paper_show_stdout_json_keyset_is_unchanged(db, capsys):
    code, out, _ = _run(db, "paper", "show", "--asof", LAST, capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert tuple(sorted(payload)) == HEAD_SHOW_TOP_KEYS
    assert payload["accounts"], "夹具里必须至少有账户，否则这条断言是空转"
    for account in payload["accounts"]:
        assert tuple(sorted(account)) == HEAD_SHOW_ACCOUNT_KEYS
        assert "display" not in account, "stdout 一个字都不许加（K7）"


def test_t5_paper_show_stderr_summary_carries_the_display_name(db, capsys):
    _add_arm(db, "arm-agent-ds-v3")
    code, _, err = _run(db, "paper", "show", "--asof", LAST, capsys=capsys)
    assert code == 0
    summary = json.loads(err.strip().splitlines()[-1])
    by_id = {a["account_id"]: a["display"] for a in summary["accounts"]}
    assert by_id["arm-agent-ds-v3"] == "AI操盘手·三版"
    assert by_id["arm-now"] == "你的实盘镜像"
    assert by_id[ARM_AGENT] == "智能体臂 · 旧世代"


def test_t5_paper_account_json_is_untouched_but_help_mentions_display_names(
        db, capsys):
    code, out, _ = _run(db, "paper", "account", "--arm", "arm-now", "--json",
                        capsys=capsys)
    assert code == 0
    assert "display" not in json.loads(out)
    main(["paper", "account", "--help"])
    assert "AI操盘手·三版" in capsys.readouterr().out
