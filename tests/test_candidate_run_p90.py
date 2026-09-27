"""P90：`candidate run` 拒绝行去重（根因 A）。

任务书：`docs/tasks/2026-09-27-p90-candidate-run拒绝行去重与快照原子化.md`
（D1/D2/D6/D7）。

根因 A（只读复核结论＝任务书 §0.2）：打分循环对 `eligible_pools(inst)` 的**每个池**
各判一次，拒绝时 `stage` 恒为 `'score'`、**不带池名** ⇒ 一只标的在 `mid` 与 `long`
两池都打分不通过时写出两行同 `(code, stage)`，撞 `candidate_rejects` 的主键
`(snapshot_id, code, stage)`（`store/schema.sql:1004-1012`）。

D1 的去重口径：按 `(code, stage)` 唯一，保留**流水线顺序**里的第一条
（`eligible_pools` 顺序 `short→mid→long` ⇒ 银行保留的是 `mid` 池那条），
**不改 reason 文案、不把池名塞进 stage、不动 schema**。

**去重落在写路径（`run_candidate` / `_dedup_rejects`）而不是内核
`score_pipeline`**：D2 要求计数键进快照 params，但 P83 的两条红线
（`tests/test_research_factor.py::test_params_keys_unchanged` /
`test_default_path_digest_is_unchanged_from_head`、
`tests/test_research_signal.py::test_pipeline_result_exposes_scored_without_changing_derivation`）
把内核 `params` 的键集与 digest **逐一钉死**，而任务书 §1 的允许改动面不含这两个
文件（「超出即停」）。把新键放在写路径上，三条硬约束同时满足：内核 `params` 键集
真的没变（红线仍然有效，不是被放宽），快照 `params_json` 里有计数（D2）。
`test_kernel_params_key_set_is_unchanged` 把这个落点固定住。

快照写入原子化（根因 B）的用例在 `tests/test_candidate_snapshot.py` 的 P90 段。
"""

from __future__ import annotations

from datetime import date, timedelta

from stocklab.candidate import pools
from stocklab.candidate import run as candidate_run
from stocklab.candidate.run import score_pipeline
from stocklab.config.universe import ASSET_STOCK, Instrument
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
ASOF = "2026-09-17"

#: 插桩0 通过；插桩1（短池）通过；插桩2（中池）/插桩3（长池）**都拒绝** ——
#: 这正是根因 A 的现场：同一 `(code, stage='score')` 被产出两次。
PLUGINS = {
    "0": "def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n",
    "1": "def run(ctx):\n    return {'score': 80.0, 'pass_flag': True,"
         " 'reason': '量价', 'risk_list': []}\n",
    "2": "def run(ctx):\n    return {'score': 0.0, 'pass_flag': False,"
         " 'reason': '可用财务因子不足 2 个', 'risk_list': []}\n",
    "3": "def run(ctx):\n    return {'score': 0.0, 'pass_flag': False,"
         " 'reason': '可用财务因子不足 2 个', 'risk_list': []}\n",
    "4": "def run(ctx):\n    return {'final_score': ctx['raw_score'],"
         " 'risk_out': []}\n",
}

BANK = Instrument("600036", "招商银行", "sh", "main", ASSET_STOCK, "银行")
BANK2 = Instrument("601398", "工商银行", "sh", "main", ASSET_STOCK, "银行")


def _weekdays(n_days: int, end: str = ASOF) -> list[str]:
    days: list[str] = []
    cur = date.fromisoformat(end)
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur -= timedelta(days=1)
    return sorted(days)


def _seed(tmp_db, codes=("600036",)) -> object:
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sh','main','stock',?)",
        [(code, f"标的{code}", NOW) for code in codes])
    days = _weekdays(300)
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


# ---------------------------------------------------------------------------
# D1：去重按 (code, stage) —— 保留流水线顺序第一条
# ---------------------------------------------------------------------------

def test_kernel_keeps_both_rows_and_write_path_folds_them(tmp_db):
    """RED（修前）：写路径把两行同 `(code, stage)` 直接交给 `write_snapshot` ⇒ 撞主键。

    `eligible_pools(BANK)` = `(short, mid, long)`；mid 与 long 都拒 ⇒ 内核产出
    **两行**（无损），`_dedup_rejects` 折叠成 **1 行**，且必须保留 **mid** 池那条
    （流水线顺序在前）。
    """
    conn = _seed(tmp_db)
    pipe = score_pipeline(conn, asof=ASOF, universe=(BANK,))
    kernel_score_rows = [r for r in pipe.rejects if r.stage == "score"]
    assert [r.reason.split("池")[0] for r in kernel_score_rows] == ["mid", "long"], \
        "内核必须无损（每个池各一行）"

    kept, dropped = candidate_run._dedup_rejects(pipe.rejects)
    score_rows = [r for r in kept if r.stage == "score"]
    assert [r.code for r in score_rows] == ["600036"]
    assert score_rows[0].reason.startswith("mid池打分未通过"), (
        f"应保留 mid 池那条（流水线顺序在前），实际：{score_rows[0].reason!r}")
    assert dropped == 1
    conn.close()


