"""P39：`session tick` 步骤③ 的**选日**必须收窄到「含未验证预测的日期」。

## 根因（实测 2026-09-22，不是推测）

`session tick` 步骤③ 原来用一条**不看「该日是否还有未验证预测」**的 SQL 选日：

```
SELECT DISTINCT target_date FROM predictions
 WHERE status='ok' AND target_date <= ? ORDER BY target_date DESC
```

于是**已经全部验证过**的历史日期每天都被重扫一遍，并且必然用**今天的复权口径**
去重算**昨天口径下写的账本**（2026-09-21 深夜 `MODEL_VERSION` `v1.0.1→v1.0.2`，
复权链从「为空」变成「有值」）→ append-only 守卫（**正确地**）报
`verification_conflict` → 每轮 tick exit 1、`ops patrol` 每天记 `step_anomaly`。
真异常会被这条永久噪声淹没。

修法：选日**复用** `stocklab/verify/pending.py::pending_predictions()`（与 CLI
`verify pending` **同一份**实现，不在 tick 里另写平行 SQL），且逐日只把该日
**未验证的 `codes`** 交给打分器。

## 本文件四条用例

- **A 全已验证** → 该日一次都不选中、打分器零调用、exit 0、`anomalies == []`；
- **B 半缺** → 只补缺失那条，已有行逐字段不变，打分器收到的 `codes` 只含缺失项；
- **C 判不了**（`pending_predictions` 返回 `n is None`）→ 不抛、如实记、零写入，
  且**不把「判不了」写成 0**；
- **D CLI** → `verify pending` 在「全部已验证」的库上仍「无待验证、零写入、exit 0」。

## 为什么夹具不起子进程（ERROR_DIARY #55）

`run_tick` 是**直接**调用的（不经 `ops chain`），`capture=False` + `fetch` 不会被调用
⇒ 不会起任何子进程；`predict run` / `verify run` 都带 `--db` 指到 `tmp_path`。
绝不在真库上跑改动后的写入路径。
"""

from __future__ import annotations

import json

from stocklab.cli.main import main
from stocklab.data.models import Bar
from stocklab.session.tick import run_tick
from stocklab.store.db import connect
from tests.test_predict_service import _to_date, _to_ord, bars, seed

CODE = "000333"
CODE2 = "600690"


# ---------- 夹具 ----------

def _hist():
    """一段**过去**的日 K（2024 年起）—— 相对真实时钟全部已收盘。"""
    return bars(n=200)


def _bar(code, day, close=10.6):
    return Bar(code=code, date=day, open=10.5, high=10.8, low=10.2, close=close,
               volume=1000, amount=1.0, turnover=1.0, source="test",
               adj_mode="none")


def _two_code_db(tmp_path, name="p39.db"):
    """两只标的、各一条「已到期」预测。返回 `(db_path, target_date)`。"""
    hist = _hist()
    asof = hist[-1].date
    target = _to_date(_to_ord(asof) + 1)
    seed(tmp_path / name, {
        CODE: hist + [_bar(CODE, target)],
        CODE2: bars(code=CODE2, n=200) + [_bar(CODE2, target)],
    }).close()
    db = str(tmp_path / name)
    assert main(["predict", "run", "--asof", asof, "--code", CODE, "--code", CODE2,
                 "--db", db, "--report", str(tmp_path / "p.json")]) == 0
    return db, target


def _score_one(db, target, code, tmp_path):
    """用 CLI 给某一只标的分好分 —— 造出「已有验证行」的现场。"""
    assert main(["verify", "run", "--target-date", target, "--code", code,
                 "--db", db, "--report", str(tmp_path / "v.json")]) == 0


def _verif_rows(db):
    """验证行（连预测的 `code` 一起取，便于逐字段比对与指认缺失项）。"""
    c = connect(db)
    try:
        return [dict(r) for r in c.execute(
            "SELECT v.*, p.code AS predicted_code FROM verifications v"
            " JOIN predictions p ON p.pred_id = v.pred_id"
            " ORDER BY v.verification_id")]
    finally:
        c.close()


def _spy(monkeypatch):
    """把 tick 用的打分器换成「记一笔再原样委托」—— 真跑，只是被记账。

    `stocklab/session/tick.py` 里是 `from ... import verify_target`，所以打补丁打在
    **它自己的命名空间**上（`tick_mod.verify_target`）才生效。
    """
    from stocklab.session import tick as tick_mod
    from stocklab.verify.service import verify_target as real

    calls: list[dict] = []

    def spy(conn, target_date, *, codes=None, now=None, **kwargs):
        calls.append({"target_date": target_date,
                      "codes": None if codes is None else list(codes)})
        return real(conn, target_date, codes=codes, now=now, **kwargs)

    monkeypatch.setattr(tick_mod, "verify_target", spy)
    return calls


def _tick(db, target=None, *, now=None, monkeypatch=None):
    calls = _spy(monkeypatch)
    conn = connect(db)
    try:
        summary = run_tick(conn, now=now or f"{target}T19:00:00+08:00", universe=(),
                           capture=False, fetch=lambda codes: [])
    finally:
        conn.close()
    return summary, calls


