"""模块2 验证周期主干：start / round / end / judge / fuse-check / status（P49 §1–§2）。

## 三张台账表的**唯一写入口**，以及它为什么必须只有一条

`validation_cycles` / `validation_rounds` / `validation_events` 都是 append-only
（触发器拦 UPDATE/DELETE），但「只能追加」不等于「追加得对」：
如果判定、熔断、收尾各写各的，就会出现「判据说要冻结 → 事件里已经冻了」
这种**建议与执行混在一起**的台账，事后无法回答「当时是谁按下去的」。
所以写入只走这一个模块，而其中**判定与熔断都只写「建议 / 事件」**：

| 动作 | 写什么 | **不写什么** |
|---|---|---|
| `judge` | `m2_judgements` 一行（建议 + 依据 + 判据原文） | 不动 `plugin_scripts` / 账户 / 参数 |
| `fuse_check` | `validation_events` 两行（`circuit_breaker` + `validation_end`，同一事务） | 不 UPDATE 任何既有行 |
| `end_cycle` | `validation_events` 一行（`validation_end`） | 同上 |
| `start_cycle` / `add_round` | 周期 / 轮次各一行 | 同上 |

`plugin_scripts` 的状态迁移（`validating` / `frozen`）**不在本模块**：那是人工
闸门（D-1/D-24）后的动作，`approve` 只能由人工 CLI 触发。本模块被
`tests/test_p49_selfeval.py` 的 AST 扫描钉住：不能出现 `approve` / 写插桩 /
写账户 / 写净值这些调用。

## 事务：拒绝之前写的任何行都要回滚

`start_cycle` / `add_round` / `judge` 的全部校验都在**写库之前**完成；
`fuse_check` 要写两行（熔断 + 收尾），两行放进**同一个事务** ——
一次失败不得留下半截状态（「触及阈值但周期还活着」正是半截状态的样子）。

## 幂等是**结构**的，不是口头的

- `m2_judgements`：`(cycle_id, asof_date)` UNIQUE；
- `validation_events`：`circuit_breaker` 与 `validation_end` 各一条**部分唯一索引**
  （一个周期至多一次熔断、至多一次收尾）。

所以重放同样的输入不会多出一行；`find_event` / `find_judgement` 只是为了让返回值
能说清「是这次写的还是上次写的」，不是防线本身。
"""

from __future__ import annotations

import datetime
import sqlite3

from stocklab.config import limits
from stocklab.m2 import config as m2_config
from stocklab.m2 import selfeval
from stocklab.m2 import store as m2_store
from stocklab.paper import store as paper_store
from stocklab.store import validation as ledger

KIND_CIRCUIT_BREAKER = "circuit_breaker"
KIND_VALIDATION_END = "validation_end"


class CycleConflict(ValueError):
    """与库里的既有行冲突（周期已存在 / 已收尾 / 轮次已存在）—— 退出码 1。

    继承 `ValueError` 是为了让 CLI 的 `except ValueError` 一处收口，**但退出码
    分开**（`_cycle_fail` 按类型判 1 / 2）：ADR-019 的纪律是一个语义一个退出码，
    「你给的合法但与已有的撞了」与「你给的不合法」必须分得开。
    """


def _iso_date(value: str, *, field: str) -> str:
    """严格 `YYYY-MM-DD`。宽松解析会把 `2026-9-1` 与 `2026-09-01` 落成两行不同的事实。"""
    text = str(value).strip()
    try:
        parsed = datetime.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field}={value!r} 不是 YYYY-MM-DD —— 拒绝，不做补零/猜测") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{field}={value!r} 不是规范写法（应为 {parsed.isoformat()}）"
                         "—— 拒绝，不做规范化")
    return text


def _load_cycle(conn: sqlite3.Connection, cycle_id: int) -> dict:
    cycle = ledger.get_cycle(conn, int(cycle_id))
    if cycle is None:
        raise ValueError(f"没有 cycle_id={cycle_id} 的验证周期 —— 先跑 "
                         "`stocklab m2 cycle start`")
    return cycle


def _ended(conn: sqlite3.Connection, cycle_id: int) -> dict | None:
    return ledger.find_event(conn, int(cycle_id), KIND_VALIDATION_END)


# ---------- start ----------


