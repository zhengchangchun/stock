"""P26：链路准确率视图（`stocklab/chain/accuracy.py` + `chain accuracy` CLI）。

四条必须被钉住的东西：
  (a) 四段**分列**存在（回放 / 实时 / 模拟盘 / 实盘），顺序固定；
  (b) 任一段 `n_days < 120` → 逐字出现「样本不足（n=X < 120）」，且**不给结论句**；
  (c) 回放 / 实时的判定**确实读的是** `predictions.created_at` 与 `asof_date`
      （改这两个字段就改变分桶 —— 而不是靠日期硬编码猜）；
  (d) 同一输入两次渲染**逐字节一致**（报告不含生成时刻）。
"""

from __future__ import annotations

import json

import pytest

from stocklab.chain.accuracy import (
    SEGMENTS,
    boot_ci_by_day,
    build_chain_accuracy,
    render_markdown,
)
from stocklab.cli.main import main
from stocklab.session.review import classify
from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from stocklab.verify.report import MIN_DAYS

NOW = "2026-09-15T16:00:00+08:00"

#: 结论句禁词：样本不足的段里**一个都不许出现**（判据 (b)）。
CONCLUSION_WORDS = ("优于", "跑赢", "推荐", "最优", "胜出", "应当加仓", "可以下注")

_PRED_COLS = (
    "code, asof_date, target_date, direction_up, direction_flat, direction_down,"
    " range_lo, range_hi, key_levels_json, action, size_pct, invalidate_if,"
    " strategy_mix_json, model_version, status, created_at"
)


def _add_prediction(conn, *, code: str, asof: str, target: str, created_at: str,
                    mv: str = "pit-rw-v1.0.1") -> int:
    cur = conn.execute(
        f"INSERT INTO predictions ({_PRED_COLS}) VALUES"
        " (?,?,?,0.5,0.2,0.3,1.0,2.0,'[]','hold',50.0,'x',\"{}\",?, 'ok',?)",
        (code, asof, target, mv, created_at))
    conn.commit()
    return int(cur.lastrowid)


def _add_verification(conn, pred_id: int, *, target: str, hit: int = 1,
                      created_at: str = NOW, scorable: bool = True) -> None:
    notes = {
        "scorable": scorable, "brier": 0.3, "sim_ret": 0.01, "bh_ret": 0.005,
        "index_pct": 0.004, "excess_ret": 0.005, "actual_class": "up",
        "undetermined": [],
    }
    if not scorable:
        notes["reason_code"] = "NO_BAR_TARGET"
        notes["reason"] = "no bar"
    conn.execute(
        "INSERT INTO verifications (pred_id, target_date, actual_close, actual_pct,"
        " benchmark_pct, hit_direction, hit_range, hit_levels, sim_pnl,"
        " score_direction, score_range, score_level, score_action, total_score,"
        " invalidated, attribution_auto, notes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'UNDETERMINED',?,?)",
        (pred_id, target, 10.0 if scorable else None, 0.01 if scorable else None,
         0.004, hit if scorable else None, 1 if scorable else None,
         1 if scorable else None, 100.0, 0.7, 1.0, 1.0, 0.0, 0.62,
         json.dumps(notes, sort_keys=True), created_at))
    conn.commit()


def _seed(path, live_created_at: str):
    """一个最小但有四段数据的库：1 条实时 + 1 条回放 + 2 天模拟盘 + 1 笔实盘。

    `live_created_at` 是**唯一**的变量：改它就改分桶，其余字段一律不动 ——
    这正是判定字段依赖性的证明方式（`predictions` 是 append-only，建好就改不动了）。
    """
    init_db(path)
    c = connect(path)
    # 实时：created_at 日期 == asof_date（同日收盘后出次日预测）
    p_live = _add_prediction(c, code="000333", asof="2026-01-05",
                             target="2026-01-06",
                             created_at=live_created_at)
    _add_verification(c, p_live, target="2026-01-06", hit=1)
    # 回放：created_at 远晚于 asof_date（一次补几年）
    p_replay = _add_prediction(c, code="600690", asof="2013-12-20",
                               target="2013-12-23",
                               created_at="2026-09-15T07:04:51+08:00")
    _add_verification(c, p_replay, target="2013-12-23", hit=0)

    c.executemany(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,'2026-01-05',0,'[]',20000,20000,0,0,0.0,20000,4000.0,"
        " '2026-01-05',?)", [("arm-hold", NOW), ("arm-now", NOW)])
    c.executemany(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,'2026-01-06',0,'[]',20100,20100,0,0,0.005,20000,4040.0,"
        " '2026-01-06',?)", [("arm-hold", NOW), ("arm-now", NOW)])
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-01-05','000333','buy',10.0,100,5.09,"
              "'首笔',?)", (NOW,))
    c.commit()
    c.close()
    return path


