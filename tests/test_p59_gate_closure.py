"""P59：P49 遗留的两处「判定层」口子收口（F1 越界前移 / F2 幂等自证）。

任务书 §3 的判据 → 用例对应：

| # | 判据 | 用例 |
|---|---|---|
| T1 | F1-a：门禁**未**过 ＋ `--fix-kind none --freeze-days 91` ⇒ exit 2 ＋ 零写入 | `test_f1a_*` |
| T2 | F1-b：门禁**达**过 ＋ `--freeze-days 91` ⇒ exit 2（既有行为，防回归） | `test_f1b_*` |
| T3 | F1-c：合法边界值（`1` 与 `FREEZE_MAX_DAYS`）⇒ 照旧工作 | `test_f1c_*` |
| T4 | F2：已有判定行 ＋ `999` ⇒ exit 2 ＋ 零写入；＋ `30` ⇒ `already` ＋ `input_used=false` | `test_f2_*` |
| T5 | 依据自证：`insufficient` 行的 `evidence.freeze_days`（若有）必在合法区间 | `test_the_stored_evidence_*` |

夹具与 `_run` / `_p49_db` / `_judge` **复用 P49 的测试模块**（`tests.test_p49_selfeval`）：
复制一份会在上游口径变动时悄悄漂移，而这两站测的是同一套台账与同一批取数函数。

**为什么这两条口子值得单独一站**：它们**没有执行路径**（判定落库的 `actions=[]`，
真执行仍要人工 `approve`），但会留下**读起来矛盾**的记录 —— 一个越界的冻结天数
进了 append-only 台账，以及一个「没用过你的入参」的成功被读成「你的请求被接受」。
记录层的口子和执行层的口子一样要收，只是后果不同（不是安全漏洞，是事后无法对账）。
"""

from __future__ import annotations

import json

import pytest

from stocklab.config import limits
from stocklab.labweb import m2_data
from stocklab.m2 import config as m2_config
from stocklab.m2 import store as m2_store
from stocklab.store import validation as ledger
from tests.test_p49_selfeval import (
    CYCLE_START_IDX,
    _conn,
    _counts,
    _judge,
    _p49_db,
    _start,
    _start_and_rounds,
)

#: `already` 返回**在 P59 之前的**键集合（F2 修复前的实测，见实施记录 §11.1）。
#: 断言「只增不减」比断言「新键在不在」强：它同时挡住**删键**与**改名**。
P49_ALREADY_KEYS = frozenset({
    "status", "judgement_id", "cycle_id", "script_id", "asof_date", "branch",
    "evidence", "criteria_text", "created_at",
})
#: P59 新增的两个自证键（**候选**键名，任务书 §2.3）。
NEW_KEYS = frozenset({"input_used", "input_note"})

#: 越界值取「上限 + 1」，不写死 91 —— 常量哪天被人拍板改大，这条仍然测的是越界。
OVER = limits.FREEZE_MAX_DAYS + 1
#: 门禁没过的夹具：40 个净值日 < 120 交易日。
GATE_FAIL_DAYS = 40
GATE_FAIL_START_IDX = 10
GATE_FAIL_ASOF_IDX = 36


def _readings(path, cycle_id: int, asof: str) -> dict:
    """判定所用的**同一份**读数（与 `judge()` 同一个取数函数，不另写口径）。"""
    c = _conn(path)
    try:
        cycle = ledger.get_cycle(c, cycle_id)
        return m2_data.cycle_readings(c, cycle, asof)
    finally:
        c.close()


def _gate_fail_case(tmp_path, capsys, *, name: str = "gate-fail.db"):
    """门禁**未**过的一个周期 —— 判定必然落在 `insufficient` 分支。

    门禁由 `cycle_readings` 自己报出来并在这里**先自证**：夹具哪天变了（净值日变多），
    这条用例会以「前提不成立」判红，而不是悄悄变成在测另一条路径。
    """
    path, days = _p49_db(tmp_path, n_days=GATE_FAIL_DAYS, name=name)
    cid = _start(path, days, capsys, start_idx=GATE_FAIL_START_IDX)
    asof = days[GATE_FAIL_ASOF_IDX]
    gate = _readings(path, cid, asof)["sample_gate"]
    assert gate["meets"] is False, f"夹具前提：门禁必须没过，实际 {gate}"
    return path, days, cid, asof


# ══════════════════════════════════════════════════════════════════════
# T1（F1-a）—— 门禁未过：越界入参也必须在进分支之前被拒
# ══════════════════════════════════════════════════════════════════════


