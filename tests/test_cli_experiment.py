"""Task 39：`experiment run` 的 CLI 层。

命令行的职责只有三件，每件都对应一条纪律：

  - **封存段不给入口**：`--selection-split test` 在 argparse 层就被拒（不是跑完再说）；
  - **失败要能被脚本看见**：结论 `falsified` → 退出码 1；用法/切分问题 → 2；
  - **落盘路径可控**：`--report` / `--report-dir` 决定报告落哪，且 stdout 的 JSON
    与磁盘上的文件同源（sha256 对得上）。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from stocklab.cli.main import main
from stocklab.experiments.runner import run_experiment
from tests.test_experiments_runner import (CODE, FIRST_TARGET, N, _days, _env, _run)


def _argv(tmp_path, **over):
    days = _days()
    a = {"--variant": "rw-mu0", "--from": days[FIRST_TARGET], "--to": days[-1],
         "--db": str(tmp_path / "a.db"), "--code": CODE}
    a.update(over)
    argv = ["experiment", "run"]
    for k, v in a.items():
        argv += [k, str(v)]
    return argv


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


def test_experiment_run_writes_both_files_and_prints_their_hashes(tmp_path, capsys):
    conn = _env(tmp_path)
    conn.close()
    out = tmp_path / "exp" / "r.md"
    assert main(_argv(tmp_path, **{"--report": str(out)})) == 0
    rep = _json_out(capsys)

    assert rep["variant"] == "rw-mu0"
    assert rep["changed_fields"] == ["mu_mode"]
    assert rep["metric_version"] == "p8-metrics-v1"
    assert rep["selection_split"] == "validate"
    assert rep["test_evaluated"] is False
    assert "validate" in rep["gates"]
    assert "test" not in rep["gates"]
    assert rep["verdict"] in ("promoted", "falsified", "inconclusive")

    md, js = out, out.with_suffix(".json")
    assert md.exists() and js.exists()
    assert hashlib.sha256(md.read_bytes()).hexdigest() == rep["sha256_md"]
    assert hashlib.sha256(js.read_bytes()).hexdigest() == rep["sha256_json"]
    assert "封存" in md.read_text()


def test_experiment_run_is_idempotent_through_the_cli(tmp_path, capsys):
    conn = _env(tmp_path)
    conn.close()
    hashes = []
    for name in ("r1", "r2"):
        target = tmp_path / name / "r.md"
        assert main(_argv(tmp_path, **{"--report": str(target)})) == 0
        hashes.append(_json_out(capsys)["sha256_md"])
    assert hashes[0] == hashes[1]


def test_report_dir_is_honoured_and_named_after_the_variant(tmp_path, capsys):
    conn = _env(tmp_path)
    conn.close()
    d = tmp_path / "reports"
    assert main(_argv(tmp_path, **{"--report-dir": str(d)})) == 0
    rep = _json_out(capsys)
    assert rep["report"].startswith(str(d))
    assert rep["report"].endswith("-exp-rw-mu0.md")


def test_selection_split_test_has_no_cli_entry_point(tmp_path, capsys):
    """封存段连参数都不提供 —— 拒绝发生在 argparse，而不是「跑完了才说不许」。

    `main()` 把 argparse 的 `SystemExit` 转成返回码（用法错误 = 2），
    所以这里断言的是返回码 + stderr 里那句 `invalid choice`。
    """
    conn = _env(tmp_path)
    conn.close()
    assert main(_argv(tmp_path, **{"--selection-split": "test"})) == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'test'" in err
    assert "{train,validate}" in err
    assert not (tmp_path / "a.db").with_suffix(".md").exists()


def test_unknown_variant_exits_2(tmp_path, capsys):
    conn = _env(tmp_path)
    conn.close()
    assert main(_argv(tmp_path, **{"--variant": "nope"})) == 2
    assert "未注册的变体" in capsys.readouterr().err


def test_bad_split_ratios_exit_2(tmp_path, capsys):
    conn = _env(tmp_path)
    conn.close()
    assert main(_argv(tmp_path, **{"--train-ratio": 0.5, "--validate-ratio": 0.2,
                                  "--test-ratio": 0.2})) == 2
    assert "切分配置不合法" in capsys.readouterr().err


def test_missing_db_exits_2(tmp_path, capsys):
    assert main(["experiment", "run", "--variant", "rw-mu0",
                 "--from", "2024-01-01", "--to", "2024-12-31",
                 "--db", str(tmp_path / "nope.db")]) == 2
    assert "db not found" in capsys.readouterr().err


def test_cli_and_library_agree_on_the_report(tmp_path, capsys):
    """CLI 只是库的壳：同一份输入，两边算出的报告逐字段相同。"""
    conn = _env(tmp_path)
    conn.close()
    out = tmp_path / "r.md"
    assert main(_argv(tmp_path, **{"--report": str(out)})) == 0
    _json_out(capsys)

    days = _days()
    from stocklab.experiments.runner import render_experiment_markdown

    # 逐参数对齐 CLI 的默认值（**不传** min_days，否则门槛不同、gate 也会不同）
    conn2 = _env(tmp_path, name="b.db")
    rep = run_experiment(conn2, variant_name="rw-mu0",
                         from_date=days[FIRST_TARGET], to_date=days[-1],
                         codes=[CODE])
    assert render_experiment_markdown(rep) == out.read_text()
    assert rep["range"]["n_days"] == N - FIRST_TARGET


def test_keep_test_sealed_flag_is_accepted_and_forwarded(tmp_path, capsys):
    """`--keep-test-sealed` 必须真的传到执行器（不是「解析了但没人用」）。

    用 monkeypatch 把执行器包一层看实参：这是 CLI 与执行器之间**唯一**的接缝，
    断言它比断言「命令跑通了」有意义得多。
    """
    from stocklab.experiments import runner as runner_mod

    seen = {}
    real = runner_mod.run_experiment

    def spy(conn, **kw):
        seen.update(kw)
        return real(conn, **kw)

    conn = _env(tmp_path)
    conn.close()
    days = _days()
    runner_mod_orig = runner_mod.run_experiment
    runner_mod.run_experiment = spy
    try:
        rc = main(["experiment", "run", "--variant", "rw-mu0",
                   "--from", days[FIRST_TARGET], "--to", days[-1],
                   "--db", str(tmp_path / "a.db"), "--code", CODE,
                   "--report-dir", str(tmp_path / "reports"),
                   "--keep-test-sealed"])
    finally:
        runner_mod.run_experiment = runner_mod_orig

    assert rc == 0
    assert seen["evaluate_test_on_win"] is False
    assert _json_out(capsys)["test_evaluated"] is False
