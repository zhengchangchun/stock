"""P71 T1/T5：宇宙载体（`stocklab/config/universes.py`）。

Falsifiability（每条都给「怎么让它红」）：

- `test_seed21_file_is_a_redundant_copy_of_the_constant`：改 `SEED_UNIVERSE` 一行
  而不 regen CSV ⇒ 成员/`sha` 对不上即红（这正是「主干常量不许悄悄改」的守门人）。
- `test_load_fails_closed_on_*`：把每条判据的守卫删掉 ⇒ 对应的 case 不再抛错即红。
- `test_csi_build_dedupes_two_pages`：去掉 `_rows_from_csi` 里的去重 ⇒ 800 变 801。
- `test_sha_is_bound_to_row_order`：把 `canonical_text` 改成排序后再写 ⇒ 换序后 sha 不变即红。
- `test_org_type_empty_loads_as_none`：把 `_parse_rows` 的
  `org_type=org if org else None` 改回 `Instrument(...)` 靠默认值 ⇒ `None` 断言红
  （§9 裁决 3：**不许**借 `"通用"` 兜底）。
"""

from __future__ import annotations

import json

import pytest

from stocklab.candidate.seeds import SEED_UNIVERSE
from stocklab.config import universes as U
from stocklab.config.paths import UNIVERSE_DIR


