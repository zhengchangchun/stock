"""P62 的历史行修复：`migrate_p62_agent_nav_settlement`。

真库那一行（`arm-agent-ds-v1 / 2026-09-23`）长这样（任务书 §1 的硬证据）：

```
cash=11320.0  market_value=0.0  nav=11320.0  cum_cost=9.2  cum_return=-0.434
positions_json=[{"code": "000333", "qty": 0}]          ← 现金是执行前的、持仓是执行后的
```

修法（append-only 纪律下）：**删**这一行 → 由 `paper agent run --asof <day>` 按修好的
`execute_decision` 重写；`paper_trades` 一行不删（成交是真的）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 可重入（跑两次第二次 `[]`） | 每次调用都删一遍 / 第二次报「没找到」 |
| 只删点名行、**其余行逐字节不变** | 顺手把别的缺陷行也删了 |
| 事后 append-only 触发器仍在 | 摘了触发器忘了挂回去 |
| 台账留在 `system_events`（旧值 + 依据文件名） | 静默删历史行 |
| 点名清单之外的**未结算**行 ⇒ 抛错、零改动 | 越界删行 |

夹具的数字**照抄真库那一行**（含 `params_json` 全文），不手编一个「看起来像」的。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from stocklab.store.db import connect
from stocklab.store.migrate import (init_db, migrate_p62_agent_nav_settlement,
                                    p62_defective_agent_nav_rows)

NOW = "2026-09-23T23:30:00+08:00"
START = "2026-09-15"
ASOF = "2026-09-23"

ARM = "arm-agent-ds-v1"
OTHER = "arm-agent"
HOLD = "arm-hold"

#: 真库 `paper_accounts` 里 `arm-agent-ds-v1` 那一行的**逐字段复制**。
REAL_PARAMS = {
    "decision_kind": "portfolio", "decision_source": "paper_agent_decisions",
    "etf_whitelist": ["510300", "510880"], "executor": "agent_decision",
    "initial_capital": 20000.0, "lot": 100, "per_step_cash_pct": 5.0,
    "preregistered": {
        "model_id": "deepseek/deepseek-v4-pro",
        "prompt_sha256": "231c12ad93d900583e4f8cce2993c70ee4697bd5485443c75ad44e8c826c6e06"},
    "seed_fee": 0.0, "start_date": START,
    "wired_from": "P56 / D-48：AI 操盘手版本账户（预注册模型与提示词指纹）",
}
BAD_ROW = {"cash": 11320.0, "positions_json": '[{"code": "000333", "qty": 0}]',
           "market_value": 0.0, "nav": 11320.0, "cum_cost": 9.2,
           "cum_return": -0.434, "net_deposits": 20000.0}
#: 真库那笔成交：`trade_id=12`，000333 sell 100 @82.4288，fee_total 9.2。
TRADE = (ARM, ASOF, "000333", "sell", 82.47, 82.4288, 100, 5.0, 4.12, 0.08, 0.0,
         9.2, "stock")
#: 另一条臂当天的净值行（干净，必须逐字节留下）：现金未动 + 000333×100 @82.47。
GOOD_ROW = {"cash": 11320.0, "positions_json": '[{"code": "000333", "qty": 100}]',
            "market_value": 8247.0, "nav": 19567.0, "cum_cost": 0.0,
            "cum_return": -0.02165, "net_deposits": 20000.0}


def _account(conn, account_id, arm, *, executor=None, params=None):
    p = dict(REAL_PARAMS if params is None else params)
    if executor is not None:
        p["executor"] = executor
    conn.execute(
        "INSERT INTO paper_accounts (account_id, arm, etf_target_pct, start_date,"
        " initial_cash, initial_positions_json, initial_nav, params_json, created_at)"
        " VALUES (?,?,NULL,?,?,?,?,?,?)",
        (account_id, arm, START, 11320.0, '[{"code": "000333", "qty": 100}]',
         20043.0, json.dumps(p, ensure_ascii=False, sort_keys=True), NOW))


def _nav(conn, account_id, row, date=ASOF):
    conn.execute(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,?,?,?,?,?,0.0,?,?,?,NULL,NULL,?)",
        (account_id, date, row["cash"], row["positions_json"], row["market_value"],
         row["nav"], row["cum_cost"], row["cum_return"], row["net_deposits"], NOW))


@pytest.fixture
def conn(tmp_path):
    """一份「跑过 P62 那一天」的库：一条未结算的 AI 臂行 + 两条别个臂的行。"""
    path = tmp_path / "p62-migrate.db"
    init_db(path)
    c = connect(path)
    c.row_factory = sqlite3.Row
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('000333','000333','sz','main','stock',?)", (NOW,))
    c.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
              " adj_mode, source, fetched_at) VALUES ('000333',?,82.47,82.47,82.47,"
              "82.47,100,'none','x',?)", (ASOF, NOW))
    _account(c, ARM, "agent")
    _account(c, OTHER, "agent")
    _account(c, HOLD, "hold", executor=None, params={**REAL_PARAMS, "seed_fee": 0.0})
    c.execute("INSERT INTO paper_trades (account_id, date, code, side, ref_price,"
              " fill_price, qty, commission, stamp_tax, transfer_fee, slippage_cost,"
              " fee_total, asset_class, rule_citation, reason, binding_json,"
              " price_source, price_asof, created_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'target_weight','卖光（夹具）','[]',"
              "'x',?,?)",
              (*TRADE, ASOF, NOW))
    _nav(c, ARM, BAD_ROW)
    _nav(c, OTHER, GOOD_ROW)
    _nav(c, HOLD, {"cash": 11320.0, "positions_json": '[{"code": "000333", "qty": 100}]',
                   "market_value": 8247.0, "nav": 19567.0, "cum_cost": 0.0,
                   "cum_return": 0.0, "net_deposits": 20000.0})
    c.commit()
    yield c
    c.close()


def _rows(conn):
    return {(str(r["account_id"]), str(r["date"])): tuple(r) for r in
            conn.execute("SELECT * FROM paper_nav_daily")}


def _events(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM system_events WHERE module = 'paper_nav_daily'"
        " ORDER BY event_id")]


# ---------- 探测 ----------

def test_probe_flags_the_real_defect_and_nothing_else(conn):
    """探测：未结算行**只有**那一行；别个臂的行要么干净、要么进 skipped。

    「零违规」与「零比对」分开报（`checked`）——判据是 `_ledger_arm_state`
    重放 + `mark_to_market`（既有实现），不是另算一份。
    """
    probe = p62_defective_agent_nav_rows(conn)
    assert probe["checked"] == 2, probe          # ds-v1（坏）+ arm-agent（干净）
    assert [(d["account_id"], d["date"], d["named"]) for d in probe["defective"]] \
        == [(ARM, ASOF, True)], probe["defective"]
    assert probe["defective"][0]["diffs"]["nav"]["replay"] == 19553.68
    assert probe["defective"][0]["diffs"]["positions_json"]["got"] == {"000333": 0}
    assert [s["account_id"] for s in probe["skipped"]] == [HOLD]
    assert "不在本判据范围内" in probe["skipped"][0]["reason"]


# ---------- 迁移 ----------

def test_migration_deletes_only_the_named_row_and_keeps_a_ledger(conn):
    before = _rows(conn)
    changes = migrate_p62_agent_nav_settlement(conn)
    after = _rows(conn)

    assert [c for c in changes if not c.startswith("⚠️")] == [
        f"{ARM}/{ASOF}: 删除未结算净值行（nav 11320.0 → 待重写）"]
    assert (ARM, ASOF) not in after
    for key, row in before.items():                       # 其余行**逐字节不变**
        if key != (ARM, ASOF):
            assert after[key] == row, f"{key} 被改动了"
    assert len(after) == len(before) - 1

    trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert trades == 1, "成交流水一行都不许删（那一笔是真的）"

    evs = _events(conn)
    assert len(evs) == 1
    ctx = json.loads(evs[0]["context_json"])
    assert ctx["task"] == "P62" and ctx["account_id"] == ARM and ctx["date"] == ASOF
    assert ctx["old"]["nav"] == 11320.0 and ctx["old"]["positions_json"] == \
        BAD_ROW["positions_json"]
    assert ctx["diff"]["nav"]["replay"] == 19553.68
    assert ctx["basis"].endswith("2026-09-23-p62-AI臂净值未结算修复.md")
    assert "paper agent run" in evs[0]["message"]


def test_migration_is_reentrant_and_restores_the_triggers(conn):
    assert migrate_p62_agent_nav_settlement(conn)
    n_events = len(_events(conn))
    second = migrate_p62_agent_nav_settlement(conn)
    assert [c for c in second if not c.startswith("⚠️")] == []      # 可重入
    assert len(_events(conn)) == n_events, "第二次跑不该再记一条台账"
    assert _rows(conn) == _rows(conn)

    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"trg_paper_nav_daily_no_update",
            "trg_paper_nav_daily_no_delete"} <= names, "触发器必须挂回去"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM paper_nav_daily WHERE account_id = ?", (OTHER,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE paper_nav_daily SET nav = 0 WHERE account_id = ?", (OTHER,))


def test_migration_refuses_to_touch_rows_outside_the_named_list(conn):
    """点名清单之外的**未结算**行 ⇒ 抛错 + **零改动**（人来拍板，不许顺手修）。"""
    target = "arm-agent-ds-v2"
    _account(conn, target, "agent")
    _nav(conn, target, BAD_ROW)
    conn.commit()
    before = _rows(conn)
    with pytest.raises(RuntimeError) as exc:
        migrate_p62_agent_nav_settlement(conn)
    assert target in str(exc.value) and "未做任何改动" in str(exc.value)
    assert _rows(conn) == before
    assert _events(conn) == []
