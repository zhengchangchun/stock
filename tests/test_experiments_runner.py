"""Task 39：实验执行器端到端（`stocklab/experiments/runner.py`）。

这一层要钉住的不是「能跑通」，而是四条纪律真的在**运行路径**上成立：

  1. **一行都不写生产表** —— 变体不是模型版本，`predictions` / `verifications`
     的行数在实验前后必须完全相同；
  2. **基线数字与 P7 逐字段一致** —— 同一天同一标的，实验里重算的基线行必须与
     `predict` / `backfill` 那一条同概率、同 `hit_*`、同 `brier`
     （「唯一被替换的是预测从哪来」）；
  3. **test 段在 validate 未达 WIN 时根本没被算过** —— 用 `_replay` 的调用参数
     当测谎仪（第一趟的日期范围里不含任何 test 日）；
  4. **不可评分 / 缺指数不进分母，且不许静默退化** —— 指数缺口那一天变体侧
     记进 `skipped`，基线侧照常出数（`keys_baseline_only` +1），
     **不是**悄悄退化成基线。

夹具的两个量是**算出来的**，不是拍的（ERROR_DIARY 2026-09-15）：
`compute_forecast` 要 `max(WINDOW, LEVEL_WINDOW) + 1 = 61` 根 bar 才肯出预测，
所以可回放的首个目标日是第 **61** 个交易日；日期轴沿用 `_to_ord/_to_date`
的编码（与 `tests/test_predict_service.bars` 同一套，故两边日期逐日对齐）。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from stocklab.config.costs import CostModel
from stocklab.experiments import metrics
from stocklab.experiments.runner import (NoReplayDays, _replay, run_experiment,
                                         write_report, write_report_at)
from stocklab.experiments.split import SplitConfig, split_days
from stocklab.experiments.variants import UnknownVariant, get_variant
from stocklab.predict.model import WINDOW, degenerate_strategy_mix
from stocklab.predict.service import PitCache, build_predictions
from stocklab.predict.version import MODEL_VERSION
from stocklab.verify.replay import backfill
from stocklab.verify.score import CAPITAL
from tests.test_predict_service import _to_date, _to_ord, bars, seed

CODE = "000333"
CODE2 = "600690"
INDEX = "sh000300"

N = 200                      # 交易日总数
FIRST_TARGET = 61            # = WINDOW + 1 根 bar 之后才出得了第一个预测
MIN_DAYS = 4                 # 真实门槛 120 由 tests/test_experiments_metrics.py 负责

_TABLES = ("predictions", "verifications", "features_daily", "sim_trades")


def _days(n: int = N) -> list[str]:
    d0 = _to_ord("2024-01-01")
    return [_to_date(d0 + i) for i in range(n)]


def _rng() -> tuple[str, str]:
    d = _days()
    return d[FIRST_TARGET], d[-1]


def _env(tmp_path, *, n=N, codes=(CODE,), with_index=False, index_missing=(),
         drop_bar_for=(), name="a.db"):
    """建库：个股 +（可选）指数。`index_missing` / `drop_bar_for` 按**日期**删 bar。

    `drop_bar_for` 是「(日期, 标的)」：只删某一个标的在某一天的 bar ——
    这才是**采集缺口**（那天仍是交易日，因为另一个标的有 bar）；把某天的 bar
    全删掉则等于那天不是交易日，`session_dates` 会把整天空掉，测不到不可评分。
    """
    bars_by_code = {c: bars(code=c, n=n, base=10.0 + 0.5 * i)
                    for i, c in enumerate(codes)}
    if with_index:
        gap = set(index_missing)
        bars_by_code[INDEX] = [b for b in bars(code=INDEX, n=n, base=3000.0, drift=0.001)
                               if b.date not in gap]
    for gone_day, gone_code in drop_bar_for:
        bars_by_code[gone_code] = [b for b in bars_by_code[gone_code]
                                   if b.date != gone_day]
    return seed(tmp_path / name, bars_by_code, cal_dates=_days(n))


def _counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in _TABLES}


def _run(conn, variant="rw-mu0", **kw):
    kw.setdefault("codes", [CODE])
    kw.setdefault("min_days", MIN_DAYS)
    kw.setdefault("cache", PitCache())
    kw.setdefault("from_date", _rng()[0])
    kw.setdefault("to_date", _rng()[1])
    return run_experiment(conn, variant_name=variant, **kw)


def _seg():
    return split_days(_days()[FIRST_TARGET:])


def _validate_day(k: int = 3) -> tuple[str, str]:
    """validate 段里第 `k` 个目标日 `t` 及其 `asof`（= 前一交易日）。

    用 validate 段的日子做断言，是因为配对计数只在**那一段**的报告里。
    """
    seg = _seg()["validate"]
    days = _days()
    target = seg[k]
    return days[days.index(target) - 1], target


def _replay_one(conn, targets, variant_name):
    return _replay(conn, targets, _days(), variant=get_variant(variant_name),
                   codes=[CODE], costs=CostModel(), capital=CAPITAL,
                   cache=PitCache(), strategy_mix=degenerate_strategy_mix())


def _stored_row(conn, target, code=CODE):
    return conn.execute(
        "SELECT v.* FROM verifications v JOIN predictions p ON p.pred_id=v.pred_id"
        " WHERE p.code=? AND v.target_date=?", (code, target)).fetchone()


# ---------- 1. 一行都不写生产表 ----------

def test_experiment_writes_nothing_to_the_production_tables(tmp_path):
    conn = _env(tmp_path)
    before = _counts(conn)
    rep = _run(conn)
    assert rep["counts"]["baseline_rows"] > 0        # 确实跑了东西
    assert _counts(conn) == before, "实验往生产表里写行了 —— 变体不是模型版本"


def test_variant_rows_are_tagged_with_the_variant_not_the_model_version(tmp_path):
    conn = _env(tmp_path)
    days = _days()
    v = get_variant("rw-mu0")
    res = _replay_one(conn, days[-8:], "rw-mu0")
    assert {r["model_version"] for r in res["rows"]["baseline"]} == {MODEL_VERSION}
    assert {r["model_version"] for r in res["rows"]["variant"]} == {v.model_tag}
    # 合成 id 全为负：这些预测从不落库，负号让它们一眼区别于真实 pred_id
    assert all(r["pred_id"] < 0 for side in res["rows"].values() for r in side)


# ---------- 2. 基线数字与 P7 逐字段一致 ----------

def test_recomputed_baseline_matches_the_p7_prediction_and_verification(tmp_path):
    """同一天同一标的：实验里重算的基线 == P7 的预测载荷 + P7 的验证行。

    若哪天真去动了「基线的取数/打分」，这条会立刻变红 ——
    「变体与基线并排」只有在**两侧口径相同**时才有意义。
    """
    conn = _env(tmp_path, with_index=True)
    days = _days()
    asof, target = days[FIRST_TARGET + 5], days[FIRST_TARGET + 6]

    payload = build_predictions(conn, asof, [CODE], cache=PitCache())["predictions"][0]
    backfill(conn, target, target, codes=[CODE], cache=PitCache())
    stored = _stored_row(conn, target)
    assert stored is not None

    res = _replay_one(conn, [target], "rw-mu0")
    base = [r for r in res["rows"]["baseline"] if r["code"] == CODE]
    assert len(base) == 1
    b = base[0]

    # ① 预测侧：三分类概率逐位一致 ——「同一个预测」的直接证据
    assert b["notes"]["probabilities"] == payload["direction"]
    assert b["notes"]["probabilities"] == json.loads(stored["notes"])["probabilities"]
    # ② 验证侧：每一项打分同值
    assert b["hit_direction"] == stored["hit_direction"]
    assert b["hit_range"] == stored["hit_range"]
    assert b["hit_levels"] == stored["hit_levels"]
    assert b["score_level"] == pytest.approx(stored["score_level"])
    assert b["invalidated"] == stored["invalidated"]
    assert b["notes"]["brier"] == pytest.approx(json.loads(stored["notes"])["brier"])
    # ③ 基准对照也来自同一次取数（指数在场 → 必须是个真数字而不是 None）
    assert b["notes"]["index_pct"] is not None
    assert b["notes"]["index_pct"] == pytest.approx(
        json.loads(stored["notes"])["index_pct"])
    assert b["asof_date"] == asof


# ---------- 3. test 段在 validate 未达 WIN 时没被算过 ----------

def test_test_split_is_never_replayed_when_validate_is_not_a_win(tmp_path, monkeypatch):
    """测谎仪：把 `_replay` 的日期参数记下来，看第一趟到底跑了哪些日子。"""
    conn = _env(tmp_path)
    seg = _seg()
    calls: list[list[str]] = []
    real = _replay

    def spy(conn_, days_, sessions_, **kw):
        calls.append(list(days_))
        return real(conn_, days_, sessions_, **kw)

    monkeypatch.setattr("stocklab.experiments.runner._replay", spy)
    rep = _run(conn)

    assert len(calls) == 1, "validate 没赢，不该有第二趟"
    assert calls[0] == seg["train"] + seg["validate"]
    assert not set(calls[0]) & set(seg["test"]), "第一趟碰到了 test 段"
    assert rep["test_evaluated"] is False
    assert "test" not in rep["splits"]
    assert rep["test_not_evaluated_reason"]
    # 行数也只覆盖 train + validate（多一行就说明有别的日期被算了）
    assert rep["counts"]["baseline_rows"] == len(seg["train"]) + len(seg["validate"])


def test_sealed_reason_reports_the_actual_gate_not_a_hardcoded_win(tmp_path, monkeypatch):
    """`--keep-test-sealed` 时，原因串必须按**实际 gate**取值，不许一律写 WIN。

    封存开关（`--keep-test-sealed`）与 gate 状态是**两个独立事实**：
    validate 可以是 `LOSE`，同时预注册又声明「即使 WIN 也封存 test」。
    报告是给审计者看的 —— 说「validate 段 gate=WIN，只是封存了 test」
    会让人以为变体赢了。所以两条原因都要能同时表达。
    """
    conn = _env(tmp_path)
    lose = {"status": "LOSE", "n_days": MIN_DAYS, "min_days": MIN_DAYS,
            "reasons": [], "beat_direction": False, "beat_brier": False}
    monkeypatch.setattr(metrics, "gate", lambda *a, **k: dict(lose))
    rep = _run(conn, evaluate_test_on_win=False)

    assert rep["test_evaluated"] is False
    reason = rep["test_not_evaluated_reason"]
    assert "gate=LOSE" in reason, f"原因串没写出实际 gate：{reason}"
    assert "gate=WIN" not in reason, f"报告谎报 gate=WIN：{reason}"
    # 封存声明也要如实表达，不能因为 gate 是 LOSE 就把它吞掉
    assert "keep-test-sealed" in reason, f"封存声明丢了：{reason}"


def test_test_split_is_replayed_exactly_once_when_validate_wins(tmp_path, monkeypatch):
    """validate 达到 WIN → 第二趟**只**跑 test，且只跑一次。

    `gate` 被打桩成 WIN：这里要验的是**接线**（赢了才开门、只开一次、只开 test），
    统计判定本身由 `tests/test_experiments_metrics.py` 负责。
    """
    conn = _env(tmp_path)
    seg = _seg()
    win = {"status": "WIN", "n_days": MIN_DAYS, "min_days": MIN_DAYS,
           "reasons": [], "beat_direction": True, "beat_brier": True}
    monkeypatch.setattr(metrics, "gate", lambda *a, **k: dict(win))
    calls: list[list[str]] = []
    real = _replay

    def spy(conn_, days_, sessions_, **kw):
        calls.append(list(days_))
        return real(conn_, days_, sessions_, **kw)

    monkeypatch.setattr("stocklab.experiments.runner._replay", spy)
    rep = _run(conn)

    assert calls == [seg["train"] + seg["validate"], seg["test"]]
    assert rep["test_evaluated"] is True
    assert "test" in rep["splits"]
    assert rep["test_not_evaluated_reason"] is None


def test_selection_split_test_is_refused_before_anything_is_run(tmp_path, monkeypatch):
    conn = _env(tmp_path)
    called: list[int] = []
    monkeypatch.setattr("stocklab.experiments.runner._replay",
                        lambda *a, **k: called.append(1))
    with pytest.raises(metrics.TestSetLeak):
        _run(conn, selection_split="test")
    assert called == [], "封存段的拒绝必须发生在跑之前，而不是跑完之后"


def test_unknown_variant_and_bad_range_are_refused(tmp_path):
    conn = _env(tmp_path)
    with pytest.raises(UnknownVariant):
        _run(conn, variant="nope")
    with pytest.raises(NoReplayDays):
        _run(conn, from_date="1990-01-01", to_date="1990-12-31")


# ---------- 4. 不可评分 / 缺指数：记录 + 不进分母 ----------

def test_missing_index_bar_skips_the_variant_side_without_falling_back(tmp_path):
    """指数在 `asof` 那天没有 K 线 → 变体侧硬拒绝并记录，**不许**退回基线。

    基线侧照常出数，于是该日配对只剩基线 → `keys_baseline_only` +1。
    「静默退化」是最难发现的自欺：报告会显示变体跑了 100 天，
    实际只有 80 天真的用了变体。
    """
    gap, target = _validate_day()
    conn = _env(tmp_path, with_index=True, index_missing=(gap,))
    rep = _run(conn, variant="index-mom-dir")

    variant_misses = {k: v for k, v in rep["skipped"]["variant"].items()
                      if k.startswith(target)}
    assert variant_misses, "指数缺口没被记录 —— 变体大概静默退化成了基线"
    assert "index_dir=None" in list(variant_misses.values())[0]
    assert not [k for k in rep["skipped"]["baseline"] if k.startswith(target)]
    paired = rep["splits"]["validate"]["paired"]
    assert paired["counts"]["keys_baseline_only"] >= 1
    assert paired["counts"]["keys_variant_only"] == 0


def test_index_variant_without_any_index_data_is_all_skipped(tmp_path):
    conn = _env(tmp_path, with_index=False)
    rep = _run(conn, variant="index-mom-dir")
    assert rep["counts"]["variant_rows"] == 0
    assert rep["splits"]["validate"]["gate"]["status"] == "INSUFFICIENT"
    assert rep["verdict"]["status"] == "inconclusive"


def test_unscorable_rows_are_recorded_but_do_not_enter_the_denominator(tmp_path):
    """目标日某标的的 bar 缺失（采集缺口）→ 该 `(日, 标的)` 不进配对分母。

    那天仍是交易日（另一个标的有 bar），所以这一天**在**样本区间里 ——
    这正是「不可评分」与「那天没开市」的区别。
    """
    gone = _validate_day(5)[1]
    conn = _env(tmp_path, codes=(CODE, CODE2), drop_bar_for=((gone, CODE),))
    rep = _run(conn, codes=[CODE, CODE2])

    split = rep["splits"]["validate"]
    paired = split["paired"]
    assert paired["counts"]["unscorable_either"] >= 1
    # 不可评分的那一行仍然被**计数并给出原因**（不是消失了，也不是算成 0 分）
    for side in ("baseline", "variant"):
        s = split["summary"]["model_versions"][
            MODEL_VERSION if side == "baseline" else get_variant("rw-mu0").model_tag]
        assert s["n_unscorable"] >= 1
        assert s["unscorable_reasons"].get("NO_BAR_TARGET", 0) >= 1
    # 丢掉的是**配对**，不是**交易日**：那天另一个标的有 bar，这一天照样进日聚类
    n_days = split["boundaries"]["n_days"]
    assert paired["direction"]["n_days"] == n_days
    # 少掉的 2 对：缺口当天（CODE 不可评分）+ 次日（它的 asof 正是缺口日 → NoBarOnAsof）
    assert paired["counts"]["n_pairs"] == 2 * n_days - 2
    assert paired["counts"]["keys_baseline_only"] == 0
    # 行数（参考值）永远大于有效样本量 —— 这就是「不许拿行数当样本量」的原因
    assert split["summary"]["model_versions"][MODEL_VERSION]["n_rows"] > n_days


# ---------- 5. 幂等与呈现 ----------

def test_same_parameters_produce_a_byte_identical_report(tmp_path):
    """同参数两次 → 报告 `sha256` 相同（否则「准确率」可以被重跑改掉）。"""
    a = _run(_env(tmp_path, name="a.db"))
    b = _run(_env(tmp_path, name="b.db"))
    ra = write_report_at(a, tmp_path / "r1" / "x.md")
    rb = write_report_at(b, tmp_path / "r2" / "x.md")
    assert ra["sha256_md"] == rb["sha256_md"]
    assert ra["sha256_json"] == rb["sha256_json"]
    # 落盘的 json 读回来 == 内存里的报告（不是「另写了一份」）
    assert json.loads((tmp_path / "r1" / "x.json").read_text()) == b
    # 磁盘上的 sha256 与返回值一致（不是算了个别的字符串）
    blob = (tmp_path / "r1" / "x.md").read_bytes()
    assert hashlib.sha256(blob).hexdigest() == ra["sha256_md"]


def test_report_file_names_follow_the_exp_convention(tmp_path):
    rep = _run(_env(tmp_path))
    w = write_report(rep, tmp_path / "reports", "2026-09-15")
    assert w["markdown"].endswith("reports/2026-09-15-exp-rw-mu0.md")
    assert w["json"].endswith("reports/2026-09-15-exp-rw-mu0.json")
    md = (tmp_path / "reports" / "2026-09-15-exp-rw-mu0.md").read_text()
    for must in ("口径版本", "selection_split", "test_evaluated", "gate = `",
                 "train", "validate", "test"):
        assert must in md, must


def test_report_states_the_metric_version_and_the_frozen_quantities(tmp_path):
    rep = _run(_env(tmp_path))
    assert rep["metric_version"] == metrics.METRIC_VERSION
    # 冻结量必须出现在报告里，且等于实际用的那几个常量
    assert rep["frozen"]["flat_band"] == 0.005
    assert rep["frozen"]["window"] == WINDOW == 60
    assert rep["frozen"]["level_window"] == 20
    assert rep["split_config"] == SplitConfig().as_dict()
    assert set(rep["split_boundaries"]) == {"train", "validate", "test"}
    assert rep["range"]["n_days"] == N - FIRST_TARGET


# ---------- P9-a：第二条轴（条件化 sigma）在**运行路径**上的行为 ----------

def test_sigma_variant_needs_its_pit_feature_and_is_not_silently_degraded(tmp_path):
    """夹具的量能是常数 → z 的分母为 0 → `vol_z` 特征**算不出** → 变体侧整段拒绝。

    这条路必须**硬拒绝并计数**。若实现选择「回退到基线 sigma」，变体侧会凭空多出
    一批「看起来是变体口径、实际是基线」的行，而报告里看不出来 —— 这正是本包
    最贵的一类错。基线侧不受影响（`const` 一个特征都不读）。
    """
    conn = _env(tmp_path)
    rep = _run(conn, variant="sigma-vol-z")
    conn.close()

    assert rep["variant"]["changed_axis"] == "sigma_mode"
    assert rep["variant"]["spec"]["sigma_mode"] == "vol_z"
    assert rep["counts"]["variant_rows"] == 0          # 一行都没出来
    assert rep["counts"]["baseline_rows"] > 0          # 基线照常
    assert rep["splits"]["validate"]["paired"]["counts"]["keys_baseline_only"] > 0
    reasons = set(rep["skipped"]["variant"].values())
    assert reasons and all("算不出来" in r for r in reasons), reasons
    assert rep["splits"]["validate"]["gate"]["status"] == "INSUFFICIENT"
    assert rep["verdict"]["status"] == "inconclusive"
    assert rep["test_evaluated"] is False


def test_sigma_variant_with_usable_features_produces_paired_rows(tmp_path):
    """够长的夹具（>= 270 根）才算得出 RV 分位 —— 这条走**成功路径**。

    要证明的是「特征真的进了模型」而不是「变体跑通了」：变体侧出了数、
    与基线配上了对，且基线行一个没少。
    """
    # 两条都要拉长：**库里**的 bar 数（`_env(n=400)`）决定特征算不算得出来，
    # **回放区间**（`_days(400)`）决定这些日子跑不跑。只改一边的话，
    # 要么特征永远算不出（区间停在 200 天，asof 处只有 < 270 根），
    # 要么跑的是没有特征的日子 —— 两边都「绿」但什么也没测到。
    conn = _env(tmp_path, n=400)
    days = _days(400)
    rep = _run(conn, variant="sigma-rv-pct",
               from_date=days[FIRST_TARGET], to_date=days[-1])
    conn.close()

    assert rep["variant"]["changed_fields"] == ["sigma_mode"]
    assert rep["counts"]["variant_rows"] > 0
    assert rep["splits"]["validate"]["paired"]["counts"]["n_pairs"] > 0
    assert rep["test_evaluated"] is False              # 夹具样本量 << 120，不可能 WIN

    # 变体行**少于**基线行：回放区间的前 209 个目标日的 `asof` 只有 < 270 根 bar，
    # 算不出 RV 分位。这些行必须**显式降低分母并计数**（进 `skipped`），
    # 而不是「填个 0 / 0.5 蒙过去」—— 后者会让报告里的样本量凭空变大。
    assert rep["counts"]["variant_rows"] < rep["counts"]["baseline_rows"]
    reasons = list(rep["skipped"]["variant"].values())
    assert len(reasons) == rep["counts"]["baseline_rows"] - rep["counts"]["variant_rows"]
    assert all("算不出来" in r for r in reasons), set(reasons)
    # 这 209 天的短缺会**越过切分边界**伸进 validate 的前几天，所以
    # validate 段的配对计数里会看到 `keys_baseline_only > 0` —— 那正是
    # 「变体在这几天没有可信输入」的可见记录，不是 bug。
    assert rep["splits"]["validate"]["paired"]["counts"]["keys_baseline_only"] > 0


def test_sigma_variant_still_writes_nothing_to_production_tables(tmp_path):
    """变体不是模型版本：P9-a 的新轴也没有改变「一行都不写生产表」。"""
    conn = _env(tmp_path)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in _TABLES}
    _run(conn, variant="sigma-rv-pct")
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in _TABLES}
    conn.close()
    assert before == after
