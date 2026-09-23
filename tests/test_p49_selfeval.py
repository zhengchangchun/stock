"""P49：模块2 自评估三分支（建议 ≠ 执行）＋ 提前熔断（可复现 / 幂等 / 只追加）。

判据 → 用例的对应（与任务书 §5 的表一一对应）：

| # | 判据 | 用例 |
|---|---|---|
| T1 | **边界越界即拒** | `test_t1_*`：轮次 1 / 4、天数 31、冻结 91 ⇒ 退出码 2，**库里零行** |
| T2 | **不 clamp** | `test_t2_*`：越界值原样出现在报错文案里；库里**没有**被改成合法值的行 |
| T3 | **建议 ≠ 执行** | `test_t3_*`：AST 扫描（判定模块零写路径）+ 反向自检 + 前后台账逐行相等 |
| T4 | **熔断可复现** | `test_t4_*`：9.99% / 10.00% 分界；重放 `at_value`/`threshold` 逐位相同 |
| T5 | **熔断幂等** | `test_t5_*`：同 `(cycle_id, asof)` 重放不增行；唯一索引是结构防线 |
| T6 | **判据原文不可事后改写** | `test_t6_*`：`criteria_text` 前后逐字节相同；UPDATE/DELETE 被触发器拒 |
| T7 | **错判回流不填归因** | `test_t7_*`：归因恒 `None`；带来源指纹与生成时间；页面上没有「原因」二字 |
| T8 | **页面零写入口** | `test_t8_*`：`labweb/` 对写路径零引用（+ 反向自检）；HTML 无表单/按钮 |

读数、门禁、判据原文等数字**一律从被测模块取**（不手抄）—— 手抄的那份会与上游漂移。

夹具里的净值路径刻意做成「先涨到峰值、再跌到谷底」：只有这样才能把
`paper/engine.py::drawdown`（相对**历史峰值**）与「首尾相减」区分开 ——
后者根本测不出回撤，于是「阈值分界」会变成一条永远通过的判据。
"""

from __future__ import annotations

import ast
import datetime
import json
import pathlib
import sqlite3

import pytest

from stocklab.cli.main import main
from stocklab.config import limits
from stocklab.labweb import m2_data, m2_render, paper_data
from stocklab.m2 import config as m2_config
from stocklab.m2 import score as m2_score
from stocklab.m2 import selfeval
from stocklab.m2 import store as m2_store
from stocklab.paper import store as paper_store
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = "2026-09-22T16:00:00+08:00"
START = "2026-01-01"
STOCK = "600519"
AI_ACCOUNT = "arm-agent-v1"
MIRROR = m2_config.MIRROR_ACCOUNT
SCRIPT_ID = 7

#: 账户 130 个净值日 —— 门禁 120 需要 ≥ `PERFORMANCE_THRESHOLD` 个交易日。
N_DAYS = 130
#: 周期起跑日 = 第 100 个净值日；轮次/判定的 asof 必须落在起跑 + 30 天以内。
CYCLE_START_IDX = 99
CRITERIA = "相对沪深300 超额 > 0 且 方向命中率 ≥ 0.5；否则不进下一轮（判据原文示例）"

#: ADR-023 的措辞纪律：门禁没过时这六个词一个都不许出现在**任何**渲染路径上。
BANNED = ("跑赢", "跑输", "优于", "劣于", "领先", "胜过")

#: 账户净值的峰值（三种回撤都从这里的峰算起）。
PEAK = 22000.0
#: 9.99% 与 10.00% 两个谷底（阈值分界用）。
TROUGH_UNDER = 19802.2
TROUGH_AT = 19800.0


