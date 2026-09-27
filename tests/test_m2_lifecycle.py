"""P87：策略生命周期闭环 —— 「连续优化失败次数」与「冻结到期时间」的**派生**读出口。

00 §全局硬性约束 6 与 06 §D-6 要的是「加字段」，但 `validation_cycles` 是
append-only（`schema.sql` 的 `trg_validation_cycles_no_update`），列根本没法维护 ——
所以本任务走**派生**路线（D1）：两个数一律从 `m2_judgements` 这一张既有台账 fold 出来。
口径定义见 `stocklab/m2/lifecycle.py` 的模块 docstring 与 ADR-037。

判据 → 用例（与任务书 §2 对应）：

| 判据 | 用例 |
|---|---|
| G2 | `test_snapshot_*` / `test_cli_show_*`：无 `freeze` 行 ⇒ `freeze_due is None` 且文案说「无冻结记录」 |
| G3 | `test_judge_flags_force_archive_on_a_three_fail_streak`（＋ `branch` 不被改写） |
| G4 | `test_fail_streak_skips_insufficient_rows_in_the_tail`（穿透）/ `test_fail_streak_stops_at_freeze` / `test_fail_streak_stops_at_tune`（归零） |
| G5 | `test_due_freezes_boundary_is_exact` |
| G6 | `test_cli_judge_freeze_days_over_the_limit_is_still_refused`（P59 入口守卫，零写入） |
| D7/D9 | `test_cli_show_is_read_only` / `test_cli_due_is_read_only` / `test_cli_on_a_missing_db_creates_nothing` |

**不手抄常量**：阈值一律从 `stocklab.config.limits` 取 —— 手抄的那份会与上游漂移
（P49 的同款纪律）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import stocklab.cli.main as cli_main
from stocklab.config import limits
from stocklab.cli.main import main
from stocklab.labweb import paper_data
from stocklab.m2 import config as m2_config
from stocklab.m2 import lifecycle, score as m2_score, store as m2_store
from stocklab.paper import store as paper_store
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-27T16:00:00+08:00"
CRITERIA = "相对沪深300 超额 > 0 且 方向命中率 ≥ 0.5（判据原文示例）"
SCRIPT_ID = 7
OTHER_SCRIPT = 8
LIMIT = limits.STRATEGY_FAIL_STREAK_LIMIT


def _day(n: int) -> str:
    """`2026-03-<n>`（本文件里的「第 n 天」一律用它，不碰墙上时钟）。"""
    return f"2026-03-{n:02d}"


def _shift(day: str, days: int) -> str:
    import datetime

    return (datetime.date.fromisoformat(day)
            + datetime.timedelta(days=days)).isoformat()


def _db(tmp_path, name: str = "life.db"):
    """一份只有 schema 的库：这一组测的是**台账的 fold 口径**，不是取数链路。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    init_db(path)
    return path


def _row(path, *, asof: str, branch: str, script_id: int = SCRIPT_ID,
         cycle_id: int = 1, freeze_days: int | None = None,
         fix_kind: str = "logic", met_target: bool | None = False) -> int:
    """灌一行判定 —— 走 `m2.store.insert_judgement`（台账自己的写入口，不是绕过它）。"""
    c = connect(path)
    try:
        return m2_store.insert_judgement(
            c, cycle_id=cycle_id, script_id=script_id, asof=asof, branch=branch,
            evidence={"fix_kind": fix_kind, "freeze_days": freeze_days,
                      "met_target": met_target},
            criteria_text=CRITERIA, now=NOW)
    finally:
        c.close()


def _conn(path):
    return connect(path)


def _run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# ══════════════════════════════════════════════════════════════════════
# D2 / D3 —— 连续优化失败次数（尾部连续 + insufficient 穿透）
# ══════════════════════════════════════════════════════════════════════


def test_fail_streak_is_zero_without_any_judgement(tmp_path):
    """空历史 ⇒ 0（不是 None、不是报错）。"""
    path = _db(tmp_path)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 0
    finally:
        c.close()


def test_fail_streak_counts_the_trailing_optimize_rows(tmp_path):
    """三条 `optimize` ⇒ 3。"""
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 3
    finally:
        c.close()


