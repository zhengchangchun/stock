"""P27：`verify pending`（补「次日验证」步）+ 缺步检测。

## 这一组测试钉住的根因（不是「命令能跑」）

实测（2026-09-16 只读查库）：
  - `trading_calendar` 的 `2026-09-16` 行 **created_at = 2026-09-16T15:32:56**；
  - 当天最后一次 `session_tick` 是 **15:23:35** —— 比日历行**早 9 分钟**；
  - `session tick` 步骤③ 的 cutoff 只来自日历（`closed_through`）→ 那次 tick 看到的
    cutoff 只能是 `2026-09-15` → 那天的 4 行早被回放写好 → `verify_inserted+0`；
  - 15:30 的收盘链用的是 `session backfill-close`（**不含验证**），根本不跑 `session tick`。

所以缺口不是「模型没产出」，而是**验证步的 cutoff 源天生滞后一天**：
日历行由 `ingest index` 在 15:30+ 才写，而 tick 最后一次跑在 15:23。

因此本文件的第一条测试不是「命令能跑」，而是：
**当 `trading_calendar` 落后于 `bars_daily` 时，判据必须仍能认出「今天已收盘」。**

fail-closed 的两条同样钉住：
  - 未来日期的 bar **不算**已收盘；
  - 两个来源都判不出来时返回 `None`（**不是 0** —— 0 会被读成「没有缺口」，
    而真相是「判不了」，见 ERROR_DIARY #36）。
"""

from __future__ import annotations

import json

from stocklab.cli.main import main
from stocklab.data.models import Bar
from stocklab.predict.service import build_predictions
from stocklab.predict.store import insert_prediction
from stocklab.session.tick import closed_through, load_calendar, run_tick
from stocklab.store.db import connect
from stocklab.verify.pending import latest_closed_session, pending_predictions
from tests.test_predict_service import _to_date, _to_ord, bars, seed

CODE = "000333"
CODE2 = "600690"


# ---------- 夹具 ----------

def _hist(n=200):
    """一段**过去**的日 K（2024 年起）—— 相对真实时钟全部已收盘。"""
    return bars(n=n)


def _bar(code, day, close=10.6):
    return Bar(code=code, date=day, open=10.5, high=10.8, low=10.2, close=close,
               volume=1000, amount=1.0, turnover=1.0, source="test",
               adj_mode="none")


def _matured(tmp_path, name="m.db", *, extra_codes=None):
    """造一个「预测的目标日 bar 已在库里」的库 —— 即「已到期却未打分」的现场。

    返回 `(asof, target)`：`target` 就是那条**已到期却没验证行**的预测的目标日。
    """
    hist = _hist()
    asof = hist[-1].date
    target = _to_date(_to_ord(asof) + 1)
    by_code = {CODE: hist + [_bar(CODE, target)]}
    if extra_codes:
        by_code[CODE2] = [_bar(CODE2, d) for d in (hist[-1].date, target)]
    seed(tmp_path / name, by_code).close()
    main(["predict", "run", "--asof", asof, "--code", CODE,
          "--db", str(tmp_path / name), "--report", str(tmp_path / "p.json")])
    return asof, target


def _verif_rows(db):
    c = connect(db)
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM verifications ORDER BY verification_id")]
    c.close()
    return rows


# ---------- 1. 到期判据：日历滞后也要认得出来（根因回归） ----------

def test_latest_closed_session_falls_back_to_bars_when_calendar_lags(tmp_path):
    """**根因回归**：日历只到 D-1，但 bars 已有 D → 最新已收盘交易日必须是 D。

    现场就是这个形状：`ingest index` 15:30+ 才写日历行，tick 15:23 就跑了。
    """
    hist = _hist()
    target = _to_date(_to_ord(hist[-1].date) + 1)
    all_dates = [b.date for b in hist]
    seed(tmp_path / "lag.db", {CODE: hist + [_bar(CODE, target)]},
         cal_dates=[d for d in all_dates if d < target]).close()

    c = connect(tmp_path / "lag.db")
    cal, _ = load_calendar(c)
    now = f"{target}T15:35:00+08:00"
    # 日历侧：只能看到 target 的前一天（这就是缺口的形状）
    assert closed_through(cal, now) == hist[-1].date
    # 判据必须靠 bars 补齐到 target
    assert latest_closed_session(c, now)[0] == target
    c.close()


