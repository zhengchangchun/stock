#!/usr/bin/env python
"""P6 变异反证：注入真实 bug → 目标测试**必须变红** → 还原 → 必须变绿。

用法：`.venv/bin/python scripts/mutations_p6.py [编号...]`

为什么要脚本而不是手工改：手工改容易忘记还原，而「忘记还原」比不测更危险
（下一次跑的绿是假的）。本脚本把原文件读进内存，`finally` 里无条件写回。

五个变异各自打在**真正的防线**上（ERROR_DIARY 2026-09-15：变异要打在防线上，
打在被上游挡住的地方会「测不出东西」，那是无效实验）：

  1. 模型层去掉 `<= asof` 裁剪        → 前视
  2. 落库去掉四态 + 改 `INSERT OR REPLACE` → 静默覆盖
  3. 复权链不可用时回退到不复权价       → 口径静默降级
  4. 读 `pe_pct_3y` 并 `or 0.0` 当默认值 → NULL 当 0
  5. `p_touch` 的 z 二次标准化         → 概率恒饱和 0/1（真实回放发现）

变异 5 与 1-4 不同：它不是预防性的，是**真实数据回放先暴露 bug、再补的反证**
（`p_touch` 四个价位全是 0.0）。补它的目的是钉住「这类 bug 不会再溜过去」。

**「打红」那一步必须真的跑了测试**：脚本会断言输出里出现 `failed` 且不是
`no tests ran`（ERROR_DIARY 2026-09-15：`no tests ran` 不是绿，是无效实验）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")

MUTATIONS = {
    1: {
        "name": "前视：模型层去掉 <= asof 裁剪",
        "file": "stocklab/predict/model.py",
        "old": "    usable = [b for b in bars if b.date <= asof]\n",
        "new": "    usable = list(bars)   # MUTATION: 不裁剪，未来数据进入窗口\n",
        "tests": ["tests/test_predict_model.py::test_model_ignores_bars_after_asof",
                  "tests/test_predict_service.py::test_future_bars_do_not_change_the_payload"],
    },
    2: {
        "name": "静默覆盖：落库改成 INSERT OR REPLACE 且不做四态检查",
        "file": "stocklab/predict/store.py",
        "old": """    key = (payload["code"], payload["asof_date"], payload["model_version"])
    row = find_prediction(conn, *key)""",
        "new": """    key = (payload["code"], payload["asof_date"], payload["model_version"])
    # MUTATION: 跳过四态检查，直接覆盖
    conn.execute(_INSERT_SQL.replace("INSERT INTO", "INSERT OR REPLACE INTO"),
                 payload_to_row(payload, now=now))
    conn.commit()
    return "inserted", int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    row = find_prediction(conn, *key)""",
        "tests": ["tests/test_predict_store.py::test_conflicting_payload_for_same_key_is_rejected",
                  "tests/test_predict_store.py::test_same_payload_is_identical_not_duplicated"],
    },
    3: {
        "name": "口径降级：复权链不可用时回退到不复权价",
        "file": "stocklab/predict/service.py",
        "old": """    floor = adjust.usable_from(conn, code)
    if floor is not None and asof < floor:
        raise UnusableWindow(""",
        "new": """    floor = adjust.usable_from(conn, code)
    if floor is not None and asof < floor:
        # MUTATION: 回退到不复权价（bars_daily 原样读出）
        from stocklab.data.models import Bar as _Bar
        return [_Bar(code=r["code"], date=r["date"], open=r["open"], high=r["high"],
                     low=r["low"], close=r["close"], volume=r["volume"],
                     amount=r["amount"], turnover=r["turnover"], source=r["source"],
                     adj_mode=r["adj_mode"])
                for r in conn.execute("SELECT * FROM bars_daily WHERE code=?"
                                      " AND date<=? ORDER BY date", (code, asof))]
    if False:
        raise UnusableWindow(""",
        "tests": ["tests/test_predict_service.py::test_blackout_window_is_hard_rejected",
                  "tests/test_predict_service.py::test_load_pit_bars_never_returns_unadjusted_prices"],
    },
    4: {
        "name": "NULL 当 0：读 pe_pct_3y 并用 `or 0.0` 当默认值参与载荷",
        "file": "stocklab/predict/service.py",
        "old": """    for code in sorted(history):
        try:""",
        "new": """    for code in sorted(history):
        # MUTATION: 把全 NULL 的 pe_pct_3y 当 0 用，塞进策略权重
        _r = conn.execute("SELECT pe_pct_3y FROM features_daily WHERE code=? AND date=?",
                          (code, asof)).fetchone()
        mix["weights"]["pe_pct_3y"] = float((_r["pe_pct_3y"] if _r else None) or 0.0)
        try:""",
        "tests": ["tests/test_predict_service.py::test_payload_is_invariant_to_features_daily"],
    },
    5: {
        "name": "二次标准化：p_touch 的 z 喂回 Normal(mu,sigma) → 概率恒饱和",
        "file": "stocklab/predict/model.py",
        "old": "        p = 1.0 - float(std.cdf(z)) if above else float(std.cdf(z))\n",
        "new": "        p = 1.0 - float(nd.cdf(z)) if above else float(nd.cdf(z))"
               "   # MUTATION: z 已标准化，再喂带 mu/sigma 的分布\n",
        "tests": ["tests/test_predict_model.py::"
                  "test_p_touch_matches_the_standard_normal_reference"],
    },
}


def run_pytest(tests: list[str]) -> tuple[int, str]:
    """`-k` 之类一律用 list 传参；这里用 nodeid，天然是独立 argv token。"""
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
        assert m["old"] in original, f"变异 {n} 的锚点没找到（{m['file']}）：{m['old']!r}"
        print(f"\n=== 变异 {n}：{m['name']} ===")
        print(f"文件：{m['file']}")
        try:
            path.write_text(original.replace(m["old"], m["new"], 1), encoding="utf-8")
            rc, out = run_pytest(m["tests"])
            red = summary(out)
            print(f"  [打红] rc={rc}  {red}")
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
