"""回归红线的「可执行不变量」（P25，错误日记 #34 的正面修复）。

#34 的根因：P8 把红线写成了 `predict run --asof 2026-09-14` 报告 sha256 =
`a61d026b…f4eb` 这样一条**快照常量**，`grep -rn a61d026b` 全仓只命中 docs ——
**没有任何测试或脚本在守它**。该值 70 分钟后因上游加了口径回显字段而失效，
此后十几轮提交无人发现。

本模块提供两档互补判据（缺一不可）：

- **字节级**：整份载荷的 sha256 逐位相等 → 抓「字段集被改动」（#34 的翻车类型）。
  `MODEL_VERSION` 没变**推不出**报告没变：加一个 `evidence.inputs` 回显字段就能改 sha。
- **数字叶子级**：递归取所有标量的键路径做比对 → 抓「数值漂移」，
  并**容忍纯文字新增/改写**（否则写一句注释就要重新基线，红线会被人嫌烦而绕过）。
  失败时列出差异键路径。

`MODEL_VERSION` 未变、预测未变，但报告字节变了 —— 这种情况**必须**被人看到并**有意**决定
是否重新基线。失败文案里固化了 #34 的三步标准动作。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "BASELINE_RELPATH",
    "build_synthetic_db",
    "compare_leaves",
    "format_failure",
    "is_red",
    "leaves",
    "leaves_sha256",
    "load_baseline",
    "merge_regen_targets",
    "repo_root",
    "run_synthetic_predict_report",
    "synthetic_asof",
]

BASELINE_RELPATH = Path("docs/baselines/redlines.json")

# 「谁在什么条件下必须重新基线」——写进基线文件，也写进失败文案。
REBASELINE_WHEN = (
    "**只有当改动是「有意的且已归因」时**才重新基线：① 先在**同一代码基线**上做归因实验"
    "（把改动回退 / stash 后重跑同一条命令，与改动前的产物 `cmp`）；② 确认差异**就是本次改动**"
    "造成的、且是想要的；③ 用 `--regen` 重新生成基线，并把**新值 + 日期 + 原因**追加进"
    "`docs/plans/2026-09-15-p8-实验流水线.md` 与 `docs/experiments/README.md` 台账"
    "（append-only，**不删旧值**）。\n"
    "**禁止**：因为检查红了就直接 `--regen`（那是把红线改成橡皮图章，正是 #34 的教训）；"
    "在没做归因实验前改动基线数字。\n"
    "**注意**：真实库那两段还可能是「库/数据变了」而不是「代码变了」—— 先归因再下结论。"
)


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def repo_root() -> Path:
    """仓库根目录（本文件位于 `<root>/stocklab/quality/redline.py`）。"""
    return Path(__file__).resolve().parents[2]


def baseline_path() -> Path:
    return repo_root() / BASELINE_RELPATH


def load_baseline(path: Path | str | None = None) -> dict:
    p = Path(path) if path is not None else baseline_path()
    return json.loads(Path(p).read_text(encoding="utf-8"))


def merge_regen_targets(existing: dict, fresh: dict) -> dict:
    """`--regen --only X` 的**部分重基**：`fresh` 里有的目标覆盖旧值，其余**原样保留**。

    为什么需要它（2026-09-21）：基线里两条真实库目标的失效**原因不同** ——
    `predict_real` 红是「复权链从空到 80461 行」（已归因、可以重基），
    `backfill_real` 的输入是 `verifications` 表里**尚未重放**的回放行（已知将要失效）。
    一起重基 = 把一份已知将要失效的值冻进基线，下次还得再重基一次并再写一条台账。

    基线条目**自带 `taken_at`**，所以「不同目标在不同日子重基」在文件里是自洽的。
    """
    out = dict(existing)
    out.update(fresh)
    return out


def canonical_json(obj: Any) -> str:
    """与 `cmd_predict_run` 写文件时**完全相同**的序列化（逐字节可复现的前提）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 叶子提取与比对
# --------------------------------------------------------------------------

def _canonical_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):        # 必须在 int 之前判：`True == 1` 但语义不同
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)                 # int / float：repr 保证往返一致


def leaves(obj: Any, prefix: str = "") -> dict[str, str]:
    """递归展开成 `{键路径: 规范化标量}`。

    - dict → `prefix.key`；list → `prefix[i]`；其余 → `prefix` 自身
    - **空容器也入账**（`[]` / `{}`）：字段被删掉时才能发现
      —— P8 报告里的 `features_used` 就是空表，它存在与不存在是两回事。
    """
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        if not obj:
            out[prefix] = "{}"
            return out
        for key in obj:
            out.update(leaves(obj[key], f"{prefix}.{key}"))
    elif isinstance(obj, list):
        if not obj:
            out[prefix] = "[]"
            return out
        for i, item in enumerate(obj):
            out.update(leaves(item, f"{prefix}[{i}]"))
    else:
        out[prefix] = _canonical_scalar(obj)
    return out


