"""P40：财报质量先验的两个变体（V1 `fin-quality-pct` / V2 `fin-roe-yoy`）。

预注册：`docs/experiments/2026-09-22-financials-quality-prior.md`（第 1–5 节跑前写死）。
本文件钉住预注册 §2 要求的五组测试：

  1. **PIT**：`notice_date > asof` 的期读不到；`notice_date == asof` 可用；
  2. **横截面**：5 只标的、因子分位已知 → `q` 与符号与手算逐位一致（含 `inv_days` 极性取反）；
  3. **退化**：可用因子 < 3 / 横截面 n < 5 / 无任何财报 → `None`，**不抛未受控异常**；
  4. **单变量**：两个新变体都能过 `assert_single_variable`；
  5. **极性真源**：源码扫描钉住「变体没有自带第二份因子极性表」（ERROR_DIARY #50 同款：
     用检查把「文件说谎」变成不可能），并带**反向自检** —— 否则「扫描零个字典」也会全绿。
"""

from __future__ import annotations

import ast
import datetime as _dt
from pathlib import Path

import pytest

from stocklab.experiments.variants import (fin_quality_scores, get_variant,
                                           load_fin_quality_sign,
                                           load_fin_roe_yoy_sign)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-20T16:00:00+08:00"
ASOF = "2026-09-02"
PREREG = "2026-09-22-financials-quality-prior.md"

