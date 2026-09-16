"""链路准确率视图（P26）：四段分列，各自标注来源与样本量。

## 这份报告存在的理由

「智能体全链路准不准」这个问题，一句话问不出来 —— 链上有四种**性质完全不同**的数据：

1. **回放段**：`verify backfill` 的 PIT 逐日重放。它是合法的样本外评估，
   但**不是实盘表现**；把它写成「实盘」是本项目最贵的一类错（ERROR_DIARY #16）。
2. **实时段**：真实运行期每天真出的预测（样本量天然很小）。
3. **模拟盘段**：`paper_nav_daily` 三条机械臂 vs `index_300`。**不含任何模型信号**，
   它衡量的是纪律与分散，不是模型的准确率。
4. **实盘段**：`real_trades` 的真实成交。

四者**不许混算**。所以本模块的输出是四个并列的段，每段自带来源说明、样本量、
口径判据与门槛判定；**任何一段 `n_days < 120` 都不给结论**（只看 n 与原始读数）。

## 回放 / 实时的判据（P32：断言优先，推断兜底）

`predictions` 新增 `origin` 来源列（`live`/`replay`，入库时由写入路径确定）后，
**有 `origin` 的行按字段断言**分段；`origin IS NULL` 的历史行才退回
`session/review.py` 的 `classify`（ERROR_DIARY #16 定的口径）：
`LIVE ⟺ created_at 前 10 位 == asof_date`。两者**分列计数**，不许把推断当断言。
边界（`created_at` 是入库时刻而非决策时刻）照实写进报告，见 `origin_limits`。

## 本模块是纯函数

不读时钟、不写库、不含 `created_at`。所以「同输入两次运行逐字节一致」是结构性的，
不是靠「跑两次碰巧一样」。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from stocklab.risk.kelly import (
    BOOTSTRAP_N,
    BOOTSTRAP_SEED,
    daily_clustered_winrate_ci,
)
from stocklab.session.review import classify, load_rows
from stocklab.verify.report import MIN_DAYS, summarize

#: 四段的固定顺序。**顺序本身是设计**：回放 → 实时 → 模拟盘 → 实盘
#: 就是链路的时间/性质顺序，报告与 JSON 都按它排，读者不会读串。
SEGMENTS: tuple[str, ...] = ("replay", "live", "paper", "real")

SEGMENT_TITLES: dict[str, str] = {
    "replay": "回放段（PIT 历史重放）",
    "live": "实时段（真实运行期）",
    "paper": "模拟盘段（三臂 vs index_300）",
    "real": "实盘段（real_trades）",
}

#: 样本不足时**必须**逐字出现的判据句。`{n}` = 交易日数，`{min_days}` = 门槛。
NOTICE_TMPL = "样本不足（n={n} < {min_days}），不构成准确率结论"

#: 判定字段说明（进报告，供审计者逐字复核）。
ORIGIN_FIELDS = (
    "predictions.origin（P32 来源列，`live`/`replay`，入库时由写入路径确定）；"
    "`origin IS NULL` 的历史行退回 "
    "predictions.created_at（前 10 位，**字符串比较**，不走 SQLite `date()`："
    "`created_at` 带 `+08:00`，`date()` 会折成 UTC，凌晨的预测会在交易日边界上静默错分）"
    " 与 predictions.asof_date"
)

#: 分段规则原文（进报告）：断言优先、推断兜底、两者分列计数。
ORIGIN_ASSERTION_RULE = (
    "有 `origin` 标记（`live`/`replay`）的行按字段**断言**分段；"
    "`origin IS NULL` 的历史行退回推断（`created_at` 前 10 位 == `asof_date`）。"
    "断言行与推断行**分列计数**，不许把推断当断言。"
)

#: 判定规则的已知边界（不许藏）。
ORIGIN_LIMITS = (
    "`origin` 由 `predict run`（`live`）/ `verify backfill`（`replay`）各自在入库时写入，"
    "是**断言**；加列前已存在的历史行 `origin` 为 NULL，退回 `created_at[:10] == "
    "asof_date` 推断，其边界照旧：`created_at` 是**入库时刻**而非决策时刻 —— "
    "当天补跑一个历史 `asof` 会被记成 LIVE，反之跨零点补跑当日预测会被记成 REPLAY。"
)

#: 实盘段「能不能算准确率」的判据说明。
REAL_ACCURACY_GATE = (
    "实盘段要算准确率，需要「预测 → 成交 → 持有」的**可比配对样本**："
    "成交行必须能连回当时的那条预测（`pred_id`）与持有区间。"
    "当前 `real_trades` 没有任何指回 `predictions` 的列 —— 所以即使笔数够，"
    "也只能算「成交层面的成本与换手」，**算不出预测准确率**。"
)

#: 模拟盘段的纪律：只并列，不挑冠军。
PAPER_NO_PICK = (
    "三臂是**并行对照**，本节**只并列**：不做排名、不做倾向性表述、"
    "不给「哪条更好」的结论。在 2 个交易日上挑出「冠军」"
    "＝多重比较下的必然噪声，不是 edge。"
)

_MIN = MIN_DAYS


# ---------- 小工具 ----------

def _num(x: Any, nd: int = 4) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(x: Any, nd: int = 2) -> str:
    return "—" if x is None else f"{float(x) * 100:.{nd}f}%"


def _provenance_of(asof_date: str, created_at: str, origin: Any) -> str:
    """有效来源（P32）：有 `origin` 标记按字段**断言**，NULL/未知退回推断。

    `origin` 只可能是 `live` / `replay` / NULL（`TEXT CHECK` 在库里钉死），
    这里仍显式判 `in ("live", "replay")`：任何落到 CHECK 之外的值都**不许**
    被当成断言 —— 退回推断是唯一诚实的兜底。
    """
    return origin if origin in ("live", "replay") else classify(asof_date, created_at)


def _daily_cell(block: Mapping[str, Any] | None) -> str:
    """`均值 ± 标准误 [95% CI]（n 天）` —— 与 `verify.report` 的同款读数格式。"""
    if not block or block.get("mean") is None:
        return f"—（n_days={0 if not block else block.get('n_days', 0)}）"
    ci = block.get("ci95")
    ci_txt = "—" if not ci else f"[{float(ci[0]):.4f}, {float(ci[1]):.4f}]"
    return (f"{float(block['mean']):.4f} ± {float(block['se'] or 0):.4f} "
            f"{ci_txt}（n={block['n_days']} 天）")


def _boot_cell(ci: Sequence[float] | None) -> str:
    if not ci:
        return "—（日数不足 2，不编造区间宽度）"
    return f"[{float(ci[0]):.4f}, {float(ci[1]):.4f}]"


def _notice(n_days: int, min_days: int) -> str:
    return NOTICE_TMPL.format(n=n_days, min_days=min_days)


def _gate(n_days: int, min_days: int) -> dict[str, Any]:
    ok = n_days >= min_days
    return {
        "effective_n_days": n_days,
        "min_days": min_days,
        "sufficient": ok,
        "label": "样本充足" if ok else _notice(n_days, min_days),
        # 判据句只在不足时出现；充足时为 None（避免读者把它当免责声明挂在好数字旁边）
        "notice": None if ok else _notice(n_days, min_days),
    }


def boot_ci_by_day(rows: Sequence[Mapping]) -> list[float] | None:
    """方向命中的**日度聚类 bootstrap** 95% CI（复用 `risk.kelly` 的先验固定参数）。

    重采样的是**天**不是**行**：同一天的多只标的共享同一段市场波动，
    按行重采样会把它们当独立样本，区间会窄得离谱（CLAUDE.md 度量纪律 2）。
    """
    by_day: dict[str, list[float]] = {}
    for r in rows:
        if not r.get("scorable") or r.get("hit_direction") is None:
            continue
        by_day.setdefault(r["target_date"], []).append(float(r["hit_direction"]))
    days = [by_day[d] for d in sorted(by_day)]
    if not days:
        return None
    return daily_clustered_winrate_ci(days, n_boot=BOOTSTRAP_N, seed=BOOTSTRAP_SEED)


# ---------- 段 1/2：回放段、实时段 ----------

def _accuracy_segment(seg: str, rows: Sequence[Mapping], *, from_date: str,
                      to_date: str, min_days: int,
                      predictions_written: int) -> dict[str, Any]:
    """回放段 / 实时段共用的一份构造（两段的**唯一**差别就是喂进来的行）。"""
    metrics = summarize(rows, from_date=from_date, to_date=to_date,
                        min_days=min_days)
    # 每个 model_version 单加一条日度 bootstrap CI（**不覆盖** summarize 的任何数字）
    for mv, group in metrics["model_versions"].items():
        sub = [r for r in rows if r["model_version"] == mv]
        group["direction_boot_ci95"] = boot_ci_by_day(sub)
        group["bootstrap"] = {"n_boot": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED,
                              "method": "day_clustered_percentile_bootstrap"}

    days = sorted({r["target_date"] for r in rows if r["scorable"]})
    scorable = [r for r in rows if r["scorable"]]
    return {
        "segment": seg,
        "title": SEGMENT_TITLES[seg],
        "source": ("`verifications` ⋈ `predictions`，取 provenance == "
                   f"`{seg}` 的行"),
        # 窗口 = 本段**实际有行**的区间（不是查询请求的区间）：空段显示
        # 「2013-12-23 → 2026-09-15」会被读成「有 13 年的数据」，那是假的。
        "window": {"from": days[0] if days else None,
                   "to": days[-1] if days else None},
        "requested_window": {"from": from_date, "to": to_date},
        "n_rows": len(rows),
        "n_scorable": len(scorable),
        "n_days": len(days),
        # 「写了多少条预测」与「其中多少条已被验证打分」**分列**：
        # 前者是模型的产出量，后者才是准确率的分母来源。混成一个数会让
        # 「实时预测已产出但尚未打分」被读成「样本量 0 = 什么都没发生」。
        "predictions_written": predictions_written,
        "predictions_verified": len(rows),
        "model_versions": sorted(metrics["model_versions"]),
        "metrics": metrics,
        "gate": _gate(len(days), min_days),
        # 只有**非准确率**的原始计数放在结论区之外，任何准确率数字都在 metrics 里，
        # 而 metrics 只在 sufficient 时才被渲染成表（见 render_markdown）。
        "readings": {
            "n_rows": len(rows),
            "n_scorable": len(scorable),
            "n_unscorable": len(rows) - len(scorable),
            "window": {"from": days[0] if days else None,
                       "to": days[-1] if days else None},
        },
    }


# ---------- 段 3：模拟盘 ----------

def _load_paper_rows(conn: sqlite3.Connection, from_date: str | None,
                     to_date: str | None) -> list[dict]:
    sql = ("SELECT account_id, date, nav, cum_return, net_deposits,"
           " index_300_level, index_300_asof FROM paper_nav_daily")
    where, args = [], []
    if from_date:
        where.append("date >= ?")
        args.append(from_date)
    if to_date:
        where.append("date <= ?")
        args.append(to_date)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY account_id, date"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _paper_segment(conn: sqlite3.Connection, *, from_date: str | None,
                   to_date: str | None, min_days: int) -> dict[str, Any]:
    rows = _load_paper_rows(conn, from_date, to_date)
    by_account: dict[str, list[dict]] = {}
    for r in rows:
        by_account.setdefault(r["account_id"], []).append(r)

    arms: list[dict[str, Any]] = []
    index_window: float | None = None
    index_days = 0
    for account_id, rs in sorted(by_account.items()):
        first, last = rs[0], rs[-1]
        if index_window is None and len(rs) >= 2:
            lv0, lv1 = first.get("index_300_level"), last.get("index_300_level")
            # 指数点位必须**真属于**那一天（`index_300_asof` 是 PIT 自审列），
            # 否则宁可不算：拿错日的点位相除会得到一个看起来正常的差值。
            ok0 = lv0 is not None and first.get("index_300_asof") == first["date"]
            ok1 = lv1 is not None and last.get("index_300_asof") == last["date"]
            if ok0 and ok1 and lv0:
                index_window = float(lv1) / float(lv0) - 1.0
                index_days = len({r["date"] for r in rs})
        r0, r1 = first.get("cum_return"), last.get("cum_return")
        win = None
        if len(rs) >= 2 and r0 is not None and r1 is not None and (1.0 + r0) != 0:
            win = (1.0 + float(r1)) / (1.0 + float(r0)) - 1.0
        arms.append({
            "account_id": account_id,
            "n_days": len(rs),
            "date_from": first["date"],
            "date_to": last["date"],
            "nav_end": last.get("nav"),
            "cum_return_end": r1,
            "window_return": win,
            "index_300_window_return": index_window,
            "excess": (None if win is None or index_window is None
                       else win - index_window),
        })

    n_days = max([len(v) for v in by_account.values()], default=0)
    return {
        "segment": "paper",
        "title": SEGMENT_TITLES["paper"],
        "source": "`paper_nav_daily`（每账户每日一行；**不含任何模型信号**）",
        "window": {"from": rows[0]["date"] if rows else None,
                   "to": rows[-1]["date"] if rows else None},
        "n_rows": len(rows),
        "n_days": n_days,
        "arms": arms,
        "index_300_window_return": index_window,
        "index_300_n_days": index_days,
        "metrics": None,          # 模拟盘不产出「预测准确率」，这一格**故意为空**
        "gate": _gate(n_days, min_days),
        "readings": {
            "n_arms": len(arms),
            "n_days": n_days,
            "index_300_window_return": index_window,
        },
        "note": PAPER_NO_PICK,
    }


# ---------- 段 4：实盘 ----------

def _real_segment(conn: sqlite3.Connection, *, from_date: str | None,
                  to_date: str | None, min_days: int) -> dict[str, Any]:
    sql = "SELECT date, side, fee FROM real_trades"
    where, args = [], []
    if from_date:
        where.append("date >= ?")
        args.append(from_date)
    if to_date:
        where.append("date <= ?")
        args.append(to_date)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trade_id"
    trades = [dict(r) for r in conn.execute(sql, args).fetchall()]

    # 现金流出里的手续费/税：与成交上的 `fee` 分列，避免「成本合计」漏掉一类
    flow_rows = conn.execute(
        "SELECT kind, amount FROM cash_flows WHERE kind IN ('fee','tax')"
    ).fetchall()
    flow_cost = sum(-float(r["amount"]) for r in flow_rows)

    trade_fees = sum(float(t["fee"] or 0.0) for t in trades)
    days = sorted({t["date"] for t in trades})
    return {
        "segment": "real",
        "title": SEGMENT_TITLES["real"],
        "source": "`real_trades`（append-only 真实成交）+ `cash_flows` 里的 fee/tax",
        "window": {"from": days[0] if days else None,
                   "to": days[-1] if days else None},
        "n_rows": len(trades),
        "n_days": len(days),
        "n_trades": len(trades),
        "n_buy": sum(1 for t in trades if t["side"] == "buy"),
        "n_sell": sum(1 for t in trades if t["side"] == "sell"),
        "trade_fees": trade_fees,
        "cash_flow_fee_tax": flow_cost,
        "cost_total": trade_fees + flow_cost,
        "days": days,
        "metrics": None,          # 实盘不产出「预测准确率」，理由见 REAL_ACCURACY_GATE
        "gate": _gate(len(days), min_days),
        "readings": {"n_trades": len(trades), "n_days": len(days),
                     "cost_total": trade_fees + flow_cost},
        "accuracy_feasible": False,
        "accuracy_note": REAL_ACCURACY_GATE,
    }


# ---------- 组装 ----------

def _window(conn: sqlite3.Connection, from_date: str | None,
            to_date: str | None) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MIN(target_date) AS a, MAX(target_date) AS b FROM verifications"
    ).fetchone()
    lo = from_date or (row["a"] if row else None) or "0000-00-00"
    hi = to_date or (row["b"] if row else None) or "9999-12-31"
    return str(lo), str(hi)


def _written_by_origin(conn: sqlite3.Connection) -> dict[str, int]:
    """**已写入**的预测按来源计数（与「已被验证打分」是两件事）。

    没有这一对读数，实时段 `n_days = 0` 会被读成「根本没有实时预测」——
    而真实情况是「实时预测写了，但它的目标日还没到/还没打分」。判据走
    `_provenance_of()`（有 `origin` 断言、无 `origin` 退 `classify()`），
    不在 SQL 里重写一遍规则。
    """
    rows = conn.execute(
        "SELECT asof_date, created_at, origin FROM predictions").fetchall()
    live = sum(1 for r in rows
               if _provenance_of(r["asof_date"], r["created_at"],
                                 r["origin"]) == "live")
    return {"live": live, "replay": len(rows) - live}


def build_chain_accuracy(conn: sqlite3.Connection, *, from_date: str | None = None,
                         to_date: str | None = None,
                         min_days: int = _MIN) -> dict[str, Any]:
    """四段分列的链路准确率报告（纯读）。"""
    lo, hi = _window(conn, from_date, to_date)
    rows = load_rows(conn, lo, hi)
    by_origin: dict[str, list[dict]] = {"replay": [], "live": []}
    for r in rows:
        by_origin[_provenance_of(r["asof_date"], r["pred_created_at"],
                                 r.get("origin"))].append(r)
    written = _written_by_origin(conn)
    asserted = sum(1 for r in rows if r.get("origin") in ("live", "replay"))

    segments = [
        _accuracy_segment("replay", by_origin["replay"], from_date=lo,
                          to_date=hi, min_days=min_days,
                          predictions_written=written["replay"]),
        _accuracy_segment("live", by_origin["live"], from_date=lo,
                          to_date=hi, min_days=min_days,
                          predictions_written=written["live"]),
        _paper_segment(conn, from_date=from_date, to_date=to_date,
                       min_days=min_days),
        _real_segment(conn, from_date=from_date, to_date=to_date,
                      min_days=min_days),
    ]
    assert [s["segment"] for s in segments] == list(SEGMENTS), "段顺序被改动了"

    return {
        "kind": "chain-accuracy",
        "min_days": min_days,
        "segment_order": list(SEGMENTS),
        "origin_rule": ORIGIN_ASSERTION_RULE,
        "origin_fields": ORIGIN_FIELDS,
        "origin_limits": ORIGIN_LIMITS,
        "origin_segmentation": {
            "rule": ORIGIN_ASSERTION_RULE,
            "asserted": asserted,                 # 按 origin 字段断言分段的行数
            "inferred": len(rows) - asserted,     # 退回推断的行数
        },
        "provenance_counts": {"live": len(by_origin["live"]),
                              "replay": len(by_origin["replay"])},
        "segments": segments,
    }


# ---------- 渲染 ----------

def _render_accuracy_body(g: Mapping[str, Any], seg: str) -> list[str]:
    """一个 model_version 的读数表（**只在样本充足时被调用**）。"""
    L: list[str] = []
    L.append(f"- 预测条数 {g['n_predictions']} / 可评分 {g['n_scorable']} / "
             f"不可评分 {g['n_unscorable']} {g['unscorable_reasons'] or ''}")
    L.append(f"- **有效样本量（交易日数）{g['effective_n']}**，行数 {g['n_rows']}"
             f"（参考值，**不得**当样本量）；日均 "
             f"{_num(g['rows_by_day_avg'], 2)} 行/日")
    L.append(f"- 本 `model_version` 的样本门槛：{g['sample_gate']['label']}"
             f"（阈值 {g['sample_gate']['min_days']} 交易日）")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|------|----|")
    L.append(f"| 方向准确率（行级，三分类 ±0.5%） | "
             f"{_pct(g['direction']['accuracy_row'])} |")
    L.append(f"| **方向准确率（按日聚类 ± 标准误 [95% CI]）** | "
             f"{_daily_cell(g['direction']['accuracy_daily'])} |")
    if seg in ("replay", "live"):
        L.append(f"| 方向准确率（**日度 bootstrap** 95% CI，"
                 f"n_boot={g['bootstrap']['n_boot']}，"
                 f"seed={g['bootstrap']['seed']}） | "
                 f"{_boot_cell(g['direction_boot_ci95'])} |")
    L.append(f"| Brier（行级，越小越好；随机猜 ≈ 0.667） | "
             f"{_num(g['direction']['brier_row'])} |")
    L.append(f"| Brier（按日聚类） | {_daily_cell(g['direction']['brier_daily'])} |")
    L.append(f"| `range_80` 覆盖率（标称 80%，行级） | "
             f"{_pct(g['range']['coverage_row'])} |")
    L.append(f"| `range_80` 覆盖率（按日聚类） | "
             f"{_daily_cell(g['range']['coverage_daily'])} |")
    L.append(f"| 关键位「会/不会触及」全部兑现率（行级） | "
             f"{_pct(g['levels']['hit_all_row'])} |")
    L.append(f"| 关键位逐位兑现率（行级） | "
             f"{_pct(g['levels']['level_realized_row'])} |")
    L.append(f"| 决策层：`action` 累计收益（扣成本，按日算术和） | "
             f"{_pct(g['action']['sim_ret_sum'])} |")
    L.append(f"| 决策层：`buy_and_hold` 同日累计（同成本，**可执行**） | "
             f"{_pct(g['action']['bh_ret_sum'])} |")
    L.append(f"| 决策层：`index_300` 同期累计（**不可交易**，仅参照） | "
             f"{_pct(g['action']['index_ret_sum'])} |")
    L.append(f"| 决策层：超额（`action` − `index_300`，按日聚类） | "
             f"{_daily_cell(g['action']['excess_daily'])} |")
    L.append(f"| 决策层：超额为正的比例（行级） | "
             f"{_pct(g['action']['excess_win_rate'])} |")
    L.append("")
    L.append("| 基准对照（同样的日、同样的样本） | 行级准确率 | 按日聚类 |")
    L.append("|--------------------------------|-----------|----------|")
    for name, b in g["baselines"].items():
        L.append(f"| {name} | {_pct(b['accuracy_row'])} | "
                 f"{_daily_cell(b['accuracy_daily'])} |")
    L.append("")
    return L


def _render_segment(s: Mapping[str, Any]) -> list[str]:
    L: list[str] = []
    L.append(f"## {s['title']}")
    L.append("")
    L.append(f"- **来源**：{s['source']}")
    w = s.get("window") or {}
    if w.get("from") or w.get("to"):
        L.append(f"- **区间**：{w.get('from') or '—'} → {w.get('to') or '—'}")
    if s.get("predictions_written") is not None:
        pending = s["predictions_written"] - s["predictions_verified"]
        tail = ("" if pending == 0 else
                f"（差 {pending} 条 = 目标日还没到 / 还没打分，**不是**「没有预测」）")
        L.append(f"- **该来源已写入的预测**：{s['predictions_written']} 条；"
                 f"其中**已被验证打分**：{s['predictions_verified']} 条{tail}")
    L.append(f"- **样本量**：有效交易日数 **n_days = {s['n_days']}**，"
             f"行数 {s['n_rows']}（行数只作参考，**不得**当样本量）")
    L.append(f"- **门槛**：{s['gate']['min_days']} 交易日"
             f"（与 `verify.report.MIN_DAYS` 同源）")
    L.append("")

    if s["gate"]["sufficient"]:
        if s["metrics"]:
            for mv, g in s["metrics"]["model_versions"].items():
                L.append(f"### `{mv}`")
                L.append("")
                L.extend(_render_accuracy_body(g, s["segment"]))
        if s["segment"] == "paper":
            L.extend(_render_paper_body(s))
        if s["segment"] == "real":
            L.extend(_render_real_body(s))
    else:
        L.append(f"⚠️ **{s['gate']['notice']}**")
        L.append("")
        if s["segment"] in ("replay", "live"):
            L.append("> 本段的**准确率指标一律不渲染** —— 样本量没到门槛时，"
                     "任何读数都会被读成结论。要看读数请等样本够了再跑。")
            L.append("")
        else:
            L.extend(_render_paper_body(s) if s["segment"] == "paper"
                     else _render_real_body(s))
            L.append("> 上表是**原始读数，仅供观察**，不构成任何结论"
                     "（样本量未过门槛，见上面的样本不足判据）。")
            L.append("")
    if s.get("accuracy_note") and s["segment"] == "real":
        L.append(f"> {s['accuracy_note']}")
        L.append("")
    return L


def _render_paper_body(s: Mapping[str, Any]) -> list[str]:
    L: list[str] = []
    L.append("| 臂 | 天数 | 区间 | 区间涨跌 | `cum_return`（期末，臂自身口径） | "
             "`index_300` 区间涨跌 | 差值（臂 − 指数） |")
    L.append("|----|------|------|---------|------------------------------|"
             "---------------------|------------------|")
    for a in s["arms"]:
        L.append(f"| `{a['account_id']}` | {a['n_days']} | "
                 f"{a['date_from']} → {a['date_to']} | "
                 f"{_pct(a['window_return'])} | {_pct(a['cum_return_end'])} | "
                 f"{_pct(a['index_300_window_return'])} | {_pct(a['excess'])} |")
    L.append("")
    L.append(f"> `index_300` 区间涨跌（全体臂同一段）："
             f"{_pct(s.get('index_300_window_return'))}"
             f"（用 `index_300_asof == date` 自审过的点位，缺则不参与）")
    L.append("")
    if s.get("note"):
        L.append(f"> {s['note']}")
        L.append("")
    return L


def _render_real_body(s: Mapping[str, Any]) -> list[str]:
    L: list[str] = []
    L.append("| 项 | 值 |")
    L.append("|----|----|")
    L.append(f"| 成交笔数 | {s['n_trades']}（买 {s['n_buy']} / 卖 {s['n_sell']}） |")
    L.append(f"| 有成交的交易日数 | {s['n_days']} |")
    L.append(f"| 成交手续费合计（`real_trades.fee`） | {_num(s['trade_fees'], 2)} |")
    L.append(f"| 现金流手续费/税合计（`cash_flows` 的 fee/tax） | "
             f"{_num(s['cash_flow_fee_tax'], 2)} |")
    L.append(f"| **成本合计** | **{_num(s['cost_total'], 2)}** |")
    verdict = "够" if s["accuracy_feasible"] else "**不够**（且当前口径下算不出，理由见下）"
    L.append(f"| 够不够算准确率 | {verdict} |")
    L.append("")
    return L


def render_markdown(report: Mapping[str, Any]) -> str:
    """渲染成 markdown（确定性：不含任何生成时刻、不含 `created_at`）。"""
    L: list[str] = []
    L.append("# 链路准确率视图（四段分列）")
    L.append("")
    L.append("> **回放段的数字不是实盘表现。** 四段来源不同、样本量不同，"
             "**不许混算、不许互相填充**：回放段是 PIT 历史重放（样本外评估），"
             "实时段是真实运行期，模拟盘段衡量的是纪律与分散（不含模型信号），"
             "实盘段是真实成交。每段各自标注来源与样本量。")
    L.append("")
    L.append("## 0. 回放 / 实时是怎么判定的")
    L.append("")
    L.append(f"- **判据**：{report['origin_rule']}")
    L.append(f"- **判定字段**：{report['origin_fields']}")
    seg = report["origin_segmentation"]
    L.append(f"- **分段方式**：按 `origin` 断言 {seg['asserted']} 行 / "
             f"退回推断 {seg['inferred']} 行（不混）")
    L.append(f"- **行数**：live {report['provenance_counts']['live']} 行 / "
             f"replay {report['provenance_counts']['replay']} 行")
    L.append(f"- **已知边界**：{report['origin_limits']}")
    L.append("")
    for s in report["segments"]:
        L.extend(_render_segment(s))
    L.append("---")
    L.append("")
    L.append(f"> 样本门槛：任一段 `n_days < {report['min_days']}` → "
             "只报样本量、不给结论。口径与工具复用仓库既有实现"
             "（`verify.report.summarize` / `risk.kelly.daily_clustered_winrate_ci`），"
             "本模块不新造统计量。")
    L.append("")
    return "\n".join(L)