def test_f1a_a_freeze_days_beyond_the_limit_is_refused_when_the_gate_fails(tmp_path, capsys):
    """门禁未过 ＋ `--fix-kind none --freeze-days 91` ⇒ 退出码 2 ＋ **零写入**。

    修复前（P49 §10.3 实测）：exit 0，且 `evidence.freeze_days = 91` 落进 append-only
    台账（该行 `branch=insufficient` / `conclusion=false` / `actions=[]`）。
    越界即拒的纪律当时只在「门禁达标、真要走 freeze 分支」那一种场景成立。
    """
    path, _days, cid, asof = _gate_fail_case(tmp_path, capsys)
    c = _conn(path)
    try:
        before = _counts(c)
    finally:
        c.close()

    code, _out, err = _judge(path, capsys, cycle=cid, asof=asof, fix_kind="none",
                             freeze_days=OVER)

    assert code == 2, (f"越界入参在 insufficient 路径上被放过了（F1）："
                       f"exit={code} err={err}")
    assert str(OVER) in err and "拒绝" in err and "clamp" in err
    c = _conn(path)
    try:
        after = _counts(c)
        assert after == before, f"越界入参必须零写入，实际 {before} → {after}"
        assert after["m2_judgements"] == 0, "连 insufficient 的判定行也不许落"
    finally:
        c.close()


def test_f1a_the_refusal_does_not_depend_on_the_fix_kind(tmp_path, capsys):
    """「与 `--fix-kind` 无关」：声明 `params`（本会走 tune）时，越界冻结天数同样被拒。

    P49 的实现把 `check_freeze_days` 放在 `fix_kind == none` 分支**里面** ——
    校验位置一旦与分支耦合，就会出现「换个声明方式就能把越界值递进去」的读法。
    """
    path, _days, cid, asof = _gate_fail_case(tmp_path, capsys, name="gate-fail-params.db")
    code, _out, err = _judge(path, capsys, cycle=cid, asof=asof, fix_kind="params",
                             freeze_days=OVER)
    assert code == 2, f"越界入参被 fix_kind 放过了：exit={code} err={err}"
    assert str(OVER) in err
    c = _conn(path)
    try:
        assert _counts(c)["m2_judgements"] == 0
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T2（F1-b）—— 门禁达标：既有行为不许被这次前移改坏
# ══════════════════════════════════════════════════════════════════════


def test_f1b_a_freeze_days_beyond_the_limit_is_refused_when_the_gate_passes(tmp_path, capsys):
    """门禁达标 ＋ `--freeze-days 91` ⇒ 退出码 2（P49 既有行为，防回归）。"""
    path, days = _p49_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)

    code, _out, err = _judge(path, capsys, cycle=cid, asof=days[-1], fix_kind="none",
                             freeze_days=OVER)

    assert code == 2, err
    assert str(OVER) in err and "拒绝" in err and "clamp" in err
    c = _conn(path)
    try:
        counts = _counts(c)
        assert counts["m2_judgements"] == 0 and counts["validation_events"] == 0
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T3（F1-c）—— 合法区间的两端照旧工作（前移校验不误伤）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("freeze_days", [1, limits.FREEZE_MAX_DAYS])
def test_f1c_the_legal_boundary_values_still_work(tmp_path, capsys, freeze_days):
    """`1` 与 `FREEZE_MAX_DAYS` 两个端点值 ⇒ 照旧走 freeze 分支，原值落库。"""
    path, days = _p49_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)

    code, out, err = _judge(path, capsys, cycle=cid, asof=days[-1], fix_kind="none",
                            freeze_days=freeze_days)

    assert code == 0, err
    payload = json.loads(out)
    assert payload["branch"] == m2_config.BRANCH_FREEZE
    assert payload["evidence"]["freeze_days"] == freeze_days, "原值落库，不许被夹紧"


@pytest.mark.parametrize("freeze_days", [1, limits.FREEZE_MAX_DAYS])
def test_f1c_the_legal_boundary_values_still_work_on_the_insufficient_path(
        tmp_path, capsys, freeze_days):
    """门禁未过时，合法入参也照旧：`insufficient` ＋ 依据里带原值（前移不误伤这条路径）。"""
    path, _days, cid, asof = _gate_fail_case(
        tmp_path, capsys, name=f"gate-fail-legal-{freeze_days}.db")

    code, out, err = _judge(path, capsys, cycle=cid, asof=asof, fix_kind="none",
                            freeze_days=freeze_days)

    assert code == 0, err
    payload = json.loads(out)
    assert payload["branch"] == m2_config.BRANCH_INSUFFICIENT
    assert payload["conclusion"] is False and payload["actions"] == []
    assert payload["evidence"]["freeze_days"] == freeze_days


# ══════════════════════════════════════════════════════════════════════
# T4（F2）—— 幂等返回要自证「本次入参未被使用」
# ══════════════════════════════════════════════════════════════════════


