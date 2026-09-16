#!/usr/bin/env python
"""P30 变异反证：注入真实 bug → 目标测试**必须变红** → 还原 → 必须变绿。

用法：`.venv/bin/python scripts/mutations_p30.py [编号...]`

五个变异各自打在**真正的防线**上（ERROR_DIARY 2026-09-15：变异要打在防线上，
打在被上游挡住的地方会「测不出东西」，那是无效实验）：

  1. **绕过休市表**：`build_predictions` 不再把 `market_holidays` 传进
     `resolve_target_date` → 退回 `weekday_fallback`。**这是本轮的主变异** ——
     它精确复现「P30 之前的行为」：长假当天被当成交易日。表建好了、写进了库、
     报告里也有字段，但**没有任何一处真的用它** —— 这类「接线没接上」的 bug
     不会被「模块单测绿了」发现，只有端到端测试能抓。
  2. **接上了但不算数**：休市表传了，却不跳过已公告的休市日（循环恒不执行）→
     `source` 写着 `holiday_table`，`target_date` 却还是放假那天。
     比 1 更阴险：**字段看起来是对的**（来源自称查了公告），值是错的。
  3. **覆盖判据失效**：`covers()` 恒 True → 未公告的年份（如 2027）也敢声称
     「我知道那天开不开市」。这就是「假装知道」—— 覆盖不到时必须如实退回
     `weekday_fallback`。
  4. **解析 fail-closed 消失**：正文含「休市」却一条日期都解不出时返回**空表**
     而不是抛错 → 「源站换了排版」被读成「今年不放假」。这是最危险的一类静默降级：
     它不报错、不留痕，只是悄悄把一整年的休市日变成开市日。
  5. **抓取 fail-closed 消失**：某一篇公告解析失败时**就地跳过**而不是整批失败 →
     半批数据入库。后果同 4，且更隐蔽：多数条目是对的，只有几个节悄悄没了。

与 `mutations_p6.py` / `mutations_p7.py` / `mutations_p8.py` 同一套纪律：
原文件读进内存、`finally` 无条件写回；「打红」那一步必须**真的跑了测试**
（`no tests ran` 不是绿，是无效实验 —— ERROR_DIARY 2026-09-15）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")

SERVICE = "stocklab/predict/service.py"
HOLIDAYS = "stocklab/calendar/holidays.py"
FETCH = "stocklab/data/fetch.py"

MUTATIONS = {
    1: {
        "name": "绕过休市表：build_predictions 不把 market_holidays 传下去",
        "file": SERVICE,
        "old": "    target_date, td_source = resolve_target_date(calendar, asof, holidays=holidays)\n",
        "new": ("    # MUTATION: 不传 holidays —— 退回 P30 之前的 weekday_fallback\n"
                "    target_date, td_source = resolve_target_date(calendar, asof)\n"),
        "tests": [
            "tests/test_calendar_holidays.py::test_predict_payload_skips_announced_holidays_end_to_end",
        ],
    },
    2: {
        "name": "接上了但不算数：不跳过已公告的休市日（source 仍自称 holiday_table）",
        "file": SERVICE,
        "old": "        while holidays.covers(d) and holidays.is_closed(d):\n",
        "new": "        while False and holidays.covers(d) and holidays.is_closed(d):\n",
        "tests": [
            "tests/test_calendar_holidays.py::test_predict_payload_skips_announced_holidays_end_to_end",
        ],
    },
    3: {
        "name": "假装知道：覆盖判据 covers() 恒 True（未公告的年份也敢答）",
        "file": HOLIDAYS,
        "old": "        return int(d[:4]) in self.annual_years\n",
        "new": "        return True   # MUTATION: 任何年份都声称「我知道开不开市」\n",
        "tests": [
            "tests/test_calendar_holidays.py::test_resolve_target_date_falls_back_when_the_table_does_not_cover",
        ],
    },
    4: {
        "name": "静默降级：公告解不出日期时返回空表（「换排版」读成「不放假」）",
        "file": HOLIDAYS,
        "old": "    if not dates:\n        raise HolidayParseError(\n",
        "new": "    if not dates:\n        return []   # MUTATION: 静默返回空表\n    if False:\n        raise HolidayParseError(\n",
        "tests": [
            "tests/test_calendar_holidays.py::test_notice_without_any_parsable_date_fails_closed",
        ],
    },
    5: {
        "name": "半批入库：单篇公告解析失败时就地跳过而非整批失败",
        "file": FETCH,
        "old": ("        except Exception as exc:                      "
                "# 逐篇失败 → 整批失败（见 docstring）\n"),
        "new": "        except Exception:   # MUTATION: 就地跳过，半批数据照样返回\n",
        "tests": [
            "tests/test_calendar_holidays.py::test_fetch_fails_closed_when_an_article_cannot_be_parsed",
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


def failure_lines(out: str, limit: int = 4) -> list[str]:
    """摘出失败断言行（不只贴退出码 —— ERROR_DIARY「贴证据别贴结论」）。"""
    out_lines = [ln.rstrip() for ln in out.splitlines()]
    marker = [i for i, ln in enumerate(out_lines) if ln.startswith("FAILED")]
    if marker:
        return [out_lines[i] for i in marker[:limit]]
    return [ln for ln in out_lines if ln.startswith("E ")] [:limit]


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
            for ln in failure_lines(out):
                print(f"         {ln}")
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
