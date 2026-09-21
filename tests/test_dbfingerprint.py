"""库指纹（P25 红线的归因辅助，2026-09-21 用户拍板「做」）。

守三件事：
1. 指纹**只读**（`mode=ro`）—— 诊断动作不允许污染被诊断的库；
2. 指纹**确定** —— 同一份库内容两次采集逐字节相同（否则它自己就是一条随机红）；
3. 差异**可读且可判** —— 红时打印的并排差异要点名「标的 6→21」「某标的 K 线行数变了」
   「账本原本 0 行」，并在指纹一致时明确说「不是库涨了，去查代码」。

真实库那一半不在这里（非 hermetic）：`scripts/check_redlines.py`。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from stocklab.quality.dbfingerprint import (
    attach_db_fingerprint,
    diff_fingerprint,
    fingerprint,
    format_fingerprint_diff,
    format_fingerprint_json,
)
from stocklab.quality.redline import build_synthetic_db, repo_root


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _sha256_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic(tmp_path):
    """小红线夹具的合成库（2 只标的 × 200 根 K 线，纯算术价格）。"""
    return build_synthetic_db(tmp_path / "synthetic.sqlite")


def _mini_db(tmp_path, name="mini.sqlite"):
    """手搓一个小库：只有 instruments / bars_daily / predictions / verifications。"""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE instruments (code TEXT PRIMARY KEY, name TEXT, market TEXT, type TEXT,
                                  board TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE bars_daily (code TEXT, date TEXT, open REAL, high REAL, low REAL,
                                 close REAL, pre_close REAL, volume INTEGER, amount REAL,
                                 turnover REAL, adj_mode TEXT DEFAULT 'none',
                                 is_suspended INTEGER DEFAULT 0, source TEXT,
                                 fetched_at TEXT, PRIMARY KEY (code, date));
        CREATE TABLE predictions (pred_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT,
                                 asof_date TEXT, target_date TEXT, direction_up REAL,
                                 direction_flat REAL, direction_down REAL, range_lo REAL,
                                 range_hi REAL, key_levels_json TEXT, action TEXT,
                                 size_pct REAL, invalidate_if TEXT, strategy_mix_json TEXT,
                                 regime_label TEXT, feature_snapshot_id INTEGER,
                                 model_version TEXT, status TEXT DEFAULT 'ok',
                                 created_at TEXT, origin TEXT);
        CREATE TABLE verifications (verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                                   pred_id INTEGER, target_date TEXT, actual_close REAL,
                                   actual_pct REAL, benchmark_pct REAL, hit_direction INTEGER,
                                   hit_range INTEGER, hit_levels INTEGER, sim_pnl REAL,
                                   score_direction REAL, score_range REAL, score_level REAL,
                                   score_action REAL, total_score REAL, invalidated INTEGER,
                                   attribution_auto TEXT, attribution_manual TEXT, notes TEXT,
                                   created_at TEXT);
    """)
    conn.executemany("INSERT INTO instruments (code, name, market, type, board) VALUES (?,?,?,?,?)",
                     [("000333", "美的集团", "sz", "stock", "main"),
                      ("600690", "海尔智家", "sh", "stock", "main")])
    conn.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, source, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [("000333", "2026-09-14", 10.0, 10.2, 9.9, 10.1, 1000, "tencent", "2026-09-15T10:00:00"),
         ("000333", "2026-09-15", 10.1, 10.3, 10.0, 10.2, 1100, "tencent", "2026-09-15T10:00:00"),
         ("600690", "2026-09-15", 20.0, 20.5, 19.8, 20.4, 2000, "tencent", "2026-09-15T10:00:00")])
    conn.commit()
    conn.close()
    return path


# --------------------------------------------------------------------------
# 1. 指纹内容与本机无关的确定性
# --------------------------------------------------------------------------

def test_fingerprint_records_universe_and_bars_per_code(tmp_path):
    fp = fingerprint(_synthetic(tmp_path), label="data/stocklab.db")
    assert fp["source_db"] == "data/stocklab.db"
    inst = fp["instruments"]
    assert inst["n"] == 2
    assert inst["codes"] == ["000333", "600690"]           # 排序 = 确定性
    bars = fp["bars_daily"]
    assert bars["n"] == 400
    assert bars["by_code"]["000333"]["n"] == 200
    # 全表区间必须与逐标的区间自洽（否则「600690 回填了」这类差异会被读错）
    assert bars["first"] == min(v["first"] for v in bars["by_code"].values())
    assert bars["last"] == max(v["last"] for v in bars["by_code"].values())
    assert sum(v["n"] for v in bars["by_code"].values()) == bars["n"]
    assert set(bars["by_code"]) == {"000333", "600690"}


