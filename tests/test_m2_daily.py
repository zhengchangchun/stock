"""P54：模块2 日更接线（`m2 daily` + `ops close` 的 `m2_daily` 一步）。

| 判据（任务书 §3） | 用例 |
|---|---|
| T1 链上**只**多一步、位置与 argv 正确、plist **逐字节不变** | `test_t1_*` |
| T2 **没有在飞版本 ⇒ skipped 且链仍绿**（不是失败） | `test_t2_*` |
| T3 **配置了却坏了 ⇒ 报红**（rejected 不许静默） | `test_t3_*` |
| T4 当日 K 线**未定型**闸门 fail-closed（三表一行不写） | `test_t4_*` |
| T5 幂等：同一天跑两次 ⇒ `already`、行数不变 | `test_t5_*` |
| ＋ | 输入不合法 / 非交易日 / 在飞过滤（frozen、熔断） |

三条不许越的线（都在用例里钉住）：

1. **闸门复用 P46 那一道**（`session/close.py::bars_finalized_on`）—— 本站**不新写判据**；
2. **退出码不发明新的**：0 全跑完（含 `already` 与「无在飞版本」的 skipped）／
   2 未定型·库不可用·输入不合法／**4 有通路 `rejected`**（沿用 `cli/main.py::EXIT_REJECTED`）；
3. **测试里绝不真起子进程**：本文件的 `m2 daily` 全程走 CLI 的**进程内**路径
   （`channel_a.run` / `channel_b.run` / `score_all` 都不起子进程），`ops close` 那几条一律
   注入 `FakeRunner`（ERROR_DIARY #55：CLI 用例真跑 `ingest *` 会把半截 bar 写进真库）。

数字一律**从被测模块取**（步骤名 / 原因码 / 退出码常量），不手抄 —— 手抄的那份会漂移。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from stocklab.cli.main import main
from stocklab.labweb import m2_data, m2_render
from stocklab.m2 import channel_a, config as m2_config, daily as m2_daily
from stocklab.ops import chain, schedule
from stocklab.plugin import lifecycle, store as plugin_store
from stocklab.store import validation as ledger
from stocklab.store.db import connect
from tests.test_m2_channels import (ACCOUNT, CAL, DAY2, NOW, POOL, START, VERSION,
                                    build_db)
from tests.test_ops_close import (CLOSE_NOW, TODAY, _default_db, _green)
from tests.test_ops_patrol import FakeRunner

NEXT = "2026-09-17"                      # 目标日：让打分**真的**落一行
STEP_B = m2_daily.STEP_CHANNEL_B
STEP_A = m2_daily.STEP_CHANNEL_A
STEP_SCORE = m2_daily.STEP_SCORE
TABLES = (m2_config.TABLE_RUNS, m2_config.TABLE_FORECASTS, m2_config.TABLE_SCORES)


# ---------- 夹具 ----------

def _add_next_session(db: Path) -> None:
    """补一天（`NEXT` = 09-17）：日历一行 + 全部标的的行情。

    没有它，`m2 score` 只会数出 `pending`（**决策日的预测，目标日必然是下一个
    交易日** —— 当天就打分等于零滞后自证，`m2/score.py` 明令不许），
    于是「打分总是跑」这条判据会空转。日历一并补上：`assert_session` 用的是
    「日历 ∩ 行情轴」，只补行情是不合法的交易日。
    """
    c = connect(db)
    c.execute("INSERT INTO trading_calendar (date, is_open, source, created_at)"
              " VALUES (?,1,'tencent',?)", (NEXT, NOW))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,100,100,100,100,1,'none','x',?)",
        [(code, NEXT, NOW) for code in (*POOL, "sh000300")])
    c.commit()
    c.close()


def _daily(db: Path, *, capsys, asof: str = DAY2, now: str | None = NOW):
    """跑一次 `m2 daily`，返回 `(exit_code, payload, stderr)`。"""
    argv = ["m2", "daily", "--asof", asof, "--db", str(db)]
    if now is not None:
        argv += ["--now", now]
    code = main(argv)
    out = capsys.readouterr()
    payload = json.loads(out.out) if out.out.strip() else None
    return code, payload, out.err


def _counts(db: Path) -> dict[str, int]:
    c = connect(db)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in TABLES}
    finally:
        c.close()


def _rows(db: Path, table: str) -> list[tuple]:
    c = connect(db)
    try:
        return [tuple(r) for r in c.execute(f"SELECT * FROM {table}")]
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T1 —— 单一入口：B → A（在飞版本）→ 打分，顺序即依赖顺序
# ══════════════════════════════════════════════════════════════════════


def test_t1_daily_runs_b_then_a_then_score_in_that_order(tmp_path, capsys):
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _add_next_session(db)

    code, payload, _ = _daily(db, capsys=capsys)

    assert code == 0 and payload["status"] == m2_config.STATUS_RAN
    assert [s["step"] for s in payload["steps"]] == [STEP_B, STEP_A, STEP_SCORE]
    by = {s["step"]: s for s in payload["steps"]}
    assert by[STEP_B]["status"] == m2_config.STATUS_RAN
    assert by[STEP_B]["account_id"] == m2_config.MIRROR_ACCOUNT
    assert by[STEP_A]["status"] == m2_config.STATUS_RAN
    assert by[STEP_A]["strategy_version"] == VERSION
    assert by[STEP_A]["account_id"] == ACCOUNT
    assert by[STEP_A]["exit_code"] == 0
    # 台账两行：B 一行、A 一行（**没有**为「打分了」写台账行 —— 那是另一张表的事）
    runs = _rows(db, m2_config.TABLE_RUNS)
    assert sorted(r[1] for r in runs) == [m2_config.CHANNEL_A, m2_config.CHANNEL_B]
    assert _counts(db)[m2_config.TABLE_FORECASTS] > 0
    # 当天新建的预测目标日必然是**下一个**交易日 ⇒ 打分这一步只会数出 pending
    assert by[STEP_SCORE]["counts"] == {
        "asof": DAY2, "n_sessions": 4, "scanned": 4, "scored": 0, "unscorable": 0,
        "already": 0, "pending": 4}


def test_t1_score_lands_rows_on_the_following_day(tmp_path, capsys):
    """打分**总是跑**，而且真的会落行：`asof` 推到 `NEXT` 之后，昨天的预测到期。"""
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _add_next_session(db)
    assert _daily(db, capsys=capsys)[0] == 0                  # 09-16

    code, payload, _ = _daily(db, asof=NEXT, capsys=capsys)   # 09-17

    assert code == 0
    counts = {s["step"]: s for s in payload["steps"]}[STEP_SCORE]["counts"]
    assert counts["scored"] == 2, counts             # 000333 的两条（A3 / B1）
    assert counts["unscorable"] == 2, counts         # 两只 ETF 没有复权因子（夹具属性）
    assert counts["scored"] + counts["unscorable"] == 4
    assert _counts(db)[m2_config.TABLE_SCORES] == 4  # 昨天那批今天**全部**有结论


def test_t1_a_step_passes_asof_and_strategy_version(tmp_path, capsys):
    """通路 A 的调用必须点名策略版本（账户 = `arm-agent-<版本>`，D-35）。"""
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    code, payload, _ = _daily(db, capsys=capsys)
    assert code == 0
    a = {s["step"]: s for s in payload["steps"]}[STEP_A]
    assert a["asof"] == DAY2 and a["account_id"] == ACCOUNT


def test_t1_close_runs_m2_daily_right_after_paper_step_and_before_doctor(
        tmp_path, monkeypatch):
    """**链上只多一步**：`m2_daily` 紧跟 `paper_step`、在 `doctor` 之前。

    位置不是排版偏好：`doctor` 是「整条链的自证」，必须仍在最后一步 ——
    而 `m2 daily` 依赖 `paper_step` 已经推完当日净值（通路 B 读的是人工流水镜像）。
    """
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              report_dir=tmp_path / "reports")

    names = [c["step"] for c in runner.calls]
    i = names.index("m2_daily")
    assert names[i - 1] == "paper_step"
    assert names[i + 1] == "doctor"
    assert names.count("m2_daily") == 1
    assert list(chain.CLOSE_STEP_ORDER) == names        # 顺序 = 声明顺序
    # argv：`--asof <运行当天>` + `--db <库>`（supports_db=True，与 paper_step 同族）
    assert {c["step"]: c["argv"] for c in runner.calls}["m2_daily"][3:] == [
        "m2", "daily", "--asof", TODAY, "--db", str(db)]
    assert payload["exit_code"] == 0 and payload["ok"] is True
    assert "steps=13/13" in chain.summary_line(payload)


def test_t1_the_plists_are_byte_identical_to_the_pre_wiring_ones(tmp_path):
    """`ops schedule` 的产物**逐字节不变**（反目标：不改时点与槽数）。

    判据是「接线前生成的那一份」的 sha256 —— 三份 plist **都不**含步骤表，
    所以 `CLOSE_STEPS` 多一步不该动它一个字节。基准值取自 P54 开工前
    （`render_plist` 的三个路径全注入 ⇒ 与跑测试的机器无关）。
    真机字节对照见任务书 §实施记录 T1。
    """
    golden = {
        "close": "8d33c0257bb7eebae320d3546546fc057f6000e70d5580dcf33115e54de8490c",
        "patrol": "4fd47ca89100ddf20f2e9f1c27d736aa02dc8b54fa0b5fdba9951105412a7c3c",
        "monthly": "e97ba0143c9d7bf2afda2e126d1205b33ce5214504f1548e2e5a5c5b2e7bdcbe",
    }
    for job in schedule.JOBS:
        blob = schedule.render_plist(job, project_root="/proj", python="/py",
                                     logs="/logs")
        assert hashlib.sha256(blob).hexdigest() == golden[job.name], job.name
        # 判据非空转：plist 里确实没有「步骤」这个概念（有的话上面那句才是空转）
        assert b"steps" not in blob and b"CLOSE_STEPS" not in blob


# ══════════════════════════════════════════════════════════════════════
# T2 —— 没有在飞版本 ⇒ skipped，**这不是失败**
# ══════════════════════════════════════════════════════════════════════


def test_t2_no_inflight_version_skips_with_a_reason_and_stays_green(tmp_path, capsys):
    db = build_db(tmp_path / "daily.db")            # 有 m2 插桩，但**没有** arm-agent-* 账户
    code, payload, _ = _daily(db, capsys=capsys)

    assert code == 0, "「没有在飞的策略版本」是如实结论，不是失败"
    a = {s["step"]: s for s in payload["steps"]}[STEP_A]
    assert a["status"] == m2_config.STATUS_SKIPPED
    assert m2_daily.REASON_NO_INFLIGHT in a["reason"]
    assert a["exit_code"] == 0
    # 跳过要写清「为什么、看了哪几个账户」，并且**不编造**一个账户 id
    assert a["account_id"] is None and a["strategy_version"] is None
    assert payload["versions"]["inflight"] == []
    # 通路 B 与打分不受影响（它们与人当天有没有下单无关）
    assert {s["step"]: s["status"] for s in payload["steps"]}[STEP_B] == \
        m2_config.STATUS_RAN
    assert _counts(db)[m2_config.TABLE_RUNS] == 1     # 只有 B 那一行


def test_t2_the_close_chain_is_still_green_without_any_version(tmp_path, monkeypatch):
    """链的判据：`m2_daily` exit 0 ⇒ `ops close` exit 0（配置没错就不许报红）。"""
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()                            # m2_daily 返回 0
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              report_dir=tmp_path / "reports")
    assert {s["name"]: s["exit_code"] for s in payload["steps"]}["m2_daily"] == 0
    assert payload["anomalies"] == []
    assert payload["exit_code"] == 0

# ══════════════════════════════════════════════════════════════════════
# T3 —— 配置了却坏了 ⇒ 报红（4），不许静默
# ══════════════════════════════════════════════════════════════════════


def test_t3_an_account_without_active_plugins_is_rejected_with_exit_four(tmp_path, capsys):
    """账户在（＝配置了）、a1 插桩没有 active 版本（＝坏了）⇒ **exit 4**。

    这一条与 T2 一起把「跳过」与「拒绝」分得开：前者等数据，后者要人去看。
    """
    db = build_db(tmp_path / "daily.db", sources={"m2_b1": _B1_ONLY_SOURCE})
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()

    code, payload, err = _daily(db, capsys=capsys)

    assert code == 4, "配置了却坏了必须报红，不许静默"
    assert payload["status"] == m2_config.STATUS_REJECTED
    a = {s["step"]: s for s in payload["steps"]}[STEP_A]
    assert a["status"] == m2_config.STATUS_REJECTED and a["exit_code"] == 4
    assert "m2_a1" in a["reason"] or "NoActivePlugin" in a["reason"]
    # 通路 B 单独能跑（夹具给了 m2_b1）⇒ 拒绝是**这一条通路**的，不是整轮崩掉
    assert {s["step"]: s["status"] for s in payload["steps"]}[STEP_B] == \
        m2_config.STATUS_RAN
    assert payload["n_rejected"] == 1
    # 拒绝了也要**留痕**：台账里那一行必须是 rejected（不是 ran，也不是没有行）
    c = connect(db)
    try:
        row = c.execute(f"SELECT status, reason FROM {m2_config.TABLE_RUNS}"
                        " WHERE channel = ?", (m2_config.CHANNEL_A,)).fetchone()
    finally:
        c.close()
    assert row["status"] == m2_config.STATUS_REJECTED and row["reason"]


def test_t3_the_close_receipt_names_the_failed_step(tmp_path, monkeypatch):
    """链侧：`m2_daily` 报 4 ⇒ 回执 `anomalies` 里**点名**这一步，且链不报绿。"""
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    payload = chain.run_close(db_path=db, now=CLOSE_NOW,
                              runner=FakeRunner({"m2_daily": 4}),
                              report_dir=tmp_path / "reports")
    fatal = [a for a in payload["anomalies"] if a.get("step") == "m2_daily"]
    assert [a["kind"] for a in fatal] == ["step_failed"]
    assert fatal[0]["exit_code"] == 4
    assert payload["exit_code"] != 0 and payload["ok"] is False
    assert "m2_daily=4" in chain.summary_line(payload)


# ══════════════════════════════════════════════════════════════════════
# T4 —— 当日 K 线未定型 ⇒ fail-closed（exit 2、三表一行都不写）
# ══════════════════════════════════════════════════════════════════════


def _refetch_day2(db: Path, fetched_at: str) -> None:
    """把 `DAY2` 全部 K 线的 `fetched_at` 改成本次采集时刻（**唯一变量**）。"""
    c = connect(db)
    c.execute("UPDATE bars_daily SET fetched_at = ? WHERE date = ?",
              (fetched_at, DAY2))
    c.commit()
    c.close()


def test_t4_unfinalized_same_day_bars_are_refused_with_nothing_written(tmp_path, capsys):
    """`asof == 今天`（这里由 `--now` 给出）且当天 bar 未定型 ⇒ exit 2、零写入。

    与 `predict run --asof 今天` **同一道闸门**（`bars_finalized_on`）：`m2_forecasts`
    也是 append-only，写错了退不回来（P46 事故的教训）。
    """
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _refetch_day2(db, "2026-09-16T12:06:00+08:00")      # 盘中采的 ⇒ 不是终值
    before = _counts(db)

    code, payload, err = _daily(db, asof=DAY2, now="2026-09-16T15:05:00+08:00",
                                capsys=capsys)

    assert code == 2, "未定型必须 fail-closed"
    assert payload is None or payload.get("steps") in (None, [])
    assert "未定型" in err
    assert before == _counts(db) == {t: 0 for t in TABLES}


def test_t4_the_same_day_is_let_through_once_the_bars_are_final(tmp_path, capsys):
    """反面对照（**唯一变量** = `fetched_at`）：15:30 之后同一份库照跑。

    没有这一条，「exit 2」可能只是因为别的地方坏了 —— 单变量原则。
    """
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _refetch_day2(db, "2026-09-16T15:30:03+08:00")      # 收盘链采的 ⇒ 终值

    code, payload, err = _daily(db, asof=DAY2, now="2026-09-16T15:35:00+08:00",
                                capsys=capsys)

    assert code == 0, err
    assert payload["asof"] == DAY2


def test_t4_a_historical_asof_never_touches_the_gate(tmp_path, capsys):
    """历史 asof 完全不判闸门（回放是整套预测体系的立足点）—— 即使当天 bar 未定型。"""
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _refetch_day2(db, "2026-09-16T12:06:00+08:00")

    code, payload, _ = _daily(db, asof=DAY2, now="2026-09-23T10:00:00+08:00",
                              capsys=capsys)
    assert code == 0 and payload["asof"] == DAY2


# ══════════════════════════════════════════════════════════════════════
# T5 —— 幂等：同一天跑两次
# ══════════════════════════════════════════════════════════════════════


def test_t5_the_second_run_is_already_and_writes_nothing(tmp_path, capsys):
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _add_next_session(db)

    assert _daily(db, capsys=capsys)[0] == 0                 # 09-16
    assert _daily(db, asof=NEXT, capsys=capsys)[0] == 0      # 09-17：分数落行
    after_first = {t: _rows(db, t) for t in TABLES}
    assert after_first[m2_config.TABLE_SCORES]               # 判据非空转

    code, payload, _ = _daily(db, asof=NEXT, capsys=capsys)

    assert code == 0
    by = {s["step"]: s for s in payload["steps"]}
    assert by[STEP_B]["status"] == m2_config.STATUS_ALREADY
    assert by[STEP_A]["status"] == m2_config.STATUS_ALREADY
    assert by[STEP_SCORE]["counts"]["scored"] == 0
    assert by[STEP_SCORE]["counts"]["already"] == 4
    assert {t: _rows(db, t) for t in TABLES} == after_first, "重放必须逐行不变"


# ══════════════════════════════════════════════════════════════════════
# T3（页面）—— `/lab/m2` 顶部一句「上次通路运行」（只读、不重算）
# ══════════════════════════════════════════════════════════════════════


def _page(db: Path, asof: str) -> tuple[str, dict]:
    c = connect(db)
    try:
        panel = m2_data.panel(c, asof)
        return m2_render.m2_page(panel, base="/lab", built_at=NOW), panel
    finally:
        c.close()


def test_the_page_says_never_when_no_run_has_happened(tmp_path):
    db = build_db(tmp_path / "daily.db")
    html, panel = _page(db, DAY2)
    assert panel["last_run"] is None
    assert f"{m2_daily.LAST_RUN_LABEL}：从未" in html


def test_the_page_shows_the_date_and_status_of_the_last_run(tmp_path):
    """页面顶部那一句 = **库里最后一行的读数**，不是页面上算出来的东西。"""
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _add_next_session(db)                            # 否则通路 A 会因缺当日 K 线跳过
    c = connect(db)
    m2_daily.run_daily(c, asof=NEXT, now=NOW)
    c.close()

    snap = _counts(db)
    html, panel = _page(db, NEXT)

    assert panel["last_run"]["asof"] == NEXT
    assert panel["last_run"]["status"] == m2_config.STATUS_RAN
    line = next(l for l in html.splitlines()
                if m2_daily.LAST_RUN_LABEL in l)
    assert f"{m2_daily.LAST_RUN_LABEL}：{NEXT} / {m2_config.STATUS_RAN}" in line
    # 哪条通路（B 总是跑、A 只在有在飞版本时跑）—— 不写清就答不了「上次跑的是什么」
    assert m2_config.CHANNEL_A in line
    # 只读：渲染**不动库**；同输入 ⇒ 同一页（那一句不是墙上时钟算出来的）
    assert _counts(db) == snap
    assert _page(db, NEXT)[0] == html
    assert "<form" not in html and "<button" not in html


def test_the_last_run_line_follows_a_rejected_run(tmp_path):
    """跳过 / 拒绝也要照实显示 —— 这一句的用处就是「上次跑成什么样」。"""
    db = build_db(tmp_path / "daily.db", sources={"m2_b1": _B1_ONLY_SOURCE})
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    c.close()
    _add_next_session(db)
    c = connect(db)
    out = m2_daily.run_daily(c, asof=NEXT, now=NOW)
    c.close()
    assert out["status"] == m2_config.STATUS_REJECTED

    html, panel = _page(db, NEXT)
    assert panel["last_run"]["status"] == m2_config.STATUS_REJECTED
    assert f"{m2_daily.LAST_RUN_LABEL}：{NEXT} / {m2_config.STATUS_REJECTED}" in html


# ══════════════════════════════════════════════════════════════════════
# ＋ 输入不合法 / 在飞过滤
# ══════════════════════════════════════════════════════════════════════


def test_a_malformed_or_non_session_asof_is_refused_with_exit_two(tmp_path, capsys):
    """`--asof` 不是交易日 / 不是规范日期 ⇒ exit 2，一行都不写。"""
    db = build_db(tmp_path / "daily.db")
    before = _counts(db)

    for asof in ("2026-9-16", "2026-09-19", "not-a-date"):     # 非规范 / 周六 / 非日期
        code, _, err = _daily(db, asof=asof, capsys=capsys)
        assert code == 2, asof
        assert err.strip(), asof
    assert _counts(db) == before == {t: 0 for t in TABLES}


def test_a_frozen_version_is_not_in_flight(tmp_path, capsys):
    """冻结的版本不进通路 A —— 但要在回执里**点名**它为什么被排除。"""
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    script_id = int(c.execute(
        "SELECT MIN(script_id) FROM plugin_scripts WHERE plugin_id = ?",
        (m2_config.PLUGIN_A1,)).fetchone()[0])
    lifecycle.freeze(c, script_id, actor="test", reason="夹具冻结", now=NOW)
    ledger.insert_cycle(c, script_id=script_id, account_id=ACCOUNT,
                        planned_rounds=3, planned_days=30, params={},
                        criteria_text="夹具判据", start_date=START, now=NOW)
    c.close()

    code, payload, _ = _daily(db, capsys=capsys)

    assert code == 0
    assert payload["versions"]["inflight"] == []
    excluded = {e["strategy_version"]: e["reason"]
                for e in payload["versions"]["excluded"]}
    assert "冻结" in excluded[VERSION]
    assert {s["step"]: s["status"] for s in payload["steps"]}[STEP_A] == \
        m2_config.STATUS_SKIPPED
    assert _counts(db)[m2_config.TABLE_RUNS] == 1              # 只有 B


def test_a_circuit_broken_version_is_not_in_flight(tmp_path, capsys):
    """熔断终止过的版本同样不进通路 A（D-27：终止本轮验证）。"""
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    script_id = int(c.execute(
        "SELECT MIN(script_id) FROM plugin_scripts WHERE plugin_id = ?",
        (m2_config.PLUGIN_A1,)).fetchone()[0])
    cycle_id = ledger.insert_cycle(
        c, script_id=script_id, account_id=ACCOUNT, planned_rounds=3,
        planned_days=30, params={}, criteria_text="夹具判据", start_date=START,
        now=NOW)
    ledger.insert_event(c, cycle_id=cycle_id, script_id=script_id,
                        kind="circuit_breaker", at_value=0.2, threshold=0.1,
                        criteria_text="夹具判据原文", reason="夹具熔断", now=NOW)
    c.close()

    code, payload, _ = _daily(db, capsys=capsys)

    assert code == 0
    excluded = {e["strategy_version"]: e["reason"]
                for e in payload["versions"]["excluded"]}
    assert "熔断" in excluded[VERSION]
    assert {s["step"]: s["status"] for s in payload["steps"]}[STEP_A] == \
        m2_config.STATUS_SKIPPED


def test_a_finished_cycle_keeps_the_version_in_flight(tmp_path, capsys):
    """**收尾（`validation_end`）不排除**：任务书 §1.2 只给了两条排除判据。

    这条把「本站**没有**顺手加第三条」钉住 —— 多一条静默的排除规则，
    就会让「今天为什么没跑」在回执里找不到答案。
    """
    db = build_db(tmp_path / "daily.db")
    c = connect(db)
    channel_a.create_account(c, strategy_version=VERSION, now=NOW)
    script_id = int(c.execute(
        "SELECT MIN(script_id) FROM plugin_scripts WHERE plugin_id = ?",
        (m2_config.PLUGIN_A1,)).fetchone()[0])
    cycle_id = ledger.insert_cycle(
        c, script_id=script_id, account_id=ACCOUNT, planned_rounds=3,
        planned_days=30, params={}, criteria_text="夹具判据", start_date=START,
        now=NOW)
    ledger.insert_event(c, cycle_id=cycle_id, script_id=script_id,
                        kind="validation_end", at_value=None, threshold=None,
                        criteria_text="夹具判据原文", reason="夹具收尾", now=NOW)
    c.close()

    code, payload, _ = _daily(db, capsys=capsys)
    assert code == 0
    assert payload["versions"]["inflight"] == [VERSION]
    assert {s["step"]: s["status"] for s in payload["steps"]}[STEP_A] == \
        m2_config.STATUS_RAN


#: 只装 `m2_b1` 的插桩集（T3 要造「账户在、通路 A 的插桩不在」）。
_B1_ONLY_SOURCE = """
def run(ctx):
    return {"range_80": [80.0, 95.0],
            "direction": {"up": 0.5, "flat": 0.3, "down": 0.2},
            "invalidate_if": "close < 80.0", "na_reasons": [],
            "schema_version": "t1"}
"""
