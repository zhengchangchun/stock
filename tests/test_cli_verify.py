"""Task 34/35 的 CLI 层：`verify run` 与 `verify backfill`（含「回放 = 同一个算法」）。

这里最重要的两条不是「命令能跑」，而是：

  - **回放不是另写一套算法**：`backfill` 落库的载荷必须与 `predict run` 逐日同 hash；
  - **报告幂等**：同参数重复跑 → 报告文件 sha256 一致（否则「准确率」可以被跑两次改掉）。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from stocklab.cli.main import main
from stocklab.data.models import Bar
from stocklab.predict.service import build_predictions
from stocklab.predict.store import payload_from_row
from stocklab.predict.model import payload_hash
from stocklab.store.db import connect
from tests.test_predict_service import _to_date, _to_ord, bars, seed

CODE = "000333"


def _env(tmp_path, n=200):
    hist = bars(n=n)
    conn = seed(tmp_path / "a.db", {CODE: hist})
    return conn, hist


def _predict_cli(conn, hist, tmp_path, asof, name="p.json"):
    return main(["predict", "run", "--asof", asof, "--code", CODE,
                 "--db", str(tmp_path / "a.db"),
                 "--report", str(tmp_path / name)])


# ---------- verify run ----------

def test_verify_run_reports_unscorable_with_data(tmp_path, capsys):
    """真实情形：target_date 是下一交易日，当天还没有 K 线 —— 必须是「不可评分 + DATA」。"""
    conn, hist = _env(tmp_path)
    assert _predict_cli(conn, hist, tmp_path, hist[-1].date) == 0
    from stocklab.predict.service import resolve_target_date
    from stocklab.calendar.trading_calendar import Calendar

    target = resolve_target_date(Calendar.load(conn), hist[-1].date)[0]
    conn.close()

    rc = main(["verify", "run", "--target-date", target,
               "--db", str(tmp_path / "a.db"),
               "--report", str(tmp_path / "v.json")])
    out = capsys.readouterr()
    assert rc == 0
    rep = json.loads((tmp_path / "v.json").read_text())
    row = rep["rows"][0]
    assert row["scorable"] is False
    assert row["reason_code"] == "NO_BAR_TARGET"
    assert row["attribution_auto"] == "DATA"
    assert row["total_score"] is None
    assert "NO_BAR_TARGET" in out.err


def test_verify_run_scores_when_the_bar_exists(tmp_path):
    conn, hist = _env(tmp_path)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=10.5, high=10.8, low=10.2,
                 close=10.6, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    repo_conn = seed(tmp_path / "b.db", {CODE: hist + extra})
    assert main(["predict", "run", "--asof", asof, "--code", CODE,
                 "--db", str(tmp_path / "b.db"),
                 "--report", str(tmp_path / "p2.json")]) == 0
    repo_conn.close()

    assert main(["verify", "run", "--target-date", target,
                 "--db", str(tmp_path / "b.db"),
                 "--report", str(tmp_path / "v2.json")]) == 0
    rep = json.loads((tmp_path / "v2.json").read_text())
    assert rep["rows"][0]["scorable"] is True
    assert rep["rows"][0]["attribution_auto"] == "UNDETERMINED"
    assert rep["rows"][0]["hit_direction"] in (0, 1)


def test_verify_run_exit_2_when_no_predictions(tmp_path, capsys):
    conn, _ = _env(tmp_path)
    conn.close()
    assert main(["verify", "run", "--target-date", "2030-01-01",
                 "--db", str(tmp_path / "a.db")]) == 2
    assert "没有任何预测" in capsys.readouterr().err


def test_verify_run_report_is_byte_identical_on_rerun(tmp_path):
    """幂等：第二次跑全部命中 `identical`，报告内容**逐字节**一样。"""
    conn, hist = _env(tmp_path)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=10.5, high=10.8, low=10.2,
                 close=10.6, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    seed(tmp_path / "c.db", {CODE: hist + extra}).close()
    main(["predict", "run", "--asof", asof, "--code", CODE,
          "--db", str(tmp_path / "c.db"), "--report", str(tmp_path / "p.json")])
    conn.close()

    digests = []
    for i in (1, 2):
        out = tmp_path / f"v{i}.json"
        assert main(["verify", "run", "--target-date", target,
                     "--db", str(tmp_path / "c.db"),
                     "--report", str(out)]) == 0
        digests.append(hashlib.sha256(out.read_bytes()).hexdigest())
    assert digests[0] == digests[1]
    # 只写了一行（第二次是 identical，没有新行）
    c = connect(tmp_path / "c.db")
    assert c.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1
    c.close()


# ---------- verify backfill ----------

def test_backfill_payloads_match_predict_run(tmp_path):
    """**回放 = 同一个算法**：`backfill` 落库的载荷必须与 `predict run` 同 hash。

    这条是「历史回放即样本外评估」的前提 —— 一旦回放走的是另一条代码路径，
    报告里的数字就不再是「模型当时会给的预测」的成绩。
    """
    conn, hist = _env(tmp_path)
    from stocklab.predict.service import PitCache
    from stocklab.verify.replay import backfill

    rng = (hist[5].date, hist[-1].date)
    backfill(conn, rng[0], rng[1], codes=[CODE], cache=PitCache(),
             now="2026-09-15T19:00:00+08:00")

    # 取样：回放**实际写过**的 asof（首/中/尾），逐个用非缓存路径重算并比 hash
    dates = [r["asof_date"] for r in conn.execute(
        "SELECT DISTINCT asof_date FROM predictions ORDER BY asof_date")]
    assert len(dates) >= 5, "回放应当写过逐日预测"
    sampled = [dates[0], dates[len(dates) // 2], dates[-1]]
    for asof in sampled:
        rep = build_predictions(conn, asof, [CODE])
        assert rep["predictions"], asof
        for p in rep["predictions"]:
            row = conn.execute(
                "SELECT * FROM predictions WHERE code=? AND asof_date=?",
                (CODE, asof)).fetchone()
            assert payload_hash(payload_from_row(row)) == payload_hash(p), asof


def test_backfill_writes_verifications_and_a_deterministic_report(tmp_path):
    conn, hist = _env(tmp_path)
    from stocklab.predict.service import PitCache
    from stocklab.verify.replay import backfill, load_verification_rows
    from stocklab.verify.report import render_markdown, summarize

    rng = (hist[5].date, hist[-1].date)
    backfill(conn, rng[0], rng[1], codes=[CODE], cache=PitCache(),
             now="2026-09-15T19:00:00+08:00")
    rows = load_verification_rows(conn, rng[0], rng[1])
    assert rows, "回放应当产出可评分的验证行"
    assert all(r["scorable"] for r in rows)
    assert {r["model_version"] for r in rows}          # 版本必须带上（分组要用）

    md1 = render_markdown(summarize(rows, from_date=rng[0], to_date=rng[1]))
    md2 = render_markdown(summarize(rows, from_date=rng[0], to_date=rng[1]))
    assert md1 == md2
    assert hist[-1].date in md1


def test_cli_backfill_end_to_end(tmp_path, capsys):
    conn, hist = _env(tmp_path)
    rng = (hist[5].date, hist[-1].date)
    conn.close()
    rc = main(["verify", "backfill", "--from", rng[0], "--to", rng[1],
               "--code", CODE, "--db", str(tmp_path / "a.db"),
               "--report", str(tmp_path / "acc.md")])
    out = capsys.readouterr().out
    assert rc == 0
    # stdout 必须把关键数字吐出来（不然「跑通了」只能靠看文件）
    assert '"direction_accuracy_row"' in out and '"effective_n_days"' in out
    md = (tmp_path / "acc.md").read_text()
    assert "准确率报告" in md
    assert (tmp_path / "acc.json").exists()
    assert "历史回放" in md


def test_cli_backfill_is_idempotent(tmp_path):
    conn, hist = _env(tmp_path)
    rng = (hist[5].date, hist[-1].date)
    conn.close()
    args = ["verify", "backfill", "--from", rng[0], "--to", rng[1], "--code", CODE,
            "--db", str(tmp_path / "a.db")]
    assert main([*args, "--report", str(tmp_path / "a.md")]) == 0
    assert main([*args, "--report", str(tmp_path / "b.md")]) == 0
    assert (tmp_path / "a.md").read_bytes() == (tmp_path / "b.md").read_bytes()


def test_cli_backfill_is_idempotent_across_a_data_gap(tmp_path):
    """区间里含**不可评分**记录时，第二次回放仍必须幂等（这是真实事故的形状）。

    600690 在中间缺一根 bar（当日是交易日、000333 有行情）→ 那天它的验证行
    `scorable=False`。这种行的 `invalidated` 在内存里是 `None`、入库被逼成 `0`；
    若幂等比较不先规约，第二次跑就会把它误判成「内容不同」而中止 ——
    全历史回放第二次跑就是这么崩在 `pred_id=27` 上的（ERROR_DIARY #10）。
    """
    a = bars(n=200)
    b = bars("600690", n=200)
    hole = a[150].date
    seed(tmp_path / "gap.db",
         {CODE: a, "600690": [x for x in b if x.date != hole]}).close()

    args = ["verify", "backfill", "--from", a[5].date, "--to", a[-1].date,
            "--db", str(tmp_path / "gap.db")]
    assert main([*args, "--report", str(tmp_path / "g1.md")]) == 0
    c = connect(tmp_path / "gap.db")
    n1 = c.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
    gaps = c.execute("SELECT COUNT(*) FROM verifications"
                     " WHERE actual_close IS NULL").fetchone()[0]
    c.close()
    assert gaps >= 1, "这个 fixture 必须真的造出「不可评分」记录，否则测不到目标 bug"

    assert main([*args, "--report", str(tmp_path / "g2.md")]) == 0
    c = connect(tmp_path / "gap.db")
    assert c.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == n1
    c.close()
    assert (tmp_path / "g1.md").read_bytes() == (tmp_path / "g2.md").read_bytes()
