#!/usr/bin/env python
"""回放成本口径的验收探针（ADR-017 D-13 / D-14）。

**只读、离线**：只连一个已有的库、不抓网、不写库。用于核对设计稿
`docs/superpowers/specs/2026-09-21-回放成本口径-仓位假设-design.md` §5 的采信条件。

它回答三件事：

1. **三池分解**：毛超额 / 费用拖累 / 滑点拖累 / 净超额（每调仓周期，相对 `sh000300`）
   —— 用于和设计 §2 记录的「旧口径」数字对照，看结论是否翻转。
2. **§5.1 正向**：`POSITION_NOTIONAL` ∈ {2万, 5万, 10万, 50万} 四档两两等价
   （逐期 < 1e-7、均值 < 1e-6）。池成员只算一次（测试接缝注入），只有成本口径在变。
3. **§5.2 反向证伪**：N = 1 万必须与 N = 10 万**不同**，且 1 万档成本更高
   （1万 × 0.025% = 2.5 < 5 → 最低佣金必生效）。**若相同 → 推导错了 → 停。**

用法:
    .venv/bin/python scripts/probe_replay_cost_basis.py --db /tmp/replay.db
    .venv/bin/python scripts/probe_replay_cost_basis.py --db /tmp/replay.db --pool short
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.candidate import replay                       # noqa: E402
from stocklab.config.costs import CostModel                 # noqa: E402

DEFAULT_START = "2015-01-01"
DEFAULT_END = "2026-09-18"
POOLS = ("short", "mid", "long")
NOTIONALS = (20_000.0, 50_000.0, 100_000.0, 500_000.0)


def _install_score_cache() -> dict:
    """给 `score_pipeline` 加按 `(asof, overrides)` 的记忆层，**只影响本进程**。

    为何需要：`benchmark_excess` / `period_returns` 每遇到一个调仓日就重算一次
    池成员（实测单次 221ms）。本探针要跑 3 池 × 3 套成本口径 + 6 次 N 档回放，
    不缓存就是 **~2700 次**重算（>10 分钟）——而池成员在成本口径之间**完全相同**，
    重算纯属浪费。缓存后唯一调用降到 712 次（三池调仓日总数）。

    不改任何口径：缓存键含 `asof` 与 `plugin_overrides`，返回值直接用原函数结果。
    """
    import stocklab.candidate.run as run_mod
    real = run_mod.score_pipeline
    cache: dict = {}

    def memo(conn, *, asof, plugin_overrides=None):
        key = (asof, tuple(sorted((plugin_overrides or {}).items())))
        if key not in cache:
            cache[key] = real(conn, asof=asof, plugin_overrides=plugin_overrides)
        return cache[key]

    # replay 内部是「调用时 import」，所以改模块属性即可生效
    run_mod.score_pipeline = memo
    return cache


def _zero_model(**kw) -> CostModel:
    base = dict(commission_rate=0.0, min_commission=0.0, transfer_fee_rate=0.0,
                stamp_tax_rate=0.0, slippage_bps=0.0)
    base.update(kw)
    return CostModel(**base)


def _pct(x: float) -> str:
    return f"{x * 100:+.4f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/stocklab.db")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--pool", default="short",
                    help="跑 N 敏感性的池（默认 short；mid/long 周期少，更快）")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"❌ 库不存在：{args.db}")
        return 1
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    gross_m = _zero_model()                       # 零成本 → 纯选股的超额
    fee_only_m = CostModel(slippage_bps=0.0)      # 只扣费用（真实费率），不扣滑点
    slip_only_m = CostModel(commission_rate=0.0, min_commission=0.0,  # 只扣滑点
                            transfer_fee_rate=0.0, stamp_tax_rate=0.0)
    full_m = CostModel()                          # 费用 + 滑点（线上口径）

    print(f"库：{args.db}   窗口：{args.start} → {args.end}")
    print(f"POSITION_NOTIONAL = {replay.POSITION_NOTIONAL:,.0f} 元\n")
    cache = _install_score_cache()
    t_all = time.time()

    print("=" * 78)
    print("① 三池分解（相对 sh000300，每调仓周期）")
    print("=" * 78)
    print(f"{'池':6s} {'周期数':>6s} {'毛超额':>11s} {'费用拖累':>11s}"
          f" {'滑点拖累':>11s} {'净超额':>11s}")
    marks_by_pool: dict[str, list[str]] = {}
    for pool in POOLS:
        t0 = time.time()
        marks = replay.rebalance_marks(conn, pool=pool, start=args.start,
                                       end=args.end)
        marks_by_pool[pool] = marks
        gross = replay.benchmark_excess(conn, asof_dates=marks, pool=pool,
                                        costs=gross_m)
        fee_only = replay.benchmark_excess(conn, asof_dates=marks, pool=pool,
                                           costs=fee_only_m)
        slip_only = replay.benchmark_excess(conn, asof_dates=marks, pool=pool,
                                            costs=slip_only_m)
        net = replay.benchmark_excess(conn, asof_dates=marks, pool=pool,
                                      costs=full_m)
        print(f"{pool:6s} {len(marks) - 1:6d} {_pct(gross):>11s}"
              f" {_pct(gross - fee_only):>11s} {_pct(gross - slip_only):>11s}"
              f" {_pct(net):>11s}   ({time.time() - t0:.1f}s)")

    pool = args.pool
    marks = marks_by_pool[pool]
    print(f"\n{'=' * 78}")
    print(f"② §5.1 正向采信：N 不敏感（池={pool}，{len(marks) - 1} 个周期）")
    print("=" * 78)
    # 池成员只算一次（成本口径之间池成员**相同**）→ 测试接缝注入
    from stocklab.candidate.run import score_pipeline       # noqa: E402
    t0 = time.time()
    members = {d: sorted(m.code for m in score_pipeline(conn, asof=d).members
                         if m.pool == pool)
               for d in marks}
    print(f"（池成员现算完成：{len(marks)} 个调仓日，{time.time() - t0:.1f}s；"
          f"缓存键 {len(cache)} 个）")

    series: dict[float, list[float]] = {}
    below: dict[float, int] = {}
    for notional in NOTIONALS:
        replay.POSITION_NOTIONAL = notional          # 探针：显式改口径再跑
        series[notional] = replay.period_returns(
            conn, asof_dates=marks, pool=pool, costs=full_m,
            _pools_for_test=members)
        # 诊断：该档有多少个「标的 × 调仓日」的实际名义额 < 2 万
        # （整股截断把名义额压到最低佣金门槛以下 → 该档**不在**常数区间）
        n_below = 0
        for d in marks:
            for code in members.get(d, []):
                px = replay._close_on(conn, code, d)
                if px and px * int(notional / px) < 20_000.0:
                    n_below += 1
        below[notional] = n_below

    print("\n  实际名义额 < 2 万（最低佣金生效）的「标的×调仓日」计数：")
    for notional in NOTIONALS:
        print(f"    N={notional:>9,.0f}元: {below[notional]:>5d} 处")

    ok = True
    for i, a in enumerate(NOTIONALS):
        for b in NOTIONALS[i + 1:]:
            per = max(abs(x - y) for x, y in zip(series[a], series[b]))
            mean = abs(sum(series[a]) / len(series[a])
                       - sum(series[b]) / len(series[b]))
            good = per < 1e-7 and mean < 1e-6
            ok &= good
            print(f"  N={a:>9,.0f} vs N={b:>9,.0f}: 逐期 max|Δ|={per:.3e}"
                  f"  均值|Δ|={mean:.3e}  {'✅' if good else '❌'}")

    print(f"\n{'=' * 78}")
    print(f"③ §5.2 反向证伪：N=1万 必须与 N=10万 不同，且 1 万档成本更高")
    print("=" * 78)
    out: dict[float, list[float]] = {}
    for notional in (10_000.0, 100_000.0):
        replay.POSITION_NOTIONAL = notional
        out[notional] = replay.period_returns(
            conn, asof_dates=marks, pool=pool, costs=full_m,
            _pools_for_test=members)
    m_small = sum(out[10_000.0]) / len(out[10_000.0])
    m_big = sum(out[100_000.0]) / len(out[100_000.0])
    different = abs(m_small - m_big) > 1e-7
    costlier = m_small < m_big
    print(f"  1 万档 均值净收益 = {_pct(m_small)}")
    print(f"  10 万档 均值净收益 = {_pct(m_big)}")
    print(f"  两者不同：{'✅' if different else '❌ 最低佣金没生效 —— 推导错了，停下来重查'}")
    print(f"  1 万档成本更高：{'✅' if costlier else '❌'}")

    print(f"\n{'=' * 78}")
    print("结论")
    print("=" * 78)
    print(f"  §5.1 N 不敏感：{'✅ 采信' if ok else '❌ 不采信'}")
    print(f"  §5.2 反向证伪：{'✅ 通过' if (different and costlier) else '❌ 不通过'}")
    print(f"  总耗时 {time.time() - t_all:.0f}s")
    return 0 if (ok and different and costlier) else 2


if __name__ == "__main__":
    raise SystemExit(main())