def start_cycle(conn: sqlite3.Connection, *, script_id: int, account_id: str,
                planned_rounds: int, planned_days: int, params: dict,
                criteria_text: str, start_date: str, now: str) -> dict:
    """开一轮验证周期。方案越界 → `limits.PlanOutOfBounds`（**不 clamp、不落库**）。

    同一 `(策略版本, 账户)` 已有**未收尾**的周期 → `CycleConflict`（退出码 1）：
    同一版本同一时间跑两个验证周期，「这一轮的成绩」就没有唯一所指。
    """
    limits.check_validation_plan(rounds=int(planned_rounds), days=int(planned_days))
    if not str(criteria_text).strip():
        raise ValueError("criteria_text 不许为空 —— 台账必须留下判据**原文**（D-31）")
    start = _iso_date(start_date, field="start_date")
    for row in ledger.list_cycles(conn, script_id=int(script_id)):
        if str(row["account_id"]) != str(account_id):
            continue
        if _ended(conn, int(row["cycle_id"])) is None:
            raise CycleConflict(
                f"策略版本 {script_id} 的账户 `{account_id}` 已有未收尾的周期 "
                f"cycle_id={row['cycle_id']}（start {row['start_date']}）—— "
                "先 `m2 cycle end --cycle <id>`，或换一个策略版本（D-26）")
    cycle_id = ledger.insert_cycle(
        conn, script_id=int(script_id), account_id=str(account_id),
        planned_rounds=int(planned_rounds), planned_days=int(planned_days),
        params=dict(params), criteria_text=str(criteria_text),
        start_date=start, now=now)
    return {
        "status": "created", "cycle_id": cycle_id, "script_id": int(script_id),
        "account_id": str(account_id), "planned_rounds": int(planned_rounds),
        "planned_days": int(planned_days), "start_date": start,
        "criteria_text": str(criteria_text),
        "note": ("已开周期。**判据原文已落库**（事后不许换口径）；"
                 "冻结/优化/微调的执行仍走人工 `approve`（D-1/D-24）"),
    }


# ---------- round ----------


def add_round(conn: sqlite3.Connection, *, cycle_id: int, round_no: int,
              asof: str, now: str, note: str | None = None) -> dict:
    """落一轮读数（**读数由 P41/P48 的既有取数函数算出**，不由调用方给）。

    边界（越界即拒，**不 clamp**）：
    - `round_no` ∈ `[1, planned_rounds]`；
    - `asof` ≥ `start_date` 且距起跑 **≤ `VALIDATION_MAX_DAYS`**（总时长上限）。
    冲突：`(cycle_id, round_no)` 已有行 → `CycleConflict`（补跑不许静默覆盖）。
    """
    from stocklab.labweb import m2_data          # 取数函数在这里（P41/P48 同源）

    cycle = _load_cycle(conn, cycle_id)
    if _ended(conn, cycle["cycle_id"]) is not None:
        raise CycleConflict(f"周期 {cycle['cycle_id']} 已收尾 —— 不再追加轮次")
    round_no = int(round_no)
    if not (1 <= round_no <= int(cycle["planned_rounds"])):
        raise limits.PlanOutOfBounds(
            f"轮次 {round_no} 不在 [1, {cycle['planned_rounds']}] 内（该周期的计划轮次）"
            " —— 拒绝，请重新生成方案（不做 clamp）")
    date = _iso_date(asof, field="asof")
    start = _iso_date(cycle["start_date"], field="start_date")
    elapsed = (datetime.date.fromisoformat(date)
               - datetime.date.fromisoformat(start)).days
    if elapsed < 0:
        raise ValueError(f"asof={date} 早于周期起跑日 {start} —— 拒绝"
                         "（验证期不能用起跑之前的数据）")
    if elapsed > limits.VALIDATION_MAX_DAYS:
        raise limits.PlanOutOfBounds(
            f"验证期总时长 {elapsed} 天（{start} → {date}）超过 "
            f"{limits.VALIDATION_MAX_DAYS} 天 —— 拒绝，请重新生成方案（不做 clamp）")
    readings = m2_data.cycle_readings(conn, cycle, date)
    try:
        round_id = ledger.insert_round(
            conn, cycle_id=int(cycle["cycle_id"]), round_no=round_no,
            window_start=readings["window"][0] or start, window_end=date,
            metrics=readings, note=note, now=now)
    except sqlite3.IntegrityError as exc:
        raise CycleConflict(
            f"周期 {cycle['cycle_id']} 的第 {round_no} 轮已有读数行（append-only，"
            f"补跑不许静默覆盖）：{exc}") from exc
    return {"status": "ran", "round_id": round_id, "cycle_id": int(cycle["cycle_id"]),
            "round_no": round_no, "asof": date, "metrics": readings}


