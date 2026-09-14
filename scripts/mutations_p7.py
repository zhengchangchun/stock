#!/usr/bin/env python
"""P7 变异反证：注入真实 bug → 目标测试**必须变红** → 还原 → 必须变绿。

用法：`.venv/bin/python scripts/mutations_p7.py [编号...]`

四个变异对应本任务四条「绝不允许」的错，每一条都是**会让准确率变成假数字**的那种：

  1. 用**不复权价**（正则裁剪前）当实际行情 → 除权日的假跌幅把方向判反
  2. 已有 verification 被**静默覆盖** → 准确率成了可以随手编辑的数字
  3. 把**不可评分**的案例当成成功算进分母 → 分母被污染，准确率虚高
  4. 把 `UNDETERMINED` 自动写成 `NOISE` → 造假归因（代码判不了的四类）

与 `mutations_p6.py` 同一套纪律：原文件读进内存、`finally` 无条件写回；
「打红」那一步必须**真的跑了测试**（`no tests ran` 不是绿，是无效实验）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")

MUTATIONS = {
    1: {
        "name": "口径降级：用不复权价（裁剪前）当实际行情",
        "file": "stocklab/verify/score.py",
        "old": "    adj = _by_date(bars)\n",
        "new": ("    adj = _by_date(raw_bars)   # MUTATION: 拿不复权价当实际价，"
                "除权日的假跌幅会留下\n"),
        "tests": ["tests/test_verify_score.py::test_score_uses_adjusted_prices_not_raw_ones",
                  "tests/test_verify_score.py::test_unadjusted_series_is_rejected_not_silently_used"],
    },
    2: {
        "name": "静默覆盖：已可评分的 verification 被直接 UPDATE 改写",
        "file": "stocklab/verify/store.py",
        "old": """        raise VerificationConflict(
            f"pred_id={pred_id} 已有一条内容不同的验证记录\"""",
        "new": """        # MUTATION: 不拒绝，直接把结果列覆盖掉
        sets = ", ".join(f"{c}=:{c}" for c in _RESCORE_COLUMNS)
        conn.execute(
            f"UPDATE verifications SET {sets} WHERE verification_id=:vid",
            {**_to_columns(payload), "vid": int(row["verification_id"])})
        conn.commit()
        return "identical", int(row["verification_id"])
        raise VerificationConflict(
            f"pred_id={pred_id} 已有一条内容不同的验证记录\"""",
        "tests": ["tests/test_verify_store.py::test_different_content_is_refused_not_overwritten",
                  "tests/test_verify_store.py::test_scorable_row_is_never_downgraded_to_unscorable"],
    },
    3: {
        "name": "污染分母：把「不可评分」当成成功（结果列写 1 而不是 NULL）",
        "file": "stocklab/verify/score.py",
        "old": """    out["scorable"] = False
    out["reason_code"] = reason_code""",
        "new": """    out["scorable"] = False
    out["reason_code"] = reason_code
    # MUTATION: 结果列不再是 NULL —— 「没数据」被算成了「预测对了」
    out["hit_direction"] = 1
    out["hit_range"] = 1
    out["hit_levels"] = 1
    out["score_direction"] = 1.0
    out["score_range"] = 1.0""",
        "tests": ["tests/test_verify_score.py::test_missing_target_bar_is_unscorable_data",
                  "tests/test_verify_score.py::test_unscorable_carries_no_score_at_all"],
    },
    4: {
        "name": "造假归因：把 UNDETERMINED 自动写成 NOISE",
        "file": "stocklab/verify/score.py",
        "old": 'ATTRIBUTION_UNDETERMINED = "UNDETERMINED"',
        "new": ('ATTRIBUTION_UNDETERMINED = "NOISE"   # MUTATION: 自动硬判归因'
                '（代码判不了 SIGNAL/STRATEGY/MODEL/NOISE）'),
        "tests": ["tests/test_verify_score.py::test_attribution_is_never_auto_assigned_to_a_cause",
                  "tests/test_verify_service.py::test_attribution_is_never_one_of_the_human_only_classes"],
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
