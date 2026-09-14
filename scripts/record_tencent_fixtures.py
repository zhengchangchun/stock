#!/usr/bin/env python
"""一次性录制腾讯真实响应为测试 fixture（评审 C5 / 铁律②）。

用法:
    .venv/bin/python scripts/record_tencent_fixtures.py

产物（提交进仓库，测试离线回放）:
    tests/fixtures/tencent_quote_sz000333_sh600690.bin/.json   快照（GBK 原始字节）
    tests/fixtures/tencent_fqkline_day_sz000333.bin/.json      前复权日K + 除权事件

设计要点:
  - 走 `HttpClient`（白名单 + 限流 + 退避重试），落 `raw_fetch_cache` 后再从缓存
    读出**原始字节**写 fixture —— fixture 与运行时缓存同源，SHA256 一致。
  - 日K 请求用 qfq：**只为取除权事件（cqr/fh_sh）与校验行序**，
    qfq 价格序列禁止作为 bars_raw 落库（ADR-001 D-01 / 铁律①）。
  - 这是**手工运行**的一次性录制工具，不是定时任务；项目禁止 cron/守护进程（ADR-001 D-05）。

URL 常量与适配器的构造器是同一个契约：Task 9 的测试会断言
`tencent.kline_url(...)` 与本脚本记录进 fixture 元数据的 url 完全一致。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.config.paths import FIXTURE_DIR, RAW_CACHE_DIR, ensure_dirs  # noqa: E402
from stocklab.data.http import HttpClient, RetryPolicy  # noqa: E402
from stocklab.data.raw_cache import RawCache, load_fixture, record_fixture  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")

QUOTE_URL = "https://qt.gtimg.cn/q=sz000333,sh600690"
QUOTE_KEY = "q=sz000333,sh600690"
QUOTE_NAME = "tencent_quote_sz000333_sh600690"

KLINE_CODES = ("sz000333",)
KLINE_COUNT = 900
KLINE_ADJ = "qfq"
KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    f"?param={KLINE_CODES[0]},day,,,{KLINE_COUNT},{KLINE_ADJ}"
)
KLINE_KEY = "param=sz000333,day,,,900,qfq"
KLINE_NAME = "tencent_fqkline_day_sz000333"

HEADERS = {"User-Agent": "Mozilla/5.0 (stocklab fixture recorder)"}


def _summary(name: str, body: bytes, meta: dict) -> None:
    print(f"✅ {name}: {len(body)} 字节  sha256={meta.get('sha256', '?')[:12]}…")
    for k in ("n_quotes", "n_bars", "n_events", "first_date", "last_date"):
        if k in meta:
            print(f"     {k} = {meta[k]}")


def record_quote(client: HttpClient, cache: RawCache) -> dict:
    """腾讯快照：GBK 文本，含「手→股 / 万元→元」的原始字段。"""
    text = client.get_text(QUOTE_URL, encoding="gbk", headers=HEADERS,
                           source="tencent", cache_key=QUOTE_KEY)
    body = cache.load("tencent", QUOTE_KEY)          # 原始字节（已校验 SHA256）
    assert body is not None, "缓存未命中：原始字节必须落盘（铁律②）"

    codes = [
        line.split("=", 1)[0].strip()[2:]
        for line in text.split(";")
        if line.strip().startswith("v_")
    ]
    meta = {
        "source": "tencent_quote",
        "url": QUOTE_URL,
        "encoding": "gbk",
        "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "codes": codes,
        "n_quotes": len(codes),
        "note": "快照为「当前」值，非历史序列；仅用于解析与单位换算测试",
    }
    record_fixture(FIXTURE_DIR, QUOTE_NAME, body, meta)
    return meta


def record_kline(client: HttpClient, cache: RawCache) -> dict:
    """腾讯前复权日K：含除权事件 dict（cqr/djr/fh_sh/FHcontent）。"""
    text = client.get_text(KLINE_URL, headers=HEADERS, source="tencent",
                           cache_key=KLINE_KEY)
    body = cache.load("tencent", KLINE_KEY)
    assert body is not None, "缓存未命中：原始字节必须落盘（铁律②）"

    payload = json.loads(text)
    node = payload["data"][KLINE_CODES[0]]
    rows = node.get("qfqday") or node.get("day") or []
    events = [r[6] for r in rows if len(r) > 6 and isinstance(r[6], dict)]
    meta = {
        "source": "tencent_fqkline",
        "url": KLINE_URL,
        "encoding": "utf-8",
        "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "code": KLINE_CODES[0],
        "adj": KLINE_ADJ,
        "requested_count": KLINE_COUNT,
        "n_bars": len(rows),
        "first_date": rows[0][0] if rows else None,
        "last_date": rows[-1][0] if rows else None,
        "n_events": len(events),
        "events": events,
        "warning": (
            "qfq 价格序列仅用于行序校验/除权事件提取/交叉校验；"
            "禁止作为 bars_raw 落库（ADR-001 D-01）"
        ),
    }
    record_fixture(FIXTURE_DIR, KLINE_NAME, body, meta)
    return meta


def main() -> int:
    ensure_dirs()
    cache = RawCache(RAW_CACHE_DIR)
    # 腾讯 ≤5 req/s：min_interval 0.35s 留足余量；只录 2 个请求
    client = HttpClient(RetryPolicy(attempts=4, min_interval=0.5), cache=cache)

    for record in (record_quote, record_kline):
        meta = record(client, cache)
        _summary(meta.get("url", "?"), b"", meta)

    print("\n复核（从 fixture 重新读取并校验 SHA256）：")
    for name in (QUOTE_NAME, KLINE_NAME):
        body, meta = load_fixture(FIXTURE_DIR, name)
        print(f"  ✅ {name}: {len(body)} 字节，sha256 一致")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:                     # 铁律③：失败必须留痕，不静默
        print(f"❌ 录制失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
