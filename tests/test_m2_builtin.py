"""P57：模块2 四支插桩 v1.0.0（A1/A2/A3/B1）—— 判据 T1–T7。

| 判据（任务书 §2） | 用例 |
|---|---|
| T1 静态预检过（`guard.check_source`） | `test_t1_*` |
| T2 契约探针过（`load_script(...)(PROBE_CTX)` 过 `validate_return`） | `test_t2_*` |
| T3 确定性：两遍同 `ctx` ⇒ 逐字节同输出；源码无 random/time/datetime | `test_t3_*` |
| T4 A1 形状与上限（池内引用、合计 100、≤5 只、单票 ≤25%、空池 ⇒ 现金 100） | `test_t4_*` |
| T5 A2 只卖持仓中的（三规则各一例、无成本不卖、越界被拒） | `test_t5_*` |
| T6 A3/B1 形状同源（逻辑逐字相同、缺 K 线给 None + 原因、概率合计 1） | `test_t6_*` |
| T7 端到端（夹具库）：`channel a` 出成交 + A3 行；`channel b` 出 B1 行；`daily` exit 0 | `test_t7_*` |

判据里的数字一律**从被测模块取**（`BUILTIN_PLUGINS` / `m2/config.py` / 退出码常量），
不手抄一份到测试里 —— 手抄的那份会与上游漂移，而漂移是静默的。
"""

from __future__ import annotations

import ast
import json
import math
import re
from pathlib import Path

import pytest

from stocklab.cli.main import main
from stocklab.m2 import config as m2_config
from stocklab.m2 import store as m2_store
from stocklab.m2.builtin import BUILTIN_PLUGINS, a3_forecast, b1_forecast
from stocklab.paper import store as paper_store
from stocklab.plugin import contract, guard, runtime
from stocklab.plugin.contract import PluginContractError
from stocklab.store.db import connect
from tests.test_m2_channels import (DAY2, NOW, POOL, build_db)

ROOT = Path(__file__).resolve().parents[1]
M2_IDS = m2_config.CHANNEL_PLUGINS[m2_config.CHANNEL_A] + m2_config.CHANNEL_PLUGINS[
    m2_config.CHANNEL_B]                      # ("m2_a1","m2_a2","m2_a3","m2_b1")
ACCOUNT = "arm-agent-v1"


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════

def _fn(plugin_id: str):
    """按 `plugin_id` 从 `BUILTIN_PLUGINS` 载入可调用脚本（走真实执行器）。"""
    return runtime.load_script(BUILTIN_PLUGINS[plugin_id], plugin_id=plugin_id)


def _cand(code: str, adj_score: float, reason: str = "池内入选理由"):
    """候选池成员的一项 —— 键照 `m2/context.py::_detail` 的产物。"""
    return {"code": code, "name": code, "asset_type": "stock", "board": "main",
            "sector": "白色家电", "bars": [], "features": {},
            "raw_score": adj_score, "adj_score": adj_score, "pool_reason": reason,
            "status": "观察中", "entered_at": DAY2, "close": 10.0,
            "price_asof": DAY2, "price_source": "bars_daily", "asof": DAY2}


def _a1_ctx(per_pool: dict) -> dict:
    return {"candidates": {k: list(v) for k, v in per_pool.items()},
            "candidates_excluded": {}, "holdings": [], "cash": 100000.0,
            "total_assets": 100000.0, "asof": DAY2, "focus": None}


def _hold(code: str, *, cost, close, pool="short", qty=100):
    return {"code": code, "qty": qty, "cost_price": cost, "close": close,
            "pool": pool, "bars": [], "asof": DAY2}


def _a2_ctx(holdings: list) -> dict:
    return {"holdings": holdings, "candidates": {"short": [], "mid": [], "long": []},
            "cash": 1000.0, "total_assets": 10000.0, "asof": DAY2, "focus": None}


def _bars(closes: list) -> list:
    """由收盘价序列造 K 线（其余字段不参与分位计算，给同值便于读）。"""
    return [{"date": "2026-08-%02d" % (i + 1), "open": c, "high": c, "low": c,
             "close": c, "volume": 1000, "amount": None, "turnover": None}
            for i, c in enumerate(closes)]


def _trend_bars(n: int = 25, *, mu: float = 0.0, sigma: float = 0.02,
                start: float = 100.0) -> list:
    """确定性 K 线：对数收益在 `mu ± sigma` 之间交替 ⇒ 样本 mu/sigma 可控。"""
    closes = [start]
    for i in range(1, n):
        step = mu + (sigma if i % 2 else -sigma)
        closes.append(closes[-1] * math.exp(step))
    return _bars([round(c, 4) for c in closes])


def _a3_ctx(bars: list, *, code: str = "000333") -> dict:
    return {"focus": {"code": code, "qty": 100}, "holdings": [
        {**_hold(code, cost=10.0, close=(bars[-1]["close"] if bars else None)),
         "bars": bars}],
        "candidates": {"short": [], "mid": [], "long": []},
        "asof": DAY2, "cash": 0.0, "total_assets": 0.0}


# ══════════════════════════════════════════════════════════════════════
# T1 —— 静态预检
# ══════════════════════════════════════════════════════════════════════

def test_t1_the_four_ids_are_exactly_the_module2_four():
    assert sorted(BUILTIN_PLUGINS) == sorted(M2_IDS)
    assert "0" not in BUILTIN_PLUGINS, "模块2 的四支**不占** 0–5 编号（D-33）"


@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_t1_passes_the_static_guard(plugin_id):
    guard.check_source(BUILTIN_PLUGINS[plugin_id])


# ══════════════════════════════════════════════════════════════════════
# T2 —— 契约探针（空形状）
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_t2_runs_on_the_probe_ctx_and_satisfies_the_contract(plugin_id):
    out = _fn(plugin_id)(contract.PROBE_CTX)
    assert contract.validate_return(plugin_id, out) == out


def test_t2_a1_on_the_probe_ctx_means_cash_100_not_an_empty_picks_list():
    """探针没有候选 ⇒ 空仓必须由 `cash_pct=100` 表达（`channel_a._weights_items`
    明确拒绝「空清单表达空仓」）。"""
    out = _fn("m2_a1")(contract.PROBE_CTX)
    assert out["picks"] == [] and out["cash_pct"] == 100.0


@pytest.mark.parametrize("plugin_id", ["m2_a3", "m2_b1"])
def test_t2_forecast_on_the_probe_ctx_is_na_with_reasons(plugin_id):
    """探针的 `focus` 是 `None` ⇒ 只能报「不知道」，且必须**逐条说明为什么**。"""
    out = _fn(plugin_id)(contract.PROBE_CTX)
    assert out["range_80"] is None and out["direction"] is None
    assert out["na_reasons"] and all(isinstance(r, str) for r in out["na_reasons"])


