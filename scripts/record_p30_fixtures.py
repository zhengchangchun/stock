#!/usr/bin/env python
"""一次性录制上交所「休市安排」fixture（P30）。

用法:
    .venv/bin/python scripts/record_p30_fixtures.py

产物（提交进仓库，供离线测试回放）:
    tests/fixtures/sse_holiday_list.{bin,json}              列表页原始 HTML
    tests/fixtures/sse_holiday_c_YYYYMMDD_NNNNNNN.{bin,json} 列表页里**每一篇**公告

## 为什么是「列表页里每一篇」，不是一个手挑的短名单

上一轮只录了 3 篇手挑的公告，于是回放测试挂在 `ReplayClient` 的
「未录制的 URL」上 —— 而 `fetch_holiday_notices` 是**整批 fail-closed** 的，
漏一篇就整批失败。**手挑名单 = 一个会飘的映射**：列表页多一篇公告，
回放测试就从「验证抓取」退化成「验证我上次挑的那 3 篇」。
本脚本改成**从列表页现推**：抓到什么就录什么，测试侧同样按 id 现推，
两边共用同一个 `sse.parse_article_list`（单一真源）。

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

LIST_FIXTURE = "sse_holiday_list"


def fixture_name(aid: str) -> str:
    """公告 id（`c_YYYYMMDD_NNNNNNN`）→ fixture 名。测试侧同款。"""
    return f"sse_holiday_{aid}"


def main() -> int:
    settings = load_settings()
    client = HttpClient(RetryPolicy(min_interval=settings.http_min_interval,
                                    timeout=settings.http_timeout))
    stamp = now_iso()
    headers = {"User-Agent": "Mozilla/5.0"}

    page = client.get_text(sse.LIST_URL, source="sse", headers=headers)
    record_fixture(FIXTURE_DIR, LIST_FIXTURE, page.encode("utf-8"),
                   {"url": sse.LIST_URL, "recorded_at": stamp, "encoding": "utf-8",
                    "note": "上交所休市安排栏目列表页（服务端渲染）"})
    print(f"✅ {LIST_FIXTURE}  ({len(page.encode('utf-8'))} 字节)")

    # 现推文章清单：解析器与生产**同一个**，不手工维护短名单。
    articles = sse.parse_article_list(page)
    if not articles:
        print("❌ 列表页一条公告都没解析出来 —— 拒绝录空（源站换排版必须表现为失败）")
        return 1

    for art in articles:
        aid = art["url"].rsplit("/", 1)[-1].removesuffix(".shtml")
        body = client.get_text(art["url"], source="sse", headers=headers)
        raw = body.encode("utf-8")
        name = fixture_name(aid)
        record_fixture(FIXTURE_DIR, name, raw,
                       {"url": art["url"], "recorded_at": stamp, "encoding": "utf-8",
                        "published_at": sse.published_at_from_article(body),
                        "title": art["title"], "doc_kind": art["doc_kind"],
                        "covered_year": art["covered_year"],
                        "note": "上交所休市安排公告"})
        print(f"✅ {name}  ({len(raw)} 字节)  {art['title']}")
    print(f"\n共 {len(articles)} 篇公告 + 1 个列表页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
