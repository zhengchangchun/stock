"""Task 15：主流程端到端（设计文档 §7 的 12 步）。"""

from datetime import date, timedelta

import pytest

from stocklab.candidate import run as candidate_run
from stocklab.cli.main import main
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
ASOF = "2026-09-17"

PLUGINS = {
    "0": "def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n",
    "1": "def run(ctx):\n    return {'score': 80.0, 'pass_flag': True,"
         " 'reason': '量价', 'risk_list': []}\n",
    "2": "def run(ctx):\n    return {'score': 60.0, 'pass_flag': True,"
         " 'reason': '景气', 'risk_list': []}\n",
    "3": "def run(ctx):\n    return {'score': 40.0, 'pass_flag': True,"
         " 'reason': '护城河', 'risk_list': []}\n",
    "4": "def run(ctx):\n    return {'final_score': ctx['raw_score'],"
         " 'risk_out': []}\n",
}


def _seed_db(tmp_db, *, n_days=300, n_instruments=3):
    """建一个够跑主流程的最小库：标的 + 日历 + 300 天 K 线 + 5 个 active 插桩。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    codes = ["000333", "600690", "600519"][:n_instruments]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, f"标的{code}", NOW) for code in codes])
    days = []
    cur = date(2025, 6, 1)
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'tencent',?)",
                  [(d, NOW) for d in days])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, 10.0, 10.0, 10.0, 10.0, NOW)
         for code in codes for d in days])
    for pid, text in PLUGINS.items():
        sid = store.insert_script(c, plugin_id=pid, version="1.0.0",
                                  source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
    c.commit()
    return c


def test_run_produces_three_pools(tmp_db):
    c = _seed_db(tmp_db)
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                         now=NOW)
    assert result.skipped is False
    pools = {m.pool for m in result.members}
    assert pools == {"short", "mid", "long"}
    c.close()


def test_run_is_idempotent(tmp_db):
    c = _seed_db(tmp_db)
    a = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    b = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert a.snapshot_id == b.snapshot_id
    assert b.skipped is True
    assert c.execute("SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 1
    c.close()


def test_run_report_is_deterministic(tmp_db):
    c = _seed_db(tmp_db)
    a = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    b = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert a.report_md == b.report_md
    c.close()


def test_etf_only_in_short_pool(tmp_db):
    c = _seed_db(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('510300','沪深300ETF','sh','main','etf',?)",
              (NOW,))
    days = [r[0] for r in c.execute("SELECT date FROM trading_calendar"
                                    " ORDER BY date")]
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES ('510300',?,4.5,4.5,4.5,4.5,1000,'none','x',?)",
                  [(d, NOW) for d in days])
    c.commit()
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                         now=NOW)
    etf_pools = {m.pool for m in result.members if m.code == "510300"}
    assert etf_pools == {"short"}
    c.close()


def test_short_history_instrument_is_rejected(tmp_db):
    """只有 10 天历史的标的必须落进淘汰清单，且原因可读。"""
    c = _seed_db(tmp_db, n_days=300)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('000858','五粮液','sz','main','stock',?)",
              (NOW,))
    days = [r[0] for r in c.execute("SELECT date FROM trading_calendar"
                                    " ORDER BY date LIMIT 10")]
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES ('000858',?,10,10,10,10,1000,'none','x',?)",
                  [(d, NOW) for d in days])
    c.commit()
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                         now=NOW)
    rejected = {r.code: r.reason for r in result.rejects}
    assert rejected.get("000858") == "insufficient_history"
    c.close()


def test_missing_plugin_aborts_run(tmp_db):
    """缺任一 active 插桩 → 明确报错，不产出半截快照。

    注意：不能用 DELETE 清掉插桩 —— 触发器会拦（append-only）。
    这里另建一个「有标的、有 K 线、但没有任何插桩」的库。
    """
    empty = tmp_db.parent / "no_plugin.db"
    init_db(empty)
    c = connect(empty)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('000333','美的','sz','main','stock',?)",
              (NOW,))
    days = []
    cur = date(2025, 6, 1)
    while len(days) < 300:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES ('000333',?,10,10,10,10,1000,'none','x',?)",
                  [(d, NOW) for d in days])
    c.commit()

    with pytest.raises(lifecycle.NoActivePlugin):
        candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert c.execute(
        "SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 0
    c.close()


def test_member_order_is_identical_between_fresh_and_skipped_run(tmp_db):
    """回归：新跑和幂等重跑的 RunResult.members/rejects 顺序必须一致。

    构造两只同分标的，按**字典序逆序**插入数据库，使「插入顺序」与
    load_snapshot 返回的「(pool, -adj_score, code) 顺序」有实质性差异。
    修复前新跑路径用 members 列表（插入序），重跑路径用 load_snapshot（排序序），
    两者不一致；修复后必须相同。
    """
    from datetime import date as _date, timedelta as _timedelta

    init_db(tmp_db)
    c = connect(tmp_db)

    # 两只股票，按逆字典序插入（600519 先 → 000333 后）
    # load_snapshot 会按 code ASC 返回同分成员，使顺序与插入序不同
    codes_reversed = ["600519", "000333"]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, f"标的{code}", NOW) for code in codes_reversed])

    days: list[str] = []
    cur = _date(2025, 6, 1)
    while len(days) < 300:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += _timedelta(days=1)
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'tencent',?)",
                  [(d, NOW) for d in days])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, 10.0, 10.0, 10.0, 10.0, NOW)
         for code in codes_reversed for d in days])
    for pid, text in PLUGINS.items():
        from stocklab.plugin import lifecycle as _lc, store as _st
        sid = _st.insert_script(c, plugin_id=pid, version="1.0.0",
                                source_text=text, note=None, now=NOW)
        _lc.record_submit(c, sid, actor="t", now=NOW)
        _lc.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        _lc.approve(c, sid, actor="t", reason="ok", now=NOW)
    c.commit()

    fresh = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                        now=NOW)
    skipped = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                          now=NOW)

    assert skipped.skipped is True, "第二次调用应当命中幂等跳过路径"

    fresh_members = [(m.code, m.pool) for m in fresh.members]
    skipped_members = [(m.code, m.pool) for m in skipped.members]
    assert fresh_members == skipped_members, (
        f"members 顺序不一致：\n  新跑={fresh_members}\n  重跑={skipped_members}")

    fresh_rejects = [(r.code, r.stage) for r in fresh.rejects]
    skipped_rejects = [(r.code, r.stage) for r in skipped.rejects]
    assert fresh_rejects == skipped_rejects, (
        f"rejects 顺序不一致：\n  新跑={fresh_rejects}\n  重跑={skipped_rejects}")

    c.close()


def test_cli_candidate_run(tmp_db, capsys, tmp_path):
    c = _seed_db(tmp_db)
    c.close()
    out = tmp_path / "pool.md"
    code = main(["candidate", "run", "--asof", ASOF, "--run-kind", "weekly",
                 "--db", str(tmp_db), "--now", NOW, "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "候选池报告" in text
    assert "## 短期池" in text


# ---------- Task 2：score_pipeline 只读内核 ----------

from stocklab.candidate.run import PipelineResult, score_pipeline


def test_score_pipeline_writes_nothing(tmp_db):
    """内核不许写任何 candidate_* 表。"""
    conn = _seed_db(tmp_db)
    before = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("candidate_snapshots", "candidate_members",
                  "candidate_rejects")
    }
    result = score_pipeline(conn, asof=ASOF)
    after = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("candidate_snapshots", "candidate_members",
                  "candidate_rejects")
    }
    assert before == after, "回放内核不得产生副作用"
    assert isinstance(result, PipelineResult)


def test_score_pipeline_matches_run_candidate_members(tmp_db):
    """回放内核与生产主流程在同一 asof 上必须选出完全相同的池成员。

    这是「回测跑的就是生产逻辑」的机械保证 —— 两者若分叉，所有回放结论
    都无意义。

    独立性守卫：score_pipeline 故意在 run_candidate 写完快照后调用，
    以验证它不依赖快照表的内容。若未来重构意外让 score_pipeline 读取
    candidate_snapshots，下面的断言会立刻暴露问题。
    """
    conn = _seed_db(tmp_db)
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 0

    # run_candidate 先跑，写入一条快照
    prod = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly", now=NOW)
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 1

    # score_pipeline 在快照已存在后才调用 —— 如果它依赖快照表会得到错误结果
    pipe = score_pipeline(conn, asof=ASOF)

    # 快照数量仍为 1：score_pipeline 既没有写入也没有（需要）读取快照
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 1, \
        "score_pipeline 不得写入快照表"

    key = lambda ms: sorted((m.code, m.pool, round(m.adj_score, 9)) for m in ms)
    assert key(pipe.members) == key(prod.members)
    assert sorted((r.code, r.stage, r.reason) for r in pipe.rejects) == \
           sorted((r.code, r.stage, r.reason) for r in prod.rejects)


def test_score_pipeline_overrides_plugin_version(tmp_db):
    """plugin_overrides 生效：换一版打分插桩，分数随之改变。"""
    conn = _seed_db(tmp_db)
    base = score_pipeline(conn, asof=ASOF)
    sid = store.insert_script(
        conn, plugin_id="1", version="9.9.9",
        source_text="def run(ctx):\n"
                    "    return {'score': 100.0, 'pass_flag': True,"
                    " 'reason': 'v2', 'risk_list': []}\n",
        note=None, now=NOW)
    over = score_pipeline(conn, asof=ASOF, plugin_overrides={"1": sid})
    short_base = {m.code: m.raw_score for m in base.members if m.pool == "short"}
    short_over = {m.code: m.raw_score for m in over.members if m.pool == "short"}
    assert short_over and all(v == 100.0 for v in short_over.values())
    assert short_over != short_base


def test_score_pipeline_unoverridden_plugin_uses_active(tmp_db):
    """没被覆盖的插件仍用 active —— 否则就不是单变量了。

    单变量验证场景：为 plugin_id="1" 插入一个非 active 的 v9.9.9（score=100.0），
    但只通过 plugin_overrides 覆盖 plugin_id="4"，不覆盖 plugin_id="1"。
    预期：未覆盖的 plugin 1 仍解析到其 active 版本（score=80.0），而非 v9.9.9
    （100.0）。这正是「单变量」的核心性质：只有被显式覆盖的插件发生变化。
    """
    conn = _seed_db(tmp_db)
    # 插入 plugin 1 的 v9.9.9（score=100.0），但不审批 → 非 active
    # 目的：证明未覆盖的 plugin 1 不会意外使用这个更高版本号的脚本
    _sid_v999 = store.insert_script(
        conn, plugin_id="1", version="9.9.9",
        source_text="def run(ctx):\n"
                    "    return {'score': 100.0, 'pass_flag': True,"
                    " 'reason': 'v2', 'risk_list': []}\n",
        note=None, now=NOW)
    # 只覆盖插桩4，插桩1 应仍走 active（80.0），而非未审批的 v9.9.9（100.0）
    r4 = store.list_scripts(conn, plugin_id="4")[0]["script_id"]
    over = score_pipeline(conn, asof=ASOF, plugin_overrides={"4": r4})
    short = [m for m in over.members if m.pool == "short"]
    # 分数必须等于 active 版本的 80.0，而非 v9.9.9 的 100.0
    assert short and all(m.raw_score == 80.0 for m in short), (
        f"期望 plugin 1 的 active 版本得分 80.0，实际: "
        f"{[m.raw_score for m in short]}"
    )