# ══════════════════════════════════════════════════════════════════════
# T3 —— 确定性
# ══════════════════════════════════════════════════════════════════════

def _rich_ctx(plugin_id: str) -> dict:
    if plugin_id == "m2_a1":
        return _a1_ctx({"short": [_cand("000333", 9.0), _cand("510300", 8.0)],
                        "mid": [_cand("510880", 7.5)],
                        "long": [_cand("600519", 6.0)]})
    if plugin_id == "m2_a2":
        return _a2_ctx([_hold("000333", cost=10.0, close=8.5),
                        _hold("510300", cost=10.0, close=13.0),
                        _hold("510880", cost=10.0, close=10.2, pool=None)])
    return _a3_ctx(_trend_bars(25, mu=0.001, sigma=0.02))


@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_t3_two_runs_on_the_same_ctx_are_byte_identical(plugin_id):
    fn = _fn(plugin_id)
    ctx = _rich_ctx(plugin_id)
    first = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    second = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    assert first == second


@pytest.mark.parametrize("plugin_id", M2_IDS)
def test_t3_source_has_no_clock_random_or_import(plugin_id):
    """源码里出现 `random` / `time` / `datetime` 一律判红。

    `import` **不在这里按字面扫**：四支的头部注释就写着「不 import」（照抄这条
    纪律的话），字面扫描会误报；它是**结构**判据，由 `guard.check_source`
    （T1）以 AST 拦 `Import` / `ImportFrom` 节点，下面再钉一次。
    """
    source = BUILTIN_PLUGINS[plugin_id]
    for token in ("random", "time", "datetime", "uuid"):
        assert token not in source, f"{plugin_id} 的源文本出现 {token!r}"
    tree = ast.parse(source)
    assert not [n for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom))]


# ══════════════════════════════════════════════════════════════════════
# T4 —— A1 形状与上限
# ══════════════════════════════════════════════════════════════════════

def _allowed_codes(ctx: dict) -> set:
    return {c["code"] for item in ctx["candidates"].values() for c in item}


def test_t4_a1_picks_stay_inside_the_pools_and_weights_sum_to_100():
    ctx = _a1_ctx({"short": [_cand("000333", 9.0), _cand("510300", 8.0)],
                   "mid": [_cand("510880", 7.5)], "long": [_cand("600519", 6.0)]})
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    out = contract.validate_references("m2_a1", out, allowed_codes=_allowed_codes(ctx))
    assert len(out["picks"]) == 4
    assert [p["code"] for p in out["picks"]] == ["000333", "510300", "510880", "600519"]
    assert abs(sum(p["weight_pct"] for p in out["picks"]) + out["cash_pct"] - 100.0) <= 1e-6
    assert out["cash_pct"] >= 10.0


def test_t4_a1_truncates_to_five_and_a_single_pick_never_exceeds_25pct():
    codes = ["000333", "510300", "510880", "600519", "600036", "601398", "601288"]
    ctx = _a1_ctx({"short": [_cand(c, 9.0 - i) for i, c in enumerate(codes)]})
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert len(out["picks"]) == 5, "上限是 5 只（写死在源码里）"
    assert [p["code"] for p in out["picks"]] == codes[:5], "选序按 adj_score 从高到低"
    assert all(p["weight_pct"] <= 25.0 for p in out["picks"])
    assert out["cash_pct"] >= 10.0
    assert abs(sum(p["weight_pct"] for p in out["picks"]) + out["cash_pct"] - 100.0) <= 1e-6


def test_t4_a1_three_picks_hit_the_single_name_cap():
    """3 只时等权会是 30%，被单票上限截到 25% ⇒ 现金 25%（不是 10%）。"""
    ctx = _a1_ctx({"short": [_cand("000333", 9.0), _cand("510300", 8.0),
                             _cand("510880", 7.0)]})
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert [p["weight_pct"] for p in out["picks"]] == [25.0, 25.0, 25.0]
    assert out["cash_pct"] == 25.0


def test_t4_empty_pools_mean_picks_empty_and_cash_100():
    out = contract.validate_return(
        "m2_a1", _fn("m2_a1")(_a1_ctx({"short": [], "mid": [], "long": []})))
    assert out["picks"] == []
    assert out["cash_pct"] == 100.0


def test_t4_reason_says_which_pool_which_rank_and_why():
    ctx = _a1_ctx({"mid": [_cand("510880", 7.5, reason="中期池：ROE 分位高分")]})
    pick = _fn("m2_a1")(ctx)["picks"][0]
    assert "中期" in pick["reason"] and "1" in pick["reason"]
    assert "ROE 分位高分" in pick["reason"]


def test_t4_a_reference_outside_the_pools_is_rejected_not_dropped():
    """负例：越界引用被 `validate_references` 拒（fail-closed 规则 2）。"""
    with pytest.raises(PluginContractError) as exc:
        contract.validate_references(
            "m2_a1",
            {"picks": [{"code": "600519", "weight_pct": 5.0, "reason": "池外"}],
             "cash_pct": 95.0, "schema_version": "1.0.0"},
            allowed_codes={"000333"})
    assert "600519" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════
# T5 —— A2 只卖持仓中的
# ══════════════════════════════════════════════════════════════════════

def _orders(holdings: list) -> list:
    out = contract.validate_return("m2_a2", _fn("m2_a2")(_a2_ctx(holdings)))
    contract.validate_references("m2_a2", out,
                                 allowed_codes={h["code"] for h in holdings})
    return out["orders"]


def test_t5_stop_loss_fires_at_minus_8pct():
    orders = _orders([_hold("000333", cost=10.0, close=9.0)])
    assert [o["code"] for o in orders] == ["000333"]
    assert orders[0]["side"] == "sell" and "止损" in orders[0]["reason"]
    assert "-10.00%" in orders[0]["reason"]


def test_t5_take_profit_fires_at_plus_20pct():
    orders = _orders([_hold("000333", cost=10.0, close=12.5)])
    assert [o["code"] for o in orders] == ["000333"]
    assert "止盈" in orders[0]["reason"] and "+25.00%" in orders[0]["reason"]


def test_t5_exit_fires_when_the_name_left_every_pool():
    orders = _orders([_hold("000333", cost=10.0, close=10.5, pool=None)])
    assert [o["code"] for o in orders] == ["000333"]
    assert "调仓退出" in orders[0]["reason"]


def test_t5_inside_the_band_and_inside_a_pool_means_no_order():
    assert _orders([_hold("000333", cost=10.0, close=10.5, pool="short")]) == []


