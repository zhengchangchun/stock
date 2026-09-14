"""特征快照与稳定哈希（Task 18）。

新增/改写计划断言 2 处，原因见 `docs/tasks/2026-09-15-p3-特征层-task17-20.md`：
1. `test_save_snapshot_returns_id_and_is_append_only` 原文期望「同一天同版本、
   仅参数不同」能存两行 —— 与 `features_daily` 的
   `UNIQUE(code,date,feature_version,feature_set)` 互斥。按 A1 意图（改数值必须
   升 feature_version）改为：同键重复写必须**报错**，升版本才追加第二行。
2. 补 NaN/inf 序列化与同日冲突去重的边界测试。
"""

import json
import sqlite3

import pytest

from stocklab.data.models import Bar
from stocklab.features import registry, snapshot
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T20:00:00+08:00"


def make_bars(n=80, base=10.0, volume=lambda i: 1000 + i):
    out = []
    for i in range(n):
        close = base + i * 0.1
        out.append(Bar(code="000333", date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                       open=close - 0.05, high=close + 0.15, low=close - 0.15,
                       close=close, volume=volume(i), amount=(1000 + i) * close,
                       turnover=1.0, source="test"))
    return out


# ---------- 稳定哈希 ----------

def test_canonical_json_is_key_order_independent():
    a = snapshot.canonical_json({"b": 1, "a": 2})
    b = snapshot.canonical_json({"a": 2, "b": 1})
    assert a == b
    assert a == '{"a":2,"b":1}'


def test_canonical_json_rejects_non_finite():
    """NaN/inf 不是合法 JSON —— 必须在序列化处硬失败，而不是写出 `NaN` 文本。"""
    with pytest.raises(ValueError):
        snapshot.canonical_json({"x": float("nan")})


def test_payload_hash_stable():
    p = {"ma20": 10.5, "atr14": 0.3}
    assert snapshot.payload_hash(p) == snapshot.payload_hash(dict(p))
    assert snapshot.payload_hash(p) != snapshot.payload_hash({"ma20": 10.6})


def test_params_hash_uses_effective_params():
    """记录「实际生效的参数」，而非「调用方传了什么」：显式默认值 == 不传。"""
    assert snapshot.params_hash(None) == snapshot.params_hash(dict(registry.PARAMS))
    assert snapshot.params_hash({"ma_short": 20}) == snapshot.params_hash(None)
    assert snapshot.params_hash({"ma_short": 30}) != snapshot.params_hash(None)


# ---------- 构建 ----------

def test_build_snapshot_produces_core_columns():
    snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80))
    assert snap is not None
    for col in registry.CORE_COLUMNS:
        assert col in snap.core
    assert set(snap.core) == set(registry.CORE_COLUMNS)


def test_snapshot_is_none_when_history_too_short():
    """样本不足时必须返回 None，由调用方记录为缺失，不能算出一个假值。"""
    assert snapshot.build_snapshot("000333", "2026-01-05", make_bars(10)) is None


def test_snapshot_is_none_when_asof_date_has_no_bar():
    """asof 当日无 K 线（停牌/非交易日）→ None，禁止拿前一日数据贴当日标签。"""
    bars = make_bars(80)
    assert all(b.date != "2026-03-25" for b in bars)
    assert snapshot.build_snapshot("000333", "2026-03-25", bars) is None


def test_build_snapshot_is_deterministic():
    bars = make_bars(80)
    a = snapshot.build_snapshot("000333", "2026-03-20", bars)
    b = snapshot.build_snapshot("000333", "2026-03-20", bars)
    assert a.payload_hash == b.payload_hash
    assert a.core == b.core


def test_build_snapshot_matches_hand_calculation():
    """核心列必须与手工计算一致（P3 DoD 第一条）。"""
    snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80))
    expected_close = 10.0 + 75 * 0.1          # i=75 是 2026-03-20
    assert snap.core["close"] == pytest.approx(expected_close)
    assert snap.core["ma20"] == pytest.approx(
        10.0 + sum(range(56, 76)) * 0.1 / 20)  # i=56..75
    assert set(snap.payload) >= set(registry.CORE_COLUMNS)


def test_params_actually_change_values_and_hash():
    bars = make_bars(80)
    a = snapshot.build_snapshot("000333", "2026-03-20", bars,
                                params={"ma_short": 20})
    b = snapshot.build_snapshot("000333", "2026-03-20", bars,
                                params={"ma_short": 30})
    assert a.params_hash != b.params_hash


def test_params_change_computed_values():
    """改短均线窗口必须真的改变 ma20 的取值（否则 params 是摆设）。"""
    bars = make_bars(80)
    a = snapshot.build_snapshot("000333", "2026-03-20", bars)
    c = snapshot.build_snapshot("000333", "2026-03-20", bars, params={"ma_short": 5})
    assert a.core["ma20"] != c.core["ma20"]
    assert a.payload_hash != c.payload_hash


def test_insufficient_history_for_overridden_params_is_none():
    """把 ma_long 调到 120 后，61 根历史不够用 → 必须 None，而不是存一堆 null。"""
    assert snapshot.build_snapshot("000333", "2026-03-20", make_bars(80),
                                   params={"ma_long": 120}) is None


