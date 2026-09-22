"""`ops patrol`（P38）的护栏。

三条主张要在这里被钉死：

1. **体检只读**（sha256 前后一致）—— 不是纪律，是 `mode=ro`；
2. **判定逐项可测**：7 个项各自缺了 → 排哪一步、退出码几；判不了 → 2（绝不给 0）；
3. **`--fix` 不越界**：非默认库 + 计划里有采集类步骤 → 拒绝，且**一次子进程都不起**
   （假执行器记数）。测试全程不联网、不起子进程。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from stocklab.cli.main import main
from stocklab.ops import patrol
from stocklab.predict.version import MODEL_VERSION
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

# 2026-09-22 是周二：盘中 10:30 / 收盘后 16:05。
NOW = "2026-09-22T10:30:00+08:00"
CLOSED_NOW = "2026-09-22T16:05:00+08:00"
TODAY = "2026-09-22"
#: 最新已收盘交易日（周一）。日历**刻意不含今天** —— 这正好是每天的真实形态：
#: 日历行由 `ingest index` 在 15:30+ 才写，盘中看它永远差一天。
L = "2026-09-21"
CAL = ("2026-09-17", "2026-09-18", "2026-09-21")
CODES = ("000333", "510300")


# ---------- 夹具 ----------

def _snap(conn, trade_date: str, ts: str, *, code: str = "000333") -> None:
    conn.execute(
        "INSERT INTO quote_snapshots (code, trade_date, ts, price, volume, source,"
        " fetched_at) VALUES (?,?,?,12.0,1000,'tencent',?)", (code, trade_date, ts, NOW))


def _pred(conn, *, code: str = "000333", asof: str = L, target: str = TODAY,
          mv: str = MODEL_VERSION) -> None:
    conn.execute(
        "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
        " direction_flat, direction_down, action, size_pct, invalidate_if,"
        " strategy_mix_json, model_version, status, created_at)"
        " VALUES (?,?,?,0.4,0.3,0.3,'hold',50.0,'x','{}',?,'ok',?)",
        (code, asof, target, mv, NOW))


def _db(tmp_path, *, report_dir: Path | None = None, **kw) -> Path:
    """一份**全绿**的库；用关键字把它逐项弄坏（每个测试只弄坏自己要测的那项）。

    默认全绿的含义：L=2026-09-21 的日线与预测在位、今天有盘中快照、没有待验证预测、
    review 报告在、模拟盘 5 行净值在、三张 PIT 表跟到 L、日历跟到 L。
    """
    path = tmp_path / "patrol.db"
    rd = report_dir or tmp_path / "reports"
    rd.mkdir(parents=True, exist_ok=True)
    init_db(path)
    c = connect(path)
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main',?,?)",
        [(code, code, "etf" if code.startswith("5") else "stock", NOW) for code in CODES])
    if kw.get("calendar", True):
        c.executemany(
            "INSERT INTO trading_calendar (date, is_open, source, created_at)"
            " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    if kw.get("bars_for_L", True):
        c.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
            " amount, adj_mode, source, fetched_at)"
            " VALUES (?,?,10.0,10.0,10.0,10.0,100,NULL,'none','x',?)",
            [(code, L, NOW) for code in CODES])
    if kw.get("bars_today"):
        # 今天也有 bar ⇒ 收盘后 `latest_closed_session` 会变成**今天**（T2 的触发条件：
        # `predict run --asof` == 今天，而这一天归 close，不归 patrol）。
        c.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
            " amount, adj_mode, source, fetched_at)"
            " VALUES (?,?,10.0,10.0,10.0,10.0,100,NULL,'none','x',?)",
            [(code, TODAY, kw.get("bars_today_fetched_at", NOW))
             for code in CODES])
    if kw.get("snapshots_today", True):
        _snap(c, TODAY, kw.get("today_ts", "20260922103000"))
    if kw.get("snapshots_for_L"):
        _snap(c, L, kw.get("L_ts", "20260921150030"))
    if kw.get("predictions", True):
        _pred(c, mv=kw.get("predictions_mv", MODEL_VERSION))
    if kw.get("pending_verification"):
        _pred(c, code="510300", asof="2026-09-18", target=L)
    if kw.get("review", True):
        (rd / f"{L}-review.md").write_text("# review\n", encoding="utf-8")
    if kw.get("paper", True):
        c.execute(
            "INSERT INTO paper_accounts (account_id, arm, start_date, initial_cash,"
            " initial_positions_json, initial_nav, params_json, created_at)"
            " VALUES ('arm-hold','hold',?,10000.0,'[]',10000.0,'{}',?)", (L, NOW))
        if kw.get("paper_nav", True):
            c.execute(
                "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
                " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
                " created_at) VALUES ('arm-hold',?,10000.0,'[]',10000.0,10000.0,0.0,"
                "0.0,0.0,10000.0,?)", (L, NOW))
    if kw.get("pit", True):
        c.execute(
            "INSERT INTO valuation_daily (code, date, source, fetched_at, created_at,"
            " resp_sha256) VALUES ('000333',?,'eastmoney',?,'h',?)", (L, NOW, NOW))
        c.execute(
            "INSERT INTO money_flow_daily (code, date, source, fetched_at, created_at,"
            " resp_sha256) VALUES ('000333',?,'sina',?,'h',?)", (L, NOW, NOW))
        c.execute("INSERT INTO adj_factors (code, date, factor, source, fetched_at)"
                  " VALUES ('000333',?,1.0,'tencent',?)", (L, NOW))
    c.commit()
    c.close()
    return path


def _check(path: Path, *, now: str = NOW, rd: Path | None = None) -> dict:
    return patrol.check_db(path, now, report_dir=rd or (path.parent / "reports"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeRunner:
    """假执行器：记下每次调用，按脚本返回退出码（**绝不真起子进程**）。"""

    def __init__(self, codes=None, *, timeout_at=None, on_call=None):
        self.calls: list[dict] = []
        self.codes = dict(codes or {})
        self.timeout_at = timeout_at
        self.on_call = on_call

    def __call__(self, step, argv, timeout):
        self.calls.append({"step": step.name, "argv": list(argv), "timeout": timeout})
        if self.on_call:
            self.on_call(step, argv)
        if self.timeout_at == step.name:
            return {"exit_code": None, "timeout": True, "duration_s": 0.0,
                    "stdout_tail": "", "stderr_tail": ""}
        code = self.codes.get(step.name, 0)
        return {"exit_code": code, "timeout": False, "duration_s": 0.0,
                "stdout_tail": f"{step.name} ok", "stderr_tail": f"{step.name} exit {code}"}


def _statuses(payload: dict) -> dict[str, str]:
    return {name: item["status"] for name, item in payload["checks"].items()}


# ---------- 主张 1：体检只读 ----------

def test_check_never_writes_the_database_file(tmp_path):
    """`mode=ro`：体检不往库里写一个字节。

    只对**库文件本身**对拍：`-shm` 与 0 字节的 `-wal` 是 SQLite 为 WAL 建的书签
    （读取方自己立，不是数据写入）。所以额外再钉一条：体检之后 `-wal` 必须是空的
    —— 里面只要有内容，就说明真把页面写进过 WAL。
    """
    db = _db(tmp_path)
    before = _sha(db)
    _check(db)
    assert _sha(db) == before
    wal = Path(str(db) + "-wal")
    assert (not wal.exists()) or wal.stat().st_size == 0


def test_check_reports_latest_closed_session_with_both_sides(tmp_path):
    """L 的判据来自两侧（日历 ∪ 行情），证据要一起给出来供审计。"""
    payload = _check(_db(tmp_path))
    assert payload["latest_closed_session"]["date"] == L
    ev = payload["latest_closed_session"]["evidence"]
    assert ev["calendar_side"] == L and ev["bars_side"] == L


# ---------- 主张 2：全绿 / 逐项缺 / 判不了 ----------

def test_healthy_chain_is_green_and_plans_nothing(tmp_path):
    payload = _check(_db(tmp_path))
    assert payload["exit_code"] == 0
    assert payload["ok"] is True
    assert _statuses(payload) == {
        "bars": "ok", "snapshot": "ok", "predictions": "ok", "verifications": "ok",
        "review": "ok", "paper_nav": "ok", "pit_tables": "ok",
    }
    assert payload["calendar"]["status"] == "ok"
    assert patrol.plan(payload)["steps"] == []
    assert payload["anomalies"] == []


def test_empty_db_cannot_determine_session_and_blocks(tmp_path):
    """两侧都没证据 → **判不了**，退出码 2（不许给 0、不许猜一个日子去补）。"""
    path = tmp_path / "empty.db"
    init_db(path)
    payload = _check(path)
    assert payload["exit_code"] == 2
    assert payload["latest_closed_session"]["date"] is None
    assert payload["checks"]["bars"]["status"] == "unknown"
    assert patrol.plan(payload)["steps"] == []
    assert payload["anomalies"][0]["kind"] == "no_closed_session"


def test_missing_bars_plans_bars_then_backfill(tmp_path):
    """① 缺 L 的日线 → 采增量；回填一并排上，且**顺序**是链序（bars 在前）。"""
    payload = _check(_db(tmp_path, bars_for_L=False))
    assert payload["checks"]["bars"]["status"] == "missing"
    assert payload["exit_code"] == 1
    assert patrol.plan(payload)["steps"] == ["ingest_bars", "session_backfill_close"]


def test_amount_null_with_snapshot_plans_backfill_only(tmp_path):
    """bar 行在、当日快照也在，但 amount/turnover 仍 NULL → 只补回填那一步。"""
    payload = _check(_db(tmp_path, snapshots_for_L=True))
    assert payload["checks"]["bars"]["status"] == "ok"
    assert payload["checks"]["bars"]["amount_null_rows"] == 2
    assert patrol.plan(payload)["steps"] == ["session_backfill_close"]


def test_amount_null_without_snapshot_plans_nothing_extra(tmp_path):
    """没有快照就没有 amount 的来源 —— 不排回填（白跑），也不算异常。"""
    payload = _check(_db(tmp_path))
    assert payload["checks"]["bars"]["amount_null_rows"] == 2
    assert payload["checks"]["bars"]["snapshots_for_date"] == 0
    assert patrol.plan(payload)["steps"] == []


def test_snapshot_unknown_when_calendar_cannot_answer_is_not_an_alarm(tmp_path):
    """② 判不出「今天是不是交易日」→ 报 `unknown`：**照样补步，但不假报警**。

    日历没覆盖今天（盘中永远如此）而休市表也没覆盖时，「今天该有快照」这句话没有
    证据。把它当异常 = 每个交易日都误报一次；不补步 = 快照链从这天起停住。
    """
    payload = _check(_db(tmp_path, snapshots_today=False))
    snap = payload["checks"]["snapshot"]
    assert snap["status"] == "unknown"
    assert snap["why"] == "calendar_not_covered"
    assert payload["session_day"]["is_trading_day"] is None
    assert payload["exit_code"] == 0                      # 不计异常
    assert patrol.plan(payload)["steps"] == ["session_tick"]


def test_snapshot_missing_on_confirmed_trading_day_is_an_alarm(tmp_path):
    """日历确认今天是交易日却没有快照 → 这是**真缺步**，退出码 1。"""
    db = _db(tmp_path, snapshots_today=False)
    c = connect(db)
    c.execute("INSERT INTO trading_calendar (date, is_open, source, created_at)"
              " VALUES (?,'1','tencent',?)", (TODAY, NOW))
    c.commit()
    c.close()
    payload = _check(db)
    assert payload["session_day"] == {"is_trading_day": True, "why": "trading_calendar"}
    assert payload["checks"]["snapshot"]["status"] == "missing"
    assert payload["exit_code"] == 1


def test_non_trading_day_snapshot_is_skipped_not_missing(tmp_path):
    """周六：② 记 `skipped`（07 §非交易日全部跳过），不排补步、不算异常。

    （周六的 L 是上周五，本夹具的日线/预测都在周一 ⇒ 其他项会缺，这里只看②。）
    """
    payload = _check(_db(tmp_path, snapshots_today=False),
                     now="2026-09-19T10:30:00+08:00")
    assert payload["session_day"] == {"is_trading_day": False, "why": "weekend"}
    assert payload["checks"]["snapshot"]["status"] == "skipped"
    assert "session_tick" not in patrol.plan(payload)["steps"]


def test_after_close_needs_a_post_close_snapshot(tmp_path):
    """收盘后：只有盘中快照（ts < 15:00）→ 缺「收盘那条」，排 tick。"""
    payload = _check(_db(tmp_path), now=CLOSED_NOW)
    snap = payload["checks"]["snapshot"]
    assert snap["status"] == "missing"
    assert snap["rows"] == 1 and snap["rows_after_close"] == 0
    assert patrol.plan(payload)["steps"] == ["session_tick"]


def test_post_close_snapshot_satisfies_the_item(tmp_path):
    payload = _check(_db(tmp_path, today_ts="20260922150500"), now=CLOSED_NOW)
    assert payload["checks"]["snapshot"]["status"] == "ok"
    assert payload["checks"]["snapshot"]["rows_after_close"] == 1


def test_missing_predictions_plans_predict_run(tmp_path):
    payload = _check(_db(tmp_path, predictions=False))
    assert payload["checks"]["predictions"]["status"] == "missing"
    assert patrol.plan(payload)["steps"] == ["predict_run"]


def test_predictions_of_another_model_version_do_not_count(tmp_path):
    """③ 只认**当前** `MODEL_VERSION`：旧版本的载荷不是「今天的预测已落库」。"""
    payload = _check(_db(tmp_path, predictions_mv="pit-rw-v1.0.1"))
    assert payload["checks"]["predictions"]["n"] == 0
    assert payload["checks"]["predictions"]["status"] == "missing"
    assert patrol.plan(payload)["steps"] == ["predict_run"]


# ---------- T2：patrol 不碰「今天 asof」的 predict（那条归 15:30 的 close） ----------

def test_today_asof_predict_is_skipped_and_the_receipt_says_why(tmp_path):
    """T2：`latest_closed_session` == 今天时，**不补** `predict run --asof 今天`。

    这是 2026-09-22 事故的形状（P46）：15:00 那一刻今天已「收盘」，而当天 K 线要到
    15:30 收盘链才定型 —— patrol 此刻补出来的预测基于**半截 bar**，写进 append-only
    的 `predictions` 后退不回来，15:30 收盘链重算必然全撞冲突（实测 17 条）。

    「跳过」必须**非静默**：回执里要带着 `skipped_reason`，否则读者只会看到
    「③ missing」而不知道有人故意没补（ERROR_DIARY #54 的教训：不新增绿码掩盖异常）。
    """
    payload = _check(_db(tmp_path, bars_today=True, predictions=False), now=CLOSED_NOW)
    assert payload["latest_closed_session"]["date"] == TODAY
    assert payload["checks"]["predictions"]["status"] == "missing"

    p = patrol.plan(payload)
    assert "predict_run" not in p["steps"]
    assert [s["step"] for s in p["skipped"]] == ["predict_run"]
    reason = p["skipped"][0]["reason"]
    assert "今天" in reason and "close" in reason


def test_yesterday_asof_predict_is_still_filled(tmp_path):
    """T2 的反面对照：**昨天的**缺口照旧补 —— 不能把整个补步功能关掉。

    盘中（10:30）`latest_closed_session` 是昨天，那时补 `predict run --asof 昨天`
    用的是已经定型的 K 线，是正经活。
    """
    payload = _check(_db(tmp_path, predictions=False), now=NOW)
    assert payload["latest_closed_session"]["date"] == L
    p = patrol.plan(payload)
    assert "predict_run" in p["steps"]
    assert p["skipped"] == []


def test_fix_never_runs_the_today_asof_predict(tmp_path):
    """T2 的端到端断言：`--fix` 一次子进程都不许为 `predict run --asof 今天` 起。"""
    path = _db(tmp_path, bars_today=True, predictions=False)
    runner = FakeRunner()
    payload = patrol.run_patrol(db_path=path, now=CLOSED_NOW, fix=True, runner=runner,
                                report_dir=path.parent / "reports")
    assert "predict_run" not in {c["step"] for c in runner.calls}
    assert [s["step"] for s in payload["plan"]["skipped"]] == ["predict_run"]
    assert payload["plan"]["skipped"][0]["reason"]


def test_summary_line_reports_the_skip(tmp_path):
    """摘要行（launchd 日志与 `/lab/ops` 逐字相同的那一行）也要看得见「跳过」。"""
    path = _db(tmp_path, bars_today=True, predictions=False)
    payload = patrol.run_patrol(db_path=path, now=CLOSED_NOW, fix=False,
                                runner=FakeRunner(),
                                report_dir=path.parent / "reports")
    line = patrol.summary_line(payload)
    assert "predict_run" not in line.split("fixed=")[1].split()[0]
    assert "skip=" in line and "predict_run" in line.split("skip=")[1]


def test_pending_verification_plans_verify_pending(tmp_path):
    payload = _check(_db(tmp_path, pending_verification=True))
    item = payload["checks"]["verifications"]
    assert (item["status"], item["n"]) == ("missing", 1)
    assert item["by_target_date"] == {L: 1}
    assert patrol.plan(payload)["steps"] == ["verify_pending"]


def test_missing_review_plans_review_daily(tmp_path):
    payload = _check(_db(tmp_path, review=False))
    assert payload["checks"]["review"]["status"] == "missing"
    assert patrol.plan(payload)["steps"] == ["review_daily"]


def test_paper_nav_missing_plans_paper_step(tmp_path):
    payload = _check(_db(tmp_path, paper_nav=False))
    assert payload["checks"]["paper_nav"]["status"] == "missing"
    assert patrol.plan(payload)["steps"] == ["paper_step"]


def test_paper_not_initialised_is_skipped_not_an_alarm(tmp_path):
    """未 `paper init` → 跳过，不算异常（07：未就绪则跳过，不假报）。"""
    payload = _check(_db(tmp_path, paper=False))
    assert payload["checks"]["paper_nav"]["status"] == "skipped"
    assert payload["exit_code"] == 0
    assert patrol.plan(payload)["steps"] == []


def test_stale_pit_tables_plan_the_three_ingests(tmp_path):
    payload = _check(_db(tmp_path, pit=False))
    item = payload["checks"]["pit_tables"]
    assert item["status"] == "stale"
    assert "valuation_daily" in item["reason"] and "adj_factors" in item["reason"]
    assert patrol.plan(payload)["steps"] == [
        "ingest_actions", "ingest_valuation", "ingest_moneyflow"]


def test_stale_calendar_plans_ingest_index(tmp_path):
    """日历落后 = `ingest index` 没跑（日历是它唯一的产物）。"""
    payload = _check(_db(tmp_path, calendar=False))
    assert payload["calendar"]["status"] == "stale"
    assert payload["calendar"]["required"] == L
    assert patrol.plan(payload)["steps"] == ["ingest_index"]


def test_market_holidays_take_precedence_for_today(tmp_path):
    """休市表覆盖今年时以它为准：周中节假日 → ② `skipped`，不排 tick（ADR-013）。"""
    db = _db(tmp_path, snapshots_today=False)
    c = connect(db)
    c.execute(
        "INSERT INTO market_holidays (date, is_open, source, doc_kind, covered_year,"
        " source_url, published_at, created_at) VALUES (?,0,'sse','annual',2026,'u',?,?)",
        (TODAY, NOW, NOW))
    c.commit()
    c.close()
    payload = _check(db)
    assert payload["session_day"] == {"is_trading_day": False,
                                      "why": "market_holidays"}
    assert payload["checks"]["snapshot"]["status"] == "skipped"
    assert payload["exit_code"] == 0


# ---------- 主张 3：补步的**字面** argv（主干固定不可修改） ----------

def test_every_step_is_a_literal_cli_command(tmp_path):
    """补步 = 一条现成的 CLI 命令，**逐字**等于 cron 里原来那行 —— 不重实现任何规则。"""
    db = Path("/tmp/x.db")
    got = {name: patrol.build_argv(patrol.STEP_BY_NAME[name], db_path=db, asof=L,
                                   action_start="2025-08-18")
           for name in patrol.STEP_ORDER}
    assert all(v[:3] == [sys.executable, "-m", "stocklab.cli.main"] for v in got.values())
    assert {k: v[3:] for k, v in got.items()} == {
        "ingest_index": ["ingest", "index"],
        "ingest_bars": ["ingest", "bars", "--days", "30"],
        "ingest_actions": ["ingest", "actions", "--start", "2025-08-18"],
        "ingest_valuation": ["ingest", "valuation", "--days", "30"],
        "ingest_moneyflow": ["ingest", "moneyflow", "--days", "30"],
        "session_tick": ["session", "tick", "--db", "/tmp/x.db"],
        "session_backfill_close": ["session", "backfill-close", "--date", L,
                                    "--db", "/tmp/x.db"],
        "predict_run": ["predict", "run", "--asof", L, "--db", "/tmp/x.db"],
        "verify_pending": ["verify", "pending", "--db", "/tmp/x.db"],
        "review_daily": ["review", "daily", "--date", L, "--db", "/tmp/x.db"],
        "paper_step": ["paper", "step", "--asof", L, "--db", "/tmp/x.db"],
    }
    # 采集类一律**不接受** `--db`（固定写默认库）—— 越界守卫靠的就是这一条。
    assert [name for name in patrol.STEP_ORDER
            if not patrol.STEP_BY_NAME[name].supports_db] == [
        "ingest_index", "ingest_bars", "ingest_actions", "ingest_valuation",
        "ingest_moneyflow"]


def test_plan_puts_everything_in_close_chain_order(tmp_path):
    """一口气弄坏能同时弄坏的项 → 计划必须是**收盘链顺序**，不是乱的。

    刻意**没有** `ingest_index`：L 必须有至少一侧证据（否则整轮就是「判不了」），
    而「日历没跟到 L」与「L 的日线缺行」不能同时成立 —— 前者要求 L 来自行情侧、
    后者要求它来自日历侧。两条规则各自单测（见下一条），这里钉顺序。
    """
    payload = _check(_db(tmp_path, bars_for_L=False, pit=False, snapshots_today=False,
                         predictions=False, review=False, pending_verification=True,
                         paper_nav=False))
    assert payload["latest_closed_session"]["date"] == L     # 日历侧给出 L
    assert patrol.plan(payload)["steps"] == [
        "ingest_bars", "ingest_actions", "ingest_valuation", "ingest_moneyflow",
        "session_tick", "session_backfill_close", "predict_run", "verify_pending",
        "review_daily", "paper_step"]


def test_plan_is_empty_when_both_evidence_sides_are_gone(tmp_path):
    """日历空 + L 的日线也缺 → 两侧都没证据 ⇒ 判不了，**一步都不排**（不猜日子）。"""
    payload = _check(_db(tmp_path, calendar=False, bars_for_L=False))
    assert payload["latest_closed_session"]["date"] is None
    assert payload["exit_code"] == 2
    assert patrol.plan(payload)["steps"] == []


# ---------- `--fix`：跑什么、跑多快、什么时候停 ----------

def test_fix_runs_planned_steps_in_chain_order(tmp_path):
    db = _db(tmp_path, snapshots_today=False, predictions=False, review=False)
    runner = FakeRunner()
    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True, runner=runner,
                                report_dir=tmp_path / "reports")
    assert [s["name"] for s in payload["steps"]] == [
        "session_tick", "predict_run", "review_daily"]
    pred = next(c for c in runner.calls if c["step"] == "predict_run")
    assert pred["argv"][3:] == ["predict", "run", "--asof", L, "--db", str(db)]
    # 假执行器没改库 → 复检仍缺 → 退出码 1（**不是** 0）
    assert payload["exit_code"] == 1 and payload["after_exit_code"] == 1
    assert payload["plan"]["steps"] == ["session_tick", "predict_run", "review_daily"]
    assert any("②" in r for r in payload["plan"]["reasons"])
    assert any("③" in r for r in payload["plan"]["reasons"])
    assert any("⑤" in r for r in payload["plan"]["reasons"])


def test_run_steps_takes_the_actions_start_from_now(tmp_path):
    """`ingest actions` 的回看起点 = `now - 400d`（不是写死的日子）。"""
    runner = FakeRunner()
    steps, aborted = patrol.run_steps(["ingest_actions"], registry=patrol.STEP_BY_NAME,
                                      runner=runner, db_path=Path("/tmp/x.db"),
                                      now=NOW, timeout_s=60.0)
    assert aborted is None
    assert runner.calls[0]["argv"][3:] == ["ingest", "actions", "--start", "2025-08-18"]
    assert steps[0]["name"] == "ingest_actions"


def test_fix_recheck_makes_a_real_repair_green(tmp_path):
    """补步真的把缺口补上 → 复检全绿、退出码 0（复检这一步不能省）。"""
    rd = tmp_path / "reports"
    db = _db(tmp_path, review=False, report_dir=rd)

    def write_review(step, argv):
        if step.name == "review_daily":
            (rd / f"{L}-review.md").write_text("# review\n", encoding="utf-8")

    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True,
                                runner=FakeRunner(on_call=write_review),
                                report_dir=rd)
    assert [s["name"] for s in payload["steps"]] == ["review_daily"]
    assert payload["exit_code"] == 0 and payload["ok"] is True
    assert payload["anomalies"] == []


def test_fix_is_off_by_default(tmp_path):
    db = _db(tmp_path, review=False)
    runner = FakeRunner()
    payload = patrol.run_patrol(db_path=db, now=NOW, runner=runner,
                                report_dir=tmp_path / "reports")
    assert runner.calls == [] and payload["steps"] == []
    assert payload["exit_code"] == 1


def test_fix_refuses_other_db_when_plan_has_ingest_steps(tmp_path):
    """**最要紧的一条**：采集类命令不接受 `--db` ⇒ 在别的库上拒绝补步，一次子进程都不起。

    照跑的后果不会报警 —— 它只会让「另一个库看起来也有数据了」。
    """
    db = _db(tmp_path, bars_for_L=False)
    runner = FakeRunner()
    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True, runner=runner,
                                report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 2
    assert runner.calls == [] and payload["steps"] == []
    assert "拒绝在非默认库" in payload["refused"]
    assert [a["kind"] for a in payload["anomalies"]] == ["check_bars", "fix_refused"]


def test_fix_allows_other_db_when_every_step_takes_db(tmp_path):
    db = _db(tmp_path, review=False)
    runner = FakeRunner()
    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True, runner=runner,
                                report_dir=tmp_path / "reports")
    assert "refused" not in payload
    assert [c["step"] for c in runner.calls] == ["review_daily"]


def test_step_exit_1_is_soft_and_the_chain_continues(tmp_path):
    """1 = 「跑完了但如实报了异常」（tick 报验证冲突就是它）→ 不能为它中止整条链。"""
    db = _db(tmp_path, predictions=False, review=False)
    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True,
                                runner=FakeRunner({"predict_run": 1}),
                                report_dir=tmp_path / "reports")
    assert [s["name"] for s in payload["steps"]] == ["predict_run", "review_daily"]
    assert "aborted" not in payload
    assert payload["exit_code"] == 1
    kinds = [a["kind"] for a in payload["anomalies"]]
    assert "step_anomaly" in kinds and "step_failed" not in kinds


def test_step_exit_2_stops_the_chain(tmp_path):
    db = _db(tmp_path, snapshots_today=False, predictions=False, review=False)
    runner = FakeRunner({"session_tick": 2})
    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True, runner=runner,
                                report_dir=tmp_path / "reports")
    assert [c["step"] for c in runner.calls] == ["session_tick"]
    assert payload["aborted"]["kind"] == "step_fatal"
    assert payload["exit_code"] == 2


def test_step_timeout_stops_and_blocks(tmp_path):
    db = _db(tmp_path, review=False)
    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True,
                                runner=FakeRunner(timeout_at="review_daily"),
                                report_dir=tmp_path / "reports")
    assert payload["aborted"]["kind"] == "step_timeout"
    assert payload["exit_code"] == 2


def test_budget_exhausted_runs_nothing_and_is_recorded(tmp_path):
    db = _db(tmp_path, review=False)
    runner = FakeRunner()
    payload = patrol.run_patrol(db_path=db, now=NOW, fix=True, runner=runner,
                                timeout_s=0, report_dir=tmp_path / "reports")
    assert runner.calls == []
    assert payload["aborted"]["kind"] == "budget_exhausted"
    # 预算用尽 = 这一轮**没跑完** → 2（断链）；1 的含义是「跑完了但如实报了异常」，
    # 而这里一步都没跑成 —— 报 1 等于把「没修」说成「修了但有噪声」。
    assert payload["exit_code"] == 2
    # 缺口仍在（⑤ 的报告没补上）——2 分／缺口两件事都如实留下
    assert "check_review" in [a["kind"] for a in payload["anomalies"]]


def test_missing_db_is_blocked_without_crashing(tmp_path):
    payload = patrol.run_patrol(db_path=tmp_path / "nope.db", now=NOW)
    assert payload["exit_code"] == 2 and payload["checks"] == {}
    assert payload["anomalies"][0]["kind"] == "db_missing"
    assert "patrol: exit=2" in patrol.summary_line(payload)


# ---------- 汇报行 + CLI ----------

def test_summary_line_pins_all_seven_items(tmp_path):
    line = patrol.summary_line(_check(_db(tmp_path)))
    assert line.startswith("patrol: exit=0 latest_closed=2026-09-21")
    assert line.endswith("fixed=- anomalies=0")
    for i, name in enumerate(patrol.CHECK_ORDER):
        assert f"{patrol.CIRCLED[i]}ok" in line


def test_summary_line_shows_missing_items(tmp_path):
    line = patrol.summary_line(_check(_db(tmp_path, review=False)))
    assert "⑤missing" in line and "①ok" in line
    assert "anomalies=1" in line


def test_cli_patrol_prints_json_and_the_summary_line(tmp_path, capsys):
    db = _db(tmp_path)
    rc = main(["ops", "patrol", "--db", str(db), "--now", NOW,
               "--report-dir", str(tmp_path / "reports")])
    out = capsys.readouterr()
    assert rc == 0
    assert json.loads(out.out)["ok"] is True        # stdout 必须是**纯 JSON**
    assert "patrol: exit=0" in out.err               # 摘要走 stderr（cron 直接贴它）


def test_cli_patrol_exit_code_is_the_projects_verdict(tmp_path, capsys):
    """退出码语义由项目定义 —— 这是 P38 存在的理由（ADR-019）。"""
    db = _db(tmp_path, review=False)
    rc = main(["ops", "patrol", "--db", str(db), "--now", NOW,
               "--report-dir", str(tmp_path / "reports")])
    out = capsys.readouterr()
    assert rc == 1
    assert "⚠️  check_review" in out.err
    assert "⑤missing" in out.err
