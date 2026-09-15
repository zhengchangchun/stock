#!/usr/bin/env python
"""P8 变异反证：注入真实 bug → 目标测试**必须变红** → 还原 → 必须变绿。

用法：`.venv/bin/python scripts/mutations_p8.py [编号...]`

四个变异对应实验流水线里四条「会让结论变成假数字」的错，
每一条都是**最容易被自己糊弄过去**的那种：

  1. **偷看未来**：指数方向不再裁到 `<= asof` → 变体用上了次日的信息，
     成绩当然好看，但那是回测作弊不是样本外；
  2. **口径漂移**：挪标签带（±0.5% → ±1%）。数字会变好，但历史成绩与
     基线不再可比 —— 报告口径一漂，跨实验的结论全部作废；
  3. **拿封存段挑选变体**：`selection_split='test'` 不再被拒 →
     「用 test 挑变体」会以「我只是看一眼」的形式发生，test 从此不再是样本外；
  4. **污染分母**：不可评分的样本被算进配对计数 → 有效样本量虚高，
     而它恰恰是最像「样本量」的那个数字（行数比交易日数大得多）。

与 `mutations_p6.py` / `mutations_p7.py` 同一套纪律：原文件读进内存、
`finally` 无条件写回；「打红」那一步必须**真的跑了测试**
（`no tests ran` 不是绿，是无效实验 —— ERROR_DIARY 2026-09-15）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")

MUTATIONS = {
    1: {
        "name": "偷看未来：指数方向不再裁到 <= asof",
        "file": "stocklab/experiments/variants.py",
        "old": "    hist = [b for b in bars if b.date <= asof]\n",
        "new": ("    hist = list(bars)   # MUTATION: 不裁 asof —— 变体偷看了未来\n"),
        "tests": [
            "tests/test_experiments_variants.py::test_index_direction_reads_only_bars_up_to_asof",
            "tests/test_experiments_variants.py::test_index_direction_follows_the_asof_day_not_the_target_day",
        ],
    },
    2: {
        "name": "口径漂移：标签带 ±0.5% 被挪成 ±1%",
        "file": "stocklab/predict/model.py",
        "old": "FLAT_BAND = 0.005\n",
        "new": "FLAT_BAND = 0.01   # MUTATION: 挪标签带 —— 历史成绩与基线不再可比\n",
        "tests": [
            "tests/test_predict_model.py::test_flat_band_matches_spec_8_2",
            "tests/test_experiments_runner.py::test_report_states_the_metric_version_and_the_frozen_quantities",
        ],
    },
    3: {
        "name": "拿封存段挑选变体：selection_split='test' 不再被拒",
        "file": "stocklab/experiments/metrics.py",
        "old": """    if selection_split == "test":
        raise TestSetLeak(
            "selection_split='test' —— `test` 是封存段，只能被晋级评审读取一次，"
            "**不许**用来挑选变体。用 test 挑出来的变体，其 test 成绩不再是样本外"
        )
    if selection_split not in ("train", "validate"):
        raise ValueError(f"未知 selection_split={selection_split!r}")""",
        "new": """    # MUTATION: 封存段不再是封存段 —— test 被当成合法的选择依据
    if selection_split not in ("train", "validate", "test"):
        raise ValueError(f"未知 selection_split={selection_split!r}")""",
        "tests": [
            "tests/test_experiments_metrics.py::test_selection_on_the_test_split_is_refused",
            "tests/test_experiments_runner.py::test_selection_split_test_is_refused_before_anything_is_run",
        ],
    },
    4: {
        "name": "污染分母：不可评分的行被算进配对计数",
        "file": "stocklab/experiments/metrics.py",
        "old": """        if not (b[k].get("scorable") and v[k].get("scorable")):
            counts["unscorable_either"] += 1
            continue""",
        "new": """        if not (b[k].get("scorable") and v[k].get("scorable")):
            counts["unscorable_either"] += 1
            # MUTATION: 有行就算一对 —— 有效样本量被「没数据」撑大
            pairs.append((b[k], v[k]))
            continue""",
        "tests": [
            "tests/test_experiments_metrics.py::test_paired_delta_excludes_unscorable_from_both_sides",
            "tests/test_experiments_runner.py::test_unscorable_rows_are_recorded_but_do_not_enter_the_denominator",
        ],
    },
}


def run_pytest(tests: list[str]) -> tuple[int, str]:
    p = subprocess.run([PY, "-m", "pytest", *tests, "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def summary(out: str) -> str:
    for line in reversed(out.strip().splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip()
    return "(无汇总行)"


def main(which: list[int]) -> int:
    bad = 0
    for n in which:
        m = MUTATIONS[n]
        path = ROOT / m["file"]
        original = path.read_text(encoding="utf-8")
        assert m["old"] in original, f"变异 {n} 的锚点没找到（{m['file']}）"
        print(f"\n=== 变异 {n}：{m['name']} ===")
        print(f"文件：{m['file']}")
        try:
            path.write_text(original.replace(m["old"], m["new"], 1), encoding="utf-8")
            rc, out = run_pytest(m["tests"])
            print(f"  [打红] rc={rc}  {summary(out)}")
            if rc == 0 or "no tests ran" in out:
                print("  ❌ 变异**没有**被发现（或压根没跑测试）—— 无效实验")
                bad += 1
            else:
                print("  ✅ 测试变红了（这是期望结果）")
        finally:
            path.write_text(original, encoding="utf-8")

        rc, out = run_pytest(m["tests"])
        print(f"  [还原] rc={rc}  {summary(out)}")
        if rc != 0:
            print("  ❌ 还原后没有变绿 —— 工作区可能已损坏")
            bad += 1
        else:
            print("  ✅ 还原后变绿")
    return 1 if bad else 0


if __name__ == "__main__":
    nums = [int(x) for x in sys.argv[1:]] or sorted(MUTATIONS)
    raise SystemExit(main(nums))
