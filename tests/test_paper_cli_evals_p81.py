"""P81 / D2：`paper agent evals` —— 未成交腿台账的**读出口**（只读命令）。

P79 把「AI 想买什么、为什么没买成」落进了 `paper_agent_evals`，但**一个消费者都没有**
（`load_agent_evals` 零调用方）。于是「AI 意图 67% 仓位、账户 0% 现金」这类事故
（P79 §0.1 的真实故障）在页面上依然看不见。本站补上这个出口 —— 而且它是**给用户的入口**，
所以必须由测试钉住（不许靠手写 `sqlite3` 查表）。

判据（逐条对应任务书 T2）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | 空台账 ⇒ `exit 0` ＋ 空列表 ＋ `note`（「还没有」是合法读数） | 空表当错误返回 / 空列表冒充「AI 没动过」 |
| ② | 有行 ⇒ `code` / `action` / `reason` **原样**（不重算、不截断） | 在这里重新解释一遍理由 |
| ③ | 账户不存在 ⇒ `exit 2` ＋ 点名（不静默返回空） | 返回空列表（= 假装这台账是空的） |
| ④ | `--asof` 缺省 = 该臂 `MAX(asof)` | 缺省取今天 ⇒ 问到一条没有腿的日子 |
| ⑤ | **零写入**：命令前后逐表行数不变 | 读命令顺手落了一行 |

本文件**不碰真库**：夹具是 `tmp_path` 里 `init_db` 出来的**新库**，没有任何真实数据。
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import main
from stocklab.paper import engine, store as paper_store
from stocklab.paper.config import PAPER_START_DATE
from stocklab.paper.rules import _hold
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
NEXT = "2026-09-16"
ARM = "arm-agent"
#: 一条**长**理由（> 120 字）：对账表会截断它，读出口**不许**截断。
LONG_REASON = "目标 ¥2,710.00 与现市值 ¥0.00 差 ¥2,710.00 < 1 手（¥2,814.63）→ **不动**；" \
              "目标**未达成**，如实上报 —— " + "。" * 60


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p81-evals-cli.db"
    init_db(path)
    c = connect(path)
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('000333','000333','sz','main','stock',?)", (NOW,))
    c.execute("INSERT INTO trading_calendar (date, is_open, source, created_at)"
              " VALUES (?,1,'tencent',?)", (START, NOW))
    c.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
              " adj_mode, source, fetched_at)"
              " VALUES ('000333',?,'87.23','87.23','87.23','87.23',100,'none','x',?)",
              (START, NOW))
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,"
              " '首笔',?)", (NOW,))
    c.commit()
    # 起跑口径与真库一致（`_declared_seed` 会验）：现金 ¥11,314.91 / 000333×100。
    engine.init_accounts(c, start_date=START, now=NOW)
    c.close()
    return path


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db)])
    out, err = capsys.readouterr()
    return code, out, err


def _evals(db, *argv, capsys):
    code, out, err = run(db, "paper", "agent", "evals", *argv, capsys=capsys)
    return code, (json.loads(out) if out.strip().startswith("{") else out), err


def _insert(db, *, arm=ARM, asof=START, code="600900", reason="夹具：不足一手"):
    c = connect(db)
    try:
        return paper_store.insert_agent_eval(
            c, arm=arm, asof=asof,
            decision=_hold(code, reason, constraints=("lot_100",)), now=NOW)
    finally:
        c.close()


def _counts(db) -> dict:
    c = connect(db)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("paper_agent_evals", "paper_trades",
                          "paper_agent_decisions", "paper_capital_events",
                          "paper_nav_daily")}
    finally:
        c.close()


# ---------- ① 空台账 ----------

def test_t2_an_empty_ledger_is_exit_zero_with_a_note(db, capsys):
    """①：一行都没有 ⇒ `exit 0` ＋ `evals: []` ＋ `note` 说明「还没有」。

    空列表**不是**错误，也不是「AI 什么都没想做」：这台账里还没有任何行
    （P79 的落库只对之后执行的 run 生效）。措辞写清这一点，才不会被读反。
    """
    code, got, _ = _evals(db, "--arm", ARM, "--json", capsys=capsys)
    assert code == 0
    assert got["evals"] == [] and got["n_evals"] == 0
    assert got["asof"] is None and got["asof_source"] is None
    assert "还没有任何未成交腿" in got["note"], got["note"]
    # 人读模式也要给出「0 条」与那句话（不是打一片空白）。
    code, text, _ = _evals(db, "--arm", ARM, capsys=capsys)
    assert code == 0
    assert "未成交腿 0 条" in text, text
    assert "还没有任何未成交腿" in text, text


# ---------- ② 原样读出 ----------

def test_t2_rows_are_reported_verbatim(db, capsys):
    """②：`code` / `action` / `reason` **逐字**来自台账（不重算、不截断）。

    理由是**结论**（append-only）：读出口再解释一遍就等于替 AI 改口供。
    这一条同时钉住「读出口不做 120 字截断」—— 截断是**对账表**的显示口径，
    不是这张表的（两者混起来就再也拿不到原文了）。
    """
    _insert(db, code="600900", reason=LONG_REASON)
    _insert(db, code="603868", reason="目标 ¥3,010.00 与现市值 ¥0.00 差 ¥3,010.00 < 1 手")
    code, got, _ = _evals(db, "--arm", ARM, "--json", "--asof", START, capsys=capsys)
    assert code == 0 and got["n_evals"] == 2
    assert [r["code"] for r in got["evals"]] == ["600900", "603868"]
    rows = {r["code"]: r for r in got["evals"]}
    assert rows["600900"]["reason"] == LONG_REASON
    assert len(rows["600900"]["reason"]) == len(LONG_REASON) > 120
    assert rows["600900"]["action"] == "hold"
    assert rows["600900"]["asof"] == START and rows["600900"]["arm"] == ARM
    # 与库里的行逐字段同值（读出口 = 那几列的投影，不是重算出来的另一份）。
    c = connect(db)
    try:
        assert got["evals"] == paper_store.load_agent_evals(c, arm=ARM, asof=START)
    finally:
        c.close()


# ---------- ③ 账户不存在 ----------

def test_t2_a_missing_account_is_exit_two_and_names_it(db, capsys):
    """③：账户不存在 ⇒ `exit 2` ＋ 点名（沿用 `paper account` 的口径）。

    「这台账是空的」与「没有这个账户」在 JSON 上长得一样 —— 静默返回空列表就是
    把两者混成一件事（账户名拼错的人会以为「AI 从来没想动过」）。
    """
    before = _counts(db)
    code, out, err = run(db, "paper", "agent", "evals", "--arm", "arm-agent-nope",
                         capsys=capsys)
    assert code == 2
    assert out == "" or "不存在" in out
    assert "arm-agent-nope" in err and "不存在" in err, err
    assert _counts(db) == before, "被拒的命令不许写库"


# ---------- ④ 缺省 asof ----------

def test_t2_the_default_asof_is_the_latest_day_with_evals(db, capsys):
    """④：`--asof` 缺省 = 该臂 `MAX(asof)`（不是今天，也不是最早的一天）。"""
    _insert(db, asof=START, code="600900")
    _insert(db, asof=NEXT, code="603868")
    code, got, _ = _evals(db, "--arm", ARM, "--json", capsys=capsys)
    assert code == 0
    assert got["asof"] == NEXT and got["asof_source"] == "latest_eval"
    assert got["latest_eval_asof"] == NEXT
    assert [r["code"] for r in got["evals"]] == ["603868"]
    # 显式问一个**没有腿**的日子：空列表 ＋ 点名最新的一天在哪（别让人以为台账是空的）。
    code, got, _ = _evals(db, "--arm", ARM, "--json", "--asof", "2026-09-14",
                          capsys=capsys)
    assert code == 0 and got["evals"] == [] and got["asof_source"] == "explicit"
    assert NEXT in got["note"], got["note"]


# ---------- ⑤ 零写入 ----------

def test_t2_the_command_writes_nothing(db, capsys):
    """⑤：只读命令 —— 前后**逐表行数**不变（读出口不许留下痕迹）。"""
    _insert(db, code="600900")
    before = _counts(db)
    for argv in (("--json",), ("--json", "--asof", START), ()):
        code, _, _ = run(db, "paper", "agent", "evals", "--arm", ARM, *argv,
                         capsys=capsys)
        assert code == 0
    assert _counts(db) == before
