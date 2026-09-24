"""P47 / D-35：模块2 双通路与镜像 —— 判据 T1–T7。

| 判据 | 被摘掉后会红的实现 |
|---|---|
| T1 幂等重放：同 `(账户, asof)` 跑两遍 → 成交/净值逐行不变 | 每次调用都下单、或把重放也记一笔 |
| T2 成交语义复用：通路 A 的一笔买入与既有引擎**逐字段相同** ＋ 费用只有一处真源 | 在 `m2/` 里另抄一份 `_fee_parts` |
| T3 镜像复用 `arm-now`：同一份人工流水 ⇒ 与 `paper step` 的净值逐字段相同 | 照 `arm-now` 再写一份镜像净值 |
| T4 资金隔离（D-26）：通路 A 下单不动通路 B，反之亦然 | 两条通路共用现金池 |
| T5 A3/B1 append-only + PIT：`asof` 之后的行不影响输入指纹；UPDATE/DELETE 被拒 | 预测表可改写 / 上下文混进未来行 |
| T6 fail-closed 不写半截账户：A1/A2 越界 ⇒ 该 asof 零写入 + 拒绝原因留事件 | 先写 A1 的成交再校验 A2 |
| T7 缺数据不造数：缺当日 K 线 ⇒ 跳过并留痕，**不许用前值顶** | 用「`<= asof` 的最近一根」推进净值 |

数字一律**从被测模块取**（`CostModel` / `m2/config.py`），不手抄一份到测试里 ——
手抄的那份会与上游漂移，而漂移是静默的。
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from stocklab.m2 import channel_a, channel_b, config as m2_config
from stocklab.m2 import context as m2_context
from stocklab.m2 import store as m2_store
from stocklab.paper import agent_decide, engine as paper_engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import ARM_NOW, PAPER_START_DATE
from stocklab.plugin import contract, lifecycle
from stocklab.plugin import store as plugin_store
from stocklab.portfolio.prices import Price
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

ROOT = Path(__file__).resolve().parents[1]

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE                       # 2026-09-15
DAY2 = "2026-09-16"
CAL = ("2026-09-11", "2026-09-14", START, DAY2)
VERSION = "t1"
ACCOUNT = f"arm-agent-{VERSION}"
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23, "2026-09-16": 87.60},
    "510300": {"2026-09-15": 4.523, "2026-09-16": 4.550},
    "510880": {"2026-09-15": 3.382, "2026-09-16": 3.390},
    "sh000300": {"2026-09-15": 4450.04, "2026-09-16": 4480.27},
}
POOL = ("000333", "510300", "510880")
M2_IDS = ("m2_a1", "m2_a2", "m2_a3", "m2_b1")

# ---------- 夹具插桩（确定性；只做「能跑通」的最小动作） ----------

A1_SOURCE = """
def run(ctx):
    codes = []
    for pool in ("short", "mid", "long"):
        for item in ctx["candidates"].get(pool, []):
            codes.append(item["code"])
    if not codes:
        return {"picks": [], "cash_pct": 100.0, "schema_version": "t1"}
    return {"picks": [{"code": c, "weight_pct": 5.0, "reason": "夹具等权 5%"}
                      for c in sorted(codes)],
            "cash_pct": 100.0 - 5.0 * len(codes), "schema_version": "t1"}
"""

A1_OUT_OF_POOL = """
def run(ctx):
    return {"picks": [{"code": "600519", "weight_pct": 5.0, "reason": "池外"}],
            "cash_pct": 95.0, "schema_version": "t1"}
"""

A1_OVER_100 = """
def run(ctx):
    return {"picks": [{"code": "510300", "weight_pct": 80.0, "reason": "越界"}],
            "cash_pct": 30.0, "schema_version": "t1"}
"""

A2_NO_SELL = """
def run(ctx):
    return {"orders": [], "schema_version": "t1"}
"""

A2_SELL_UNHELD = """
def run(ctx):
    return {"orders": [{"code": "600519", "side": "sell", "reason": "没持有"}],
            "schema_version": "t1"}
"""

A2_SELL_HELD = """
def run(ctx):
    codes = [h["code"] for h in ctx["holdings"]]
    if not codes:
        return {"orders": [], "schema_version": "t1"}
    return {"orders": [{"code": sorted(codes)[0], "side": "sell",
                        "reason": "夹具：清掉第一只"}], "schema_version": "t1"}
