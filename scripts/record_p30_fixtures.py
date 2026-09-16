#!/usr/bin/env python
"""一次性录制上交所「休市安排」fixture（P30）。

用法:
    .venv/bin/python scripts/record_p30_fixtures.py

产物（提交进仓库，供离线测试回放）:
    tests/fixtures/sse_holiday_list.html                        列表页原始 HTML
    tests/fixtures/sse_holiday_2026_annual.html                 2026 年度通知
    tests/fixtures/sse_holiday_2026_dragonboat.html             2026 端午节单节公告
    tests/fixtures/sse_holiday_2025_annual.html                 2025 年度通知
    *.json                                                      溯源元数据 + sha256

注意：这是**手工运行**的录制工具，不是定时任务（项目禁止 cron/守护进程）。
走 `HttpClient`（R13 白名单 + 限流 + raw_cache），与生产同一条通道 ——
这样「录制」与「实时抓取」的解析路径完全一致（ADR-003 的教训）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.config.paths import FIXTURE_DIR  # noqa: E402
from stocklab.config.settings import load_settings  # noqa: E402
from stocklab.data.http import HttpClient, RetryPolicy, now_iso  # noqa: E402
from stocklab.data.raw_cache import record_fixture  # noqa: E402
from stocklab.data.sources import sse  # noqa: E402

#: (fixture 名, 文章 URL)。列表页单独录，名字固定。
ARTICLES = [
    ("sse_holiday_2026_annual",
     "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml"),
    ("sse_holiday_2026_dragonboat",
     "https://www.sse.com.cn/disclosure/announcement/general/c/c_20260611_10821419.shtml"),
    ("sse_holiday_2025_annual",
     "https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml"),
]


def main() -> int:
    settings = load_settings()
    client = HttpClient(RetryPolicy(min_interval=settings.http_min_interval,
                                    timeout=settings.http_timeout))
    stamp = now_iso()

    page = client.get_text(sse.LIST_URL, source="sse", headers={"User-Agent": "Mozilla/5.0"})
    p = record_fixture(FIXTURE_DIR, "sse_holiday_list", page.encode("utf-8"),
                       {"url": sse.LIST_URL, "recorded_at": stamp, "encoding": "utf-8",
                        "note": "上交所休市安排栏目列表页（服务端渲染）"})
    print(f"✅ {p}  ({len(page.encode('utf-8'))} 字节)")

    for name, url in ARTICLES:
        body = client.get_text(url, source="sse", headers={"User-Agent": "Mozilla/5.0"})
        raw = body.encode("utf-8")
        p = record_fixture(FIXTURE_DIR, name, raw,
                           {"url": url, "recorded_at": stamp, "encoding": "utf-8",
                            "published_at": sse.published_at_from_article(body),
                            "note": "上交所休市安排公告"})
        print(f"✅ {p}  ({len(raw)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