def test_t5_unknown_cost_never_sells_even_when_it_left_the_pool():
    """「不知道成本」≠「该卖」：账户里没记成本 ⇒ 一条指令都不给。"""
    assert _orders([_hold("000333", cost=None, close=8.0, pool=None)]) == []


def test_t5_unknown_price_never_sells():
    assert _orders([_hold("000333", cost=10.0, close=None, pool=None)]) == []


def test_t5_one_order_at_most_per_holding():
    """同时命中「止损」与「掉出池子」⇒ 只出一条（按 ①→②→③ 的顺序）。"""
    orders = _orders([_hold("000333", cost=10.0, close=8.0, pool=None)])
    assert [o["code"] for o in orders] == ["000333"]
    assert "止损" in orders[0]["reason"]


def test_t5_selling_something_not_held_is_rejected():
    """负例：A2 的允许集合是**持仓**，卖未持有的被拒（不是静默丢弃）。"""
    with pytest.raises(PluginContractError) as exc:
        contract.validate_references(
            "m2_a2",
            {"orders": [{"code": "600519", "side": "sell", "reason": "没持有"}],
             "schema_version": "1.0.0"},
            allowed_codes={"000333"})
    assert "600519" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════
# T6 —— A3 / B1 形状同源
# ══════════════════════════════════════════════════════════════════════

_VERSION_RE = re.compile(r"'[0-9]+\.[0-9]+\.[0-9]+'")


def _strip_version(text: str) -> str:
    """把版本串归一 —— 两份源文本**只许**在这一处不同（D-30 的可比性）。"""
    return _VERSION_RE.sub("'<V>'", text)


def test_t6_the_two_forecast_sources_differ_only_in_the_version_string():
    assert _strip_version(a3_forecast.SOURCE) == _strip_version(b1_forecast.SOURCE)
    assert "'1.0.0'" in a3_forecast.SOURCE and "'1.0.0'" in b1_forecast.SOURCE


def test_t6_the_version_normalizer_is_falsifiable():
    """反向自检：**逻辑**改一个字（不是版本串）时，归一后的比对必须判红。"""
    mutated = a3_forecast.SOURCE.replace("FLAT_BAND = 0.005", "FLAT_BAND = 0.006")
    assert mutated != a3_forecast.SOURCE, "前提：这个替换真的改了文本"
    assert _strip_version(mutated) != _strip_version(b1_forecast.SOURCE)
    bumped = a3_forecast.SOURCE.replace("'1.0.0'", "'9.9.9'")
    assert _strip_version(bumped) == _strip_version(b1_forecast.SOURCE)


@pytest.mark.parametrize("plugin_id", ["m2_a3", "m2_b1"])
def test_t6_a_real_series_gives_a_range_and_normed_probabilities(plugin_id):
    out = contract.validate_return(plugin_id,
                                   _fn(plugin_id)(_a3_ctx(_trend_bars(25))))
    assert out["na_reasons"] == []
    lo, hi = out["range_80"]
    assert lo <= hi
    direction = out["direction"]
    assert abs(sum(direction.values()) - 1.0) <= 1e-6
    assert all(0.0 <= p <= 1.0 for p in direction.values())
    assert isinstance(out["invalidate_if"], str) and out["invalidate_if"].strip()
    assert out["schema_version"] == "1.0.0"


@pytest.mark.parametrize("plugin_id", ["m2_a3", "m2_b1"])
def test_t6_short_history_is_na_with_a_reason_not_a_zero(plugin_id):
    """样本不足 ⇒ `range_80=None` + `na_reasons`，**不许给 0.0 冒充「最差」**。"""
    out = contract.validate_return(plugin_id,
                                   _fn(plugin_id)(_a3_ctx(_trend_bars(5))))
    assert out["range_80"] is None and out["direction"] is None
    assert out["invalidate_if"] is None
    assert out["na_reasons"] and "5" in out["na_reasons"][0]


@pytest.mark.parametrize("plugin_id", ["m2_a3", "m2_b1"])
def test_t6_no_bars_at_all_is_na_with_a_reason(plugin_id):
    out = contract.validate_return(plugin_id, _fn(plugin_id)(_a3_ctx([])))
    assert out["range_80"] is None and out["na_reasons"]


@pytest.mark.parametrize("plugin_id", ["m2_a3", "m2_b1"])
def test_t6_flat_history_is_na_not_a_degenerate_range(plugin_id):
    """恒定收盘价 ⇒ 收益标准差 0 ⇒ **分布退化**，报 None + 原因（不编默认值）。

    与模块1 的 `DegenerateInput` 同一条口径（`predict/model.py` 对 `stdev == 0`
    直接拒绝，而不是给一个宽度为 0 的区间）。
    """
    out = contract.validate_return(plugin_id,
                                   _fn(plugin_id)(_a3_ctx(_bars([10.0] * 25))))
    assert out["range_80"] is None and out["direction"] is None
    assert any("退化" in r for r in out["na_reasons"])


@pytest.mark.parametrize("plugin_id", ["m2_a3", "m2_b1"])
def test_t6_probabilities_stay_in_range_across_a_mu_sigma_grid(plugin_id):
    """扫一遍 (mu, sigma)：三概率恒在 [0,1] 且合计为 1。

    **本用例实捕到过一个真错**：按任务书 §1.5 的字面写法 `p_flat = 1 − p_up − p_down`
    实现时，`mu=0.05 / sigma=0.005`（带子在均值下方 ~9–11 个 sigma）给出
    `{up: 1.0, flat: -6.995e-27, down: 6.995e-27}` —— `1 − (1−ε)` 把尾巴舍没了，
    而契约拒绝任何 `<0` 的概率（`_check_probs`）⇒ 整份载荷被判红。
    现实现按 CDF 差值算 `p_flat = Φ(hi_b) − Φ(lo_b)`（与 `1−p_up−p_down` 代数等价），
    三者之和恒为 1.0、无负值。这条网格就是把那个 (mu, sigma) 钉住的用例。
    """
    fn = _fn(plugin_id)
    for mu in (-0.05, -0.005, 0.0, 0.005, 0.05):
        for sigma in (0.0005, 0.005, 0.02, 0.1):
            out = contract.validate_return(
                plugin_id,
                fn(_a3_ctx(_trend_bars(25, mu=mu, sigma=sigma))))
            for name, p in out["direction"].items():
                assert 0.0 <= p <= 1.0, f"mu={mu} sigma={sigma} {name}={p}"
            assert abs(sum(out["direction"].values()) - 1.0) <= 1e-6


# ══════════════════════════════════════════════════════════════════════
# T7 —— 端到端（夹具库）
# ══════════════════════════════════════════════════════════════════════