def test_fail_streak_skips_insufficient_rows_in_the_tail(tmp_path):
    """`optimize` 之后又一条 `insufficient` ⇒ 仍算 1（读数不足不是「这一版失败」）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_OPTIMIZE)
    _row(path, asof=_day(11), branch=m2_config.BRANCH_INSUFFICIENT)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 1
    finally:
        c.close()


def test_fail_streak_d3_worked_example(tmp_path):
    """D3 逐字例：[freeze, optimize, insufficient, optimize]（升序）⇒ streak = 2。"""
    path = _db(tmp_path)
    for n, branch in ((10, m2_config.BRANCH_FREEZE), (11, m2_config.BRANCH_OPTIMIZE),
                      (12, m2_config.BRANCH_INSUFFICIENT),
                      (13, m2_config.BRANCH_OPTIMIZE)):
        _row(path, asof=_day(n), branch=branch)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 2
    finally:
        c.close()


def test_fail_streak_stops_at_freeze(tmp_path):
    """`[optimize, optimize, freeze]` ⇒ 0：冻结是「停用」，不是「又失败一次」。"""
    path = _db(tmp_path)
    for n, branch in ((10, m2_config.BRANCH_OPTIMIZE), (11, m2_config.BRANCH_OPTIMIZE),
                      (12, m2_config.BRANCH_FREEZE)):
        _row(path, asof=_day(n), branch=branch)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 0
    finally:
        c.close()


def test_fail_streak_stops_at_tune(tmp_path):
    """`[optimize, tune, optimize]` ⇒ 1：`tune` 归零后只数得动它后面那一条。"""
    path = _db(tmp_path)
    for n, branch in ((10, m2_config.BRANCH_OPTIMIZE), (11, m2_config.BRANCH_TUNE),
                      (12, m2_config.BRANCH_OPTIMIZE)):
        _row(path, asof=_day(n), branch=branch)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 1
    finally:
        c.close()


def test_fail_streak_is_zero_when_the_tail_is_all_insufficient(tmp_path):
    """整条尾巴都是 `insufficient` ⇒ 0（穿透到底，一条失败都没有）。"""
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_INSUFFICIENT)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 0
    finally:
        c.close()


def test_fail_streak_is_pit_bounded(tmp_path):
    """PIT：`asof` 之后的行**不许被读到** —— asof 截在第二条 ⇒ streak = 2。"""
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(11)) == 2
    finally:
        c.close()


def test_fail_streak_does_not_leak_across_scripts(tmp_path):
    """另一个 script_id 的 `optimize` 不计入本策略版本（按 script 隔离）。"""
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE, script_id=OTHER_SCRIPT)
    _row(path, asof=_day(13), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 1
        assert lifecycle.fail_streak(c, OTHER_SCRIPT, _day(20)) == 3
    finally:
        c.close()


def test_fail_streak_is_capped_by_nothing_but_the_tail(tmp_path):
    """失败可以超过阈值（阈值只用于**判定**，不用来截断读数）。"""
    path = _db(tmp_path)
    for n in range(1, 8):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        assert lifecycle.fail_streak(c, SCRIPT_ID, _day(20)) == 7
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# judgement_tail —— 尾部摘要（只读、PIT）
# ══════════════════════════════════════════════════════════════════════


def test_judgement_tail_is_newest_first(tmp_path):
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        tail = lifecycle.judgement_tail(c, SCRIPT_ID)
        assert [r["asof_date"] for r in tail] == [_day(12), _day(11), _day(10)]
    finally:
        c.close()


def test_judgement_tail_honours_the_limit(tmp_path):
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        assert len(lifecycle.judgement_tail(c, SCRIPT_ID, limit=2)) == 2
    finally:
        c.close()


def test_judgement_tail_is_pit_bounded(tmp_path):
    """D3 的 PIT 守卫：`asof` 之后的行不许出现在尾部。"""
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        tail = lifecycle.judgement_tail(c, SCRIPT_ID, asof=_day(11))
        assert [r["asof_date"] for r in tail] == [_day(11), _day(10)]
    finally:
        c.close()


def test_judgement_tail_rows_are_flat_and_labelled(tmp_path):
    """摘要行是**扁平的**（不把 `evidence_json` 整个塞进来）+ 带中文标签。"""
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    c = _conn(path)
    try:
        row = lifecycle.judgement_tail(c, SCRIPT_ID)[0]
        assert tuple(row) == ("judgement_id", "cycle_id", "asof_date", "branch",
                              "branch_label", "freeze_days")
        assert row["branch_label"] == m2_config.BRANCH_LABELS[m2_config.BRANCH_FREEZE]
        assert row["freeze_days"] == 30
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# D4 —— 冻结到期日
# ══════════════════════════════════════════════════════════════════════


def test_freeze_due_is_the_freeze_day_plus_freeze_days(tmp_path):
    """冻结日 + `freeze_days` 个**自然日**（D4）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    c = _conn(path)
    try:
        out = lifecycle.freeze_due(c, SCRIPT_ID, _day(20))
        assert out["found"] is True
        assert out["asof_date"] == _day(10)
        assert out["freeze_days"] == 30
        assert out["due"] == _shift(_day(10), 30)
    finally:
        c.close()