def test_latest_closed_session_ignores_future_dated_bar(tmp_path):
    """fail-closed：未来日期的 bar **不算**已收盘（否则会去给未来的预测打分）。"""
    hist = _hist()
    target = _to_date(_to_ord(hist[-1].date) + 1)
    future = _to_date(_to_ord(target) + 400)
    seed(tmp_path / "fut.db", {CODE: hist + [_bar(CODE, target), _bar(CODE, future)]}).close()

    c = connect(tmp_path / "fut.db")
    now = f"{target}T15:35:00+08:00"
    assert c.execute("SELECT MAX(date) FROM bars_daily").fetchone()[0] == future
    assert latest_closed_session(c, now)[0] == target      # 不是 future
    c.close()


def test_latest_closed_session_is_none_when_undeterminable(tmp_path):
    """两个来源都判不出来 → `None`（**不是**某个猜测值）。"""
    hist = _hist()
    seed(tmp_path / "none.db", {CODE: hist}).close()
    c = connect(tmp_path / "none.db")
    # 时钟放在行情之前：两侧都得不到「已收盘」
    got, evidence = latest_closed_session(c, "2000-01-01T09:00:00+08:00")
    assert got is None
    assert evidence["bars_side"] is None and evidence["calendar_side"] is None
    c.close()


# ---------- 2. 待验证清单 ----------

def test_pending_lists_matured_unscored_prediction(tmp_path):
    _asof, target = _matured(tmp_path)
    c = connect(tmp_path / "m.db")
    rep = pending_predictions(c, f"{target}T19:00:00+08:00")
    assert rep["latest_closed_session"] == target
    assert rep["n"] == 1
    assert [(r["code"], r["target_date"]) for r in rep["rows"]] == [(CODE, target)]
    assert rep["by_target_date"] == {target: 1}
    c.close()


def test_pending_excludes_prediction_whose_target_has_not_arrived(tmp_path):
    """目标日还没到的预测**不**进清单（否则会写下一行「数据缺口」的假记录）。"""
    asof, target = _matured(tmp_path)
    # 再给「target 之后」落一条预测（asof=target → target_date 更晚）
    main(["predict", "run", "--asof", target, "--code", CODE,
          "--db", str(tmp_path / "m.db"), "--report", str(tmp_path / "p2.json")])
    c = connect(tmp_path / "m.db")
    rep = pending_predictions(c, f"{target}T19:00:00+08:00")
    assert rep["n"] == 1
    assert {r["target_date"] for r in rep["rows"]} == {target}
    c.close()


def test_pending_is_none_not_zero_when_undeterminable(tmp_path):
    """判不出来时 `n = None` —— **不许**返回 0（0 会被读成「没有缺口」, #36）。"""
    _matured(tmp_path)
    c = connect(tmp_path / "m.db")
    rep = pending_predictions(c, "2000-01-01T09:00:00+08:00")
    assert rep["latest_closed_session"] is None
    assert rep["n"] is None
    assert rep["reason"] == "cannot_determine_latest_closed_session"
    assert rep["rows"] == []
    c.close()


# ---------- 3. CLI：幂等 + append-only ----------

