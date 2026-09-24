"""P73 T2：用**真实抓到的**沪深300 ∪ 中证500 名单（800 行）驱动 `build_universe`。

fixture `tests/fixtures/csi800_rows.json` 是 2026-09-25 只读抓取东财
`RPT_INDEX_TS_COMPONENT TYPE=1,3` 的原样名单（`code`/`name`/`sector` 逐字未改），外加
一个顶层 `pages`（真源两页的分页归属：`TYPE=1`→300 行、`TYPE=3`→500 行）。分页必须一并
留下：它决定每只的 `index_membership`，而该列**进 canonical 文本**（`members_sha256`），
所以扁平行列表复现不出真 sha。

离线：假 fetcher 是纯函数（fixture 内存读取），**不起 HttpClient、不联网**；`out_dir` 是
`tmp_path`，**不碰真库**。

Falsifiability（每条都给「怎么让它红」）：

- `test_real_components_reproduce_the_real_sha`：`members_sha256` 是**真实抓取产物**
  （2026-09-25 联网 build 落下的 `csi300-500.csv`）的 sha。改板别表任一项（例如把 `302`
  写成 `main`）、改去重/排序/列口径，这个 sha 立刻变。
- `test_real_prefix_set_is_exactly_the_13_seen`：板别表多认或漏认一个前缀（P73 的真实
  缺口正是 `302` 未收录）⇒ 红。
- `test_derive_board_on_one_real_code_per_prefix`：把某个前缀的 board 写错（`302`→`main`）
  ⇒ 红。这条是**用真名单**打的，不是自造的 6 位数（枚举类常量要被真实数据打一次）。
- `test_unknown_prefix_still_fails_closed`：G4 —— 把 fail-closed 改成静默归 `main` ⇒ 红。
- `test_fixture_rows_have_no_empty_code_or_name`：钉住 P73 §7 第 3 项「code/name 800/800
  非空」这个实测结论（fixture 被换成半截名单 ⇒ 红）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stocklab.config import universes as U

FIXTURE = Path(__file__).parent / "fixtures" / "csi800_rows.json"

#: 真实名单撞出来的前缀集合（13 个，非推测：见 `test_real_prefix_set_...`）。
REAL_PREFIXES = (
    "000", "001", "002", "003",              # 深主板
    "300", "301", "302",                     # 创业板（302 是本档补的那一项）
    "600", "601", "603", "605",              # 沪主板
    "688", "689",                            # 科创板
)

#: 每个前缀一个**真实**代码（取自 fixture），及其派生的 board。
REAL_CODE_PER_PREFIX = (
    ("000001", "main"), ("001203", "main"), ("002001", "main"), ("003021", "main"),
    ("300001", "gem"), ("301165", "gem"), ("302132", "gem"),
    ("600000", "main"), ("601000", "main"), ("603000", "main"), ("605117", "main"),
    ("688002", "star"), ("689009", "star"),
)

#: 真抓取产物（2026-09-25 01:0x 联网 build）的 canonical sha。
REAL_MEMBERS_SHA256 = (
    "eff7b478ea9a812fff152e4e8d87a2ff345963fdf30532492a124b14096ec302")


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _real_fetcher() -> U.Fetcher:
    """fixture → 假 fetcher：页归属与行数与真源一致（`TYPE=1`→300、`TYPE=3`→500）。"""
    doc = _fixture()
    rows = {r["code"]: r for r in doc["rows"]}

    def fetch(index_type: int):
        return [rows[code] for code in doc["pages"][str(index_type)]]

    return fetch


def test_fixture_rows_have_no_empty_code_or_name():
    """P73 实测：真源 800 行 `code`/`name` 全非空 ⇒ 列名候选键（T3）是有效的。"""
    rows = _fixture()["rows"]
    assert len(rows) == 800
    assert [r["code"] for r in rows if not r.get("code")] == []
    assert [r["code"] for r in rows if not r.get("name")] == []
    assert len({r["code"] for r in rows}) == 800           # 无重复


def test_fake_fetcher_pages_match_the_real_source():
    fetch = _real_fetcher()
    assert len(list(fetch(1))) == 300                      # 沪深300
    assert len(list(fetch(3))) == 500                      # 中证500


def test_real_components_reproduce_the_real_sha(tmp_path):
    """fixture 驱动的 build ⇒ 800 行 ＋ 真抓取产物的 sha（板别表缺 `302` 时这里跑不起来）。"""
    csv_p, meta_p = U.build_universe("csi300+csi500", out_dir=tmp_path,
                                     fetcher=_real_fetcher(),
                                     built_at="2026-09-25T00:58:00+00:00")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    assert meta["n_members"] == 800
    assert meta["members_sha256"] == REAL_MEMBERS_SHA256
    assert meta["pit"] is False
    u = U.load_universe("csi300-500", root=tmp_path)
    assert len(u.members) == 800
    assert u.members_sha256 == REAL_MEMBERS_SHA256
    assert len(u.codes) == 800
    assert all(i.org_type is None for i in u.members)       # 成员表不含机构类型
    assert csv_p.name == "csi300-500.csv"


def test_real_prefix_set_is_exactly_the_13_seen(tmp_path):
    """真名单的前缀集合 ⇒ 与板别表**逐个**对得上（漏录一个前缀即在此暴露）。"""
    U.build_universe("csi300+csi500", out_dir=tmp_path, fetcher=_real_fetcher())
    u = U.load_universe("csi300-500", root=tmp_path)
    assert {c[:3] for c in u.codes} == set(REAL_PREFIXES)


@pytest.mark.parametrize("code, want", REAL_CODE_PER_PREFIX)
def test_derive_board_on_one_real_code_per_prefix(code, want):
    assert U.derive_board(code) == want


def test_302132_is_the_real_gem_stock_that_cracked_the_table():
    """`302` 段的依据（G2）：`302132` ＝ 中航成飞，创业板。

    真源名单里 `302` 段**只有这一只** —— 它是 P73 这次收口的唯一触发者。
    """
    names = {r["code"]: r["name"] for r in _fixture()["rows"]}
    assert names["302132"] == "中航成飞"
    assert U.derive_board("302132") == "gem"


@pytest.mark.parametrize("code", ["999132", "740000", "302132x"])
def test_unknown_prefix_still_fails_closed(code):
    """G4：未知前缀 / 非 6 位数字照样抛错，**不静默归 `main`**（反向自检）。"""
    with pytest.raises(U.UniverseError):
        U.derive_board(code)


def test_real_303_segment_is_not_silently_main(tmp_path):
    """把真名单里的一只 `302` 代码伪装成未收录段 ⇒ 仍抛错（不是「302 收进来了就都放过」）。"""
    def fetcher(index_type):
        rows = list(_real_fetcher()(index_type))
        if index_type == 1:
            rows.append({"code": "303999", "name": "不存在的段", "sector": None})
        return rows

    with pytest.raises(U.UniverseError, match="板别前缀未收录"):
        U.build_universe("csi300+csi500", out_dir=tmp_path, fetcher=fetcher)