def test_fingerprint_is_deterministic_across_calls(tmp_path):
    """**两次采集逐字节相同** —— 否则指纹自己就是一条随机红。"""
    db = _synthetic(tmp_path)
    a = json.dumps(fingerprint(db), sort_keys=True, ensure_ascii=False)
    b = json.dumps(fingerprint(db), sort_keys=True, ensure_ascii=False)
    assert a == b


def test_fingerprint_ignores_ingest_timestamps(tmp_path):
    """改 `fetched_at`（入库时刻）**不算库变了**；改 `close` 才算。"""
    db = _mini_db(tmp_path)
    before = fingerprint(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE bars_daily SET fetched_at='2030-01-01T00:00:00'")
    conn.commit()
    conn.close()
    assert fingerprint(db) == before

    conn = sqlite3.connect(db)
    conn.execute("UPDATE bars_daily SET close=99.9 WHERE code='000333' AND date='2026-09-14'")
    conn.commit()
    conn.close()
    after = fingerprint(db)
    assert after["content_sha256"]["bars_daily"]["sha256"] != \
        before["content_sha256"]["bars_daily"]["sha256"]
    assert after["content_sha256"]["bars_daily"]["rows"] == \
        before["content_sha256"]["bars_daily"]["rows"]      # 行数没变，内容变了


def test_fingerprint_does_not_write_to_the_database(tmp_path):
    """诊断动作必须零写入：整库 sha256 前后一致。"""
    db = _synthetic(tmp_path)
    before = _sha256_file(db)
    fingerprint(db)
    assert _sha256_file(db) == before


def test_fingerprint_opens_the_db_read_only(tmp_path):
    """`mode=ro` 由 SQLite 自己保证 —— 即便有人往指纹代码里塞写语句也写不进去。"""
    import pytest
    db = _synthetic(tmp_path)
    from stocklab.quality.dbfingerprint import _connect_ro
    conn = _connect_ro(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO instruments (code, name, market, type, board)"
                         " VALUES ('x','x','sz','stock','main')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM bars_daily")
    finally:
        conn.close()


def test_fingerprint_reports_missing_tables_instead_of_crashing(tmp_path):
    """裁剪库/老库也要能跑：缺表记 `{"present": false}`。"""
    path = tmp_path / "bare.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE instruments (code TEXT PRIMARY KEY, name TEXT, market TEXT,"
                 " type TEXT, board TEXT)")
    conn.execute("INSERT INTO instruments VALUES ('000333','美的集团','sz','stock','main')")
    conn.commit()
    conn.close()
    fp = fingerprint(path)
    assert fp["instruments"]["n"] == 1
    assert fp["bars_daily"] == {"present": False}
    assert fp["content_sha256"]["bars_daily"] == {"present": False}


def test_fingerprint_counts_replay_and_live_separately(tmp_path):
    """回放账本与实盘账本**分开计**：origin 是 P32 的口径，指纹要如实反映。"""
    db = _mini_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO predictions (code, asof_date, target_date, direction_up, direction_flat,"
        " direction_down, action, size_pct, invalidate_if, strategy_mix_json, model_version,"
        " created_at, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("000333", "2026-09-14", "2026-09-15", .4, .3, .3, "hold", 40.0, "x", "{}", "v1", "t", "replay"),
         ("600690", "2026-09-14", "2026-09-15", .4, .3, .3, "hold", 40.0, "x", "{}", "v1", "t", "replay"),
         ("000333", "2026-09-15", "2026-09-16", .4, .3, .3, "hold", 40.0, "x", "{}", "v1", "t", "live")])
    conn.commit()
    conn.close()
    fp = fingerprint(db)
    assert fp["predictions"]["n"] == 3
    assert fp["predictions"]["by_origin"] == {"live": 1, "replay": 2}
    assert fp["predictions"]["by_model_version"] == {"v1": 3}
    assert fp["predictions"]["first_asof"] == "2026-09-14"
    assert fp["predictions"]["last_asof"] == "2026-09-15"


