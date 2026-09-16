"""T7：`trend evaluate` 的 CLI 层。

命令行的职责（与 `experiment run` 同一套纪律）：

  - **一条命令跑完整轮**：取数 → 切段 → M1/M2 → 判定 →（达标才）开 test → 落盘；
  - **落盘路径可控**、stdout 的 JSON 与磁盘文件**同源**（sha256 对得上）；
  - **失败要能被脚本看见**：`falsified` → 1；用法 / 数据 / 预注册问题 → 2；
  - **预注册外标的没有入口**：`run_evaluation` 在前一行就拒收（本层只透传）。
"""

from __future__ import annotations

import hashlib
import json

from stocklab.cli.main import main
from stocklab.store.db import connect
from tests.test_trend_evaluate import N_BARS, _trend_env


def _argv(tmp_path, *, flags=(), **over):
    a = {"--db": str(tmp_path / "trend.db"), "--min-days": "5"}
    a.update(over)
    argv = ["trend", "evaluate"]
    for k, v in a.items():
        argv += [k, str(v)]
    return argv + list(flags)


def _counts(db):
    conn = connect(db)
    try:
        return {r["name"]: conn.execute(f"SELECT COUNT(*) c FROM {r['name']}")
                .fetchone()["c"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


def test_trend_evaluate_writes_both_files_and_prints_their_hashes(tmp_path, capsys):
    _trend_env(tmp_path, n=N_BARS).close()
    out = tmp_path / "rep" / "r.md"
    code = main(_argv(tmp_path, **{"--report": str(out)}))
    assert code in (0, 1)
    rep = _json_out(capsys)

    assert rep["prereg_doc"] == "docs/experiments/2026-09-15-trend-state-hit-rate.md"
    assert rep["universe"] == ["000333", "600690", "sh000300"]
    assert rep["trend_metric_version"] == "trend-state-v1"
    assert rep["verdict"] in ("WIN", "falsified", "inconclusive")
    assert set(rep["split_boundaries"]) == {"train", "validate", "test"}
    assert rep["criteria"]["a"] in (True, False) and rep["criteria"]["b"] in (True, False)
    assert code == (1 if rep["verdict"] == "falsified" else 0)

    md, js = out, out.with_suffix(".json")
    assert md.exists() and js.exists()
    assert hashlib.sha256(md.read_bytes()).hexdigest() == rep["sha256_md"]
    assert hashlib.sha256(js.read_bytes()).hexdigest() == rep["sha256_json"]
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["report_sha256"] == rep["sha256_md"]


def test_missing_db_is_a_usage_error(tmp_path, capsys):
    assert main(_argv(tmp_path)) == 2
    assert "db" in capsys.readouterr().err


def test_bad_split_ratios_are_a_usage_error(tmp_path, capsys):
    _trend_env(tmp_path, n=N_BARS).close()
    assert main(_argv(tmp_path, **{"--train-ratio": "0.9",
                                   "--validate-ratio": "0.9"})) == 2
    assert "切分" in capsys.readouterr().err


def test_empty_range_is_a_usage_error(tmp_path, capsys):
    _trend_env(tmp_path, n=N_BARS).close()
    assert main(_argv(tmp_path, **{"--from": "2099-01-01",
                                   "--to": "2099-12-31"})) == 2
    assert "交易日" in capsys.readouterr().err


def test_report_dir_default_naming_matches_the_prereg_slug(tmp_path, capsys):
    """默认落点是 `reports/<today>-trend-state-hit-rate.md`（README 里的复现命令）。"""
    _trend_env(tmp_path, n=N_BARS).close()
    rep_dir = tmp_path / "reports"
    code = main(_argv(tmp_path, **{"--report-dir": str(rep_dir)}))
    assert code in (0, 1)
    rep = _json_out(capsys)
    assert rep["report"].endswith("-trend-state-hit-rate.md")
    assert "trend-state-hit-rate.json" in rep["summary_json"]


def test_cli_writes_nothing_to_production_tables(tmp_path, capsys):
    _trend_env(tmp_path, n=N_BARS).close()
    db = tmp_path / "trend.db"
    before = _counts(db)
    assert main(_argv(tmp_path, flags=("--report", str(tmp_path / "r.md")))) in (0, 1)
    assert _counts(db) == before
    assert before["bars_daily"] == N_BARS * 3
    capsys.readouterr()


def test_keep_test_sealed_is_accepted_and_reported(tmp_path, capsys):
    _trend_env(tmp_path, n=N_BARS).close()
    code = main(_argv(tmp_path, flags=("--report", str(tmp_path / "r.md"),
                                       "--keep-test-sealed")))
    assert code in (0, 1)
    rep = _json_out(capsys)
    assert rep["test_evaluated"] is False
    if rep["verdict"] == "inconclusive":
        assert any("封存" in r or "F3" in r or "样本" in r
                   for r in rep["verdict_reasons"])
