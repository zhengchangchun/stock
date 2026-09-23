"""P48：模块2 三方对标（§1）＋ 预测校验（§2）＋ `/lab/m2`（§3）。

判据 → 用例的对应（与任务书 §4 的表一一对应）：

| # | 判据 | 用例 |
|---|---|---|
| T1 | 三方读数**同源** | `test_t1_*`：三方每行的五个值与 `paper_data.performance` **逐字段相等** |
| T2 | 指标**不平行实现** | `test_t2_*`：源码扫描 + **反向自检**（喂一段违规代码必须判红） |
| T3 | 门禁与**禁词** | `test_t3_*`：119 ⇒ `insufficient`；页面文本六个禁词逐词查；120 ⇒ 正常出读数 |
| T4 | 命中判据**对拍** | `test_t4_*`：同一输入分别喂本模块与 `verify/` 的打分器，逐位相等 |
| T5 | 分数 append-only + **幂等** | `test_t5_*`：重跑零写入；UPDATE / DELETE 被触发器拒 |
| T6 | **分版本不相加** | `test_t6_*`：两个 `script_version` 各一行；载荷里没有「合计」 |
| T7 | 错判案例**纪律** | `test_t7_*`：归因恒空、有总量与上限、没有筛选参数 |
| ＋ | 中证500（D-43） | `test_deferred_*`：缺席时明文写「本轮只对沪深300」；落库后变成显式待办 |

数字**一律从被测模块取**，不手抄 —— 手抄的那份会与上游漂移。
"""

from __future__ import annotations

import ast
import datetime
import json
import sqlite3
from pathlib import Path

import pytest

from stocklab.cli.main import main
from stocklab.labweb import m2_data, m2_render, paper_data
from stocklab.m2 import config as m2_config
from stocklab.m2 import score as m2_score
from stocklab.m2 import store as m2_store
from stocklab.paper import store as paper_store
from stocklab.paper.config import ARM_KIND_AGENT_RANDOM
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.predict.model import FLAT_BAND
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-22T16:00:00+08:00"
START = "2026-09-15"
STOCK = "600519"
AI_ACCOUNT = "arm-agent-v1"
MIRROR = m2_config.MIRROR_ACCOUNT

#: ADR-023 的措辞纪律：门禁没过时这六个词一个都不许出现在**任何**渲染路径上。
BANNED = ("跑赢", "跑输", "优于", "劣于", "领先", "胜过")

#: `paper_data.performance` 那五个指标（名字与顺序由 ADR-023 定死）。
KEYS = tuple(paper_data.METRIC_KEYS)


