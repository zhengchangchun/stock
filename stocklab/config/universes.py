"""宇宙（universe）配置载体：**repo 文件＝真源、DB 表＝投影**（P70 设计稿 D3＝C / ADR-026）。

## 为什么是文件而不是表

真源必须能被 `git log` 追溯 —— 「asof=2016-06-30 的扫描宇宙是哪几只、什么时候写进去的」
这个问题，只有提交历史能回答，而 `added_at` 可以被 rewrite。DB 表
（`universe_memberships`）是**投影**，供 SQL join 用；两者的一致性由
`universe doctor` 只读对账（D3 的代价就是这份对账，不做就没有防线）。

## 载入一律 fail-closed

`load_universe` 把「文件缺失 / 列不合规 / 行数与 `meta.n_members` 不符 /
`members_sha256` 与 `meta` 不符 / 代码重复 / `board` 与代码前缀不符」全部抛成
`UniverseError`，**绝不回退到 `SEED_UNIVERSE`**（D2：扩宇宙只能走显式 id，
隐式回退会让「我以为是宇宙 A」与「实际跑的是 21 只」不可区分）。

## CSV 列（固定顺序，表头必写）

```
code,name,market,board,asset_type,org_type,sector,index_membership
```

对齐 `stocklab/config/universe.py::Instrument` —— 注意它是 `market`
（`"sz"/"sh"/"bj"`）**不是** `exchange`；`asset_type` 取该模块的
`ASSET_STOCK`/`ASSET_ETF` 常量值。`sector` / `index_membership` 是**宇宙级注记**
（`Instrument` 没有这两个字段），按 code 存在 `Universe.sectors` /
`Universe.index_membership` 里。

### `org_type` 留空 ⇒ `None`，**不许**借 dataclass 默认值兜底

`Instrument.org_type` 的默认值是 `"通用"`，那是「不破坏既有调用点」的兼容默认。
本模块对**留空的 `org_type` 显式写 `None`**，因为这一列决定东财 F10 报表名
（`G`/`B`/`I`）—— 把「不知道」当成「通用」会**静默拿不到财报**，
而症状是「这只标的没有财报」，看起来像数据源的问题（§9 接口裁决 3）。
`financials` 取数路径对 `None` 显式跳过 F10 公告日，退回法定披露截止日。

## `board` 是**派生量**，不查表、不猜

按代码前缀确定性推导（见 `derive_board`），载入时还会拿 CSV 里写的 `board`
与派生值**对账**：不一致即抛错。这样「手工把某个 300 开头的标的写成 main」
不会静默生效（那会算错涨跌停）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping, Sequence

from stocklab.config import paths
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK, Instrument

#: CSV 列（固定顺序，表头必写）。**改这里就等于改文件格式** ⇒ 老文件全部载入失败。
CSV_COLUMNS: tuple[str, ...] = (
    "code", "name", "market", "board", "asset_type", "org_type",
    "sector", "index_membership")

#: 老 21 只的宇宙 id（D2：`SEED_UNIVERSE` 语义与取值一字不动，这个 id 是它的**冗余副本**）。
SEED21_UNIVERSE_ID = "seed21"

#: `build` 的 `--source` 取值 → 落盘的宇宙 id。
SOURCE_IDS: Mapping[str, str] = {
    "seed21": SEED21_UNIVERSE_ID,
    "csi300+csi500": "csi300-500",
}

#: `csi300+csi500` 的两个指数页（东财 `RPT_INDEX_TS_COMPONENT` 的 `TYPE` 编码）：
#: 实测 `TYPE=1`→沪深300 300 行、`TYPE=3`→中证500 500 行（P70 设计稿 §2.1）。
CSI_INDEX_PAGES: tuple[tuple[int, str], ...] = ((1, "hs300"), (3, "csi500"))

#: meta 里**必备**的字段（设计稿 §3.2 问 1）。多出来的键不算违规。
META_REQUIRED: tuple[str, ...] = (
    "source", "built_at", "pit", "n_members", "members_sha256")

#: 合法的 `asset_type`（白名单语义，见 `config/universe.py`）。
_ASSET_TYPES: frozenset[str] = frozenset({ASSET_STOCK, ASSET_ETF})

#: 代码前缀 → `market`。**未命中即抛错**，不静默归某个市场。
_MARKET_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("6", "sh"), ("5", "sh"),          # 5：沪市 ETF / 基金
    ("0", "sz"), ("3", "sz"), ("1", "sz"),   # 1：深市 ETF / 基金
    ("4", "bj"), ("8", "bj"), ("920", "bj"),
)

#: 代码前缀 → `board`（决定涨跌停幅度）。按前缀匹配，**顺序即优先级**。
#: ⚠️ 比 §9 裁决 2 多认一个 `301`：深市创业板注册制后 `301xxx` 是创业板
#: （±20% 涨跌停），中证500 里就有这一类；把它当 `main` 会算错涨跌停。
_BOARD_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("300", "gem"), ("301", "gem"),
    ("688", "star"), ("689", "star"),
    ("920", "bse"), ("4", "bse"), ("8", "bse"),
    ("600", "main"), ("601", "main"), ("603", "main"), ("605", "main"),
    ("000", "main"), ("001", "main"), ("002", "main"), ("003", "main"),
)


class UniverseError(ValueError):
    """宇宙文件缺失 / 不合规 / 与 meta 不符。调用方一律停下，**不回退**。"""


@dataclass(frozen=True)
class Universe:
    """一个宇宙的**显式对象**：id ＋ 成员元组 ＋ sha ＋ 产地。

    `members_sha256` 是 **canonical CSV 文本**（表头 ＋ 按行序的全部行）的 sha256
    —— 与**行序绑定**：同样的成员换一个顺序就是一个不同的宇宙指纹。
    """

    universe_id: str
    members: tuple[Instrument, ...]
    meta: Mapping
    members_sha256: str
    path: Path
    #: `code → sector | None`（宇宙级注记，`Instrument` 不含此字段）。
    sectors: Mapping[str, str | None] = field(default_factory=dict)
    #: `code → index_membership`（`"hs300"` / `"csi500"` / 空）。**不许猜**：
    #: `seed21` 的 21 只不全是中证800 成员，所以这一列对它们**留空**。
    index_membership: Mapping[str, str] = field(default_factory=dict)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.members)


# ---------------------------------------------------------------------------
# 派生规则（确定性，不查表）
# ---------------------------------------------------------------------------

def derive_market(code: str) -> str:
    """6 位代码 → `market`（`"sz"/"sh"/"bj"`）。非 6 位数字 / 未知前缀 ⇒ 抛错。"""
    _require_six_digits(code)
    for prefix, market in _MARKET_BY_PREFIX:
        if code.startswith(prefix):
            return market
    raise UniverseError(f"{code!r} 的市场前缀未收录 —— 不猜，请显式扩充派生规则")


def derive_board(code: str) -> str:
    """**个股**的 6 位代码 → `board`（决定涨跌停幅度）。未知前缀 ⇒ 抛错，**不静默归 main**。

    ETF **不走这里**：场内 ETF 一律 `main`（±10%），见 `_parse_rows` —— 这沿用
    `config/universe.py::DEFAULT_UNIVERSE` 的既有口径（`board` 的语义是涨跌停幅度，
    刻意不把 `'etf'` 塞进 `board`，那要改 schema 的 CHECK）。
    """
    _require_six_digits(code)
    for prefix, board in _BOARD_BY_PREFIX:
        if code.startswith(prefix):
            return board
    raise UniverseError(
        f"{code!r} 的板别前缀未收录 —— 拒绝静默归 'main'"
        "（猜错的 board 会算错涨跌停，而症状是零）")


def _require_six_digits(code: str) -> None:
    if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
        raise UniverseError(f"代码必须是 6 位数字，收到 {code!r}")


# ---------------------------------------------------------------------------
# canonical 文本与 sha
# ---------------------------------------------------------------------------

def _cell(value: object) -> str:
    return "" if value is None else str(value)


def canonical_text(rows: Sequence[Mapping]) -> str:
    """行序 → canonical CSV 文本（表头必写、`\\n` 结尾、`None` 写空串）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in CSV_COLUMNS])
    return buf.getvalue()


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rows_from_members(members: Sequence[Instrument], *,
                      sectors: Mapping[str, str | None] | None = None,
                      index_membership: Mapping[str, str] | None = None
                      ) -> list[dict]:
    """`Instrument` 元组 → canonical 行（补上两个宇宙级注记列）。"""
    sectors = sectors or {}
    index_membership = index_membership or {}
    return [{
        "code": i.code, "name": i.name, "market": i.market, "board": i.board,
        "asset_type": i.asset_type, "org_type": i.org_type,
        "sector": sectors.get(i.code), "index_membership": index_membership.get(i.code, ""),
    } for i in members]