def test_dedup_count_reaches_run_params(tmp_db):
    """D2：丢弃数写进快照 params 的新键 `n_reject_dups_dropped`（只增键）。"""
    conn = _seed(tmp_db)
    result = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly",
                                         now=NOW, universe=(BANK,))
    assert result.params["n_reject_dups_dropped"] == 1
    conn.close()


def test_kernel_params_key_set_is_unchanged(tmp_db):
    """P83 的两条红线（`test_research_factor` / `test_research_signal`）钉住内核
    `params` 的**键集**。D2 的新键必须落在**写路径**、不得进内核 —— 这条用例把
    那个落点固定下来，免得后来人「顺手」把它挪回 `score_pipeline` 里。"""
    conn = _seed(tmp_db)
    pipe = score_pipeline(conn, asof=ASOF, universe=(BANK,))
    assert set(pipe.params) == {"seed_count", "universe_id", "members_sha256",
                                "topn", "scoring_price_mode", "n_adj_fallback"}
    conn.close()


def test_dedup_count_is_zero_without_duplicates(tmp_db):
    """无重复时计数必须存在且为 0（不是缺键）。"""
    conn = _seed(tmp_db)
    # 只扫短池（ETF 口径）⇒ 不会产生跨池重复
    etf = Instrument("510300", "沪深300ETF", "sh", "main", "etf")
    result = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly",
                                         now=NOW, universe=(etf,))
    assert result.params["n_reject_dups_dropped"] == 0
    conn.close()


def test_dedup_is_per_code(tmp_db):
    """去重键是 `(code, stage)`：两只标的各丢一行 ⇒ 计数 2、剩两行。"""
    conn = _seed(tmp_db, codes=("600036", "601398"))
    pipe = score_pipeline(conn, asof=ASOF, universe=(BANK, BANK2))
    kept, dropped = candidate_run._dedup_rejects(pipe.rejects)
    score_rows = [r for r in kept if r.stage == "score"]
    assert sorted(r.code for r in score_rows) == ["600036", "601398"]
    assert dropped == 2
    conn.close()


def test_other_stages_are_not_dropped(tmp_db):
    """去重只收同 `(code, stage)` 的重复行；pre_screen 的淘汰行原样保留。"""
    conn = _seed(tmp_db)
    # 一只没有 K 线的标的 ⇒ pre_screen 淘汰（与 score 无关）
    conn.execute("INSERT INTO instruments (code, name, market, board, type,"
                 " added_at) VALUES ('000858','五粮液','sz','main','stock',?)",
                 (NOW,))
    conn.commit()
    short = Instrument("000858", "五粮液", "sz", "main", ASSET_STOCK)
    pipe = score_pipeline(conn, asof=ASOF, universe=(BANK, short))
    kept, dropped = candidate_run._dedup_rejects(pipe.rejects)
    stages = {(r.code, r.stage) for r in kept}
    assert ("000858", "pre_screen") in stages
    assert ("600036", "score") in stages
    assert dropped == 1
    conn.close()


# ---------------------------------------------------------------------------
# D6/D7：打分/分流口径不动、幂等语义不变
# ---------------------------------------------------------------------------

def test_run_candidate_writes_deduped_rejects(tmp_db):
    """端到端：主流程落库不再撞主键，rejects 每 `(code, stage)` 一行。"""
    conn = _seed(tmp_db)
    result = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly",
                                         now=NOW, universe=(BANK,))
    assert result.skipped is False
    keys = [(r.code, r.stage) for r in result.rejects]
    assert len(keys) == len(set(keys)), f"落库后仍有重复 (code, stage)：{keys}"
    assert keys == [("600036", "score")]
    assert result.rejects[0].reason.startswith("mid池打分未通过")
    conn.close()


def test_run_candidate_dedup_count_reaches_snapshot_params(tmp_db):
    """计数必须能在**读回的快照**里看到（candidate_snapshots.params_json）。"""
    from stocklab.candidate import snapshot

    conn = _seed(tmp_db)
    result = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly",
                                         now=NOW, universe=(BANK,))
    loaded = snapshot.load_snapshot(conn, result.snapshot_id)
    assert loaded["snapshot"]["params"]["n_reject_dups_dropped"] == 1
    # 幂等重跑路径读回的 params 与首跑逐位相同（同一份快照）
    again = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly",
                                        now=NOW, universe=(BANK,))
    assert again.skipped is True
    assert again.params == loaded["snapshot"]["params"]
    conn.close()


def test_default_universe_path_keeps_pool_topn_untouched(tmp_db):
    """D6：只增计数键，其余 params 键与分流口径一字不动。"""
    conn = _seed(tmp_db)
    pipe = score_pipeline(conn, asof=ASOF, universe=(BANK,))
    assert pipe.params["topn"] == dict(pools.POOL_TOPN)
    assert pipe.params["seed_count"] == 1
    conn.close()
