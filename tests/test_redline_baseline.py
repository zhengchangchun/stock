"""P25：回归红线的**可执行形态**（错误日记 #34 的正面修复）。

#34 的根因是「把不变量写成没人守的快照常量」：`a61d026b…` 只出现在 docs 里，
70 分钟后失效而无人发现。本文件 + `scripts/check_redlines.py` 就是那条守卫。

两层设计：
- **字节级** sha256 逐位相等 —— 抓「字段集被改动」（#34 的翻车类型）；
- **数字叶子级** 递归键路径比对 —— 抓「数值漂移」，**容忍纯文字新增/改写**。

本文件是 **hermetic** 的那一半：合成夹具建库，不碰真实库、不联网。
真实库那一半在 `scripts/check_redlines.py`（**非 hermetic，显式标注**）。
"""

import hashlib
import json

import pytest

from stocklab.quality.redline import (
    BASELINE_RELPATH,
    build_synthetic_db,
    compare_leaves,
    format_failure,
    is_red,
    leaves,
    leaves_sha256,
    load_baseline,
    repo_root,
    run_synthetic_predict_report,
)


# --------------------------------------------------------------------------
# 1. 叶子提取
# --------------------------------------------------------------------------

def test_leaves_flattens_nested_dict_and_indexes_lists():
    obj = {"a": {"b": 1.5, "c": "文字"}, "d": [{"e": True}, 2]}
    assert leaves(obj) == {
        ".a.b": "1.5",
        ".a.c": '"文字"',
        ".d[0].e": "true",
        ".d[1]": "2",
    }


def test_leaves_distinguishes_bool_from_int():
    """`True` 与 `1` 在 Python 里 `==`，但它们是不同的叶子。"""
    assert leaves({"x": True}) != leaves({"x": 1})
    assert leaves({"x": True})[".x"] == "true"
    assert leaves({"x": 1})[".x"] == "1"


def test_leaves_records_empty_container_as_leaf():
    """空容器也要入账：字段被删掉时才能被发现（P8 报告里 `features_used` 就是空表）。"""
    assert leaves({"a": [], "b": {}}) == {".a": "[]", ".b": "{}"}


def test_leaves_is_order_insensitive_over_dict_keys():
    assert leaves({"a": 1, "b": 2}) == leaves({"b": 2, "a": 1})


def test_leaves_sha256_is_stable_and_order_insensitive():
    a = leaves({"a": 1, "b": "x"})
    b = leaves({"b": "x", "a": 1})
    assert leaves_sha256(a) == leaves_sha256(b)
    assert len(leaves_sha256(a)) == 64


# --------------------------------------------------------------------------
# 2. 比对：数值漂移必须红，纯文字改动必须容忍
# --------------------------------------------------------------------------

def test_compare_leaves_detects_numeric_drift_as_red():
    diff = compare_leaves(leaves({"p": 0.5}), leaves({"p": 0.6}))
    assert is_red(diff) is True
    assert diff["changed"] == [(".p", "0.5", "0.6")]


def test_compare_leaves_detects_bool_flip_as_red():
    diff = compare_leaves(leaves({"flag": True}), leaves({"flag": False}))
    assert is_red(diff) is True
    assert diff["changed"] == [(".flag", "true", "false")]


def test_compare_leaves_tolerates_new_text_key():
    """**纯文字新增必须容忍** —— 否则每写一句注释都要重新基线。"""
    diff = compare_leaves(leaves({"a": 1}), leaves({"a": 1, "note": "新写的说明"}))
    assert is_red(diff) is False
    assert diff["text"]["added"] == [".note"]


def test_compare_leaves_tolerates_rewritten_text():
    diff = compare_leaves(leaves({"note": "旧文案"}), leaves({"note": "新文案"}))
    assert is_red(diff) is False
    assert diff["text"]["changed"] == [(".note", '"旧文案"', '"新文案"')]


def test_compare_leaves_flags_new_numeric_key_as_red():
    """新增一个**数字**字段是漂移，不是文案 —— 这正是 #34 那类改动的数字面。"""
    diff = compare_leaves(leaves({"a": 1}), leaves({"a": 1, "sigma_mode": 1.2}))
    assert is_red(diff) is True
    assert diff["added_numeric"] == [".sigma_mode"]


def test_compare_leaves_flags_missing_numeric_key_as_red():
    diff = compare_leaves(leaves({"a": 1, "b": 2}), leaves({"a": 1}))
    assert is_red(diff) is True
    assert diff["missing_numeric"] == [".b"]


def test_identical_leaves_are_green():
    same = leaves({"a": {"b": [1, 2.5, "s"]}})
    diff = compare_leaves(same, dict(same))
    assert is_red(diff) is False
    assert diff["changed"] == []


# --------------------------------------------------------------------------
# 3. 失败信息必须可操作（把 #34 的教训固化成代码）
# --------------------------------------------------------------------------

