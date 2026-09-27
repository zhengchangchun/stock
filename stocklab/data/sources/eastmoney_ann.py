"""东财**公告列表**适配器（P88；`np-anotice-stock`）。

只管「取什么 URL + 怎么把 JSON 读成事实 + 翻到 cutoff 就停」，不落库
（落库由 `stocklab.data.ingest` 负责），联网经注入的 `client`（与 `fetch.py` 同款）。

## 实测（2026-09-27 只读探针，任务书 §0.2）

`GET https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=50&page_index=1
&ann_type=A&client_source=web&stock_list=000333` → 200 JSON；
`data.total_hits`（000333 = **2784**）、`data.list[]` 按 **`notice_date` 倒序**。

行字段：`art_code`（**稳定唯一号**，如 `AN202609151829423536`）/ `notice_date`（公告日，
PIT 锚）/ `display_time`（发布时刻，形如 `2026-09-15 19:35:36:855`）/ `title` / `title_ch` /
`columns[].column_name`（如「调研活动」）/ `codes[].stock_code` / `source_type` / `listing_state`。

## ⚠️ 这个接口**不支持时间窗**（本模块最重要的一条）

`begin_time` / `end_time`（无论 `YYYY-MM-DD` 还是带时分）**一加就 `total_hits=0`**
（实测）。所以增量只能是「从第 1 页翻页，翻到 `notice_date < cutoff` 即停」
（`fetch_announcements`）—— 那正是本模块存在的理由。**不要**把时间窗加回去。

## `ann_type` 落在哪一列（口径说明）

请求参数里叫 `ann_type`（`A` = A 股公告），行里另有 `codes[].ann_type`（如 `A,SZA`/`INV`）。
落库列 `ann_type` 取**请求口径**（本模块的 `ANN_TYPE` 常量）：§0.2 的字段清单枚举了行内字段
却**没有** `ann_type`，而 URL 参数正是 `ann_type=A` ⇒ 那一列是「这一行来自哪条公告流」的留痕。
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping

from stocklab.data.errors import FetchError

ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"

#: 单页行数（实测 50；源站按 `page_size` 截断）。
PAGE_SIZE = 50

#: 公告类型：`A` = A 股（URL 参数 `ann_type`）。落库时作为该行的 `ann_type`。
ANN_TYPE = "A"

SOURCE = "eastmoney-ann"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stocklab/0.1)"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def announcement_url(code: str, *, page: int, page_size: int = PAGE_SIZE) -> str:
    """公告列表 URL。`code` 是 6 位代码（`stock_list` 口径，不带市场前缀）。

    **刻意不含 `begin_time`/`end_time`** —— 一加就 `total_hits=0`（见模块 docstring）。
    """
    return (f"{ANN_URL}?sr=-1&page_size={page_size}&page_index={page}"
            f"&ann_type={ANN_TYPE}&client_source=web&stock_list={code}")


def _first_column_name(row: Mapping) -> str | None:
    """取 `columns[]` 里第一个非空的 `column_name`（公告栏目，如「调研活动」）。"""
    for col in row.get("columns") or []:
        if isinstance(col, Mapping) and col.get("column_name"):
            return str(col["column_name"])
    return None


def parse_announcements(payload: dict, code: str) -> list[dict]:
    """响应 → 规范化行字典（**保持源站顺序**，即 `notice_date` 倒序）。

    - `notice_date` 带 `" 00:00:00"` → 裁成 `YYYY-MM-DD`（PIT 锚）
    - `display_time` **原样保留**（含毫秒分隔的那个冒号）——它只作留痕，
      任务书 D2 明写「不得进任何时间轴筛选」
    - `title` 空则退回 `title_ch`；两者都空 → 该行丢掉（标题是这一行的可读身份）
    - 缺 `art_code` / `notice_date` 的行**丢掉**（没有稳定唯一号或没有公告日的行没法幂等落库）
    """
    rows = ((payload or {}).get("data") or {}).get("list") or []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        art = str(row.get("art_code") or "").strip()
        date = str(row.get("notice_date") or "").strip()[:10]
        title = str(row.get("title") or row.get("title_ch") or "").strip()
        if not art or not date or not title:
            continue
        out.append({
            "code": code,
            "art_code": art,
            "notice_date": date,
            "display_time": (str(row.get("display_time")).strip()
                             if row.get("display_time") else None),
            "title": title,
            "column_name": _first_column_name(row),
            "ann_type": ANN_TYPE,
            "source": SOURCE,
        })
    return out


def fetch_announcements(client, *, code: str, cutoff: str, page_limit: int = 5,
                        page_size: int = PAGE_SIZE
                        ) -> tuple[list[dict], list[dict], bool]:
    """翻页抓 `notice_date >= cutoff` 的公告，返回 `(rows, page_refs, truncated)`。

    翻页规则（D5）：

    - 从 `page_index=1` 起逐页取；**某页 `min(notice_date) < cutoff` 即停**
      （列表倒序 ⇒ 后面的只会更老）；
    - 取到空页 / 已取满 `total_hits` ⇒ 正常收尾，`truncated=False`；
    - `page_limit` 是**硬上限**：跑满仍未见 cutoff ⇒ `truncated=True`，
      **不报错**（下轮继续；报错会让「台账太长」变成每天都红）。

    每行带 `resp_sha256`（**它来自哪一份原始响应**，可复现可溯源）；`page_refs`
    记每页的 `page` / `resp_sha256` / `cache_key`。
    """
    out: list[dict] = []
    refs: list[dict] = []
    truncated = False
    page = 1
    while True:
        cache_key = f"announcement:{code}:{page}:{page_size}"
        text = client.get_text(announcement_url(code, page=page, page_size=page_size),
                               headers=HEADERS, source="eastmoney",
                               cache_key=cache_key)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise FetchError(f"{code} 公告响应不是合法 JSON: {exc}") from exc
        sha = _sha256(text)
        refs.append({"page": page, "resp_sha256": sha, "cache_key": cache_key})
        rows = parse_announcements(payload, code)
        if not rows:
            break
        for r in rows:
            out.append({**r, "resp_sha256": sha})
        if min(r["notice_date"] for r in rows) < cutoff:
            break                                    # 已翻过 cutoff，再翻只会更老
        total = int(((payload or {}).get("data") or {}).get("total_hits") or 0)
        if total and page * page_size >= total:
            break                                    # 这个标的的公告取完了
        if page >= page_limit:
            truncated = True
            break
        page += 1
    return [r for r in out if r["notice_date"] >= cutoff], refs, truncated
