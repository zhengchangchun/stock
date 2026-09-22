"""通路 B（人工 → 镜像）：人工真实操作 → 镜像复刻 → B1 预测。

## 真源只有一个：`real_trades` + `cash_flows`

§6.3 的归属规则：人工操作**继续录在既有实盘账本里**（`portfolio/` 的 CLI），
通路 B 读的就是它。不再开第二套「人工录入表」—— 两份人工流水必然走样，
而「镜像的是哪一份」会变成一句口头约定（D-25 的口径唯一）。

## 镜像 = **既有 `arm-now`**（D-25）

不是「照着 `arm-now` 写的那一份」，而是它本身：净值由
`paper/engine.py::step_account` 落盘，走的是与 `paper step` **同一个**
`_step_all`。所以「同一份人工流水 ⇒ 镜像账户当日的 `paper_nav_daily` 与
`arm-now` 逐字段相同」这条判据**结构上成立**，不是靠两边算得一样。

## 与 B1 的先后（为什么先算预测、再落净值）

若先落净值再跑 B1，B1 一越界就会留下「净值有了、预测没有」的半截日。
所以顺序是：**先跑 B1 并把载荷拿在手里**（此时一个字节都没写），
净值落盘之后才把预测与运行台账追加进库。任一步拒绝 ⇒ 当天零写入。

## 幂等

幂等单元 = `(账户, asof)`，判据是 `m2_channel_runs` 里那一行 `status='ran'`
（与通路 A 同一条）。命中就整个跳过，**一个字节都不写**。
"""

from __future__ import annotations

import sqlite3

from stocklab.m2 import config
from stocklab.m2 import context as ctx_mod
from stocklab.m2 import plugin_hooks
from stocklab.m2 import store as m2_store
from stocklab.m2.channel_a import REJECTED
from stocklab.paper import engine
from stocklab.paper import store as paper_store
from stocklab.paper.rules import check_no_lookahead