# ---------------------------------------------------------------------------
# 老 21 只：`seed21` 由 `SEED_UNIVERSE` **离线派生**
# ---------------------------------------------------------------------------

def _seed_universe():
    from stocklab.candidate.seeds import SEED_UNIVERSE
    return SEED_UNIVERSE


def seed21_rows() -> list[dict]:
    """`seed21` 的 canonical 行 —— **离线**从主干常量派生，无需任何抓取。

    ⚠️ `sector` / `index_membership` 对 21 只**一律留空**：真库 `instruments.sector`
    0/21 非空，且 21 只不全是中证800 成员 —— 填任何一个值都是猜。
    """
    return rows_from_members(_seed_universe())


@lru_cache(maxsize=1)
def seed21_sha256() -> str:
    """`seed21` 的 canonical sha（与 `config/universes/seed21.csv` 逐位一致）。"""
    return sha256_of(canonical_text(seed21_rows()))


# ---------------------------------------------------------------------------
# 抓取 → 有 id 与 sha 的宇宙
# ---------------------------------------------------------------------------

#: `csi300+csi500` 的默认 fetcher：`(index_type) -> [{"code","name","sector"}, ...]`。
#: 站内测试一律注入假 fetcher；真实抓取（联网）由 nanobot 跑。
Fetcher = Callable[[int], Sequence[Mapping]]