"""

FORECAST = """
def run(ctx):
    return {"range_80": [80.0, 95.0],
            "direction": {"up": 0.5, "flat": 0.3, "down": 0.2},
            "invalidate_if": "close < 80.0", "na_reasons": [],
            "schema_version": "t1"}
"""

DEFAULT_SOURCES = {"m2_a1": A1_SOURCE, "m2_a2": A2_NO_SELL,
                   "m2_a3": FORECAST, "m2_b1": FORECAST}


def _install(conn, plugin_id: str, source: str, *, version: str) -> int:
    script_id = plugin_store.insert_script(
        conn, plugin_id=plugin_id, version=version, source_text=source,
        note="夹具", now=NOW)
    lifecycle.record_submit(conn, script_id, actor="test", now=NOW)
    lifecycle.record_sandbox(conn, script_id, passed=True, reason="夹具", now=NOW)
    lifecycle.approve(conn, script_id, actor="test", reason="夹具上线", now=NOW)
    return script_id


def build_db(path: Path, *, sources: dict | None = None) -> Path:
    """一份 `paper init` 过、有当日候选池与四支 m2 插桩的库。"""
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main',?,?)",
                  [(code, code, "stock" if code == "000333" else "etf", NOW)
                   for code in POOL])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, series in BARS.items()
         for d, v in series.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,"
              " '首笔',?)", (NOW,))
    c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
              " created_at) VALUES (?,'light','{}',?)", (START, NOW))
    sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
    for code in POOL:
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool,"
                  " raw_score, adj_score, reason, risk_json, status, entered_at)"
                  " VALUES (?,?,'short',1.0,1.0,'夹具','{}','观察中',?)",
                  (sid, code, NOW))
    c.commit()
    paper_engine.init_accounts(c, start_date=START, now=NOW)
    for plugin_id, source in (sources or DEFAULT_SOURCES).items():
        _install(c, plugin_id, source, version="t1")
    c.close()
    return path


@pytest.fixture
def db(tmp_path):
    return build_db(tmp_path / "m2.db")


@pytest.fixture
def conn(db):
    c = connect(db)
    yield c
    c.close()


def _init_account(conn, version: str = VERSION) -> dict:
    return channel_a.create_account(conn, strategy_version=version, now=NOW)


def _dump(conn) -> dict:
    """两张 append-only 表的全量快照（T1/T4 的逐行比对用）。"""
    out = {}
    for table in ("paper_trades", "paper_nav_daily", "m2_channel_runs",
                  "m2_forecasts"):
        out[table] = [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
    return out


# ══════════════════════════════════════════════════════════════════════
# T1 —— 幂等重放
# ══════════════════════════════════════════════════════════════════════


def test_t1_replay_writes_nothing_the_second_time(conn, db):
    """第二遍**一个字节都不写**（连台账行都不追加）。"""
    _init_account(conn)
    first = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert first["status"] == m2_config.STATUS_RAN
    assert first["n_orders"] > 0, "夹具应当真的下单，否则这条判据空转"
    after_first = _dump(conn)
    trades_on_day = len(paper_store.trades_on(conn, ACCOUNT, DAY2))
    assert trades_on_day == first["n_orders"]

    second = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert second["status"] == m2_config.STATUS_ALREADY
    assert _dump(conn) == after_first, "重放必须逐行不变"
    assert len(paper_store.trades_on(conn, ACCOUNT, DAY2)) == trades_on_day


def test_t1_replay_is_stable_after_a_future_row_lands(conn):
    """重放判据不看「今天几号」，只看台账那一格 —— 隔天补跑同样命中。"""
    _init_account(conn)
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    conn.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                 " adj_mode, source, fetched_at)"
                 " VALUES ('000333','2026-09-20',90,90,90,90,1,'none','x',?)", (NOW,))
    conn.commit()
    assert channel_a.run(conn, asof=DAY2, strategy_version=VERSION,
                         now=NOW)["status"] == m2_config.STATUS_ALREADY


# ══════════════════════════════════════════════════════════════════════
# T2 —— 成交语义复用（不另造费用/整手/滑点）
# ══════════════════════════════════════════════════════════════════════


def test_t2_channel_a_trades_are_field_identical_to_the_existing_engine(conn):
    """同一组信号、同一个开盘状态，分别喂两条路径 ⇒ **逐字段相同**。

    两条路径是**不同调用点**：通路 A 走它自己的主干（`m2/channel_a.py`），
    对照走 P52 的执行函数（`agent_decide.execute_decision`，也是 `paper step`
    对 `arm-agent` 用的那一条）。这条判据防的是「在 m2 里抄一份 `_fee_parts`」。
    """
    _init_account(conn)
    marks = paper_engine.resolve_marks(conn, set(POOL) | {"sh000300"}, DAY2)
    open_state = paper_engine.arm_state_for(conn, ACCOUNT, DAY2)

    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    mine = paper_store.trades_on(conn, ACCOUNT, DAY2)

    items = []
    for code in sorted(POOL):
        price = float(marks[code].price)
        qty = int(open_state["positions"].get(code, 0))
        current = round(price * qty, 4)
        target = round(open_state["total_assets"] * 5.0 / 100.0, 4)
        items.append({
            "code": code, "reason": "夹具等权 5%", "price": price,
            "price_asof": str(marks[code].price_asof),
            "price_source": str(marks[code].source), "current_qty": qty,
            "current_value": current, "target_value": target,
            "side": agent_decide.side_for(target_value=target,
                                          current_value=current) or "buy",
            "target_weight_pct": 5.0,
        })
    _cash, _pos, theirs, _evals = agent_decide.execute_decision(
        conn, arm=ACCOUNT, asof=DAY2,
        decision={"asof": DAY2, "decisions": items, "cash_pct": 100.0 - 5.0 * len(POOL),
                  "rationale": "对照路径", "total_assets": open_state["total_assets"]},
        cash=float(open_state["cash"]), positions=dict(open_state["positions"]),
        marks=marks, total_assets=float(open_state["total_assets"]))

    assert len(mine) == len(theirs) > 0
    for row, decision in zip(mine, theirs):
        assert (row["code"], row["side"], row["qty"]) == \
            (decision.code, decision.action, decision.qty)
        assert row["fill_price"] == decision.fill_price
        assert row["ref_price"] == decision.ref_price
        assert row["fee_total"] == decision.fees["total"]
        assert row["slippage_cost"] == decision.fees["slippage_cost"]
        assert row["price_source"] == decision.price_source
        assert row["price_asof"] == decision.price_asof
        assert row["asset_class"] == decision.asset_class


#: 佣金 / 印花税 / 过户费的**费率字面量**。它们只许出现在 `config/costs.py`。
FEE_LITERALS = ("0.00025", "0.0005", "0.00001")


def test_t2_fee_rates_have_exactly_one_source():
    """全仓扫描：三个费率字面量只出现在 `config/costs.py`。"""
    hits = {}
    for path in sorted((ROOT / "stocklab").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        found = sorted(lit for lit in FEE_LITERALS if lit in text)
        if found:
            hits[str(path.relative_to(ROOT))] = found
    assert list(hits) == ["stocklab/config/costs.py"], (
        f"费率常量出现了第二份真源：{hits} —— 成本口径必须只有一处，"
        f"复制一份就等于给「两边慢慢漂」留门")


def test_t2_the_fee_scan_is_not_satisfied_by_an_empty_scan(tmp_path):
    """反向自检：把费率字面量放进**别的**模块，扫描必须能判红。"""
    bad = tmp_path / "fake_costs.py"
    bad.write_text("RATE = 0.00025\n", encoding="utf-8")
    text = bad.read_text(encoding="utf-8")
    assert sorted(lit for lit in FEE_LITERALS if lit in text) == ["0.00025"]


# ══════════════════════════════════════════════════════════════════════
# T3 —— 镜像复用 `arm-now`（不新建第二套镜像代码）
# ══════════════════════════════════════════════════════════════════════

NAV_COLUMNS = ("account_id", "date", "cash", "positions_json", "market_value",
               "nav", "drawdown", "cum_cost", "cum_return", "net_deposits",
               "index_300_level", "index_300_asof", "created_at")


def _nav_row(conn, account_id, date):
    row = conn.execute("SELECT * FROM paper_nav_daily WHERE account_id = ? AND date = ?",
                       (account_id, date)).fetchone()
    return None if row is None else {k: row[k] for k in NAV_COLUMNS}


def test_t3_mirror_row_equals_the_row_paper_step_writes(tmp_path):
    """同一份人工流水：`paper step` 与通路 B 产出的 `arm-now` 净值行**逐字段相同**。"""
    a = build_db(tmp_path / "a.db")
    b = build_db(tmp_path / "b.db")
    ca, cb = connect(a), connect(b)
    try:
        paper_engine.step(ca, DAY2, now=NOW)             # 既有路径（全账户）
        out = channel_b.run(cb, asof=DAY2, now=NOW)      # 通路 B（只镜像）
        assert out["status"] == m2_config.STATUS_RAN
        assert _nav_row(ca, ARM_NOW, DAY2) == _nav_row(cb, ARM_NOW, DAY2)
        assert _nav_row(cb, ARM_NOW, DAY2) is not None
    finally:
        ca.close()
        cb.close()


def test_t3_channel_b_does_not_recompute_nav_itself():
    """源码扫描：通路 B 的净值只能来自 `engine.step_account`；且 `m2/` 里
    **不许有**自己的净值实现（`mark_to_market` / `drawdown` 的定义）。"""
    source = Path(channel_b.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not ({"mark_to_market", "drawdown"} & defined), (
        "通路 B 自己定义了净值口径 —— D-25 明令镜像不另写实现")
    assert "step_account" in {n.attr for n in ast.walk(tree)
                              if isinstance(n, ast.Attribute)}, \
        "镜像必须经 engine.step_account（与 paper step 同一条实现）"


def test_t3_no_m2_module_writes_paper_tables_itself():
    """成交与净值**只有一个写入口**（`paper/store.py`，ERROR_DIARY #61 的反面）。

    `store.py` 里允许的 INSERT 是**模块2 自己的四张表**（P47 两张 + P48 的
    校验分数表 + P49 的判定建议表）—— 白名单只增，且不许出现任何 `paper_*` 表。
    """
    m2_tables = ("{TABLE_RUNS}", "{TABLE_FORECASTS}", "{TABLE_SCORES}",
                 "{TABLE_JUDGEMENTS}", "{TABLE_ATTRIBUTIONS}")
    for path in sorted((ROOT / "stocklab/m2").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == "store.py":
            inserts = [ln.strip() for ln in text.splitlines()
                       if "INSERT INTO" in ln]
            assert inserts and all(any(t in ln for t in m2_tables)
                                   for ln in inserts), inserts
            assert not any("paper_" in ln for ln in inserts), inserts
            continue
        assert "INSERT INTO" not in text, (
            f"{path.name} 自己拼了 INSERT —— 落库必须经既有写入口")


# ══════════════════════════════════════════════════════════════════════
# T4 —— 资金隔离（D-26：每策略版本一行 + 独立 NAV）
# ══════════════════════════════════════════════════════════════════════


def test_t4_channel_a_does_not_touch_the_mirror_account(conn):
    _init_account(conn)
    before = {k: v for k, v in _dump(conn).items() if k in ("paper_trades",
                                                            "paper_nav_daily")}
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    after = {k: v for k, v in _dump(conn).items() if k in ("paper_trades",
                                                           "paper_nav_daily")}
    other = [r for r in after["paper_nav_daily"] if r[0] == ARM_NOW]
    assert other == [r for r in before["paper_nav_daily"] if r[0] == ARM_NOW], \
        "通路 A 动了镜像账户的净值行 —— 资金隔离破了"
    assert _nav_row(conn, ARM_NOW, DAY2) is None, "通路 A 不该给镜像账户写净值"
    assert not [r for r in after["paper_trades"] if r[1] == ARM_NOW], \
        "通路 A 在镜像账户上下单了"


def test_t4_channel_b_does_not_touch_the_strategy_account(conn):
    _init_account(conn)
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    mine = [r for r in _dump(conn)["paper_nav_daily"] if r[0] == ACCOUNT]
    channel_b.run(conn, asof=DAY2, now=NOW)
    assert [r for r in _dump(conn)["paper_nav_daily"]
            if r[0] == ACCOUNT] == mine, "通路 B 动了通路 A 的账户"


def test_t4_each_strategy_version_is_its_own_account_and_nav(conn):
    """两个策略版本 = 两个账户行 + 两条独立净值曲线（D-26 的粒度本身）。"""
    _init_account(conn, "t1")
    _init_account(conn, "t2")
    assert paper_store.account_exists(conn, "arm-agent-t1")
    assert paper_store.account_exists(conn, "arm-agent-t2")
    channel_a.run(conn, asof=DAY2, strategy_version="t1", now=NOW)
    assert _nav_row(conn, "arm-agent-t1", DAY2) is not None
    assert _nav_row(conn, "arm-agent-t2", DAY2) is None, "另一版本不该跟着落净值"


# ══════════════════════════════════════════════════════════════════════
# T5 —— A3/B1 落库 append-only + PIT
# ══════════════════════════════════════════════════════════════════════


def test_t5_forecasts_land_once_per_holding_with_the_contract_shape(conn):
    _init_account(conn)
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    rows = m2_store.list_forecasts(conn, account_id=ACCOUNT, asof=DAY2)
    positions = json.loads(_nav_row(conn, ACCOUNT, DAY2)["positions_json"])
    assert [r["code"] for r in rows] == sorted(p["code"] for p in positions)
    for row in rows:
        assert row["plugin_id"] == m2_config.PLUGIN_A3
        assert row["range_80"] == [80.0, 95.0]
        assert row["direction"] == {"up": 0.5, "flat": 0.3, "down": 0.2}
        assert row["na_reasons"] == []
        assert row["script_version"] == "t1" and row["script_id"] > 0
        assert len(row["input_sha256"]) == 64


def test_t5_append_only_triggers_reject_update_and_delete(conn):
    _init_account(conn)
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    for sql in ("UPDATE m2_forecasts SET code = 'x'",
                "DELETE FROM m2_forecasts",
                "UPDATE m2_channel_runs SET status = 'skipped'",
                "DELETE FROM m2_channel_runs"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    assert m2_store.list_forecasts(conn, account_id=ACCOUNT, asof=DAY2)


def test_t5_a_second_ran_row_for_the_same_day_is_impossible(conn):
    """部分唯一索引：`ran` 那一格只能被占一次（结构性防线，不是代码检查）。"""
    _init_account(conn)
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        m2_store.insert_run(conn, channel=m2_config.CHANNEL_A, account_id=ACCOUNT,
                            asof=DAY2, status=m2_config.STATUS_RAN, reason="伪造",
                            plugins={}, now=NOW)


def test_t5_forecast_input_fingerprint_is_pit(conn):
    """把 `asof` 之后的行塞进库 → 预测输入指纹**不变**。

    重建的 ctx 用**同一份账户状态**（`arm_state_for` 是纯读的重放），所以这条
    判据只检验一件事：`<= asof` 的过滤有没有漏。漏了的话，补采一天数据就会
    把历史预测的「输入指纹」改掉 —— 而那个指纹是「当时看到了什么」的唯一凭据。
    """
    _init_account(conn)
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)

    def fingerprints() -> dict:
        state = paper_engine.arm_state_for(conn, ACCOUNT, DAY2)
        marks = paper_engine.resolve_marks(
            conn, set(state["positions"]) | set(POOL), DAY2)
        ctx = m2_context.channel_ctx(
            conn, channel=m2_config.CHANNEL_A, account_id=ACCOUNT, asof=DAY2,
            cash=state["cash"], positions=state["positions"], marks=marks,
            total_assets=state["total_assets"],
            cost_prices=m2_context.cost_prices_of(
                channel_a.load_account(conn, ACCOUNT)))
        return {code: m2_context.ctx_sha256(
            {**ctx, "focus": {"code": code, "qty": int(qty)}})
            for code, qty in state["positions"].items()}

    before = fingerprints()
    assert before and all(len(v) == 64 for v in before.values())

    conn.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                 " adj_mode, source, fetched_at)"
                 " VALUES ('000333','2026-09-20',99,99,99,99,1,'none','x',?)", (NOW,))
    conn.commit()
    assert fingerprints() == before, "asof 之后的行改变了预测输入指纹 —— PIT 破了"


def test_t5_probe_ctx_keys_cover_the_real_ctx(conn):
    """P45 的作业：探针 ctx 的键必须覆盖真实 ctx 的键（键名对不上 = 误拒正常脚本）。"""
    _init_account(conn)
    channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    state = paper_engine.arm_state_for(conn, ACCOUNT, DAY2)
    marks = paper_engine.resolve_marks(conn, set(POOL), DAY2)
    real = m2_context.channel_ctx(
        conn, channel=m2_config.CHANNEL_A, account_id=ACCOUNT, asof=DAY2,
        cash=state["cash"], positions=state["positions"], marks=marks,
        total_assets=state["total_assets"],
        cost_prices={}, focus=None)
    missing = sorted(set(real) - set(contract.PROBE_CTX))
    assert missing == [], f"真实 ctx 有而探针 ctx 没有的键：{missing}"
    module2_keys = {"candidates", "holdings", "cash", "total_assets", "channel",
                    "account_id", "focus", "candidates_excluded", "candidate_pool"}
    assert module2_keys <= set(contract.PROBE_CTX)
    assert set(contract.PROBE_CTX["candidates"]) == {"short", "mid", "long"}


# ══════════════════════════════════════════════════════════════════════
# T6 —— fail-closed：不写半截账户
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("source,needle", [
    (A1_OUT_OF_POOL, "600519"),
    (A1_OVER_100, "100"),
])
def test_t6_a1_violations_write_nothing_and_leave_a_reason(conn, source, needle):
    _init_account(conn)
    _install(conn, "m2_a1", source, version="bad")
    before = _dump(conn)
    out = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert out["status"] == m2_config.STATUS_REJECTED
    assert paper_store.trades_on(conn, ACCOUNT, DAY2) == []
    assert _nav_row(conn, ACCOUNT, DAY2) is None
    runs = m2_store.list_runs(conn, channel=m2_config.CHANNEL_A, asof=DAY2)
    assert [r["status"] for r in runs] == [m2_config.STATUS_REJECTED]
    assert needle in runs[0]["reason"]
    assert _dump(conn)["paper_nav_daily"] == before["paper_nav_daily"]


def test_t6_a2_violation_rolls_back_the_a1_fills_already_planned(conn):
    """A2 越界 ⇒ **A1 的成交一起回滚**（整日一个事务）。

    顺序是「A1 成交 → A2 判断」，所以这条判据防的是「A1 的成交已落库、
    A2 才发现越界」那种半截日 —— 它看起来像「这天跑过了」。
    """
    _init_account(conn)
    _install(conn, "m2_a2", A2_SELL_UNHELD, version="bad")
    before = _dump(conn)
    out = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert out["status"] == m2_config.STATUS_REJECTED
    assert "600519" in out["reason"]
    assert paper_store.trades_on(conn, ACCOUNT, DAY2) == [], "A1 的成交没有回滚"
    assert _nav_row(conn, ACCOUNT, DAY2) is None
    assert _dump(conn)["paper_nav_daily"] == before["paper_nav_daily"]


def test_t6_missing_plugin_is_rejected_not_bootstrapped(tmp_path):
    """没有 active 版本 ⇒ 拒绝（**不兜底、不编一个默认策略**）。"""
    c = connect(build_db(tmp_path / "noplugin.db",
                         sources={k: v for k, v in DEFAULT_SOURCES.items()
                                  if k != "m2_a1"}))
    try:
        _init_account(c)
        out = channel_a.run(c, asof=DAY2, strategy_version=VERSION, now=NOW)
        assert "m2_a1" in out["reason"]
        assert paper_store.trades_on(c, ACCOUNT, DAY2) == []
    finally:
        c.close()
    assert out["status"] == m2_config.STATUS_REJECTED


def test_t6_refuses_to_trade_an_account_that_is_not_its_own(conn):
    """指到别人的账户 ⇒ 拒绝（不许在镜像账户或 arm-agent 上下单）。"""
    _init_account(conn)
    assert channel_a.load_account(conn, ACCOUNT)["account_id"] == ACCOUNT
    for other in (ARM_NOW, "arm-hold", "arm-agent"):
        with pytest.raises(m2_config.ChannelReject):
            channel_a.load_account(conn, other)


# ══════════════════════════════════════════════════════════════════════
# T7 —— 缺数据不造数
# ══════════════════════════════════════════════════════════════════════


def test_t7_missing_daily_bar_skips_the_day_and_writes_no_nav(conn):
    """删掉持仓当日的那根 K 线 ⇒ 跳过 + 留痕 + **没有新净值行**（不用前值顶）。"""
    _init_account(conn)
    conn.execute("DELETE FROM bars_daily WHERE code = '000333' AND date = ?", (DAY2,))
    conn.commit()
    before = _dump(conn)["paper_nav_daily"]
    out = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert out["status"] == m2_config.STATUS_SKIPPED
    assert out["skip_code"] == m2_config.SKIP_NO_BARS
    assert _nav_row(conn, ACCOUNT, DAY2) is None
    assert _dump(conn)["paper_nav_daily"] == before
    runs = m2_store.list_runs(conn, channel=m2_config.CHANNEL_A, asof=DAY2)
    assert runs[-1]["status"] == m2_config.STATUS_SKIPPED
    assert "缺当日" in runs[-1]["reason"] and "000333" in runs[-1]["reason"]


def test_t7_the_skip_is_recoverable_once_the_bar_arrives(conn):
    """数据补齐后**同一天可以重跑**（跳过的格子不占位，与 `ran` 不同）。"""
    _init_account(conn)
    conn.execute("DELETE FROM bars_daily WHERE code = '000333' AND date = ?", (DAY2,))
    conn.commit()
    assert channel_a.run(conn, asof=DAY2, strategy_version=VERSION,
                         now=NOW)["status"] == m2_config.STATUS_SKIPPED
    conn.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                 " adj_mode, source, fetched_at)"
                 " VALUES ('000333',?,87.6,87.6,87.6,87.6,1,'none','x',?)", (DAY2, NOW))
    conn.commit()
    assert channel_a.run(conn, asof=DAY2, strategy_version=VERSION,
                         now=NOW)["status"] == m2_config.STATUS_RAN
    assert _nav_row(conn, ACCOUNT, DAY2) is not None


def test_t7_channel_b_skips_a_day_without_a_bar(conn):
    conn.execute("DELETE FROM bars_daily WHERE code = '000333' AND date = ?", (DAY2,))
    conn.commit()
    out = channel_b.run(conn, asof=DAY2, now=NOW)
    assert out["status"] == m2_config.STATUS_SKIPPED
    assert _nav_row(conn, ARM_NOW, DAY2) is None


# ══════════════════════════════════════════════════════════════════════
# 账户命名与「谁落这一天」的认领权
# ══════════════════════════════════════════════════════════════════════


def test_strategy_version_shape_is_rejected_not_sanitized():
    assert m2_config.account_id_for("v1") == "arm-agent-v1"
    for bad in ("V1", " a", "a b", "", "x" * 33, "v/1"):
        with pytest.raises(m2_config.StrategyVersionError):
            m2_config.account_id_for(bad)


def test_paper_step_does_not_claim_the_channel_a_account(conn):
    """`paper step` 让出 `executor` 声明的账户 —— 否则那条策略永远不下单。"""
    _init_account(conn)
    out = paper_engine.step(conn, DAY2, now=NOW)
    assert _nav_row(conn, ARM_NOW, DAY2) is not None
    assert _nav_row(conn, ACCOUNT, DAY2) is None, \
        "paper step 认领了通路 A 的日终（会让 A1/A2 永远不下单）"
    assert not [a for a in out["accounts"] if a["account_id"] == ACCOUNT]
    assert paper_engine.external_executor(
        channel_a.load_account(conn, ACCOUNT)) == m2_config.EXECUTOR_CHANNEL_A


def test_channel_a_refuses_a_day_whose_nav_was_written_by_someone_else(conn):
    """别人先写了这一格 ⇒ 拒绝 + 留痕，**不静默覆盖**。"""
    _init_account(conn)
    state = paper_engine.arm_state_for(conn, ACCOUNT, DAY2)
    paper_store.insert_nav(
        conn, account_id=ACCOUNT, date=DAY2, cash=state["cash"], positions=[],
        market_value=0.0, nav=state["total_assets"], drawdown=0.0, cum_cost=0.0,
        cum_return=0.0, net_deposits=state["net_deposits"],
        index_300_level=None, index_300_asof=None, now=NOW)
    out = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert out["status"] == m2_config.STATUS_REJECTED
    assert out["reject_code"] == "nav_conflict"
    assert paper_store.trades_on(conn, ACCOUNT, DAY2) == []


def test_cost_model_is_not_reconstructed_in_m2():
    """`m2/` 不许自己算费用：`CostModel` 只能从 `config.costs` 拿默认口径。"""
    for path in sorted((ROOT / "stocklab/m2").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else getattr(node.func, "attr", None))
                assert name != "CostModel", \
                    f"{path.name} 直接构造了 CostModel —— 费用口径只许在 paper/ 里用"


# ══════════════════════════════════════════════════════════════════════
# P63 —— 执行层 fail-closed：同一 code 两条 pick ⇒ 具名拒绝（不是 IntegrityError）
# ══════════════════════════════════════════════════════════════════════

#: A1 交下来的选股清单里同一个 code 出现两次（`m2_a1` v1.0.0 的真故障形态）。
#: 两条 18% 合计 36% —— 这**正是不能合并**的那种输入：合并就把一份 36% 单票方案
#: 伪装成合规并真的下出去（任务书 §6 的反目标）。
#: 重复项取池内**未持有**的那一只（`codes[1]`）：夹具账户起跑就握着 `000333`，
#: 对它下 18% 只会是「减仓」而推不出两条同向单；取未持有的才复现「两条一模一样的
#: 买单 ⇒ 撞 append-only 唯一键」。同时它仍在池内，于是「越界引用」那条既有闸门
#: 不会被误触 —— 唯一能拦住它的就是本条新判据。
A1_DUPLICATE_CODE = """
def run(ctx):
    codes = sorted({i["code"] for p in ("short", "mid", "long")
                    for i in ctx["candidates"].get(p, [])})
    if not codes:
        return {"picks": [], "cash_pct": 100.0, "schema_version": "t1"}
    code = codes[1]
    return {"picks": [{"code": code, "weight_pct": 18.0, "reason": "短期池第 1 名"},
                      {"code": code, "weight_pct": 18.0, "reason": "中期池第 2 名"}],
            "cash_pct": 64.0, "schema_version": "t1"}