def _run(capsys, *argv) -> tuple[int, str, str]:
    """跑一次 CLI 并把 `(退出码, stdout, stderr)` 一次性取出来。

    不用 `assert code == 0, capsys.readouterr().err` 的写法：那条消息是**急切求值**的，
    会把 stdout 一起吃掉，后面的 `json.loads` 就拿到空串。
    """
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _sessions(n: int, *, start: str = START) -> list[str]:
    base = datetime.date.fromisoformat(start)
    return [(base + datetime.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _nav_values(n: int, *, trough: float) -> list[float]:
    """净值路径：升到 `PEAK` 后跌到 `trough`（长度严格 = `n`）。"""
    peak_at = int(n * 0.85)                  # 峰值所在的下标
    rise = [20000.0 + (PEAK - 20000.0) * i / peak_at for i in range(peak_at + 1)]
    steps = n - peak_at - 1                  # 峰值之后的条数
    fall = [PEAK + (trough - PEAK) * i / steps for i in range(1, steps + 1)]
    assert len(rise) + len(fall) == n and fall[-1] == trough
    return rise + fall


def _p49_db(tmp_path, *, trough: float = 21500.0, name: str = "p49.db",
            n_days: int = N_DAYS, index_slope: float = 9.0,
            with_forecasts: bool = True, with_nav: bool = True):
    """一份能端到端跑 P49 的库：模拟盘账户 + 净值 + P48 已落库的校验分数。

    净值行与 K 线都是直接插的（不跑 `paper step` / `ingest`）：这一组测的是
    周期主干、判定与熔断，不是「引擎会不会算净值」—— 后者已有自己的用例。
    `index_slope` 为负 ⇒ 基准下跌 ⇒ 账户**跑出**正超额（冻结的前提不成立）。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    init_db(path)
    c = connect(path)
    days = _sessions(n_days)
    axis = [START, *days]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sh','main',?,?)",
        [(STOCK, "贵州茅台", "stock", NOW), (INDEX_300_SYMBOL, "沪深300", "index", NOW)])
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in axis])
    # 标的价：前 122 天 100 → 130（涨），最后 8 天跌回 118 —— 让末尾的「预测涨」判错。
    closes = ([100.0 + 30.0 * i / (n_days - 9) for i in range(n_days - 8)]
              + [130.0 - 12.0 * i / 8 for i in range(1, 9)])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(STOCK, d, v, v, v, v, NOW) for d, v in zip(axis, [100.0, *closes])]
        + [(INDEX_300_SYMBOL, d, 4000.0 + index_slope * i, 4000.0 + index_slope * i,
            4000.0 + index_slope * i, 4000.0 + index_slope * i, NOW)
           for i, d in enumerate(axis)])
    c.commit()
    for account_id, arm, params in (
            (AI_ACCOUNT, "agent", {m2_config.EXECUTOR_KEY:
                                   m2_config.EXECUTOR_CHANNEL_A,
                                   "strategy_version": "v1"}),
            (MIRROR, "now", {"initial_capital": 20000.0})):
        paper_store.insert_account(
            c, account_id=account_id, arm=arm, etf_target_pct=None,
            start_date=START, initial_cash=20000.0, initial_positions=[],
            initial_nav=20000.0, params=params, now=NOW)
    navs = _nav_values(n_days, trough=trough)
    for i, d in enumerate(days if with_nav else [], start=1):
        paper_store.insert_nav(
            c, account_id=AI_ACCOUNT, date=d, cash=navs[i - 1], positions=[],
            market_value=0.0, nav=navs[i - 1], drawdown=0.0, cum_cost=0.0,
            cum_return=round(navs[i - 1] / 20000.0 - 1.0, 6), net_deposits=20000.0,
            index_300_level=None, index_300_asof=None, now=NOW, commit=False)
    c.commit()
    if with_forecasts:
        for i in range(n_days - 1):      # 每条预测：决策日 = 第 i 天，目标日 = 下一天
            m2_store.insert_forecast(
                c, plugin_id="m2_a3", channel="A", account_id=AI_ACCOUNT,
                asof=days[i], code=STOCK,
                payload={"range_80": [95.0, 105.0],
                         "direction": {"up": 0.6, "flat": 0.2, "down": 0.2},
                         "invalidate_if": "跌破 90 或 站上 110", "na_reasons": [],
                         "schema_version": "1"},
                script_id=SCRIPT_ID, script_version="s1",
                input_sha256=f"{i:064d}", now=NOW)
        c.commit()
        m2_score.score_all(c, asof=days[-1], now=NOW)
    c.close()
    return path, days


def _conn(path):
    return connect(path)


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("validation_cycles", "validation_rounds", "validation_events",
                      "m2_judgements")}


def _start(path, days, capsys, *, rounds: int = 3, days_n: int = 30,
           start_idx: int = CYCLE_START_IDX):
    code, out, err = _run(
        capsys, "m2", "cycle", "start", "--script-id", str(SCRIPT_ID),
        "--account-id", AI_ACCOUNT, "--rounds", str(rounds), "--days", str(days_n),
        "--criteria-text", CRITERIA, "--start-date", days[start_idx],
        "--db", str(path), "--now", NOW)
    assert code == 0, err
    return json.loads(out)["cycle_id"]


def _start_and_rounds(path, days, capsys, *, start_idx: int = CYCLE_START_IDX,
                      rounds: int = 3) -> int:
    """开一个周期并落满 2 轮（判定要求 ≥ `VALIDATION_ROUNDS_MIN` 轮）。"""
    cid = _start(path, days, capsys, rounds=rounds, start_idx=start_idx)
    for no, idx in ((1, start_idx + 6), (2, start_idx + 26)):
        code, _out, err = _run(capsys, "m2", "cycle", "round", "--cycle", str(cid),
                               "--round-no", str(no), "--asof", days[idx],
                               "--db", str(path), "--now", NOW)
        assert code == 0, err
    return cid


def _judge(path, capsys, *, cycle: int = 1, asof: str, fix_kind: str,
           freeze_days: int | None = None):
    argv = ["m2", "cycle", "judge", "--cycle", str(cycle), "--asof", asof,
            "--fix-kind", fix_kind, "--db", str(path), "--now", NOW]
    if freeze_days is not None:
        argv += ["--freeze-days", str(freeze_days)]
    return _run(capsys, *argv)


def _fuse(path, capsys, *, cycle: int, asof: str):
    return _run(capsys, "m2", "cycle", "fuse-check", "--cycle", str(cycle),
                "--asof", asof, "--db", str(path), "--now", NOW)


# ══════════════════════════════════════════════════════════════════════
# T1 —— 边界越界即拒（且库里不留半截行）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("rounds,days", [(1, 30), (4, 30), (2, 31), (0, 30), (2, 0)])
def test_t1_plan_out_of_bounds_is_refused_with_no_rows(tmp_path, capsys, rounds, days):
    """轮次 ∉ [2,3] / 天数 ∉ [1,30] ⇒ 退出码 2 + 点名文案，**库里零行**。

    断言的是「零行」而不是「只有一行合法的」：拒绝路径**不许**先写再删，
    也不许把值夹到边界上再写（那正是 T2 要排除的实现）。
    """
    path, _ = _p49_db(tmp_path)
    code, _out, err = _run(capsys, "m2", "cycle", "start", "--script-id",
                           str(SCRIPT_ID), "--account-id", AI_ACCOUNT,
                           "--rounds", str(rounds), "--days", str(days),
                           "--criteria-text", CRITERIA, "--start-date", START,
                           "--db", str(path), "--now", NOW)
    assert code == 2, err
    assert (str(rounds) in err or str(days) in err), "报错要点名越界的那个值"
    assert "拒绝" in err and "clamp" in err
    c = _conn(path)
    try:
        assert _counts(c) == {"validation_cycles": 0, "validation_rounds": 0,
                              "validation_events": 0, "m2_judgements": 0}
    finally:
        c.close()


def test_t1_the_legal_plan_lands_with_the_original_values(tmp_path, capsys):
    """对照组：合法方案（2 轮 / 30 天）落库，且**原值**落库（不是被改写过的值）。"""
    path, _ = _p49_db(tmp_path)
    code, out, err = _run(capsys, "m2", "cycle", "start", "--script-id",
                          str(SCRIPT_ID), "--account-id", AI_ACCOUNT,
                          "--rounds", "2", "--days", "30", "--criteria-text",
                          CRITERIA, "--start-date", START, "--db", str(path),
                          "--now", NOW)
    assert code == 0, err
    assert json.loads(out)["status"] == "created"
    c = _conn(path)
    try:
        row = c.execute("SELECT * FROM validation_cycles").fetchone()
        assert (row["planned_rounds"], row["planned_days"]) == (2, 30)
        assert row["criteria_text"] == CRITERIA          # 判据原文逐字节
    finally:
        c.close()


def test_t1_a_round_past_the_total_duration_is_refused(tmp_path, capsys):
    """轮次的 `asof` 距起跑 > 30 天 ⇒ 拒绝（**总时长**上限同样不 clamp）。"""
    path, days = _p49_db(tmp_path)
    cid = _start(path, days, capsys)
    over = (datetime.date.fromisoformat(days[CYCLE_START_IDX])
            + datetime.timedelta(days=limits.VALIDATION_MAX_DAYS + 1)).isoformat()
    code, _out, err = _run(capsys, "m2", "cycle", "round", "--cycle", str(cid),
                           "--round-no", "1", "--asof", over, "--db", str(path),
                           "--now", NOW)
    assert code == 2, err
    assert "31" in err and "拒绝" in err
    c = _conn(path)
    try:
        assert _counts(c)["validation_rounds"] == 0
    finally:
        c.close()


def test_t1_a_round_number_beyond_the_plan_is_refused(tmp_path, capsys):
    """轮次号 > 计划轮次 ⇒ 拒绝（补跑不许写到计划之外）。"""
    path, days = _p49_db(tmp_path)
    cid = _start(path, days, capsys, rounds=2)
    code, _out, err = _run(capsys, "m2", "cycle", "round", "--cycle", str(cid),
                           "--round-no", "3", "--asof", days[CYCLE_START_IDX + 3],
                           "--db", str(path), "--now", NOW)
    assert code == 2, err
    assert "3" in err and "拒绝" in err
    c = _conn(path)
    try:
        assert _counts(c)["validation_rounds"] == 0
    finally:
        c.close()


def test_t1_freeze_beyond_the_limit_is_refused(tmp_path, capsys):
    """冻结 91 天 > `FREEZE_MAX_DAYS`(90) ⇒ 拒绝，判定行与事件行都是 0。"""
    path, days = _p49_db(tmp_path)          # trough=21500：未达标，冻结前提成立
    _start_and_rounds(path, days, capsys)
    code, _out, err = _judge(path, capsys, asof=days[-1], fix_kind="none",
                             freeze_days=limits.FREEZE_MAX_DAYS + 1)
    assert code == 2, err
    assert "91" in err and "拒绝" in err and "clamp" in err
    c = _conn(path)
    try:
        counts = _counts(c)
        assert counts["m2_judgements"] == 0 and counts["validation_events"] == 0
    finally:
        c.close()


def test_t1_a_freeze_without_days_is_refused(tmp_path, capsys):
    """声明「无方向」却不给冻结天数 ⇒ 拒绝（冻结期是主干常量管的，缺了无法校验）。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    code, _out, err = _judge(path, capsys, asof=days[-1], fix_kind="none")
    assert code == 2, err
    assert "冻结天数" in err
    c = _conn(path)
    try:
        assert _counts(c)["m2_judgements"] == 0
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T2 —— 越界不 clamp
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("rounds,days", [(1, 30), (4, 30), (2, 31)])
def test_t2_no_clamped_row_is_written(tmp_path, capsys, rounds, days):
    """越界输入**不许**被改成合法值落库（clamp 实现会留下 2 / 3 或 30）。"""
    path, _ = _p49_db(tmp_path)
    _run(capsys, "m2", "cycle", "start", "--script-id", str(SCRIPT_ID),
         "--account-id", AI_ACCOUNT, "--rounds", str(rounds), "--days", str(days),
         "--criteria-text", CRITERIA, "--start-date", START, "--db", str(path),
         "--now", NOW)
    c = _conn(path)
    try:
        written = [tuple(r) for r in c.execute(
            "SELECT planned_rounds, planned_days FROM validation_cycles")]
    finally:
        c.close()
    assert written == [], f"越界输入被写了进去（clamp 或漏校验）：{written}"


def test_t2_the_limits_module_refuses_instead_of_returning_a_value():
    """底层的边界函数**只抛错、不返回值** —— 「取最近的合法值」在结构上不存在。"""
    for kwargs in ({"rounds": 1, "days": 30}, {"rounds": 4, "days": 30},
                   {"rounds": 2, "days": 31}):
        with pytest.raises(limits.PlanOutOfBounds):
            limits.check_validation_plan(**kwargs)
    with pytest.raises(limits.PlanOutOfBounds):
        limits.check_freeze_days(days=limits.FREEZE_MAX_DAYS + 1)


def test_t2_over_bound_judgement_writes_nothing(tmp_path, capsys):
    """越界判定之后，判定台账零行、冻结事件零行（连「夹到 90 天」的行也没有）。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    _judge(path, capsys, asof=days[-1], fix_kind="none", freeze_days=91)
    c = _conn(path)
    try:
        assert _counts(c)["m2_judgements"] == 0
        assert c.execute("SELECT COUNT(*) FROM validation_events WHERE kind IN"
                         " ('freeze','unfreeze')").fetchone()[0] == 0
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T3 —— 建议 ≠ 执行（静态扫描 + 行为对照）
# ══════════════════════════════════════════════════════════════════════

#: 执行路径的函数/方法名 —— 判定模块里出现**任何**一个都判红。
EXECUTION_CALLS = ("approve", "apply_event", "set_status", "freeze", "unfreeze",
                   "finish_validation", "start_validation", "insert_script",
                   "insert_account", "insert_nav", "insert_trade", "update")

#: 判定模块（`selfeval.py`）里**一个 SQL 写语句都不许有**。
SQL_WRITE_WORDS = ("INSERT", "UPDATE", "DELETE")

JUDGE_DIR = ROOT / "stocklab/m2"


def _execution_calls_in(path: pathlib.Path) -> list[str]:
    """AST 扫描：对执行路径的**调用**（注释与文档字符串里出现不算 —— 子串匹配会假红）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name in EXECUTION_CALLS:
            hits.append(f"{name}@{node.lineno}")
    return hits


def _sql_write_literals(path: pathlib.Path) -> list[str]:
    """代码里的 SQL 写语句字面量 —— **文档字符串不算**。

    模块 docstring 往往需要说清「本模块没有任何 INSERT / UPDATE / DELETE」，
    把 docstring 一起扫就会把这句话本身判红（然后只能放宽判据，最后什么都测不到，
    ERROR_DIARY #50 同款）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            for word in SQL_WRITE_WORDS:
                if f"{word} " in node.value.upper():
                    hits.append(f"{word}@{node.lineno}")
    return hits


def test_t3_the_judge_module_has_no_execution_path():
    """判定模块 `selfeval.py`：调用不了 approve 之类，也没有任何 SQL 写语句。"""
    selfeval_py = JUDGE_DIR / "selfeval.py"
    assert selfeval_py.is_file(), "扫描目标不存在 = 无效实验"
    assert _execution_calls_in(selfeval_py) == [], "判定模块里出现了执行路径的调用"
    assert _sql_write_literals(selfeval_py) == [], "判定模块里出现了 SQL 写语句"


def test_t3_cycle_module_writes_only_ledgers(tmp_path):
    """主干模块 `cycle.py` 也不许碰插桩 / 账户 / 净值 / 成交的写函数。"""
    cycle_py = JUDGE_DIR / "cycle.py"
    offenders = [h for h in _execution_calls_in(cycle_py)
                 if not h.startswith(("insert_cycle", "insert_round", "insert_event",
                                      "insert_judgement", "find_event",
                                      "find_judgement"))]
    assert offenders == [], f"主干模块里出现了执行路径的调用：{offenders}"


def test_t3_the_same_scan_flags_a_planted_violation(tmp_path):
    """反向自检：喂一段真的会越权的代码，两条扫描**必须**判红（否则等于没写）。"""
    bad = tmp_path / "evil.py"
    bad.write_text("from stocklab.plugin import lifecycle\n"
                   "def go(conn, script_id):\n"
                   "    lifecycle.approve(conn, script_id=script_id)\n"
                   "    conn.execute('UPDATE plugin_scripts SET x = 1')\n",
                   encoding="utf-8")
    assert _execution_calls_in(bad) == ["approve@3"]
    assert _sql_write_literals(bad) == ["UPDATE@4"]


def test_t3_judging_changes_nothing_outside_the_judgement_ledger(tmp_path, capsys):
    """判定前后：插桩 / 审计 / 账户 / 净值 / 成交 / 周期 / 轮次 / 事件**逐行相等**。

    只有 `m2_judgements` 多一行 —— 「建议不是执行」的行为证据（静态扫描的另一半）。
    """
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    watched = ("plugin_scripts", "plugin_audit", "paper_accounts", "paper_nav_daily",
               "paper_trades", "validation_cycles", "validation_rounds",
               "validation_events")
    c = _conn(path)
    try:
        before = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t}")]
                  for t in watched}
    finally:
        c.close()
    code, out, err = _judge(path, capsys, asof=days[-1], fix_kind="none",
                            freeze_days=30)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["branch"] == m2_config.BRANCH_FREEZE
    assert payload["conclusion"] is True
    c = _conn(path)
    try:
        after = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t}")]
                 for t in watched}
        assert after == before, "判定动了台账之外的东西（建议被当成了执行）"
        assert _counts(c)["m2_judgements"] == 1
    finally:
        c.close()
    assert any("approve" in a for a in payload["actions"])
    assert any("不执行" in a for a in payload["actions"])


def test_t3_the_other_two_branches_also_write_no_execution(tmp_path, capsys):
    """`optimize` / `tune` 两个分支同样只写建议（有案例时的落地动作仍在文案里）。"""
    for fix_kind, expected in (("logic", m2_config.BRANCH_OPTIMIZE),
                               ("params", m2_config.BRANCH_TUNE)):
        path, days = _p49_db(tmp_path, name=f"{fix_kind}.db")
        _start_and_rounds(path, days, capsys)
        code, out, err = _judge(path, capsys, asof=days[-1], fix_kind=fix_kind)
        assert code == 0, err
        payload = json.loads(out)
        assert payload["branch"] == expected
        assert payload["evidence"]["cases"]["n_cases"] > 0
        c = _conn(path)
        try:
            assert c.execute("SELECT COUNT(*) FROM plugin_scripts").fetchone()[0] == 0
            assert _counts(c)["validation_events"] == 0
        finally:
            c.close()


def test_t3_a_branch_claim_without_evidence_is_refused(tmp_path, capsys):
    """声明「有方向」但案例集为空 ⇒ 拒绝（依据为空不许编一个方向）。"""
    path, days = _p49_db(tmp_path, with_forecasts=False)
    _start_and_rounds(path, days, capsys)
    code, _out, err = _judge(path, capsys, asof=days[-1], fix_kind="logic")
    assert code == 2, err
    assert "依据为空" in err
    c = _conn(path)
    try:
        assert _counts(c)["m2_judgements"] == 0
        assert c.execute("SELECT COUNT(*) FROM m2_forecast_scores").fetchone()[0] == 0
    finally:
        c.close()


def test_t3_a_freeze_claim_on_a_passing_strategy_is_refused(tmp_path, capsys):
    """声明「无方向（冻结）」但读数**达标** ⇒ 拒绝（冻结的前提不成立）。

    达标由 P41 的读数判定（相对基准超额 > 0）—— 基准下跌、账户上涨，不手写一个 excess。
    """
    path, days = _p49_db(tmp_path, index_slope=-9.0)
    cid = _start_and_rounds(path, days, capsys)
    c = _conn(path)
    try:
        perf = paper_data.performance(c, days[-1])
        assert perf["excess_vs_index_300"][AI_ACCOUNT] > 0   # 夹具的前提先自证
    finally:
        c.close()
    code, _out, err = _judge(path, capsys, cycle=cid, asof=days[-1],
                             fix_kind="none", freeze_days=30)
    assert code == 2, err
    assert "达标" in err
    c = _conn(path)
    try:
        assert _counts(c)["m2_judgements"] == 0
    finally:
        c.close()


def test_t3_insufficient_sample_only_reports_readings(tmp_path, capsys):
    """样本不足 120 ⇒ 分支 `insufficient`（**结论性 = 否**），只给读数。"""
    path, days = _p49_db(tmp_path, n_days=40)
    cid = _start_and_rounds(path, days, capsys, start_idx=10)
    code, out, err = _judge(path, capsys, cycle=cid, asof=days[36], fix_kind="none",
                            freeze_days=30)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["branch"] == m2_config.BRANCH_INSUFFICIENT
    assert payload["conclusion"] is False
    assert payload["actions"] == []
    gate = payload["evidence"]["gate"]
    assert gate["meets"] is False and gate["n_sessions"] < gate["threshold"]


# ══════════════════════════════════════════════════════════════════════
# T4 —— 熔断可复现（分界 + 重放）
# ══════════════════════════════════════════════════════════════════════


def test_t4_the_threshold_boundary_is_exact(tmp_path, capsys):
    """9.99% **不**触发、10.00% 触发 —— 分界就在 `CIRCUIT_BREAKER_DRAWDOWN` 上。"""
    threshold = limits.CIRCUIT_BREAKER_DRAWDOWN
    cases = ((_p49_db(tmp_path, trough=TROUGH_UNDER, name="under.db"), False),
             (_p49_db(tmp_path, trough=TROUGH_AT, name="at.db"), True))
    for (path, days), expect in cases:
        cid = _start(path, days, capsys, rounds=2)
        code, out, err = _fuse(path, capsys, cycle=cid, asof=days[-1])
        assert code == 0, err
        payload = json.loads(out)
        assert payload["tripped"] is expect, payload["reason"]
        assert payload["threshold"] == threshold
        assert abs(payload["at_value"] - (PEAK - (TROUGH_UNDER if not expect
                                                 else TROUGH_AT)) / PEAK) < 1e-9
        c = _conn(path)
        try:
            assert c.execute("SELECT COUNT(*) FROM validation_events WHERE kind ="
                             " 'circuit_breaker'").fetchone()[0] == (1 if expect else 0)
        finally:
            c.close()


def test_t4_replay_of_the_same_input_is_bit_identical(tmp_path, capsys):
    """同一份历史输入重放两次 ⇒ `at_value` / `threshold` **逐位相同**。"""
    path, days = _p49_db(tmp_path, trough=TROUGH_AT)
    cid = _start(path, days, capsys, rounds=2)
    code1, out1, err1 = _fuse(path, capsys, cycle=cid, asof=days[-1])
    code2, out2, err2 = _fuse(path, capsys, cycle=cid, asof=days[-1])
    assert (code1, code2) == (0, 0), (err1, err2)
    p1, p2 = json.loads(out1), json.loads(out2)
    assert p1["at_value"] == p2["at_value"] and p1["threshold"] == p2["threshold"]
    assert abs(p1["at_value"] - limits.CIRCUIT_BREAKER_DRAWDOWN) < 1e-9
    assert p2["status"] == "already"
    assert p1["criteria_text"] == m2_config.CIRCUIT_CRITERIA_TEXT


def test_t4_a_later_asof_reports_the_same_verdict_without_new_rows(tmp_path, capsys):
    """换一个更晚的 asof 复查：结论仍是「已熔断」，事件数**不增**。"""
    path, days = _p49_db(tmp_path, trough=TROUGH_AT)
    cid = _start(path, days, capsys, rounds=2)
    code, out, err = _fuse(path, capsys, cycle=cid, asof=days[-1])
    assert code == 0, err
    p1 = json.loads(out)
    code, out, err = _fuse(path, capsys, cycle=cid,
                           asof=days[CYCLE_START_IDX + 20])
    assert code == 0, err
    p2 = json.loads(out)
    assert p2["status"] == "already" and p2["at_value"] == p1["at_value"]
    c = _conn(path)
    try:
        assert c.execute("SELECT COUNT(*) FROM validation_events WHERE kind="
                         " 'circuit_breaker'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM validation_events WHERE kind="
                         " 'validation_end'").fetchone()[0] == 1
    finally:
        c.close()


def test_t4_fuse_verdict_is_a_pure_function_of_the_nav_rows():
    """纯函数口径：同一串净值算两次逐位相同；空窗口 ⇒ `at_value=None` 且**不触发**。"""
    rows = [{"date": "2026-01-01", "nav": 100.0}, {"date": "2026-01-02", "nav": 91.0},
            {"date": "2026-01-03", "nav": 95.0}]
    a, b = selfeval.fuse_verdict(rows), selfeval.fuse_verdict(rows)
    assert a == b
    assert abs(a["at_value"] - 0.09) < 1e-9 and a["tripped"] is False
    assert a["trough_date"] == "2026-01-02"
    empty = selfeval.fuse_verdict([])
    assert empty["at_value"] is None and empty["tripped"] is False
    assert "没有净值行" in empty["reason"]


def test_t4_a_window_without_nav_rows_neither_trips_nor_writes(tmp_path, capsys):
    """窗口内没有净值行 ⇒ 不触发、不写事件、理由写清「判不了」（不拿 0 顶替）。"""
    path, days = _p49_db(tmp_path, with_nav=False)
    cid = _start(path, days, capsys, rounds=2)
    code, out, err = _fuse(path, capsys, cycle=cid, asof=days[-1])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["tripped"] is False and payload["at_value"] is None
    assert "没有净值行" in payload["reason"]
    c = _conn(path)
    try:
        assert _counts(c)["validation_events"] == 0
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T5 —— 熔断幂等（结构防线）
# ══════════════════════════════════════════════════════════════════════


def test_t5_the_unique_index_is_the_defense(tmp_path, capsys):
    """绕开接口直接再插一条熔断事件 ⇒ **唯一索引**拒掉（不是靠「先查再写」）。"""
    path, days = _p49_db(tmp_path, trough=TROUGH_AT)
    cid = _start(path, days, capsys, rounds=2)
    assert _fuse(path, capsys, cycle=cid, asof=days[-1])[0] == 0
    c = _conn(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO validation_events (cycle_id, script_id, kind,"
                " at_value, threshold, criteria_text, reason, created_at)"
                " VALUES (?,?,'circuit_breaker',0.9,0.1,'x','y',?)",
                (cid, SCRIPT_ID, NOW))
    finally:
        c.close()


def test_t5_repeat_checks_do_not_add_rows(tmp_path, capsys):
    """同 `(cycle_id, asof)` 连查三次 ⇒ 事件数恒为 2（熔断 + 收尾），退出码 0。"""
    path, days = _p49_db(tmp_path, trough=TROUGH_AT)
    cid = _start(path, days, capsys, rounds=2)
    for _ in range(3):
        code, _out, err = _fuse(path, capsys, cycle=cid, asof=days[-1])
        assert code == 0, err
    c = _conn(path)
    try:
        assert _counts(c)["validation_events"] == 2
    finally:
        c.close()


def test_t5_a_fused_cycle_is_closed_for_further_rounds_and_judgements(tmp_path, capsys):
    """熔断后周期已终止 ⇒ 再加轮次 / 再判定一律冲突（退出码 1），行数不变。"""
    path, days = _p49_db(tmp_path, trough=TROUGH_AT)
    cid = _start(path, days, capsys, rounds=2)
    assert _fuse(path, capsys, cycle=cid, asof=days[-1])[0] == 0
    code_a, _o, err_a = _run(capsys, "m2", "cycle", "round", "--cycle", str(cid),
                             "--round-no", "1",
                             "--asof", days[CYCLE_START_IDX + 5],
                             "--db", str(path), "--now", NOW)
    code_b, _o, err_b = _run(capsys, "m2", "cycle", "end", "--cycle", str(cid),
                             "--reason", "再收一次", "--db", str(path), "--now", NOW)
    assert code_a == 1, err_a
    assert code_b == 0, err_b            # 收尾是幂等的（already → 0）
    c = _conn(path)
    try:
        counts = _counts(c)
        assert counts["validation_rounds"] == 0
        assert counts["validation_events"] == 2      # 熔断那次已经写了收尾行
    finally:
        c.close()


def test_t5_a_second_cycle_start_for_the_same_account_conflicts(tmp_path, capsys):
    """同一 (策略版本, 账户) 已有未收尾周期 ⇒ 再开一个退出码 1（不重复落库）。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    code, _out, err = _run(capsys, "m2", "cycle", "start", "--script-id",
                           str(SCRIPT_ID), "--account-id", AI_ACCOUNT,
                           "--rounds", "2", "--days", "30", "--criteria-text",
                           CRITERIA, "--start-date", days[CYCLE_START_IDX],
                           "--db", str(path), "--now", NOW)
    assert code == 1, err
    c = _conn(path)
    try:
        assert _counts(c)["validation_cycles"] == 1
    finally:
        c.close()


def test_t5_a_duplicate_round_number_conflicts(tmp_path, capsys):
    """同一周期同一轮次再落一次 ⇒ 退出码 1（补跑不许静默覆盖）。"""
    path, days = _p49_db(tmp_path)
    cid = _start(path, days, capsys)
    argv = ("m2", "cycle", "round", "--cycle", str(cid), "--round-no", "1",
            "--asof", days[CYCLE_START_IDX + 6], "--db", str(path), "--now", NOW)
    assert _run(capsys, *argv)[0] == 0
    code, _out, err = _run(capsys, *argv)
    assert code == 1, err
    c = _conn(path)
    try:
        assert _counts(c)["validation_rounds"] == 1
    finally:
        c.close()


def test_t5_judging_twice_on_the_same_day_is_idempotent(tmp_path, capsys):
    """同 `(周期, 判定日)` 再判一次 ⇒ 退出码 0、返回既有行、台账仍 1 行。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    code, out, err = _judge(path, capsys, asof=days[-1], fix_kind="none",
                            freeze_days=30)
    assert code == 0, err
    first = json.loads(out)
    code, out, err = _judge(path, capsys, asof=days[-1], fix_kind="none",
                            freeze_days=30)
    assert code == 0, err
    second = json.loads(out)
    assert second["status"] == "already"
    assert second["branch"] == first["branch"]
    assert second["evidence"] == first["evidence"]
    c = _conn(path)
    try:
        assert _counts(c)["m2_judgements"] == 1
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T6 —— 判据原文不可事后改写
# ══════════════════════════════════════════════════════════════════════


def test_t6_criteria_text_is_byte_identical_before_and_after_judging(tmp_path, capsys):
    """判定前后，周期与判定行里的 `criteria_text` **逐字节相同**。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    c = _conn(path)
    try:
        before = c.execute("SELECT criteria_text FROM validation_cycles"
                           " WHERE cycle_id = 1").fetchone()[0]
    finally:
        c.close()
    assert _judge(path, capsys, asof=days[-1], fix_kind="none", freeze_days=30)[0] == 0
    c = _conn(path)
    try:
        after = c.execute("SELECT criteria_text FROM validation_cycles"
                          " WHERE cycle_id = 1").fetchone()[0]
        stored = c.execute("SELECT criteria_text FROM m2_judgements").fetchone()[0]
    finally:
        c.close()
    assert before == after == stored == CRITERIA


def test_t6_the_round_does_not_rewrite_the_cycle(tmp_path, capsys):
    """落轮次也不许改写周期行（`validation_cycles` 前后逐行相等）。"""
    path, days = _p49_db(tmp_path)
    cid = _start(path, days, capsys)
    c = _conn(path)
    try:
        before = [tuple(r) for r in c.execute("SELECT * FROM validation_cycles")]
    finally:
        c.close()
    code, _out, err = _run(capsys, "m2", "cycle", "round", "--cycle", str(cid),
                           "--round-no", "1", "--asof", days[CYCLE_START_IDX + 6],
                           "--db", str(path), "--now", NOW)
    assert code == 0, err
    c = _conn(path)
    try:
        after = [tuple(r) for r in c.execute("SELECT * FROM validation_cycles")]
    finally:
        c.close()
    assert after == before


@pytest.mark.parametrize("sql", [
    "UPDATE validation_cycles SET criteria_text = '换一个口径'",
    "UPDATE validation_events SET criteria_text = '换一个口径'",
    "UPDATE m2_judgements SET criteria_text = '换一个口径'",
    "DELETE FROM validation_cycles",
    "DELETE FROM validation_events",
    "DELETE FROM m2_judgements",
])
def test_t6_append_only_triggers_block_rewrites(tmp_path, capsys, sql):
    """三张台账表 + 判定表都被 append-only 触发器护住（UPDATE / DELETE 一律拒）。"""
    path, days = _p49_db(tmp_path, trough=TROUGH_AT)
    cid = _start(path, days, capsys, rounds=2)
    assert _fuse(path, capsys, cycle=cid, asof=days[-1])[0] == 0
    c = _conn(path)
    try:
        c.execute("INSERT INTO m2_judgements (cycle_id, script_id, asof_date, branch,"
                  " evidence_json, criteria_text, created_at)"
                  " VALUES (?,?,?,?,'{}',?,?)",
                  (cid, SCRIPT_ID, days[CYCLE_START_IDX], "insufficient", CRITERIA, NOW))
        c.commit()
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(sql)
    finally:
        c.close()


def test_t6_the_event_stores_at_value_and_threshold_separately(tmp_path, capsys):
    """`at_value`（实测）与 `threshold`（阈值）**分列**，判据原文取自常量。"""
    path, days = _p49_db(tmp_path, trough=TROUGH_AT)
    cid = _start(path, days, capsys, rounds=2)
    assert _fuse(path, capsys, cycle=cid, asof=days[-1])[0] == 0
    c = _conn(path)
    try:
        rows = c.execute("SELECT at_value, threshold, criteria_text, reason FROM"
                         " validation_events WHERE kind='circuit_breaker'").fetchall()
    finally:
        c.close()
    assert len(rows) == 1
    assert abs(rows[0]["at_value"] - limits.CIRCUIT_BREAKER_DRAWDOWN) < 1e-9
    assert rows[0]["threshold"] == limits.CIRCUIT_BREAKER_DRAWDOWN
    assert rows[0]["criteria_text"] == m2_config.CIRCUIT_CRITERIA_TEXT
    assert days[-1] in rows[0]["reason"]          # 实测留痕里带 asof（PIT 可复现）


# ══════════════════════════════════════════════════════════════════════
# T7 —— 错判案例回流：只读汇总、不填归因
# ══════════════════════════════════════════════════════════════════════


def _cycle_dict(conn, cid):
    from stocklab.store import validation as ledger
    return ledger.get_cycle(conn, cid)


def test_t7_the_backlog_never_fills_attribution(tmp_path, capsys):
    """清单里归因字段**恒 None**（含每个明细项），并带来源指纹与生成时间。"""
    path, days = _p49_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    c = _conn(path)
    try:
        backlog = m2_data.review_backlog(c, _cycle_dict(c, cid), days[-1])
    finally:
        c.close()
    assert backlog["n_cases"] > 0, "夹具应当造出方向判错的样本"
    assert backlog["generated_at"] is not None
    assert backlog["fingerprints"]["script_versions"] == ["s1"]
    assert all(len(s) == 64 for s in backlog["fingerprints"]["input_sha256"])
    for group in backlog["codes"]:
        for item in group["items"]:
            assert item[m2_config.ATTRIBUTION_FIELD] is None
    assert backlog["read_only_note"].startswith("只读汇总")


def test_t7_the_backlog_is_read_only(tmp_path, capsys):
    """回流是**只读汇总**：调用前后全部相关表逐行相等（它不写任何表）。"""
    path, days = _p49_db(tmp_path)
    cid = _start_and_rounds(path, days, capsys)
    watched = ("validation_cycles", "validation_rounds", "validation_events",
               "m2_forecasts", "m2_forecast_scores", "m2_judgements")
    c = _conn(path)
    try:
        before = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t}")]
                  for t in watched}
        cycle = _cycle_dict(c, cid)
        m2_data.review_backlog(c, cycle, days[-1])
        m2_data.case_summary(c, cycle, days[-1])
        after = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t}")]
                 for t in watched}
    finally:
        c.close()
    assert after == before