def test_extra_features_land_in_json_payload():
    snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80),
                                   extra={"custom": 42})
    assert snap.payload["custom"] == 42
    assert json.loads(snap.db_row(NOW)["json_payload"])["custom"] == 42


def test_non_finite_feature_becomes_null_never_nan_text():
    """长期均量为 0 → vol_ratio 不可计算 → 必须是 null，且 JSON 合法可解析。"""
    snap = snapshot.build_snapshot("000333", "2026-03-20",
                                   make_bars(80, volume=lambda i: 0))
    assert snap is not None
    assert snap.core["vol_ratio_5_20"] is None
    text = snap.db_row(NOW)["json_payload"]
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["vol_ratio_5_20"] is None


# ---------- 输入裁剪（PIT 前置条件） ----------

def test_usable_bars_filters_and_sorts():
    bars = make_bars(80)
    usable = snapshot.usable_bars(list(reversed(bars)), "2026-03-20")
    assert [b.date for b in usable] == sorted(b.date for b in bars
                                              if b.date <= "2026-03-20")
    assert usable[-1].date == "2026-03-20"


def test_usable_bars_rejects_conflicting_duplicate_date():
    """同一天两条**取值不同**的记录 → 报错。静默取一条会让结果依赖入参顺序。"""
    bars = make_bars(80)
    conflict = Bar(code="000333", date="2026-03-20", open=1.0, high=1.0, low=1.0,
                   close=999.0, volume=1, amount=1.0, turnover=1.0, source="test")
    with pytest.raises(ValueError, match="2026-03-20"):
        snapshot.usable_bars(bars + [conflict], "2026-03-20")


def test_usable_bars_dedups_identical_duplicate_date():
    """完全相同的重复记录是同一根 K 线（DB 的 PK 保证唯一），不算冲突。"""
    bars = make_bars(80)
    dup = [b for b in bars if b.date == "2026-03-20"][0]
    out = snapshot.usable_bars(bars + [dup], "2026-03-20")
    assert len(out) == len([b for b in bars if b.date <= "2026-03-20"])


# ---------- 落库 ----------

def test_save_snapshot_returns_id(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80))
        sid = snapshot.save_snapshot(conn, snap, now=NOW)
        assert sid > 0
        row = conn.execute("SELECT * FROM features_daily WHERE snapshot_id=?",
                           (sid,)).fetchone()
        assert row["payload_hash"] == snap.payload_hash
        assert row["params_hash"] == snap.params_hash
        assert row["created_at"] == NOW
        assert row["close"] == pytest.approx(snap.core["close"])
        assert row["regime_label"] is None
        assert snapshot.latest_snapshot_id(conn, "000333", "2026-03-20") == sid


def test_duplicate_key_is_rejected_and_version_bump_appends(tmp_db):
    """append-only（A1）：同键重复写必须报错；改数值必须**升 feature_version**。

    计划原文这里期望「同一天同版本、仅参数不同」并存两行，与
    `UNIQUE(code,date,feature_version,feature_set)` 互斥（ERROR_DIARY 同类型第 4 次）。
    取舍：schema 的 append-only + A1 意图更强 —— 存两行会让「(code,date,version)
    唯一定位一份快照」失效，而 params_hash 恰是用来**发现**参数变更的。
    """
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80))
        sid = snapshot.save_snapshot(conn, snap, now=NOW)
        with pytest.raises(sqlite3.IntegrityError):
            snapshot.save_snapshot(conn, snap, now=NOW)

        bumped = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80),
                                         params={"ma_short": 30},
                                         feature_version="v1-ma30")
        sid2 = snapshot.save_snapshot(conn, bumped, now=NOW)
        assert sid2 != sid
        n = conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0]
        assert n == 2
        assert snapshot.latest_snapshot_id(conn, "000333", "2026-03-20") == sid
        assert snapshot.latest_snapshot_id(
            conn, "000333", "2026-03-20", feature_version="v1-ma30") == sid2


def test_repo_rejects_unexpected_column(tmp_db):
    """写入口只认声明过的列：多传字段必须报错，避免静默丢弃。"""
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        row = snapshot.build_snapshot("000333", "2026-03-20",
                                      make_bars(80)).db_row(NOW)
        row["typo_column"] = 1
        with pytest.raises(ValueError, match="typo_column"):
            repo.insert_feature_snapshot(conn, row)


def test_unknown_param_is_rejected():
    """参数名写错不得静默失效 —— 否则整轮实验会白跑且看不出原因。"""
    with pytest.raises(ValueError, match="ma"):
        registry.effective_params({"ma": 20})


def test_no_connection_leak_and_hash_readable_by_sql(tmp_db):
    """hash 必须能直接被 SQL 查询/join（P4 选样本要用）。"""
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        snapshot.save_snapshot(
            conn, snapshot.build_snapshot("000333", "2026-03-20", make_bars(80)),
            now=NOW)
        row = conn.execute(
            "SELECT payload_hash FROM features_daily WHERE code=? AND date=?",
            ("000333", "2026-03-20")).fetchone()
        assert len(row["payload_hash"]) == 64