def _sessions(n: int, *, start: str = START) -> list[str]:
    """`start` 之后的 n 个**递增日期**（本夹具不需要真实日历，单调即可）。"""
    base = datetime.date.fromisoformat(start)
    return [(base + datetime.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _paper_db(tmp_path, *, n_sessions: int = 5, with_ai: bool = True
              ) -> tuple[Path, list[str]]:
    """一份带**两个模拟盘账户 + 基准**的最小库（三方对标用）。

    净值行是直接插的（不跑 `paper step`）：这一组测的是「三方读数与 P41 同源」，
    不是「引擎会不会算净值」—— 后者已有 P19/P41 自己的用例。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "three_way.db"
    init_db(path)
    c = connect(path)
    days = _sessions(n_sessions)
    axis = [START, *days]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sh','main',?,?)",
        [(STOCK, "贵州茅台", "stock", NOW), (INDEX_300_SYMBOL, "沪深300", "index", NOW)])
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in axis])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(STOCK, d, 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i, NOW)
         for i, d in enumerate(axis)]
        + [(INDEX_300_SYMBOL, d, 4000.0 + 8 * i, 4000.0 + 8 * i, 4000.0 + 8 * i,
            4000.0 + 8 * i, NOW) for i, d in enumerate(axis)])
    c.commit()
    accounts = [(MIRROR, "now", {"initial_capital": 20000.0})]
    if with_ai:
        accounts.append((AI_ACCOUNT, "agent", {
            "initial_capital": 20000.0,
            m2_config.EXECUTOR_KEY: m2_config.EXECUTOR_CHANNEL_A,
            "strategy_version": "v1"}))
    for account_id, arm, params in accounts:
        paper_store.insert_account(
            c, account_id=account_id, arm=arm, etf_target_pct=None,
            start_date=START, initial_cash=20000.0, initial_positions=[],
            initial_nav=20000.0, params=params, now=NOW)
    for account_id, drift in accounts_drift(accounts):
        for i, d in enumerate(days):
            nav = 20000.0 + drift + 20.0 * i
            paper_store.insert_nav(
                c, account_id=account_id, date=d, cash=nav, positions=[],
                market_value=0.0, nav=nav, drawdown=0.0, cum_cost=0.0,
                cum_return=round(nav / 20000.0 - 1.0, 6), net_deposits=20000.0,
                index_300_level=None, index_300_asof=None, now=NOW, commit=False)
    c.commit()
    c.close()
    return path, axis


def accounts_drift(accounts) -> list[tuple[str, float]]:
    """每臂的净值起点差（让两条净值曲线**不同** —— 相同的话「三方同源」是巧合）。"""
    return [(account_id, 100.0 * i) for i, (account_id, _, _) in enumerate(accounts)]


def _forecast_payload(direction: dict | None, *, lo: float = 95.0,
                      hi: float = 105.0) -> dict:
    return {"range_80": [lo, hi] if lo is not None else None,
            "direction": direction, "invalidate_if": "跌破 90 或 站上 110",
            "na_reasons": [], "schema_version": "1"}


def _scoring_db(tmp_path, *, n_sessions: int, closes: list[float] | None = None,
                script_version: str = "s1", plugin_id: str = "m2_a3",
                direction: dict | None = None, scores: bool = True,
                direction_missing: bool = False) -> tuple[Path, list[str]]:
    """一份能端到端打分的库：**价格路径 → 预测 → `m2 score` → 已落库的分数**。

    预测与打分都走真实实现（`m2_store.insert_forecast` / `m2_score.score_all`）——
    手插分数行就只能测「页面会不会读表」，测不出打分链路本身。

    交易日取 `n_sessions + 1` 个：第 i 条预测的决策日是第 i 天、目标日是第 i+1 天
    （`target_date` 是**下一个交易日**，见 `m2/score.py` 的模块 docstring）。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "scoring.db"
    init_db(path)
    c = connect(path)
    days = _sessions(n_sessions + 1)
    series = closes if closes is not None else [100.0 + i
                                               for i in range(n_sessions + 1)]
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES (?,?,'sh','main','stock',?)", (STOCK, "贵州茅台", NOW))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(STOCK, d, v, v, v, v, NOW) for d, v in zip(days, series)])
    c.commit()
    direction = (None if direction_missing
                 else direction or {"up": 0.6, "flat": 0.2, "down": 0.2})
    for i in range(n_sessions):
        m2_store.insert_forecast(
            c, plugin_id=plugin_id, channel="A", account_id=AI_ACCOUNT,
            asof=days[i], code=STOCK,
            payload=_forecast_payload(direction), script_id=1,
            script_version=script_version, input_sha256=f"{i:064d}", now=NOW)
    c.commit()
    if scores:
        m2_score.score_all(c, asof=days[-1], now=NOW)
    c.close()
    return path, days


def _conn(path):
    return connect(path)


# ══════════════════════════════════════════════════════════════════════
# T1 —— 三方读数同源（与 P41 的绩效指标集是同一份数）
# ══════════════════════════════════════════════════════════════════════


def test_t1_the_three_sides_are_field_identical_to_the_p41_payload(tmp_path):
    """三方每行的五个值 == `paper_data.performance` 同行的五个值（逐字段）。

    真正的钉子在这里：只断言「数字出现在页面上」不够 —— 页面完全可以自己
    再算一份而仍然通过。所以拿 **P41 的返回**逐字段比。
    """
    path, days = _paper_db(tmp_path)
    c = _conn(path)
    try:
        p41 = paper_data.performance(c, days[-1])
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    p41_rows = {r["account_id"]: r for r in p41["rows"]}
    assert {s["account_id"] for s in panel["three_way"]["sides"]} == {
        AI_ACCOUNT, MIRROR, INDEX_300_SYMBOL}
    for side in panel["three_way"]["sides"]:
        base = p41_rows[side["account_id"]]
        for key in KEYS:
            assert side[key] == base[key], f"{side['account_id']} 的 {key} 与 P41 不同源"
        assert side["n_sessions"] == base["n_sessions"]
        assert side["missing"] == base["missing"]
    # 门禁也是同一对象（同源同值，不是「凑巧一样」）
    assert panel["three_way"]["sample_gate"] == p41["sample_gate"]
    assert m2_data.SAMPLE_THRESHOLD == paper_data.PERFORMANCE_THRESHOLD


def test_t1_the_ai_side_is_the_channel_a_account_not_a_name_prefix(tmp_path):
    """AI 那一方按 `executor` 认：`arm-agent-random`（前缀相同）**不许**被列进来。"""
    path, days = _paper_db(tmp_path)
    c = _conn(path)
    try:
        paper_store.insert_account(
            c, account_id="arm-agent-random", arm=ARM_KIND_AGENT_RANDOM,
            etf_target_pct=None, start_date=START, initial_cash=20000.0,
            initial_positions=[], initial_nav=20000.0,
            params={"initial_capital": 20000.0}, now=NOW)
        c.commit()
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    ids = [s["account_id"] for s in panel["three_way"]["sides"]]
    assert AI_ACCOUNT in ids and "arm-agent-random" not in ids


def test_t1_a_missing_side_says_why_instead_of_writing_zero(tmp_path):
    """没有通路 A 的账户 ⇒ 那一方写「无」+ 原因（**不写 0，也不拿别的账户顶替**）。"""
    path, days = _paper_db(tmp_path, with_ai=False)
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    side = next(s for s in panel["three_way"]["sides"] if s["role"] == "ai")
    assert side["available"] is False
    assert side["reason"] and "不拿别的账户顶替" in side["reason"]
    assert all(side[k] is None for k in KEYS)
    html = m2_render.three_way_block(panel["three_way"])
    assert m2_render.NONE_MARK in html


def test_t1_the_benchmark_is_flagged_as_not_directly_tradable(tmp_path):
    """指数不可直接交易、无成本 ⇒ 口径偏乐观，**必须标注**（P48 §1 表格第 ③ 行）。"""
    path, days = _paper_db(tmp_path)
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    assert "不可直接交易" in panel["three_way"]["benchmark_caveat"]
    assert "无成本" in panel["three_way"]["benchmark_caveat"]


# ══════════════════════════════════════════════════════════════════════
# T2 —— 指标不平行实现（源码扫描 + 反向自检）
# ══════════════════════════════════════════════════════════════════════

#: 这四类**算法**只许有一份实现（`backtest/metrics.py` / `paper/engine.py`）。
FORBIDDEN_DEFS = frozenset({
    "annualize", "summarize", "win_rate", "profit_loss_ratio", "drawdown",
    "max_drawdown", "daily_returns", "excess_return",
})


def _scan(root: Path) -> dict[str, list[str]]:
    """哪些文件**定义**了被判为第二套实现的函数（`{}` = 没有）。"""
    hits: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = sorted({n.name for n in ast.walk(tree)
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                         & FORBIDDEN_DEFS)
        if defined:
            hits[str(path)] = defined
    return hits


def test_t2_no_second_metric_implementation_in_m2_or_labweb():
    """`m2/` 与 `labweb/` 里**一个**指标算法都不许自己定义。"""
    hits: dict[str, list[str]] = {}
    for pkg in ("stocklab/m2", "stocklab/labweb"):
        hits.update(_scan(ROOT / pkg))
    assert hits == {}, (
        f"出现了第二套指标实现：{hits} —— 五指标与门禁只许有一份真源"
        "（`backtest/metrics.py` / `paper/engine.py`），复制的那个会先漂")


def test_t2_the_scan_is_not_satisfied_by_an_empty_scan(tmp_path):
    """反向自检：把一份违规实现放进目录，扫描**必须**判红。"""
    bad = tmp_path / "fake_metrics.py"
    bad.write_text("def profit_loss_ratio(xs):\n    return 0.0\n\n"
                   "def drawdown(hist, nav):\n    return 0.0\n", encoding="utf-8")
    assert _scan(tmp_path) == {str(bad): ["drawdown", "profit_loss_ratio"]}


def test_t2_the_sources_are_actually_imported():
    """扫描之外再钉一条**正向**判据：本模块确实在用真源，而不是谁都没用。"""
    scoring = (ROOT / "stocklab/m2/score.py").read_text(encoding="utf-8")
    assert "score_prediction" in scoring and "load_scoring_bars" in scoring
    panel_src = (ROOT / "stocklab/labweb/m2_data.py").read_text(encoding="utf-8")
    tree = ast.parse(panel_src)
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert {"profit_loss_ratio", "performance"} <= attrs
    assert "drawdown" in names
    # 五个指标与门禁是**引用** P41 的，不是另写一遍
    assert "paper_data.METRIC_KEYS" in panel_src
    assert "paper_data.sample_gate" in panel_src


# ══════════════════════════════════════════════════════════════════════
# T3 —— 门禁与禁词
# ══════════════════════════════════════════════════════════════════════


def test_t3_119_scored_samples_are_insufficient(tmp_path):
    """样本 119（差一个）⇒ `insufficient`；页面照旧只给读数。"""
    path, days = _scoring_db(tmp_path, n_sessions=119,
                             closes=[100.0 * 1.01 ** i for i in range(120)])
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
        html = m2_render.m2_page(panel, base="/lab", built_at=NOW)
        text = m2_render.m2_text(panel)
    finally:
        c.close()
    group = panel["forecast"]["groups"][0]
    assert group["n_scored"] == 119
    assert group["gate"]["gate_status"] == "insufficient"
    assert group["gate"]["threshold"] == m2_data.SAMPLE_THRESHOLD
    for banned in BANNED:
        assert banned not in html, f"页面在样本不足时出现了结论性措辞：{banned}"
        assert banned not in text


def test_t3_120_scored_samples_clear_the_gate(tmp_path):
    """正例：120 条可评分 ⇒ `ok`，读数照常出（门禁翻转不是靠改文案）。"""
    path, days = _scoring_db(tmp_path, n_sessions=120,
                             closes=[100.0 * 1.01 ** i for i in range(121)])
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    group = panel["forecast"]["groups"][0]
    assert group["n_scored"] == 120
    assert group["gate"]["gate_status"] == "ok"
    assert group["win_rate"] == pytest.approx(1.0)   # 单调上涨 + 预测涨


def test_t3_the_gate_is_the_same_object_as_p41(tmp_path):
    """门禁走 `paper_data.sample_gate`（P41 那一把尺子）—— 断言同一函数、同一值。"""
    assert m2_data.SAMPLE_THRESHOLD == paper_data.PERFORMANCE_THRESHOLD
    # 「同一把尺子」的**函数级**判据在 `test_t2_the_sources_are_actually_imported`
    # （`"paper_data.sample_gate" in panel_src`）。这里原本有一条永远为真的空断言
    # （`assert ... if False else True`），nanobot 独立复核时删掉 ——
    # 「不能失败」的断言不是判据。
    short = paper_data.sample_gate(119)
    long_ = paper_data.sample_gate(120)
    assert (short["gate_status"], long_["gate_status"]) == ("insufficient", "ok")
    assert m2_data.panel is not None


def test_t3_missing_data_renders_none_not_zero(tmp_path):
    """缺数据 ⇒ 「无」+ 原因，**不写 0**，也不出现裸 `None` / `nan`。"""
    path, days = _scoring_db(tmp_path, n_sessions=2, scores=False,
                             closes=[100.0, 101.0, 102.0])
    c = _conn(path)
    try:
        html = m2_render.m2_page(m2_data.panel(c, days[-1]), base="/lab", built_at=NOW)
    finally:
        c.close()
    assert "0.00%" not in html
    for bad in ("None", "nan", "NaN"):
        assert bad not in html


# ══════════════════════════════════════════════════════════════════════
# T4 —— 命中判据对拍（与 `verify/` 同输入同结果）
# ══════════════════════════════════════════════════════════════════════

#: 构造用例：`(asof 收盘, target 收盘, 方向概率, 手工期望的「实际」档)`。
#: 「平」的边界由 `FLAT_BAND`（0.5%）定义 —— 这里挑的是**不含浮点歧义**的
#: 三档（±0.4% 在带内、±0.51% 刚出带、±0.6% 明显出带），
#: 正好落在带上的那一格由 `>` 比较本身决定，由下面的对拍逐位钉住。
T4_CASES = (
    (100.0, 100.4, {"up": 0.6, "flat": 0.2, "down": 0.2}, "flat"),
    (100.0, 100.51, {"up": 0.6, "flat": 0.2, "down": 0.2}, "up"),
    (100.0, 99.6, {"up": 0.6, "flat": 0.2, "down": 0.2}, "flat"),
    (100.0, 99.49, {"up": 0.6, "flat": 0.2, "down": 0.2}, "down"),
    (100.0, 100.6, {"up": 0.4, "flat": 0.4, "down": 0.2}, "up"),      # 平手 up/flat
    (100.0, 100.6, {"up": 0.2, "flat": 0.4, "down": 0.4}, "up"),
)


def _one_case(tmp_path, name: str, close_a: float, close_t: float,
              direction: dict) -> tuple[dict, dict]:
    """跑一条真实预测 + `m2 score`，同时**独立**构造同一份输入问 verify 的打分器。"""
    path, days = _scoring_db(tmp_path / name, n_sessions=1,
                             closes=[close_a, close_t], direction=direction)
    c = _conn(path)
    try:
        mine = m2_store.list_scores(c)[0]
        target = str(mine["target_date"])
        from stocklab.verify.score import score_prediction
        from stocklab.verify.service import load_scoring_bars
        adjusted, raw, suspended, err = load_scoring_bars(c, STOCK, target)
        pred = {"pred_id": 1, "asof_date": _sessions(2)[0], "target_date": target,
                "code": STOCK, "direction": direction, "range_80": [95.0, 105.0],
                "invalidate_if": "跌破 90 或 站上 110", "key_levels": [],
                "size_pct": 0.0}
        theirs = score_prediction(pred, bars=adjusted, raw_bars=raw,
                                  suspended=suspended, adjust_error=err)
    finally:
        c.close()
    return mine, theirs


@pytest.mark.parametrize("close_a,close_t,direction,expected_class",
                         T4_CASES, ids=[f"c{i}" for i in range(len(T4_CASES))])
def test_t4_direction_hit_is_bit_identical_to_the_verify_scorer(
        tmp_path, close_a, close_t, direction, expected_class):
    """同一输入 → 本模块的落库行与 `verify/` 的打分器**逐位相等**。

    两件事一起钉：① 我这一层的接线没走样（值从打分器原样落到库里）；
    ② 档位判据（±`FLAT_BAND` + argmax 平手序）与 verify 是同一套 —— 上面
    手写的 `expected_class` 是**独立**的一档期望，谁改了 `FLAT_BAND`
    或平手优先序，它立刻与打分器打架。
    """
    mine, theirs = _one_case(tmp_path, f"c{close_a}-{close_t}", close_a, close_t,
                             direction)
    assert mine["hit_direction"] == theirs["hit_direction"]
    assert mine["actual_class"] == theirs["notes"]["actual_class"]
    assert mine["pred_class"] == theirs["notes"]["pred_class"]
    assert mine["actual_pct"] == theirs["actual_pct"]
    assert mine["range_hit"] == theirs["hit_range"]
    assert mine["invalidated"] == theirs["invalidated"]
    # 手工期望的「实际」档（与打分器无关的一档）
    assert mine["actual_class"] == expected_class
    pct = close_t / close_a - 1.0
    expected = ("up" if pct > FLAT_BAND
                else "down" if pct < -FLAT_BAND else "flat")
    assert mine["actual_class"] == expected


def test_t4_a_flat_prediction_gets_no_bet(tmp_path):
    """预测「平」⇒ `bet_pct = 0`（零收益日两边都不计入盈亏比，不是赢也不是输）。"""
    path, days = _scoring_db(tmp_path, n_sessions=1, closes=[100.0, 100.2],
                             direction={"up": 0.2, "flat": 0.6, "down": 0.2})
    c = _conn(path)
    try:
        row = m2_store.list_scores(c)[0]
    finally:
        c.close()
    assert row["pred_class"] == "flat"
    assert row["bet_pct"] == 0.0


def test_t4_a_prediction_without_direction_is_unscorable_not_guessed(tmp_path):
    """插桩明说「不知道」（`direction=None`）⇒ 一行空结果 + 专用原因码，**不猜**。"""
    path, days = _scoring_db(tmp_path, n_sessions=1, closes=[100.0, 101.0],
                             direction_missing=True)
    c = _conn(path)
    try:
        row = m2_store.list_scores(c)[0]
    finally:
        c.close()
    assert row["scorable"] == 0
    assert row["reason_code"] == m2_config.REASON_NO_DIRECTION
    assert row["hit_direction"] is None and row["actual_pct"] is None


# ══════════════════════════════════════════════════════════════════════
# T5 —— append-only + 幂等
# ══════════════════════════════════════════════════════════════════════


def test_t5_rescoring_writes_nothing_the_second_time(tmp_path):
    """同一批预测重跑 ⇒ 零新行（`already` = 全部），分数表逐行不变。"""
    path, days = _scoring_db(tmp_path, n_sessions=5)
    c = _conn(path)
    try:
        before = [tuple(r) for r in c.execute(
            "SELECT * FROM m2_forecast_scores ORDER BY score_id")]
        out = m2_score.score_all(c, asof=days[-1], now=NOW)
        after = [tuple(r) for r in c.execute(
            "SELECT * FROM m2_forecast_scores ORDER BY score_id")]
    finally:
        c.close()
    assert out["already"] == 5 and out["scored"] == 0
    assert before == after and len(before) == 5


def test_t5_a_pending_target_writes_no_row(tmp_path):
    """目标日还没到（`> asof`）⇒ **不落行**（「尚未到期」不同于「判出不可评分」）。"""
    path, days = _scoring_db(tmp_path, n_sessions=5, scores=False)
    c = _conn(path)
    try:
        # 截止日卡在中间：4 条预测里只有前两条的目标日 <= days[2]
        out = m2_score.score_all(c, asof=days[2], now=NOW)
        n_rows = c.execute("SELECT COUNT(*) FROM m2_forecast_scores").fetchone()[0]
    finally:
        c.close()
    assert out["scored"] == 2 and out["pending"] == 3
    assert n_rows == 2


def test_t5_the_scores_table_is_append_only(tmp_path):
    """UPDATE / DELETE 被触发器拒（P48 §2 的「不许覆盖历史行」是结构性的）。"""
    path, days = _scoring_db(tmp_path, n_sessions=2)
    c = _conn(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE m2_forecast_scores SET hit_direction = 1")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM m2_forecast_scores")
    finally:
        c.close()


def test_t5_one_forecast_cannot_have_two_scores(tmp_path):
    """幂等键是**结构性**的：绕过本模块直接插第二行也进不去。"""
    path, days = _scoring_db(tmp_path, n_sessions=2)
    c = _conn(path)
    try:
        row = m2_store.list_scores(c)[0]
        with pytest.raises(sqlite3.IntegrityError):
            m2_store.insert_score(c, score=row, now=NOW)
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T6 —— 分版本不相加
# ══════════════════════════════════════════════════════════════════════


def test_t6_two_script_versions_appear_separately(tmp_path):
    """两个 `script_version` 的行分别出现，且**没有**任何「合计」行或合计字段。"""
    a, da = _scoring_db(tmp_path / "a", n_sessions=3, script_version="s1")
    b, db_ = _scoring_db(tmp_path / "b", n_sessions=4, script_version="s2")
    c = _conn(a)
    try:
        m2_score.score_all(c, asof=da[-1], now=NOW)
        panel = m2_data.panel(c, da[-1])
    finally:
        c.close()
    c = _conn(b)
    try:
        panel_b = m2_data.panel(c, db_[-1])
    finally:
        c.close()
    versions = {g["script_version"] for g in panel["forecast"]["groups"]}
    assert versions == {"s1"} and panel["forecast"]["groups"][0]["n_scored"] == 3
    assert {g["script_version"] for g in panel_b["forecast"]["groups"]} == {"s2"}
    # 载荷里没有「合计」：既没有合计行，也没有把所有版本加起来的字段
    assert len(panel["forecast"]["groups"]) == 1
    assert not any(g["script_version"] in ("合计", "total", "") for g in
                   panel["forecast"]["groups"])
    assert "不许相加" in panel["forecast"]["per_version_rule"]
    assert "total" not in json.dumps(panel["forecast"], ensure_ascii=False).lower()


def test_t6_each_account_is_its_own_row(tmp_path):
    """同一版脚本跑在两个账户上也是**两行**（D-26/D-35：账户 = 策略版本的身份）。"""
    path, days = _scoring_db(tmp_path, n_sessions=3)
    c = _conn(path)
    try:
        row = m2_store.list_forecasts(c)[0]
        m2_store.insert_forecast(
            c, plugin_id=row["plugin_id"], channel="A", account_id="arm-agent-v2",
            asof=row["asof_date"], code=STOCK,
            payload=_forecast_payload({"up": 0.6, "flat": 0.2, "down": 0.2}),
            script_id=1, script_version=row["script_version"],
            input_sha256="f" * 64, now=NOW)
        c.commit()
        m2_score.score_all(c, asof=days[-1], now=NOW)
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    keys = {(g["plugin_id"], g["script_version"], g["account_id"])
            for g in panel["forecast"]["groups"]}
    assert keys == {("m2_a3", "s1", AI_ACCOUNT), ("m2_a3", "s1", "arm-agent-v2")}


def test_t6_a3_and_b1_are_never_merged(tmp_path):
    """`m2_a3`（AI）与 `m2_b1`（人工镜像）各一行 —— 两条通路的读数不许并起来。"""
    path, days = _scoring_db(tmp_path, n_sessions=3, plugin_id="m2_a3")
    c = _conn(path)
    try:
        row = m2_store.list_forecasts(c)[0]
        m2_store.insert_forecast(
            c, plugin_id="m2_b1", channel="B", account_id=MIRROR,
            asof=row["asof_date"], code=STOCK,
            payload=_forecast_payload({"up": 0.6, "flat": 0.2, "down": 0.2}),
            script_id=2, script_version="b1", input_sha256="e" * 64, now=NOW)
        c.commit()
        m2_score.score_all(c, asof=days[-1], now=NOW)
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    assert {g["plugin_id"] for g in panel["forecast"]["groups"]} == {"m2_a3", "m2_b1"}


# ══════════════════════════════════════════════════════════════════════
# T7 —— 错判案例纪律
# ══════════════════════════════════════════════════════════════════════


def test_t7_the_case_set_has_empty_attribution_and_no_filters(tmp_path):
    """归因恒空（D-31）、显示总样本量与条数上限、**没有**筛选参数。"""
    path, days = _scoring_db(tmp_path, n_sessions=5, closes=[100.0, 101.0, 100.5,
                                                             101.5, 100.2, 101.0],
                             direction={"up": 0.6, "flat": 0.2, "down": 0.2})
    c = _conn(path)
    try:
        cases = m2_data.miss_cases(c, days[-1])
        html = m2_render.m2_page(m2_data.panel(c, days[-1]), base="/lab",
                                 built_at=NOW)
    finally:
        c.close()
    assert cases["n_miss_total"] >= 1
    assert cases["limit"] == m2_config.CASE_LIMIT
    assert cases["n_scored"] == 5
    for case in cases["cases"]:
        assert case["attribution"] is None, "归因被自动填充了（D-31 明令禁止）"
        assert case["input_sha256"] and case["predicted_class"] != case["actual_class"]
    # 案例集**没有**任何筛选入参：签名只有 (conn, asof)
    import inspect
    params = set(inspect.signature(m2_data.miss_cases).parameters)
    assert params == {"conn", "asof"}
    assert "归因恒空" in html


def test_t7_the_case_set_is_ordered_by_target_date_descending_with_a_cap(tmp_path):
    """条数上限生效时取的是**最近**的 N 条（按目标日倒序），不是挑出来的。"""
    path, days = _scoring_db(tmp_path, n_sessions=5, closes=[100.0, 99.0, 98.0,
                                                             97.0, 96.0, 95.0],
                             direction={"up": 0.6, "flat": 0.2, "down": 0.2})
    c = _conn(path)
    try:
        cases = m2_data.miss_cases(c, days[-1])
    finally:
        c.close()
    targets = [case["target_date"] for case in cases["cases"]]
    assert targets == sorted(targets, reverse=True)
    assert cases["n_miss_total"] == 5 and cases["n_cases"] == 5


def test_t7_an_empty_case_set_says_why(tmp_path):
    """没有错判样本 ⇒ 写清为什么（而不是一张看起来「全对」的空表）。"""
    path, days = _scoring_db(tmp_path, n_sessions=3, closes=[100.0, 101.0, 102.0,
                                                             103.0])
    c = _conn(path)
    try:
        cases = m2_data.miss_cases(c, days[-1])
    finally:
        c.close()
    assert cases["cases"] == [] and cases["n_miss_total"] == 0
    assert cases["empty_reason"]


# ══════════════════════════════════════════════════════════════════════
# 中证500（D-43）：拿不到就明文写清代价
# ══════════════════════════════════════════════════════════════════════


def test_deferred_benchmark_is_named_with_its_cost_when_absent(tmp_path):
    """库里没有 `sh000905` ⇒ 明文写「本轮只对沪深300」+ 缺口与前置条件。"""
    path, days = _paper_db(tmp_path)
    c = _conn(path)
    try:
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    item = next(d for d in panel["three_way"]["deferred"]
                if d["code"] == "sh000905")
    assert item["present_in_db"] is False
    assert panel["three_way"]["scope"].startswith("本轮基准只对")
    html = m2_render.three_way_block(panel["three_way"])
    assert "中证500" in html
    assert "没有点位就没有读数" in html and "不许拿别的指数/ETF 顶替" in html
    assert item["prerequisite"]


def test_deferred_benchmark_present_in_db_is_a_loud_todo(tmp_path):
    """一旦 `sh000905` 落进 `bars_daily`，那句话就变成**显式待办**（不许沉默）。"""
    path, days = _paper_db(tmp_path)
    c = _conn(path)
    try:
        c.execute("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES ('sh000905', ?, 5000,5000,5000,5000, 100,'none','x',?)",
                  (days[0], NOW))
        c.commit()
        panel = m2_data.panel(c, days[-1])
    finally:
        c.close()
    blocked = panel["three_way"]["deferred_blocked"]
    assert [d["code"] for d in blocked] == ["sh000905"]
    html = m2_render.three_way_block(panel["three_way"])
    assert "已经落进" in html and "尚未把它接入" in html
    # 即使它入库了，也**不许**悄悄把它当成第二个基准行（那要显式接入）
    assert {s["account_id"] for s in panel["three_way"]["sides"]} == {
        AI_ACCOUNT, MIRROR, INDEX_300_SYMBOL}


# ══════════════════════════════════════════════════════════════════════
# 页面 / CLI（同源、只读）
# ══════════════════════════════════════════════════════════════════════


def test_page_takes_its_numbers_from_the_data_function(tmp_path):
    """`/lab/m2` 拿到的就是 `m2_data.panel` 的那一份 —— 页面不自己算一遍。"""
    from stocklab.labweb.data import Lab

    path, days = _paper_db(tmp_path)
    c = _conn(path)
    try:
        direct = m2_data.panel(c, days[-1])
    finally:
        c.close()
    via_page = Lab(path, asof=days[-1]).m2_panel()
    assert via_page == direct, "页面取数与 panel() 不同源 —— 两处必然走样"
    html = m2_render.m2_page(via_page, base="/lab", built_at=NOW)
    for label in ("三方对标", "预测校验", "错判案例集"):
        assert label in html
    assert 'href="/lab/m2"' in html            # 导航里有「模块2」
    assert "arm-agent" in html


def test_the_page_has_no_write_entry_point(tmp_path):
    """只读：`POST /lab/m2` 不是一条路由，页面里也没有表单 / 触发按钮。"""
    from stocklab.labweb.app import Context, handle
    from stocklab.labweb.data import Lab
    from stocklab.labweb.tokens import TokenSigner, new_secret

    path, days = _paper_db(tmp_path)
    ctx = Context(lab=Lab(path, asof=days[-1]), signer=TokenSigner(new_secret()),
                  base_path="/lab")
    resp = handle("POST", "/lab/m2", body=b"", ctx=ctx)
    assert resp.status == 404
    c = _conn(path)
    try:
        html = m2_render.m2_page(m2_data.panel(c, days[-1]), base="/lab",
                                 built_at=NOW)
    finally:
        c.close()
    assert "<form" not in html and "method=\"post\"" not in html


def test_cli_report_is_the_same_payload_as_the_page(tmp_path, capsys):
    from stocklab.config import paths

    path, days = _paper_db(tmp_path)
    fake = tmp_path / "REAL-DB-MUST-NOT-BE-TOUCHED.db"
    fake.write_bytes(b"")
    code = main(["m2", "report", "--db", str(path), "--asof", days[-1], "--json"])
    out, err = capsys.readouterr()
    assert code == 0, err
    payload = json.loads(out)
    c = _conn(path)
    try:
        assert payload == m2_data.panel(c, days[-1])
    finally:
        c.close()
    assert fake.read_bytes() == b""           # ERROR_DIARY #55：真库不许被碰
    assert paths  # 保留 import 供将来的 #55 检查复用


def test_cli_score_writes_the_scores_and_exits_zero(tmp_path, capsys):
    path, days = _scoring_db(tmp_path, n_sessions=4, scores=False)
    code = main(["m2", "score", "--db", str(path), "--asof", days[-1], "--now", NOW])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert json.loads(out)["scored"] == 4
    c = _conn(path)
    try:
        assert len(m2_store.list_scores(c)) == 4
    finally:
        c.close()