def test_t7_the_page_has_no_attribution_wording(tmp_path, capsys):
    """页面上没有「原因」二字，归因列写「恒空」，也没有 ADR-023 的禁词。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    assert _judge(path, capsys, asof=days[-1], fix_kind="none",
                  freeze_days=30)[0] == 0
    c = _conn(path)
    try:
        html = m2_render.m2_page(m2_data.panel(c, days[-1]), base="/lab",
                                 built_at=NOW)
    finally:
        c.close()
    assert "原因" not in html
    assert "归因（恒空）" in html and "待复核清单" in html
    for word in BANNED:
        assert word not in html, f"页面出现了 ADR-023 禁词 {word}"


# ══════════════════════════════════════════════════════════════════════
# T8 —— 页面与 Web 层零写入口
# ══════════════════════════════════════════════════════════════════════

#: 周期主干的写入口 —— `labweb/` 里出现**任何一个**名字都判红。
WRITE_ENTRIES = ("start_cycle", "add_round", "end_cycle", "insert_event",
                 "insert_cycle", "insert_round", "insert_judgement", "judge",
                 "fuse_check")


def _labweb_write_refs(root: pathlib.Path) -> list[str]:
    hits = []
    for path in sorted((root / "labweb").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        found = sorted(names & set(WRITE_ENTRIES))
        if found:
            hits.append(f"{path.relative_to(root).as_posix()}: {found}")
    return hits


def test_t8_labweb_never_touches_a_write_entry():
    """`labweb/` 对周期主干的写路径**零引用**（只读视图只读**值**，不读入口）。"""
    assert (ROOT / "stocklab/labweb").is_dir(), "扫描零个目录 = 无效实验"
    assert list((ROOT / "stocklab/labweb").glob("*.py")), "扫描零个文件 = 无效实验"
    assert _labweb_write_refs(ROOT / "stocklab") == []


def test_t8_the_labweb_scan_flags_a_planted_violation(tmp_path):
    """反向自检：把一次 `cycle.fuse_check(...)` 放进 labweb，扫描必须判红。"""
    (tmp_path / "labweb").mkdir(parents=True)
    (tmp_path / "labweb" / "sneaky.py").write_text(
        "from stocklab.m2 import cycle as m2_cycle\n"
        "def go(conn):\n"
        "    return m2_cycle.fuse_check(conn, cycle_id=1, asof='x', now='y')\n",
        encoding="utf-8")
    hits = _labweb_write_refs(tmp_path)
    assert len(hits) == 1 and "fuse_check" in hits[0]


def test_t8_the_page_has_no_form_or_button(tmp_path, capsys):
    """页面 HTML 里没有表单、没有按钮、也没有 POST 路由。"""
    from stocklab.labweb.app import Context, handle
    from stocklab.labweb.data import Lab
    from stocklab.labweb.tokens import TokenSigner, new_secret

    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    resp = handle("POST", "/lab/m2", body=b"",
                  ctx=Context(lab=Lab(path, asof=days[-1]),
                              signer=TokenSigner(new_secret()), base_path="/lab"))
    assert resp.status == 404
    c = _conn(path)
    try:
        html = m2_render.m2_page(m2_data.panel(c, days[-1]), base="/lab",
                                 built_at=NOW)
    finally:
        c.close()
    assert "<form" not in html and 'method="post"' not in html
    assert "<button" not in html and "<input" not in html
    assert "criteria" in html.lower()             # 判据原文确实摆在页面上


def test_t8_the_page_takes_its_numbers_from_the_data_function(tmp_path, capsys):
    """`/lab/m2` 拿到的就是 `m2_data.panel` 的那一份（新增一节之后仍然同源）。"""
    from stocklab.labweb.data import Lab

    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    c = _conn(path)
    try:
        direct = m2_data.panel(c, days[-1])
    finally:
        c.close()
    via_page = Lab(path, asof=days[-1]).m2_panel()
    assert via_page == direct
    html = m2_render.m2_page(via_page, base="/lab", built_at=NOW)
    for label in ("三方对标", "预测校验", "错判案例集", "自评估判定与熔断"):
        assert label in html
    assert "否则不进下一轮" in html and "周期 #1" in html


def test_t8_the_cli_report_carries_the_new_section(tmp_path, capsys):
    """`m2 report` 的文本版与页面**同一份载荷**，且有 P49 那一节。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    code, out, err = _run(capsys, "m2", "report", "--db", str(path),
                          "--asof", days[-1], "--json")
    assert code == 0, err
    payload = json.loads(out)
    c = _conn(path)
    try:
        assert payload == m2_data.panel(c, days[-1])
    finally:
        c.close()
    text = m2_render.m2_text(payload)
    assert "自评估判定与熔断" in text and "待复核清单" in text


