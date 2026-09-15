#!/usr/bin/env python
"""异质子群诊断（P8 Task 42）：命中 `invalidate_if` 的行方向准确率为何高达 66.9%？

只读、只诊断，**不改任何策略/口径**。全部数字从库里现算（`verifications` ×
`predictions`），可复现：`.venv/bin/python scripts/diagnose_invalidated_subgroup.py`。

要回答的三选一：
  (a) 泄漏 / 口径 bug；
  (b) 标签构造或失效条件与结果**同源**（失效条件本身就在描述明天）；
  (c) 真实可解释的异质性。

关键中间量（缺一不可）：
  1. 子群规模与准确率（复现 66.9% / 34.5%）；
  2. **条件类型分布**：跌破下界 vs 站上上界 —— 各自对应的 `actual_class` 是**确定**的；
  3. `actual_class` 在子群内的分布（是否为「非 flat」的机械后果）；
  4. 同期 `always_up` / `always_down` 在这些行上的表现（模型是否真的比常数猜法强）；
  5. 日期分布（按年 / 按标的）—— 是否集中在少数几天；
  6. 边界距离：`close_asof` 距 support / resistance 的相对距离（趋势中的不对称性）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stocklab.verify.score import parse_invalidate_bounds  # noqa: E402

DB = ROOT / "data" / "stocklab.db"


def load(db: Path):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT v.target_date, v.invalidated, v.hit_direction, v.notes,
               p.code, p.asof_date, p.invalidate_if,
               p.direction_up, p.direction_flat, p.direction_down
        FROM verifications v JOIN predictions p ON p.pred_id = v.pred_id
        WHERE v.total_score IS NOT NULL
        ORDER BY v.target_date, p.code
    """
    return [dict(r) for r in conn.execute(sql)]


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def pct(x):
    return "—" if x is None else f"{100 * x:.2f}%"


def pred_class(r: str) -> str:
    """模型的预测类（与 P6 `action` 同序：flat 优先于 up/down 的平手）。"""
    return max(("flat", "up", "down"),
               key=lambda c: {"up": r["direction_up"], "flat": r["direction_flat"],
                              "down": r["direction_down"]}[c])