@pytest.fixture
def db(tmp_db):
    return _seed(tmp_db, "2026-01-05T18:00:00+08:00")


def _report(db):
    conn = connect(db)
    try:
        return build_chain_accuracy(conn)
    finally:
        conn.close()


def _seg(rep, name):
    return next(s for s in rep["segments"] if s["segment"] == name)


def _section(md: str, title: str, *, until: str | None = None) -> str:
    """截出 `## <title>` 到下一个 `## ` 之间的正文。"""
    start = md.index(f"## {title}")
    rest = md[start + len(title) + 3:]
    end = rest.index("\n## ") if "\n## " in rest else len(rest)
    body = rest[:end]
    if until:
        assert until in body
    return body


# ---------- (a) 四段分列 ----------

def test_four_segments_are_listed_separately(db):
    rep = _report(db)
    assert [s["segment"] for s in rep["segments"]] == list(SEGMENTS)
    assert SEGMENTS == ("replay", "live", "paper", "real")

    # 每段**各自**有来源与样本量，且互不相等 —— 混算就不可能有这四个不同的来源串
    sources = [s["source"] for s in rep["segments"]]
    assert len(set(sources)) == 4
    for s in rep["segments"]:
        assert s["source"] and "n_days" in s and "gate" in s

    # 分桶正确：1 实时 + 1 回放
    assert rep["provenance_counts"] == {"live": 1, "replay": 1}
    assert _seg(rep, "live")["n_rows"] == 1
    assert _seg(rep, "replay")["n_rows"] == 1
    # 「写了多少条预测」与「其中多少条已被打分」**分列**：写了 1 条、打分 1 条
    assert _seg(rep, "live")["predictions_written"] == 1
    assert _seg(rep, "live")["predictions_verified"] == 1
    # 两个准确率段的「准确率」是各自算的，不许互相填充
    assert _seg(rep, "live")["metrics"]["model_versions"]
    assert _seg(rep, "replay")["metrics"]["model_versions"]

    md = render_markdown(rep)
    for title in ("回放段（PIT 历史重放）", "实时段（真实运行期）",
                  "模拟盘段（三臂 vs index_300）", "实盘段（real_trades）"):
        assert f"## {title}" in md
    # 模拟盘段/实盘段**不产出预测准确率**：这一格故意为空
    assert _seg(rep, "paper")["metrics"] is None
    assert _seg(rep, "real")["metrics"] is None


# ---------- (b) 样本不足要明说，且不给结论 ----------

def test_insufficient_segment_says_so_and_gives_no_conclusion(db):
    rep = _report(db)
    md = render_markdown(rep)
    assert MIN_DAYS == 120

    for name, title in (("live", "实时段（真实运行期）"),
                        ("paper", "模拟盘段（三臂 vs index_300）"),
                        ("real", "实盘段（real_trades）")):
        s = _seg(rep, name)
        n = s["n_days"]
        assert n < MIN_DAYS
        assert s["gate"]["sufficient"] is False
        notice = f"样本不足（n={n} < {MIN_DAYS}），不构成准确率结论"
        assert s["gate"]["notice"] == notice
        body = _section(md, title)
        assert notice in body, f"{name} 段缺样本不足判据句"
        for word in CONCLUSION_WORDS:
            assert word not in body, f"{name} 段出现了结论词 {word!r}"

    # 实时段样本不足 → **准确率表一行都不渲染**（不是靠人记得删）
    live_body = _section(md, "实时段（真实运行期）")
    assert "方向准确率" not in live_body
    assert "Brier" not in live_body
    assert "基准对照" not in live_body

    # 本夹具里回放段也只有 1 天 → 同样只给样本量、不给读数
    replay_body = _section(md, "回放段（PIT 历史重放）")
    assert "样本不足（n=1 < 120）" in replay_body
    assert "方向准确率" not in replay_body

    # 模拟盘段只并列、不挑冠军
    paper_body = _section(md, "模拟盘段（三臂 vs index_300）")
    assert "只并列" in paper_body
    assert "arm-hold" in paper_body and "arm-now" in paper_body

    # 实盘段：笔数 + 成本合计 + 「够不够算准确率」
    real_body = _section(md, "实盘段（real_trades）")
    assert "成交笔数 | 1" in real_body
    assert "**成本合计** | **5.09**" in real_body
    assert "够不够算准确率" in real_body


