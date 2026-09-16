"""上交所「休市安排」适配器（P30）。

只管**取什么 URL + 怎么把 HTML 读成事实**，不落库、不联网（联网在 `fetch.py`）。

## 实测（2026-09-17）

- 列表页 `https://www.sse.com.cn/disclosure/dealinstruc/closed/list/`
  → HTTP 200 / 32873 字节，**服务端渲染**：`<a href="…/c/…shtml">关于2026年端午节休市安排的公告</a>`
  直接可正则取出（**不是**背景里说的「纯 JS 外壳」—— 那是 `…/closed/` 那个父页）；
- 文章页 `https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml`
  → HTTP 200 / 26542 字节，正文是普通 HTML，含发布日 `2025-12-22`；
- 深交所那个 URL 404 → **本模块不覆盖深交所**（见 ADR-013 §遗留）。

## 两类文档（正文格式同构）

| doc_kind | 例子 | 覆盖范围 |
|---|---|---|
| `annual` | 《关于上海证券交易所2026年部分节假日休市安排的通知》 | **该年全部日期**（未列出者即开市） |
| `holiday` | 《关于2026年端午节休市安排的公告》 | 只有那一个节 |

## fail-closed

标题说「休市」而正文一条都解析不出 → 抛 `SseFormatError`。**不返回空列表** ——
「源站换了排版」与「今年真的不放假」必须能区分（ERROR_DIARY 2026-09-14「回退」那条）。
"""

from __future__ import annotations

import re

#: 休市安排栏目列表页（服务端渲染）。
LIST_URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/list/"
#: 文章页 URL 前缀（列表页给出的是站内绝对路径 `/<…>.shtml`）。
BASE_URL = "https://www.sse.com.cn"

#: 列表页里文章链接的形状（`/disclosure/announcement/general/c/c_YYYYMMDD_NNNNNNN.shtml`）。
_ARTICLE_HREF = re.compile(r'href="(/[^"]*c_\d+_\d+\.shtml)"[^>]*>([^<]*)<')
#: 公告标题里的年份：`关于上海证券交易所2026年部分节假日休市安排的通知` / `关于2026年端午节休市安排的公告`
_TITLE_YEAR = re.compile(r"(\d{4})年")
#: 正文里的发布日：`2025-12-22`
_PUBLISHED = re.compile(r"(\d{4}-\d{2}-\d{2})")


class SseFormatError(RuntimeError):
    """源站排版与适配器约定不符 —— 显式失败，**不许**静默降级成「没有休市」。"""


def _strip_tags(html: str) -> str:
    """去标签取纯文本（script/style 先整块摘掉，再替换标签为空白）。"""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    return text


def _unescape(text: str) -> str:
    from html import unescape

    return unescape(text)


def parse_article_list(html: str) -> list[dict]:
    """列表页 → `[{url, title, doc_kind, covered_year}]`（只收「休市安排」类公告）。

    `doc_kind` 由标题判定：含「部分节假日休市安排」的年度通知 = `annual`；
    其余含「休市安排」的单节公告 = `holiday`。标题里**没有**四位年份的公告直接跳过
    （无法确定它覆盖哪一年，收了也没法用）。
    """
    out: list[dict] = []
    for href, raw_title in _ARTICLE_HREF.findall(html):
        title = _unescape(" ".join(raw_title.split()))
        if "休市安排" not in title:
            continue
        m = _TITLE_YEAR.search(title)
        if not m:
            continue
        out.append({
            "url": BASE_URL + href,
            "title": title,
            "doc_kind": "annual" if "部分节假日休市安排" in title else "holiday",
            "covered_year": int(m.group(1)),
        })
    return out


def published_at_from_article(html: str) -> str:
    """从文章页取公告发布日（`YYYY-MM-DD`）。

    取**第一个**形如日期的串：实测文章页顺序为「标题 → 发布日 → 文号 → 正文」，
    文号形如 `上证公告〔2025〕45号` 不含 `YYYY-MM-DD`，正文日期是 `M月D日` 亦不匹配。
    取不到 → 抛错（发布日是「这行是什么时候公告的」的证据，不能瞎填）。
    """
    m = _PUBLISHED.search(_strip_tags(html))
    if not m:
        raise SseFormatError("文章页里找不到发布日（YYYY-MM-DD）—— 拒绝用抓取时间顶替")
    return m.group(1)


def article_text(html: str) -> str:
    """文章页 → 纯文本（解析器与断言都用它，避免两处去标签逻辑漂移）。"""
    return _unescape(_strip_tags(html))
