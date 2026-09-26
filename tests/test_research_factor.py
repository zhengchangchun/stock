"""P83 T5：因子级 IC 分解（`research factor-ic`）的可用例。

五组：

1. **D4 一致性** —— 同一 ctx 上 `reconstruct_score(factor_inputs[code])` 必须
   **逐位等于**打分插桩实际返回的 `score`。夹具里的插桩1 是**现役 v1.1.0 的逐字
   源文本**（`source_sha256` 用用例钉住），所以这不是「拿自己的实现对自己」。
2. **`want_factors=False` 逐位不变** —— 默认路径的产物与 HEAD 版本**逐位相同**
   （黄金 digest 由改动前的 `stocklab/candidate/run.py` 实测得出，写死在用例里）；
   打开开关也不许动 `members`/`rejects`/`params`/`eligible`/`scored`。
3. **裁剪诊断** —— 构造被裁满的夹具，核对计数与 P50（**只报不判**）。
4. **IC 复用同值** —— 本模块的每日 IC 与 `signal` 的既有实现同值；`factor.py`
   源码守卫（门槛 import、零 ML 依赖、无手抄数字）。
5. **预注册 fail-closed** —— 字段/值不符 ⇒ `PreregError`；CLI ⇒ exit 2、零输出；
   仓库那份预注册的键必须与 `factor.PREREG_FIELDS` **逐字一致**。

⚠️ 与 `rank-ic` 一样，「两次跑逐位相同」的确定性断言**排除三个耗时键**。
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from stocklab.candidate import pools as candidate_pools
from stocklab.candidate import replay
from stocklab.candidate.run import PipelineResult, score_pipeline
from stocklab.cli.main import main
from stocklab.config.replay import REBALANCE_DAYS
from stocklab.config.universe import Instrument
from stocklab.data import adjust
from stocklab.data.models import Bar
from stocklab.plugin import lifecycle, sandbox, store
from stocklab.research import factor, signal, xsec
from stocklab.store.db import connect
from tests.test_research_signal import _weekdays
from tests.test_research_xsec import NOW, _seed_pipeline_db

START = "2015-01-01"
REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_PREREG = (REPO_ROOT / "docs" / "experiments"
               / "2026-09-26-factor-ic-short-csi300-500.md")

#: 现役短池打分插桩（`plugin_id='1'` / `script_id=7` / v1.1.0）的**逐字源文本**，
#: 真库只读读出；sha256 与真库 `plugin_scripts.source_sha256` 相同（用例钉住）。
#: 把它当夹具用，才有可能证「本站算的因子 = 插桩用的因子」——用别的假插桩证不了。
DEPLOYED_SHORT_SOURCE = '''def run(ctx):
    bars = ctx["bars"]
    if len(bars) < 20:
        return {"score": 0.0, "pass_flag": False,
                "reason": "K 线不足 20 根，短期因子无法计算", "risk_list": []}

    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]

    # 20 日动量（权重不变）
    mom = (closes[-1] - closes[-20]) / closes[-20] if closes[-20] else 0.0
    # 量比：最近 5 日均量 / 前 20 日均量（v1.1.0：分母改用前15日均量，对近期缩量更敏感）
    v5 = sum(vols[-5:]) / 5.0
    v15 = sum(vols[-20:-5]) / 15.0 if len(vols) >= 20 else v5
    vol_ratio = (v5 / v15) if v15 else 1.0

    # v1.1.0：动量权重从 200 → 150，量比权重从 20 → 30（偏向量能信号）
    score = 50.0 + mom * 150.0 + (vol_ratio - 1.0) * 30.0
    score = max(0.0, min(100.0, score))

    risks = []
    if vol_ratio > 2.0:
        risks.append("放量异常（量比 %.2f）" % vol_ratio)
    if mom < -0.10:
        risks.append("20 日跌幅超过 10%")

    return {"score": score, "pass_flag": True,
            "reason": "20 日动量 %.2f%%，量比 %.2f（v1.1.0 量能权重）" % (mom * 100, vol_ratio),
            "risk_list": risks}
'''

DEPLOYED_SHORT_SHA = \
    "558a6a08b62e508c9355c607d29217269c5897cfc374cefdb15cd2fc42c2fe0e"

#: `want_factors=False` 时五个原字段（members/rejects/params/eligible/scored）的
#: 黄金 digest —— 由 **HEAD `5c720eb` 的源码导出**（`git archive HEAD | tar -x`）在
#: 同一个夹具上实测得出，不是「跑一遍现在的实现再把输出抄下来」：
#: `/tmp/p83/digest.py`（内容见任务书 §7）在 HEAD 导出与工作区各跑一次，
#: 两次数值**逐位相同** `02b20b5f…`。从此任何对默认路径的污染都会让它变红。
FALSE_PATH_DIGEST = \
    "02b20b5f4eb90b35fd0a1558edc31be5b7ec7cad956d11cbd791c31158a84671"

N_CODES = 40
CODES = [f"{600000 + i:06d}" for i in range(N_CODES)]
N_WEEKDAYS = 2100
_TIMING_KEYS = ("elapsed_s", "scan_s", "fwd_load_s")


def _stable(report: dict) -> dict:
    return {k: v for k, v in report.items() if k not in _TIMING_KEYS}


def _digest(res: PipelineResult) -> str:
    """五个**原字段**的确定性 digest（`factor_inputs` 刻意不进 —— 那是只增字段）。"""
    blob = {"members": [dataclasses.asdict(m) for m in res.members],
            "rejects": [dataclasses.asdict(r) for r in res.rejects],
            "params": res.params, "eligible": res.eligible, "scored": res.scored}
    return hashlib.sha256(json.dumps(
        blob, sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")).hexdigest()


def _prereg(path: Path, **over) -> Path:
    """写一份合法预注册（字段值一律从代码/sandbox **读出**，不手抄）。"""
    data = {"experiment": factor.EXPERIMENT, "pool": "short", "start": START,
            "universe": "seed21",
            "factors": list(factor.MAIN_FACTORS),
            "secondary_factors": list(factor.SECONDARY_FACTORS),
            "ic_type": factor.IC_TYPE, "n_layers": factor.N_LAYERS,
            "horizon": REBALANCE_DAYS["short"],
            "min_periods": sandbox.MIN_VALID_PERIODS,
            "min_xsec_n": factor.MIN_XSEC_N,
            "bootstrap_n": sandbox._BOOTSTRAP_N,
            "bootstrap_seed": sandbox._BOOTSTRAP_SEED,
            "rule": "逐因子独立判：95% CI 不含 0 ⇒ IC_SIGNIFICANT；含 0 ⇒ "
                    "IC_NOT_SIGNIFICANT；有效日期 < MIN_VALID_PERIODS ⇒ INCONCLUSIVE"}
    data.update(over)
    path.write_text("# 夹具预注册（factor-ic）\n\n```json\n"
                    + json.dumps(data, ensure_ascii=False) + "\n```\n",
                    encoding="utf-8")
    return path


def _counts(db) -> dict[str, int]:
    c = connect(db)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("candidate_snapshots", "plugin_scripts",
                          "paper_agent_decisions", "paper_nav_daily")}
    finally:
        c.close()


def _install_deployed_short_plugin(db) -> int:
    """把插桩1 的 active 版本换成**现役 v1.1.0 的逐字源文本**（approve 成 active）。"""
    c = connect(db)
    try:
        sid = store.insert_script(c, plugin_id="1", version="1.1.0",
                                  source_text=DEPLOYED_SHORT_SOURCE, note=None,
                                  now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
        c.commit()
    finally:
        c.close()
    return sid


# ---------------------------------------------------------------------------
# 1. D4 一致性：本站算的因子 == 插桩用的因子
# ---------------------------------------------------------------------------

def test_deployed_source_fixture_matches_real_plugin_sha():
    """夹具里那段源文本必须**逐字**是真库的现役插桩（否则证不了任何事）。"""
    assert hashlib.sha256(DEPLOYED_SHORT_SOURCE.encode("utf-8")).hexdigest() == \
        DEPLOYED_SHORT_SHA
    assert factor.SCORING_SCRIPT["source_sha256"] == DEPLOYED_SHORT_SHA


def test_factor_inputs_reconstructs_the_plugin_score_bitwise(tmp_db):
    """**D4 的核心用例**：同一 ctx 上重建的分数 == 插桩实际返回的 `score`。"""
    days = _seed_pipeline_db(tmp_db, n_weekdays=300)
    _install_deployed_short_plugin(tmp_db)
    c = connect(tmp_db)
    try:
        res = score_pipeline(c, asof=days[280], want_factors=True)
    finally:
        c.close()

    rows = {r["code"]: r for r in res.scored["short"]}
    assert rows, "夹具没产出任何短池打分行，用例失去意义"
    # factor_inputs 的键集合恒等于短池已打分标的集合
    assert set(res.factor_inputs) == set(rows)
    for code, payload in res.factor_inputs.items():
        assert factor.reconstruct_score(payload) == rows[code]["raw_score"]
    # 载荷形状（D3）
    for payload in res.factor_inputs.values():
        assert set(payload) == {"closes", "volumes", "feats"}
        assert len(payload["closes"]) == 20 and len(payload["volumes"]) == 20
        assert set(payload["feats"]) == set(factor.SECONDARY_FACTORS) | {"period"}


def test_mom20_and_vr15_hand_computed():
    """手算核对（不喂实现自己的输出）：`mom20`/`vr15`/`reconstruct_score`。"""
    closes = [100.0] * 19 + [110.0]
    volumes = [1000.0] * 15 + [2000.0] * 5
    payload = {"closes": closes, "volumes": volumes, "feats": {}}
    assert factor.mom20(payload) == pytest.approx(0.10)
    assert factor.vr15(payload) == pytest.approx(2.0)
    # 50 + 150×0.10 + 30×(2.0−1) = 95.0
    assert factor.reconstruct_score(payload) == pytest.approx(95.0)
    # 裁剪：超过 100 夹到 100
    hi = {"closes": [100.0] * 19 + [200.0], "volumes": [1000.0] * 15 + [4000.0] * 5,
          "feats": {}}
    assert factor.reconstruct_score(hi) == 100.0
    lo = {"closes": [100.0] * 19 + [10.0], "volumes": [1000.0] * 15 + [1.0] * 5,
          "feats": {}}
    assert factor.reconstruct_score(lo) == 0.0


def test_mom20_vr15_need_twenty_bars():
    """不足 20 根 ⇒ 取不到（`None`，不是 0）—— 与插桩的 `len(bars) < 20` 对齐。"""
    short = {"closes": [1.0] * 19, "volumes": [1.0] * 19, "feats": {}}
    assert factor.mom20(short) is None
    assert factor.vr15(short) is None
    assert factor.reconstruct_score(short) is None


def test_secondary_value_none_is_not_zero():
    p = {"closes": [], "volumes": [],
         "feats": {"roe": None, "gross_margin": 0.0, "period": "2014Q4"}}
    assert factor.secondary_value(p, "roe") is None
    assert factor.secondary_value(p, "gross_margin") == 0.0


def test_secondary_factor_names_match_the_payload_shape():
    """`SECONDARY_FACTORS` 与 `candidate/run.py::FACTOR_FEATURE_KEYS` 必须同源。"""
    from stocklab.candidate.run import FACTOR_FEATURE_KEYS
    assert factor.SECONDARY_FACTORS == FACTOR_FEATURE_KEYS
    assert factor.SECONDARY_FACTORS == ("roe", "gross_margin", "gm_yoy_pp",
                                        "inv_days", "fcf_margin")
    assert factor.MAIN_FACTORS == ("mom20", "vr15")


# ---------------------------------------------------------------------------
# 2. `want_factors=False`：默认路径逐位不变
# ---------------------------------------------------------------------------

def test_default_path_digest_is_unchanged_from_head(tmp_db):
    """黄金 digest：默认路径的五个原字段与**改动前**一致（literal 来自 HEAD 实测）。"""
    days = _seed_pipeline_db(tmp_db, n_weekdays=300)
    c = connect(tmp_db)
    try:
        res = score_pipeline(c, asof=days[280])
    finally:
        c.close()
    assert res.factor_inputs == {}
    assert _digest(res) == FALSE_PATH_DIGEST


def test_want_factors_flag_does_not_move_any_existing_field(tmp_db):
    """打开开关只多一个字段 —— `members`/`rejects`/`params`/`eligible`/`scored` 不动。"""
    days = _seed_pipeline_db(tmp_db, n_weekdays=300)
    c = connect(tmp_db)
    try:
        base = score_pipeline(c, asof=days[280])
        on = score_pipeline(c, asof=days[280], want_factors=True)
    finally:
        c.close()
    assert base.members == on.members and base.rejects == on.rejects
    assert base.params == on.params and base.eligible == on.eligible
    assert base.scored == on.scored
    assert base.factor_inputs == {} and on.factor_inputs != {}
    assert set(on.factor_inputs) == set(on.eligible["short"])


def test_pipeline_result_field_order_and_defaults_are_only_additive():
    names = tuple(f.name for f in dataclasses.fields(PipelineResult))
    assert names == ("members", "rejects", "params", "eligible", "scored",
                     "factor_inputs")
    assert PipelineResult(members=[], rejects=[], params={}).factor_inputs == {}
    assert inspect.signature(score_pipeline).parameters["want_factors"].default \
        is False


def test_params_keys_unchanged(tmp_db):
    """`params` 的键集一字未增（`factor_inputs` 不进 params）。"""
    days = _seed_pipeline_db(tmp_db, n_weekdays=300)
    c = connect(tmp_db)
    try:
        res = score_pipeline(c, asof=days[280], want_factors=True)
    finally:
        c.close()
    assert set(res.params) == {"seed_count", "universe_id", "members_sha256",
                               "topn", "scoring_price_mode", "n_adj_fallback"}


# ---------------------------------------------------------------------------
# 3. 裁剪诊断（只报不判）
# ---------------------------------------------------------------------------

def test_clip_diag_counts_and_p50():
    scored = {
        "2020-01-06": [{"code": "a", "raw_score": 0.0},
                       {"code": "b", "raw_score": 100.0},
                       {"code": "c", "raw_score": 50.0}],
        "2020-01-13": [{"code": "a", "raw_score": 100.0},
                       {"code": "b", "raw_score": 100.0},
                       {"code": "c", "raw_score": 100.0}],
        "2020-01-20": [{"code": "a", "raw_score": 60.0},
                       {"code": "b", "raw_score": 61.0},
                       {"code": "c", "raw_score": 62.0}],
    }
    d = factor.clip_diag(scored)
    assert d["n_dates"] == 3
    assert d["ratio_p50"] == pytest.approx(2 / 3)      # 三个比例 2/3、1.0、0.0
    assert d["ratio_min"] == 0.0 and d["ratio_max"] == 1.0
    assert d["n_clipped_p50"] == 2.0 and d["n_clipped_max"] == 3
    assert d["xsec_p50"] == 3.0
    assert "只报不判" in d["rule"]


def test_clip_diag_empty_is_none_not_zero():
    d = factor.clip_diag({})
    assert d["n_dates"] == 0 and d["ratio_p50"] is None and d["xsec_p50"] is None


def test_clip_diag_ignores_rows_without_score():
    d = factor.clip_diag({"d": [{"code": "a", "raw_score": None},
                                {"code": "b", "raw_score": 100.0}]})
    assert d["n_dates"] == 1 and d["ratio_p50"] == 1.0 and d["xsec_p50"] == 1.0


# ---------------------------------------------------------------------------
# 4. IC / 分层 / bootstrap 复用 signal 的既有实现
# ---------------------------------------------------------------------------

def test_pure_functions_are_the_signal_implementation():
    """本模块**不另写**相关/分层实现：名字就是 `signal` 的那些对象。"""
    assert factor.MIN_XSEC_N is signal.MIN_XSEC_N
    assert factor.N_LAYERS == signal.N_LAYERS
    assert factor.IC_TYPE == signal.IC_TYPE
    assert factor.MIN_START == signal.MIN_START
    assert factor.ONLY_POOL == signal.ONLY_POOL
    assert factor.PreregError is not xsec.PreregError
    assert issubclass(factor.PreregError, xsec.PreregError)


def test_signal_public_helpers_still_give_golden_values():
    """抽公共 helper（P83 只改名）之后，`signal` 的读数**逐位不变**。"""
    scores = {f"c{i:02d}": float(i) for i in range(40)}
    fwd = {f"c{i:02d}": 0.001 * (i * 3 + 7) for i in range(40)}
    assert signal.spearman_ic(scores, fwd) == 1.0
    tied = {"a": 3.0, "b": 3.0, "c": 1.0, "d": 2.0}
    assert signal.spearman_ic(tied, {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}) == \
        pytest.approx(-0.7378647873726218, abs=1e-12)
    assert signal.assign_layers([f"c{i:02d}" for i in range(32)],
                                {f"c{i:02d}": float(i) for i in range(32)}, 5) == {
        1: [f"c{i:02d}" for i in range(31, 24, -1)],
        2: [f"c{i:02d}" for i in range(24, 17, -1)],
        3: [f"c{i:02d}" for i in range(17, 11, -1)],
        4: [f"c{i:02d}" for i in range(11, 5, -1)],
        5: [f"c{i:02d}" for i in range(5, -1, -1)]}
    assert signal.ascending_steps([0.03, 0.0]) == 1
    assert signal.ForwardPrices is not None and signal.ic_stats is not None
    # `tail_mean` = 按周期序号 70/30 切分后的验证段均值（4 条 ⇒ 验 2 条）
    assert signal.tail_mean([0.1, 0.2, 0.3, 0.4]) == pytest.approx(0.35)


def test_factor_module_is_stdlib_only_and_imports_its_thresholds():
    src = (REPO_ROOT / "stocklab" / "research" / "factor.py").read_text(
        encoding="utf-8")
    assert "import numpy" not in src and "import scipy" not in src
    assert "import pandas" not in src
    assert "stocklab.paper" not in src and "stocklab.m2" not in src
    assert "MIN_VALID_PERIODS = 120" not in src
    assert "2000" not in src and "20260918" not in src
    assert "sandbox.MIN_VALID_PERIODS" in src
    assert "signal.MIN_XSEC_N" in src


# ---------------------------------------------------------------------------
# 5. 预注册 fail-closed
# ---------------------------------------------------------------------------

def test_repo_prereg_field_list_is_verbatim_identical():
    """仓库那份预注册 json 的**键集合**必须与 `PREREG_FIELDS` 逐字一致（D6）。"""
    data, sha = factor.load_prereg(REPO_PREREG)
    assert set(data) == set(factor.PREREG_FIELDS)
    assert data["experiment"] == "factor-ic"
    assert data["universe"] == "csi300-500"
    assert data["factors"] == list(factor.MAIN_FACTORS)
    assert data["secondary_factors"] == list(factor.SECONDARY_FACTORS)
    assert data["min_xsec_n"] == factor.MIN_XSEC_N
    assert data["horizon"] == REBALANCE_DAYS["short"]
    factor.validate_prereg(data, pool="short", start=data["start"],
                           horizon=REBALANCE_DAYS["short"],
                           universe="csi300-500")
    assert len(sha) == 64


def test_repo_prereg_json_matches_the_committed_task_doc_numbers():
    """并排引用的 P78 数字必须与预注册 §4.4 写下的 4 位小数一致（对不上就是抄错）。"""
    ref = factor.RANK_IC_REFERENCE
    assert round(ref["mean_validate"], 4) == -0.0158
    assert round(ref["ci_low"], 4) == -0.0458
    assert round(ref["ci_high"], 4) == 0.0143
    assert ref["verdict"] == "IC_NOT_SIGNIFICANT"
    _data, _sha = factor.load_prereg(REPO_PREREG)
    text = REPO_PREREG.read_text(encoding="utf-8")
    assert "−0.0158" in text and "−0.0458" in text and "+0.0143" in text


@pytest.mark.parametrize("field,value", [
    ("experiment", "rank-ic"), ("pool", "mid"), ("start", "2019-01-01"),
    ("universe", "csi300-500"), ("factors", ["mom20"]),
    ("secondary_factors", ["roe"]), ("ic_type", "pearson"), ("n_layers", 4),
    ("horizon", 10), ("min_periods", 30), ("min_xsec_n", 10),
    ("bootstrap_n", 100), ("bootstrap_seed", 1),
])
def test_validate_prereg_rejects_any_field_mismatch(tmp_path, field, value):
    data, _sha = factor.load_prereg(_prereg(tmp_path / "p.md", **{field: value}))
    with pytest.raises(xsec.PreregError, match=field):
        factor.validate_prereg(data, pool="short", start=START,
                               horizon=REBALANCE_DAYS["short"],
                               universe="seed21")


@pytest.mark.parametrize("field", ["factors", "secondary_factors", "min_xsec_n"])
def test_load_prereg_rejects_missing_own_fields(tmp_path, field):
    """本站多出来的三个键**缺一个即拒**（`signal` 的子集检查兜不住它们）。"""
    p = _prereg(tmp_path / "p.md")
    data = json.loads(p.read_text(encoding="utf-8").split("```json\n")[1]
                      .split("\n```")[0])
    del data[field]
    p.write_text("```json\n" + json.dumps(data, ensure_ascii=False) + "\n```\n",
                 encoding="utf-8")
    with pytest.raises(xsec.PreregError, match="缺字段"):
        factor.load_prereg(p)


def test_load_prereg_rejects_missing_universe(tmp_path):
    """本站在 `universe` 上是**必填**（与 `rank-ic` 的「缺席 ⇒ seed21」不同）。"""
    p = _prereg(tmp_path / "p.md")
    data = json.loads(p.read_text(encoding="utf-8").split("```json\n")[1]
                      .split("\n```")[0])
    del data["universe"]
    p.write_text("```json\n" + json.dumps(data, ensure_ascii=False) + "\n```\n",
                 encoding="utf-8")
    with pytest.raises(xsec.PreregError, match="缺字段"):
        factor.load_prereg(p)


def test_load_prereg_propagates_io_and_parse_errors(tmp_path):
    with pytest.raises(xsec.PreregError, match="读不到"):
        factor.load_prereg(tmp_path / "nope.md")
    p = tmp_path / "p.md"
    p.write_text("# 没有 json 块\n", encoding="utf-8")
    with pytest.raises(xsec.PreregError, match="json"):
        factor.load_prereg(p)


# ---------------------------------------------------------------------------
# 6. 端到端：合成数据（monkeypatch 打分内核与前向收益，**不碰真库**）
# ---------------------------------------------------------------------------

class _Synth:
    """合成跑的全部输入。因子值与分数**由同一个载荷算出**（D4 的一致性在夹具里也成立）。"""

    def __init__(self, days: list[str], *, short_closes: bool = False,
                 roe: float | None = None) -> None:
        self.days = days
        self.marks = replay.rebalance_dates(
            days, period=REBALANCE_DAYS["short"], start=days[0], end=days[-1])
        self.payloads: dict[str, dict] = {}
        self.closes: dict[str, list[float]] = {}
        for i, code in enumerate(CODES):
            # mom20 = 0.001×i（升序）；vr15 = 1 + 0.002×i（升序）⇒ 分数也升序
            closes = [100.0] * 18 + [100.0, 100.0 * (1.0 + 0.001 * i)]
            if short_closes:
                closes = closes[1:]
            self.payloads[code] = {
                "closes": closes,
                "volumes": [1000.0] * 15 + [1000.0 * (1.0 + 0.002 * i)] * 5,
                "feats": {"roe": roe, "gross_margin": None, "gm_yoy_pp": None,
                          "inv_days": float(i), "fcf_margin": None,
                          "period": "2014Q4"}}
            # 前向收益与 i 同序（g_i 递增）⇒ 与两个因子都同序
            self.closes[code] = [100.0 * (1.0 + 0.0005 * i) ** k
                                 for k in range(len(self.marks))]

    def scores(self) -> dict[str, float]:
        return {c: factor.reconstruct_score(p)
                for c, p in self.payloads.items()}


def _install(monkeypatch, synth: _Synth) -> None:
    members = tuple(Instrument(code=c, name=c, market="sh", board="main")
                    for c in CODES)

    def _fake_resolve(universe_id):
        return "seed21", members, "0" * 64

    def _fake_score_pipeline(conn, *, asof, universe=None, want_factors=False,
                             **_kw):
        scores = synth.scores()
        rows = [{"code": c, "pool": "short", "raw_score": scores[c],
                 "adj_score": scores[c], "reason": "合成", "risk_json": "[]"}
                for c in CODES]
        scored = {p: [] for p in candidate_pools.ALL_POOLS}
        scored["short"] = rows
        return PipelineResult(members=[], rejects=[], params={},
                              eligible={"short": sorted(CODES)}, scored=scored,
                              factor_inputs=(dict(synth.payloads)
                                             if want_factors else {}))

    def _fake_load_bars_adjusted(conn, code, as_of, *, start=None):
        # `closes` 里没有的 code ⇒ 空序列 ⇒ 该 (日期, code) 取不到前向收益
        return [Bar(code=code, date=m, open=p, high=p, low=p, close=p,
                    volume=1000, amount=None, turnover=None, source="synth")
                for m, p in zip(synth.marks, synth.closes.get(code, []))]

    monkeypatch.setattr(factor, "resolve_universe", _fake_resolve)
    monkeypatch.setattr("stocklab.candidate.run.score_pipeline",
                        _fake_score_pipeline)
    monkeypatch.setattr(adjust, "load_bars_adjusted", _fake_load_bars_adjusted)


def _synth_db(tmp_db, synth: _Synth):
    """只有日历 ＋ 逐 code 一条 `corp_actions` 的库（让 `ForwardPrices` 走真复权分支）。"""
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'t',?)",
                  [(d, NOW) for d in synth.days])
    c.executemany("INSERT INTO corp_actions (code, cqr, content, source,"
                  " first_seen, last_seen) VALUES (?,?,'10派1元','t',?,?)",
                  [(code, synth.days[0], NOW, NOW) for code in CODES])
    c.commit()
    c.close()
    return tmp_db


def _run(monkeypatch, tmp_db, tmp_path, synth: _Synth, *extra):
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "prereg.md")
    out = tmp_path / "out"
    rc = main(["research", "factor-ic", "--pool", "short", "--start", START,
               "--end", synth.days[-1], "--universe", "seed21",
               "--prereg", str(prereg), "--out", str(out), "--db", str(db),
               *extra])
    return rc, out


def _run_inproc(monkeypatch, tmp_db, tmp_path, synth: _Synth) -> dict:
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    c = connect(db)
    try:
        return factor.run_factor_ic(c, pool="short", start=START,
                                    end=synth.days[-1],
                                    prereg_path=_prereg(tmp_path / "p.md"))
    finally:
        c.close()


def test_reconstruct_is_not_tautological_in_the_fixture():
    """夹具的分数是**手算值**：`50 + 150×0.001i + 30×0.002i`，不是把它自己喂给自己。"""
    synth = _Synth(_weekdays(60))
    scores = synth.scores()
    assert scores[CODES[0]] == pytest.approx(50.0)
    assert scores[CODES[39]] == pytest.approx(50.0 + 0.15 * 39 + 0.06 * 39)
    assert synth.payloads[CODES[39]]["closes"][-1] == pytest.approx(103.9)


def test_run_factor_ic_perfect_signal_is_significant(tmp_db, tmp_path, monkeypatch):
    """因子与前向收益同序 ⇒ 每日 IC=1.0 ⇒ 两个主因子都 `IC_SIGNIFICANT`。"""
    synth = _Synth(_weekdays(N_WEEKDAYS))
    report = _run_inproc(monkeypatch, tmp_db, tmp_path, synth)

    assert report["experiment"] == "factor-ic"
    assert report["n_periods"] == len(synth.marks) - 1
    for name in ("mom20", "vr15"):
        f = report["factors"][name]
        assert f["n_dates"] == len(synth.marks) - 1
        assert all(ic == pytest.approx(1.0) for ic in f["series"])
        assert f["mean_validate"] == pytest.approx(1.0)
        assert f["verdict"] == "IC_SIGNIFICANT"
        assert f["n_validate"] >= sandbox.MIN_VALID_PERIODS
        assert f["low_coverage"] is False
        assert f["n_dates_skipped"] == 0
        # 分层：第 1 层（最高分）平均前向收益最高 ⇒ 4 个上升步
        means = [f["layers"]["mean_by_layer"][str(k)] for k in range(1, 6)]
        assert means == sorted(means, reverse=True)
        assert f["layers"]["n_ascending_steps"] == 4
        assert f["layers"]["spread"]["mean"] > 0
        assert f["layers"]["size_p50_by_layer"] == {str(k): 8.0 for k in range(1, 6)}
    # 次读数：roe 全 None ⇒ 覆盖度不足 ⇒ 标 LOW_COVERAGE（这就是它的标签，非判据本身）；
    # inv_days 有值、只报读数 ⇒ verdict 一律 None
    assert report["secondary"]["roe"]["low_coverage"] is True
    assert report["secondary"]["roe"]["coverage_flag"] == "LOW_COVERAGE"
    assert report["secondary"]["roe"]["verdict"] == "LOW_COVERAGE"
    assert report["secondary"]["inv_days"]["low_coverage"] is False
    assert report["secondary"]["inv_days"]["coverage_flag"] is None
    assert report["secondary"]["inv_days"]["verdict"] is None
    assert report["secondary"]["inv_days"]["mean_validate"] == pytest.approx(1.0)
    assert report["secondary"]["inv_days"]["exploratory"] is True
    # 覆盖度与裁剪诊断
    assert report["n_dates_skipped"] == {"mom20": 0, "vr15": 0}
    assert report["n_fwd_fallback"] == 0
    assert report["coverage"]["xsec_size_p50"] == float(N_CODES)
    assert report["coverage"]["n_no_fwd_ret"] == 0
    assert report["clip_diag"]["ratio_max"] == 0.0      # 分数 50~58，裁不到边界
    assert report["rank_ic_reference"]["verdict"] == "IC_NOT_SIGNIFICANT"


def test_run_factor_ic_report_keys_are_complete(tmp_db, tmp_path, monkeypatch):
    """T4 点名的键一个都不能少（只增键）。"""
    synth = _Synth(_weekdays(600))
    report = _run_inproc(monkeypatch, tmp_db, tmp_path, synth)
    assert set(report) >= {
        "experiment", "pool", "start", "end", "universe_id", "prereg_sha256",
        "factors", "secondary", "clip_diag", "rank_ic_reference",
        "n_fwd_fallback", "n_dates_skipped", "coverage",
        "main_factors", "secondary_factors", "scoring_script"}
    assert set(report["factors"]) == set(factor.MAIN_FACTORS)
    assert set(report["secondary"]) == set(factor.SECONDARY_FACTORS)
    for name in factor.MAIN_FACTORS:
        f = report["factors"][name]
        assert set(f) >= {"factor", "kind", "exploratory", "low_coverage",
                          "n_dates", "n_validate", "mean_all", "mean_train",
                          "mean_validate", "ci_low", "ci_high", "verdict",
                          "overfit_flag", "n_dates_skipped", "layers",
                          "value_coverage_p50", "xsec_size_p50"}
        assert f["exploratory"] is False
    assert report["clip_diag"]["rule"] and report["rank_ic_reference"]["note"]


def test_run_factor_ic_thin_cross_section_is_skipped_not_zero(
        tmp_db, tmp_path, monkeypatch):
    """「有因子值 ∩ 有有效前向收益」< `MIN_XSEC_N` ⇒ 该日记跳过并计数
    （**不拿 0 顶替**），verdict `INCONCLUSIVE`（样本不足 ≠ 没效果）。

    构造：40 只有因子值（覆盖度够 ⇒ 不是 `LOW_COVERAGE`），但只有 25 只有前向
    收益 ⇒ 每个周期的可用截面都是 25 < 30。
    """
    days = _weekdays(N_WEEKDAYS)
    synth = _Synth(days)
    synth.closes = {c: synth.closes[c] for c in CODES[:25]}
    report = _run_inproc(monkeypatch, tmp_db, tmp_path, synth)
    for name in factor.MAIN_FACTORS:
        f = report["factors"][name]
        assert f["series"] == [] and f["n_dates"] == 0
        assert f["verdict"] == "INCONCLUSIVE"
        assert f["n_dates_skipped"] == report["n_periods"]
        assert f["low_coverage"] is False          # 覆盖度够：40 只都有因子值
        assert f["value_coverage_p50"] == float(N_CODES)
        assert f["xsec_size_max"] == 25
    assert report["n_dates_skipped"] == {"mom20": report["n_periods"],
                                         "vr15": report["n_periods"]}
    assert report["n_fwd_fallback"] == 0
    assert report["coverage"]["n_no_fwd_ret"] == 15 * report["n_periods"]


def test_run_factor_ic_low_coverage_blocks_verdict(tmp_db, tmp_path, monkeypatch):
    """主因子取不到值（覆盖度不足）⇒ 标 `LOW_COVERAGE`、**不出 verdict**。"""
    days = _weekdays(600)
    synth = _Synth(days, short_closes=True)
    report = _run_inproc(monkeypatch, tmp_db, tmp_path, synth)
    mom = report["factors"]["mom20"]
    assert mom["value_coverage_p50"] == 0.0
    assert mom["low_coverage"] is True
    assert mom["verdict"] == "LOW_COVERAGE"
    assert "不出 verdict" in mom["note"]
    assert mom["n_dates_skipped"] == report["n_periods"]


def test_run_factor_ic_is_deterministic_across_two_runs(
        tmp_db, tmp_path, monkeypatch):
    days = _weekdays(600)
    synth = _Synth(days)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    c = connect(db)
    try:
        a = factor.run_factor_ic(c, pool="short", start=START, end=days[-1],
                                 prereg_path=prereg)
        b = factor.run_factor_ic(c, pool="short", start=START, end=days[-1],
                                 prereg_path=prereg)
    finally:
        c.close()
    assert json.dumps(_stable(a), ensure_ascii=False, sort_keys=True) == \
        json.dumps(_stable(b), ensure_ascii=False, sort_keys=True)


def test_run_factor_ic_rejects_bad_pool_start_and_universe(tmp_db, tmp_path):
    prereg = _prereg(tmp_path / "p.md")
    c = connect(tmp_db)
    try:
        with pytest.raises(xsec.PreregError, match="只跑"):
            factor.run_factor_ic(c, pool="mid", start=START, end=START,
                                 prereg_path=prereg)
        with pytest.raises(xsec.PreregError, match="不得早于"):
            factor.run_factor_ic(c, pool="short", start="2014-12-31", end=START,
                                 prereg_path=prereg)
        with pytest.raises(xsec.PreregError, match="宇宙载入失败"):
            factor.run_factor_ic(c, pool="short", start=START, end=START,
                                 prereg_path=prereg,
                                 universe="no-such-universe")
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------

def test_cli_help_lists_factor_ic(capsys):
    assert main(["research", "--help"]) == 0
    assert "factor-ic" in capsys.readouterr().out
    assert main(["research", "factor-ic", "--help"]) == 0
    assert "--prereg" in capsys.readouterr().out


def test_cli_factor_ic_end_to_end_writes_two_files_and_no_table(
        tmp_db, tmp_path, monkeypatch, capsys):
    days = _weekdays(600)
    synth = _Synth(days)
    rc, out = _run(monkeypatch, tmp_db, tmp_path, synth)
    assert rc == 0
    json_p = out / f"{days[-1]}-factor-ic-seed21.json"
    md_p = out / f"{days[-1]}-factor-ic-seed21.md"
    assert json_p.is_file() and md_p.is_file()
    assert _counts(tmp_db) == {"candidate_snapshots": 0, "plugin_scripts": 0,
                               "paper_agent_decisions": 0, "paper_nav_daily": 0}
    report = json.loads(json_p.read_text(encoding="utf-8"))
    assert report["experiment"] == "factor-ic" and report["pool"] == "short"
    assert report["universe_id"] == "seed21"
    assert report["min_periods"] == sandbox.MIN_VALID_PERIODS
    assert report["min_xsec_n"] == signal.MIN_XSEC_N
    assert report["prereg_sha256"] == hashlib.sha256(
        (tmp_path / "prereg.md").read_bytes()).hexdigest()
    md = md_p.read_text(encoding="utf-8")
    assert md.rstrip().splitlines()[-1].startswith("summary: factor-ic")
    assert factor.summary_line(report) in md
    for item in xsec.NON_PIT_ITEMS:
        assert item in md
    assert signal.REPLAY_RAW_PRICE_NOTE in md
    assert factor.CLIP_DIAG_NOTE in md
    stdout = capsys.readouterr().out
    assert "verdict=" in stdout and "json=" in stdout and "md=" in stdout


def test_cli_factor_ic_bad_inputs_exit_2_with_zero_output(
        tmp_db, tmp_path, monkeypatch):
    days = _weekdays(600)
    synth = _Synth(days)
    _install(monkeypatch, synth)
    _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")

    def _call(out, *extra, **kw):
        return main(["research", "factor-ic", "--pool", kw.get("pool", "short"),
                     "--start", kw.get("start", START), "--end", days[-1],
                     "--universe", kw.get("universe", "seed21"),
                     "--prereg", str(kw.get("prereg", prereg)),
                     "--out", str(out), "--db", str(tmp_db), *extra])

    out = tmp_path / "out"
    assert _call(out, pool="mid") == 2
    assert _call(out, start="2014-12-31") == 2
    assert _call(out, prereg=tmp_path / "nope.md") == 2
    # 预注册说 csi300-500、命令行传 seed21 ⇒ 拒跑
    mism = _prereg(tmp_path / "mism.md", universe="csi300-500")
    assert _call(out, prereg=mism) == 2
    # 字段值被篡改（min_xsec_n）⇒ 拒跑
    bad = _prereg(tmp_path / "bad.md", min_xsec_n=10)
    assert _call(out, prereg=bad) == 2
    # factors 名单被换（多变量）⇒ 拒跑
    bad2 = _prereg(tmp_path / "bad2.md", factors=["mom20"])
    assert _call(out, prereg=bad2) == 2
    assert not out.exists(), "exit 2 必须零输出（既没产物也没落盘目录）"


def test_random_factor_ic_is_around_zero(tmp_db, tmp_path, monkeypatch):
    """因子与收益都随机 ⇒ |IC| 小、CI 跨 0（不是「一定显著」的假尺子）。"""
    days = _weekdays(N_WEEKDAYS)
    rng = random.Random(20260926)
    synth = _Synth(days)
    # 打乱前向收益：把 closes 换成与因子无关的随机游走
    for code in CODES:
        px, out = 100.0, []
        for _ in synth.marks:
            px *= 1.0 + rng.gauss(0.0, 0.01)
            out.append(px)
        synth.closes[code] = out
    report = _run_inproc(monkeypatch, tmp_db, tmp_path, synth)
    mom = report["factors"]["mom20"]
    assert abs(mom["mean_validate"]) < 0.15
    assert mom["ci_low"] < 0.0 < mom["ci_high"]
    assert mom["verdict"] == "IC_NOT_SIGNIFICANT"