def _execute(conn: sqlite3.Connection, *, asof: str, now: str,
             prices: dict | None) -> dict:
    """跑通路 B 的一天。**先算预测、再落净值**（见模块 docstring）。"""
    account_id = config.MIRROR_ACCOUNT
    account = next((a for a in paper_store.load_accounts(conn)
                    if a["account_id"] == account_id), None)
    if account is None:
        raise config.ChannelReject(
            "account", f"镜像账户 {account_id} 不存在 —— 先跑 `paper init`")

    # 幂等单元与通路 A 不同：`arm-now` 的净值**本来**就归 `paper step` 管，
    # 所以这里的「已存在」不是冲突，而是「今天已经镜像过了」。
    already = paper_store.nav_exists(conn, account_id, asof)

    led = engine.ledger_state(conn, asof)
    positions = dict(led["positions"])
    codes = set(positions)
    marks = dict(prices) if prices is not None else engine.resolve_marks(
        conn, codes, asof)
    check_no_lookahead(asof, marks)
    missing_marks = sorted(c for c in positions if c not in marks)
    if missing_marks:
        raise config.ChannelSkip(
            config.SKIP_NO_BARS,
            f"{asof} 取不到人工持仓 {missing_marks} 的收盘价 → 镜像净值不可判定。"
            f"**跳过这一天并留痕**（不用成本价冒充现价）")
    missing_bars = ctx_mod.require_daily_bars(conn, positions, asof=asof)
    if missing_bars:
        raise config.ChannelSkip(
            config.SKIP_NO_BARS,
            f"{asof} 缺当日（不复权）K 线：{missing_bars} → 跳过并留痕"
            f"（拿前值顶上去会让镜像曲线在缺口处凭空延续）")

    total = round(float(led["cash"])
                  + sum(float(marks[c].price) * int(q) for c, q in positions.items()), 4)
    cost_prices = ctx_mod.cost_prices_of(account)
    ctx = ctx_mod.channel_ctx(
        conn, channel=config.CHANNEL_B, account_id=account_id, asof=asof,
        cash=float(led["cash"]), positions=positions, marks=marks,
        total_assets=total, cost_prices=cost_prices)

    # ---- B1：逐个人工持仓标的的收益预测（先拿在手里，不落库）----
    forecasts: list[tuple[str, dict, dict]] = []
    fp: dict | None = None
    for code in sorted(positions):
        fctx = {**ctx, "focus": {"code": code, "qty": int(positions[code])}}
        payload, fp = plugin_hooks.call_forecast(conn, config.PLUGIN_B1, fctx)
        forecasts.append((code, payload, fctx))
    if fp is None:                        # 空仓：没有可预测的持仓
        fp = plugin_hooks.script_fingerprint(conn, config.PLUGIN_B1)

    # ---- 镜像复刻：既有 `arm-now` 的实现（D-25）----
    step = engine.step_account(conn, account_id, asof, now=now, prices=prices)
    nav_row = paper_store.latest_nav(conn, account_id, asof=asof)
    if nav_row is None or str(nav_row["date"]) != asof:
        raise config.ChannelReject(
            "nav", f"{account_id} 在 {asof} 没有净值行（`step_account` 未产出）—— "
                   f"拒绝写台账：台账说『ran』而净值表里没有那一行，"
                   f"是所有下游读数的地基塌掉")

    for code, payload, fctx in forecasts:
        m2_store.insert_forecast(
            conn, plugin_id=config.PLUGIN_B1, channel=config.CHANNEL_B,
            account_id=account_id, asof=asof, code=code, payload=payload,
            script_id=fp["script_id"], script_version=fp["version"],
            input_sha256=ctx_mod.ctx_sha256(fctx), now=now)
    m2_store.insert_run(
        conn, channel=config.CHANNEL_B, account_id=account_id, asof=asof,
        status=config.STATUS_RAN,
        reason=(f"通路B：人工流水 {len(led['trades'])} 笔复刻成镜像净值 "
                f"{float(nav_row['nav']):,.2f}（持仓 {len(positions)} 只，"
                f"净值行{'已存在' if already else '本次写入'}）；"
                f"B1({fp['version']}) 预测 {len(forecasts)} 条"),
        plugins={config.PLUGIN_B1: fp},
        n_orders=0, now=now,
        detail={"nav": float(nav_row["nav"]), "cash": float(nav_row["cash"]),
                "positions": {c: int(q) for c, q in sorted(positions.items())},
                "n_real_trades": len(led["trades"]),
                "net_deposits": float(led["net_deposits"]),
                "wrote_nav": bool(step["wrote_nav"]),
                "ctx_sha256": ctx_mod.ctx_sha256(ctx)})
    return {"status": config.STATUS_RAN, "nav": float(nav_row["nav"]),
            "cash": float(nav_row["cash"]),
            "positions": {c: int(q) for c, q in sorted(positions.items())},
            "n_forecasts": len(forecasts), "wrote_nav": bool(step["wrote_nav"]),
            "n_real_trades": len(led["trades"]),
            "plugin_versions": {"m2_b1": fp["version"]}}


def run(conn: sqlite3.Connection, *, asof: str, now: str,
        prices: dict | None = None) -> dict:
    """跑通路 B 的一天。与通路 A 同款：**不抛业务异常**，如实返回并留痕。"""
    account_id = config.MIRROR_ACCOUNT
    done = m2_store.ran_run(conn, config.CHANNEL_B, account_id, asof)
    if done is not None:
        return {"status": config.STATUS_ALREADY, "account_id": account_id,
                "asof": asof, "run_id": int(done["run_id"]),
                "note": "同 (通路, 账户, 日) 已经跑过 → **一个字节都不写**"}
    try:
        out = _execute(conn, asof=asof, now=now, prices=prices)
    except config.ChannelSkip as exc:
        run_id = m2_store.insert_run(
            conn, channel=config.CHANNEL_B, account_id=account_id, asof=asof,
            status=config.STATUS_SKIPPED, reason=f"[{exc.code}] {exc.reason}",
            plugins={}, n_orders=0, now=now)
        return {"status": config.STATUS_SKIPPED, "account_id": account_id,
                "asof": asof, "run_id": run_id, "skip_code": exc.code,
                "reason": exc.reason}
    except REJECTED as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        code = getattr(exc, "code", None) or type(exc).__name__
        run_id = m2_store.insert_run(
            conn, channel=config.CHANNEL_B, account_id=account_id, asof=asof,
            status=config.STATUS_REJECTED, reason=f"[{code}] {reason}",
            plugins={}, n_orders=0, now=now)
        return {"status": config.STATUS_REJECTED, "account_id": account_id,
                "asof": asof, "run_id": run_id, "reject_code": str(code),
                "reason": str(reason)}
    out.update({"account_id": account_id, "asof": asof})
    return out
