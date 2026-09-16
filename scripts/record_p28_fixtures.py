#!/usr/bin/env python
"""一次性录制 P28 估值 / 资金流真实响应为测试 fixture（评审 C5 / 铁律②）。

用法:
    .venv/bin/python scripts/record_p28_fixtures.py

产物（提交进仓库，测试离线回放）:
    tests/fixtures/eastmoney_valuation_000333_p1_s3.bin/.json   估值第 1 页（pageSize=3，pages=705）
    tests/fixtures/eastmoney_valuation_510300_p1_s3.bin/.json   估值 ETF 无数据（pageSize=3，pages=0）
    tests/fixtures/sina_moneyflow_000333_p1_n3.bin/.json        资金流第 1 页（num=3）
    tests/fixtures/sina_moneyflow_000333_pastend_n3.bin/.json   资金流翻越尽头（num=3，空数组）

设计要点（同 record_tencent_fixtures.py）:
  - 走 `HttpClient`（白名单 + 限流 + 退避重试），落 `raw_fetch_cache` 后从缓存读出
    **原始字节**写 fixture —— fixture 与运行时缓存同源，SHA256 一致。
  - 这是**手工运行**的一次性录制工具，不是定时任务；项目禁止 cron/守护进程。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.config.paths import FIXTURE_DIR, RAW_CACHE_DIR, ensure_dirs  # noqa: E402
from stocklab.data.http import HttpClient, RetryPolicy  # noqa: E402
from stocklab.data.raw_cache import RawCache, load_fixture, record_fixture  # noqa: E402
from stocklab.data.sources import eastmoney, sina  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")

HEADERS = {"User-Agent": "Mozilla/5.0 (stocklab p28 fixture recorder)"}

# (name, url, source, cache_key, headers, note)
RECORDS = [
    (
        "eastmoney_valuation_000333_p1_s3",
        eastmoney.valuation_url("000333", page=1, page_size=3),
        "eastmoney",
        "valuation:000333:2026-09-16:1:3",
        eastmoney.DATACENTER_HEADERS,
        "估值第 1 页（pageSize=3，实测 result.pages=705）：分页/解析的真实样本",
    ),
    (
        "eastmoney_valuation_510300_p1_s3",
        eastmoney.valuation_url("510300", page=1, page_size=3),
        "eastmoney",
        "valuation:510300:2026-09-16:1:3",
        eastmoney.DATACENTER_HEADERS,
        "估值 ETF 无数据（pages=0 / data 空）：『无数据 → 空列表』的真实样本",
    ),
    (
        "sina_moneyflow_000333_p1_n3",
        sina.moneyflow_url("sz000333", page=1, num=3),
        "sina",
        "moneyflow:sz000333:2026-09-16:1:3",
        HEADERS,
        "资金流第 1 页（num=3）：3 行真实样本",
    ),
    (
        "sina_moneyflow_000333_pastend_n3",
        sina.moneyflow_url("sz000333", page=9999, num=3),
        "sina",
        "moneyflow:sz000333:2026-09-16:9999:3",
        HEADERS,
        "资金流翻越尽头（num=3）：真实空数组，翻页终止判据样本",
    ),
]


def main() -> int:
    ensure_dirs()
    cache = RawCache(RAW_CACHE_DIR)
    # 新浪/东财 datacenter 限流不严；min_interval 0.5s 留足余量，仅 4 个请求
    client = HttpClient(RetryPolicy(attempts=4, min_interval=0.5), cache=cache)

    for name, url, source, cache_key, headers, note in RECORDS:
        client.get_text(url, headers=headers, source=source, cache_key=cache_key)
        body = cache.load(source, cache_key)
        assert body is not None, f"缓存未命中: {name}（原始字节必须落盘）"
        record_fixture(
            FIXTURE_DIR,
            name,
            body,
            {
                "source": source,
                "url": url,
                "encoding": "utf-8",
                "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "note": note,
            },
        )
        print(f"✅ {name}: {len(body)} 字节")

    print("\n复核（从 fixture 重新读取并校验 SHA256）：")
    for name, *_ in RECORDS:
        body, meta = load_fixture(FIXTURE_DIR, name)
        print(f"  ✅ {name}: {len(body)} 字节，sha256 一致")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:                     # 铁律③：失败必须留痕，不静默
        print(f"❌ 录制失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
