"""P79 / D2：喂给 AI 的 `tradability` 块（一手契约进上下文）。

本站要修的第一件事是**接口**而不是模型：`601398` 一手约 ¥813、`600519` 一手 ¥14 万+，
两万本金的账户上「目标权重 < 1.6%」就等于「不许买」。这条约束**必须写在输入侧**
（P79 §0.3），否则每次换模型/换提示词都要重新踩一遍 —— 2026-09-23 的
`arm-agent-ds-v1` 四笔买入全部因「目标市值 < 一手」被判 `hold` 就是这么来的。

判据（逐条对应任务书 T1）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | `one_lot_cost` 与 `rules._fee_parts` 逐位一致 | 自己抄一份费用公式（两边慢慢漂） |
| ② | `min_weight_pct × total_assets / 100 ≈ one_lot_cost` | 权重与金额各算一份 |
| ③ | 块进/不进、块内改一个价 ⇒ 指纹变（且**同输入在 P79 前后不同**） | 加了字段却没进指纹 |
| ④ | 池外 / 无价标的不出现在 `by_code` | 把不可投的标的也报进门槛里 |

数字一律**从被测模块取**（`rules.one_lot_cost` / `rules._fee_parts` /
`CostModel`），不手抄一份到测试里。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from stocklab.config.costs import ASSET_ETF, ASSET_STOCK, CostModel
from stocklab.paper import agent_context, engine
from stocklab.paper import rules as paper_rules
from stocklab.paper.config import LOT, PAPER_START_DATE
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.portfolio.prices import Price
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
ARM = "arm-agent"

CAL = ("2026-09-11", "2026-09-14", START)
BARS = {
    "000333": {"2026-09-14": 86.80, START: 87.23},
    "510300": {START: 4.523},
    "510880": {START: 3.382},
    "600519": {START: 1251.24},     # 一手 ¥12.5 万+ —— 「买不起」的那一端
    "sh000300": {START: 4450.04},
}
POOL = ("000333", "510300", "510880", "600519")
#: `instruments.type`：**口径的唯一真相**（本站不自己判 ETF/股票）。
ASSET = {"000333": ASSET_STOCK, "510300": ASSET_ETF, "510880": ASSET_ETF,
         "600519": ASSET_STOCK}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p79-context.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sh','main',?,?)",
                  [(code, code, ASSET[code], NOW) for code in POOL])
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
    c.commit()
    c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
              " created_at) VALUES (?,'light','{}',?)", (START, NOW))
    sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
    for code in POOL:
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                  " adj_score, reason, risk_json, status, entered_at)"
                  " VALUES (?,?, 'short', 1,1,'夹具','{}','观察中',?)",
                  (sid, code, NOW))
    c.commit()
    engine.init_accounts(c, start_date=START, now=NOW)
    c.close()
    return path


def _marks(**prices) -> dict:
    return {code: Price(code=code, price=price, source="bars",
                        price_asof=START, detail="夹具")
            for code, price in prices.items()}


def _context(db) -> dict:
    c = connect(db)
    try:
        return engine.decision_context_for(c, arm=ARM, asof=START)
    finally:
        c.close()


# ---------- ① one_lot_cost 复用既有费用公式 ----------

@pytest.mark.parametrize("price,asset_class", [
    (8.13, ASSET_STOCK), (4.523, ASSET_ETF), (87.23, ASSET_STOCK),
    (1251.24, ASSET_STOCK), (3.382, ASSET_ETF)])
def test_t1_one_lot_cost_is_the_existing_fee_formula(price, asset_class):
    """①：`one_lot_cost` ≡ `fill_price("buy") × lot + _fee_parts(...)["total"]`。

    手工按 D2 的公式展开算一遍对拍 —— 「一行费用公式都不许另写」这句话，
    只能靠「两边算出来的是同一个数」兑现。
    """
    costs = CostModel(asset_class=asset_class)
    fill = costs.fill_price("buy", price)
    manual = round(fill * LOT + paper_rules._fee_parts(
        costs, "buy", fill, LOT, ref=price)["total"], 4)
    assert paper_rules.one_lot_cost(price=price, asset_class=asset_class) == manual
    # 含费 ⇒ 严格 ≥ `fill × lot`：执行层判「不足一手」用的是后者，
    # 所以「目标 ≥ one_lot_cost」推得出执行层必定成交 ≥1 手（门槛只会更严）。
    assert manual >= round(fill * LOT, 4)


def test_t1_one_lot_cost_rejects_an_unknown_asset_class():
    """口径未知 ⇒ 抛 `ValueError`，**不静默退化成股票费率**（ADR-008 的纪律）。"""
    with pytest.raises(ValueError):
        paper_rules.one_lot_cost(price=1.0, asset_class="warrant")


# ---------- ② min_weight_pct 与 one_lot_cost 是同一个数 ----------

def test_t1_min_weight_pct_is_the_same_number_as_one_lot_cost():
    """②：`min_weight_pct × total_assets / 100 ≈ one_lot_cost`，**逐只**。

    上下文里同时给出「一手多少钱」与「一手占几个点」；两个数若各算一份，
    迟早有一天它们互相矛盾，而矛盾是静默的（AI 会照其中一个下注）。
    """
    block = agent_context._tradability_block(
        prices=_marks(**{"000333": 87.23, "510300": 4.523, "600519": 1251.24}),
        total_assets=20037.91,
        # P80 / D6：`cash` 与 `net_deposits` 是新增的必填口径（购买力与净收益的
        # 分母）。本用例只测 `min_weight_pct` 与 `one_lot_cost` 同源，
        # 故给夹具的起跑口径（现金 11314.91 = 20000 − 8680 − 5.09）。
        cash=11314.91, net_deposits=20000.0,
        asset_classes={"000333": ASSET_STOCK, "510300": ASSET_ETF,
                       "600519": ASSET_STOCK})
    assert block["lot"] == LOT
    for code, row in block["by_code"].items():
        want = paper_rules.one_lot_cost(
            price=float(_marks(**{"000333": 87.23, "510300": 4.523,
                                  "600519": 1251.24})[code].price),
            asset_class=ASSET[code])
        assert row["one_lot_cost"] == want, code
        assert row["min_weight_pct"] == round(want / 20037.91 * 100.0, 4), code
    # 顶层 = 各只里的**最小值**（「最小的一笔可成交建仓占总资产几个点」）。
    assert block["min_weight_pct"] == min(
        row["min_weight_pct"] for row in block["by_code"].values())
    # 600519 一手 ¥12.5 万+ ⇒ 2 万本金的账户上「买得起它」等于 100% 仓位。
    assert block["by_code"]["600519"]["min_weight_pct"] > 100.0


def test_t1_zero_total_assets_gives_none_not_a_guessed_number():
    """总资产算不出（≤0）⇒ 逐只与顶层都是 `None` —— 不猜一个数。"""
    block = agent_context._tradability_block(
        prices=_marks(**{"510300": 4.523}), total_assets=0.0,
        cash=0.0, net_deposits=20000.0,
        asset_classes={"510300": ASSET_ETF})
    assert block["by_code"]["510300"]["one_lot_cost"] > 0
    assert block["by_code"]["510300"]["min_weight_pct"] is None
    assert block["min_weight_pct"] is None


# ---------- ③ 指纹 ----------

#: P79 **之前**的 `DECISION_HASHED_KEYS`（冻结副本）—— 用来证明「同一输入、
#: 加了 `tradability` 之后指纹不同」。与 `_p68_v2_payload` 同款：反向对照必须是
#: **改动前实现的一字不改副本**，不能调用现役实现（那是同义反复）。
PRE_P79_HASHED_KEYS: tuple[str, ...] = (
    "arm", "asof", "account", "marks", "index_300", "pool", "guardrails",
    "disclosure", "non_goals", "counter_arm",
)


def _pre_p79_sha256(context: dict) -> str:
    payload = {k: context[k] for k in PRE_P79_HASHED_KEYS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_t1_the_block_enters_the_fingerprint(db):
    """③ 红-绿：**块进了指纹**，且**同一输入在 P79 前后指纹不同**。

    - 绿：`decision_context_sha256(ctx)` 正常算出；
    - 红：把块摘掉 ⇒ `KeyError`（指纹覆盖的键缺一个就报错，不静默跳过）；
    - 块内改一个价 ⇒ 指纹变（证明它真的被**读进去**了，不是摆设）；
    - 同一份输入按 P79 之前的键集算 ⇒ 与现在的指纹**不同**。
    """
    ctx = _context(db)
    assert "tradability" in agent_context.DECISION_HASHED_KEYS
    assert "tradability" in ctx
    now_sha = agent_context.decision_context_sha256(ctx)

    without = {k: v for k, v in ctx.items() if k != "tradability"}
    with pytest.raises(KeyError):
        agent_context.decision_context_sha256(without)

    # 同一输入、P79 之前的口径 ⇒ 另一个指纹（这正是「必须通报」的那件事）。
    assert _pre_p79_sha256(ctx) != now_sha

    # 块里少报一只标的（等价于「它的价变了」）⇒ 指纹必须跟着变。
    thinner = dict(ctx)
    block = json.loads(json.dumps(ctx["tradability"]))
    block["by_code"].pop("600519")
    thinner["tradability"] = block
    assert agent_context.decision_context_sha256(thinner) != now_sha


def test_t1_a_price_change_inside_the_block_changes_the_fingerprint(db):
    """③ 的另一半：**价格**变 ⇒ `one_lot_cost` 变 ⇒ 指纹变。

    这是「块不是摆设」的最直接证据：光把键加进 `HASHED_KEYS` 而块里恒为空
    也能过上面那条 —— 这一条把那种实现判红。
    """
    before = _context(db)
    after = dict(before)
    block = json.loads(json.dumps(before["tradability"]))
    block["by_code"]["510300"]["one_lot_cost"] += 1.0
    after["tradability"] = block
    assert agent_context.decision_context_sha256(after) \
        != agent_context.decision_context_sha256(before)


# ---------- ④ 键集 = marks ∩ pool ----------

def test_t1_only_pool_codes_with_a_price_are_reported():
    """④：池外标的（如指数）与无价标的不进 `by_code`。"""
    c_marks = _marks(**{"510300": 4.523, "600519": 1251.24})
    block = agent_context._tradability_block(
        prices=c_marks, total_assets=20037.91,
        cash=11314.91, net_deposits=20000.0,
        asset_classes={"510300": ASSET_ETF, "600519": ASSET_STOCK})
    assert set(block["by_code"]) == {"510300", "600519"}


def test_t1_the_context_reports_exactly_the_pool_and_marks_intersection(db):
    """④（接线面）：真实上下文里的键集 == `pool ∩ marks`。

    账户持有的 `000333` 在池内 ⇒ 报；指数 `sh000300` 不在池内 ⇒ 不报
    （它本来也不是 `pool ∩ marks` 的元素）。决策空间就是 `pool ∩ marks`
    （写入口同时要求两者），门槛只报这一格才不至于报出一批下不了单的票。
    """
    ctx = _context(db)
    pool_codes = {str(c) for c in ctx["pool"]["codes"]}
    marks = set(ctx["marks"])
    assert set(ctx["tradability"]["by_code"]) == pool_codes & marks
    assert INDEX_300_SYMBOL not in ctx["tradability"]["by_code"]
    for code, row in ctx["tradability"]["by_code"].items():
        assert row["one_lot_cost"] > 0, code
        assert row["min_weight_pct"] == round(
            row["one_lot_cost"] / ctx["account"]["total_assets"] * 100.0, 4), code