def _write(tmp_path, rows, *, n_members=None, sha=None, pit=False, header=None,
           meta_extra=None):
    """手写一份宇宙文件（用来逐条打坏判据）。"""
    text = U.canonical_text(rows)
    if header is not None:                        # 故意换表头
        text = header + text.split("\n", 1)[1]
    (tmp_path / "u.csv").write_text(text, encoding="utf-8")
    meta = {"source": "t", "built_at": "2026-09-24T00:00:00+00:00", "pit": pit,
            "n_members": len(rows) if n_members is None else n_members,
            "members_sha256": U.sha256_of(text) if sha is None else sha}
    meta.update(meta_extra or {})
    (tmp_path / "u.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return tmp_path


def _row(code="000333", **kw):
    row = {"code": code, "name": "美的集团", "market": "sz", "board": "main",
           "asset_type": "stock", "org_type": "通用", "sector": None,
           "index_membership": ""}
    row.update(kw)
    return row


# ---------- 主干常量 ↔ 冗余副本 ----------

def test_seed21_file_is_a_redundant_copy_of_the_constant():
    """`config/universes/seed21.csv` 必须**逐位**是 `SEED_UNIVERSE` 的副本。

    改主干常量而不 regen ⇒ 这里红（D2：21 只的取值/语义不许动）。
    """
    u = U.load_universe("seed21", root=UNIVERSE_DIR)
    assert (UNIVERSE_DIR / "seed21.csv").is_file()
    assert u.members == SEED_UNIVERSE
    assert u.universe_id == "seed21"
    assert u.members_sha256 == U.seed21_sha256()
    assert len(u.members) == len(SEED_UNIVERSE) == len(u.codes)


def test_seed21_meta_is_non_pit_and_has_all_required_keys():
    meta = U.load_universe("seed21", root=UNIVERSE_DIR).meta
    assert set(U.META_REQUIRED) <= set(meta)
    assert meta["pit"] is False and meta["n_members"] == 21


def test_rebuilding_seed21_offline_reproduces_the_committed_file(tmp_path):
    """离线可建：`build --source seed21` 的产物与仓库里那份**逐位相同**。"""
    csv_p, meta_p = U.build_universe("seed21", out_dir=tmp_path,
                                     built_at="2026-09-24T00:00:00+00:00")
    assert (csv_p.read_text(encoding="utf-8")
            == (UNIVERSE_DIR / "seed21.csv").read_text(encoding="utf-8"))
    assert json.loads(meta_p.read_text())["members_sha256"] == U.seed21_sha256()


# ---------- fail-closed ----------

def test_load_missing_file_raises_and_does_not_fall_back(tmp_path):
    with pytest.raises(U.UniverseError, match="拒绝回退"):
        U.load_universe("nope", root=tmp_path)


def test_load_missing_meta_raises(tmp_path):
    _write(tmp_path, [_row()])
    (tmp_path / "u.meta.json").unlink()
    with pytest.raises(U.UniverseError, match="meta 不存在"):
        U.load_universe("u", root=tmp_path)


def test_load_rejects_wrong_header(tmp_path):
    _write(tmp_path, [_row()], header="code,name,market,board,asset_type,org_type,"
                                      "sector,exchange\n")
    with pytest.raises(U.UniverseError, match="表头"):
        U.load_universe("u", root=tmp_path)


@pytest.mark.parametrize("kwargs, match", [
    ({"n_members": 2}, "n_members"),
    ({"sha": "0" * 64}, "members_sha256"),
    ({"pit": True}, "pit"),
])
def test_load_rejects_meta_mismatch(tmp_path, kwargs, match):
    _write(tmp_path, [_row()], **kwargs)
    with pytest.raises(U.UniverseError, match=match):
        U.load_universe("u", root=tmp_path)


def test_load_rejects_duplicate_code(tmp_path):
    _write(tmp_path, [_row(), _row()])
    with pytest.raises(U.UniverseError, match="重复"):
        U.load_universe("u", root=tmp_path)


def test_load_rejects_board_that_contradicts_the_code_prefix(tmp_path):
    """手工把 300 开头的写成 main ⇒ 红（涨跌停会算错，而症状是零）。"""
    _write(tmp_path, [_row(code="300750", market="sz", board="main")])
    with pytest.raises(U.UniverseError, match="board"):
        U.load_universe("u", root=tmp_path)


def test_load_rejects_hand_reformatted_bytes(tmp_path):
    """文件字节 ≠ canonical 形式（多一个空行 / 手工加行）即拒 —— sha 才真的钉在字节上。

    ⚠️ 不能用 CRLF 试：`read_text` 的 universal newlines 会先把 `\\r\\n` 归一成 `\\n`
    （实测），那样测到的是「归一化之后仍相等」，不是本判据。
    """
    _write(tmp_path, [_row("000333"), _row("000651", name="格力电器")])
    p = tmp_path / "u.csv"
    p.write_text(p.read_text(encoding="utf-8").replace("美的集团", "美的集团 "),
                 encoding="utf-8")
    with pytest.raises(U.UniverseError, match="canonical"):
        U.load_universe("u", root=tmp_path)


def test_load_rejects_empty_member_list(tmp_path):
    _write(tmp_path, [], n_members=0)
    with pytest.raises(U.UniverseError, match="没有成员行"):
        U.load_universe("u", root=tmp_path)


# ---------- org_type 留空 ⇒ None（§9 裁决 3）----------

def test_org_type_empty_loads_as_none_not_generic(tmp_path):
    """留空 ⇒ `None`。**不许**借 `Instrument.org_type` 的默认 `"通用"` 兜底。"""
    _write(tmp_path, [_row()])
    p = tmp_path / "u.csv"
    p.write_text(p.read_text(encoding="utf-8").replace("stock,通用,", "stock,,"),
                 encoding="utf-8")
    text = p.read_text(encoding="utf-8")
    meta = json.loads((tmp_path / "u.meta.json").read_text())
    meta["members_sha256"] = U.sha256_of(text)
    (tmp_path / "u.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert U.load_universe("u", root=tmp_path).members[0].org_type is None


# ---------- sha 钉住行序 ----------

def test_sha_is_bound_to_row_order():
    a = [_row("000333"), _row("000651", name="格力电器")]
    b = list(reversed(a))
    assert U.sha256_of(U.canonical_text(a)) != U.sha256_of(U.canonical_text(b))


# ---------- board / market 派生 ----------

@pytest.mark.parametrize("code, want", [
    ("300750", "gem"), ("301269", "gem"), ("688981", "star"), ("689009", "star"),
    ("830799", "bse"), ("430047", "bse"), ("600519", "main"), ("000333", "main"),
    ("002032", "main"),
])
def test_derive_board(code, want):
    assert U.derive_board(code) == want


@pytest.mark.parametrize("code", ["900001", "12345", "abcdef", "740000"])
def test_derive_board_unknown_prefix_raises_instead_of_main(code):
    with pytest.raises(U.UniverseError):
        U.derive_board(code)


@pytest.mark.parametrize("code, want", [
    ("600519", "sh"), ("300750", "sz"), ("000333", "sz"), ("830799", "bj"),
    ("510300", "sh"),
])
def test_derive_market(code, want):
    assert U.derive_market(code) == want


# ---------- build：csi300+csi500（假 fetcher，两页去重成 800）----------

def _fake_csi(index_type):
    if index_type == 1:
        return [{"code": f"60{i:04d}", "name": f"沪{i}", "sector": "银行Ⅱ"}
                for i in range(300)]
    return [{"code": f"00{i:04d}", "name": f"深{i}", "sector": None}
            for i in range(500)]


def test_csi_build_dedupes_two_pages_into_800(tmp_path):
    csv_p, meta_p = U.build_universe("csi300+csi500", out_dir=tmp_path,
                                     fetcher=_fake_csi)
    u = U.load_universe("csi300-500", root=tmp_path)
    assert csv_p.name == "csi300-500.csv" and meta_p.is_file()
    assert len(u.members) == 800                       # 300 + 500，无交集
    assert all(i.org_type is None for i in u.members)   # 成员表不含机构类型
    assert u.sectors["600000"] == "银行Ⅱ" and u.sectors["000000"] is None
    assert u.index_membership["600000"] == "hs300"
    assert u.index_membership["000000"] == "csi500"


def test_csi_build_overlap_is_marked_not_silently_dropped(tmp_path):
    def fetcher(index_type):
        rows = _fake_csi(index_type)
        return rows + ([{"code": "600000", "name": "重复", "sector": None}]
                       if index_type == 3 else [])

    U.build_universe("csi300+csi500", out_dir=tmp_path, fetcher=fetcher)
    u = U.load_universe("csi300-500", root=tmp_path)
    assert len(u.members) == 800
    assert u.index_membership["600000"] == "hs300+csi500"


def test_csi_build_refuses_an_empty_page(tmp_path):
    """源站返回空页 ⇒ 抛错，**不许**静默写出一个偏小的宇宙。"""
    with pytest.raises(U.UniverseError, match="0 行"):
        U.build_universe("csi300+csi500", out_dir=tmp_path,
                         fetcher=lambda t: [])


def test_build_unknown_source_raises(tmp_path):
    with pytest.raises(U.UniverseError, match="未知 --source"):
        U.build_universe("sz50", out_dir=tmp_path)


# ---------- resolve_universe：唯一入口 ----------

def test_resolve_seed21_does_not_read_the_file(tmp_path):
    """`None` / `"seed21"` 走主干常量（不读文件）—— 扩池不许动默认路径。"""
    uid, members, sha = U.resolve_universe(None, root=tmp_path)
    assert (uid, members, sha) == ("seed21", SEED_UNIVERSE, U.seed21_sha256())
    assert U.resolve_universe("seed21", root=tmp_path)[1] is SEED_UNIVERSE


def test_resolve_other_id_is_fail_closed(tmp_path):
    with pytest.raises(U.UniverseError):
        U.resolve_universe("csi300-500", root=tmp_path)