def test_failure_message_contains_the_three_actionable_steps():
    diff = compare_leaves(leaves({"p": 0.5}), leaves({"p": 0.6}))
    msg = format_failure(
        "predict_real_2026-09-14",
        expected_sha="a" * 64, actual_sha="b" * 64, leaf_diff=diff,
        regen_cmd="python scripts/check_redlines.py --regen",
        baseline_file="docs/baselines/redlines.json",
    )
    # ① 先归因：回退改动重跑同命令 cmp
    assert "cmp" in msg
    # ② 有意改动 → 明确给出重新基线命令
    assert "--regen" in msg
    # ③ 把新值写进台账/文档，且不删旧值
    assert "append-only" in msg or "不删" in msg
    # 失败输出必须**点名差异键路径**，不能只说「sha 不一致」
    assert ".p" in msg
    assert "a" * 64 in msg and "b" * 64 in msg
    # 也要提醒「可能不是预测变了，而是库/数据变了」
    assert "归因" in msg


# --------------------------------------------------------------------------
# 4. 基线文件本身的元数据（谁在什么条件下必须重新基线）
# --------------------------------------------------------------------------

def test_baseline_file_exists_and_is_git_tracked():
    p = repo_root() / BASELINE_RELPATH
    assert p.exists(), f"基线文件缺失：{BASELINE_RELPATH}"
    # 必须落在 git 跟踪的路径（reports/ 被 .gitignore 忽略，不能放那里）
    assert str(BASELINE_RELPATH).startswith("docs/baselines/")
    assert _git_tracked(BASELINE_RELPATH), f"{BASELINE_RELPATH} 未被 git 跟踪"


def _git_tracked(relpath):
    import subprocess
    out = subprocess.run(["git", "ls-files", "--", str(relpath)],
                         cwd=repo_root(), capture_output=True, text=True)
    return bool(out.stdout.strip())


@pytest.mark.parametrize("name", [
    "predict_real_2026-09-14",
    "backfill_real_2013-12-23_2026-09-14",
    "predict_synthetic",
])
def test_every_baseline_entry_declares_provenance(name):
    entry = load_baseline()["targets"][name]
    for field in ("taken_at", "command", "model_version", "rebaseline_when", "sha256"):
        assert entry.get(field), f"{name} 缺少 `{field}`（#34：快照值必须写明谁在何时重测）"
    assert len(entry["sha256"]) == 64
    assert entry["leaves_sha256"] and entry["n_leaves"] > 0
    assert "rebaseline" in entry["rebaseline_when"] or "重新基线" in entry["rebaseline_when"]


# --------------------------------------------------------------------------
# 5. hermetic 红线：合成夹具 → 与基线 `predict_synthetic` 段比 sha + 叶子
# --------------------------------------------------------------------------

def test_synthetic_predict_report_matches_baseline_bytes_and_leaves(tmp_path):
    """**这条是「提交时就会红」的那道守卫**（不依赖真实库）。

    改了 `stocklab/predict/` 的字段集或数值 → 这里立刻红，无需等真实库。
    """
    entry = load_baseline()["targets"]["predict_synthetic"]
    report = run_synthetic_predict_report(tmp_path)
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    actual_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    actual_leaves = leaves(report)
    diff = compare_leaves(entry["leaves"], actual_leaves)

    # 两条断言分开写：叶子级先跑（能点名差异键路径），字节级后跑（抓字段集改动）。
    # 只做文字改动时可能出现「叶子绿、字节红」—— 那正是「字段集/字节被改动」，
    # 按失败文案里的三步确认是否**有意**，再用 --regen 重新基线。
    assert not is_red(diff), format_failure(
        "predict_synthetic(叶子)", expected_sha=entry["sha256"], actual_sha=actual_sha,
        leaf_diff=diff, regen_cmd=".venv/bin/python scripts/check_redlines.py --regen",
        baseline_file=str(BASELINE_RELPATH))
    assert actual_sha == entry["sha256"], format_failure(
        "predict_synthetic(字节)", expected_sha=entry["sha256"], actual_sha=actual_sha,
        leaf_diff=diff, regen_cmd=".venv/bin/python scripts/check_redlines.py --regen",
        baseline_file=str(BASELINE_RELPATH))


def test_synthetic_fixture_is_deterministic(tmp_path):
    """两次建库跑同一条命令 → 逐字节相同（否则上面的基线会是随机红）。"""
    a = run_synthetic_predict_report(tmp_path / "a")
    b = run_synthetic_predict_report(tmp_path / "b")
    ja = json.dumps(a, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    jb = json.dumps(b, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    assert ja == jb


def test_synthetic_baseline_leaves_match_live_report_leaf_for_leaf(tmp_path):
    """叶子级单跑：即使 sha 断言先失败，也要能看清**哪些键路径**动了。"""
    entry = load_baseline()["targets"]["predict_synthetic"]
    diff = compare_leaves(entry["leaves"], leaves(run_synthetic_predict_report(tmp_path)))
    assert not is_red(diff), format_failure(
        "predict_synthetic(叶子)", expected_sha=entry["sha256"], actual_sha="(见上)",
        leaf_diff=diff, regen_cmd=".venv/bin/python scripts/check_redlines.py --regen",
        baseline_file=str(BASELINE_RELPATH))