def test_sufficient_segment_renders_metrics(tmp_db):
    """样本过门槛（≥120 交易日）→ 该段**照实给读数**，且不再挂样本不足判据。"""
    import datetime as _dt

    init_db(tmp_db)
    c = connect(tmp_db)
    day0 = _dt.date(2020, 1, 1)
    for i in range(MIN_DAYS + 1):                      # 121 个交易日
        target = (day0 + _dt.timedelta(days=i)).isoformat()
        asof = (day0 + _dt.timedelta(days=i - 1)).isoformat()
        pred_id = _add_prediction(c, code="000333", asof=asof, target=target,
                                  created_at="2026-09-15T07:04:51+08:00")
        _add_verification(c, pred_id, target=target, hit=i % 2)
    c.close()

    rep = _report(tmp_db)
    s = _seg(rep, "replay")
    assert s["n_days"] == MIN_DAYS + 1
    assert s["gate"]["sufficient"] is True
    assert s["gate"]["notice"] is None
    body = _section(render_markdown(rep), "回放段（PIT 历史重放）")
    assert "样本不足" not in body
    assert "方向准确率" in body and "日度 bootstrap" in body
    assert "有效样本量（交易日数）121" in body


# ---------- (c) 判定字段确实被读取 ----------

def test_origin_classification_reads_created_at_and_asof_date(db):
    """同一个 asof，只改 `created_at` → 分桶必须跟着变。

    这是「读的是字段」而不是「按日期硬编码猜」的**唯一**证明：
    若实现里写的是「asof < 某天就算回放」，这条用例会红。
    """
    assert classify("2026-01-05", "2026-01-05T18:00:00+08:00") == "live"
    assert classify("2026-01-05", "2026-01-06T09:00:00+08:00") == "replay"
    # 凌晨边界：字符串比较不折 UTC（SQLite 的 date() 会把 +08:00 的 00:30 折回前一天）
    assert classify("2026-01-05", "2026-01-05T00:30:00+08:00") == "live"

    # 同一个库、同样的 asof/target/命中，**只把 created_at 挪到次日** → 分桶必须翻转。
    # （`predictions` 是 append-only，UPDATE 会被触发器 ABORT，所以只能另建一个库。）
    shifted = _seed(db.parent / "shifted.db", "2026-01-06T09:00:00+08:00")
    rep = _report(shifted)
    assert rep["provenance_counts"] == {"live": 0, "replay": 2}
    assert _seg(rep, "live")["n_days"] == 0
    assert _seg(rep, "replay")["n_rows"] == 2
    # 对照：只差这一个字段的库，分桶是 1/1（证明是字段在决定，不是日期硬编码）
    assert _report(db)["provenance_counts"] == {"live": 1, "replay": 1}
    # 报告里**写出**了实际用的字段与规则（可被审计者逐字复核）
    assert "created_at" in rep["origin_fields"]
    assert "asof_date" in rep["origin_fields"]
    assert "created_at" in rep["origin_rule"]


def test_boot_ci_is_day_clustered():
    """同日多标的算 **1 个日度观测**：3 天 → CI 只由 3 个日级值决定。"""
    rows = [{"target_date": d, "code": c, "scorable": True, "hit_direction": h}
            for d, h in (("2026-01-05", 1), ("2026-01-06", 0), ("2026-01-07", 1))
            for c in ("000333", "600690")]
    ci = boot_ci_by_day(rows)
    assert ci is not None and ci[0] <= 2 / 3 <= ci[1]
    # 行级重采样会把同样的输入当成 6 个独立样本 → 区间不同（这里只证明它按日）
    single = boot_ci_by_day([{"target_date": "2026-01-05", "code": "000333",
                              "scorable": True, "hit_direction": 1}])
    assert single == [1.0, 1.0]        # 只有 1 天 → 区间退化但不编造宽度


# ---------- (d) 逐字节一致 ----------

def test_render_is_byte_identical_for_same_input(db):
    rep = _report(db)
    a, b = render_markdown(rep), render_markdown(_report(db))
    assert a == b
    # 报告里不许出现「生成时刻」这类每次跑都不同的东西
    for stamp in ("2026-09-15T16:00", "generated", "生成时间", "生成时刻"):
        assert stamp not in a


def test_cli_writes_json_and_md_deterministically(db, tmp_path, capsys):
    out = tmp_path / "chain.md"
    argv = ["chain", "accuracy", "--db", str(db), "--out", str(out),
            "--date", "2026-01-06"]
    assert main(argv) == 0
    capsys.readouterr()
    first_md, first_json = out.read_text(), out.with_suffix(".json").read_text()
    assert main(argv) == 0
    capsys.readouterr()
    assert out.read_text() == first_md
    assert out.with_suffix(".json").read_text() == first_json
    payload = json.loads(first_json)
    assert payload["kind"] == "chain-accuracy"
    assert payload["segment_order"] == list(SEGMENTS)
    assert payload["origin_rule"]


def test_cli_missing_db_returns_2(tmp_path):
    assert main(["chain", "accuracy", "--db", str(tmp_path / "nope.db")]) == 2