def _e2e_db(path: Path) -> Path:
    """夹具库 + **真源文本**四支已上线 + 25 根历史 K 线（让预测算得出区间）。

    `build_db` 用的是 `m2/context.py` 定稿的那份 ctx 形状（候选池一项一份
    `build_ctx` 产物 / 持仓带成本与池），所以这里直接用它，不另搭一套 ——
    两套夹具会让「端到端过的是哪一份形状」变成说不清的事。
    """
    build_db(path, sources=BUILTIN_PLUGINS)
    c = connect(path)
    rows = []
    for code, close in (("000333", 80.0), ("510300", 4.0), ("510880", 3.0)):
        for i in range(25):
            close = round(close * (1.004 if i % 3 else 0.998), 4)
            rows.append((code, "2026-08-%02d" % (i + 1), close, NOW))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, d, v, _ in rows])
    c.commit()
    c.close()
    return path


def _run(argv: list, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def _daily(db: Path, capsys, asof: str = DAY2):
    return _run(["m2", "daily", "--asof", asof, "--db", str(db), "--now", NOW], capsys)


@pytest.fixture
def e2e(tmp_path):
    return _e2e_db(tmp_path / "e2e.db")


def test_t7_channel_a_produces_fills_and_a3_forecast_rows(e2e, capsys):
    code, _out, _err = _run(["m2", "account", "init", "--strategy-version", "v1",
                             "--db", str(e2e), "--now", NOW], capsys)
    assert code == 0
    code, payload, err = _run(
        ["m2", "channel", "a", "--asof", DAY2, "--strategy-version", "v1",
         "--db", str(e2e), "--now", NOW], capsys)
    assert code == 0, err
    assert payload["status"] == m2_config.STATUS_RAN
    assert payload["n_orders"] > 0, "夹具应当真的下单（否则这条判据空转）"
    conn = connect(e2e)
    try:
        assert paper_store.trades_on(conn, ACCOUNT, DAY2)
        rows = m2_store.list_forecasts(conn, account_id=ACCOUNT, asof=DAY2)
        positions = json.loads(conn.execute(
            "SELECT positions_json FROM paper_nav_daily WHERE account_id = ?"
            " AND date = ?", (ACCOUNT, DAY2)).fetchone()["positions_json"])
        assert rows and all(r["plugin_id"] == m2_config.PLUGIN_A3 for r in rows)
        assert {r["code"] for r in rows} == {p["code"] for p in positions}
        assert all(r["script_version"] == "t1" for r in rows)
        assert any(r["range_80"] is not None for r in rows), \
            "补了 25 根 K 线，预测应当真的算得出区间（否则 T6 的绿路没被端到端走到）"
    finally:
        conn.close()


def test_t7_channel_b_produces_b1_forecast_rows(e2e, capsys):
    code, payload, err = _run(["m2", "channel", "b", "--asof", DAY2,
                               "--db", str(e2e), "--now", NOW], capsys)
    assert code == 0, err
    assert payload["status"] == m2_config.STATUS_RAN
    assert payload["n_forecasts"] > 0
    conn = connect(e2e)
    try:
        rows = m2_store.list_forecasts(conn, asof=DAY2)
        assert [r for r in rows if r["plugin_id"] == m2_config.PLUGIN_B1]
    finally:
        conn.close()


def test_t7_daily_is_green_end_to_end(e2e, capsys):
    """`m2 daily` exit 0 —— 这一条正是真库现在（缺四支）拿不到的绿路径。"""
    code, _out, _err = _run(["m2", "account", "init", "--strategy-version", "v1",
                             "--db", str(e2e), "--now", NOW], capsys)
    assert code == 0
    code, payload, err = _daily(e2e, capsys)
    assert code == 0, err
    assert payload["status"] == m2_config.STATUS_RAN
    assert payload["n_rejected"] == 0
    assert [s["status"] for s in payload["steps"]] == [
        m2_config.STATUS_RAN] * len(payload["steps"])
    conn = connect(e2e)
    try:
        assert m2_store.list_forecasts(conn, plugin_id=m2_config.PLUGIN_B1, asof=DAY2)
        assert m2_store.list_forecasts(conn, plugin_id=m2_config.PLUGIN_A3, asof=DAY2)
    finally:
        conn.close()


def test_t7_daily_replays_green_when_the_day_already_ran(e2e, capsys):
    """跑过一天后再跑：全部 `already`（幂等），**仍然 exit 0**。"""
    _run(["m2", "account", "init", "--strategy-version", "v1",
          "--db", str(e2e), "--now", NOW], capsys)
    assert _daily(e2e, capsys)[0] == 0
    code, payload, _err = _daily(e2e, capsys)
    assert code == 0
    assert payload["n_skipped"] == 0
    assert all(s["status"] in (m2_config.STATUS_ALREADY, m2_config.STATUS_RAN)
               for s in payload["steps"])


# ══════════════════════════════════════════════════════════════════════
# P63 —— A1 跨池去重（同一 code 至多一条）
# ══════════════════════════════════════════════════════════════════════

#: 真库 `agent_pool.pool_snapshot(conn, "2026-09-23")` 的池形态：long 5 ⊂ mid 8、
#: short 6 ⇒ 19 个槽 / 11 只。分数用**真库那 5 行**的原值，其余给互不相同、
#: 都更低的分数 —— 于是「重复项恰在前 5 行里」这件事被真形态复现出来。
_P63_LONG = {"600519": 79.1346, "600900": 72.8846}
_P63_MID = {"600900": 73.4135, "600519": 71.4663}
_P63_SHORT = {"603868": 77.1352}


def _p63_ctx() -> dict:
    long_codes = ["600519", "600900", "002415", "601318", "603868"]
    mid_codes = ["600900", "600519", "000651", "002032", "600036", "002415",
                 "601318", "603868"]                 # long ⊂ mid
    short_codes = ["603868", "000333", "002032", "002508", "600036", "601398"]
    long_bg = [c for c in long_codes if c not in _P63_LONG]
    mid_bg = [c for c in mid_codes if c not in _P63_MID]
    short_bg = [c for c in short_codes if c not in _P63_SHORT]
    return _a1_ctx({
        "long": [_cand(c, s) for c, s in _P63_LONG.items()]
                + [_cand(c, 70.0 - i) for i, c in enumerate(long_bg)],
        "mid": [_cand(c, s) for c, s in _P63_MID.items()]
               + [_cand(c, 67.0 - i) for i, c in enumerate(mid_bg)],
        "short": [_cand(c, s) for c, s in _P63_SHORT.items()]
                 + [_cand(c, 61.0 - i) for i, c in enumerate(short_bg)],
    })


def test_p63_the_fixture_really_carries_a_cross_pool_duplicate():
    """反向自检：夹具里**确实**有同一个 code 占两个槽（否则下面三条空转）。"""
    ctx = _p63_ctx()
    slots = [c["code"] for items in ctx["candidates"].values() for c in items]
    assert len(slots) == 19 and len(set(slots)) == 11, "池形态不是真库那份"
    duplicated = sorted({c for c in slots if slots.count(c) > 1})
    assert duplicated == ["002032", "002415", "600036", "600519", "600900", "601318",
                          "603868"], duplicated
    assert {c["code"] for c in ctx["candidates"]["long"]} \
        <= {c["code"] for c in ctx["candidates"]["mid"]}, "前提：long ⊂ mid"

def test_p63_a1_keeps_one_pick_per_code():
    out = contract.validate_return("m2_a1", _fn("m2_a1")(_p63_ctx()))
    codes = [p["code"] for p in out["picks"]]
    assert len(codes) == len(set(codes)), f"选股清单里出现了重复 code：{codes}"
    assert codes == ["600519", "603868", "600900", "002415", "601318"], codes
    assert [p["weight_pct"] for p in out["picks"]] == [18.0] * 5
    assert out["cash_pct"] == 10.0
    assert abs(sum(p["weight_pct"] for p in out["picks"]) + out["cash_pct"] - 100.0) \
        <= 1e-6


def test_p63_the_dedup_happens_before_the_limit_truncation():
    """**判据本身**：先去重再取前 N。

    若写成「先取前 N 再去重」，原始前 5 行是
    `600519, 603868, 600900, 600900, 600519` ⇒ 只剩 3 条，等权 30% 被单票上限
    截到 25%（现金 25%）。所以「5 条 / 18% / 现金 10%」这组数字只在
    「先去重再截断」下成立 —— 它把两种实现区分开，不是同义反复。
    """
    out = contract.validate_return("m2_a1", _fn("m2_a1")(_p63_ctx()))
    assert len(out["picks"]) == 5, "去重后仍有 11 只可选，前 5 条应当取满"
    assert [p["weight_pct"] for p in out["picks"]] == [18.0] * 5, \
        "3 条时才该出现 25% 上限；这里 18% 说明去重发生在截断之前"
    assert out["cash_pct"] == 10.0
    # 同一 code 只保留它**排名最好的那只池**（600519 是长期池第 1 名，不是中期第 2 名）
    top = out["picks"][0]
    assert top["code"] == "600519" and "长期池第 1 名" in top["reason"], top["reason"]


def test_p63_a1_dedup_is_byte_identical_across_two_runs():
    """重复 code 的 ctx 上也必须两遍逐字节相同（排序键仍是全序）。"""
    fn = _fn("m2_a1")
    ctx = _p63_ctx()
    first = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    second = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    assert first == second


def test_p63_dedup_can_leave_fewer_than_five_names_and_then_the_cap_bites():
    """去重后不足 5 只 ⇒ 条数如实变少、单票被 25% 上限截断（不补足、不凑数）。"""
    dup = _a1_ctx({"short": [_cand("000333", 9.0), _cand("510300", 8.0)],
                   "mid": [_cand("000333", 7.0), _cand("510300", 6.0)]})
    out = contract.validate_return("m2_a1", _fn("m2_a1")(dup))
    assert [p["code"] for p in out["picks"]] == ["000333", "510300"]
    assert [p["weight_pct"] for p in out["picks"]] == [25.0, 25.0]
    assert out["cash_pct"] == 50.0


# ══════════════════════════════════════════════════════════════════════
# P64 —— A1 排除「按目标权重连一手都买不起」的标的（源版本 1.0.2）
#
# 真库读数（P63 T6，`asof=2026-09-23`，`total_assets ≈ 19,547`）：v1.0.1 选了
# 5 只各 18%，其中茅台（一手 ¥125,124）与 601318（一手 ¥5,387）的目标市值
# ¥3,518.51 连一手都不够 ⇒ 0 股、无订单 ⇒ 成交 3 笔、现金 ~40%。
# 判据里的数字一律取自这份真库读数，不另编一套。
# ══════════════════════════════════════════════════════════════════════

#: 真库 2026-09-23 的账户总资产（P63 §1 的读数）。
_P64_TOTAL = 19547.0


def _cand_at(code: str, adj_score: float, close: float) -> dict:
    """带指定 PIT 收盘价的候选池成员（`close` 是 `candidates_ctx` 的成品字段）。"""
    item = _cand(code, adj_score)
    item["close"] = close
    return item


def _a1_ctx_at(per_pool: dict, total_assets) -> dict:
    """照 `_a1_ctx`，但总资产可给 `None`（= 账户数据未知，探针形状）。"""
    ctx = _a1_ctx(per_pool)
    ctx["total_assets"] = total_assets
    return ctx


def test_p64_t1_lot_is_100_and_points_at_the_account_side_convention():
    """T1：`LOT` 写死在源文本里，且注释点名它与账户参数是同一条约定。"""
    from stocklab.m2.builtin import a1_pick
    assert a1_pick.LOT == 100
    source = BUILTIN_PLUGINS["m2_a1"]
    assert re.search(r"^LOT = 100\b", source, re.M), "源文本里没有 LOT = 100"
    assert "params_json.lot" in source, "注释没说清 LOT 与账户参数的耦合"
    guard.check_source(source)          # 静态预检过（T1 判据的后半句）


def test_p64_t2_maotai_is_unaffordable_at_every_weight_up_to_the_cap():
    """T2：`total_assets=19547` / `close=1251.24` ⇒ 任何 `w ≤ 25` 都买不起。

    `n=1` 时权重已经顶到 `CAP_PCT=25`（目标市值 ¥4,886.75），连一手
    ¥125,124 的零头都不够 —— 所以「往下试」试到底也不能选它。
    """
    ctx = _a1_ctx_at({"short": [_cand_at("600519", 9.0, 1251.24)]}, _P64_TOTAL)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert out["picks"] == []
    assert out["cash_pct"] == 100.0
    # 判据自证：不是「恰好没选」，而是**每个** n 的权重都不够一手
    for n in range(1, 6):
        w = min(25.0, (100.0 - 10.0) / n)
        assert _P64_TOTAL * w / 100.0 < 1251.24 * 100, f"n={n} 竟然买得起"


def test_p64_t2_a_mid_priced_name_is_excluded_even_at_the_cap():
    """T2：`close=53.87`（601318）连 `w=25` 都买不起（¥5,387 > ¥4,886.75）。"""
    ctx = _a1_ctx_at({"short": [_cand_at("601318", 9.0, 53.87)]}, _P64_TOTAL)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert out["picks"] == []
    assert out["cash_pct"] == 100.0
    assert _P64_TOTAL * 25.0 / 100.0 < 53.87 * 100, "前提：w=25 也不够一手"


def test_p64_t2_a_cheap_name_is_affordable_at_eighteen_percent():
    """T2：`close=31.26` ⇒ `w=18` 下目标市值 ¥3,518.46 ≥ 一手 ¥3,126，**可买**。

    与上面两条构成对照：同一账户、同一权重，只差一个价格。
    """
    ctx = _a1_ctx_at({"short": [_cand_at("C%d" % i, 9.0 - i, 31.26)
                                for i in range(5)]}, _P64_TOTAL)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert len(out["picks"]) == 5
    assert [p["weight_pct"] for p in out["picks"]] == [18.0] * 5
    assert out["cash_pct"] == 10.0
    assert _P64_TOTAL * 18.0 / 100.0 >= 31.26 * 100, "前提：w=18 买得起一手"


def test_p64_t2_unaffordable_names_are_skipped_and_the_rest_fill_the_list():
    """真库那份 5 只的镜像：贵的被跳过，剩下的按排序补齐（只数如实变少）。"""
    ctx = _a1_ctx_at({"short": [
        _cand_at("600519", 9.0, 1251.24),      # 一手 ¥125,124 ⇒ 任何权重都不行
        _cand_at("601318", 8.0, 53.87),        # 一手 ¥5,387 ⇒ w≤25 都不行
        _cand_at("603868", 7.0, 31.26),        # 一手 ¥3,126 ⇒ w=18 可行
        _cand_at("600900", 6.0, 20.0),
        _cand_at("002415", 5.0, 10.0),
    ]}, _P64_TOTAL)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    codes = [p["code"] for p in out["picks"]]
    # n=5（w=18）只剩 3 只买得起 ⇒ 依次下探 n=4（w=22.5）、n=3（w=25）都还是 3 只
    assert codes == ["603868", "600900", "002415"], codes
    assert [p["weight_pct"] for p in out["picks"]] == [25.0] * 3
    assert out["cash_pct"] == 25.0
    assert abs(sum(p["weight_pct"] for p in out["picks"]) + out["cash_pct"] - 100.0) <= 1e-6


def test_p64_t3_the_largest_feasible_n_wins():
    """T3：`w=18` 下只有 3 只可买，但 `w=22.5` 下能凑到 4 只 ⇒ 选 **n=4**。

    构造：一手 ≤ ¥3,518.46 的 3 只（`close=30`）＋ 只在 `w=22.5`
    （¥4,398.08）下够得着的 1 只（`close=40`）＋ 谁都够不着的 1 只（`close=100`）。
    若实现写成「先按 `w=18` 定死只数」，结果会是 n=3 / w=25 / 现金 25 —— 与
    判据的 n=4 / w=22.5 / 现金 10 不同，所以这组数字把两种实现区分开。
    """
    ctx = _a1_ctx_at({"short": [
        _cand_at("A%d" % i, 9.0 - i, 30.0) for i in range(3)
    ] + [_cand_at("B", 5.0, 40.0), _cand_at("C", 4.0, 100.0)]}, _P64_TOTAL)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert len(out["picks"]) == 4, "更大的 n 优先"
    assert [p["code"] for p in out["picks"]] == ["A0", "A1", "A2", "B"]
    assert [p["weight_pct"] for p in out["picks"]] == [22.5] * 4
    assert out["cash_pct"] == 10.0
    # 判据自证：这一组在 w=18 下确实只有 3 只可买、在 w=22.5 下正好 4 只
    assert sum(1 for c in (30.0, 30.0, 30.0, 40.0, 100.0)
               if _P64_TOTAL * 18.0 / 100.0 >= c * 100) == 3
    assert sum(1 for c in (30.0, 30.0, 30.0, 40.0, 100.0)
               if _P64_TOTAL * 22.5 / 100.0 >= c * 100) == 4


def test_p64_t3_five_affordable_names_are_bit_identical_to_v101():
    """T3 后半：名单里 5 只都买得起 ⇒ n=5 / w=18 / 现金 10（与 1.0.1 逐位相同）。"""
    ctx = _a1_ctx_at({"short": [_cand_at("C%d" % i, 9.0 - i, 31.26)
                                for i in range(5)]}, _P64_TOTAL)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert len(out["picks"]) == 5
    assert [p["weight_pct"] for p in out["picks"]] == [18.0] * 5
    assert out["cash_pct"] == 10.0


def test_p64_t3_n_and_weight_are_self_consistent_across_the_grid():
    """T3 不变量：`n` 只 ⇒ `w == min(CAP_PCT, 90/n)`、`cash == 100 − w×n`。

    在一组价格/总资产网格上扫一遍 —— 手写单例只能钉住某一个点。
    """
    closes = (5.0, 20.0, 31.26, 53.87, 1251.24)
    for total in (1000.0, 19547.0, 50000.0, 1000000.0):
        ctx = _a1_ctx_at({"short": [_cand_at("C%d" % i, 9.0 - i, c)
                                    for i, c in enumerate(closes)]}, total)
        out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
        n = len(out["picks"])
        if n == 0:
            assert out["cash_pct"] == 100.0
            continue
        w = min(25.0, (100.0 - 10.0) / n)
        assert [p["weight_pct"] for p in out["picks"]] == [round(w, 2)] * n
        assert out["cash_pct"] == round(100.0 - round(w, 2) * n, 2)


def test_p64_t4_nothing_affordable_means_empty_picks_and_cash_100():
    """T4：全买不起 ⇒ 空仓由 `cash_pct=100.0` 表达（与空池同一条口径）。"""
    ctx = _a1_ctx_at({"short": [_cand_at("C%d" % i, 9.0 - i, 10.0)
                                for i in range(5)]}, 1000.0)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert out["picks"] == [], "1000 元连一手 10 元的标的都买不起"
    assert out["cash_pct"] == 100.0
    assert 1000.0 * 25.0 / 100.0 < 10.0 * 100, "前提：n=1 时也不够一手"


def test_p64_t5_unknown_total_assets_never_filters():
    """T5：`total_assets is None`（账户数据未知）⇒ 跳过过滤，行为同 1.0.1。

    「不知道」≠「买不起」（规则 4）：探针形状的 ctx 上选茅台仍然选得出来。
    """
    ctx = _a1_ctx_at({"short": [_cand_at("600519", 9.0, 1251.24)]}, None)
    out = contract.validate_return("m2_a1", _fn("m2_a1")(ctx))
    assert out["picks"] and out["picks"][0]["code"] == "600519"
    assert out["picks"][0]["weight_pct"] == 25.0
    assert out["cash_pct"] == 75.0


def test_p64_t5_the_probe_ctx_still_runs_green():
    """T5：契约探针（`total_assets=None`）不崩且合契约 —— 过滤不许把它弄红。"""
    out = contract.validate_return("m2_a1", _fn("m2_a1")(dict(contract.PROBE_CTX)))
    assert out["picks"] == []
    assert out["cash_pct"] == 100.0


def test_p64_t6_two_runs_on_the_affordability_ctx_are_byte_identical():
    """T6：同一 ctx 两遍逐字节相同（新分支不许引入顺序依赖）。"""
    ctx = _a1_ctx_at({"short": [
        _cand_at("600519", 9.0, 1251.24), _cand_at("601318", 8.0, 53.87),
        _cand_at("603868", 7.0, 31.26), _cand_at("600900", 6.0, 20.0),
    ]}, _P64_TOTAL)
    fn = _fn("m2_a1")
    first = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    second = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    assert first == second


# ══════════════════════════════════════════════════════════════════════
# P65 —— A1 的权重上限先扣掉「不在 picks 里的存量持仓占比」（源版本 1.0.3）
#
# 真库 2026-09-23 形态（P64 §7.3）：`total_assets = 19,567`，其中
# `000333 ×100 @ ¥82.47 = ¥8,247`（**42.1475%**）是存量持仓、可用现金只有
# ¥11,320。A1 按 `total_assets × w` 定目标敞口 ⇒ 5 × 18% = ¥17,610 > ¥11,320
# ⇒ 成交后现金 **¥−4,490.01**（ERROR_DIARY #73）。v1.0.3 把 Σw 的上限先扣掉
# 那 42.15%，于是总敞口自然回到 ≤ 100%。
# ══════════════════════════════════════════════════════════════════════

#: 真库 2026-09-23 的账户读数（P64 §7.3）。
_P65_TOTAL = 19567.0
_P65_CASH = 11320.0
#: `000333 ×100 @ ¥82.47` ⇒ ¥8,247 / ¥19,567 = 42.1475%。
_P65_HOLD_VALUE = 8247.0
_P65_HOLD_PCT = 42.1475

#: 真库 09-23 的 A1 排序（1.0.1 的 5 只，再接 1.0.2 补的 2 只）＋当日收盘价。
_P65_RANKED = (("600519", 1251.24), ("603868", 31.26), ("600900", 28.08),
               ("002415", 33.11), ("601318", 53.87), ("601398", 8.09),
               ("002508", 16.48))


def _p65_ctx(holdings: list | None = None, *, total=_P65_TOTAL, cash=_P65_CASH,
             ranked=_P65_RANKED) -> dict:
    items = [_cand_at(code, 9.0 - i, close)
             for i, (code, close) in enumerate(ranked)]
    return {"candidates": {"short": items}, "candidates_excluded": {},
            "holdings": [] if holdings is None else holdings,
            "cash": cash, "total_assets": total, "asof": DAY2, "focus": None}


def _p65_holding(code: str = "000333", *, value=_P65_HOLD_VALUE,
                 close: float = 82.47, qty: int = 100) -> dict:
    """一条 ctx 形态的存量持仓（键照 `m2/context.py::holdings_ctx` 的产物）。"""
    return {"code": code, "qty": qty, "cost_price": 86.80, "close": close,
            "market_value": value, "pool": "long", "asof": DAY2,
            "weight_pct": round(value / _P65_TOTAL * 100.0, 4)}


def _p65_out(ctx: dict) -> dict:
    return contract.validate_return("m2_a1", _fn("m2_a1")(ctx))


def test_p65_t1_the_source_says_v103_and_points_at_the_execution_gate():
    """T2 前半：源文本标 v1.0.3，且**写明** v1.0.2 的取整只是偶然闸门。

    四个常量（`LIMIT_N` / `CAP_PCT` / `CASH_FLOOR` / `LOT`）的数值一个字都没改
    —— 判据原文如此，任务书 §4 也把「改主干常量」列为反目标（D-34 只有用户能改）。
    """
    from stocklab.m2.builtin import a1_pick
    assert (a1_pick.LIMIT_N, a1_pick.CAP_PCT, a1_pick.CASH_FLOOR, a1_pick.LOT) \
        == (5, 25.0, 10.0, 100)
    source = BUILTIN_PLUGINS["m2_a1"]
    assert "源版本 v1.0.3" in source
    assert "CashShortfall" in source and 'code="cash"' in source, \
        "源文本必须点名取代它的那道**执行层显式闸门**"
    assert "偶然" in source, \
        "必须写明 v1.0.2 的「整手取整 ⇒ 0 股」只是偶然闸门（P64 §1 的教训）"
    assert "weight_pct" in source and "_reserved_pct(" in source
    guard.check_source(source)


def test_p65_t2_the_locked_holding_lowers_the_weight_cap():
    """T2 主判据：真库 09-23 形态 ⇒ `cap = 90 − 42.1475 = 47.8525`、`cash = 52.15`。

    ⚠️ **实测只数不是判据里那句话的 5 只**。判据 §3.2 写「5 只各 9.57%」，
    但同一节的步骤 2 又要求 affordability **沿用 1.0.2 的 `_affordable`**：
    在 `w0 = 9.57%` 下，`603868`（一手 ¥3,126）、`600900`（¥2,808）、
    `002415`（¥3,311）的目标市值只有 ¥1,872.56 ⇒ 全都买不起 ⇒ 凑不齐 5 只，
    下降式循环一路降到 `n=3`（`w0 = 15.95%`，目标 ¥3,120.94）才凑齐。
    两组解的 `cash_pct` **都是 52.15**（因为 `w × n` 都等于那个 47.85 的上限），
    所以「cap / cash」这两个判据数都对得上，差别只在**只数**。

    这一点如实记在任务书 §7 —— 若改成「5 只各 9.57%」= 放弃 affordability 过滤、
    让 3 只不可执行的标的进 picks，那正是 P64 修掉的病（计划与执行长得一样）。
    """
    ctx = _p65_ctx([_p65_holding()])
    out = _p65_out(ctx)
    weights = [p["weight_pct"] for p in out["picks"]]
    assert weights == [15.95] * 3, weights
    assert [p["code"] for p in out["picks"]] == ["600900", "601398", "002508"]
    assert out["cash_pct"] == 52.15
    assert abs(sum(weights) + out["cash_pct"] - 100.0) <= 1e-9, "契约 _check_cross"
    # 判据里的两个数：上限与现金。
    assert round(90.0 - _P65_HOLD_PCT, 4) == 47.8525
    assert round(sum(weights), 2) <= round(90.0 - _P65_HOLD_PCT, 4) + 1e-9
    # 自证：w0=9.57 下真的凑不齐 5 只（否则上面那条「判据对不上」是空话）
    assert sum(1 for _c, close in _P65_RANKED
               if _P65_TOTAL * 9.57 / 100.0 >= close * 100) < 5


def test_p65_t2_a_holding_inside_the_picks_is_not_reserved():
    """细化那一步真的生效：存量持仓**就在 picks 里** ⇒ 它不占额度，权重被抬高。

    `600900` 一手 ¥2,808 ⇒ 占 ¥19,567 的 14.35%。第一轮（保守，把它也当占用）
    `w0 = round(min(25, (90 − 14.35)/4), 2) = 18.91`；细化后 `reserved1 = 0`
    （唯一那笔存量已在 picks 里）⇒ `w1 = min(25, 90/4) = 22.5 > w0` ⇒ 用 22.5。
    """
    ctx = _p65_ctx([_p65_holding("600900", value=2808.0, close=28.08)])
    out = _p65_out(ctx)
    weights = [p["weight_pct"] for p in out["picks"]]
    assert weights == [22.5] * 4, weights
    assert [p["code"] for p in out["picks"]] == ["603868", "600900", "002415", "601398"]
    assert out["cash_pct"] == 10.0
    # 第一轮的保守解是 18.91% —— 若没有细化这一步，权重会停在那儿
    assert round(min(25.0, (90.0 - 2808.0 / _P65_TOTAL * 100.0) / 4), 2) == 18.91


def test_p65_t2_no_holdings_is_bit_identical_to_v101():
    """回归钉住：`holdings` 为空 ⇒ 与 1.0.1/1.0.2 逐位相同（5 只 / 18% / 现金 10）。"""
    ctx = _p65_ctx([])
    out = _p65_out(ctx)
    assert len(out["picks"]) == 5
    assert [p["weight_pct"] for p in out["picks"]] == [18.0] * 5
    assert out["cash_pct"] == 10.0
    assert [p["code"] for p in out["picks"]] == \
        ["603868", "600900", "002415", "601398", "002508"]


def test_p65_t2_a_holding_without_weight_pct_falls_back_to_v102():
    """退化口径：任一 holding 缺 `weight_pct` ⇒ `reserved = 0`，行为同 1.0.2。

    「不知道存量占比」≠「存量把额度占满了」（与 `_affordable` 同一条规则 4）。
    上面那条（空 holdings）是它的对照组：**同样**的一份 picks。
    """
    ctx = _p65_ctx([{**_p65_holding(), "weight_pct": None}])
    out = _p65_out(ctx)
    assert len(out["picks"]) == 5
    assert [p["weight_pct"] for p in out["picks"]] == [18.0] * 5
    assert out["cash_pct"] == 10.0


def test_p65_t2_unknown_total_assets_ignores_the_reserved():
    """`total_assets is None`（探针形状）⇒ 不过滤存量 ⇒ 与 1.0.2 逐位相同。"""
    ctx = _p65_ctx([_p65_holding()], total=None)
    out = _p65_out(ctx)
    assert len(out["picks"]) == 5
    assert [p["weight_pct"] for p in out["picks"]] == [18.0] * 5
    assert out["cash_pct"] == 10.0
    assert [p["code"] for p in out["picks"]] == \
        ["600519", "603868", "600900", "002415", "601318"]


def test_p65_t2_reserved_beyond_the_cap_means_empty_picks_and_cash_100():
    """边界：存量占比 ≥ 90% ⇒ 上限 ≤ 0 ⇒ 空 picks ＋ `cash_pct = 100.0`。

    与 1.0.2 的空仓分支同一条口径（空清单必须由 `cash_pct=100` 表达）。
    **不许**产出负权重。
    """
    for pct in (90.0, 95.0):
        ctx = _p65_ctx([{**_p65_holding(),
                         "weight_pct": pct, "market_value": _P65_TOTAL * pct / 100.0}])
        out = _p65_out(ctx)
        assert out["picks"] == [], f"reserved={pct}% 时不该选出任何标的"
        assert out["cash_pct"] == 100.0


def test_p65_t6_two_runs_on_the_reserved_ctx_are_byte_identical():
    """确定性：同一份带存量的 ctx 两遍逐字节相同（新分支不许引入顺序依赖）。"""
    ctx = _p65_ctx([_p65_holding()])
    fn = _fn("m2_a1")
    first = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    second = json.dumps(fn(ctx), sort_keys=True, ensure_ascii=False)
    assert first == second


def test_p65_t4_grid_weights_stay_self_consistent_and_inside_the_cap():
    """网格不变量：`Σw + cash_pct == 100`，且 `Σw ≤ (90 − 未在库占比)`。

    扫 4 档总资产 × 4 档存量占比 × 2 个价格档（含茅台那种一手 6 位数的）。
    只用手写单例钉不住这类性质 —— 它得在一张网格上成立才叫不变量。
    """
    tiers = ((31.26, 20.0, 10.0, 5.0, 3.0),
             (1251.24, 53.87, 31.26, 20.0, 10.0))
    for total in (5_000.0, 19_567.0, 100_000.0, 1_000_000.0):
        for held_pct in (0.0, 10.0, 42.147, 80.0):
            for tier in tiers:
                ranked = tuple(("C%d" % i, close)
                               for i, close in enumerate(tier))
                hold_value = round(total * held_pct / 100.0, 4)
                holdings = [] if held_pct == 0.0 else [
                    {"code": "HOLD", "qty": 100, "cost_price": 1.0,
                     "close": round(hold_value / 100.0, 4),
                     "market_value": hold_value, "weight_pct": held_pct,
                     "pool": None, "asof": DAY2}]
                cash = round(total - hold_value, 4)
                ctx = _p65_ctx(holdings, total=total, cash=cash, ranked=ranked)
                out = _p65_out(ctx)
                weights = [p["weight_pct"] for p in out["picks"]]
                label = f"total={total} held={held_pct}% tier={tier[0]}"
                assert abs(sum(weights) + out["cash_pct"] - 100.0) <= 1e-9, label
                assert out["cash_pct"] == round(100.0 - sum(weights), 2), label
                if not weights:
                    assert out["cash_pct"] == 100.0, label
                    continue
                assert all(w > 0 for w in weights), f"{label}: 出现非正权重 {weights}"
                assert all(w <= 25.0 for w in weights), label
                # 「不杠杆」：Σw 不许越过 (90 − 未在库占比)。HOLD 不在候选池里，
                # 所以未被 pick 时它就是「未在库」的那一笔。
                #
                # 允差 `0.005 × n`：权重是 `round(cap/n, 2)`，逐项四舍五入后
                # Σw 可能比 cap 高出不到一分 —— 这是**既有**的取整口径
                # （v1.0.2 的 `round(90/n, 2)` 同款，`n=7` 时会给出 90.02），
                # 不是 v1.0.3 引入的，且远小于 10% 的现金下限。
                if not any(p["code"] == "HOLD" for p in out["picks"]):
                    assert sum(weights) <= (90.0 - held_pct + 0.005 * len(weights)
                                            + 1e-9), label
