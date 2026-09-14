"""实测腾讯/东财能回溯多久的日K —— 决定 walk-forward 是否可行（评审 B2）。

用法（**需要联网，不属于测试套**）:
    .venv/bin/python scripts/probe_history.py            # 全量
    .venv/bin/python scripts/probe_history.py --quick    # 只跑关键项（省钱）

输出: JSON —— 每个探测项的真实 URL、条数、首末日期、耗时、响应 SHA256。
原始响应同时落 `data/raw_cache/tencent/probe_*`（可复现）。

为什么探针要自己拼 URL 而不只用 `kline_url` 默认值：
  ADR-001 记「qfq 900 根」与上半轮实测「641 根」矛盾，只有把
  count / beg / end / adj 四个旋钮逐一扫过才能定位限制来源。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from stocklab.config import paths  # noqa: E402
from stocklab.data.http import HttpClient, RetryPolicy  # noqa: E402
from stocklab.data.raw_cache import RawCache  # noqa: E402
from stocklab.data.sources import eastmoney, tencent  # noqa: E402

TEN = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={}"


def _fetch(client: HttpClient, cache: RawCache, url: str, key: str) -> tuple[str, str]:
    """返回 (text, sha256[:16])；原始响应落缓存。"""
    text = client.get_text(url, source="tencent", cache_key=key)
    body = text.encode("utf-8")
    if not cache.has("tencent", key):
        cache.store("tencent", key, url, body, encoding="utf-8", fetched_at="probe")
    return text, hashlib.sha256(body).hexdigest()[:16]


def _summary(rows: list) -> dict:
    return {"count": len(rows), "first_req": None if not rows else rows[0],
            "last_req": None if not rows else rows[-1]}


def _bars(url: str, client: HttpClient, cache: RawCache, code: str,
          adj: str, key: str) -> dict:
    t0 = time.time()
    try:
        text, sha = _fetch(client, cache, url, key)
    except Exception as exc:                      # noqa: BLE001 — 探针要报告一切失败
        return {"url": url, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 2)}
    secs = round(time.time() - t0, 2)
    try:
        payload = json.loads(text)
    except ValueError:
        return {"url": url, "error": "非 JSON 响应", "bytes": len(text),
                "seconds": secs, "sha256_16": sha}
    msg = payload.get("msg")
    if msg:
        return {"url": url, "error": f"服务端 msg={msg!r}", "seconds": secs,
                "sha256_16": sha}
    bars = tencent.parse_kline(payload, code, adj_mode=adj or "none")
    return {"url": url, "count": len(bars),
            "first": bars[0].date if bars else None,
            "last": bars[-1].date if bars else None,
            "bytes": len(text), "seconds": secs, "sha256_16": sha}


def probe_tencent_count_cap(client, cache, code="sz000333",
                            counts=(320, 640, 800, 801, 900, 1200, 2000, 2500)):
    """扫 count 旋钮：找单次请求的实际上限（ADR-001 的 801/900 之争）。"""
    out = []
    for cnt in counts:
        for adj in (("", "qfq") if cnt in (320, 800, 2000, 2500) else ("",)):
            url = TEN.format(f"{code},day,,,{cnt},{adj}")
            r = _bars(url, client, cache, code[2:], adj, f"probe_count_{cnt}_{adj or 'bfq'}")
            r["requested"] = cnt
            r["adj"] = adj or "none"
            out.append(r)
    return out


def probe_tencent_pagination(client, cache, code="sz000333", page=tencent.MAX_COUNT,
                             max_pages=12, adj=""):
    """向后翻页：以「上一页首日的前一天」为新的 end，直到返回空。

    beg 被服务端忽略（实测），只有 end 能定位窗口 —— 这是取全量历史的唯一方式。
    """
    end = (date.today() + timedelta(days=1)).isoformat()
    pages, seen = [], set()
    for i in range(max_pages):
        url = tencent.kline_url(code, page, adj, end=end)
        r = _bars(url, client, cache, code[2:], adj, f"probe_page_{code}_{end}_{adj or 'bfq'}")
        r["page"] = i
        r["end_anchor"] = end
        pages.append(r)
        if r.get("error") or not r.get("count"):
            break
        first = r["first"]
        if first in seen:                      # 服务端不再往前 → 到底了
            r["note"] = "首日与前页重复，已到数据尽头"
            break
        seen.add(first)
        end = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
    return pages


def probe_eastmoney(client, cache, secid="0.000333", code="000333"):
    """东财长历史（ADR-001 记其返回空响应/限流，需复测）。"""
    out = []
    for tag, beg, end in (("beg=0", "0", "20500101"),
                          ("beg=19900101", "19900101", "20260914")):
        url = eastmoney.kline_url(secid, beg=beg, end=end, adjust=0)
        t0 = time.time()
        try:
            text, sha = _fetch(client, cache, url, f"probe_em_{beg}")
            payload = json.loads(text)
            bars = eastmoney.parse_kline(payload, code, adj_mode="none")
            out.append({"tag": tag, "url": url, "count": len(bars),
                        "first": bars[0].date if bars else None,
                        "last": bars[-1].date if bars else None,
                        "bytes": len(text), "seconds": round(time.time() - t0, 2),
                        "sha256_16": sha})
        except Exception as exc:                  # noqa: BLE001
            out.append({"tag": tag, "url": url,
                        "error": f"{type(exc).__name__}: {str(exc)[:90]}",
                        "seconds": round(time.time() - t0, 2)})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只跑关键项")
    ap.add_argument("--code", default="sz000333")
    args = ap.parse_args(argv)

    paths.ensure_dirs()
    cache = RawCache(paths.RAW_CACHE_DIR)
    client = HttpClient(RetryPolicy(attempts=2, base_delay=0.5, min_interval=0.35),
                        cache=cache)
    report = {"probed_at": date.today().isoformat(), "code": args.code}
    report["count_cap"] = probe_tencent_count_cap(
        client, cache, args.code,
        counts=(800, 801, 900, 2000, 2500) if args.quick
        else (320, 640, 800, 801, 900, 1200, 2000, 2500))
    report["pagination"] = probe_tencent_pagination(
        client, cache, args.code, max_pages=2 if args.quick else 12)
    report["eastmoney"] = probe_eastmoney(client, cache)
    if not args.quick:
        report["pagination_old_stock"] = probe_tencent_pagination(
            client, cache, "sh600000", max_pages=12)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