def test_freeze_due_is_overdue_when_due_equals_or_precedes_asof(tmp_path):
    """D4：`due <= asof` 即「已到点」—— 相等也算到点（边界不外扩也不内缩）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_FREEZE, freeze_days=10)
    c = _conn(path)
    try:
        assert lifecycle.freeze_due(c, SCRIPT_ID, _day(19))["overdue"] is False
        assert lifecycle.freeze_due(c, SCRIPT_ID, _day(20))["overdue"] is True
        assert lifecycle.freeze_due(c, SCRIPT_ID, _day(21))["overdue"] is True
    finally:
        c.close()


def test_freeze_due_is_null_when_there_is_no_freeze_row(tmp_path):
    """G2 的核心：真库没有 `freeze` 行 ⇒ `due is None`、`overdue is None`。

    **不是** 0、**不是**空串 —— 「没有到期日」与「今天到期」必须分得开。
    """
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        out = lifecycle.freeze_due(c, SCRIPT_ID, _day(20))
        assert out == {"found": False, "asof_date": None, "freeze_days": None,
                       "due": None, "overdue": None}
    finally:
        c.close()


def test_freeze_due_is_null_when_freeze_days_is_missing(tmp_path):
    """有冻结判定但该行没有 `freeze_days` ⇒ 无到期日（给 `null`，不猜 90 / 不猜 0）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_FREEZE, freeze_days=None)
    c = _conn(path)
    try:
        out = lifecycle.freeze_due(c, SCRIPT_ID, _day(20))
        assert out["found"] is True and out["freeze_days"] is None
        assert out["due"] is None and out["overdue"] is None
    finally:
        c.close()


def test_freeze_due_uses_the_newest_freeze_row(tmp_path):
    """多条冻结 ⇒ 取**最新**那条（旧冻结的到期日不是「当前状态」）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(5), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    _row(path, asof=_day(12), branch=m2_config.BRANCH_FREEZE, freeze_days=60)
    c = _conn(path)
    try:
        out = lifecycle.freeze_due(c, SCRIPT_ID, _day(20))
        assert out["asof_date"] == _day(12) and out["freeze_days"] == 60
        assert out["due"] == _shift(_day(12), 60)
    finally:
        c.close()


def test_freeze_due_is_pit_bounded(tmp_path):
    """`asof` 之后的冻结行不算数（不许拿未来的冻结算今天的到期）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    _row(path, asof=_day(25), branch=m2_config.BRANCH_FREEZE, freeze_days=90)
    c = _conn(path)
    try:
        out = lifecycle.freeze_due(c, SCRIPT_ID, _day(20))
        assert out["asof_date"] == _day(10) and out["freeze_days"] == 30
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# due_freezes —— 到期清单
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("offset,appears", [(-31, True), (-30, True), (-29, False),
                                            (-1, False), (0, False)])
def test_due_freezes_boundary_is_exact(tmp_path, offset, appears):
    """G5：`freeze_days=30`，判定日在 `day-31` ⇒ 出现（已过期 1 天）；`day-29` ⇒ 不出现。

    `day-30` ⇒ 到期日**正好等于** day ⇒ 出现（D4 的 `due <= asof`）；
    `day`（今天冻结）⇒ 不出现（到期日 = day+30）。
    """
    day = _day(20)
    path = _db(tmp_path)
    _row(path, asof=_shift(day, offset), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    c = _conn(path)
    try:
        rows = lifecycle.due_freezes(c, day)
        assert ([r["script_id"] for r in rows] == [SCRIPT_ID]) is appears
    finally:
        c.close()


def test_due_freezes_reports_how_late_it_is(tmp_path):
    """清单里带「过期了几天」—— 一个「到期了」的布尔值不足以排优先级。"""
    path = _db(tmp_path)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_FREEZE, freeze_days=10)
    c = _conn(path)
    try:
        row = lifecycle.due_freezes(c, _day(20))[0]
        assert tuple(row) == ("script_id", "judgement_id", "cycle_id", "asof_date",
                              "freeze_days", "due", "overdue_days")
        assert row["due"] == _day(11) and row["overdue_days"] == 9
    finally:
        c.close()


def test_due_freezes_dedupes_by_script(tmp_path):
    """同一 script 两条冻结 ⇒ 只列**最新**那条（去重按 script_id）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_FREEZE, freeze_days=5)
    _row(path, asof=_day(2), branch=m2_config.BRANCH_FREEZE, freeze_days=10)
    c = _conn(path)
    try:
        rows = lifecycle.due_freezes(c, _day(20))
        assert len(rows) == 1 and rows[0]["asof_date"] == _day(2)
    finally:
        c.close()


def test_due_freezes_skips_a_freeze_without_a_due_date(tmp_path):
    """没有 `freeze_days` ⇒ 无到期日 ⇒ **不进清单**（不是「立刻到期」）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_FREEZE, freeze_days=None)
    c = _conn(path)
    try:
        assert lifecycle.due_freezes(c, _day(20)) == []
    finally:
        c.close()