"""

#: 与 `A1_DUPLICATE_CODE` **逐字相同**，只把第二条换成池内另一只 ⇒ 唯一差别是
#: 「重复 / 不重复」。两条用例互为反例（任务书 T4 的单变量原则）。
A1_TWO_DISTINCT_CODES = A1_DUPLICATE_CODE.replace(
    '{"code": code, "weight_pct": 18.0, "reason": "中期池第 2 名"}',
    '{"code": codes[2], "weight_pct": 18.0, "reason": "中期池第 2 名"}')


def _mark(code: str, price: float):
    return Price(code=code, price=price, source="bars", price_asof=DAY2,
                 detail="夹具")


def test_p63_the_two_a1_fixtures_differ_only_in_the_second_code():
    """反向自检：反例夹具**逐字相同**、只差第二条 pick 的 code。"""
    a = A1_DUPLICATE_CODE.splitlines()
    b = A1_TWO_DISTINCT_CODES.splitlines()
    assert len(a) == len(b)
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert len(diff) == 1, diff
    assert "codes[2]" in b[diff[0]] and '{"code": code' in a[diff[0]]


def test_p63_weights_items_rejects_a_repeated_code_before_building_any_order():
    """单元级：**生成条目之前**就拒 —— 消息点名重复的 code，reject 码沿用 `picks`。"""
    picks = [{"code": "600900", "weight_pct": 18.0, "reason": "中期池第 1 名"},
             {"code": "600900", "weight_pct": 18.0, "reason": "长期池第 2 名"}]
    marks = {"600900": _mark("600900", 10.0)}
    with pytest.raises(m2_config.ChannelReject) as exc:
        channel_a._weights_items(picks, positions={}, marks=marks,
                                 total_assets=100000.0, rationale="夹具")
    assert exc.value.code == "picks"
    assert "600900" in exc.value.reason
    assert "一只 code 一条" in exc.value.reason


def test_p63_a_repeated_code_is_not_rescued_by_merging_the_weights():
    """**反兜底**：36% 的单票方案不许被「合并权重」糊过去（合并 = 静默越单票上限）。"""
    picks = [{"code": "600900", "weight_pct": 18.0, "reason": "中期池第 1 名"},
             {"code": "600900", "weight_pct": 18.0, "reason": "长期池第 2 名"}]
    marks = {"600900": _mark("600900", 10.0)}
    with pytest.raises(m2_config.ChannelReject):
        channel_a._weights_items(picks, positions={}, marks=marks,
                                 total_assets=100000.0, rationale="夹具")


def test_p63_a_duplicate_pick_is_rejected_and_writes_nothing(conn):
    """整链路级：A1 吐重复 code ⇒ 拒绝 + **零写入**（不是 `sqlite3.IntegrityError`）。"""
    _init_account(conn)
    _install(conn, "m2_a1", A1_DUPLICATE_CODE, version="dup")
    before = _dump(conn)
    decisions_before = conn.execute(
        "SELECT COUNT(*) FROM paper_agent_decisions").fetchone()[0]
    out = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert out["status"] == m2_config.STATUS_REJECTED
    assert out["reject_code"] == "picks"
    assert POOL[1] in out["reason"], out["reason"]
    after = _dump(conn)
    assert paper_store.trades_on(conn, ACCOUNT, DAY2) == []
    assert _nav_row(conn, ACCOUNT, DAY2) is None
    for table in ("paper_trades", "paper_nav_daily"):
        assert after[table] == before[table], f"{table} 被写入了"
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_agent_decisions").fetchone()[0] \
        == decisions_before, "拒绝路径写下了决策行"


def test_p63_swapping_the_duplicate_for_a_second_name_turns_the_day_green(conn):
    """反例：**唯一**的差别是「第二条 pick 换成另一只」⇒ 同日同链跑到 `ran`。

    少了这条，「拒绝」有可能来自别的闸门（越界 / 超 100）而新判据空转。
    """
    _init_account(conn)
    _install(conn, "m2_a1", A1_TWO_DISTINCT_CODES, version="ok")
    out = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)
    assert out["status"] == m2_config.STATUS_RAN, out
    assert out["n_orders"] > 0
    assert paper_store.trades_on(conn, ACCOUNT, DAY2)
