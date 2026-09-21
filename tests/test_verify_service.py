"""Task 34：`verify_target`（PIT 取数 + 打分 + 归因 + 落库）。

钉住四件事：
  1. **不可评分必须显式**（`DATA` + 原因），且**不进任何成功分母**；
  2. **按 `model_version` 分组**：v1.0.0 的错误预测单独成组，不许混进 v1.0.1；
  3. **打分不看未来**（PIT）：target 之后的行情不许改变分数；
  4. **归因不许自动硬判**：SIGNAL/STRATEGY/MODEL/NOISE 一律留给人工列。
"""

from __future__ import annotations

import pytest

from stocklab.data.models import Bar
from stocklab.predict.service import build_predictions
from stocklab.predict.store import insert_prediction
from stocklab.predict.version import MODEL_VERSION
from stocklab.store.db import connect
from stocklab.verify import service as VS
from stocklab.verify.score import HUMAN_ONLY_ATTRIBUTIONS
from tests.test_predict_service import _to_date, _to_ord, bars, seed

NOW = "2026-09-15T19:00:00+08:00"
CODE = "000333"
CODE2 = "600690"


def _predict_all(conn, asof):
    rep = build_predictions(conn, asof)
    ids = {}
    for p in rep["predictions"]:
        _, pid = insert_prediction(conn, p, now=NOW, origin="replay")
        ids[p["code"]] = pid
    return rep, ids


# ---------- 1. 可评分路径 ----------

def test_verify_target_scores_a_real_prediction(tmp_db, tmp_path):
    hist = bars(n=200)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=11.0, high=11.4, low=10.9,
                 close=11.2, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    conn = seed(tmp_path / "a.db", {CODE: hist + extra})
    _predict_all(conn, asof)

    out = VS.verify_target(conn, target)
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["scorable"] is True
    assert row["attribution_auto"] == VS.ATTRIBUTION_UNDETERMINED
    assert row["hit_direction"] in (0, 1)
    assert row["total_score"] is not None
    # 落库了，而且带着 pred_id
    stored = conn.execute("SELECT * FROM verifications").fetchall()
    assert len(stored) == 1 and stored[0]["pred_id"] == row["pred_id"]


def test_rerun_is_idempotent_and_writes_nothing_new(tmp_db, tmp_path):
    hist = bars(n=200)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=11.0, high=11.4, low=10.9,
                 close=11.2, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    conn = seed(tmp_path / "a.db", {CODE: hist + extra})
    _predict_all(conn, asof)

    first = VS.verify_target(conn, target)
    second = VS.verify_target(conn, target)
    assert all(v.startswith("inserted") for v in first["storage"].values())
    # 第二次必须**一行都不写**：状态是 identical，而且指向同一个 verification_id
    assert all(s.startswith("identical") for s in second["storage"].values())
    assert [v.split(":")[1] for v in second["storage"].values()] == \
           [v.split(":")[1] for v in first["storage"].values()]
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1
    # 两次的内容必须**逐字段相同** —— 幂等不是「没报错」，是「算出来一样」
    assert [r["total_score"] for r in first["rows"]] == \
           [r["total_score"] for r in second["rows"]]


# ---------- 2. 不可评分：显式 DATA，不进分母 ----------

def test_no_bar_on_target_is_unscorable_with_data_attribution(tmp_db, tmp_path):
    """`target_date` 是日历的下一交易日 —— 它当天必然还没有 bar（这就是真实情形）。"""
    hist = bars(n=200)
    conn = seed(tmp_path / "a.db", {CODE: hist})
    rep, _ = _predict_all(conn, hist[-1].date)
    target = rep["target_date"]

    out = VS.verify_target(conn, target)
    row = out["rows"][0]
    assert row["scorable"] is False
    assert row["reason_code"] == "NO_BAR_TARGET"
    assert row["attribution_auto"] == "DATA"
    # 结果列全空 → 统计层无法把它当 0 分算进分母
    for col in ("hit_direction", "score_direction", "total_score", "sim_pnl"):
        assert row[col] is None, col
    assert out["by_model_version"][rep["model_version"]]["unscorable"] == 1
    assert out["by_model_version"][rep["model_version"]]["scorable"] == 0