def leaves_sha256(leaf_map: dict[str, str]) -> str:
    """对叶子映射取一个**顺序无关**的聚合 sha（快速完整性读数）。"""
    lines = [f"{k}={leaf_map[k]}" for k in sorted(leaf_map)]
    return sha256_text("\n".join(lines))


def _is_numeric(value: str) -> bool:
    """规范化后的标量是否属于「数值面」（数字 / 布尔 / null）。

    字符串是「文字面」—— 单改文字**不算漂移**（红线要容忍纯文字编辑），
    但新增/删除一个**数字**字段算漂移。
    """
    return value in ("true", "false", "null") or not value.startswith('"')


def compare_leaves(expected: dict[str, str], actual: dict[str, str]) -> dict:
    """按**数值面**判红、按**文字面**只记录。

    返回 `{changed, added_numeric, missing_numeric, text:{added,changed,missing}}`；
    用 `is_red()` 取结论。`changed` 是 `[(路径, 期望, 实际)]`。
    """
    changed: list[tuple[str, str, str]] = []
    added_numeric: list[str] = []
    missing_numeric: list[str] = []
    text: dict[str, list] = {"added": [], "changed": [], "missing": []}

    for path in sorted(set(expected) | set(actual)):
        exp = expected.get(path)
        act = actual.get(path)
        if exp is None:                                   # 新增键路径
            (added_numeric if _is_numeric(act) else text["added"]).append(path)
        elif act is None:                                 # 键路径消失
            (missing_numeric if _is_numeric(exp) else text["missing"]).append(path)
        elif exp == act:
            continue
        elif _is_numeric(exp) and _is_numeric(act):       # 数值漂移
            changed.append((path, exp, act))
        elif _is_numeric(exp) or _is_numeric(act):        # 数字↔文字互变 = 数值面漂移
            changed.append((path, exp, act))
        else:                                             # 纯文字改写 → 容忍
            text["changed"].append((path, exp, act))

    return {"changed": changed, "added_numeric": added_numeric,
            "missing_numeric": missing_numeric, "text": text}


def is_red(diff: dict) -> bool:
    """数值面有差异即红。文字面差异**不**判红（只在上报里列出）。"""
    return bool(diff["changed"] or diff["added_numeric"] or diff["missing_numeric"])


# --------------------------------------------------------------------------
# 失败文案：把 #34 的三步标准动作固化进代码
# --------------------------------------------------------------------------

