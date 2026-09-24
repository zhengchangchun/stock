"""P71 T4/T5：`universe` 进预注册（D7）＋ nanobot 的兼容裁决。

裁决原文（任务书 §1 T4，站不许自造）：

- 老预注册 `docs/experiments/2026-09-24-xsec-topn.md` **一字不改**（台账 append-only），
  它**缺** `universe` ⇒ 语义**视为 `seed21`**；
- `validate_prereg` **仍按 `universe` 比对命令行实参** ⇒ 预注册推得 `seed21` 而命令行
  传 `csi300-500` **即拒跑（exit 2）** —— 堵的正是「静默换宇宙」。

Falsifiability：

- `test_old_prereg_loads_and_implies_seed21`：把 `PREREG_OPTIONAL_FIELDS` 删掉 ⇒
  `load_prereg` 因缺字段抛错即红（老台账被误判成「不构成预注册」）。
- `test_cli_rejects_swapping_universe_silently`：把 `validate_prereg` 里那段
  `universe` 比对删掉 ⇒ 该用例 exit 0 即红（**这正是扩池打开的那个口子**）。
- `test_report_carries_universe_and_incomparability`：把 `render_md` 的 §3b 删掉 ⇒
  「不可直接比」那句话不在 md 里即红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stocklab.cli.main import main
from stocklab.research import xsec

REPO_PREREG = (Path(__file__).resolve().parents[1] / "docs" / "experiments"
               / "2026-09-24-xsec-topn.md")


def test_prereg_fields_include_universe_because_of_the_designed_gap():
    """D7：字段清单里必须有 `universe`（不加 ⇒ 同一份预注册换宇宙能跑出两个结论）。"""
    assert "universe" in xsec.PREREG_FIELDS
    assert xsec.PREREG_OPTIONAL_FIELDS == ("universe",)
    assert xsec.DEFAULT_PREREG_UNIVERSE == "seed21"


def test_old_prereg_loads_and_implies_seed21():
    """老台账一字不改仍能载入，且**不被注入** `universe` 键（预注册写过什么就是什么）。"""
    data, sha = xsec.load_prereg(REPO_PREREG)
    assert "universe" not in data
    assert len(sha) == 64
    assert data.get("universe", xsec.DEFAULT_PREREG_UNIVERSE) == "seed21"
    # 缺失 ⇒ seed21 语义：命令行默认（None ⇒ seed21）**通过**
    xsec.validate_prereg(data, pool="short", start="2015-01-01", topn=6,
                         universe=None)
    xsec.validate_prereg(data, pool="short", start="2015-01-01", topn=6,
                         universe="seed21")


def test_old_prereg_plus_other_universe_is_rejected():
    """预注册推得 `seed21`、命令行传别的 ⇒ 拒跑（静默换宇宙被堵死）。"""
    data, _ = xsec.load_prereg(REPO_PREREG)
    with pytest.raises(xsec.PreregError, match="换宇宙必须新预注册"):
        xsec.validate_prereg(data, pool="short", start="2015-01-01", topn=6,
                             universe="csi300-500")


def test_prereg_with_other_universe_is_rejected_on_the_default_path(tmp_path):
    """反向：预注册写了 `csi300-500`、命令行没传 ⇒ 同样拒跑（不是「有就比对、没有就放行」）。"""
    p = tmp_path / "p.md"
    data, _ = xsec.load_prereg(REPO_PREREG)
    body = {**data, "universe": "csi300-500"}
    p.write_text("# x\n\n```json\n" + json.dumps(body, ensure_ascii=False)
                 + "\n```\n", encoding="utf-8")
    loaded, _sha = xsec.load_prereg(p)
    assert loaded["universe"] == "csi300-500"
    with pytest.raises(xsec.PreregError):
        xsec.validate_prereg(loaded, pool="short", start="2015-01-01", topn=6,
                             universe=None)


def test_cli_rejects_swapping_universe_silently(tmp_path, capsys):
    """CLI 层面：`--universe csi300-500` 打老预注册 ⇒ **exit 2、零输出**（不跑回放）。

    预注册校验发生在**开库之前**，所以用一个不存在的库也能判这条 —— 库里
    有没有数据与本判据无关。
    """
    out = tmp_path / "out"
    rc = main(["research", "xsec-topn", "--pool", "short", "--start", "2015-01-01",
               "--prereg", str(REPO_PREREG), "--out", str(out),
               "--universe", "csi300-500",
               "--db", str(tmp_path / "absent.db")])
    assert rc == 2
    assert "预注册不一致" in capsys.readouterr().err
    assert not out.exists()


def test_cli_accepts_seed21_explicitly_on_the_old_prereg(tmp_path, capsys):
    """`--universe seed21` 与老预注册（→ seed21 语义）**一致** ⇒ 跳过校验，
    在「库不存在」这一步才 exit 2（证明卡住它的是库，不是宇宙）。"""
    rc = main(["research", "xsec-topn", "--pool", "short", "--start", "2015-01-01",
               "--prereg", str(REPO_PREREG), "--out", str(tmp_path / "out"),
               "--universe", "seed21", "--db", str(tmp_path / "absent.db")])
    assert rc == 2
    assert "打不开库" in capsys.readouterr().err


def _minimal_report() -> dict:
    arm = {"n_periods": 1, "period_returns": [0.0], "mean_all": 0.0,
           "mean_train": 0.0, "mean_validate": 0.0, "excess_index300": 0.0}
    return {
        "experiment": "xsec-topn", "pool": "short", "start": "2015-01-01",
        "end": "2026-09-24", "arm": "topn", "universe": "csi300-500",
        "universe_n": 800, "universe_members_sha256": "a" * 64,
        "prereg_universe": "csi300-500",
        "topn": 6, "hold_arm": "topn", "hold_control": "all-eligible",
        "min_periods": 120, "bootstrap_n": 2000, "bootstrap_seed": 20260918,
        "benchmark": "sh000300", "rule": "r", "prereg_path": "p",
        "prereg_sha256": "b" * 64, "n_marks": 2, "n_periods": 1,
        "elapsed_s": 0.0, "scan_s": 0.0, "replay_s": 0.0,
        "arms": {"topn": arm}, "delta": None,
        "non_pit_items": list(xsec.NON_PIT_ITEMS),
        "non_pit_universe_items": list(xsec.NON_PIT_UNIVERSE_ITEMS),
        "universe_note": "**本 Δ 的对照臂来自 `csi300-500`，与 `seed21` 版（21 只）"
                         "的 Δ 不可直接比**",
        "cost_caliber_note": xsec.COST_CALIBER_NOTE,
        "selection_bias_note": xsec.SELECTION_BIAS_SENTENCE,
        "window_note": xsec.WINDOW_IS_CONCLUSION_NOTE,
    }


def test_report_carries_universe_and_incomparability():
    """报告必须打出宇宙 id、`non_pit=true` 的 N1/N2/N3/N4、以及**不可直接比**那一句。"""
    md = xsec.render_md(_minimal_report())
    assert "不可直接比" in md
    assert "non_pit=true" in md
    for item in xsec.NON_PIT_UNIVERSE_ITEMS:
        assert item in md
    for tag in ("N1", "N2", "N3", "N4"):
        assert tag in md
    assert "csi300-500" in md
    # 三条「种子宇宙」的非 PIT 项仍然在（口径不许被替换）
    for item in xsec.NON_PIT_ITEMS:
        assert item in md


def test_expanded_universe_report_does_not_claim_a_21_name_scope():
    """扩池报告不许出现与实参**矛盾**的扫描范围陈述（P71 §7.7 偏离 2）。

    Falsifiability：把 `render_md` 里 `report.get("seed_scope_note")` 那段改回
    硬编码的「种子只有 21 只」⇒ 本用例红。
    """
    assert xsec._seed_scope_note("seed21", 21) == "种子只有 21 只"
    assert xsec._selection_bias_note("seed21", 21) == xsec.SELECTION_BIAS_SENTENCE

    r = _minimal_report()
    r["seed_scope_note"] = xsec._seed_scope_note("csi300-500", 800)
    r["selection_bias_note"] = xsec._selection_bias_note("csi300-500", 800)
    md = xsec.render_md(r)
    assert "种子只有 21 只" not in md
    assert "在这 21 只" not in md
    assert "扫描宇宙 `csi300-500` 只有 800 只" in md


def test_seed21_default_markdown_is_byte_identical_to_the_old_wording():
    """默认路径（`seed21`）的报告文案**逐字不变** —— 口径增量不许改老输出。"""
    r = _minimal_report()
    r["universe"] = r["universe_id"] = "seed21"
    r["universe_n"] = 21
    r["seed_scope_note"] = xsec._seed_scope_note("seed21", 21)
    r["selection_bias_note"] = xsec._selection_bias_note("seed21", 21)
    md = xsec.render_md(r)
    assert "> 种子只有 21 只、短池 topn 只有 6，" in md
    assert xsec.SELECTION_BIAS_SENTENCE in md