_INSERT = ("INSERT INTO financial_reports (code, report_date, notice_date,"
           " notice_date_source, report_type, total_assets, parent_equity,"
           " total_equity, total_liabilities, inventory, total_operate_income,"
           " operate_cost, parent_netprofit, netcash_operate,"
           " construct_long_asset, industry_name, source, fetched_at, created_at,"
           " raw_refs_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

#: 让 V2 的「去年同期」可算：最近期 2026Q2 的去年同期是 2025Q2，
#: 其 TTM 需要 2024Q3/Q4 + 2025Q1/Q2 的单季 → 累计行须从 2024Q1 起。
_QUARTERS = [(2024, 1), (2024, 2), (2024, 3), (2024, 4),
             (2025, 1), (2025, 2), (2025, 3), (2025, 4),
             (2026, 1), (2026, 2)]
_Q_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
_RTYPE = {1: "一季报", 2: "中报", 3: "三季报", 4: "年报"}


def _plus_days(iso: str, days: int) -> str:
    d = _dt.date.fromisoformat(iso) + _dt.timedelta(days=days)
    return d.isoformat()


def _row(conn, code, report_date, notice, *, q, base, equity, inv, weak=False):
    conn.execute(_INSERT, (
        code, report_date, notice, "f10", _RTYPE[int(report_date[5:7]) // 3],
        8e10, equity, 8.4e10, 7.6e10,
        None if weak else inv,
        base * q, None if weak else 0.6 * base * q, 0.1 * base * q,
        None if weak else 0.15 * base * q, None if weak else 0.05 * base * q,
        "白色家电", "eastmoney-datacenter", NOW, NOW, "[]"))


def _seed(conn, code, *, base=1e9, equity=5e9, inv=0.6e9, last_notice=None,
          last_inv=None, drop_last=False, scale_by_year=None, weak=False):
    """插入 `code` 的十期累计财报。

    默认 `notice = report_date + 45 天`（全部 <= ASOF）；`last_notice` / `last_inv`
    只覆写最后一期（2026Q2），用于 PIT 边界测试。`scale_by_year` 给某年整体乘一个
    系数（V2 的同比来源）。`weak=True` 把 `operate_cost` / `inventory` /
    `netcash_operate` / `construct_long_asset` 置 **NULL** → 只剩 `roe` 一个可用因子
    （< 3，应被剔除）。
    """
    for y, q in _QUARTERS:
        if drop_last and (y, q) == (2026, 2):
            continue
        s = (scale_by_year or {}).get(y, 1.0)
        rd = f"{y}-{_Q_END[q]}"
        notice = last_notice if ((y, q) == (2026, 2) and last_notice) else _plus_days(rd, 45)
        inv_v = last_inv if ((y, q) == (2026, 2) and last_inv is not None) else inv
        _row(conn, code, rd, notice, q=q, base=base * s, equity=equity, inv=inv_v,
             weak=weak)


def _db(path, codes, **kw):
    init_db(path)
    conn = connect(path)
    for code, over in codes.items():
        _seed(conn, code, **over)
    return conn


# ---------- 1. PIT：notice_date > asof 读不到；notice_date == asof 可用 ----------

def test_v1_never_sees_a_report_noticed_after_asof(tmp_db):
    """同一批数据、只改最后一期的公告日：`> asof` 看不见（回落到最新**可见**期），
    `== asof` 看得见。用「有/无该行」的差分把「取最近一根」堵死。

    A 的最后一期 `inv` 故意给一个**会翻转横截面排名**的量（50e9）：可见时 A 变成
    存货最差 → 质量分位最低；不可见时 A 回落到更早期、与他人并列 → 分位回到中位。
    """
    codes = {c: {"inv": 0.6e9} for c in "ABCDE"}
    codes["A"]["last_inv"] = 50e9
    future = {c: dict(o, last_notice=_plus_days(ASOF, 1)) for c, o in codes.items()}
    same = {c: dict(o, last_notice=ASOF) for c, o in codes.items()}
    early = {c: dict(o, drop_last=True) for c, o in codes.items()}

    cf = _db(tmp_db.parent / "future.db", future)
    cs = _db(tmp_db.parent / "same.db", same)
    ce = _db(tmp_db.parent / "early.db", early)
    try:
        fut = fin_quality_scores(cf, ASOF, codes=list(codes))
        same_q = fin_quality_scores(cs, ASOF, codes=list(codes))
        early_q = fin_quality_scores(ce, ASOF, codes=list(codes))
    finally:
        cf.close(); cs.close(); ce.close()

    # 不可见 → 与「根本没有那一期」逐位相同（说明回落到最新**可见**期，不是取最近一根）
    assert fut == early_q, "notice_date > asof 的行被读到了（或没回落到最新可见期）"
    # 可见 → A 的排名被那一期翻转，q 必然不同
    assert same_q["A"] != fut["A"], "notice_date == asof 应当可用（口径选「当日起可用」）"


# ---------- 2. 横截面：5 只标的、手算逐位一致 ----------

def test_v1_cross_section_matches_hand_calculation(tmp_db):
    """5 只标的只在 `inventory` 上不同 → 只有 `inv_days` 分位不同，其余四因子
    bit-identical 故分位恒为 0.5（`gm_yoy_pp` 因 2024 行齐全也可算）。手算见注释。"""
    invs = {"A": 0.4e9, "B": 0.6e9, "C": 0.8e9, "D": 1.0e9, "E": 1.2e9}  # 升序
    conn = _db(tmp_db, {c: {"inv": v} for c, v in invs.items()})
    try:
        q = fin_quality_scores(conn, ASOF, codes=list(invs))
        signs = [load_fin_quality_sign(conn, c, ASOF, codes=list(invs))
                 for c in invs]
    finally:
        conn.close()

    # 手算：cost_ttm = 4×0.6×1e9 = 2.4e9；inv_days = 365×inv/2.4e9
    #   → 60.83 / 91.25 / 121.67 / 152.08 / 182.50（升序；极性取反 → 分位 1.0…0.0）
    # roe / gross_margin / gm_yoy_pp / fcf_margin 四因子全部并列 → _pct_of 恒给 0.5
    #   q_raw = mean(0.5×4, inv_pct) = (2.0 + inv_pct)/5
    #   → 0.6 / 0.55 / 0.5 / 0.45 / 0.4（**降序**：存货周转越快 → 质量越高）
    # 再对 q_raw 取横截面分位（越大越好）→ 0.0 / 0.25 / 0.5 / 0.75 / 1.0（升序）
    # 于是按 inv 升序排列的 q = 1.0 / 0.75 / 0.5 / 0.25 / 0.0
    assert q["A"] == pytest.approx(1.0)
    assert q["B"] == pytest.approx(0.75)
    assert q["C"] == pytest.approx(0.5)
    assert q["D"] == pytest.approx(0.25)
    assert q["E"] == pytest.approx(0.0)
    # 符号：q>=0.70 → +1；q<=0.30 → −1；区间内 0（Q(D)=0.25、Q(E)=0.0 都 <= 0.30 → −1）
    assert signs == [1, 1, 0, -1, -1]
    # 极性真源点：`inv_days` **最小**（存货周转最快）者拿**最高**质量分位
    assert q["A"] > q["E"]


# ---------- 3. 退化：返回 None 且不抛未受控异常 ----------

def test_v1_weak_factor_stock_is_dropped_not_guessed(tmp_db):
    """可用因子 < 3（只剩 roe）→ 该标的当日的 q = `None`（剔除），其余照常。"""
    codes = {"A": {"inv": 0.4e9}, "B": {"inv": 0.6e9}, "C": {"inv": 0.8e9},
             "D": {"inv": 1.0e9}, "E": {"inv": 1.2e9}, "F": {"weak": True}}
    conn = _db(tmp_db, codes)
    try:
        q = fin_quality_scores(conn, ASOF, codes=list(codes))
    finally:
        conn.close()
    assert q["F"] is None, "可用因子 < 3 必须剔除，不许拿 roe 单因子冒充合成分"
    assert sum(v is not None for v in q.values()) == 5


def test_v1_cross_section_below_five_codes_gives_none(tmp_db):
    """横截面 n < 5 → 分位无意义，**全体** `None`（不许只给那 4 个算分位）。"""
    codes = {"A": {"inv": 0.4e9}, "B": {"inv": 0.6e9},
             "C": {"inv": 0.8e9}, "D": {"inv": 1.0e9}}
    conn = _db(tmp_db, codes)
    try:
        q = fin_quality_scores(conn, ASOF, codes=list(codes))
    finally:
        conn.close()
    assert q == {"A": None, "B": None, "C": None, "D": None}


def test_v1_without_any_report_returns_none_without_raising(tmp_db):
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        q = fin_quality_scores(conn, ASOF, codes=["A", "B", "C", "D", "E"])
    finally:
        conn.close()
    assert q == {c: None for c in "ABCDE"}


# ---------- V2 fin-roe-yoy ----------

def test_v2_roe_yoy_signs(tmp_db):
    """自身 roe(TTM) 相对去年同期：Δ>+0.01 → +1、Δ<−0.01 → −1、其余 0。

    手算：equity 恒 5e9、单季利润 0.1×base×scale →
      roe_now = 0.2×base×(1+scale)/5e9、roe_prev = 0.4×base/5e9 ⇒ Δ = 0.04×(scale−1)。
      scale 1.50 → Δ=+0.020 → +1；0.50 → −0.020 → −1；1.00 → 0；1.10 → +0.004 → 0。
    """
    conn = _naive_db(tmp_db)
    try:
        for scale, want in ((1.50, 1), (0.50, -1), (1.00, 0), (1.10, 0)):
            code = f"S{scale}"
            _seed(conn, code, scale_by_year={2026: scale})
            got = load_fin_roe_yoy_sign(conn, code, ASOF)
            assert got == want, f"scale={scale}"
    finally:
        conn.close()


def test_v2_degenerate_returns_none_without_raising(tmp_db):
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        assert load_fin_roe_yoy_sign(conn, "NOPE", ASOF) is None
        # 只有最近一期（无去年同期）→ None
        _row(conn, "ONLY", "2026-06-30", "2026-08-14", q=2, base=1e9,
             equity=5e9, inv=0.6e9)
        _row(conn, "ONLY", "2026-03-31", "2026-05-14", q=1, base=1e9,
             equity=5e9, inv=0.6e9)
        assert load_fin_roe_yoy_sign(conn, "ONLY", ASOF) is None
    finally:
        conn.close()


# ---------- 4. 单变量不变量 ----------

@pytest.mark.parametrize("name,mode", [("fin-quality-pct", "fin_quality_pct"),
                                       ("fin-roe-yoy", "fin_roe_yoy")])
def test_new_variants_change_exactly_one_variable(name, mode):
    v = get_variant(name)                    # get_variant 内部已 assert_single_variable
    assert v.changed_axis == "mu_mode"
    assert v.spec.changed_fields() == ("mu_mode",)
    assert v.spec.mu_mode == mode
    assert v.spec.sigma_mode == "const" and v.spec.dist_mode == "gaussian"
    assert v.prereg_doc.endswith(PREREG)


def test_both_new_mu_modes_are_refused_when_missing():
    """变体生效但当日返回 None → `DegenerateInput` 硬拒绝，**不许**静默回落基线。"""
    from stocklab.data.models import Bar
    from stocklab.predict.model import DegenerateInput, ForecastSpec, compute_forecast

    base = 10.0
    hist = [Bar(code="000333", date=f"2024-{m:02d}-{d:02d}", open=base, high=base,
                low=base, close=base, volume=1000, amount=base * 1000, turnover=1.0,
                source="test", adj_mode="none")
            for m in (1, 2, 3) for d in range(1, 25)]     # 72 根 > WINDOW+1
    asof = hist[-1].date
    for mode in ("fin_quality_pct", "fin_roe_yoy"):
        with pytest.raises(DegenerateInput, match="fin_sign=None"):
            compute_forecast(code="000333", asof=asof, bars=hist, target_date=asof,
                             strategy_mix={}, spec=ForecastSpec(mu_mode=mode))


# ---------- 5. 极性真源：变体不许自带第二份极性表 ----------

_FACTOR_NAMES = {"roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"}


def _factor_bool_dicts(src: str) -> list[list[str]]:
    """源码里所有「因子名 → bool」的字典字面量（= 第二份极性表）。"""
    found: list[list[str]] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        vals = [v for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, bool)]
        if len(keys) >= 2 and len(vals) >= 2 and any(k in _FACTOR_NAMES for k in keys):
            found.append(keys)
    return found


def _uses_polarity_attribute(src: str) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "POLARITY"
               for n in ast.walk(ast.parse(src)))


def test_polarity_scanner_has_teeth():
    """反向自检：把一份违规样本喂给扫描函数，必须判红（ERROR_DIARY #50）。"""
    bad = 'POLARITY = {"roe": True, "inv_days": False, "gm_yoy_pp": True}\n'
    assert _factor_bool_dicts(bad) != []


def test_fin_variants_do_not_carry_a_second_polarity_table():
    import stocklab.experiments.variants as variants_mod

    src = Path(variants_mod.__file__).read_text(encoding="utf-8")
    assert _factor_bool_dicts(src) == [], (
        "variants.py 里出现了「因子名 → bool」的字典 —— 那是第二份极性表，"
        "必然与 candidate/cross_section.POLARITY 走样")
    assert _uses_polarity_attribute(src), (
        "没有引用 cross_section.POLARITY —— 极性必须取自真源，不许在变体里自己定")


def _naive_db(path):
    """`_db` 的空表版本（V2 用例逐条自己插）。"""
    init_db(path)
    return connect(path)