def test_verifications_digest_uses_prediction_identity_not_autoincrement_id(tmp_path):
    """内容摘要走 `(code, asof_date)` 身份 —— 自增 `pred_id` 换一批行就会变，不能进摘要。"""
    db = _mini_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO predictions (code, asof_date, target_date, direction_up, direction_flat,"
        " direction_down, action, size_pct, invalidate_if, strategy_mix_json, model_version,"
        " created_at, origin) VALUES ('000333','2026-09-14','2026-09-15',.4,.3,.3,'hold',40.0,"
        "'x','{}','v1','t','replay')", ())
    conn.execute("DELETE FROM predictions")
    # 用自动生成的 id 插两批：同一份语义行，id 不同 → 摘要必须相同
    def _insert(tag):
        conn.execute(
            "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
            " direction_flat, direction_down, action, size_pct, invalidate_if,"
            " strategy_mix_json, model_version, created_at, origin)"
            " VALUES ('000333','2026-09-14','2026-09-15',.4,.3,.3,'hold',40.0,'x','{}',"
            "'v1',?,'replay')", (tag,))
        pred_id = conn.execute("SELECT MAX(pred_id) FROM predictions").fetchone()[0]
        conn.execute(
            "INSERT INTO verifications (pred_id, target_date, actual_close, actual_pct,"
            " total_score, invalidated, created_at) VALUES (?,?,?,?,?,0,?)",
            (pred_id, "2026-09-15", 10.2, 0.0099, 0.8, tag))
    _insert("t1")
    first = fingerprint(db)["content_sha256"]["verifications"]["sha256"]
    conn.execute("DELETE FROM verifications")
    conn.execute("DELETE FROM predictions")
    _insert("t2")
    second = fingerprint(db)["content_sha256"]["verifications"]["sha256"]
    conn.close()
    assert first == second


# --------------------------------------------------------------------------
# 2. 差异：结构化 + 可读
# --------------------------------------------------------------------------

def test_no_diff_when_fingerprints_are_equal(tmp_path):
    fp = fingerprint(_synthetic(tmp_path))
    assert diff_fingerprint(fp, fp) == {"rows": [], "n_changes": 0}


def test_diff_reports_universe_growth_as_added_codes():
    base = {"instruments": {"n": 6, "codes": ["000333", "000651"]}}
    now = {"instruments": {"n": 21, "codes": ["000333", "000651", "600690", "601318"]}}
    diff = diff_fingerprint(base, now)
    rows = diff["rows"]
    assert {"section": "instruments", "key": "", "field": "n",
            "expected": 6, "actual": 21} in rows
    added = [r for r in rows if r["field"] == "codes+"]
    assert added and added[0]["actual"] == ["600690", "601318"]
    assert not [r for r in rows if r["field"] == "codes-"]
    # 清单本身不逐元素刷屏
    assert not [r for r in rows if r["field"] == "codes"]


def test_diff_reports_per_code_bar_growth():
    base = {"bars_daily": {"n": 6062, "first": "2013-12-23", "last": "2026-09-14",
                           "by_code": {"600690": {"n": 6119, "first": "2013-12-23",
                                                  "last": "2026-09-14"}}}}
    now = {"bars_daily": {"n": 7807, "first": "1993-11-19", "last": "2026-09-21",
                          "by_code": {"600690": {"n": 7807, "first": "1993-11-19",
                                                 "last": "2026-09-21"}}}}
    rows = diff_fingerprint(base, now)["rows"]
    keys = {(r["key"], r["field"]) for r in rows}
    assert ("", "n") in keys
    assert ("", "first") in keys
    assert ("600690", "n") in keys
    assert ("600690", "first") in keys


def test_diff_reports_nested_origin_and_model_version_changes():
    base = {"predictions": {"n": 0, "by_origin": {}, "by_model_version": {}}}
    now = {"predictions": {"n": 42329, "by_origin": {"live": 17, "replay": 42312},
                           "by_model_version": {"pit-rw-v1.0.1": 42329}}}
    rows = diff_fingerprint(base, now)["rows"]
    fields = {r["field"] for r in rows}
    assert "n" in fields
    assert "by_origin.replay" in fields and "by_origin.live" in fields
    assert "by_model_version.pit-rw-v1.0.1" in fields


def test_diff_reports_missing_and_present_sections():
    base = {"adj_factors": {"n": 0, "codes": 0}, "corp_actions": {"present": False}}
    now = {"adj_factors": {"n": 80461, "codes": 17}, "corp_actions": {"n": 417, "codes": 17}}
    rows = diff_fingerprint(base, now)["rows"]
    assert {"section": "adj_factors", "key": "", "field": "n",
            "expected": 0, "actual": 80461} in rows
    assert {"section": "corp_actions", "key": "", "field": "present",
            "expected": False, "actual": True} in rows