def main() -> int:
    rows = load(DB)
    for r in rows:
        r["notes"] = json.loads(r["notes"]) if r["notes"] else {}
        r["actual_class"] = r["notes"].get("actual_class")
        b = parse_invalidate_bounds(r["invalidate_if"])
        r["lo"], r["hi"] = b if b else (None, None)
        r["close_asof"] = r["notes"].get("close_asof")
        r["close_t"] = r["notes"].get("close_target")
        r["pred"] = pred_class(r)

    known = [r for r in rows if r["invalidated"] is not None
             and r["actual_class"]]
    undet = [r for r in rows if r["invalidated"] is None]
    hit = [r for r in known if r["invalidated"] == 1]
    miss = [r for r in known if r["invalidated"] == 0]

    print("=" * 78)
    print("0. 规模与复现")
    print("=" * 78)
    print(f"可评分行 total_score IS NOT NULL : {len(rows)}")
    print(f"  invalidated 判不了（UNDETERMINED）: {len(undet)}")
    print(f"  可判 invalidated                : {len(known)}")
    print(f"    ├ 命中 invalidate_if (=1)     : {len(hit)}  方向准确率 {pct(mean(r['hit_direction'] for r in hit))}")
    print(f"    └ 未命中 (=0)                 : {len(miss)}  方向准确率 {pct(mean(r['hit_direction'] for r in miss))}")

    # ---- 2. 条件类型：跌破下界 / 站上上界 -------------------------------
    print()
    print("=" * 78)
    print("1. 条件类型分布（命中子群内部）—— actual_class 是不是机械确定的？")
    print("=" * 78)
    kinds = defaultdict(list)
    for r in hit:
        if r["close_t"] is None or r["lo"] is None:
            kinds["无法解析/缺价"].append(r)
        elif r["close_t"] > r["hi"]:
            kinds["站上上界（突破）"].append(r)
        elif r["close_t"] < r["lo"]:
            kinds["跌破下界（破位）"].append(r)
        else:
            kinds["边界内（不应出现）"].append(r)
    for k, g in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        cls = Counter(r["actual_class"] for r in g)
        prd = Counter(r["pred"] for r in g)
        print(f"  {k:12s} n={len(g):5d}  实际类={dict(cls)}  模型预测类={dict(prd)}")
    # 关键判据：命中子群里 flat 占比
    flat_share = mean(1.0 if r["actual_class"] == "flat" else 0.0 for r in hit)
    print(f"\n  → 命中子群里 actual_class=='flat' 占比 = {pct(flat_share)}"
          f"（未命中子群 = {pct(mean(1.0 if r['actual_class']=='flat' else 0.0 for r in miss))}）")

    # ---- 3. 常数猜法对照 -------------------------------------------------
    print()
    print("=" * 78)
    print("2. 同期常数猜法对照（**同一批行**）—— 模型是否真的强于 always_*？")
    print("=" * 78)
    for label, g in (("命中 invalidate_if", hit), ("未命中", miss),
                     ("全体可判", known)):
        au = mean(1.0 if r["actual_class"] == "up" else 0.0 for r in g)
        ad = mean(1.0 if r["actual_class"] == "down" else 0.0 for r in g)
        af = mean(1.0 if r["actual_class"] == "flat" else 0.0 for r in g)
        print(f"  {label:18s} n={len(g):5d}  模型 {pct(mean(r['hit_direction'] for r in g)):>7s}"
              f"  | always_up {pct(au):>7s}  always_down {pct(ad):>7s}  always_flat {pct(af):>7s}")

    # ---- 4. 日期分布 -----------------------------------------------------
    print()
    print("=" * 78)
    print("3. 日期分布（命中子群是否集中在少数几天）")
    print("=" * 78)
    by_year = Counter(r["target_date"][:4] for r in hit)
    tot_year = Counter(r["target_date"][:4] for r in known)
    yrs = sorted(tot_year)
    print("  年份   命中/可判   命中率     命中行准确率")
    for y in yrs:
        g = [r for r in hit if r["target_date"][:4] == y]
        a = mean(r["hit_direction"] for r in g)
        print(f"  {y}   {len(g):5d}/{tot_year[y]:5d}   {pct(len(g)/tot_year[y]):>7s}   {pct(a):>7s}")
    days = Counter(r["target_date"] for r in hit)
    print(f"\n  命中子群覆盖 {len(days)} 个交易日；最多的 5 天：")
    for d, n in days.most_common(5):
        print(f"    {d}  n={n}")

    # ---- 5. 标的分布 -----------------------------------------------------
    print()
    print("=" * 78)
    print("4. 标的分布")
    print("=" * 78)
    by_code = defaultdict(list)
    tot_code = Counter(r["code"] for r in known)
    for r in hit:
        by_code[r["code"]].append(r)
    for c in sorted(tot_code):
        g = by_code.get(c, [])
        print(f"  {c}  命中 {len(g):5d}/{tot_code[c]:5d}  准确率 {pct(mean(r['hit_direction'] for r in g)):>7s}"
              f"  | 该标的未命中行准确率 "
              f"{pct(mean(r['hit_direction'] for r in miss if r['code']==c)):>7s}")

    # ---- 6. 边界距离不对称性 ---------------------------------------------
    print()
    print("=" * 78)
    print("5. close_asof 到两条边界的相对距离（趋势中的不对称性）")
    print("=" * 78)
    up_gap, dn_gap = [], []
    for r in known:
        if not r["close_asof"] or not r["hi"]:
            continue
        up_gap.append((r["hi"] - r["close_asof"]) / r["close_asof"])
        dn_gap.append((r["close_asof"] - r["lo"]) / r["close_asof"])
    print(f"  close_asof → resistance 的相对距离：均值 {pct(mean(up_gap))}"
          f"  中位 {pct(sorted(up_gap)[len(up_gap)//2])}")
    print(f"  close_asof → support    的相对距离：均值 {pct(mean(dn_gap))}"
          f"  中位 {pct(sorted(dn_gap)[len(dn_gap)//2])}")

    # 趋势方向 × 命中侧 交叉表：验证「突破方向 = 模型预测方向」
    print()
    print("  模型预测类 × 实际类（命中子群）：")
    tab = Counter((r["pred"], r["actual_class"]) for r in hit)
    for p in ("up", "flat", "down"):
        print(f"    pred={p:4s} " + "  ".join(
            f"actual={a}:{tab.get((p,a),0):4d}" for a in ("up", "flat", "down")))
    print("  模型预测类 × 实际类（未命中）：")
    tab2 = Counter((r["pred"], r["actual_class"]) for r in miss)
    for p in ("up", "flat", "down"):
        print(f"    pred={p:4s} " + "  ".join(
            f"actual={a}:{tab2.get((p,a),0):4d}" for a in ("up", "flat", "down")))

    # ---- 7. 条件距离：模型预测类 × 两条边界的距离 -------------------------
    print()
    print("=" * 78)
    print("6. 按模型预测类条件化的边界距离（共享输入 → 机械相关的来源）")
    print("=" * 78)
    for p in ("up", "flat", "down"):
        g = [r for r in known if r["pred"] == p and r["close_asof"]]
        if not g:
            continue
        gu = mean((r["hi"] - r["close_asof"]) / r["close_asof"] for r in g)
        gd = mean((r["close_asof"] - r["lo"]) / r["close_asof"] for r in g)
        n_hit = mean(1.0 if r["invalidated"] == 1 else 0.0 for r in g)
        print(f"  pred={p:4s} n={len(g):5d}  到上界 {pct(gu):>7s}  到下界 {pct(gd):>7s}"
              f"  → 命中率 {pct(n_hit):>7s}")

    # ---- 8. 决定性检验：PIT 可观测的「窄边界」能否复现这个子群？ -----------
    print()
    print("=" * 78)
    print("7. **决定性检验**：用 PIT 可观测的「窄边界」选行，能否复现 66.9%？")
    print("=" * 78)
    print("  若一个只用 <=asof 信息的选行规则也能拿到 ~67%，则异质性是**可交易**的；")
    print("  若一选就塌回 ~38%，则该子群是**结果条件**的，66.9% 不可交易。")
    print()
    elig = [r for r in known if r["close_asof"] and r["hi"] and r["lo"]
            and r["lo"] > 0]
    for label, key in (("min(上/下)距离 最小 10%", 0.10),
                       ("最小 20%", 0.20),
                       ("最小 33%", 0.33),
                       ("最宽 20%（反向对照）", -0.20)):
        vals = sorted(
            ((min((r["hi"] - r["close_asof"]) / r["close_asof"],
                  (r["close_asof"] - r["lo"]) / r["close_asof"]), i, r)
             for i, r in enumerate(elig)),
            key=lambda t: (t[0], t[1]))
        k = int(len(vals) * abs(key))
        sub = [t[2] for t in (vals[:k] if key > 0 else vals[-k:])]
        acc = mean(r["hit_direction"] for r in sub)
        sub_hit = mean(1.0 if r["invalidated"] == 1 else 0.0 for r in sub)
        # 子群里「真的命中 invalidate_if」的那部分有多准（对照）
        real = [r for r in sub if r["invalidated"] == 1]
        print(f"  {label:22s} n={len(sub):5d}  模型准确率 {pct(acc):>7s}"
              f"  实际命中率 {pct(sub_hit):>7s}"
              f"  其中真命中 {len(real):4d}（准确率 {pct(mean(r['hit_direction'] for r in real)):>7s}）")
    print()
    print("  同上，但按「距离 / 模型 sigma」标准化（sigma 从 notes 取不到则跳过）：")
    sig = [(r, r["notes"].get("sigma")) for r in elig]
    sig = [(r, s) for r, s in sig if s]
    if sig:
        print(f"    可取到 sigma 的行 n={len(sig)}")
    else:
        print("    notes 里没有 sigma 字段 —— 跳过（诊断不到就说不，不猜）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
