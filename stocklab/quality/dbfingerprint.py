"""库指纹（P25 回归红线的**归因辅助**，2026-09-21 用户拍板「做」）。

## 为什么需要它

`scripts/check_redlines.py` 的两条「真实库」红线是拿一份**冻结的报告 sha** 当基准。
它们一红，只说明「今天的报告 ≠ 那天的报告」，**分不清是代码回归还是库变了**：

- 2026-09-16 取基线时，本机库只有 **6 只**标的、`predictions`/`verifications` **全 0 行**、
  复权链**为空**（ADR-008 之后才补的链）；
- 2026-09-21 再跑，库里 **21 只**、600690 历史回填到 1993-11-19、`n_bars_used 6062→7807`。

当时只能靠「单变量实验 + 直查库」逐条试，花了约 40 分钟才归因到「库变了」。

**本模块把这个归因成本压到一次打印**：基线里每条**非 hermetic** 目标多存一份库指纹；
红线红时脚本把「基线时刻的库」与「现在的库」**并排打出来**。

## 指纹都记什么

用户拍板的三项（`docs/plans/2026-09-21-红线库指纹.md`）+ 两项必要的补强：

| 项 | 内容 | 为什么 |
|---|---|---|
| 标的清单 | `instruments.codes`（排序后的代码） | 「6 只 → 21 只」是这次红的直接原因 |
| K 线 | 各标的 `n` / `first` / `last` + 全表 `n`/`first`/`last` | 「600690 回填、`n_bars_used 6062→7807`」 |
| 预测/验证 | `n`、按 `origin` 分组、按 `model_version` 分组、asof/target 区间 | 「账本原本全 0 行」 |
| **内容摘要** | 关键表的 `content_sha256` | **行数相同、内容不同**是真实存在的（复权口径改变不增行），只看行数会漏 |
| 采集覆盖 | `corp_actions`/`adj_factors`/`valuation_daily`/`money_flow_daily`/`financial_reports` 的 `n` | 复权链从空到 80461 行 = 口径变了 |

## 两条设计约束

1. **零写入**：连接一律走 `file:...?mode=ro`。指纹是要在**诊断**时跑的，不能污染被诊断的库
   （`bars_daily` 之外的预测类表都是 append-only，写错一次不可撤）。
2. **确定性**：不含时间戳、不含自增主键、不含 `fetched_at`/`created_at`/`added_at` 这类入库时刻列。
   同一份库内容 → 逐字节相同的指纹；否则它就会自己变成一条「随机红」。
   （`verifications` 的内容摘要因此走 `JOIN predictions` 取 `(code, asof_date)` 做身份，
   而不是自增 `pred_id` —— 自增 id 在同一批预测重放后就会变。）

## 它不是红线

指纹**不一致不判红**：`bars_daily` 每天都在涨，判红等于每条红线天天红。
指纹的职责只有一个 —— **把「库变了」这件事在红线红的时候直接摆出来**：
指纹一致 ⇒ 这次红不是数据涨了，去代码里找；指纹不同 ⇒ 先按「库/口径变了」解释。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "attach_db_fingerprint",
    "diff_fingerprint",
    "fingerprint",
    "format_fingerprint_diff",
    "format_fingerprint_json",
]

#: 内容摘要覆盖的表 → 参与哈希的列（**刻意排除**入库时刻列与自增主键）。
#: 表不存在时记 `{"present": False}`，不报错（老库/裁剪库都能跑）。
_CONTENT_SPEC: dict[str, tuple[str, ...]] = {
    "bars_daily": ("code", "date", "open", "high", "low", "close", "pre_close",
                   "volume", "amount", "turnover", "adj_mode", "is_suspended", "source"),
    "predictions": ("code", "asof_date", "target_date", "direction_up", "direction_flat",
                    "direction_down", "range_lo", "range_hi", "key_levels_json", "action",
                    "size_pct", "invalidate_if", "strategy_mix_json", "regime_label",
                    "model_version", "origin"),
    "adj_factors": ("code", "date", "factor"),
    "corp_actions": ("code", "cqr", "djr", "fh_sh", "content", "source"),
    # verifications 走 JOIN：身份是 (code, asof_date, target_date)，不是自增 pred_id
    "verifications": ("p.code", "p.asof_date", "v.target_date", "v.actual_close",
                      "v.actual_pct", "v.benchmark_pct", "v.hit_direction", "v.hit_range",
                      "v.hit_levels", "v.sim_pnl", "v.score_direction", "v.score_range",
                      "v.score_level", "v.score_action", "v.total_score", "v.invalidated"),
}

_VERIFICATIONS_FROM = ("verifications v"
                       " JOIN predictions p ON p.pred_id = v.pred_id")

#: 只记「规模 + 日期区间」的表（内容摘要对它们没意义或代价不划算）。
_RANGE_TABLES: dict[str, str] = {
    "valuation_daily": "date",
    "money_flow_daily": "date",
    "financial_reports": "report_date",
}


# --------------------------------------------------------------------------
# 连接与基础工具
# --------------------------------------------------------------------------

def _connect_ro(db: Path | str) -> sqlite3.Connection:
    """**只读**打开。`mode=ro` 由 SQLite 自己保证不写入（不靠调用方自觉）。"""
    p = Path(db)
    if not p.exists():
        raise FileNotFoundError(f"库不存在：{p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _cell(value: Any) -> str:
    """单元格的规范化文本（跨平台逐位一致；`None` 与字符串 `"None"` 必须区分）。"""
    if value is None:
        return "\x00"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _rows_digest(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> tuple[str, int]:
    """按给定 `sql`（**必须自带 ORDER BY**）逐行哈希。返回 (sha256, 行数)。"""
    h = hashlib.sha256()
    n = 0
    for row in conn.execute(sql, tuple(params)):
        h.update("\x1f".join(_cell(v) for v in row).encode("utf-8"))
        h.update(b"\n")
        n += 1
    return h.hexdigest(), n


def _one(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> Any:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else tuple(row)[0]


# --------------------------------------------------------------------------
# 指纹本体
# --------------------------------------------------------------------------

def fingerprint(db: Path | str, *, label: str | None = None) -> dict:
    """采集一份**确定性**的库指纹（只读，零写入）。

    `label` 只用于记录「这是哪个库」（默认取传入路径的 basename 之外的相对形态由调用方给）。
    指纹里不含任何机器相关路径 —— 但它会被写进 git 跟踪的基线文件，所以调用方应当传相对路径。
    """
    conn = _connect_ro(db)
    try:
        return _fingerprint_conn(conn, label=label)
    finally:
        conn.close()


def _fingerprint_conn(conn: sqlite3.Connection, *, label: str | None = None) -> dict:
    fp: dict = {}
    if label:
        fp["source_db"] = label

    # ---- 标的清单 ----
    if _table_exists(conn, "instruments"):
        codes = [r[0] for r in conn.execute("SELECT code FROM instruments ORDER BY code")]
        fp["instruments"] = {"n": len(codes), "codes": codes}
    else:
        fp["instruments"] = {"present": False}

    # ---- K 线：总规模 + 逐标的起止与行数 ----
    if _table_exists(conn, "bars_daily"):
        total = _one(conn, "SELECT COUNT(*) FROM bars_daily") or 0
        first = _one(conn, "SELECT MIN(date) FROM bars_daily")
        last = _one(conn, "SELECT MAX(date) FROM bars_daily")
        by_code = {
            r["code"]: {"n": r["n"], "first": r["first"], "last": r["last"]}
            for r in conn.execute(
                "SELECT code, COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last"
                " FROM bars_daily GROUP BY code ORDER BY code")
        }
        fp["bars_daily"] = {"n": total, "first": first, "last": last, "by_code": by_code}
    else:
        fp["bars_daily"] = {"present": False}

    # ---- 预测 / 验证（回放与实时的规模与口径） ----
    if _table_exists(conn, "predictions"):
        by_origin = {
            (r["origin"] or "(none)"): r["n"] for r in conn.execute(
                "SELECT origin, COUNT(*) AS n FROM predictions GROUP BY origin ORDER BY origin")
        }
        by_model = {
            r["mv"]: r["n"] for r in conn.execute(
                "SELECT model_version AS mv, COUNT(*) AS n FROM predictions"
                " GROUP BY model_version ORDER BY model_version")
        }
        fp["predictions"] = {
            "n": _one(conn, "SELECT COUNT(*) FROM predictions") or 0,
            "by_origin": by_origin,
            "by_model_version": by_model,
            "first_asof": _one(conn, "SELECT MIN(asof_date) FROM predictions"),
            "last_asof": _one(conn, "SELECT MAX(asof_date) FROM predictions"),
            "codes": _one(conn, "SELECT COUNT(DISTINCT code) FROM predictions") or 0,
        }
    else:
        fp["predictions"] = {"present": False}

    if _table_exists(conn, "verifications"):
        fp["verifications"] = {
            "n": _one(conn, "SELECT COUNT(*) FROM verifications") or 0,
            "first_target": _one(conn, "SELECT MIN(target_date) FROM verifications"),
            "last_target": _one(conn, "SELECT MAX(target_date) FROM verifications"),
        }
    else:
        fp["verifications"] = {"present": False}

    # ---- 复权链 / 除权事件（口径变化的直接证据） ----
    if _table_exists(conn, "corp_actions"):
        fp["corp_actions"] = {
            "n": _one(conn, "SELECT COUNT(*) FROM corp_actions") or 0,
            "codes": _one(conn, "SELECT COUNT(DISTINCT code) FROM corp_actions") or 0,
        }
    else:
        fp["corp_actions"] = {"present": False}

    if _table_exists(conn, "adj_factors"):
        fp["adj_factors"] = {
            "n": _one(conn, "SELECT COUNT(*) FROM adj_factors") or 0,
            "codes": _one(conn, "SELECT COUNT(DISTINCT code) FROM adj_factors") or 0,
        }
    else:
        fp["adj_factors"] = {"present": False}

    # ---- 其余采集表：只要规模与区间 ----
    for table, date_col in _RANGE_TABLES.items():
        if _table_exists(conn, table):
            fp[table] = {
                "n": _one(conn, f"SELECT COUNT(*) FROM {table}") or 0,
                "first": _one(conn, f"SELECT MIN({date_col}) FROM {table}"),
                "last": _one(conn, f"SELECT MAX({date_col}) FROM {table}"),
                "codes": _one(conn, f"SELECT COUNT(DISTINCT code) FROM {table}") or 0,
            }
        else:
            fp[table] = {"present": False}

    # ---- 内容摘要（行数相同、内容不同也能看出来） ----
    content: dict[str, Any] = {}
    for table, cols in _CONTENT_SPEC.items():
        if not _table_exists(conn, table):
            content[table] = {"present": False}
            continue
        select = ", ".join(cols)
        if table == "verifications":
            sql = f"SELECT {select} FROM {_VERIFICATIONS_FROM}"
            order = " ORDER BY p.code, p.asof_date, v.target_date"
        elif table == "bars_daily":
            sql = f"SELECT {select} FROM {table}"
            order = " ORDER BY code, date"
        elif table == "predictions":
            sql = f"SELECT {select} FROM {table}"
            order = " ORDER BY code, asof_date, model_version"
        else:
            sql = f"SELECT {select} FROM {table}"
            order = " ORDER BY " + (", ".join(cols[:2]))
        sha, n = _rows_digest(conn, sql + order)
        content[table] = {"rows": n, "sha256": sha}
    fp["content_sha256"] = content
    return fp


# --------------------------------------------------------------------------
# 挂到基线条目上
# --------------------------------------------------------------------------

def attach_db_fingerprint(targets: dict, fp: dict) -> dict:
    """把库指纹挂到**非 hermetic** 的基线条目上（hermetic 目标不挂：它们与库无关）。

    就地修改并返回 `targets`；顺带返回 `targets` 便于链式调用。
    """
    for entry in targets.values():
        if entry.get("non_hermetic"):
            entry["db_fingerprint"] = fp
    return targets


# --------------------------------------------------------------------------
# 比对
# --------------------------------------------------------------------------

#: 逐标的展开会刷屏；差异行数上限（超出只报「还有 N 行」）。
MAX_DIFF_ROWS = 40


def _flatten_scalars(obj: Any, prefix: str = "") -> dict[str, Any]:
    """把「标量 / 标量字典 / 标量字典的字典」展平成 `{点分键: 值}`。

    - 标量 → `prefix`
    - 值是标量的字典（`by_origin`、`by_model_version`）→ 继续下钻
    - 值是字典的字典（`by_code`）→ **不下钻**（由调用方逐标的比对，才能报「某标的」）
    """
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and not any(isinstance(x, dict) for x in v.values()):
                out.update(_flatten_scalars(v, key))
            elif isinstance(v, dict):
                continue
            else:
                out[key] = v
    else:
        out[prefix] = obj
    return out


def _add(rows: list[dict], section: str, key: str, field: str,
         expected: Any, actual: Any) -> None:
    rows.append({"section": section, "key": key, "field": field,
                 "expected": expected, "actual": actual})


def _sort_key(row: dict) -> tuple:
    """打印顺序 = **重要程度**，不是字母序。

    整表规模（「account 原本 0 行」「标的 6→21」）与内容摘要排在前，
    逐标的的 K 线逐日增长排在后 —— 后者是真信息但每天都在动，
    抢在前头会把「真正解释这次红」的几行挤出屏幕。
    """
    if row["section"] == "content_sha256":
        rank = 1
    elif row["key"] == "":
        rank = 0 if not row["field"].startswith("codes") else 2
    else:
        rank = 3
    return (rank, row["section"], row["key"], row["field"])


def diff_fingerprint(expected: dict, actual: dict) -> dict:
    """结构化差异：`{"rows": [...], "n_changes": int}`。

    行形如 `{"section": "bars_daily", "key": "600690", "field": "n",
             "expected": None, "actual": 7807}`；`key` 为空表示「整表」。
    **不含时间戳**，同一对指纹 → 同一份差异。
    """
    rows: list[dict] = []
    exp_sections = {k: v for k, v in expected.items() if k != "content_sha256"}
    act_sections = {k: v for k, v in actual.items() if k != "content_sha256"}

    for section in sorted(set(exp_sections) | set(act_sections)):
        e = exp_sections.get(section)
        a = act_sections.get(section)
        if e is None or a is None:
            _add(rows, section, "", "present", e is not None, a is not None)
            continue
        if not isinstance(e, dict) or not isinstance(a, dict):
            if e != a:
                _add(rows, section, "", "(值)", e, a)
            continue

        # 「整节不存在」（`{"present": false}`）先处理：它是**表/字段被裁剪**，
        # 不是「某个字段消失了」——否则会打出一行 `n: None → 417` 这种误导性差异。
        e_absent = len(e) == 1 and e.get("present") is False
        a_absent = len(a) == 1 and a.get("present") is False
        if e_absent or a_absent:
            if e_absent != a_absent:
                _add(rows, section, "", "present", not e_absent, not a_absent)
            continue

        # `codes` 是清单：按集合比，产出「+ 新增 / - 消失」两行，比逐元素行好读
        e_codes = e.get("codes") if isinstance(e.get("codes"), list) else None
        a_codes = a.get("codes") if isinstance(a.get("codes"), list) else None
        for side, codes in (("+", a_codes), ("-", e_codes)):
            other = e_codes if side == "+" else a_codes
            if codes is not None and other is not None:
                delta = sorted(set(codes) - set(other))
                if delta:
                    _add(rows, section, "", f"codes{side}",
                         None if side == "+" else delta,
                         delta if side == "+" else None)

        e_scal, a_scal = _flatten_scalars({k: v for k, v in e.items() if k != "by_code"}), \
            _flatten_scalars({k: v for k, v in a.items() if k != "by_code"})
        for field in sorted(set(e_scal) | set(a_scal)):
            ev, av = e_scal.get(field, "<缺>"), a_scal.get(field, "<缺>")
            if ev != av and field != "codes":
                _add(rows, section, "", field, ev if ev != "<缺>" else None,
                     av if av != "<缺>" else None)

        # 逐标的（by_code）：只看两端都有的字段，新增/消失单独成行
        e_bc = e.get("by_code") or {}
        a_bc = a.get("by_code") or {}
        if isinstance(e_bc, dict) and isinstance(a_bc, dict):
            for code in sorted(set(e_bc) | set(a_bc)):
                ec, ac = e_bc.get(code), a_bc.get(code)
                if ec is None or ac is None:
                    _add(rows, section, code, "present", ec is not None, ac is not None)
                    continue
                ec_f, ac_f = _flatten_scalars(ec), _flatten_scalars(ac)
                for field in sorted(set(ec_f) | set(ac_f)):
                    if ec_f.get(field) != ac_f.get(field):
                        _add(rows, section, code, field, ec_f.get(field), ac_f.get(field))

    # 内容摘要单独一段：行数与摘要分开报，摘要变了才是「内容变了」
    e_c = expected.get("content_sha256") or {}
    a_c = actual.get("content_sha256") or {}
    for table in sorted(set(e_c) | set(a_c)):
        ec, ac = e_c.get(table), a_c.get(table)
        if ec is None or ac is None:
            _add(rows, "content_sha256", table, "present", ec is not None, ac is not None)
            continue
        if not isinstance(ec, dict) or not isinstance(ac, dict):
            continue
        for field in ("rows", "sha256"):
            if ec.get(field) != ac.get(field):
                _add(rows, "content_sha256", table, field, ec.get(field), ac.get(field))
    rows.sort(key=_sort_key)
    return {"rows": rows, "n_changes": len(rows)}


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "有" if value else "无"
    if isinstance(value, list):
        head = ", ".join(str(v) for v in value[:6])
        return head + (f" …（共 {len(value)} 个）" if len(value) > 6 else "")
    text = str(value)
    if len(text) == 64:                     # sha256 → 缩到 12 位，够比对
        return text[:12] + "…"
    return text


def format_fingerprint_diff(expected: dict | None, actual: dict | None, *,
                            taken_at: str | None = None, label: str = "库指纹",
                            max_rows: int = MAX_DIFF_ROWS) -> str:
    """并排打印「基线时刻的库 vs 现在的库」，最后给一句**结论**。"""
    lines: list[str] = []
    if actual is None:
        return f"📚 {label}：当前没有库可比（库不存在）—— 无法归因。\n"
    if not expected:
        return (
            f"📚 {label}：**基线里没有库指纹**（该条目生成于加指纹之前，无法重建当时的库）。\n"
            "     —— 归因时请手工比对库现状（`.venv/bin/python scripts/check_redlines.py"
            " --fingerprint`），并按 #34 三步决定是否需要重新基线（`--regen` 会一并补上指纹）。\n")

    diff = diff_fingerprint(expected, actual)
    rows = diff["rows"]
    when = f"（基线 {taken_at}）" if taken_at else ""

    # 逐标的的行（`by_code`）单列在后：K 线逐日增长每天都在动，是真信息但不解释「这次红」。
    # ⚠️ `content_sha256` 的 `key` 是**表名**不是标的，不能混进来计数。
    head = [r for r in rows if r["key"] == "" or r["section"] == "content_sha256"]
    per_code = [r for r in rows if r["key"] != "" and r["section"] != "content_sha256"]
    head_cap = max_rows
    per_code_cap = 8

    lines.append(f"📚 {label}对比{when}：基线时刻 vs 现在")
    lines.append(f"    {'节':<18}{'键':<14}{'字段':<26}{'基线':<22}现在")
    for row in head[:head_cap]:
        lines.append(f"    {row['section']:<18}{row['key'] or '—':<14}"
                     f"{row['field']:<26}{_fmt(row['expected']):<22}{_fmt(row['actual'])}")
    if len(head) > head_cap:
        lines.append(f"    …（整表/清单类差异还有 {len(head) - head_cap} 行）")
    for row in per_code[:per_code_cap]:
        lines.append(f"    {row['section']:<18}{row['key']:<14}"
                     f"{row['field']:<26}{_fmt(row['expected']):<22}{_fmt(row['actual'])}")
    if len(per_code) > per_code_cap:
        lines.append(f"    …（另有 {len(per_code) - per_code_cap} 行逐标的差异：K 线逐日增长属常态，"
                     "不单独列全）")

    lines.append("")
    if not rows:
        lines.append("   ⇒ **库指纹与基线完全一致** —— 这次红**不是「库涨了」**（标的、K 线、"
                     "账本、复权链、关键表内容都没动），请直接按 #34 三步做**代码**归因实验。")
    else:
        lines.append(f"   ⇒ **库变了**（{len(rows)} 处差异）—— 这次红很可能是「数据/口径涨了」"
                     "而不是代码回归。**先按库变异解释，再去代码里找回归**；")
        lines.append("      确认库变异是**有意**的（本次采集/回放/补链造成的）之后，才 `--regen`"
                     " 重新基线，并把新值 + 日期 + 原因 append-only 追加进台账。")
        touched = sorted({r["section"] for r in rows})
        lines.append(f"      涉及：{', '.join(touched)}")
        if per_code:
            codes = sorted({r["key"] for r in per_code})
            lines.append(f"      逐标的差异涉及 {len(codes)} 个标的（首个：{codes[0]}）；"
                         "只有 `by_code` 变了而整表规模没变时，才需要逐个看。")
    lines.append("")
    return "\n".join(lines)


def format_fingerprint_json(fp: dict) -> str:
    import json
    return json.dumps(fp, ensure_ascii=False, sort_keys=True, indent=2)