def test_cli_verify_pending_backfills_then_reports_nothing_to_do(tmp_path, capsys):
    """连跑两次：第一次补 1 行；第二次「无待验证」且**零写入**。"""
    _asof, target = _matured(tmp_path)
    db = str(tmp_path / "m.db")
    capsys.readouterr()                                    # 丢掉建库期的 predict 输出

    assert main(["verify", "pending", "--db", db]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["pending_before"] == 1
    assert first["pending_after"] == 0
    assert first["inserted"] == 1 and first["identical"] == 0
    assert first["targets"] == {target: {"pending": 1, "inserted": 1}}
    assert len(_verif_rows(tmp_path / "m.db")) == 1

    assert main(["verify", "pending", "--db", db]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["pending_before"] == 0
    assert second["pending_after"] == 0
    assert second["inserted"] == 0 and second["identical"] == 0
    assert second["targets"] == {}
    assert second["note"] == "无待验证的到期预测"
    assert len(_verif_rows(tmp_path / "m.db")) == 1        # 没有新行


def test_cli_verify_pending_leaves_existing_verification_rows_untouched(tmp_path, capsys):
    """append-only：**已评分**的那一行一个字节都不许动（只补缺失的那些）。"""
    hist = _hist()
    asof = hist[-1].date
    target = _to_date(_to_ord(asof) + 1)
    seed(tmp_path / "two.db", {
        CODE: hist + [_bar(CODE, target)],
        CODE2: bars(code=CODE2, n=200) + [_bar(CODE2, target)],
    }).close()
    db = str(tmp_path / "two.db")
    main(["predict", "run", "--asof", asof, "--code", CODE, "--code", CODE2,
          "--db", db, "--report", str(tmp_path / "p.json")])
    # 只给 CODE 先打过分 → CODE2 才是缺口
    assert main(["verify", "run", "--target-date", target, "--code", CODE,
                 "--db", db, "--report", str(tmp_path / "v.json")]) == 0
    before = _verif_rows(tmp_path / "two.db")
    assert len(before) == 1
    capsys.readouterr()                                    # 丢掉建库期的 predict/verify 输出

    assert main(["verify", "pending", "--db", db]) == 0
    after = _verif_rows(tmp_path / "two.db")
    assert len(after) == 2
    assert after[0] == before[0]                            # 既有行逐字段未变
    assert after[1]["target_date"] == target                # 补的是缺口那条


def test_cli_verify_pending_exit_2_when_db_missing(tmp_path, capsys):
    assert main(["verify", "pending", "--db", str(tmp_path / "nope.db")]) == 2
    assert "db not found" in capsys.readouterr().err


# ---------- 4. 缺步检测：review daily 要能自动发现 ----------

def test_review_daily_reports_pending_verifications(tmp_path, capsys):
    _asof, target = _matured(tmp_path)
    db = str(tmp_path / "m.db")
    capsys.readouterr()                                    # 丢掉建库期的 predict 输出
    assert main(["review", "daily", "--db", db, "--date", target,
                 "--out", str(tmp_path / "rev.md")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pending_verifications"] == 1
    assert out["pending_verifications_detail"]["rows"][0]["code"] == CODE
    md = (tmp_path / "rev.md").read_text()
    assert "verify pending" in md                          # 可操作提示：跑哪条命令
    assert "到期未验证" in md


def test_review_daily_reports_zero_when_nothing_pending(tmp_path, capsys):
    _asof, target = _matured(tmp_path)
    db = str(tmp_path / "m.db")
    assert main(["verify", "pending", "--db", db]) == 0
    capsys.readouterr()
    assert main(["review", "daily", "--db", db, "--date", target,
                 "--out", str(tmp_path / "rev2.md")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pending_verifications"] == 0
    assert "verify pending" not in (tmp_path / "rev2.md").read_text()


# ---------- 5. tick 步骤③ 也要用同一份判据（根因修复） ----------

def test_tick_scores_today_when_calendar_lags(tmp_path):
    """**根因回归**：日历落后一天时，收盘后 15:35 的 tick 必须能给「今天到期」打分。

    修前：cutoff 只来自日历 → cutoff = 昨天 → 今天的预测永远轮不到（inserted=0）。
    """
    hist = _hist()
    asof = hist[-1].date
    target = _to_date(_to_ord(asof) + 1)
    all_dates = [b.date for b in hist]
    conn = seed(tmp_path / "tick.db", {CODE: hist + [_bar(CODE, target)]},
                cal_dates=[d for d in all_dates if d < target])
    rep = build_predictions(conn, asof, [CODE])
    for p in rep["predictions"]:
        insert_prediction(conn, p, now=f"{asof}T19:00:00+08:00")
    assert rep["target_date"] == target

    s = run_tick(conn, now=f"{target}T15:35:00+08:00", universe=(),
                 capture=False, fetch=lambda codes: [])
    assert s["verify"]["cutoff"] == target
    assert s["verify"]["inserted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM verifications WHERE target_date=?",
                        (target,)).fetchone()[0] == 1
    conn.close()
