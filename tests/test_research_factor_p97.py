"""P97 T5：研究侧因子注册表 ＋ `--factor` 的可用例（新增，旧用例不改）。

七组：

1. **缺省逐位不变**（本任务第一判据）—— 默认路径（不传 `--factor`）的 report JSON /
   `render_md` / `summary_line` 与改动前**逐字节相同**。黄金 digest 由**改动前**的
   源码在同一夹具上实测得出（`/tmp/p97/golden.py`），内联在本文件里 ——
   **不**写成 `reports/` 里的文件。`prereg_path` 是 tmp 绝对路径 ⇒ 比对前剔除
   （其内容由 `prereg_sha256` 逐位钉住）。
2. **注册表**：`FACTOR_SOURCES` 是唯一真源，`SECONDARY_FACTORS == FACTOR_FEATURE_KEYS`
   继续成立，四个集合的重叠关系按 L2 断言（`gm_yoy_pp` 可与 `SECONDARY_FACTORS`
   重叠，**不许**与 `RESEARCH_FACTORS` 重叠）。
3. **`mf_ratio_5d` 取值正确性**：逐字公式 ＋ 4 条边界（缺行 / NULL / 不及 5 日 / PIT）。
4. **`one_factor` 取值来源参数化**：`values_by_mark=None` ⇒ 既有行为；
   给了 ⇒ 从外部来源取，`None` 不进截面。
5. **`--factor` 端到端**：ctx 因子升格（`gm_yoy_pp`）＋ 新取值器因子（`mf_ratio_5d`）；
   `MAIN_FACTORS` 既有读数只增不改。
6. **命名防静默覆盖**：缺省旧名；给了加 `tag`；非法字符换 `_`；`factor_tag` 落 JSON。
7. **预注册 fail-closed**：未登记名 / 未进预注册名 ⇒ exit 2、零产物。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stocklab.cli.main import main
from stocklab.research import factor, xsec
from stocklab.store.db import connect
from tests.test_research_factor import (
    CODES, N_WEEKDAYS, START, _Synth, _install, _prereg, _run_inproc, _synth_db,
)
from tests.test_research_signal import _weekdays

NOW = "2026-09-29T00:00:00+08:00"

#: 逐位比对时剔除的键：三个耗时键（与既有用例同规）＋ `prereg_path`
#: （tmp 绝对路径，同内容不同目录 ⇒ 会假红；内容由 `prereg_sha256` 钉住）。
_ENV_KEYS = ("elapsed_s", "scan_s", "fwd_load_s", "prereg_path")

#: 缺省路径的黄金 digest —— **改动前**源码（HEAD `3da2889` 的工作区）在同一夹具上
#: 实测得出（`/tmp/p97/golden.py`，600 个交易日 / seed21 夹具预注册）。
GOLDEN_REPORT_DIGEST = \
    "ef26338e3baee88761689571fe8dd650249aa7851b872c9e2d660247f3606fa2"
GOLDEN_MD_DIGEST = \
    "f2f459b3f0a595737cc352d791dcb67a14c3fb9266657f2c8c974a1d694f026a"
GOLDEN_SUMMARY_DIGEST = \
    "cfb4b2c7db45986cdfa022a3cb14cf57198b05d0b976a5d4155a6df47fb53368"


def _digest(obj) -> str:
    if not isinstance(obj, str):
        blob = json.dumps({k: v for k, v in obj.items() if k not in _ENV_KEYS},
                          sort_keys=True, ensure_ascii=False)
    else:
        blob = obj
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _normalize_md(text: str, prereg: Path) -> str:
    return text.replace(str(prereg), "<PREREG>")


def _default_report(monkeypatch, db, tmp_path, synth=None) -> dict:
    """默认路径（不传 `factors`）的一次完整跑 —— 与黄金快照同一个夹具。"""
    return _run_inproc(monkeypatch, db, tmp_path,
                       synth if synth is not None else _Synth(_weekdays(600)))


# ---------------------------------------------------------------------------
# 1. 缺省逐位不变（本任务第一判据）
# ---------------------------------------------------------------------------

def test_default_report_json_is_byte_identical_to_pre_p97(
        tmp_db, tmp_path, monkeypatch):
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    assert _digest(rep) == GOLDEN_REPORT_DIGEST


def test_default_render_md_and_summary_line_are_byte_identical(
        tmp_db, tmp_path, monkeypatch):
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    prereg = tmp_path / "p.md"
    assert _digest(_normalize_md(factor.render_md(rep), prereg)) == \
        GOLDEN_MD_DIGEST
    assert _digest(factor.summary_line(rep)) == GOLDEN_SUMMARY_DIGEST


def test_default_report_carries_no_new_key(tmp_db, tmp_path, monkeypatch):
    """缺省 ⇒ **一个新键都不许出现**（`§6` 第一判据是「报告 JSON 逐字节相同」）。

    ⚠️ 与任务书 §3.2 的字面「`factor_tag`（缺省 None）」冲突：新增键必然改 JSON，
    两条不能同时满足 ⇒ 硬约束优先（§7 点名）。
    """
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    for key in ("research", "factor_tag", "selected_factors", "factor_sources",
                "verdict"):
        assert key not in rep, f"缺省路径不得新增键 {key}"


def test_default_write_report_keeps_the_old_file_name(tmp_db, tmp_path):
    """缺省 ⇒ 文件名一个字不改（`<end>-factor-ic-<universe>`）。"""
    fake = {"end": "2026-09-24", "universe_id": "csi300-500"}
    assert factor._report_stem(fake) == "2026-09-24-factor-ic-csi300-500"


# ---------------------------------------------------------------------------
# 2. 注册表
# ---------------------------------------------------------------------------

def test_secondary_factors_still_equal_factor_feature_keys():
    """`tests/test_research_factor.py:233-234` 的那条断言在这里再钉一次（不得改旧文件）。"""
    from stocklab.candidate.run import FACTOR_FEATURE_KEYS
    assert factor.SECONDARY_FACTORS == FACTOR_FEATURE_KEYS
    assert factor.SECONDARY_FACTORS == ("roe", "gross_margin", "gm_yoy_pp",
                                        "inv_days", "fcf_margin")


def test_factor_sources_is_the_single_truth():
    union = (set(factor.MAIN_FACTORS) | set(factor.SECONDARY_FACTORS)
             | set(factor.PROMOTABLE_FACTORS) | set(factor.RESEARCH_FACTORS))
    assert set(factor.FACTOR_SOURCES) == union
    for name in factor.MAIN_FACTORS + factor.SECONDARY_FACTORS + \
            factor.PROMOTABLE_FACTORS:
        assert factor.FACTOR_SOURCES[name] == "ctx"
    for name in factor.RESEARCH_FACTORS:
        assert factor.FACTOR_SOURCES[name] == "research"


def test_registry_overlap_rules_are_exactly_l2():
    """`gm_yoy_pp` 可与 `SECONDARY_FACTORS` 重叠，**不许**与 `RESEARCH_FACTORS` 重叠。

    P101 起 `RESEARCH_FACTORS` 含两个名字（`mf_ratio_5d` ＋ `ep_ttm`），P102 再追加
    `ann_count_5d`、P103 再追加 `mf_net_surprise_20d` —— 本用例只把「集合内容」对齐
    到事实，其余重叠规则（新名字不进 `SECONDARY_FACTORS` / `MAIN_FACTORS` /
    `PROMOTABLE_FACTORS`）一字不改。
    """
    assert set(factor.PROMOTABLE_FACTORS) == {"gm_yoy_pp"}
    assert set(factor.RESEARCH_FACTORS) == {"mf_ratio_5d", "ep_ttm",
                                            "ann_count_5d",
                                            "mf_net_surprise_20d"}
    assert "gm_yoy_pp" in factor.SECONDARY_FACTORS
    assert "gm_yoy_pp" not in factor.RESEARCH_FACTORS
    assert "mf_ratio_5d" not in factor.SECONDARY_FACTORS
    assert "mf_ratio_5d" not in factor.MAIN_FACTORS
    assert "ep_ttm" not in factor.SECONDARY_FACTORS
    assert "ep_ttm" not in factor.MAIN_FACTORS
    assert "ann_count_5d" not in factor.SECONDARY_FACTORS
    assert "ann_count_5d" not in factor.MAIN_FACTORS
    # 同一个因子只有一个入口：两集合不相交
    assert not (set(factor.RESEARCH_FACTORS) & set(factor.PROMOTABLE_FACTORS))


def test_registered_names_do_not_leak_into_factor_feature_keys():
    """新名字**不许**塞进 `FACTOR_FEATURE_KEYS`（那是打分载荷的形状真源）。"""
    from stocklab.candidate.run import FACTOR_FEATURE_KEYS
    for name in factor.RESEARCH_FACTORS + factor.PROMOTABLE_FACTORS:
        assert name not in FACTOR_FEATURE_KEYS or name in factor.SECONDARY_FACTORS


# ---------------------------------------------------------------------------
# 3. `mf_ratio_5d` 取值正确性（fixture 库）
# ---------------------------------------------------------------------------

def _mf_db(tmp_db, dates: list[str], rows: list[tuple]) -> None:
    """只建日历 ＋ `money_flow_daily` 的夹具库（`rows` = `(code, date, value)`）。"""
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO money_flow_daily (code, date, ratio_amount, source,"
        " fetched_at, created_at, resp_sha256) VALUES (?,?,?,'t',?,?,'x')",
        [(code, d, v, NOW, NOW) for code, d, v in rows])
    c.commit()
    c.close()


def test_mf_ratio_5d_is_the_mean_of_the_last_five_trading_days(tmp_db):
    """① 正常 5 日 ⇒ 5 个 `ratio_amount` 的均值。"""
    dates = _weekdays(30)[:10]
    _mf_db(tmp_db, dates, [("600000", d, float(i + 1))
                           for i, d in enumerate(dates[:5])])
    c = connect(tmp_db)
    try:
        assert factor.mf_ratio_5d(c, "600000", dates[4]) == pytest.approx(3.0)
    finally:
        c.close()


def test_mf_ratio_5d_is_none_when_any_of_the_five_is_null(tmp_db):
    """② 5 日中任一 `NULL` ⇒ `None`（**不补 0**）。"""
    dates = _weekdays(30)[:10]
    rows = [("600000", d, float(i + 1)) for i, d in enumerate(dates[:5])]
    rows[2] = ("600000", dates[2], None)
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        assert factor.mf_ratio_5d(c, "600000", dates[4]) is None
    finally:
        c.close()


def test_mf_ratio_5d_is_none_when_the_row_is_missing(tmp_db):
    """行缺失与 `NULL` 同规（缺行也是「算不出」）。"""
    dates = _weekdays(30)[:10]
    rows = [("600000", d, float(i + 1)) for i, d in enumerate(dates[:5])
            if i != 1]
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        assert factor.mf_ratio_5d(c, "600000", dates[4]) is None
    finally:
        c.close()


def test_mf_ratio_5d_is_none_before_five_trading_days_exist(tmp_db):
    """③ `asof` 前不足 5 个交易日 ⇒ `None`（不是「有几日算几日」）。"""
    dates = _weekdays(30)[:10]
    _mf_db(tmp_db, dates, [("600000", d, float(i + 1))
                           for i, d in enumerate(dates[:5])])
    c = connect(tmp_db)
    try:
        assert factor.mf_ratio_5d(c, "600000", dates[3]) is None
        assert factor.mf_ratio_5d(c, "600000", dates[4]) is not None
    finally:
        c.close()


def test_mf_ratio_5d_never_reads_rows_after_asof(tmp_db):
    """④ `date > asof` 的行**绝不能**被读到（PIT 锚 = `money_flow_daily.date`）。

    `dates[5]`/`dates[6]` 的值极大 —— 若泄漏进来，均值立刻爆表。
    """
    dates = _weekdays(30)[:10]
    rows = [("600000", d, float(i + 1)) for i, d in enumerate(dates[:5])]
    rows += [("600000", dates[5], 1e6), ("600000", dates[6], 1e6)]
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        assert factor.mf_ratio_5d(c, "600000", dates[4]) == pytest.approx(3.0)
    finally:
        c.close()


def test_mf_ratio_5d_counts_only_trading_days_not_natural_days(tmp_db):
    """窗口是「`trading_calendar` 里 `<= asof` 的最后 5 个**交易日**」，不是 5 自然日。"""
    dates = ["2026-01-05", "2026-01-09", "2026-01-20", "2026-01-21",
             "2026-02-03", "2026-02-04", "2026-02-05"]
    _mf_db(tmp_db, dates, [("600000", d, float(i + 1))
                           for i, d in enumerate(dates[:5])])
    c = connect(tmp_db)
    try:
        # 5 个交易日跨了 29 个自然日，按自然日窗口只会取到最后 1~2 行
        assert factor.mf_ratio_5d(c, "600000", dates[4]) == pytest.approx(3.0)
    finally:
        c.close()


def test_research_map_is_batch_shaped_and_drops_incomplete_codes(tmp_db):
    """`_research_map` 一次给出该 `asof` 的**全部**可用标的（批量友好）。

    完整 5 日的进结果集；缺 1 日的**不进**（`None` 不落进结果里）。
    """
    dates = _weekdays(30)[:10]
    rows = [("600000", d, float(i + 1)) for i, d in enumerate(dates[:5])]
    rows += [("600001", d, 1.0) for d in dates[:4]]          # 缺 dates[4]
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._research_map(c, dates[4], "mf_ratio_5d")
        assert got == {"600000": pytest.approx(3.0)}
        assert factor._research_value(c, "600000", dates[4],
                                      "mf_ratio_5d") == pytest.approx(3.0)
        assert factor._research_value(c, "600001", dates[4],
                                      "mf_ratio_5d") is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 4. `one_factor` 取值来源参数化
# ---------------------------------------------------------------------------

_SEL_CODES = [f"c{i:03d}" for i in range(35)]


def _one(name, **kw):
    kw.setdefault("kind", "main")
    kw.setdefault("payloads_by_mark", {})
    kw.setdefault("periods", [("d0", "d1")])
    kw.setdefault("fwd_by_period", [{c: float(i) for i, c in
                                     enumerate(_SEL_CODES)}])
    kw.setdefault("val_idx", [0])
    return factor.one_factor(name, **kw)


def test_one_factor_uses_external_values_when_values_by_mark_is_given():
    vals = {"d0": {c: float(i) for i, c in enumerate(_SEL_CODES)}}
    out = _one("mf_ratio_5d", values_by_mark=vals)
    assert out["value_coverage_p50"] == float(len(_SEL_CODES))
    assert out["n_dates"] == 1
    assert out["series"] == [pytest.approx(1.0)]      # 外部值确实喂进了 IC


def test_one_factor_default_source_is_the_payload_path():
    """`values_by_mark=None` ⇒ 走 `factor_values(payloads_by_mark[d0], value_of)`。"""
    payloads = {"d0": {c: {"feats": {"gm_yoy_pp": float(i)}}
                       for i, c in enumerate(_SEL_CODES)}}
    out = _one("gm_yoy_pp", payloads_by_mark=payloads,
               value_of=lambda p: factor.secondary_value(p, "gm_yoy_pp"))
    assert out["value_coverage_p50"] == float(len(_SEL_CODES))
    # 空载荷 ⇒ 覆盖度 0（外部来源与载荷来源不互相顶替）
    assert _one("gm_yoy_pp", payloads_by_mark={},
                value_of=lambda p: factor.secondary_value(p, "gm_yoy_pp")
                )["value_coverage_p50"] == 0.0


def test_one_factor_none_values_never_enter_the_cross_section():
    vals = {"d0": {c: None for c in _SEL_CODES}}
    out = _one("mf_ratio_5d", values_by_mark=vals)
    assert out["value_coverage_p50"] == 0.0
    assert out["n_dates_skipped"] == 1


# ---------------------------------------------------------------------------
# 5. `--factor` 端到端
# ---------------------------------------------------------------------------

def _synth_with(synth, *, gm_yoy_pp: bool = False, money_flow: bool = False):
    if gm_yoy_pp:
        for i, code in enumerate(CODES):
            synth.payloads[code]["feats"]["gm_yoy_pp"] = float(i)
    return synth


def _seed_money_flow(db, synth, extra_codes=()):
    """给 `CODES`（＋`extra_codes`，宇宙**外**的标的）逐日写一条 `ratio_amount`。

    `extra_codes` 用来钉「研究侧取值器的覆盖度按**宇宙成员**裁剪」——
    不裁剪的话 `value_coverage_p50` 会算进宇宙外的标的（真库实测 805 > 800）。
    """
    c = connect(db)
    c.executemany(
        "INSERT INTO money_flow_daily (code, date, ratio_amount, source,"
        " fetched_at, created_at, resp_sha256) VALUES (?,?,?,'t',?,?,'x')",
        [(code, d, 0.1 * (i + 1), NOW, NOW)
         for i, code in enumerate(list(CODES) + list(extra_codes))
         for d in synth.days])
    c.commit()
    c.close()


def _run_selected(monkeypatch, db_path, tmp_path, selected, *, money_flow=False,
                  gm_yoy_pp=False, n_weekdays=600, synth=None, extra_codes=()):
    if synth is None:
        synth = _synth_with(_Synth(_weekdays(n_weekdays)), gm_yoy_pp=gm_yoy_pp)
    _install(monkeypatch, synth)
    db = _synth_db(db_path, synth)
    if money_flow:
        _seed_money_flow(db, synth, extra_codes)
    # 预注册声明的 `factors` = `MAIN_FACTORS` ＋ 去重保序的被选因子（与校验同一条规则）。
    prereg = _prereg(
        tmp_path / "p.md",
        factors=list(factor.MAIN_FACTORS)
        + [n for n in factor.selected_factors(selected)
           if n not in factor.MAIN_FACTORS])
    c = connect(db)
    try:
        return factor.run_factor_ic(c, pool="short", start=START,
                                    end=synth.days[-1], prereg_path=prereg,
                                    factors=list(selected))
    finally:
        c.close()


def test_promoting_a_ctx_factor_keeps_every_existing_reading(
        tmp_db, tmp_path, monkeypatch):
    """升格 = 换 kind：`MAIN_FACTORS` 的读数与缺省路径**逐位相同**，只多新键。"""
    # 两次跑用**同一个** synth（`gm_yoy_pp` 有值的那个），只是 db / 因子名单不同 ——
    # 否则比的是「夹具数据不同」，不是「被选因子把既有读数改了没」。
    synth = _synth_with(_Synth(_weekdays(600)), gm_yoy_pp=True)
    default = _default_report(monkeypatch, tmp_path / "default.db", tmp_path, synth)
    rep = _run_selected(monkeypatch, tmp_path / "selected.db", tmp_path,
                        ["gm_yoy_pp"], synth=synth)
    for name in factor.MAIN_FACTORS:
        assert rep["factors"][name] == default["factors"][name]
    for name in factor.SECONDARY_FACTORS:
        assert rep["secondary"][name] == default["secondary"][name]
    for key in ("clip_diag", "rank_ic_reference", "coverage", "main_factors",
                "secondary_factors", "n_dates_skipped", "n_fwd_fallback"):
        assert rep[key] == default[key], f"既有键 {key} 不许变"


def test_promoted_factor_is_main_kind_and_kept_in_secondary(
        tmp_db, tmp_path, monkeypatch):
    rep = _run_selected(monkeypatch, tmp_db, tmp_path, ["gm_yoy_pp"],
                        gm_yoy_pp=True)
    r = rep["research"]
    assert r["selected"] == ["gm_yoy_pp"]
    assert r["family_size"] == 1
    assert r["family_note"] and r["note"]
    assert set(r["factors"]) == {"gm_yoy_pp"}
    promoted = r["factors"]["gm_yoy_pp"]
    assert promoted["kind"] == "main" and promoted["exploratory"] is False
    assert promoted["verdict"] in ("IC_SIGNIFICANT", "IC_NOT_SIGNIFICANT",
                                   "INCONCLUSIVE", "LOW_COVERAGE")
    assert rep["verdict"] == r["verdict"]
    # 同一条路两种 kind 各出一份读数：secondary 段**不删**（既有键只增不改）
    assert rep["secondary"]["gm_yoy_pp"]["kind"] == "secondary"
    assert rep["secondary"]["gm_yoy_pp"]["verdict"] is None
    # 报告要显式解释这件事
    assert "kind" in r["note"] or "次读数" in r["note"]


def test_research_source_factor_runs_through_the_new_value_provider(
        tmp_db, tmp_path, monkeypatch):
    """新取值器因子走完整 harness：`mf_ratio_5d` 出 verdict、覆盖度取自 `money_flow_daily`。

    用 2100 个交易日（≈419 周期 ⇒ 验证段 ≥ `MIN_VALID_PERIODS`）才可能拿到
    `IC_SIGNIFICANT`；600 个交易日那档只有 36 个验证周期 ⇒ 一律 `INCONCLUSIVE`。
    """
    rep = _run_selected(monkeypatch, tmp_db, tmp_path, ["mf_ratio_5d"],
                        money_flow=True, n_weekdays=N_WEEKDAYS)
    assert rep["selected_factors"] == ["mf_ratio_5d"]
    assert rep["factor_sources"] == {"mf_ratio_5d": "research"}
    assert rep["factor_tag"] == "mf_ratio_5d"
    f = rep["research"]["factors"]["mf_ratio_5d"]
    assert f["kind"] == "main" and f["verdict"] == "IC_SIGNIFICANT"
    assert f["n_validate"] >= 120
    assert f["value_coverage_p50"] == float(len(CODES))
    assert "mf_ratio_5d" not in rep["secondary"]
    assert rep["verdict"] == "IC_SIGNIFICANT"
    assert "research" in rep["research"]["note"] and "只读" in rep["research"]["note"]


def test_research_coverage_is_clipped_to_the_universe(tmp_db, tmp_path, monkeypatch):
    """覆盖度按**宇宙成员**裁剪：宇宙外标的的 `money_flow_daily` 行不算进 `ctx` 口径。

    真库冒烟实测：不裁剪时 `value_coverage_p50 = 805`，而宇宙是 800 只 —— 与 `ctx`
    那一路（载荷本来就只有宇宙成员）不同规，并会放松 `LOW_COVERAGE` 闸门。
    """
    outside = ("999998", "999999")
    rep = _run_selected(monkeypatch, tmp_path / "u.db", tmp_path, ["mf_ratio_5d"],
                        money_flow=True, extra_codes=outside)
    f = rep["research"]["factors"]["mf_ratio_5d"]
    assert f["value_coverage_p50"] == float(len(CODES))     # 不是 40 + 2


def test_selected_factors_are_deduplicated_and_order_preserving(
        tmp_db, tmp_path, monkeypatch):
    rep = _run_selected(monkeypatch, tmp_db, tmp_path,
                        ["inv_days", "gm_yoy_pp", "inv_days"], gm_yoy_pp=True)
    assert rep["research"]["selected"] == ["inv_days", "gm_yoy_pp"]
    assert rep["factor_tag"] == "gm_yoy_pp_inv_days"      # tag 按字典序


def test_multiple_selected_take_the_most_conservative_verdict(
        tmp_db, tmp_path, monkeypatch):
    rep = _run_selected(monkeypatch, tmp_db, tmp_path, ["inv_days", "roe"],
                        gm_yoy_pp=True)
    verdicts = {n: rep["research"]["factors"][n]["verdict"]
                for n in rep["research"]["selected"]}
    assert verdicts["roe"] == "LOW_COVERAGE"              # 夹具里 roe 全 None
    assert rep["research"]["verdict"] == "LOW_COVERAGE"   # 最保守者胜出
    assert rep["verdict"] == "LOW_COVERAGE"
    assert "保守" in rep["research"]["note"]


def test_research_section_shows_up_in_md_and_summary_line(
        tmp_db, tmp_path, monkeypatch):
    rep = _run_selected(monkeypatch, tmp_db, tmp_path, ["gm_yoy_pp"],
                        gm_yoy_pp=True)
    md = factor.render_md(rep)
    assert "## 8. 研究侧被选因子" in md or "研究侧" in md
    assert "gm_yoy_pp" in md
    assert "--factor gm_yoy_pp" in md
    assert "gm_yoy_pp" in factor.summary_line(rep)
    assert "research:" in factor.summary_line(rep)


# ---------------------------------------------------------------------------
# 6. 命名防静默覆盖
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("selected,expected", [
    (["a"], "a"),
    (["b", "a"], "a_b"),
    (["x y"], "x_y"),
    (["a/b", "c.d"], "a_b_c.d"),
])
def test_factor_tag_sanitizes_and_sorts(selected, expected):
    assert factor.factor_tag(selected) == expected


def test_report_stem_is_old_name_when_no_tag():
    assert factor._report_stem({"end": "2026-09-24",
                                "universe_id": "csi300-500"}) == \
        "2026-09-24-factor-ic-csi300-500"
    assert factor._report_stem({"end": "2026-09-24", "universe_id": "csi300-500",
                                "factor_tag": None}) == \
        "2026-09-24-factor-ic-csi300-500"


def test_report_stem_carries_the_tag():
    assert factor._report_stem({"end": "2026-09-24", "universe_id": "csi300-500",
                                "factor_tag": "gm_yoy_pp"}) == \
        "2026-09-24-factor-ic-csi300-500-gm_yoy_pp"


def test_cli_writes_the_tagged_file_and_names_it_in_the_report(
        tmp_db, tmp_path, monkeypatch):
    synth = _synth_with(_Synth(_weekdays(600)), gm_yoy_pp=True)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md",
                     factors=list(factor.MAIN_FACTORS) + ["gm_yoy_pp"])
    out = tmp_path / "out"
    rc = main(["research", "factor-ic", "--pool", "short", "--start", START,
               "--end", synth.days[-1], "--universe", "seed21",
               "--prereg", str(prereg), "--out", str(out), "--db", str(db),
               "--factor", "gm_yoy_pp"])
    assert rc == 0
    tagged = out / f"{synth.days[-1]}-factor-ic-seed21-gm_yoy_pp.json"
    assert tagged.is_file()
    assert not (out / f"{synth.days[-1]}-factor-ic-seed21.json").exists()
    rep = json.loads(tagged.read_text(encoding="utf-8"))
    assert rep["factor_tag"] == "gm_yoy_pp"
    assert rep["selected_factors"] == ["gm_yoy_pp"]


# ---------------------------------------------------------------------------
# 7. 预注册 fail-closed（未登记名 / 未进预注册名）
# ---------------------------------------------------------------------------

def _cli_args(tmp_db, out, prereg, end, *extra):
    return ["research", "factor-ic", "--pool", "short", "--start", START,
            "--end", end, "--universe", "seed21", "--prereg", str(prereg),
            "--out", str(out), "--db", str(tmp_db), *extra]


def test_unregistered_factor_name_exits_2_with_zero_output(
        tmp_db, tmp_path, monkeypatch):
    synth = _Synth(_weekdays(600))
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    out = tmp_path / "out"
    rc = main(_cli_args(db, out, prereg, synth.days[-1], "--factor", "foo"))
    assert rc == 2
    assert not out.exists()


def test_registered_but_undeclared_factor_exits_2_with_zero_output(
        tmp_db, tmp_path, monkeypatch):
    """`mf_ratio_5d` 已登记，但预注册（`factors`/`secondary_factors`）里没有它 ⇒ 拒跑。"""
    synth = _Synth(_weekdays(600))
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    before = sorted(p.name for p in factor.default_out_dir().glob("*")) \
        if factor.default_out_dir().exists() else []
    rc = main(["research", "factor-ic", "--pool", "short", "--start", START,
               "--end", synth.days[-1], "--universe", "seed21",
               "--prereg", str(prereg), "--db", str(db),
               "--factor", "mf_ratio_5d"])
    assert rc == 2
    after = sorted(p.name for p in factor.default_out_dir().glob("*")) \
        if factor.default_out_dir().exists() else []
    assert after == before, "exit 2 必须零产物"


def test_selected_factor_must_be_declared_in_the_prereg(
        tmp_db, tmp_path, monkeypatch):
    """预注册 `factors` 少了被选因子 ⇒ `run_factor_ic` 直接拒（fail-closed 链复用）。"""
    synth = _synth_with(_Synth(_weekdays(600)), gm_yoy_pp=True)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")           # 只有 MAIN_FACTORS
    c = connect(db)
    try:
        with pytest.raises(xsec.PreregError, match="factors"):
            factor.run_factor_ic(c, pool="short", start=START,
                                 end=synth.days[-1], prereg_path=prereg,
                                 factors=["gm_yoy_pp"])
    finally:
        c.close()