# ---------- end ----------


def end_cycle(conn: sqlite3.Connection, *, cycle_id: int, reason: str,
              now: str) -> dict:
    """周期收尾：追加一条 `validation_end` 事件（**不改写任何既有行**）。"""
    cycle = _load_cycle(conn, cycle_id)
    if not str(reason).strip():
        raise ValueError("reason 不许为空 —— 收尾原因要能被事后读到")
    existing = _ended(conn, cycle["cycle_id"])
    if existing is not None:
        return {"status": "already", "event_id": int(existing["event_id"]),
                "cycle_id": int(cycle["cycle_id"]),
                "reason": str(existing["reason"])}
    event_id = ledger.insert_event(
        conn, cycle_id=int(cycle["cycle_id"]), script_id=int(cycle["script_id"]),
        kind=KIND_VALIDATION_END, at_value=None, threshold=None,
        criteria_text=str(cycle["criteria_text"]), reason=str(reason), now=now)
    return {"status": "ran", "event_id": event_id,
            "cycle_id": int(cycle["cycle_id"]), "reason": str(reason)}


# ---------- fuse-check（D-27） ----------


def fuse_check(conn: sqlite3.Connection, *, cycle_id: int, asof: str,
               now: str) -> dict:
    """提前熔断检查：**只追加**，同输入可复现，`(周期, asof)` 幂等。

    触发（`at_value ≥ CIRCUIT_BREAKER_DRAWDOWN`）时在一个事务里写两行：
    `circuit_breaker`（`at_value` 与 `threshold` 分列 + 判据原文）与
    `validation_end`（本轮验证终止）。重放同一输入 → 返回**已存的那两个数**，
    逐位相同。
    """
    cycle = _load_cycle(conn, cycle_id)
    date = _iso_date(asof, field="asof")
    existing = ledger.find_event(conn, int(cycle["cycle_id"]), KIND_CIRCUIT_BREAKER)
    if existing is not None:
        return {
            "status": "already", "cycle_id": int(cycle["cycle_id"]), "asof": date,
            "tripped": True, "at_value": existing["at_value"],
            "threshold": existing["threshold"],
            "criteria_text": existing["criteria_text"],
            "reason": existing["reason"], "event_id": int(existing["event_id"]),
            "note": "该周期已熔断（结构性幂等键：一个周期至多一条熔断事件）—— "
                    "返回**当时**落库的实测值与阈值，不重算、不改写",
        }
    if _ended(conn, cycle["cycle_id"]) is not None:
        raise CycleConflict(
            f"周期 {cycle['cycle_id']} 已收尾 —— 不再做熔断检查"
            "（先收尾后触发的顺序不可能发生，报出来而不是静默跳过）")

    rows = [r for r in paper_store.load_nav(conn, str(cycle["account_id"]),
                                           asof=date)
            if str(r["date"]) >= str(cycle["start_date"])]
    verdict = selfeval.fuse_verdict(rows)
    out = {
        "status": "ok", "cycle_id": int(cycle["cycle_id"]), "asof": date,
        "account_id": str(cycle["account_id"]), "tripped": bool(verdict["tripped"]),
        "at_value": verdict["at_value"], "threshold": verdict["threshold"],
        "criteria_text": verdict["criteria_text"], "reason": verdict["reason"],
        "trough_date": verdict["trough_date"], "n_obs": verdict["n_obs"],
        "window": [verdict["window_start"], verdict["window_end"]],
        "event_id": None,
    }
    if not verdict["tripped"]:
        return out
    reason = (f"[asof {date}] {verdict['reason']} —— 本轮策略失效，终止本轮验证"
              f"（判据原文：{verdict['criteria_text']}）")
    try:
        with conn:                                  # 两行同事务：不留半截状态
            event_id = ledger.insert_event(
                conn, cycle_id=int(cycle["cycle_id"]),
                script_id=int(cycle["script_id"]), kind=KIND_CIRCUIT_BREAKER,
                at_value=verdict["at_value"], threshold=verdict["threshold"],
                criteria_text=verdict["criteria_text"], reason=reason, now=now,
                commit=False)
            ledger.insert_event(
                conn, cycle_id=int(cycle["cycle_id"]),
                script_id=int(cycle["script_id"]), kind=KIND_VALIDATION_END,
                at_value=None, threshold=None,
                criteria_text=str(cycle["criteria_text"]),
                reason=f"[asof {date}] 熔断终止：{verdict['reason']}", now=now,
                commit=False)
    except sqlite3.IntegrityError as exc:           # 并发下第二个写者：返回既有行
        existing = ledger.find_event(conn, int(cycle["cycle_id"]),
                                     KIND_CIRCUIT_BREAKER)
        if existing is None:
            raise
        return {**out, "status": "already", "event_id": int(existing["event_id"]),
                "at_value": existing["at_value"],
                "threshold": existing["threshold"],
                "reason": existing["reason"], "note": f"并发重放：{exc}"}
    return {**out, "status": "tripped", "event_id": event_id,
            "note": "熔断即终止本轮验证：账户不再下单由通路侧读「周期已终止」决定"
                    "（D-27），本模块只把状态与判据暴露出去"}


