"""录制财报 fixture（**联网**，手工运行，不属于测试）。

用法：.venv/bin/python scripts/record_financials_fixtures.py
产物：tests/fixtures/financials/<code>.json（6 个端点的原始响应）

这个脚本是本项目**唯一**允许联网访问东财财报接口的入口。
测试文件（tests/test_ingest_financials.py 等）必须使用录制好的 fixture 回放，
不得直接联网。
"""
from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "financials"

# 要录制的代码：用典型的通用 / 银行 / 保险各一只
CODES = [
    ("000333", "通用"),   # 美的集团
    ("600036", "银行"),   # 招商银行
    ("601318", "保险"),   # 中国平安
]


def main() -> None:
    from stocklab.data.fetch import fetch_financial_reports
    from stocklab.data.http import HttpClient

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    client = HttpClient()
    from datetime import date
    fetched_date = date.today().isoformat()

    for code, org_type in CODES:
        print(f"录制 {code} ({org_type}) ...")
        try:
            reports, refs = fetch_financial_reports(
                client, code=code, org_type=org_type, fetched_date=fetched_date)
        except Exception as exc:
            print(f"  ❌ 失败: {exc}")
            continue
        out = {
            "code": code,
            "org_type": org_type,
            "fetched_date": fetched_date,
            "reports": [
                {k: getattr(r, k) for k in r.__dataclass_fields__}
                for r in reports
            ],
            "refs": refs,
        }
        path = FIXTURES_DIR / f"{code}.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✅ {len(reports)} 期财报 → {path}")


if __name__ == "__main__":
    main()