def test_diff_flags_content_change_with_unchanged_row_count():
    """**行数相同、内容不同**也必须能看出来（复权口径改变就是这种）。"""
    base = {"content_sha256": {"verifications": {"rows": 42312, "sha256": "a" * 64}}}
    now = {"content_sha256": {"verifications": {"rows": 42312, "sha256": "b" * 64}}}
    rows = diff_fingerprint(base, now)["rows"]
    assert {"section": "content_sha256", "key": "verifications", "field": "sha256",
            "expected": "a" * 64, "actual": "b" * 64} in rows
    assert not [r for r in rows if r["field"] == "rows"]


def test_diff_is_deterministic_and_order_insensitive_over_sections():
    a = {"instruments": {"n": 1, "codes": ["a"]}}
    b = {"instruments": {"n": 2, "codes": ["a", "b"]}}
    r1 = diff_fingerprint(a, b)
    r2 = diff_fingerprint({"x": {"n": 1}}, {"x": {"n": 2}})
    assert r1["rows"][0]["section"] == "instruments"
    assert r2["rows"][0]["section"] == "x"


# --------------------------------------------------------------------------
# 3. 打印：并排差异 + 一句结论
# --------------------------------------------------------------------------

def _real_like_pair():
    base = {
        "source_db": "data/stocklab.db",
        "instruments": {"n": 6, "codes": ["000333", "000651", "000858"]},
        "bars_daily": {"n": 6062, "first": "2013-12-23", "last": "2026-09-14",
                       "by_code": {"600690": {"n": 6119, "first": "2013-12-23",
                                              "last": "2026-09-14"}}},
        "predictions": {"n": 0, "by_origin": {}, "by_model_version": {}},
        "adj_factors": {"n": 0, "codes": 0},
        "content_sha256": {"bars_daily": {"rows": 6062, "sha256": "a" * 64}},
    }
    now = {
        "source_db": "data/stocklab.db",
        "instruments": {"n": 21, "codes": ["000333", "000651", "000858", "600690"]},
        "bars_daily": {"n": 7807, "first": "1993-11-19", "last": "2026-09-21",
                       "by_code": {"600690": {"n": 7807, "first": "1993-11-19",
                                              "last": "2026-09-21"}}},
        "predictions": {"n": 42329, "by_origin": {"live": 17, "replay": 42312},
                        "by_model_version": {"pit-rw-v1.0.1": 42329}},
        "adj_factors": {"n": 80461, "codes": 17},
        "content_sha256": {"bars_daily": {"rows": 7807, "sha256": "b" * 64}},
    }
    return base, now


def test_print_puts_baseline_and_now_side_by_side():
    base, now = _real_like_pair()
    text = format_fingerprint_diff(base, now, taken_at="2026-09-16")
    # 两边都要出现，且能读到「哪一项从什么变成了什么」
    assert "2026-09-16" in text
    assert "instruments" in text and "bars_daily" in text and "predictions" in text
    assert "6" in text and "21" in text
    assert "6062" in text and "7807" in text
    assert "42312" in text
    assert "600690" in text
    # 结论：库变了
    assert "库变了" in text
    assert "--regen" in text
    # 64 位 sha 缩略显示（并排表不能被 sha 挤爆）
    assert "a" * 64 not in text


def test_print_says_not_the_database_when_fingerprints_match():
    fp = {"instruments": {"n": 21, "codes": ["000333"]}}
    text = format_fingerprint_diff(fp, fp, taken_at="2026-09-21")
    assert "完全一致" in text
    assert "库" in text and "代码" in text
    assert "库变了" not in text


def test_print_handles_baseline_without_fingerprint():
    """老基线（加指纹之前生成的）→ 明确说「没有指纹可比」并给出补录路径。"""
    text = format_fingerprint_diff(None, {"instruments": {"n": 21, "codes": []}})
    assert "没有库指纹" in text
    assert "--fingerprint" in text and "--regen" in text


def test_print_handles_missing_database():
    text = format_fingerprint_diff(None, None)
    assert "没有库可比" in text