def _default_fetcher() -> Fetcher:
    """生产用 fetcher（**联网**）：东财 `RPT_INDEX_TS_COMPONENT`，`TYPE=1/3`。"""
    from stocklab.data.fetch import fetch_index_components
    from stocklab.data.http import HttpClient

    client = HttpClient()

    def fetch(index_type: int) -> Sequence[Mapping]:
        rows, _refs = fetch_index_components(client, index_type=index_type)
        return rows

    return fetch


def build_universe(source: str, *, out_dir: Path | str,
                   fetcher: Fetcher | None = None,
                   built_at: str | None = None) -> tuple[Path, Path]:
    """抓/派生一个宇宙 ⇒ 写 `<id>.csv` ＋ `<id>.meta.json`，返回两个路径。

    `source` 取值见 `SOURCE_IDS`。`seed21` **离线可建**（本站直接生成、可逐位对账）；
    `csi300+csi500` 需要 fetcher（生产＝东财两页，测试＝假 fetcher 注入）。

    `org_type` 一律留空 ⇒ `None`：成员表**不含**机构类型，猜错会静默拿不到财报
    （§9 裁决 3）。要补只能靠人工点名，不许批量填「通用」。
    """
    if source not in SOURCE_IDS:
        raise UniverseError(
            f"未知 --source {source!r}；已知 {sorted(SOURCE_IDS)}")
    universe_id = SOURCE_IDS[source]
    built_at = built_at or datetime.now(timezone.utc).isoformat()

    if source == "seed21":
        rows: list[dict] = seed21_rows()
        probe = None
    else:
        rows, probe = _rows_from_csi(fetcher or _default_fetcher())

    members, sectors, membership = _parse_rows(rows)
    text = canonical_text(rows)
    meta = {
        "source": _source_label(source, rows),
        "built_at": built_at,
        "pit": False,
        "n_members": len(members),
        "members_sha256": sha256_of(text),
        "probe_sha256": probe,
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"{universe_id}.csv"
    meta_path = out / f"{universe_id}.meta.json"
    csv_path.write_text(text, encoding="utf-8")
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return csv_path, meta_path


def _source_label(source: str, rows: Sequence[Mapping]) -> str:
    if source == "seed21":
        return "seed21：由 stocklab/candidate/seeds.py::SEED_UNIVERSE 离线派生"
    return ("eastmoney:RPT_INDEX_TS_COMPONENT TYPE=1,3（沪深300 ∪ 中证500，现成分，"
            f"非 PIT；{len(rows)} 行去重前）")


def _rows_from_csi(fetcher: Fetcher) -> tuple[list[dict], str]:
    """两页指数成分 → 去重后的 canonical 行（＋一个抓取指纹）。

    去重按 code **首次出现生效**（`hs300` 优先）；同一 code 在两页都出现时
    `index_membership` 记为 `"hs300+csi500"`（**实测两集合无交集**，出现即异常，
    记下来让调用方看得见）。
    """
    merged: dict[str, dict] = {}
    probe = hashlib.sha256()
    for index_type, label in CSI_INDEX_PAGES:
        page = list(fetcher(index_type))
        if not page:
            raise UniverseError(
                f"指数页 TYPE={index_type}（{label}）返回 0 行 —— 拒绝静默写出一个"
                "空宇宙（源站换接口必须表现为失败）")
        probe.update(f"{index_type}:{len(page)};".encode("utf-8"))
        for raw in page:
            code = str(raw.get("code") or "").strip()
            _require_six_digits(code)
            probe.update(code.encode("utf-8"))
            if code in merged:
                merged[code]["index_membership"] = "hs300+csi500"
                continue
            merged[code] = {
                "code": code,
                "name": str(raw.get("name") or "").strip(),
                "market": derive_market(code),
                "board": derive_board(code),
                "asset_type": ASSET_STOCK,
                "org_type": None,               # 成员表不含机构类型 ⇒ 不许猜
                "sector": (str(raw.get("sector")).strip() if raw.get("sector") else None),
                "index_membership": label,
            }
    return [merged[c] for c in sorted(merged)], probe.hexdigest()


def _parse_rows(rows: Sequence[Mapping]
                ) -> tuple[tuple[Instrument, ...], dict, dict]:
    """canonical 行 → `(Instrument 元组, sectors, index_membership)`。逐行 fail-closed。"""
    members: list[Instrument] = []
    sectors: dict[str, str | None] = {}
    membership: dict[str, str] = {}
    seen: set[str] = set()
    for n, row in enumerate(rows, start=1):
        code = _cell(row.get("code"))
        _require_six_digits(code)
        if code in seen:
            raise UniverseError(f"第 {n} 行代码重复：{code}")
        seen.add(code)
        asset_type = _cell(row.get("asset_type")) or ASSET_STOCK
        if asset_type not in _ASSET_TYPES:
            raise UniverseError(
                f"{code} 的 asset_type={asset_type!r} 不在白名单 "
                f"{sorted(_ASSET_TYPES)} —— 口径未知的标的不许进宇宙")
        market = _cell(row.get("market")) or derive_market(code)
        if market not in ("sz", "sh", "bj"):
            raise UniverseError(f"{code} 的 market={market!r} 不合法")
        if asset_type == ASSET_ETF:
            # 场内 ETF 一律 main（±10%）—— 与 DEFAULT_UNIVERSE 的既有口径一致。
            board = _cell(row.get("board")) or "main"
            want_board = "main"
        else:
            board = _cell(row.get("board")) or derive_board(code)
            want_board = derive_board(code)
        if board != want_board:
            hint = ("ETF 的 board 固定 'main'" if asset_type == ASSET_ETF
                    else "board 决定涨跌停幅度，不许手写覆盖")
            raise UniverseError(
                f"{code} 的 board={board!r} 与派生的 {want_board!r} 不符 —— {hint}")
        org = _cell(row.get("org_type"))
        members.append(Instrument(code=code, name=_cell(row.get("name")),
                                  market=market, board=board,
                                  asset_type=asset_type,
                                  org_type=org if org else None))
        sectors[code] = (_cell(row.get("sector")) or None)
        membership[code] = _cell(row.get("index_membership"))
    return tuple(members), sectors, membership


# ---------------------------------------------------------------------------
# 载入（fail-closed）
# ---------------------------------------------------------------------------

def load_universe(universe_id: str, *,
                  root: Path | str = paths.UNIVERSE_DIR) -> Universe:
    """载入一个宇宙。**任何一处不符即抛 `UniverseError`，决不回退到 `SEED_UNIVERSE`。**

    逐条判据：文件缺失 / 表头不是 `CSV_COLUMNS` / 文件字节 ≠ canonical 形式
    （行序、空串、换行都算）/ `meta` 缺失或缺必备键 / `pit` 不是 `false` /
    `n_members` 与行数不符 / `members_sha256` 与 canonical 文本不符 /
    代码重复 / `board` 与代码前缀派生值不符。
    """
    root = Path(root)
    csv_path = root / f"{universe_id}.csv"
    meta_path = root / f"{universe_id}.meta.json"
    if not csv_path.is_file():
        raise UniverseError(
            f"宇宙文件不存在：{csv_path} —— 拒绝回退到 SEED_UNIVERSE（D2："
            "扩宇宙只能走显式 --universe，隐式回退会让『跑的是哪个宇宙』不可查）")
    if not meta_path.is_file():
        raise UniverseError(f"宇宙 meta 不存在：{meta_path}（sha 与 n_members 无从对账）")

    raw = csv_path.read_text(encoding="utf-8")
    rows = _read_rows(raw, csv_path)
    members, sectors, membership = _parse_rows(rows)

    text = canonical_text(rows)
    if raw != text:
        raise UniverseError(
            f"{csv_path} 的字节与 canonical 形式不一致（行序/空串/换行被改过？）"
            " —— 请用 `universe build` 重新生成，不要手改文件")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UniverseError(f"宇宙 meta 读不出/不是 json：{meta_path}（{exc}）") from exc
    if not isinstance(meta, dict):
        raise UniverseError(f"宇宙 meta 不是对象：{meta_path}")
    missing = [k for k in META_REQUIRED if k not in meta]
    if missing:
        raise UniverseError(f"宇宙 meta 缺字段：{missing}（{meta_path}）")
    if meta["pit"] is not False:
        raise UniverseError(
            f"宇宙 {universe_id} 的 meta.pit={meta['pit']!r} —— 本档只做"
            "「现成分」（non_pit=true，D1），没有 PIT 成分的来源")
    if meta["n_members"] != len(members):
        raise UniverseError(
            f"meta.n_members={meta['n_members']} 与实际行数 {len(members)} 不符"
            f"（{csv_path}）")
    sha = sha256_of(text)
    if meta["members_sha256"] != sha:
        raise UniverseError(
            f"meta.members_sha256={meta['members_sha256']} 与文件 canonical sha={sha} 不符"
            f"（{csv_path}）—— 文件被改过而 meta 没重算")

    return Universe(universe_id=universe_id, members=members, meta=meta,
                    members_sha256=sha, path=csv_path,
                    sectors=sectors, index_membership=membership)


def _read_rows(raw: str, csv_path: Path) -> list[dict]:
    reader = csv.reader(io.StringIO(raw))
    try:
        header = next(reader)
    except StopIteration:
        raise UniverseError(f"{csv_path} 是空文件") from None
    if tuple(header) != CSV_COLUMNS:
        raise UniverseError(
            f"{csv_path} 的表头是 {tuple(header)}，应为 {CSV_COLUMNS}"
            "（固定顺序；改列名即改文件格式）")
    rows: list[dict] = []
    for n, cells in enumerate(reader, start=2):
        if len(cells) != len(CSV_COLUMNS):
            raise UniverseError(
                f"{csv_path} 第 {n} 行有 {len(cells)} 列，应为 {len(CSV_COLUMNS)} 列")
        rows.append(dict(zip(CSV_COLUMNS, cells)))
    if not rows:
        raise UniverseError(f"{csv_path} 只有表头、没有成员行")
    return rows


def resolve_universe(universe_id: str | None, *,
                     root: Path | str = paths.UNIVERSE_DIR
                     ) -> tuple[str, tuple[Instrument, ...], str]:
    """消费方的**唯一入口**：宇宙 id（`None` ⇒ `seed21`）→ `(id, 成员元组, sha)`。

    `None` / `"seed21"` ⇒ 主干常量 `SEED_UNIVERSE`（它才是 21 只的**真源**，
    `config/universes/seed21.csv` 是它的冗余副本，两者由 `seed21_sha256()` 对账）
    ⇒ **不读文件、不改默认路径的行为**；
    其余 id ⇒ `load_universe(id)`（fail-closed，**绝不回退**）。
    """
    if universe_id in (None, SEED21_UNIVERSE_ID):
        return SEED21_UNIVERSE_ID, _seed_universe(), seed21_sha256()
    u = load_universe(universe_id, root=root)
    return u.universe_id, u.members, u.members_sha256


__all__ = [
    "CSV_COLUMNS", "CSI_INDEX_PAGES", "META_REQUIRED", "SEED21_UNIVERSE_ID",
    "SOURCE_IDS", "Universe", "UniverseError", "build_universe", "canonical_text",
    "derive_board", "derive_market", "load_universe", "resolve_universe",
    "rows_from_members", "seed21_rows", "seed21_sha256", "sha256_of",
]