def test_unscorable_is_still_persisted_as_a_record(tmp_db, tmp_path):
    hist = bars(n=200)
    conn = seed(tmp_path / "a.db", {CODE: hist})
    rep, _ = _predict_all(conn, hist[-1].date)
    VS.verify_target(conn, rep["target_date"])
    row = conn.execute("SELECT * FROM verifications").fetchone()
    assert row["actual_close"] is None
    assert row["attribution_auto"] == "DATA"
    assert "NO_BAR_TARGET" in row["notes"]


# ---------- 3. 按 model_version 分组 ----------

def test_groups_by_model_version(tmp_db, tmp_path):
    """同一份数据、两个版本各出一次预测 → 两组各自统计，不混算。"""
    hist = bars(n=200)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=30.0, high=30.4, low=29.6,
                 close=30.2, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    conn = seed(tmp_path / "a.db", {CODE: hist + extra})

    rep = build_predictions(conn, asof)
    pred = rep["predictions"][0]
    insert_prediction(conn, pred, now=NOW, origin="replay")
    old = dict(pred, model_version="pit-rw-v1.0.0", direction={"up": 0.2, "flat": 0.2,
                                                              "down": 0.6})
    insert_prediction(conn, old, now=NOW, origin="replay")

    out = VS.verify_target(conn, target)
    groups = out["by_model_version"]
    # 当前版本从常量取，不写死：版本号会升（2026-09-21 → `pit-rw-v1.0.2`），
    # 而这条测试钉的是「按版本分组」这件事本身。
    assert set(groups) == {"pit-rw-v1.0.0", MODEL_VERSION}
    assert groups["pit-rw-v1.0.0"]["n"] == 1
    assert groups["pit-rw-v1.0.0"]["pred_ids"] != groups[MODEL_VERSION]["pred_ids"]
    # 每一行的版本必须能对上组（不许出现「行说 v1.0.1、组算到 v1.0.0」）
    for row in out["rows"]:
        assert row["pred_id"] in groups[row["model_version"]]["pred_ids"]


# ---------- 4. PIT：未来行情不许改变分数 ----------

def test_bars_after_target_do_not_change_the_scores(tmp_db, tmp_path):
    hist = bars(n=200)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=11.0, high=11.4, low=10.9,
                 close=11.2, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    future = [Bar(code=CODE, date=_to_date(_to_ord(target) + i), open=99.0,
                  high=99.0, low=99.0, close=99.0, volume=1, amount=1.0,
                  turnover=0.0, source="test", adj_mode="none")
              for i in (1, 2, 3)]

    c1 = seed(tmp_path / "a.db", {CODE: hist + extra})
    c2 = seed(tmp_path / "b.db", {CODE: hist + extra + future})
    for c in (c1, c2):
        _predict_all(c, asof)
    r1 = VS.verify_target(c1, target)["rows"][0]
    r2 = VS.verify_target(c2, target)["rows"][0]
    assert r1["notes"] == r2["notes"]
    assert r1["total_score"] == r2["total_score"]


# ---------- 5. 归因与契约自洽 ----------

def test_attribution_is_never_one_of_the_human_only_classes(tmp_db, tmp_path):
    hist = bars(n=200)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=11.0, high=11.4, low=10.9,
                 close=11.2, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    conn = seed(tmp_path / "a.db", {CODE: hist + extra})
    _predict_all(conn, asof)
    out = VS.verify_target(conn, target)
    for row in out["rows"]:
        assert row["attribution_auto"] not in HUMAN_ONLY_ATTRIBUTIONS
        assert row["attribution_auto"] in ("DATA", "UNDETERMINED")


def test_invalidate_if_bounds_match_the_key_levels(tmp_db, tmp_path):
    """`invalidate_if` 是给用户看的那句话，`key_levels` 是结构化版本 ——
    两者漂移时**测试要红**，而不是由打分器替它圆场（分数会因此悄悄算错）。"""
    hist = bars(n=200)
    asof = hist[-1].date
    conn = seed(tmp_path / "a.db", {CODE: hist})
    rep = build_predictions(conn, asof)
    for p in rep["predictions"]:
        levels = {lv["role"]: lv["price"] for lv in p["key_levels"]}
        invalidated, undetermined = VS._parse_invalidate_bounds(p["invalidate_if"])
        assert undetermined == []
        assert invalidated == (levels["support"], levels["resistance"])


