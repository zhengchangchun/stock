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