def test_due_freezes_lists_each_script_once_and_sorted(tmp_path):
    """多 script 各自一条，按到期日升序（最该处理的排前面）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(10), branch=m2_config.BRANCH_FREEZE, freeze_days=9,
         script_id=OTHER_SCRIPT)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_FREEZE, freeze_days=9,
         script_id=SCRIPT_ID)
    c = _conn(path)
    try:
        rows = lifecycle.due_freezes(c, _day(20))
        assert [r["script_id"] for r in rows] == [SCRIPT_ID, OTHER_SCRIPT]
        assert [r["due"] for r in rows] == [_day(10), _day(19)]
    finally:
        c.close()


def test_due_freezes_is_empty_when_nothing_has_come_due(tmp_path):
    path = _db(tmp_path)
    _row(path, asof=_day(19), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    c = _conn(path)
    try:
        assert lifecycle.due_freezes(c, _day(20)) == []
    finally:
        c.close()


def test_due_freezes_ignores_non_freeze_branches(tmp_path):
    """`optimize` / `tune` / `insufficient` 都不是冻结 —— 不进清单。"""
    path = _db(tmp_path)
    for n, branch in ((1, m2_config.BRANCH_OPTIMIZE), (2, m2_config.BRANCH_TUNE),
                      (3, m2_config.BRANCH_INSUFFICIENT)):
        _row(path, asof=_day(n), branch=branch)
    c = _conn(path)
    try:
        assert lifecycle.due_freezes(c, _day(20)) == []
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# snapshot —— D7 的载荷形状（键名与顺序固定）
# ══════════════════════════════════════════════════════════════════════

#: `snapshot` 的**键序**（下游按位置读也成立；改这个顺序 = 改接口）。
SHAPE = ("script_id", "asof", "fail_streak", "limit", "force_archive", "tail",
         "freeze_due", "overdue", "archive_reason", "note")


def test_snapshot_has_the_fixed_key_order(tmp_path):
    path = _db(tmp_path)
    c = _conn(path)
    try:
        assert tuple(lifecycle.snapshot(c, SCRIPT_ID, _day(20))) == SHAPE
    finally:
        c.close()


def test_snapshot_says_there_is_no_freeze_record(tmp_path):
    """G2 的文案要求：无冻结记录要**显式说出来**（不许印 0 / 空串 / 省略键）。"""
    path = _db(tmp_path)
    c = _conn(path)
    try:
        out = lifecycle.snapshot(c, SCRIPT_ID, _day(20))
        assert out["freeze_due"] is None and out["overdue"] is None
        assert "无冻结记录" in out["note"]
        assert out["archive_reason"] is None
    finally:
        c.close()


def test_snapshot_says_the_due_date_and_how_late_it_is(tmp_path):
    path = _db(tmp_path)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_FREEZE, freeze_days=10)
    c = _conn(path)
    try:
        out = lifecycle.snapshot(c, SCRIPT_ID, _day(20))
        assert out["freeze_due"] == _day(11) and out["overdue"] is True
        assert _day(11) in out["note"] and "9" in out["note"]
    finally:
        c.close()


def test_snapshot_limit_comes_from_the_config_limits(tmp_path):
    """阈值只有一个真源（`config/limits.py`）—— 读出口不另抄一个字面量。"""
    path = _db(tmp_path)
    c = _conn(path)
    try:
        assert lifecycle.snapshot(c, SCRIPT_ID, _day(20))["limit"] == LIMIT == 3
    finally:
        c.close()


@pytest.mark.parametrize("n_fail,expected", [(0, False), (2, False), (3, True), (4, True)])
def test_snapshot_force_archive_is_at_the_threshold(tmp_path, n_fail, expected):
    """`fail_streak >= 阈值` ⇒ `force_archive`（D6：只提示，不执行）。"""
    path = _db(tmp_path)
    for n in range(1, n_fail + 1):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        out = lifecycle.snapshot(c, SCRIPT_ID, _day(20))
        assert out["fail_streak"] == n_fail
        assert out["force_archive"] is expected
        assert (out["archive_reason"] is None) is not expected
    finally:
        c.close()


def test_snapshot_archive_reason_quotes_the_streak_and_the_limit(tmp_path):
    """归档建议要带**数字与阈值**（「看起来像个结论」必须能被核对）。"""
    path = _db(tmp_path)
    for n in (1, 2, 3):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        reason = lifecycle.snapshot(c, SCRIPT_ID, _day(20))["archive_reason"]
        assert "3" in reason and str(LIMIT) in reason
        assert "归档" in reason
    finally:
        c.close()


def test_snapshot_tail_is_the_tail_of_the_ledger(tmp_path):
    path = _db(tmp_path)
    for n in (10, 11, 12):
        _row(path, asof=_day(n), branch=m2_config.BRANCH_OPTIMIZE)
    c = _conn(path)
    try:
        tail = lifecycle.snapshot(c, SCRIPT_ID, _day(12))["tail"]
        assert [r["asof_date"] for r in tail] == [_day(12), _day(11), _day(10)]
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# CLI（只读出口）
# ══════════════════════════════════════════════════════════════════════


def test_cli_show_json_prints_null_freeze_due_on_an_empty_ledger(tmp_path, capsys):
    """G2：`--json` 载荷里 `freeze_due` 键**在**、值是 `null`。"""
    path = _db(tmp_path)
    code, out, err = _run(capsys, "m2", "lifecycle", "show", "--script", str(SCRIPT_ID),
                          "--asof", _day(20), "--json", "--db", str(path))
    assert code == 0, err
    payload = json.loads(out)
    # CLI 的 JSON 走 `sort_keys=True`（键序由它在载荷里的定义处钉住，见
    # `test_snapshot_has_the_fixed_key_order`）—— 这里钉**键集合**与值。
    assert set(payload) == set(SHAPE)
    assert payload["freeze_due"] is None and payload["fail_streak"] == 0
    assert payload["limit"] == LIMIT


def test_cli_show_text_says_no_freeze_record(tmp_path, capsys):
    """G2：人话摘要里出现「无冻结记录」。"""
    path = _db(tmp_path)
    code, out, _err = _run(capsys, "m2", "lifecycle", "show", "--script", str(SCRIPT_ID),
                           "--asof", _day(20), "--db", str(path))
    assert code == 0
    assert "无冻结记录" in out
    assert str(SCRIPT_ID) in out


def test_cli_show_defaults_to_today_from_the_now_pin(tmp_path, capsys):
    """不带 `--asof` ⇒ 按 `--now` 推的今天（时钟是参数不是环境，错误日记 #30）。"""
    path = _db(tmp_path)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_OPTIMIZE)
    code, out, err = _run(capsys, "m2", "lifecycle", "show", "--script", str(SCRIPT_ID),
                          "--json", "--db", str(path), "--now", NOW)
    assert code == 0, err
    assert json.loads(out)["asof"] == NOW[:10]