def test_print_truncates_long_diff_lists():
    base = {"instruments": {"n": 0, "codes": []}}
    now = {"instruments": {"n": 100, "codes": [f"{i:06d}" for i in range(100)]}}
    base["bars_daily"] = {"by_code": {f"{i:06d}": {"n": 0} for i in range(100)}}
    now["bars_daily"] = {"by_code": {f"{i:06d}": {"n": 7} for i in range(100)}}
    text = format_fingerprint_diff(base, now)
    # 逐标的的行必须**折叠**（K 线逐日增长是常态，不能让 100 个标的刷屏）
    assert "另有" in text and "逐标的差异" in text
    assert text.count("00000") < 20


def test_print_puts_whole_table_facts_before_pending_per_code_noise():
    """“账本原本 0 行”“标的 6→21”必须排在数十行「某标的 K 线 +1」之前。"""
    base = {"instruments": {"n": 6, "codes": [f"{i:06d}" for i in range(6)]},
            "predictions": {"n": 0, "by_origin": {}},
            "content_sha256": {"predictions": {"rows": 0, "sha256": "a" * 64}},
            "bars_daily": {"by_code": {f"{i:06d}": {"n": 100} for i in range(40)}}}
    now = {"instruments": {"n": 21, "codes": [f"{i:06d}" for i in range(21)]},
           "predictions": {"n": 42329, "by_origin": {"replay": 42312}},
           "content_sha256": {"predictions": {"rows": 42329, "sha256": "b" * 64}},
           "bars_daily": {"by_code": {f"{i:06d}": {"n": 101} for i in range(40)}}}
    rows = diff_fingerprint(base, now)["rows"]
    order = [(r["section"], r["key"]) for r in rows]
    assert order.index(("predictions", "")) < order.index(("bars_daily", "000000"))
    assert order.index(("content_sha256", "predictions")) < order.index(("bars_daily", "000000"))

    text = format_fingerprint_diff(base, now)
    assert text.index("42329") < text.index("101")     # 整表事实先于逐标的细节


def test_fingerprint_json_is_stable_and_parseable(tmp_path):
    fp = fingerprint(_synthetic(tmp_path))
    text = format_fingerprint_json(fp)
    assert json.loads(text) == fp


# --------------------------------------------------------------------------
# 4. 挂到基线条目上：只挂非 hermetic 的
# --------------------------------------------------------------------------

def test_attach_only_touches_non_hermetic_entries():
    targets = {
        "predict_real_2026-09-14": {"non_hermetic": True},
        "predict_synthetic": {"non_hermetic": False},
    }
    fp = {"instruments": {"n": 21, "codes": []}}
    attach_db_fingerprint(targets, fp)
    assert targets["predict_real_2026-09-14"]["db_fingerprint"] == fp
    assert "db_fingerprint" not in targets["predict_synthetic"]


def test_baseline_entries_with_fingerprint_keep_required_provenance():
    """`--regen` 之后基线里每条真实库目标要同时有指纹与 #34 要求的字段。"""
    from stocklab.quality.redline import load_baseline
    targets = load_baseline()["targets"]
    for name, entry in targets.items():
        if not entry.get("non_hermetic"):
            continue
        fp = entry.get("db_fingerprint")
        if fp is None:
            continue                        # 加指纹之前生成的基线：不判红（由 --regen 补）
        assert fp["instruments"]["n"] > 0
        assert "bars_daily" in fp and "predictions" in fp and "verifications" in fp
        assert "content_sha256" in fp


def test_check_redlines_script_prints_fingerprint_when_red():
    """脚本接线：`check_one` 判红时必须把指纹差异打出来（不是只存在库里）。"""
    import contextlib
    import importlib.util
    import io

    path = repo_root() / "scripts" / "check_redlines.py"
    spec = importlib.util.spec_from_file_location("check_redlines_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    entry = {
        "sha256": "a" * 64, "leaves": {"x": "1.0"}, "taken_at": "2026-09-16",
    }
    base, now = _real_like_pair()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = mod.check_one("predict_real_2026-09-14", entry, sha="b" * 64,
                           payload={"x": 2.0}, db_fingerprint=(base, now))
    out = buf.getvalue()
    assert ok is False
    assert "库指纹对比" in out
    assert "库变了" in out

    # 指纹一致时也必须给出「不是库涨了」的结论，而不是沉默
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        mod.check_one("predict_real_2026-09-14", entry, sha="c" * 64,
                      payload={"x": 3.0}, db_fingerprint=(base, base))
    assert "完全一致" in buf2.getvalue()

    # 不传指纹（老调用）→ 行为与从前一致，不多打东西
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        mod.check_one("predict_real_2026-09-14", entry, sha="d" * 64, payload={"x": 4.0})
    assert "库指纹" not in buf3.getvalue()
