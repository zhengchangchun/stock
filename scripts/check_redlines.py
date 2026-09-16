#!/usr/bin/env python
"""回归红线的**可执行入口**（P25 / 错误日记 #34）。

    ⚠️ 本脚本的两个「真实库」目标**不是 hermetic 的** —— 它们依赖本机真实库
       `data/stocklab.db`（15MB，未提交、被 .gitignore 忽略）。库不存在时会**显式打印
       「⏭ 跳过」并退出 0，绝不假装跑过**。hermetic 的那一半在
       `tests/test_redline_baseline.py`（合成夹具，提交时就能红）。

用法:
    .venv/bin/python scripts/check_redlines.py                 # 跑全部（默认）
    .venv/bin/python scripts/check_redlines.py --only predict  # 只跑 predict 红线
    .venv/bin/python scripts/check_redlines.py --regen         # **有意**重新基线
    .venv/bin/python scripts/check_redlines.py --cli-backfill  # backfill 走 CLI 原命令（3m35s）

三条红线目标（见 `docs/baselines/redlines.json`）:
    predict_real_2026-09-14              真实库；`predict run --asof 2026-09-14`
    backfill_real_2013-12-23_2026-09-14  真实库；`verify backfill --from 2013-12-23 --to 2026-09-14`
    predict_synthetic                    hermetic 合成夹具（与 pytest 同源）

判据两档互补（缺一不可）:
    ① **字节级** sha256 逐位相等 —— 抓「字段集被改动」（#34 的翻车类型：
       `MODEL_VERSION` 没变、预测没变，但加一个 `evidence.inputs` 回显字段就改了 sha）；
    ② **数字叶子级** 递归键路径比对 —— 抓「数值漂移」，**容忍纯文字新增/改写**。

失败时按 #34 的三步走：先归因实验 → 确认有意 → 才 `--regen`，并把新值 **append-only**
写进 plan 与台账（不删旧值）。

**只读保证**：`predict run` 会写 `predictions` 表（幂等 `identical`），
所以本脚本一律**在库的临时副本上**跑它 —— 真实库零写入。
backfill 用只读重算路径（`load_verification_rows` + `summarize` + `render_markdown`），
已实测与 CLI 原命令**逐字节相同**（见基线文件里的 `evidence`）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.quality.redline import (  # noqa: E402
    BASELINE_RELPATH,
    REBASELINE_WHEN,
    canonical_json,
    compare_leaves,
    format_failure,
    is_red,
    leaves,
    leaves_sha256,
    load_baseline,
    repo_root,
    run_synthetic_predict_report,
    sha256_text,
    synthetic_asof,
)

DEFAULT_DB = "data/stocklab.db"
PREDICT_ASOF = "2026-09-14"
BACKFILL_FROM = "2013-12-23"
BACKFILL_TO = "2026-09-14"
MODEL_VERSION = "pit-rw-v1.0.1"
REGEN_CMD = ".venv/bin/python scripts/check_redlines.py --regen"
TODAY = "2026-09-16"


# --------------------------------------------------------------------------
# 采集：真实库（库不存在 → 返回 None，调用方显式「跳过」）
# --------------------------------------------------------------------------

def _predict_real(db: Path, workdir: Path) -> dict | None:
    """在**库的副本**上跑红线命令，返回报告 dict。"""
    from stocklab.cli.main import main

    db_copy = workdir / "copy.sqlite"
    shutil.copy(db, db_copy)                 # 真实库零写入
    report = workdir / "predict.json"
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = main(["predict", "run", "--asof", PREDICT_ASOF, "--db", str(db_copy),
                   "--report", str(report)])
    if rc != 0:
        raise RuntimeError(f"`predict run --asof {PREDICT_ASOF}` 退出码 {rc}\n{buf.getvalue()}")
    return json.loads(report.read_text(encoding="utf-8"))


def _backfill_readonly(db: Path) -> tuple[str, dict]:
    """只读重算 backfill 报告（**不写任何表**）。返回 (md 文本, summary dict)。"""
    from stocklab.store.db import connect
    from stocklab.verify.replay import load_verification_rows
    from stocklab.verify.report import render_markdown, summarize

    conn = connect(db)
    try:
        rows = load_verification_rows(conn, BACKFILL_FROM, BACKFILL_TO)
    finally:
        conn.close()
    summary = summarize(rows, from_date=BACKFILL_FROM, to_date=BACKFILL_TO)
    return render_markdown(summary), summary


def _backfill_cli(db: Path, workdir: Path) -> tuple[str, dict]:
    """CLI 原命令路径（幂等；3m35s）。用于复验「只读重算与 CLI 逐字节相同」。"""
    from stocklab.cli.main import main

    db_copy = workdir / "copy.sqlite"
    shutil.copy(db, db_copy)
    md = workdir / "acc.md"
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = main(["verify", "backfill", "--from", BACKFILL_FROM, "--to", BACKFILL_TO,
                   "--db", str(db_copy), "--report", str(md)])
    if rc != 0:
        raise RuntimeError(f"`verify backfill` 退出码 {rc}\n{buf.getvalue()}")
    summary = json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
    return md.read_text(encoding="utf-8"), summary


# --------------------------------------------------------------------------
# 基线条目构造
# --------------------------------------------------------------------------

def _real_entry(name: str, command: str, sha: str, payload: dict, **extra) -> dict:
    lv = leaves(payload)
    entry = {
        "kind": "real_db",
        "non_hermetic": True,
        "depends_on": DEFAULT_DB,
        "taken_at": TODAY,
        "command": command,
        "model_version": MODEL_VERSION,
        "sha256": sha,
        "n_leaves": len(lv),
        "leaves_sha256": leaves_sha256(lv),
        "leaves": lv,
        "rebaseline_when": REBASELINE_WHEN,
    }
    entry.update(extra)
    return entry


def collect(db: Path, workdir: Path, cli_backfill: bool) -> dict:
    targets: dict[str, dict] = {}

    rep = _predict_real(db, workdir)
    raw = canonical_json(rep)
    targets["predict_real_2026-09-14"] = _real_entry(
        "predict_real_2026-09-14",
        f".venv/bin/python -m stocklab.cli.main predict run --asof {PREDICT_ASOF} "
        f"--db <{DEFAULT_DB} 的临时副本> --report <tmp>",
        sha256_text(raw), rep,
        note="跑在库副本上，真实库零写入；报告本身不含时间戳，同 asof 可逐字节复现。",
    )

    md, summary = (_backfill_cli(db, workdir) if cli_backfill
                   else _backfill_readonly(db))
    targets["backfill_real_2013-12-23_2026-09-14"] = _real_entry(
        "backfill_real_2013-12-23_2026-09-14",
        f".venv/bin/python -m stocklab.cli.main verify backfill --from {BACKFILL_FROM} "
        f"--to {BACKFILL_TO} --db <{DEFAULT_DB} 的临时副本> --report <tmp>/acc.md",
        sha256_text(md), summary,
        summary_sha256=sha256_text(canonical_json(summary)),
        note=("默认走**只读重算**（load_verification_rows+summarize+render_markdown，0.7s，不写库），"
              "已实测与 CLI 原命令（3m35s，幂等）产出**逐字节相同**；"
              "用 `--cli-backfill` 可原样复验该等价性。数字叶子取自 summary JSON。"),
    )

    syn = run_synthetic_predict_report(workdir / "synthetic")
    raw_syn = canonical_json(syn)
    lv = leaves(syn)
    targets["predict_synthetic"] = {
        "kind": "hermetic",
        "non_hermetic": False,
        "depends_on": "(无 —— 合成夹具，见 stocklab/quality/redline.py::build_synthetic_db)",
        "taken_at": TODAY,
        "command": f"predict run --asof {synthetic_asof()} --db <合成库> --report <tmp>"
                   "（由 tests/test_redline_baseline.py 与 --regen 共同驱动）",
        "model_version": MODEL_VERSION,
        "sha256": sha256_text(raw_syn),
        "n_leaves": len(lv),
        "leaves_sha256": leaves_sha256(lv),
        "leaves": lv,
        "rebaseline_when": REBASELINE_WHEN,
        "note": "合成夹具用纯算术价格（无 RNG/无网络），所以这条基线是 hermetic 的。",
    }
    return targets


def write_baseline(path: Path, targets: dict) -> None:
    doc = {
        "schema": "stocklab-redline-v1",
        "_doc": [
            "回归红线的可执行基线（P25；错误日记 #34 的正面修复）。",
            "每条目含两档判据所需的全部信息：",
            "  ① 字节级  : sha256（整份载荷逐位相等）",
            "  ② 数字叶子: leaves（键路径 → 规范化标量）+ leaves_sha256 + n_leaves",
            "叶子比对**容忍纯文字新增/改写**（只记录不判红），**抓住数值漂移**（判红并列出差异键路径）。",
            "字段集被改动这类漂移由 ① 兜住 —— MODEL_VERSION 没变推不出报告没变。",
            "重新基线：`.venv/bin/python scripts/check_redlines.py --regen`，且必须先做归因实验。",
            "新增/修订一律 append-only：旧值标注失效时间与原因，**不删不覆盖**。",
        ],
        "rebaseline_when": REBASELINE_WHEN,
        "targets": targets,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


# --------------------------------------------------------------------------
# 比对
# --------------------------------------------------------------------------

def check_one(name: str, entry: dict, *, sha: str, payload: dict) -> bool:
    """跑两档判据。任一红 → 返回 False 并打印可操作失败文案。"""
    lv = leaves(payload)
    diff = compare_leaves(entry["leaves"], lv)
    ok = True
    if is_red(diff):
        ok = False
    if sha != entry["sha256"]:
        ok = False
    if ok:
        print(f"  ✅ {name}")
        print(f"       sha256 {sha[:16]}… == 基线；叶子 {len(lv)} 项一致"
              f"（leaves_sha256 {leaves_sha256(lv)[:16]}…）")
        return True
    print(format_failure(name, expected_sha=entry["sha256"], actual_sha=sha,
                         leaf_diff=diff, regen_cmd=REGEN_CMD,
                         baseline_file=str(BASELINE_RELPATH)))
    if not is_red(diff):
        print("  ⚠️  数值叶子**全部一致**，只有字节变了 —— 这是「字段集/格式被改动」的特征"
              "（#34 的翻车类型：加了口径回显字段）。\n")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回归红线检查（P25 / #34）")
    ap.add_argument("--only", choices=["predict", "backfill", "synthetic", "all"],
                    default="all")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"真实库路径（默认 {DEFAULT_DB}；**非 hermetic**）")
    ap.add_argument("--baseline", default=str(BASELINE_RELPATH))
    ap.add_argument("--regen", action="store_true",
                    help="**有意**重新基线（先做归因实验！见 #34）")
    ap.add_argument("--cli-backfill", action="store_true",
                    help="backfill 走 CLI 原命令（3m35s）而非只读重算")
    ap.add_argument("--require-db", action="store_true",
                    help="真实库缺失时**失败**而不是跳过（CI/交付用）")
    args = ap.parse_args(argv)

    root = repo_root()
    db = root / args.db
    baseline_file = root / args.baseline
    want = set()
    if args.only == "all":
        want = {"predict", "backfill", "synthetic"}
    elif args.only == "predict":
        want = {"predict"}
    elif args.only == "backfill":
        want = {"backfill"}
    else:
        want = {"synthetic"}

    print("=" * 78)
    print(f"🔍 回归红线检查（{args.only}）  baseline={args.baseline}")
    print("=" * 78)

    needs_db = bool(want & {"predict", "backfill"})
    db_ready = db.exists()

    if args.regen:
        if needs_db and not db_ready:
            print(f"  ⏭ 真实库不存在：{db}")
            print("     —— 真实库目标**非 hermetic**，无法重新基线；本次只重基合成目标。")
            if args.require_db:
                print("  ❌ --require-db：真实库缺失，失败。")
                return 1
        with tempfile.TemporaryDirectory(prefix="p25-regen-") as td:
            targets = collect(db, Path(td), args.cli_backfill) if db_ready else {}
        if not db_ready:
            syn = run_synthetic_predict_report(Path(tempfile.mkdtemp(prefix="p25-syn-")))
            lv = leaves(syn)
            targets["predict_synthetic"] = {
                "kind": "hermetic", "non_hermetic": False,
                "depends_on": "(无 —— 合成夹具)",
                "taken_at": TODAY,
                "command": f"predict run --asof {synthetic_asof()} --db <合成库>",
                "model_version": MODEL_VERSION,
                "sha256": sha256_text(canonical_json(syn)),
                "n_leaves": len(lv), "leaves_sha256": leaves_sha256(lv), "leaves": lv,
                "rebaseline_when": REBASELINE_WHEN,
            }
        write_baseline(baseline_file, targets)
        print(f"  ✍️  已写入基线：{args.baseline}（{len(targets)} 个目标）")
        for name, e in sorted(targets.items()):
            print(f"       {name:42s} sha256={e['sha256'][:16]}… leaves={e['n_leaves']}")
        print()
        print("  ⚠️  别忘了第 ③ 步：把新值 + 日期 + 原因 **append-only** 追加进")
        print("     `docs/plans/2026-09-15-p8-实验流水线.md` 与 `docs/experiments/README.md`。")
        return 0

    if not baseline_file.exists():
        print(f"  ❌ 基线文件缺失：{args.baseline}")
        print(f"     —— 先做归因实验确认现状无误，再 `{REGEN_CMD}` 生成。")
        return 1

    baseline = load_baseline(baseline_file)
    targets = baseline["targets"]
    ok = True

    if needs_db and not db_ready:
        print(f"  ⏭ 跳过真实库目标：{db} 不存在")
        print("     —— **本脚本对这两个目标不是 hermetic 的**：它们依赖本机真实库，")
        print("        真实库未提交（.gitignore `/data/`）。hermetic 的那一半在")
        print("        `tests/test_redline_baseline.py`（提交时就会跑）。")
        if args.require_db:
            print("  ❌ --require-db：真实库缺失，失败。")
            return 1

    if "synthetic" in want:
        syn = run_synthetic_predict_report(Path(tempfile.mkdtemp(prefix="p25-syn-")))
        ok &= check_one("predict_synthetic", targets["predict_synthetic"],
                        sha=sha256_text(canonical_json(syn)), payload=syn)

    if db_ready and want & {"predict", "backfill"}:
        with tempfile.TemporaryDirectory(prefix="p25-check-") as td:
            td = Path(td)
            if "predict" in want:
                rep = _predict_real(db, td)
                ok &= check_one("predict_real_2026-09-14",
                                targets["predict_real_2026-09-14"],
                                sha=sha256_text(canonical_json(rep)), payload=rep)
            if "backfill" in want:
                md, summary = (_backfill_cli(db, td) if args.cli_backfill
                               else _backfill_readonly(db))
                entry = targets["backfill_real_2013-12-23_2026-09-14"]
                ok &= check_one("backfill_real_2013-12-23_2026-09-14", entry,
                                sha=sha256_text(md), payload=summary)
                exp = entry.get("summary_sha256")
                act = sha256_text(canonical_json(summary))
                if exp and exp != act:
                    print(f"  ❌ backfill summary JSON sha 不符：期望 {exp} 实际 {act}")
                    ok = False

    print("=" * 78)
    print("✅ 回归红线全部成立" if ok else "❌ 回归红线不成立（见上方可操作提示）")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
