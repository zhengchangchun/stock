"""实测「腾讯 fqkline 的除权除息事件行」到底挂在哪、覆盖到哪 —— ADR-004 的证据来源。

用法（**需要联网，不属于测试套**）:
    .venv/bin/python scripts/probe_corp_actions.py --quick
    .venv/bin/python scripts/probe_corp_actions.py

要回答的三个问题（ADR-001 D-01 自建因子链的前提）：
  P1. `adj=none`（不复权）的响应里**是否也带事件行**（行尾第 7 个元素 dict）？
      若不带，取事件就只能走 qfq，于是**继承 qfq 的 800 根上限**，翻页次数翻倍。
  P2. 事件是否随分页一起到位 → 能否回溯到**上市首日**（全历史事件）。
  P3. qfq 的静默降级点复测：801~2000 之间是否仍回落到 640 根（ADR-003）。
      探针**刻意绕过** `fetch._validate` 的守卫，直接拼 URL 复现上游行为。

输出: JSON —— 每项的完整 URL、条数、事件条数、首末事件日、耗时、响应 SHA256。
原始响应落 `data/raw_cache/tencent/probe_ca_*`（可复现）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.config import paths  # noqa: E402
from stocklab.data.http import HttpClient, RetryPolicy  # noqa: E402
from stocklab.data.raw_cache import RawCache  # noqa: E402
from stocklab.data.sources import tencent  # noqa: E402

#: qfq 单次上限（ADR-003 实测）；探针里用它作为 qfq 翻页步长
QFQ_PAGE = 800


def _fetch(client: HttpClient, cache: RawCache, url: str, key: str) -> tuple[str, str]:
    text = client.get_text(url, source="tencent", cache_key=key)
    body = text.encode("utf-8")
    if not cache.has("tencent", key):
        cache.store("tencent", key, url, body, encoding="utf-8", fetched_at="probe")
    return text, hashlib.sha256(body).hexdigest()[:16]


def _event_rows(payload: dict, code: str, adj: str) -> tuple[list[list], list[dict]]:
    """返回 (原始行, 事件列表)。事件 = 行尾第 7 个元素是 dict 且含 cqr/fh_sh。"""
    rows = tencent._rows(payload, code, adj or "none")
    events: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= tencent._EVENT_INDEX:
            continue
        ev = row[tencent._EVENT_INDEX]
        if isinstance(ev, dict):
            events.append({"row_date": str(row[0]), **ev})
    return rows, events


def _probe_page(client: HttpClient, cache: RawCache, code: str, adj: str,
                count: int, end: str, tag: str) -> dict:
    url = tencent.kline_url(code, count, adj, end=end)
    t0 = time.time()
    try:
        text, sha = _fetch(client, cache, url, f"probe_ca_{tag}_{code}_{end}_{adj or 'bfq'}")
    except Exception as exc:                       # noqa: BLE001 — 探针要报告一切失败
        return {"url": url, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 2)}
    secs = round(time.time() - t0, 2)
    try:
        payload = json.loads(text)
    except ValueError:
        return {"url": url, "error": "非 JSON 响应", "seconds": secs, "sha256_16": sha}
    if payload.get("msg"):
        return {"url": url, "error": f"服务端 msg={payload['msg']!r}",
                "seconds": secs, "sha256_16": sha}
    rows, events = _event_rows(payload, code[2:], adj)
    return {
        "url": url, "requested": count, "adj": adj or "none",
        "count": len(rows),
        "first": str(rows[0][0]) if rows else None,
        "last": str(rows[-1][0]) if rows else None,
        "rows_with_event": sum(1 for r in rows
                               if isinstance(r, (list, tuple))
                               and len(r) > tencent._EVENT_INDEX
                               and isinstance(r[tencent._EVENT_INDEX], dict)),
        "n_events": len(events),
        "events": events,
        # 事件行是否只出现在 qfq 口径：P1 的核心判据
        "event_field_names": sorted({k for e in events for k in e}),
        "bytes": len(text), "seconds": secs, "sha256_16": sha,
    }


def probe_single_page(client, cache, code: str) -> dict:
    """P1：同一窗口下 none / qfq 的**单页**对比 —— 事件行挂在哪一侧。"""
    end = (date.today() + timedelta(days=1)).isoformat()
    out = {}
    for adj in ("", "qfq"):
        out[adj or "none"] = _probe_page(client, cache, code, adj, 800, end,
                                         "single")
    return out


def probe_count_cap(client, cache, code: str,
                    counts=(800, 801, 900, 2000)) -> list[dict]:
    """P3：复测 qfq 静默降级点（绕过 fetch 的守卫，复现上游行为）。"""
    end = (date.today() + timedelta(days=1)).isoformat()
    return [_probe_page(client, cache, code, "qfq", c, end, "cap")
            for c in counts]


def probe_pagination(client, cache, code: str, adj: str, page: int,
                     max_pages: int = 12) -> tuple[dict, dict[str, float]]:
    """P2：以 `end` 为锚点向后翻页直到空页，统计**全历史事件**覆盖。

    返回 (JSON 可序列化的摘要, {日期: 收盘价})；价格只用于 P4 分析，不进报告。
    """
    end = (date.today() + timedelta(days=1)).isoformat()
    pages, events, seen, closes = [], [], set(), {}
    for i in range(max_pages):
        r = _probe_page(client, cache, code, adj, page, end, "page")
        r["page"] = i
        r["end_anchor"] = end
        pages.append({k: v for k, v in r.items() if k != "events"})
        if r.get("error") or not r.get("count"):
            break
        for e in r["events"]:
            if e["cqr"] not in seen:
                seen.add(e["cqr"])
                events.append(e)
        # 重新解析一次拿价格（_probe_page 的摘要里没有价格序列）
        for row in tencent._rows(json.loads(
                cache.load("tencent", f"probe_ca_page_{code}_{end}_{adj or 'bfq'}"
                           ).decode("utf-8")), code[2:], adj or "none"):
            closes.setdefault(str(row[0]), float(row[2]))
        first = r["first"]
        end = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
    events.sort(key=lambda e: e["cqr"])
    filled = [p for p in pages if p.get("count")]
    return {
        "adj": adj or "none", "page_size": page, "n_pages": len(pages),
        "pages": pages,
        "total_bars": sum(p.get("count") or 0 for p in pages),
        # 覆盖区间用**最后一个非空页**的首日（空页的 first 是 None）
        "coverage_first": filled[-1].get("first") if filled else None,
        "coverage_last": filled[0].get("last") if filled else None,
        "n_events_distinct": len(events),
        "first_event": events[0] if events else None,
        "last_event": events[-1] if events else None,
        "events_distinct": events,
    }, closes


def _parse_content(content: str) -> dict:
    """从 `FHcontent`（如 "10派20元转15股"）拆出现金与送转。

    返回 {"cash": 每10股派息(元), "shares": 每10股送转股数}；缺失记 0。
    """
    import re

    cash = re.search(r"派(?:发现金)?([0-9.]+)元", content or "")
    send = re.search(r"送([0-9.]+)股", content or "")
    turn = re.search(r"转(?:增)?([0-9.]+)股", content or "")
    return {
        "cash": float(cash.group(1)) if cash else 0.0,
        "shares": (float(send.group(1)) if send else 0.0)
                  + (float(turn.group(1)) if turn else 0.0),
    }


def analyse_events(events: list[dict], closes: dict[str, float],
                   qfq_closes: dict[str, float]) -> list[dict]:
    """P4：用 qfq/none 两条真实序列**反推**每个事件的真实复权系数。

    原理：腾讯 qfq(t) = none(t) * K/factor(t)，故 ratio(t)=qfq(t)/none(t) 的
    跳变恰是除权日的调整：`k_true = ratio(cqr前一交易日) / ratio(cqr)`。

    用途：检验 ADR-001 的「只算现金分红」公式是否够用 ——
    若某事件含**送转股**（如 000333 的 "10派20元转15股"），只扣现金会严重低估调整。
    """
    dates = sorted(closes)
    out = []
    for e in events:
        cqr = e["cqr"]
        prev = [d for d in dates if d < cqr]
        if not prev or cqr not in closes:
            out.append({"cqr": cqr, "content": e.get("FHcontent"),
                        "skipped": "无除权前收盘（超出K线覆盖）"})
            continue
        d0 = prev[-1]
        pre = closes[d0]
        r0 = qfq_closes.get(d0)
        r1 = qfq_closes.get(cqr)
        k_true = (r0 / r1) if (r0 and r1) else None
        c = _parse_content(e.get("FHcontent", ""))
        cash_ps = c["cash"] / 10.0
        share_ratio = c["shares"] / 10.0
        k_cash = 1 - cash_ps / pre
        k_full = (pre - cash_ps) / (pre * (1 + share_ratio))
        out.append({
            "cqr": cqr, "prev_trade_date": d0, "content": e.get("FHcontent"),
            "fh_sh": e.get("fh_sh"), "pre_close": pre,
            "parsed_cash_per_share": round(cash_ps, 6),
            "parsed_share_ratio": share_ratio,
            "k_true_from_qfq": None if k_true is None else round(k_true, 6),
            "k_cash_only": round(k_cash, 6),
            "k_cash_plus_split": round(k_full, 6),
            "err_cash_only": None if k_true is None else round(abs(k_cash - k_true), 6),
            "err_cash_plus_split": None if k_true is None
                                   else round(abs(k_full - k_true), 6),
            "fh_sh_matches_content_cash": abs(c["cash"] - float(e.get("fh_sh") or 0)) < 1e-9,
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只探测 000333")
    ap.add_argument("--out", default=None, help="证据 JSON 落盘路径")
    args = ap.parse_args(argv)

    paths.ensure_dirs()
    cache = RawCache(paths.RAW_CACHE_DIR)
    client = HttpClient(RetryPolicy(attempts=2, base_delay=0.5, min_interval=0.35),
                        cache=cache)

    codes = ["sz000333"] if args.quick else ["sz000333", "sh600690"]
    report: dict = {"probed_at": date.today().isoformat(),
                    "note": "腾讯 fqkline 除权除息事件行探针（ADR-004 证据）"}

    report["P1_single_page_adj_compare"] = probe_single_page(client, cache, "sz000333")
    report["P3_qfq_count_cap"] = probe_count_cap(client, cache, "sz000333")
    report["P2_pagination"] = {}
    report["P4_event_factor_truth"] = {}
    for code in codes:
        none_sum, none_closes = probe_pagination(client, cache, code, "",
                                                 tencent.MAX_COUNT)
        qfq_sum, qfq_closes = probe_pagination(client, cache, code, "qfq", QFQ_PAGE)
        report["P2_pagination"][code] = {"none": none_sum, "qfq": qfq_sum}
        report["P4_event_factor_truth"][code] = analyse_events(
            none_sum["events_distinct"], none_closes, qfq_closes)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"✅ 证据落盘: {p}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