# ---------- judge（D-44） ----------


def judge(conn: sqlite3.Connection, *, cycle_id: int, asof: str, fix_kind: str,
          freeze_days: int | None, now: str) -> dict:
    """三分支判定：写一行**建议 + 依据**，**不执行任何动作**。

    判定所用读数全部来自 P41/P48 的既有函数（`m2_data.cycle_readings` /
    `m2_data.case_summary`），本模块不重算任何一个指标。
    幂等键 = `(cycle_id, asof_date)`。
    """
    from stocklab.labweb import m2_data

    cycle = _load_cycle(conn, cycle_id)
    date = _iso_date(asof, field="asof")
    if _ended(conn, cycle["cycle_id"]) is not None:
        raise CycleConflict(f"周期 {cycle['cycle_id']} 已收尾 —— 不再判定")
    existing = m2_store.find_judgement(conn, int(cycle["cycle_id"]), date)
    if existing is not None:
        return {"status": "already", **existing}
    readings = m2_data.cycle_readings(conn, cycle, date)
    cases = m2_data.case_summary(conn, cycle, date)
    verdict = selfeval.decide_branch(
        n_rounds=len(ledger.list_rounds(conn, int(cycle["cycle_id"]))),
        planned_rounds=int(cycle["planned_rounds"]),
        planned_days=int(cycle["planned_days"]),
        gate=readings["sample_gate"], metrics=readings["metrics"],
        excess=readings["excess_vs_index_300"], cases=cases,
        fix_kind=str(fix_kind),
        freeze_days=(None if freeze_days is None else int(freeze_days)))
    evidence = {**verdict["evidence"], "reason": verdict["reason"],
                "actions": list(verdict["actions"]),
                "conclusion": bool(verdict["conclusion"]),
                "readings_window": list(readings["window"]),
                "criteria_text": str(cycle["criteria_text"])}
    try:
        judgement_id = m2_store.insert_judgement(
            conn, cycle_id=int(cycle["cycle_id"]),
            script_id=int(cycle["script_id"]), asof=date,
            branch=verdict["branch"], evidence=evidence,
            criteria_text=str(cycle["criteria_text"]), now=now)
    except sqlite3.IntegrityError as exc:            # 并发重放
        existing = m2_store.find_judgement(conn, int(cycle["cycle_id"]), date)
        if existing is None:
            raise
        return {"status": "already", **existing}
    return {
        "status": "judged", "judgement_id": judgement_id,
        "cycle_id": int(cycle["cycle_id"]), "script_id": int(cycle["script_id"]),
        "asof": date, "branch": verdict["branch"],
        "branch_label": verdict["branch_label"],
        "conclusion": bool(verdict["conclusion"]), "reason": verdict["reason"],
        "actions": list(verdict["actions"]), "evidence": evidence,
        "criteria_text": str(cycle["criteria_text"]),
        "note": ("**建议不是执行**：本命令不改策略版本状态、不改账户、不改参数；"
                 "三个分支的落地动作一律要人 `approve`（D-1/D-24）"),
    }


# ---------- status（只读） ----------


def status(conn: sqlite3.Connection, *, cycle_id: int) -> dict:
    """周期现状（只读）：轮次 / 事件 / 判定。**一个字段都不改**。"""
    cycle = _load_cycle(conn, cycle_id)
    cid = int(cycle["cycle_id"])
    return {
        "cycle": cycle,
        "rounds": ledger.list_rounds(conn, cid),
        "events": ledger.list_events(conn, cid),
        "judgements": m2_store.list_judgements(conn, cycle_id=cid),
        "ended": _ended(conn, cid) is not None,
        "fused": ledger.find_event(conn, cid, KIND_CIRCUIT_BREAKER) is not None,
        "boundaries": dict(selfeval.BOUNDARIES),
        "branch_labels": dict(m2_config.BRANCH_LABELS),
    }