def test_cli_due_lists_the_overdue_version(tmp_path, capsys):
    """G5 的 CLI 面：构造「判定日 = day-31、freeze_days=30」⇒ 该 script 出现。"""
    day = _day(20)
    path = _db(tmp_path)
    _row(path, asof=_shift(day, -31), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    code, out, err = _run(capsys, "m2", "lifecycle", "due", "--asof", day, "--json",
                          "--db", str(path))
    assert code == 0, err
    payload = json.loads(out)
    assert payload["asof"] == day and payload["n_due"] == 1
    assert payload["due"][0]["script_id"] == SCRIPT_ID
    assert payload["due"][0]["overdue_days"] == 1


def test_cli_due_text_says_nothing_is_due(tmp_path, capsys):
    day = _day(20)
    path = _db(tmp_path)
    _row(path, asof=_shift(day, -29), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    code, out, _err = _run(capsys, "m2", "lifecycle", "due", "--asof", day,
                           "--db", str(path))
    assert code == 0
    assert "没有到期" in out


def test_cli_lifecycle_commands_are_parser_wired():
    """两条命令真的挂在 `m2 lifecycle` 下（typo 的 argparse 会静默走 SystemExit）。"""
    parser = cli_main.build_parser()
    args = parser.parse_args(["m2", "lifecycle", "show", "--script", "7"])
    assert args.func is cli_main.cmd_m2_lifecycle_show and args.asof is None
    args = parser.parse_args(["m2", "lifecycle", "due", "--asof", "2026-03-20"])
    assert args.func is cli_main.cmd_m2_lifecycle_due and args.json is False


# ══════════════════════════════════════════════════════════════════════
# G6 —— 既有行为不变（P59 的入口守卫）
# ══════════════════════════════════════════════════════════════════════


def test_cli_judge_freeze_days_over_the_limit_is_still_refused(tmp_path, capsys):
    """`m2 cycle judge --freeze-days 999` ⇒ 退出码 2 ＋ **零写入**（校验在入口、在
    读周期之前 —— 这就是 P59 那道守卫，本任务不许把它挪到别处）。"""
    path = _db(tmp_path)
    code, _out, err = _run(capsys, "m2", "cycle", "judge", "--cycle", "1",
                           "--asof", _day(20), "--fix-kind", "none",
                           "--freeze-days", "999", "--db", str(path), "--now", NOW)
    assert code == 2, err
    assert str(limits.FREEZE_MAX_DAYS) in err
    c = _conn(path)
    try:
        for table in ("validation_cycles", "validation_rounds", "m2_judgements"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# D7 / D9 —— 只读守卫（结构防线，不是注释里的自觉）
# ══════════════════════════════════════════════════════════════════════

_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER")


class _WriteGuard:
    """连接包装：写语句一律抛，并把「怎么打开的」记下来。

    只比对 `execute*` 的**第一段关键字**，不做全文子串匹配 —— 后者会把
    `... WHERE update_at = ?` 之类判红，假红比不测更糟（ERROR_DIARY #50 同款）。
    """

    def __init__(self, conn, uri: str):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "uri", uri)
        object.__setattr__(self, "writes", [])

    def __setattr__(self, name, value):          # row_factory 之类要落到真连接上
        if name in ("_conn", "uri", "writes"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def _refuse(self, sql):
        self.writes.append(str(sql))
        raise AssertionError(f"只读出口尝试写库：{sql}")

    def _verb(self, sql) -> str:
        text = str(sql).strip()
        return text.split(None, 1)[0].upper() if text else ""

    def execute(self, sql, *args, **kwargs):
        if self._verb(sql) in _WRITE_VERBS:
            self._refuse(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        if self._verb(sql) in _WRITE_VERBS:
            self._refuse(sql)
        return self._conn.executemany(sql, *args, **kwargs)

    def executescript(self, sql, *args, **kwargs):
        self._refuse(sql)

    def commit(self):
        self._refuse("COMMIT")

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _run_readonly(monkeypatch, capsys, *argv):
    """在「写就抛 + 必须用 `file:…?mode=ro` 打开 + 不许 ensure_schema」的条件下跑 CLI。

    三条一起钉：只拦写语句挡不住 `ensure_schema` 的整包 DDL，也挡不住
    「不小心用了读写连接」——「零写入」是三件事合起来才成立。
    """
    guards: list[_WriteGuard] = []
    real_connect = sqlite3.connect

    def factory(path, *args, **kwargs):
        text = str(path)
        assert text.startswith("file:/"), f"不是绝对路径的只读 URI：{text}"
        assert "mode=ro" in text, f"没带 mode=ro：{text}"
        assert kwargs.get("uri") is True, f"没开 uri 开关：{kwargs}"
        guard = _WriteGuard(real_connect(path, *args, **kwargs), text)
        guards.append(guard)
        return guard

    def boom(*_args, **_kwargs):
        raise AssertionError("只读命令调了 ensure_schema（那是一次写库）")

    monkeypatch.setattr(sqlite3, "connect", factory)
    monkeypatch.setattr(cli_main, "ensure_schema", boom)
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err, guards


def test_cli_show_is_read_only(monkeypatch, capsys, tmp_path):
    """`m2 lifecycle show`：零写语句、只读 URI、不前滚 schema。"""
    day = _day(20)
    path = _db(tmp_path)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_FREEZE, freeze_days=10)
    _, out, err, guards = _run_readonly(
        monkeypatch, capsys, "m2", "lifecycle", "show", "--script", str(SCRIPT_ID),
        "--asof", day, "--json", "--db", str(path))
    assert err == "" and json.loads(out)["freeze_due"] == _day(11)
    assert len(guards) == 1 and guards[0].writes == []


def test_cli_due_is_read_only(monkeypatch, capsys, tmp_path):
    """`m2 lifecycle due`：同上（两条出口都必须是纯读）。"""
    day = _day(20)
    path = _db(tmp_path)
    _row(path, asof=_shift(day, -31), branch=m2_config.BRANCH_FREEZE, freeze_days=30)
    _, out, err, guards = _run_readonly(
        monkeypatch, capsys, "m2", "lifecycle", "due", "--asof", day, "--db", str(path))
    assert err == "" and str(SCRIPT_ID) in out
    assert len(guards) == 1 and guards[0].writes == []


def test_cli_on_a_missing_db_creates_nothing(monkeypatch, capsys, tmp_path):
    """库不存在 ⇒ 退出码 2，且**不把库建出来**（只读出口不许有建文件的副作用）。"""
    missing = tmp_path / "nope.db"
    code, _out, err, guards = _run_readonly(
        monkeypatch, capsys, "m2", "lifecycle", "show", "--script", str(SCRIPT_ID),
        "--db", str(missing))
    assert code == 2 and "db not found" in err
    assert guards == [] and not missing.exists()


def test_snapshot_never_writes_even_when_a_write_would_be_tempting(monkeypatch, tmp_path):
    """数据层直接钉一遍（CLI 之外的第二条路径）：`snapshot` 的 SQL 全是 SELECT。"""
    day = _day(20)
    path = _db(tmp_path)
    _row(path, asof=_day(1), branch=m2_config.BRANCH_FREEZE, freeze_days=10)
    c = _WriteGuard(connect(path), "file:test")
    try:
        assert lifecycle.snapshot(c, SCRIPT_ID, day)["freeze_due"] == _day(11)
        assert lifecycle.due_freezes(c, day)[0]["overdue_days"] == 9
        assert c.writes == []
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# G3 —— `judge()` 的生命周期**接线**（扩展，不是改判：D6 + D8）
#
# 这一组要的是「真的跑一遍判定」：读数 / 门禁 / 案例集全部来自 P41/P48 的既有取数
# （不 mock 取数层）。夹具与 `tests/test_p49_selfeval.py` 同构 —— 两处都自建，
# 因为「跨测试文件 import 夹具」会让一处的改动悄悄改掉另一处的实验前提。
# ══════════════════════════════════════════════════════════════════════

START = "2026-01-01"
STOCK = "600519"
AI_ACCOUNT = "arm-agent-v1"
N_DAYS = 130
CYCLE_START_IDX = 99
PEAK = 22000.0
TROUGH = 21500.0

#: D-44 的 `evidence` **既有键**（`selfeval.decide_branch` 的 12 个 +
#: `cycle.judge` 补的 5 个）。加生命周期只许多一个 `lifecycle`。
D44_EVIDENCE_KEYS = frozenset({
    "n_rounds", "planned_rounds", "planned_days", "gate", "metrics",
    "excess_vs_index_300", "met_target", "cases", "fix_kind", "freeze_days",
    "thresholds", "steps", "reason", "actions", "conclusion", "readings_window",
    "criteria_text",
})


def _sessions(n: int) -> list[str]:
    import datetime

    base = datetime.date.fromisoformat(START)
    return [(base + datetime.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _nav_values(n: int) -> list[float]:
    """先涨到 `PEAK` 再跌到 `TROUGH`（只有这样才能把回撤与「首尾相减」分开）。"""
    peak_at = int(n * 0.85)
    rise = [20000.0 + (PEAK - 20000.0) * i / peak_at for i in range(peak_at + 1)]
    steps = n - peak_at - 1
    fall = [PEAK + (TROUGH - PEAK) * i / steps for i in range(1, steps + 1)]
    assert len(rise) + len(fall) == n
    return rise + fall


def _judgeable_db(tmp_path, name: str = "p87.db", index_slope: float = 9.0):
    """一份能端到端跑判定的库：账户 + 净值 + K 线 + 已落库的校验分数。

    净值行与 K 线直接插（不跑 `paper step` / `ingest`）：这一组测的是判定的
    **接线**，不是「引擎会不会算净值」。`index_slope > 0` ⇒ 基准上涨幅度大于账户
    ⇒ 相对超额 ≤ 0 ⇒ 「读数未达标」成立（冻结分支的前提）。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    init_db(path)
    c = connect(path)
    days = _sessions(N_DAYS)
    axis = [START, *days]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sh','main',?,?)",
        [(STOCK, "贵州茅台", "stock", NOW),
         (INDEX_300_SYMBOL, "沪深300", "index", NOW)])
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in axis])
    closes = ([100.0 + 30.0 * i / (N_DAYS - 9) for i in range(N_DAYS - 8)]
              + [130.0 - 12.0 * i / 8 for i in range(1, 9)])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(STOCK, d, v, v, v, v, NOW) for d, v in zip(axis, [100.0, *closes])]
        + [(INDEX_300_SYMBOL, d, 4000.0 + index_slope * i, 4000.0 + index_slope * i,
            4000.0 + index_slope * i, 4000.0 + index_slope * i, NOW)
           for i, d in enumerate(axis)])
    c.commit()
    paper_store.insert_account(
        c, account_id=AI_ACCOUNT, arm="agent", etf_target_pct=None, start_date=START,
        initial_cash=20000.0, initial_positions=[], initial_nav=20000.0,
        params={m2_config.EXECUTOR_KEY: m2_config.EXECUTOR_CHANNEL_A,
                "strategy_version": "v1"}, now=NOW)
    navs = _nav_values(N_DAYS)
    for i, d in enumerate(days, start=1):
        paper_store.insert_nav(
            c, account_id=AI_ACCOUNT, date=d, cash=navs[i - 1], positions=[],
            market_value=0.0, nav=navs[i - 1], drawdown=0.0, cum_cost=0.0,
            cum_return=round(navs[i - 1] / 20000.0 - 1.0, 6), net_deposits=20000.0,
            index_300_level=None, index_300_asof=None, now=NOW, commit=False)
    c.commit()
    for i in range(N_DAYS - 1):
        m2_store.insert_forecast(
            c, plugin_id="m2_a3", channel="A", account_id=AI_ACCOUNT, asof=days[i],
            code=STOCK,
            payload={"range_80": [95.0, 105.0],
                     "direction": {"up": 0.6, "flat": 0.2, "down": 0.2},
                     "invalidate_if": "跌破 90 或 站上 110", "na_reasons": [],
                     "schema_version": "1"},
            script_id=SCRIPT_ID, script_version="s1", input_sha256=f"{i:064d}", now=NOW)
    c.commit()
    m2_score.score_all(c, asof=days[-1], now=NOW)
    c.close()
    return path, days


def _start_and_rounds(path, days, capsys) -> int:
    """开一个周期并落满 2 轮（判定要求 ≥ `VALIDATION_ROUNDS_MIN` 轮）。"""
    code, out, err = _run(
        capsys, "m2", "cycle", "start", "--script-id", str(SCRIPT_ID),
        "--account-id", AI_ACCOUNT, "--rounds", "3", "--days", "30",
        "--criteria-text", CRITERIA, "--start-date", days[CYCLE_START_IDX],
        "--db", str(path), "--now", NOW)
    assert code == 0, err
    cid = json.loads(out)["cycle_id"]
    for no, idx in ((1, CYCLE_START_IDX + 6), (2, CYCLE_START_IDX + 26)):
        code, _out, err = _run(capsys, "m2", "cycle", "round", "--cycle", str(cid),
                               "--round-no", str(no), "--asof", days[idx],
                               "--db", str(path), "--now", NOW)
        assert code == 0, err
    return cid


def _judge_cli(path, capsys, *, cycle: int, asof: str, fix_kind: str,
               freeze_days: int | None = None):
    argv = ["m2", "cycle", "judge", "--cycle", str(cycle), "--asof", asof,
            "--fix-kind", fix_kind, "--db", str(path), "--now", NOW]
    if freeze_days is not None:
        argv += ["--freeze-days", str(freeze_days)]
    return _run(capsys, *argv)


def test_judge_flags_force_archive_on_a_three_fail_streak(tmp_path, capsys):
    """G3：同 script 连续 3 条 `optimize` ⇒ `force_archive=True` ＋ `actions` 追加归档建议，
    **同时** `branch` 仍由 D-44 规则决定（不被 `archive` 改写）。"""
    path, days = _judgeable_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    for n in (1, 2, 3):                      # 三个更早的失败判定（同 script、同周期）
        _row(path, asof=f"2026-01-0{n}", cycle_id=cid,
             branch=m2_config.BRANCH_OPTIMIZE)
    code, out, err = _judge_cli(path, capsys, cycle=cid, asof=days[-1],
                                fix_kind="logic")
    assert code == 0, err
    payload = json.loads(out)
    life = payload["evidence"]["lifecycle"]
    assert life["fail_streak"] == 3 and life["limit"] == LIMIT
    assert life["force_archive"] is True
    assert life["archive_reason"] and "归档" in life["archive_reason"]
    assert life["archive_action"] in payload["actions"], payload["actions"]
    assert payload["branch"] == m2_config.BRANCH_OPTIMIZE
    assert payload["branch"] in m2_config.BRANCHES      # `archive` 不是新分支
    assert "archive" not in m2_config.BRANCHES


def test_judge_adds_one_lifecycle_key_and_drops_none(tmp_path, capsys):
    """D8：`evidence` 既有键**一个不少**，只多一个 `lifecycle`。"""
    path, days = _judgeable_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    code, out, err = _judge_cli(path, capsys, cycle=cid, asof=days[-1],
                                fix_kind="logic")
    assert code == 0, err
    evidence = json.loads(out)["evidence"]
    assert set(evidence) == D44_EVIDENCE_KEYS | {"lifecycle"}


def test_judge_leaves_actions_alone_below_the_threshold(tmp_path, capsys):
    """不到阈值 ⇒ 不追加、`force_archive=False`（提示不许变成噪音）。"""
    path, days = _judgeable_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    for n in (1, 2):
        _row(path, asof=f"2026-01-0{n}", cycle_id=cid,
             branch=m2_config.BRANCH_OPTIMIZE)
    code, out, err = _judge_cli(path, capsys, cycle=cid, asof=days[-1],
                                fix_kind="logic")
    assert code == 0, err
    payload = json.loads(out)
    life = payload["evidence"]["lifecycle"]
    assert life["fail_streak"] == 2 and life["force_archive"] is False
    assert life["archive_reason"] is None and life["archive_action"] is None
    assert len(payload["actions"]) == 2                  # optimize 分支原本两句
    assert not any("归档" in a for a in payload["actions"])


def test_judge_lifecycle_reports_the_due_date_of_the_previous_freeze(tmp_path, capsys):
    """判定落库的那一刻，生命周期读的是**此前**的台账：冻结分支判出来的
    `freeze_due` 是上一次冻结的到期日（这一条自己还没进台账）。"""
    path, days = _judgeable_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    _row(path, asof="2026-01-02", cycle_id=cid, branch=m2_config.BRANCH_FREEZE,
         freeze_days=10, fix_kind="none")
    c = _conn(path)
    try:                                    # 夹具前提先自证：读数未达标（冻结的前提）
        assert paper_data.performance(c, days[-1])["excess_vs_index_300"][AI_ACCOUNT] <= 0
    finally:
        c.close()
    code, out, err = _judge_cli(path, capsys, cycle=cid, asof=days[-1],
                                fix_kind="none", freeze_days=30)
    assert code == 0, err
    payload = json.loads(out)
    life = payload["evidence"]["lifecycle"]
    assert payload["branch"] == m2_config.BRANCH_FREEZE
    assert life["freeze_due"] == _shift("2026-01-02", 10)
    assert life["overdue"] is True and life["fail_streak"] == 0