# ---------- A. 全已验证：该日一次都不选中 ----------

def test_tick_does_not_rescan_a_fully_verified_date(tmp_path, monkeypatch, capsys):
    """**核心回归**：某到期日的预测**全部**已有验证行 → 不选中、不打分、exit 0、无异常。

    修前：选日不含「有没有验证行」这个条件 ⇒ 每天重扫该日 ⇒ 用今天的复权口径
    重算旧口径账本 ⇒ `verification_conflict`（真异常被噪声淹没）。
    """
    db, target = _two_code_db(tmp_path)
    _score_one(db, target, CODE, tmp_path)
    _score_one(db, target, CODE2, tmp_path)
    before = _verif_rows(db)
    assert len(before) == 2
    capsys.readouterr()                                   # 丢掉建库期 CLI 输出

    s, calls = _tick(db, target, monkeypatch=monkeypatch)

    assert calls == []                                     # 打分器一次都没被调用
    assert s["anomalies"] == []
    assert s["exit_code"] == 0 and s["ok"] is True
    assert s["verify"]["due_dates"] == 0                   # 没有「含未验证预测的日期」
    assert s["verify"]["verified_dates"] == []
    assert s["verify"]["inserted"] == 0
    assert s["verify"]["unscorable"] == []
    assert _verif_rows(db) == before                       # 已有行一个字节不动
    job = connect(db)
    try:
        row = job.execute("SELECT status, detail FROM job_runs"
                          " ORDER BY run_id DESC LIMIT 1").fetchone()
    finally:
        job.close()
    assert row["status"] == "ok" and "anomalies=0" in row["detail"]


# ---------- B. 半缺：只补缺失的那一个 code ----------

def test_tick_scores_only_the_missing_code_of_a_partly_verified_date(
        tmp_path, monkeypatch, capsys):
    """某日一半已有验证行、一半没有 → 只补缺失那条；已有行逐字段不变。"""
    db, target = _two_code_db(tmp_path)
    _score_one(db, target, CODE, tmp_path)                 # CODE 已有验证行
    before = _verif_rows(db)
    assert len(before) == 1 and before[0]["predicted_code"] == CODE
    capsys.readouterr()

    s, calls = _tick(db, target, monkeypatch=monkeypatch)

    assert calls == [{"target_date": target, "codes": [CODE2]}]   # 只把缺口交给打分器
    assert s["verify"]["due_dates"] == 1
    assert s["verify"]["verified_dates"] == [target]
    assert s["verify"]["inserted"] == 1
    assert s["exit_code"] == 0 and s["anomalies"] == []

    after = _verif_rows(db)
    assert len(after) == 2
    assert after[0] == before[0]                           # 既有行逐字段未变
    assert after[1]["predicted_code"] == CODE2             # 补的是缺口那条
    assert after[1]["target_date"] == target


# ---------- C. 判不了：不抛、如实记、零写入、不许写成 0 ----------

def test_tick_records_undetermined_cutoff_instead_of_zero(tmp_path, monkeypatch, capsys):
    """两侧都判不出「已收盘交易日」→ 沿用 skip 分支：不抛、如实记、不写成 0。

    与现状一致（`verify.skipped == no_closed_session_in_calendar`、不记 anomaly），
    但**必须带上原因码**：判不了 ≠ 没有缺口（ERROR_DIARY #36 / #37）。
    """
    db, _target = _two_code_db(tmp_path)
    before = _verif_rows(db)
    assert before == []
    capsys.readouterr()

    calls = _spy(monkeypatch)
    conn = connect(db)
    try:
        s = run_tick(conn, now="2000-01-01T09:00:00+08:00", universe=(),
                     capture=False, fetch=lambda codes: [])
    finally:
        conn.close()

    assert calls == []
    assert s["verify"]["skipped"] == "no_closed_session_in_calendar"
    assert s["verify"]["reason"] == "cannot_determine_latest_closed_session"
    assert s["verify"]["cutoff"] is None
    assert s["verify"]["due_dates"] is None                # **不是 0**
    assert _verif_rows(db) == before                       # 零写入
    assert s["exit_code"] == 0                             # 现状：skip 分支不记 anomaly


# ---------- D. CLI：全部已验证时仍「无待验证、零写入、exit 0」 ----------

def test_cli_verify_pending_is_a_noop_when_everything_is_verified(tmp_path, capsys):
    """CLI 侧与 tick 侧共用同一份选取逻辑 —— 全已验证时它同样一次都不写。"""
    db, target = _two_code_db(tmp_path)
    _score_one(db, target, CODE, tmp_path)
    _score_one(db, target, CODE2, tmp_path)
    before = _verif_rows(db)
    capsys.readouterr()

    assert main(["verify", "pending", "--db", db]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pending_before"] == 0 and out["pending_after"] == 0
    assert out["inserted"] == 0 and out["identical"] == 0
    assert out["targets"] == {}
    assert out["note"] == "无待验证的到期预测"
    assert _verif_rows(db) == before