def format_failure(
    target: str,
    *,
    expected_sha: str,
    actual_sha: str,
    leaf_diff: dict,
    regen_cmd: str,
    baseline_file: str,
) -> str:
    """可操作的失败信息。**必须**包含三步：归因 → 有意则重新基线 → 写进台账（不删旧值）。"""
    lines = [
        "",
        "=" * 78,
        f"❌ 回归红线不成立：{target}",
        "=" * 78,
        f"  基线文件     : {baseline_file}",
        f"  期望 sha256  : {expected_sha}",
        f"  实际 sha256  : {actual_sha}",
        "",
    ]
    if is_red(leaf_diff):
        lines.append("  【数值面漂移】差异键路径：")
        for path, exp, act in leaf_diff["changed"]:
            lines.append(f"    ~ {path}: {exp} → {act}")
        for path in leaf_diff["added_numeric"]:
            lines.append(f"    + {path}  (新增数字/布尔字段)")
        for path in leaf_diff["missing_numeric"]:
            lines.append(f"    - {path}  (数字/布尔字段消失)")
    else:
        lines.append("  【数值面无差异】数值叶子全部一致 —— 漂移发生在**文字面或字段集**上：")
    t = leaf_diff["text"]
    for path in t["added"][:20]:
        lines.append(f"    + {path}  (新增文字键，本身不算漂移)")
    for path, exp, act in t["changed"][:20]:
        lines.append(f"    ~ {path}: {exp} → {act}  (文字改写，本身不算漂移)")
    for path in t["missing"][:20]:
        lines.append(f"    - {path}  (文字键消失)")
    if len(t["changed"]) + len(t["added"]) + len(t["missing"]) > 60:
        lines.append("    …（文字面差异过多，已截断）")

    lines += [
        "",
        "  先别急着改基线 —— 按 #34 的三步走：",
        "  ① 归因实验：把本次改动回退（git stash / git checkout <改动前>）后重跑**同一条命令**，",
        "     与改动前的产物 `cmp`。逐字节相同 ⇒ 是你在别处改了东西；不同 ⇒ 差异来自本次改动。",
        "     真实库的目标还要多想一步：**也可能是库/数据变了，不是代码变了。**",
        "  ② 确认是**有意**改动后，重新基线：",
        f"       {regen_cmd}",
        "  ③ 把新值 + 日期 + 原因**追加**进 plan 与 `docs/experiments/README.md` 台账",
        "     （append-only，**不删旧值**；旧值标注失效时间与原因即可）。",
        "",
        f"  重新基线的条件（摘自基线文件）：{REBASELINE_WHEN}",
        "=" * 78,
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# hermetic 合成夹具：不依赖真实库、不联网
# --------------------------------------------------------------------------

_SYNTH_CODES = (("000333", "美的集团", "sz"), ("600690", "海尔智家", "sh"))
_SYNTH_N = 200
_SYNTH_EXTRA_CALENDAR_DAYS = 3      # 让 target_date 能由 trading_calendar 解析出来
SYNTH_NOW = "2026-09-15T10:00:00+08:00"


def _synth_dates() -> list[str]:
    out = []
    for i in range(_SYNTH_N):
        y, rem = divmod(2024 * 372 + 1 * 31 + 1 + i, 372)
        m, d = divmod(rem, 31)
        out.append(f"{y:04d}-{m:02d}-{d:02d}")
    return out


def _plus_one_day(date: str) -> str:
    y, m, d = (int(x) for x in date.split("-"))
    return f"{y:04d}-{m:02d}-{d + 1:02d}"


def synthetic_asof() -> str:
    return _synth_dates()[-1]


def build_synthetic_db(db_path: Path | str) -> Path:
    """建一个**确定性**的离线库（纯算术价格，无 RNG、无网络）。

    只用整数/浮点四则运算生成价格 —— IEEE 双精度四则在各平台逐位一致，
    所以这个夹具产出的报告可以当**字节级基线**用。
    """
    from stocklab.config.universe import Instrument
    from stocklab.data.models import Bar
    from stocklab.store import repo
    from stocklab.store.db import connect
    from stocklab.store.migrate import init_db

    db_path = Path(db_path)
    dates = _synth_dates()
    init_db(db_path)
    conn = connect(db_path)
    try:
        repo.upsert_instruments(
            conn,
            [Instrument(code, name, mkt, "main") for code, name, mkt in _SYNTH_CODES],
            now=SYNTH_NOW,
        )
        bars = []
        for idx, (code, _name, _mkt) in enumerate(_SYNTH_CODES):
            for i, d in enumerate(dates):
                base = 10.0 + 2.0 * idx + 0.01 * i + 0.07 * (i % 13)
                bars.append(Bar(
                    code=code, date=d, open=base, high=base + 0.1,
                    low=base - 0.1, close=base + 0.03 * ((i * 7) % 5),
                    volume=1000 + 3 * i, amount=1000.0 + i, turnover=1.0,
                    source="synthetic", adj_mode="none",
                ))
        repo.insert_bars(conn, bars, now=SYNTH_NOW)

        cal = list(dates)
        nxt = dates[-1]
        for _ in range(_SYNTH_EXTRA_CALENDAR_DAYS):
            nxt = _plus_one_day(nxt)
            cal.append(nxt)
        conn.executemany(
            "INSERT OR IGNORE INTO trading_calendar (date, is_open, source, created_at)"
            " VALUES (?,1,'synthetic',?)", [(d, SYNTH_NOW) for d in cal])
        conn.commit()
    finally:
        conn.close()
    return db_path


def run_synthetic_predict_report(tmpdir: Path | str) -> dict:
    """建合成库 → 跑**与红线同一条命令形状**的 `predict run` → 返回报告 dict。

    返回的是**解析后的 dict**；比较时用 `canonical_json(report)` 还原成
    与 `cmd_predict_run` 写盘时**逐字节相同**的文本（见 `canonical_json`）。

    **为什么走子进程**：报告的 `strategies[]` 段来自进程级全局 `strategy_registry`，
    而测试套件里有用例把自己的策略注册进这个全局表且**不做清理**
    （`tests/test_strategies_evaluate.py` 的 `tests_once_per_fold`）。
    在 pytest 里 in-process 跑，报告内容就**依赖执行顺序** —— P25 首次全量跑就是
    这样红的（`.`strategies[1].strategy_id: "trend_ma" → "tests_once_per_fold"`）。
    子进程拿到的是一份**全新解释器**，全局态天然干净，夹具因此在
    「谁先跑」这个维度上也是 hermetic 的。
    """
    import subprocess

    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    db = build_synthetic_db(tmpdir / "synthetic.sqlite")
    report = tmpdir / "predict.json"
    proc = subprocess.run(
        [sys.executable, "-m", "stocklab.cli.main", "predict", "run",
         "--asof", synthetic_asof(), "--db", str(db), "--report", str(report)],
        cwd=str(repo_root()), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"合成夹具 `predict run` 退出码 {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return json.loads(report.read_text(encoding="utf-8"))


def synthetic_report_bytes(report: dict) -> bytes:
    return canonical_json(report).encode("utf-8")
