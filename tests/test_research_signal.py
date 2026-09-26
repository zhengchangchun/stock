"""P78 T1–T4：信号有效性度量（`research rank-ic`）的可用例。

四组：

1. **纯函数层**（T2）—— Spearman/Pearson/分层的手算核对；并列、常数、
   样本过少的边界；`_avg_ranks` 与 `cross_section._pct_of` 那套排名法同值。
2. **预注册 fail-closed**（T2/T3）—— 缺字段 / 无 json 块 / `universe` 不一致 /
   `n_layers` 不一致 ⇒ `xsec.PreregError`；仓库那份预注册必须能通过校验。
3. **`PipelineResult.scored`**（T1）—— 暴露 `scored` **没有**改变
   `members` / `rejects` / `params` / `eligible` 的派生；老构造点仍可只给三个字段。
4. **端到端**（T3/T4）—— 合成数据（monkeypatch 打分与前向收益，**不碰真库数据**）
   跑 `run_rank_ic` 与 CLI：信号完美 ⇒ IC=1.0 ⇒ `IC_SIGNIFICANT`；随机 ⇒ CI 跨 0；
   截面过薄 ⇒ 记 `None` 并计数（**不拿 0 顶替**）；同输入两次逐位相同。

⚠️ 「逐位相同」的确定性断言**排除三个耗时键**（`elapsed_s` / `scan_s` /
`fwd_load_s`）—— 它们本来就是墙钟读数，把它们算进「逐位」是伪判据。
"""

from __future__ import annotations

import json
import random
import statistics
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
from stocklab.plugin import sandbox
from stocklab.research import signal, xsec
from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from tests.test_research_xsec import NOW, _seed_pipeline_db

START = "2015-01-01"
REPO_PREREG = (Path(__file__).resolve().parents[1] / "docs" / "experiments"
               / "2026-09-26-rank-ic-short-csi300-500.md")

#: 合成宇宙的标的数：必须 > `signal.MIN_XSEC_N`，否则每天截面都不够。
N_CODES = 40
CODES = [f"{600000 + i:06d}" for i in range(N_CODES)]
#: 够 420 个调仓边界（5 日一调）⇒ 验证段 ≥ 120，才够跑出显著性结论。
N_WEEKDAYS = 2100

#: 「两次跑逐位相同」要比的键 —— 耗时键是墙钟读数，天生不同。
_TIMING_KEYS = ("elapsed_s", "scan_s", "fwd_load_s")


def _stable(report: dict) -> dict:
    return {k: v for k, v in report.items() if k not in _TIMING_KEYS}