def test_t8_cycle_status_is_read_only(tmp_path, capsys):
    """`m2 cycle status` 只读：跑完之后四张表的行数不变。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    c = _conn(path)
    try:
        before = _counts(c)
    finally:
        c.close()
    code, out, err = _run(capsys, "m2", "cycle", "status", "--cycle", "1",
                          "--db", str(path))
    assert code == 0, err
    payload = json.loads(out)
    assert payload["cycle"]["cycle_id"] == 1 and payload["rounds"]
    assert payload["ended"] is False and payload["fused"] is False
    assert payload["boundaries"] == selfeval.BOUNDARIES
    c = _conn(path)
    try:
        assert _counts(c) == before
    finally:
        c.close()


def test_t8_a_missing_cycle_is_an_input_error(tmp_path, capsys):
    """周期不存在 ⇒ 退出码 2（「输入不合法」），不是 1、也不是崩栈。"""
    path, _days = _p49_db(tmp_path)
    code, _out, err = _run(capsys, "m2", "cycle", "status", "--cycle", "99",
                           "--db", str(path))
    assert code == 2, err
    assert "没有 cycle_id=99" in err


def test_t8_a_bad_date_is_refused(tmp_path, capsys):
    """日期不是 `YYYY-MM-DD` ⇒ 拒绝（不做补零/猜测，否则同一事实会落成两行）。"""
    path, _days = _p49_db(tmp_path)
    code, _out, err = _run(capsys, "m2", "cycle", "start", "--script-id",
                           str(SCRIPT_ID), "--account-id", AI_ACCOUNT,
                           "--rounds", "2", "--days", "30", "--criteria-text",
                           CRITERIA, "--start-date", "2026-1-1", "--db", str(path),
                           "--now", NOW)
    assert code == 2, err
    assert "YYYY-MM-DD" in err
    c = _conn(path)
    try:
        assert _counts(c)["validation_cycles"] == 0
    finally:
        c.close()


def test_t8_the_round_metrics_come_from_the_existing_readout_functions(tmp_path, capsys):
    """轮次读数的五指标与门禁与 `paper_data.performance` **逐字段相等**（不平行实现）。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    c = _conn(path)
    try:
        perf = paper_data.performance(c, days[CYCLE_START_IDX + 26])   # 第 2 轮的 asof
        row = next(r for r in perf["rows"] if r["account_id"] == AI_ACCOUNT)
        stored = json.loads(c.execute(
            "SELECT metrics_json FROM validation_rounds WHERE round_no = 2"
        ).fetchone()[0])
    finally:
        c.close()
    assert stored["metrics"] == {k: row[k] for k in perf["metric_keys"]}
    assert stored["sample_gate"] == perf["sample_gate"]
    assert stored["excess_vs_index_300"] == perf["excess_vs_index_300"][AI_ACCOUNT]
    assert "paper_data.performance" in stored["source"]


def test_t8_reverse_self_check_the_watched_tables_are_not_empty(tmp_path, capsys):
    """反向自检的另一半：`_counts` 真的读到了行（否则「前后相等」毫无意义）。"""
    path, days = _p49_db(tmp_path)
    _start_and_rounds(path, days, capsys)
    c = _conn(path)
    try:
        counts = _counts(c)
        assert counts["validation_cycles"] == 1 and counts["validation_rounds"] == 2
        assert c.execute("SELECT COUNT(*) FROM m2_forecast_scores").fetchone()[0] > 0
    finally:
        c.close()