def test_no_predictions_for_that_target_is_an_explicit_error(tmp_db, tmp_path):
    conn = seed(tmp_path / "a.db", {CODE: bars(n=200)})
    with pytest.raises(VS.NoPredictions):
        VS.verify_target(conn, "2030-01-01")


def test_rows_carry_code_and_model_version_for_grouping(tmp_db, tmp_path):
    hist = bars(n=200)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=11.0, high=11.4, low=10.9,
                 close=11.2, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    conn = seed(tmp_path / "a.db", {CODE: hist + extra})
    rep, _ = _predict_all(conn, asof)
    row = VS.verify_target(conn, target)["rows"][0]
    assert row["code"] == CODE
    assert row["model_version"] == rep["model_version"]
    assert row["asof_date"] == asof


# ---------- 5. `model_version` 过滤：跨版本重放不许篡改旧账本 ----------

def _env_with_target_bar(tmp_path):
    hist = bars(n=200)
    asof, target = hist[-1].date, _to_date(_to_ord(hist[-1].date) + 1)
    extra = [Bar(code=CODE, date=target, open=11.0, high=11.4, low=10.9,
                 close=11.2, volume=1000, amount=1.0, turnover=1.0,
                 source="test", adj_mode="none")]
    return seed(tmp_path / "a.db", {CODE: hist + extra}), asof, target


def test_model_version_filter_spares_the_previous_versions_ledger(tmp_db, tmp_path):
    """跨版本重放的真实碰撞（2026-09-21 实测，`pred_id=1831`）。

    库里本来就有上一版的预测 + 已评分的验证行。新版重放时**只评自己那一版**：
    不带过滤会把旧版行按新口径重算 ⇒ append-only 守卫（正确地）报冲突；
    带上过滤则旧行一个字节不碰 —— 旧口径的账本保留为历史，而不是被新口径改写。
    """
    from stocklab.verify.store import VerificationConflict

    conn, asof, target = _env_with_target_bar(tmp_path)
    rep = build_predictions(conn, asof, [CODE])
    legacy = {**rep["predictions"][0], "model_version": "pit-rw-v0.9.9"}
    _, old_pid = insert_prediction(conn, legacy, now=NOW, origin="replay")
    VS.verify_target(conn, target)                       # 旧版的账本行（唯一一条）
    before = dict(conn.execute("SELECT * FROM verifications WHERE pred_id=?",
                               (old_pid,)).fetchone())

    # 口径/数据变了：同一根 bar 算出不同结果（模拟「复权链从空到有」）
    conn.execute("UPDATE bars_daily SET close=13.0, high=13.5 WHERE code=? AND date=?",
                 (CODE, target))
    conn.commit()
    with pytest.raises(VerificationConflict):           # 守卫是真的：不带过滤就撞
        VS.verify_target(conn, target)
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1

    # 新版预测落库 + **只评这一版**
    _, new_pid = insert_prediction(conn, rep["predictions"][0], now=NOW,
                                   origin="replay")
    out = VS.verify_target(conn, target, model_version=MODEL_VERSION)
    assert sorted(out["storage"]) == [str(new_pid)]
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 2
    # 旧行逐字段未动（不是「重算成一样」，是**压根没重算**）
    after = dict(conn.execute("SELECT * FROM verifications WHERE pred_id=?",
                              (old_pid,)).fetchone())
    assert after == before


def test_model_version_filter_matching_nothing_is_an_explicit_error(tmp_db, tmp_path):
    """过滤后一条都不剩 ⇒ `NoPredictions`（不是静默成功 —— 那会把缺口读成「已评完」）。"""
    conn, asof, target = _env_with_target_bar(tmp_path)
    _predict_all(conn, asof)
    with pytest.raises(VS.NoPredictions):
        VS.verify_target(conn, target, model_version="pit-rw-v9.9.9")
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 0