def _weekdays(n: int, start: str = START) -> list[str]:
    out: list[str] = []
    cur = date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _prereg(path: Path, **over) -> Path:
    """写一份合法预注册（字段值一律从代码/sandbox **读出**，不手抄）。"""
    data = {"experiment": signal.EXPERIMENT, "pool": "short", "start": START,
            "universe": "seed21", "ic_type": signal.IC_TYPE,
            "n_layers": signal.N_LAYERS, "horizon": REBALANCE_DAYS["short"],
            "min_periods": sandbox.MIN_VALID_PERIODS,
            "bootstrap_n": sandbox._BOOTSTRAP_N,
            "bootstrap_seed": sandbox._BOOTSTRAP_SEED,
            "rule": "IC_SIGNIFICANT = 验证段 IC 的 95% CI 不含 0；跨 0 ⇒ 写「无可测排序能力」"}
    data.update(over)
    path.write_text("# 夹具预注册（rank-ic）\n\n```json\n"
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


# ---------------------------------------------------------------------------
# 合成接缝：打分与前向收益都注入假实现（不读真库里的行情）
# ---------------------------------------------------------------------------

class _Synth:
    """一次合成跑的全部输入与**期望值**（由原始输入算出，不是抄实现）。"""

    def __init__(self, days: list[str], scores: dict, closes: dict[str, list[float]],
                 missing: tuple[str, ...] = ()) -> None:
        self.days = days
        self.scores = scores
        self.closes = closes
        self.missing = missing
        self.marks = replay.rebalance_dates(
            days, period=REBALANCE_DAYS["short"], start=days[0], end=days[-1])

    def fwd(self, code: str, k: int) -> float:
        px = self.closes[code]
        return px[k + 1] / px[k] - 1.0

    def expected_ic(self, k: int) -> float:
        """第 k 个周期的**手算** Spearman：直接喂原始分数与原始前向收益。"""
        day = self.marks[k]
        codes = [c for c in CODES if c not in self.missing]
        sc = {c: self.scores[(day, c)] for c in codes}
        fw = {c: self.fwd(c, k) for c in codes}
        return signal.spearman_ic(sc, fw)


def _install(monkeypatch, synth: _Synth) -> None:
    """装上三处接缝：打分、宇宙、复权价。**不碰真库的 `bars_daily` / `instruments`。**"""
    members = tuple(Instrument(code=c, name=c, market="sh", board="main")
                    for c in CODES)

    def _fake_resolve(universe_id):
        return "seed21", members, "0" * 64

    def _fake_score_pipeline(conn, *, asof, universe=None, **_kw):
        rows = [{"code": c, "pool": "short", "raw_score": synth.scores[(asof, c)],
                 "adj_score": synth.scores[(asof, c)], "reason": "合成",
                 "risk_json": "[]"} for c in CODES]
        scored = {p: [] for p in candidate_pools.ALL_POOLS}
        scored["short"] = rows
        return PipelineResult(members=[], rejects=[], params={},
                              eligible={"short": sorted(CODES)}, scored=scored)

    def _fake_load_bars_adjusted(conn, code, as_of, *, start=None):
        if code in synth.missing:
            # 数据侧不可用（D4 的一类）⇒ 回退未复权价：本夹具的 bars_daily 为空，
            # 于是这一只在所有周期都取不到前向收益 ⇒ 计入 n_no_fwd_ret。
            raise adjust.MissingFactor(f"合成：{code} 不可复权")
        return [Bar(code=code, date=m, open=p, high=p, low=p, close=p, volume=1000,
                    amount=None, turnover=None, source="synth")
                for m, p in zip(synth.marks,
                                synth.closes[code])]

    monkeypatch.setattr(signal, "resolve_universe", _fake_resolve)
    monkeypatch.setattr("stocklab.candidate.run.score_pipeline", _fake_score_pipeline)
    monkeypatch.setattr(adjust, "load_bars_adjusted", _fake_load_bars_adjusted)


def _synth_db(tmp_db, synth: _Synth):
    """只有日历的库（打分与复权价都是注入的）＋ 逐 code 一条 `corp_actions`。

    `corp_actions` 那条是为了让 `_ForwardPrices` 走**真复权**分支（链非空），
    而不是「空链回退」——空链回退由 `missing` 那组单独覆盖。
    """
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
    rc = main(["research", "rank-ic", "--pool", "short", "--start", START,
               "--end", synth.days[-1], "--prereg", str(prereg),
               "--out", str(out), "--db", str(db), *extra])
    return rc, out


# ---------------------------------------------------------------------------
# 1. 纯函数层
# ---------------------------------------------------------------------------

def test_spearman_perfect_monotone_is_plus_one():
    scores = {f"c{i:02d}": float(i) for i in range(40)}
    fwd = {f"c{i:02d}": 0.001 * (i * 3 + 7) for i in range(40)}
    assert signal.spearman_ic(scores, fwd) == 1.0


def test_spearman_perfect_antitone_is_minus_one():
    scores = {f"c{i:02d}": float(i) for i in range(40)}
    fwd = {f"c{i:02d}": -0.002 * i for i in range(40)}
    assert signal.spearman_ic(scores, fwd) == -1.0


def test_spearman_with_ties_matches_hand_computed():
    """含并列的定值向量 —— 手算值写死在用例里（平均名次法）。

    分数 a/b=3、c=1、d=2；前向 a/b/c/d = 0.1/0.2/0.3/0.4。
    分数名次（0 基平均名次）：1 ⇒ 0、2 ⇒ 1、3 与 3 并列占名次 2、3 ⇒ 都取 2.5。
    ⇒ 秩序 [2.5, 2.5, 0, 1]，与 [0.1, 0.2, 0.3, 0.4] 的 Pearson：
      mean_x=1.5、mean_y=0.25
      sxy = 1.0×(−0.15) + 1.0×(−0.05) + (−1.5)×0.05 + (−0.5)×0.15 = −0.35
      sxx = 1+1+2.25+0.25 = 4.5；syy = 0.0225×2 + 0.0025×2 = 0.05
      r = −0.35 / sqrt(4.5×0.05) = −0.7378647873726218
    """
    scores = {"a": 3.0, "b": 3.0, "c": 1.0, "d": 2.0}
    fwd = {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}
    assert signal.spearman_ic(scores, fwd) == pytest.approx(
        -0.35 / (4.5 * 0.05) ** 0.5)
    assert signal.spearman_ic(scores, fwd) == pytest.approx(
        -0.7378647873726218, abs=1e-12)


def test_avg_ranks_is_isomorphic_to_pct_of_ranking():
    """`_avg_ranks` 与 `cross_section._pct_of` 那套逐个计数法**逐位同值**。

    那不是「差不多」：并列段的平均名次 `(i+j)/2` 恰好等于
    `less + (equal − 1) / 2`（`_pct_of` 的 rank 项）。
    """
    from stocklab.candidate.cross_section import _pct_of

    values = [3.0, 3.0, 1.0, 2.0, 5.0, 5.0, 5.0, 0.5]
    got = signal._avg_ranks(values)
    for k, v in enumerate(values):
        less = sum(1 for x in values if x < v)
        equal = sum(1 for x in values if x == v)
        assert got[k] == less + (equal - 1) / 2.0
    # `_pct_of` 的分位 = rank/(n−1)，逆着能唯一还原出名次
    for k, v in enumerate(values):
        assert _pct_of(values, v, higher_better=True) == \
            pytest.approx(got[k] / (len(values) - 1))


def test_spearman_noise_is_small():
    """与 fwd 独立的噪声 ⇒ |IC| 小（固定种子、写死阈值 0.15，不用近似断言糊过去）。"""
    rng = random.Random(20260926)
    codes = [f"c{i:03d}" for i in range(200)]
    scores = {c: rng.gauss(0, 1) for c in codes}
    fwd = {c: rng.gauss(0, 1) for c in codes}
    ic = signal.spearman_ic(scores, fwd)
    assert abs(ic) < 0.15


def test_spearman_none_when_undefined():
    """样本过少 / 分数全相等 ⇒ `None`（「算不出」不是「不相关」，更不是 0）。"""
    assert signal.spearman_ic({"a": 1.0}, {"a": 1.0}) is None
    assert signal.spearman_ic({"a": 1.0, "b": 2.0}, {"a": 1.0, "b": 2.0}) == 1.0
    assert signal.spearman_ic({c: 1.0 for c in "abcd"},
                              {c: float(i) for i, c in enumerate("abcd")}) is None
    # 只取两边都有的 code：交集 < 2 ⇒ None
    assert signal.spearman_ic({"a": 1.0, "b": 2.0}, {"c": 1.0, "d": 2.0}) is None


def test_pearson_on_linear_relation_is_one():
    scores = {f"c{i:02d}": float(i) for i in range(30)}
    fwd = {f"c{i:02d}": 5.0 * i - 3.0 for i in range(30)}
    assert signal.pearson_ic(scores, fwd) == 1.0


def test_assign_layers_30_into_5_is_six_each_layer1_highest():
    codes = [f"c{i:02d}" for i in range(30)]
    scores = {c: float(i) for i, c in enumerate(codes)}
    layers = signal.assign_layers(codes, scores, 5)
    assert sorted(layers) == [1, 2, 3, 4, 5]
    assert all(len(layers[k]) == 6 for k in layers)
    hi = max(scores, key=scores.get)
    lo = min(scores, key=scores.get)
    assert hi in layers[1] and lo in layers[5]
    assert "c29" in layers[1] and "c00" in layers[5]


def test_assign_layers_array_split_semantics_and_code_order_for_ties():
    """非整除时靠前的层更宽（`numpy.array_split` 同形）；并列按 code 升序定序。"""
    codes = [f"c{i:02d}" for i in range(32)]
    scores = {c: float(i) for i, c in enumerate(codes)}
    layers = signal.assign_layers(codes, scores, 5)
    assert [len(layers[k]) for k in range(1, 6)] == [7, 7, 6, 6, 6]
    # 并列：同样的分数 ⇒ 按 code 升序
    tied = {"b": 1.0, "a": 1.0, "d": 1.0, "c": 1.0}
    layers2 = signal.assign_layers(sorted(tied), tied, 2)
    assert layers2[1] == ["a", "b"] and layers2[2] == ["c", "d"]


def test_assign_layers_rejects_unusable_input():
    with pytest.raises(ValueError, match="n_layers"):
        signal.assign_layers(["a"], {"a": 1.0}, 0)
    with pytest.raises(ValueError, match="没有带分数"):
        signal.assign_layers(["a"], {"a": None}, 3)


def test_layer_means_and_ascending_steps():
    layers = {1: ["a", "b"], 2: ["c", "d"]}
    fwd = {"a": 0.02, "b": 0.04, "c": 0.0, "d": 0.0}
    means = signal.layer_means(layers, fwd)
    assert means == {1: pytest.approx(0.03), 2: 0.0}
    assert signal.ascending_steps([0.03, 0.0]) == 1
    assert signal.ascending_steps([0.03, 0.01, -0.02, 0.0, -0.01]) == 3
    # 缺读数的那一对不计（不是「没上升」）
    assert signal.ascending_steps([None, 0.0]) == 0
    assert signal.layer_means({1: ["a"]}, {}) == {1: None}


# ---------------------------------------------------------------------------
# 2. 预注册 fail-closed
# ---------------------------------------------------------------------------

def test_prereg_error_is_an_xsec_prereg_error():
    """CLI 只 except `xsec.PreregError` ⇒ 本站的错误类型必须是它的子类。"""
    assert issubclass(signal.PreregError, xsec.PreregError)


def test_load_prereg_roundtrip_and_sha(tmp_path):
    p = _prereg(tmp_path / "p.md")
    data, sha = signal.load_prereg(p)
    assert data["experiment"] == "rank-ic" and len(sha) == 64
    signal.validate_prereg(data, pool="short", start=START,
                           horizon=REBALANCE_DAYS["short"], universe="seed21")


@pytest.mark.parametrize("field,value", [
    ("experiment", "xsec-topn"), ("pool", "mid"), ("start", "2019-01-01"),
    ("universe", "csi300-500"), ("ic_type", "pearson"),
    ("n_layers", 4), ("horizon", 10), ("min_periods", 30),
    ("bootstrap_n", 100), ("bootstrap_seed", 1),
])
def test_validate_prereg_rejects_any_field_mismatch(tmp_path, field, value):
    data, _sha = signal.load_prereg(_prereg(tmp_path / "p.md", **{field: value}))
    with pytest.raises(xsec.PreregError, match=field):
        signal.validate_prereg(data, pool="short", start=START,
                               horizon=REBALANCE_DAYS["short"], universe="seed21")


def test_validate_prereg_rejects_missing_universe_field_vs_cli_universe(tmp_path):
    """预注册没写 `universe` ⇒ 语义 `seed21`；命令行传 csi300-500 ⇒ 拒跑。"""
    p = tmp_path / "p.md"
    data = json.loads(json.dumps({
        "experiment": "rank-ic", "pool": "short", "start": START,
        "ic_type": signal.IC_TYPE, "n_layers": signal.N_LAYERS,
        "horizon": REBALANCE_DAYS["short"],
        "min_periods": sandbox.MIN_VALID_PERIODS,
        "bootstrap_n": sandbox._BOOTSTRAP_N,
        "bootstrap_seed": sandbox._BOOTSTRAP_SEED, "rule": "r"}))
    p.write_text("```json\n" + json.dumps(data) + "\n```\n", encoding="utf-8")
    got, _sha = signal.load_prereg(p)
    assert "universe" not in got
    signal.validate_prereg(got, pool="short", start=START,
                           horizon=REBALANCE_DAYS["short"], universe=None)
    with pytest.raises(xsec.PreregError, match="universe"):
        signal.validate_prereg(got, pool="short", start=START,
                               horizon=REBALANCE_DAYS["short"],
                               universe="csi300-500")


def test_load_prereg_rejects_missing_field_and_bad_json(tmp_path):
    p = tmp_path / "p.md"
    p.write_text("```json\n{\"experiment\": \"rank-ic\"}\n```\n", encoding="utf-8")
    with pytest.raises(xsec.PreregError, match="缺字段"):
        signal.load_prereg(p)
    p.write_text("# 没有 json 块\n", encoding="utf-8")
    with pytest.raises(xsec.PreregError, match="json"):
        signal.load_prereg(p)
    p.write_text("```json\n{不是 json}\n```\n", encoding="utf-8")
    with pytest.raises(xsec.PreregError, match="解析失败"):
        signal.load_prereg(p)
    with pytest.raises(xsec.PreregError, match="读不到"):
        signal.load_prereg(tmp_path / "nope.md")


def test_repo_prereg_validates_against_code_constants():
    """仓库那份预注册必须能通过 `load_prereg` ＋ `validate_prereg`（T7 判据）。"""
    data, sha = signal.load_prereg(REPO_PREREG)
    assert data["experiment"] == "rank-ic"
    assert data["universe"] == "csi300-500"
    assert data["horizon"] == REBALANCE_DAYS["short"]
    signal.validate_prereg(data, pool="short", start=data["start"],
                           horizon=REBALANCE_DAYS["short"],
                           universe="csi300-500")
    assert len(sha) == 64


# ---------------------------------------------------------------------------
# 3. `PipelineResult.scored`（T1）
# ---------------------------------------------------------------------------

def test_pipeline_result_exposes_scored_without_changing_derivation(tmp_db):
    """暴露 `scored` **没有**改变既有的四个字段的取值与顺序。

    `members` 必须仍**逐位等于**「对同一个 `scored` 跑 `select_top` 再包成
    `MemberRow`」；`eligible` 必须仍**逐位等于** `sorted(scored[pool] 的 code)`。
    """
    days = _seed_pipeline_db(tmp_db, n_weekdays=300)
    c = connect(tmp_db)
    try:
        # `screen.MIN_HISTORY_DAYS = 250` ⇒ 取一个历史足够的 asof，否则全被排雷淘汰
        res = score_pipeline(c, asof=days[280])
    finally:
        c.close()

    assert set(res.scored) == set(candidate_pools.ALL_POOLS)
    for pool in candidate_pools.ALL_POOLS:
        rows = res.scored[pool]
        assert res.eligible[pool] == sorted(r["code"] for r in rows)
        # 每一行都带 D2 点名的三个键（度量侧只读这三个）
        assert all({"code", "raw_score", "adj_score"} <= set(r) for r in rows)
    from stocklab.candidate import snapshot
    expected = [snapshot.MemberRow(**row)
                for pool in candidate_pools.ALL_POOLS
                for row in candidate_pools.select_top(res.scored[pool], pool)]
    assert res.members == expected
    # params 的键集一字未增（`scored` 不进 params）
    assert set(res.params) == {"seed_count", "universe_id", "members_sha256",
                               "topn", "scoring_price_mode", "n_adj_fallback"}
    assert len(res.members) > 0


def test_pipeline_result_default_scored_is_empty():
    """老构造点（测试夹具、`_hydrate` 路径）不传 `scored` 也必须能构造。"""
    r = PipelineResult(members=[], rejects=[], params={})
    assert r.scored == {} and r.eligible == {}


# ---------------------------------------------------------------------------
# 4. 端到端：合成数据（monkeypatch 打分与前向收益）
# ---------------------------------------------------------------------------

def _perfect_synth(days: list[str]) -> _Synth:
    """分数与**前向收益**严格同增（两个不同的变换，不是把 fwd 喂给自己）。"""
    scores = {(d, c): float(i) for d in days for i, c in enumerate(CODES)}
    closes = {c: [100.0 * (1.0 + 0.0005 * i) ** k for k in range(len(
        replay.rebalance_dates(days, period=REBALANCE_DAYS["short"],
                               start=days[0], end=days[-1])))]
        for i, c in enumerate(CODES)}
    return _Synth(days, scores, closes)


def _random_synth(days: list[str], *, seed: int = 20260926) -> _Synth:
    rng = random.Random(seed)
    scores = {(d, c): rng.gauss(0.0, 1.0) for d in days for c in CODES}
    marks = replay.rebalance_dates(days, period=REBALANCE_DAYS["short"],
                                   start=days[0], end=days[-1])
    closes = {}
    for c in CODES:
        px, out = 100.0, []
        for _ in marks:
            px *= 1.0 + rng.gauss(0.0, 0.01)
            out.append(px)
        closes[c] = out
    return _Synth(days, scores, closes)


def test_run_rank_ic_perfect_signal_is_significant(tmp_db, tmp_path, monkeypatch):
    """合成数据让 IC 有**已知答案**：分数与前向收益同增 ⇒ 每日 IC=1.0。

    这条同时是「不是把 fwd 自己喂给自己」的等价性检查：分数是 `i`，前向收益是
    `0.05% × i` 的复利 —— 两个不同的数，只是同序。
    """
    days = _weekdays(N_WEEKDAYS)
    synth = _perfect_synth(days)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    c = connect(db)
    try:
        report = signal.run_rank_ic(c, pool="short", start=START, end=days[-1],
                                    prereg_path=prereg)
    finally:
        c.close()

    adj = report["ic"]["adj_score"]
    assert adj["n_dates"] == len(synth.marks) - 1
    assert all(ic == pytest.approx(1.0) for ic in adj["series"])
    assert adj["mean_validate"] == pytest.approx(1.0)
    assert adj["verdict"] == "IC_SIGNIFICANT"
    assert adj["n_validate"] == sandbox.MIN_VALID_PERIODS or \
        adj["n_validate"] > sandbox.MIN_VALID_PERIODS
    assert adj["overfit_flag"] is None          # 两段一样好 ⇒ 不是过拟合形态
    # 分层：第 1 层（最高分）平均前向收益最高、单调 ⇒ 4 个上升步
    layers = report["layers"]
    means = [layers["mean_by_layer"][str(k)] for k in range(1, 6)]
    assert means == sorted(means, reverse=True)
    assert layers["n_ascending_steps"] == 4
    assert layers["spread"]["mean"] > 0
    assert layers["size_p50_by_layer"] == {str(k): 8.0 for k in range(1, 6)}
    # 覆盖度：没有跳过日、没有回退、没有缺价
    cov = report["coverage"]
    assert cov["n_dates_skipped"] == 0 and cov["n_fwd_fallback"] == 0
    assert cov["n_no_fwd_ret"] == 0 and cov["xsec_size_p50"] == float(N_CODES)


def test_run_rank_ic_when_score_is_the_fwd_itself_ic_is_one(tmp_db, tmp_path, monkeypatch):
    """定向等价证（T4.3）：把「分数来源」接到**前向收益本身** ⇒ 每日 IC = 1.0。

    与上一条合起来证明「不是把 fwd 自己喂给自己」这类恒等式假绿 —— 分数**等于**
    前向收益时 IC 必须恰好 1.0（上界），而当分数与收益无关时 IC 必须回到 0 附近。
    """
    days = _weekdays(N_WEEKDAYS)
    marks = replay.rebalance_dates(days, period=REBALANCE_DAYS["short"],
                                   start=days[0], end=days[-1])
    g = {c: 0.0005 * i for i, c in enumerate(CODES)}
    closes = {c: [100.0 * (1.0 + g[c]) ** k for k in range(len(marks))]
              for c in CODES}
    # 分数 = 前向收益本身（每个 code 每周期都相等）
    scores = {(d, c): g[c] for d in days for c in CODES}
    synth = _Synth(days, scores, closes)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    c = connect(db)
    try:
        report = signal.run_rank_ic(c, pool="short", start=START, end=days[-1],
                                    prereg_path=prereg)
    finally:
        c.close()
    adj = report["ic"]["adj_score"]
    assert all(ic == 1.0 for ic in adj["series"])
    assert adj["verdict"] == "IC_SIGNIFICANT"


def test_run_rank_ic_random_signal_crosses_zero(tmp_db, tmp_path, monkeypatch):
    """分数与收益都随机 ⇒ IC ≈ 0、CI 跨 0 ⇒ `IC_NOT_SIGNIFICANT`。"""
    days = _weekdays(N_WEEKDAYS)
    synth = _random_synth(days)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    c = connect(db)
    try:
        report = signal.run_rank_ic(c, pool="short", start=START, end=days[-1],
                                    prereg_path=prereg)
    finally:
        c.close()
    adj = report["ic"]["adj_score"]
    assert abs(adj["mean_validate"]) < 0.15
    assert adj["ci_low"] < 0.0 < adj["ci_high"]
    assert adj["verdict"] == "IC_NOT_SIGNIFICANT"


def test_run_rank_ic_matches_hand_computed_ic_per_day(tmp_db, tmp_path, monkeypatch):
    """报告里每天的 IC **逐位等于**用原始输入手算的 Spearman（不是重算了一遍自己的实现）。"""
    days = _weekdays(600)
    synth = _random_synth(days, seed=7)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    c = connect(db)
    try:
        report = signal.run_rank_ic(c, pool="short", start=START, end=days[-1],
                                    prereg_path=prereg)
    finally:
        c.close()
    got = report["ic"]["adj_score"]["series"]
    want = [synth.expected_ic(k) for k in range(len(got))]
    assert got == pytest.approx(want)
    assert len(got) == len(synth.marks) - 1


def test_run_rank_ic_thin_cross_section_is_skipped_not_zero(tmp_db, tmp_path, monkeypatch):
    """截面 < `MIN_XSEC_N` ⇒ 该日记 `None` 并计入跳过数（**不拿 0 顶替**）。"""
    days = _weekdays(600)
    synth = _random_synth(days, seed=11)
    # 缺 11 只 ⇒ 截面 29 < 30 ⇒ 每天都该被跳过
    synth.missing = tuple(CODES[:11])
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    c = connect(db)
    try:
        report = signal.run_rank_ic(c, pool="short", start=START, end=days[-1],
                                    prereg_path=prereg)
    finally:
        c.close()
    adj = report["ic"]["adj_score"]
    assert adj["series"] == [] and adj["n_dates"] == 0
    assert adj["verdict"] == "INCONCLUSIVE"
    cov = report["coverage"]
    assert cov["n_dates_skipped"] == report["n_periods"]
    assert cov["n_fwd_fallback"] == 11          # 每只至多计一次（D4）
    assert cov["n_no_fwd_ret"] == 11 * report["n_periods"]
    assert cov["xsec_size_max"] == N_CODES - 11


def test_run_rank_ic_is_deterministic_across_two_runs(tmp_db, tmp_path, monkeypatch):
    days = _weekdays(600)
    synth = _random_synth(days, seed=13)
    _install(monkeypatch, synth)
    db = _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")
    c = connect(db)
    try:
        a = signal.run_rank_ic(c, pool="short", start=START, end=days[-1],
                               prereg_path=prereg)
        b = signal.run_rank_ic(c, pool="short", start=START, end=days[-1],
                               prereg_path=prereg)
    finally:
        c.close()
    assert json.dumps(_stable(a), ensure_ascii=False, sort_keys=True) == \
        json.dumps(_stable(b), ensure_ascii=False, sort_keys=True)
    # 切训练/验证窗只由周期序号决定 ⇒ n 也必须相同
    assert a["ic"]["adj_score"]["n_validate"] == b["ic"]["adj_score"]["n_validate"]


def test_run_rank_ic_rejects_bad_pool_start_and_universe(tmp_db, tmp_path):
    prereg = _prereg(tmp_path / "p.md")
    c = connect(tmp_db)
    try:
        with pytest.raises(xsec.PreregError, match="只跑"):
            signal.run_rank_ic(c, pool="mid", start=START, end=START,
                               prereg_path=prereg)
        with pytest.raises(xsec.PreregError, match="不得早于"):
            signal.run_rank_ic(c, pool="short", start="2014-12-31", end=START,
                               prereg_path=prereg)
        with pytest.raises(xsec.PreregError, match="宇宙载入失败"):
            signal.run_rank_ic(c, pool="short", start=START, end=START,
                               prereg_path=prereg, universe="no-such-universe")
    finally:
        c.close()


def test_signal_module_is_stdlib_only_and_imports_its_thresholds():
    """零 ML 依赖；判定门槛**import** 自 `plugin/sandbox.py`，不抄数字。"""
    src = (Path(__file__).resolve().parents[1] / "stocklab" / "research"
           / "signal.py").read_text(encoding="utf-8")
    assert "import numpy" not in src and "import scipy" not in src
    assert "import pandas" not in src
    assert "stocklab.paper" not in src and "stocklab.m2" not in src
    assert "MIN_VALID_PERIODS = 120" not in src
    assert "2000" not in src and "20260918" not in src
    assert "sandbox.MIN_VALID_PERIODS" in src


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

def test_cli_help_lists_rank_ic(capsys):
    assert main(["research", "--help"]) == 0
    out = capsys.readouterr().out
    assert "rank-ic" in out and "xsec-topn" in out
    assert main(["research", "rank-ic", "--help"]) == 0
    assert "--prereg" in capsys.readouterr().out


def test_cli_rank_ic_end_to_end_writes_two_files_and_no_table(
        tmp_db, tmp_path, monkeypatch, capsys):
    days = _weekdays(600)
    synth = _random_synth(days, seed=17)
    rc, out = _run(monkeypatch, tmp_db, tmp_path, synth)
    assert rc == 0
    # 文件名**必须带宇宙 id**（P77 T7 的教训：不带会静默覆盖历史产物）
    json_p = out / f"{days[-1]}-rank-ic-seed21.json"
    md_p = out / f"{days[-1]}-rank-ic-seed21.md"
    assert json_p.is_file() and md_p.is_file()
    assert _counts(tmp_db) == {"candidate_snapshots": 0, "plugin_scripts": 0,
                               "paper_agent_decisions": 0, "paper_nav_daily": 0}

    report = json.loads(json_p.read_text(encoding="utf-8"))
    assert report["experiment"] == "rank-ic" and report["pool"] == "short"
    assert report["universe_id"] == "seed21" and report["n_layers"] == 5
    assert report["horizon"] == REBALANCE_DAYS["short"]
    assert report["min_periods"] == sandbox.MIN_VALID_PERIODS
    # 预注册 sha 必须等于那份文件的 sha（否则「跑的就是那份预注册」无从证明）
    import hashlib
    assert report["prereg_sha256"] == hashlib.sha256(
        (tmp_path / "prereg.md").read_bytes()).hexdigest()
    md = md_p.read_text(encoding="utf-8")
    assert md.rstrip().splitlines()[-1].startswith("summary: rank-ic")
    assert signal.summary_line(report) in md
    for item in xsec.NON_PIT_ITEMS:
        assert item in md
    assert signal.REPLAY_RAW_PRICE_NOTE in md
    stdout = capsys.readouterr().out
    assert "verdict=" in stdout and "json=" in stdout and "md=" in stdout


def test_cli_rank_ic_bad_inputs_exit_2_with_zero_output(tmp_db, tmp_path, monkeypatch):
    days = _weekdays(600)
    synth = _random_synth(days, seed=19)
    _install(monkeypatch, synth)
    _synth_db(tmp_db, synth)
    prereg = _prereg(tmp_path / "p.md")

    def _call(out, *extra, **kw):
        return main(["research", "rank-ic", "--pool", kw.get("pool", "short"),
                     "--start", kw.get("start", START), "--end", days[-1],
                     "--prereg", str(kw.get("prereg", prereg)),
                     "--out", str(out), "--db", str(tmp_db), *extra])

    out = tmp_path / "out"
    assert _call(out, pool="mid") == 2
    assert _call(out, start="2014-12-31") == 2
    assert _call(out, prereg=tmp_path / "nope.md") == 2
    bad = _prereg(tmp_path / "bad.md", n_layers=4)
    assert _call(out, prereg=bad) == 2
    # 预注册说 csi300-500、命令行不说 ⇒ 拒跑
    mism = _prereg(tmp_path / "mism.md", universe="csi300-500")
    assert _call(out, prereg=mism) == 2
    assert not out.exists(), "exit 2 必须零输出（既没产物也没落盘目录）"
