"""`ops patrol` 的**业务巡检块**（P92 / B5）：把「30 分钟该做的业务读数」搬进巡检。

## 这块解决什么

需求原文（`stock/07_定时任务调度需求.md` §任务1）里，每 30 分钟的巡检有 7 件事；
P38 把其中的「数据链体检」搬进了 `ops/patrol.py`，剩下的**业务**部分一直没落点：

| 需求 | 本模块 |
|---|---|
| ② 插桩0 重校验风险 | `risk_screen` —— 池内成员逐只跑 `score.industry_screen` |
| ⑤ 熔断检测 | `fuse` —— **只读**复用 `m2.selfeval.fuse_verdict` |
| ⑥ 更新候选池标的状态 | `candidate_status` —— 默认只算不写，`--update-status` 才调 P91 的 CLI |
| ① 北向 / 公告的新鲜度 | `freshness` —— 只看两张 P88 表的 `max(日期)/行数`（**采集**属 P88） |

④（A3/B1 刷新收益概率）**刻意不做**：盘中 `bars` 未定型（P46 口径＝当天
`fetched_at ≥ 15:00` 才算定型），30 分钟重复跑会拿半天数据反复写 append-only 台账，
并与 `m2_daily` 的收盘口径打架（任务书 D6）。

## 五条纪律

1. **只读**：四个子块都不写业务表。唯一例外是 `--update-status` 打开时的
   `candidate_status_events` 追加 —— 而那也不是本模块写的，是**子进程**调
   `candidate status set`（本仓 `ops` 纪律：补步一律子进程复用现成 CLI）。
2. **熔断绝不调 `m2 cycle fuse-check`**：那条命令往 append-only 台账追加
   `circuit_breaker` + `validation_end` 两行 —— 30 分钟一轮会把台账写花。
   只读路径＝「未收尾周期」＋ `fuse_verdict`（**复用**，不重写回撤算法）。
3. **只有熔断计入退出码**（`tripped=True` ⇒ `verdict()` 给 1）。其余三块只展示：
   盘中每 30 分钟报一次假警会把巡检变成噪音。
4. **判不了就说判不了**：插桩0 没有 active 版本、净值窗口为空 ⇒ `unknown` /
   `reason` 里写清「判不了」，**不**给 `ok`（ERROR_DIARY #36 的同一条）。
5. **不阻断**：业务块里任何一条读数炸了（沙盒抛错、表未前滚），记进 `reason`／
   返回 `unknown` —— 巡检是闹钟，不能被某个读数带停。

## 为什么 `risk_screen` 复用 `candidate/run.py` 的**私有**助手

口径必须与 `candidate run` 第 4 步逐位相同（PIT 复权 bars + 同一横截面），
而那份口径就写在 `_load_bars_adjusted` / `_cross_section_map` 里。
在两处各抄一份 = 迟早分叉（而分叉的后果是「巡检说干净、主流程却拦下了」）。
所以这里**只 import、不重写**：它们是同一份实现，本模块给它们第二组调用点。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from stocklab.candidate import pools as cand_pools
from stocklab.candidate import run as cand_run
from stocklab.candidate import score as cand_score
from stocklab.candidate import snapshot as cand_snapshot
from stocklab.candidate import status as cand_status
from stocklab.config.universe import Instrument
from stocklab.m2 import selfeval
from stocklab.m2.cycle import KIND_VALIDATION_END
from stocklab.ops import journal, patrol
from stocklab.ops.runner import DEFAULT_TIMEOUT_S, Step, build_argv, default_runner
from stocklab.paper import engine as paper_engine
from stocklab.paper import store as paper_store
from stocklab.store import validation as ledger

#: 四个子块的固定顺序与键集（D1：**只增不减**；页面与简报都按它读）。
BLOCK_ORDER: tuple[str, ...] = ("risk_screen", "fuse", "candidate_status", "freshness")

#: 状态推导的**白名单只有一条**（D3）：池内成员若被**任一 live 账户实际持有**，
#: 目标状态 = 已建仓。其余三个状态（观察中 / 等待买点 / 逻辑证伪移出）**不可自动推导**。
DERIVED_STATUS = "已建仓"


# ---------- 快照读取 ----------

def latest_snapshot(conn: sqlite3.Connection) -> list[dict]:
    """最新一条候选池快照的**成员**（按 `created_at` 最新 —— 与页面同一把尺子）。

    没有快照 / 老库未前滚 / 快照行读不出来 ⇒ `[]`（调用方把「没有成员」说成人话）。
    """
    try:
        keys = cand_snapshot.list_snapshot_keys(conn)
    except sqlite3.Error:
        return []
    if not keys:
        return []
    asof, run_kind = keys[0]
    sid = cand_snapshot.find_snapshot(conn, asof=asof, run_kind=run_kind)
    if sid is None:
        return []
    try:
        return list(cand_snapshot.load_snapshot(conn, sid)["members"])
    except (LookupError, sqlite3.Error):
        return []


def _codes(members) -> list[str]:
    """成员 → **去重保序**的代码表（同一只可能在多个池里）。"""
    out: list[str] = []
    for m in members or ():
        code = str(m["code"] if isinstance(m, dict) else m.code)
        if code not in out:
            out.append(code)
    return out


# ---------- ② 插桩0 排雷 ----------

def _instrument(conn: sqlite3.Connection, code: str) -> Instrument | None:
    row = conn.execute(
        "SELECT code, name, market, board, type FROM instruments WHERE code = ?",
        (code,)).fetchone()
    if row is None:
        return None
    return Instrument(code=str(row["code"]), name=str(row["name"]),
                      market=str(row["market"]), board=str(row["board"]),
                      asset_type=str(row["type"]))


def _screen_one(conn, code: str, *, asof: str, xsec: dict) -> str | None:
    """排雷一只标的：`None` = 干净；否则是风险注记。**只读**。

    与 `candidate run` 第 4 步同一口径：因子侧吃 **PIT 复权** bars、
    横截面用**同一个** `xsec`、池口径取 `POOL_SHORT`（第 4 步就是这么造的 ctx）。
    """
    inst = _instrument(conn, code)
    if inst is None:
        raise LookupError(f"{code} 不在 instruments 表里 —— 排雷要先有标的口径")
    raw = cand_run._load_bars(conn, code, asof=asof)
    bars, _fallback = cand_run._load_bars_adjusted(conn, code, asof=asof, raw=raw)
    ctx = cand_score.build_ctx(conn, inst, cand_pools.POOL_SHORT, bars,
                               asof=asof, cross_section=xsec)
    out = cand_score.industry_screen(conn, inst, ctx)
    if out["pass_flag"]:
        return None
    return "; ".join(out["risk_note"]) or "行业排雷未通过"


def risk_screen(conn: sqlite3.Connection, *, asof: str | None, members,
                screen_fn=None) -> dict:
    """② 插桩0 重校验风险。**只展示**（D2：不计异常）。

    `screen_fn` 是注入点（测试用假排雷器）；默认走真插桩0。
    """
    codes = _codes(members)
    base = {"n_members": len(codes), "n_checked": 0, "n_risky": 0, "risky": []}
    if not codes:
        return {**base, "status": patrol.SKIPPED,
                "reason": "候选池还没有成员（先跑 candidate run 出快照）—— 本轮不排雷"}
    if not asof:
        return {**base, "status": patrol.SKIPPED,
                "reason": "最新已收盘交易日判不出 —— 排雷要一个 PIT 截止日，不猜"}
    fn = screen_fn or _screen_one
    try:
        xsec = cand_run._cross_section_map(conn, asof=asof)
        risky: list[dict] = []
        for code in codes:
            note = fn(conn, code, asof=asof, xsec=xsec)
            if note:
                risky.append({"code": code, "note": str(note)})
    except Exception as exc:                # noqa: BLE001 —— 纪律 5：巡检不被读数带停
        return {**base, "status": patrol.UNKNOWN,
                "reason": (f"插桩0 排雷跑不了（{type(exc).__name__}: {exc}）—— "
                           "判不了，**不**当成「都干净」")}
    n = len(codes)
    if risky:
        return {**base, "n_checked": n, "n_risky": len(risky), "risky": risky,
                "status": patrol.RISKY,
                "reason": (f"插桩0 排雷命中 {len(risky)}/{n} 只（asof {asof}）："
                           + "；".join(f"{r['code']} {r['note']}" for r in risky))}
    return {**base, "n_checked": n, "status": patrol.OK,
            "reason": f"插桩0 排雷 {n}/{n} 只全部通过（asof {asof}）"}


# ---------- ⑤ 熔断（**只读**） ----------

def fuse_block(conn: sqlite3.Connection, *, asof: str | None) -> dict:
    """⑤ 熔断读数。**绝不调 `m2 cycle fuse-check`**（它写 append-only 台账）。

    只读路径与那条命令**共用**同一份判定：`m2.selfeval.fuse_verdict`，
    窗口＝周期 `start_date` 起的净值行（同 `fuse_check` 的取数）。
    """
    base = {"cycle_id": None, "at_value": None, "threshold": None,
            "tripped": False, "window": None}
    try:
        cycles = ledger.list_cycles(conn)
    except sqlite3.Error:
        return {**base, "status": patrol.SKIPPED,
                "reason": "validation_cycles 表不存在（老库未前滚）—— 本轮跳过，不计异常"}
    active = None
    for cycle in cycles:                    # 有多个未收尾周期时取**最后一条**
        if ledger.find_event(conn, int(cycle["cycle_id"]),
                             KIND_VALIDATION_END) is None:
            active = cycle
    if active is None:
        return {**base, "status": patrol.SKIPPED,
                "reason": (f"没有进行中的验证周期（{len(cycles)} 条周期都已收尾）"
                           "—— 熔断检查本轮跳过，不计异常")}
    cid = int(active["cycle_id"])
    if not asof:
        return {**base, "cycle_id": cid, "status": patrol.SKIPPED,
                "reason": "最新已收盘交易日判不出 —— 熔断窗口算不出来，不猜"}
    rows = [r for r in paper_store.load_nav(conn, str(active["account_id"]),
                                            asof=str(asof))
            if str(r["date"]) >= str(active["start_date"])]
    v = selfeval.fuse_verdict(rows)
    tripped = bool(v["tripped"])
    window = (f"{v['window_start']} ~ {v['window_end']}"
              if v.get("window_start") else None)
    return {
        "status": patrol.TRIPPED if tripped else patrol.OK,
        "cycle_id": cid, "at_value": v["at_value"], "threshold": v["threshold"],
        "tripped": tripped, "window": window,
        "reason": (f"周期 {cid}（账户 {active['account_id']}，"
                   f"起 {active['start_date']}）：{v['reason']}"),
    }


# ---------- ⑥ 候选池标的状态（默认只算不写） ----------

def _live_holdings(conn: sqlite3.Connection, *, asof: str | None
                   ) -> tuple[dict[str, list[str]], list[str]]:
    """`(code → 持有它的 live 账户, 读不了的账户)`。**只读**。

    「live」走 `paper.engine.live_of`（**同一把尺子**：`params.live` 缺省 true），
    持仓取每账户**最新净值行**的 `positions_json` 里 `qty > 0` 的标的。
    """
    holds: dict[str, list[str]] = {}
    bad: list[str] = []
    try:
        accounts = paper_store.load_accounts(conn)
    except sqlite3.Error:
        return holds, bad
    for acc in accounts:
        account_id = str(acc["account_id"])
        try:
            params = json.loads(acc["params_json"] or "{}")
            live = paper_engine.live_of(params)
        except (ValueError, TypeError, AttributeError) as exc:
            bad.append(f"{account_id}（params 读不了：{exc}）")
            continue
        if not live:
            continue
        nav = paper_store.latest_nav(conn, account_id,
                                     asof=str(asof) if asof else None)
        if nav is None:
            continue
        try:
            positions = json.loads(nav["positions_json"] or "[]")
        except ValueError as exc:
            bad.append(f"{account_id}（净值行读不了：{exc}）")
            continue
        for p in positions:
            if int(p.get("qty") or 0) > 0:
                holds.setdefault(str(p["code"]), []).append(account_id)
    return holds, bad


def _status_step(code: str, asof: str, reason: str) -> Step:
    """一条状态写入步 = 逐字一条 P91 的 CLI 命令（**不 import 业务函数**）。"""
    return Step(
        name="candidate_status_set",
        args=("candidate", "status", "set", "--code", code, "--asof", asof,
              "--status", DERIVED_STATUS, "--actor", "patrol", "--reason", reason),
        supports_db=True,
        why="P91 的状态写入口：append-only、同 (code,asof,status) 幂等零写入")


def _apply_status(would_set: list[dict], *, db_path, runner, timeout_s) -> list[dict]:
    """逐条起子进程写状态。**失败照实记，不改退出码**（D2：只有熔断进退出码）。"""
    if db_path is None:
        return [{"code": w["code"], "asof": w["asof"], "status": w["status"],
                 "exit_code": None, "detail": "没有 db_path —— 拒绝起子进程"}
                for w in would_set]
    run = runner or default_runner
    out: list[dict] = []
    for w in would_set:
        step = _status_step(w["code"], w["asof"], w["reason"])
        res = run(step, build_argv(step, db_path=Path(db_path)), timeout_s)
        out.append({
            "code": w["code"], "asof": w["asof"], "status": w["status"],
            "exit_code": res.get("exit_code"),
            "detail": (res.get("stderr_tail") or res.get("stdout_tail")
                       or res.get("error") or "")[-200:],
        })
    return out


def candidate_status_block(conn: sqlite3.Connection, *, asof: str | None, members,
                           update_status: bool = False, db_path=None, runner=None,
                           timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """⑥ 候选池标的状态。默认 **dry-read**（只列出「打开开关会写哪几条」）。

    `status` 取 `ok`（有差异）/ `skipped`（无差异）—— 两个都**不计异常**。
    """
    codes = _codes(members)
    base = {"counts": {}, "would_set": [], "applied": []}
    if not codes:
        return {**base, "status": patrol.SKIPPED,
                "reason": "候选池还没有成员（先跑 candidate run 出快照）—— 状态无从推导"}
    if not asof:
        return {**base, "status": patrol.SKIPPED,
                "reason": "最新已收盘交易日判不出 —— 状态是 PIT 的，不猜一个日子去写"}
    current = cand_status.status_map(conn, codes, asof=str(asof))
    counts = {s: 0 for s in cand_snapshot.STATUSES}
    for value in current.values():
        counts[value] = counts.get(value, 0) + 1
    holds, bad = _live_holdings(conn, asof=asof)
    would_set = [
        {"code": code, "status": DERIVED_STATUS, "asof": str(asof),
         "reason": f"被 live 账户实际持有：{','.join(sorted(holds[code]))}"}
        for code in codes
        if holds.get(code) and current[code] != DERIVED_STATUS
    ]
    applied = (_apply_status(would_set, db_path=db_path, runner=runner,
                             timeout_s=timeout_s) if update_status else [])
    tail = f"；另有 {len(bad)} 条账户读物读不了：{bad}" if bad else ""
    if not would_set:
        return {**base, "counts": counts, "status": patrol.SKIPPED,
                "reason": (f"盘内 {len(codes)} 只成员的状态与 live 账户实际持仓一致"
                           f"（无差异）{tail}")}
    if update_status:
        ok = sum(1 for a in applied if a["exit_code"] == 0)
        how = (f"--update-status 已开：{ok}/{len(applied)} 条走子进程写入成功"
               "（重复的三元组由 P91 的表兜底成 already）")
    else:
        how = (f"待写 {len(would_set)} 条；本轮**只算不写**（dry-read，"
               "加 --update-status 才写）")
    return {**base, "counts": counts, "would_set": would_set, "applied": applied,
            "status": patrol.OK,
            "reason": (f"{len(would_set)} 只成员被 live 账户实际持有但状态不是"
                       f"「{DERIVED_STATUS}」 ⇒ {how}{tail}")}


# ---------- ① 新鲜度（两张 P88 表） ----------

#: (输出键, 表, 日期列, 人话标签, 采集节奏)。节奏写进 reason：北向**本来就是季度频率**，
#: 拿它当 30 分钟的 SLA 会天天误报（P67 T2 资金流那次的同款教训）。
_FRESHNESS_TABLES = (
    ("announcements", "announcements", "notice_date", "公告", "月链才采，空是预期"),
    ("northbound", "northbound_holdings", "trade_date", "北向持股",
     "个股北向已是季度频率"),
)


def _table_freshness(conn, table: str, col: str, label: str, cadence: str,
                     *, asof: str | None) -> dict:
    rows = patrol._one(conn, f"SELECT COUNT(*) FROM {table}", default=None)
    if rows is None:
        return {"rows": None, "max_date": None, "status": patrol.SKIPPED,
                "reason": f"{label} 表不存在（老库未前滚）"}
    rows = int(rows)
    if not rows:
        return {"rows": 0, "max_date": None, "status": patrol.SKIPPED,
                "reason": f"{label} 表是空的（{cadence}）"}
    latest = patrol._one(conn, f"SELECT MAX({col}) FROM {table}", default=None)
    behind = not (asof and latest and str(latest) >= str(asof))
    return {"rows": rows, "max_date": latest,
            "status": patrol.STALE if behind else patrol.OK,
            "reason": (f"{label} {rows} 行，最新 {latest}（{cadence}；对比 {asof}："
                       + ("落后" if behind else "跟到最新") + "）")}


def freshness_block(conn: sqlite3.Connection, *, asof: str | None) -> dict:
    """① 北向 / 公告的**新鲜度**（采集本身属 P88，本档只读读数）。**只展示**。"""
    sub = {key: _table_freshness(conn, table, col, label, cadence, asof=asof)
           for key, table, col, label, cadence in _FRESHNESS_TABLES}
    blocks = list(sub.values())
    if all(b["status"] == patrol.SKIPPED for b in blocks):
        status = patrol.SKIPPED
    elif any(b["status"] == patrol.STALE for b in blocks):
        status = patrol.STALE
    else:
        status = patrol.OK
    return {
        "status": status,
        "announcements": sub["announcements"],
        "northbound": sub["northbound"],
        "reason": ("；".join(b["reason"] for b in blocks)
                   + " —— 新鲜度只展示、不计异常（D2：只有熔断进退出码）"),
    }


# ---------- 组装 ----------

def build_business(conn: sqlite3.Connection, *, latest: str | None, db_path=None,
                   update_status: bool = False, runner=None, screen_fn=None,
                   timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """`check_chain()` 的 `business` 顶层键：四个子块，形状固定（D1）。"""
    members = latest_snapshot(conn)
    return {
        "risk_screen": risk_screen(conn, asof=latest, members=members,
                                   screen_fn=screen_fn),
        "fuse": fuse_block(conn, asof=latest),
        "candidate_status": candidate_status_block(
            conn, asof=latest, members=members, update_status=update_status,
            db_path=db_path, runner=runner, timeout_s=timeout_s),
        "freshness": freshness_block(conn, asof=latest),
    }


# ---------- ⑦ 巡检简报（**产物不阻断**） ----------

def _anomaly_line(anomalies: list[dict]) -> str:
    if not anomalies:
        return "无"
    return f"{len(anomalies)} 项（" + "、".join(
        str(a.get("kind")) for a in anomalies) + "）"


def _next_hint(payload: dict) -> str:
    """下一轮建议**一句** —— 只由本轮 payload 派生，不现场查库。"""
    biz = payload.get("business") or {}
    if (biz.get("fuse") or {}).get("tripped"):
        return "熔断已触发：本轮验证应终止，人工确认后走 m2 cycle end"
    if payload.get("exit_code") == 2:
        return "断链 / 判不了优先：先让它恢复可判，业务读数这轮不作数"
    anomalies = payload.get("anomalies") or []
    if anomalies:
        return f"先处理上面 {len(anomalies)} 项异常（业务块只展示、不计异常）"
    cs = biz.get("candidate_status") or {}
    if cs.get("would_set"):
        return (f"{len(cs['would_set'])} 只成员待写「{DERIVED_STATUS}」——"
                "确认持仓无误后加 --update-status（默认只算不写）")
    return "无需动作"


def brief_text(payload: dict) -> str:
    """一轮巡检 → **一节** markdown。只读 `payload`，不碰库（D4）。"""
    biz = payload.get("business") or {}
    anomalies = payload.get("anomalies") or []
    plan = payload.get("plan") or {}
    steps = [s.get("name") for s in (payload.get("steps") or [])]
    skip = [s.get("step") for s in (plan.get("skipped") or [])]
    exit_code = payload.get("exit_code")
    lines = [
        f"# {payload.get('today')} 巡检",
        "",
        f"- 时刻：{payload.get('now')}",
        f"- 交易日：{payload.get('today')}"
        f"（{(payload.get('session_day') or {}).get('why') or '未判'}）",
        f"- 最新已收盘交易日：{(payload.get('latest_closed_session') or {}).get('date')}",
        f"- 退出码：{exit_code}（exit={exit_code}）",
        f"- 异常：{_anomaly_line(anomalies)}",
    ]
    for name in BLOCK_ORDER:
        block = biz.get(name) or {}
        lines.append(f"- `{name}`：{block.get('status')} —— {block.get('reason')}")
    lines += [
        f"- 补步：{','.join(steps) or '无'}（跳过：{','.join(skip) or '无'}）",
        f"- 下一轮：{_next_hint(payload)}",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_brief(payload: dict, *, report_dir=None) -> dict:
    """写两个文件：`<ops>/brief/<date>.md`（**追加**）＋ `<ops>/latest-patrol-brief.md`
    （**覆盖**）。返回路径或 `{"error": ...}` —— **绝不抛**（纪律 5：产物不阻断）。"""
    try:
        root = journal.report_dir_of(report_dir)
        text = brief_text(payload)
        day = str(payload.get("today") or (payload.get("now") or "")[:10] or "unknown")
        brief_dir = root / "brief"
        brief_dir.mkdir(parents=True, exist_ok=True)
        day_file = brief_dir / f"{day}.md"
        with day_file.open("a", encoding="utf-8") as fh:
            fh.write(text)                  # 同日第二轮**追加**，不覆盖第一轮
        latest_file = root / "latest-patrol-brief.md"
        latest_file.write_text(text, encoding="utf-8")
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"day_file": str(day_file), "latest_file": str(latest_file)}