def test_f2_an_existing_judgement_still_validates_this_calls_input(tmp_path, capsys):
    """已有判定行 ＋ 越界入参 ⇒ 退出码 2 ＋ 零写入（幂等**不等于**免校验）。

    修复前（P49 §10.3 实测）：`--freeze-days 999` 静默 exit 0、`status=already` ——
    调用方读到的是一个「没用过入参的成功」。
    """
    path, days = _p49_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    assert _judge(path, capsys, cycle=cid, asof=days[-1], fix_kind="none",
                  freeze_days=30)[0] == 0
    c = _conn(path)
    try:
        before = _counts(c)
    finally:
        c.close()

    code, _out, err = _judge(path, capsys, cycle=cid, asof=days[-1], fix_kind="none",
                             freeze_days=999)

    assert code == 2, f"幂等路径把越界入参静默放过了（F2）：exit={code} err={err}"
    assert "999" in err and "拒绝" in err
    c = _conn(path)
    try:
        assert _counts(c) == before, "拒绝路径不许增行"
    finally:
        c.close()


def test_f2_the_idempotent_return_says_this_calls_input_was_not_used(tmp_path, capsys):
    """已有判定行 ＋ 合法入参 ⇒ `already`，且新增两个自证键、**既有字段逐字不变**。"""
    path, days = _p49_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    code, out, err = _judge(path, capsys, cycle=cid, asof=days[-1], fix_kind="none",
                            freeze_days=30)
    assert code == 0, err
    first = json.loads(out)

    code, out, err = _judge(path, capsys, cycle=cid, asof=days[-1], fix_kind="none",
                            freeze_days=30)

    assert code == 0, err
    second = json.loads(out)
    assert second["status"] == "already"
    # ① 只增键：既有的一个不少、一个不改名，也不许多冒别的键
    assert set(second) == P49_ALREADY_KEYS | NEW_KEYS, (
        f"`already` 的键集合被改动了：多了 {sorted(set(second) - P49_ALREADY_KEYS - NEW_KEYS)}、"
        f"少了 {sorted(P49_ALREADY_KEYS - set(second))}")
    # ② 自证键在，且说的是「没使用本次入参」
    assert second["input_used"] is False
    assert isinstance(second["input_note"], str) and second["input_note"].strip()
    # ③ 既有字段**逐字不变** —— 与库里那一行逐个比，而不是与上一次的输出比
    c = _conn(path)
    try:
        stored = m2_store.find_judgement(c, cid, days[-1])
        assert _counts(c)["m2_judgements"] == 1
    finally:
        c.close()
    old = P49_ALREADY_KEYS - {"status"}
    assert {k: second[k] for k in sorted(old)} == {k: stored[k] for k in sorted(old)}, (
        "幂等返回的既有字段与库里那一行不一致 —— 键名没变但值被改写了")
    assert second["evidence"] == first["evidence"] and second["branch"] == first["branch"]


# ══════════════════════════════════════════════════════════════════════
# T5 —— 依据自证：落库的 `freeze_days` 只可能来自合法区间
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("freeze_days", [
    0, 1, 45, limits.FREEZE_MAX_DAYS, OVER, 999,
])
def test_the_stored_evidence_never_carries_an_out_of_range_freeze_days(
        tmp_path, capsys, freeze_days):
    """`insufficient` 行的 `evidence.freeze_days`（若有）必在 `[0, FREEZE_MAX_DAYS]`。

    **这是防回归的断言，不只是复述实现**：把入口校验挪回分支里面（或挪到幂等判断
    之后），越界值就会重新落进 append-only 台账（P49 §10.3 F1 的实测），这里判红。
    `0` 与 `FREEZE_MAX_DAYS` 两端都在参数里 —— 合法区间由 `config/limits.py` 定义，
    本用例不自己另定一个区间。
    """
    path, _days, cid, asof = _gate_fail_case(
        tmp_path, capsys, name=f"gate-fail-{freeze_days}.db")

    code, _out, _err = _judge(path, capsys, cycle=cid, asof=asof, fix_kind="none",
                              freeze_days=freeze_days)

    c = _conn(path)
    try:
        rows = [json.loads(r[0]) for r in
                c.execute("SELECT evidence_json FROM m2_judgements"
                          " ORDER BY judgement_id")]
    finally:
        c.close()
    if 0 <= freeze_days <= limits.FREEZE_MAX_DAYS:
        assert code == 0, f"合法值 {freeze_days} 被误拒"
        assert len(rows) == 1 and rows[0]["freeze_days"] == freeze_days
    else:
        assert code == 2, f"越界值 {freeze_days} 没被拒"
        assert rows == [], "越界入参不许落进 append-only 台账"
    for evidence in rows:                     # 不论走哪条路径，依据里的值都必须合法
        value = evidence.get("freeze_days")
        assert value is None or 0 <= value <= limits.FREEZE_MAX_DAYS, evidence
