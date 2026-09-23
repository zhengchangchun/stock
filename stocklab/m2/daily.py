"""模块2 日更接线（P54）：一天把**通路 B → 通路 A → 事后打分**跑完。

## 为什么是一个入口而不是三步写进 `CLOSE_STEPS`

`CLOSE_STEPS` 是**静态元组**（`ops/chain.py`），而「这一轮要跑哪几个策略版本」
**只有运行期才知道**（账户 + 验证周期状态都在库里）。把遍历塞进链的步骤表，
步数就会跟着库的状态变 —— 而链的顺序语义与页面「一行一步」的读法都建立在
「计划是固定的」这件事上。所以链上只加**一步** `m2_daily`，遍历留在 CLI 里。

## 跑什么（顺序即依赖顺序）

| 步 | 命令 | 什么时候跑 |
|---|---|---|
| ① 通路 B | 镜像 `arm-now`（人工流水复刻） | **总是跑**：与人当天有没有下单无关，幂等 |
| ② 通路 A | 每个**在飞版本**跑一遍 | 账户存在且验证周期不在飞时**记一行 skipped**，不是失败 |
| ③ 打分 | `m2 score`（只给 `target_date <= asof` 且还没打分的） | **总是跑** |

「在飞版本」= 账户行存在（`params.executor == m2_channel_a`）**且**该版本**最近一轮**
验证周期既没被熔断终止、脚本也没冻结。判据只有这两条 —— **不加第三条**：
多一条静默的排除规则，就会让「今天为什么没跑」在回执里找不到答案。
（`validation_end` 收尾**不排除**：D-44 的「微调 ⇒ 同版本新开周期」正是靠它接回来的。）

## 退出码（沿用既有语义，**不发明新的**）

| 码 | 含义 |
|---|---|
| 0 | 全跑完（含 `already` 幂等、含「没有在飞版本」的 skipped） |
| 2 | 当日 K 线未定型 / 库不可用 / 输入不合法（`--asof` 不是交易日） |
| 4 | **有通路 `rejected`**（配置了却坏了 ⇒ 链上必须报红，不许静默） |

两条**刻意**的取舍，都写在这里而不是留给读者猜：

1. **一步的 `exit_code` 是「它对整轮退出码的贡献」**，所以 `skipped` 的步给 0 ——
   §1.4 的 0 明确覆盖「没有在飞版本」的 skipped。与 `m2 channel a` **单跑**时
   把 skipped 报成 3 是两件事：那一条命令的语义是「这一条通路没跑成」，
   而这里的语义是「这一轮该做的都做了」。**不能用 3**：链的停止线把 ≥2 当**结构性
   失败**（`ops/runner.py::run_steps`）⇒ 报 3 会让一次等数据把整条收盘链**截断**
   （`doctor` 再也跑不到）。
2. **K 线定型闸门复用 P46 那一道**（`session/close.py::bars_finalized_on`），
   **不另写判据**：`m2_forecasts` 与 `predictions` 同族（append-only），
   写错了退不回来（ERROR_DIARY #60）。判据只在 `asof == 今天` 时生效 ——
   历史 `asof` 的复算不受影响。

## 不做重试、不做补写

通路与打分各自的幂等键已经够（`ran_run` / `forecast_id` UNIQUE）。给它们加
「重试」「补跑昨天」之类的兜底，会把「漏跑」变成**看不见**。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.m2 import channel_a, channel_b
from stocklab.m2 import config
from stocklab.m2 import score as m2_score
from stocklab.m2 import store as m2_store
from stocklab.paper import engine
from stocklab.paper import store as paper_store
from stocklab.plugin import lifecycle
from stocklab.store import validation as ledger

#: 回执里的步骤名（与 `/lab/ops` 页面上那一行同名）。
STEP_CHANNEL_B: str = "channel_b"
STEP_CHANNEL_A: str = "channel_a"
STEP_SCORE: str = "score"

#: `/lab/m2` 顶部那一句的标签（页面与用例引用**同一个**串，不各写一份）。
LAST_RUN_LABEL: str = "上次通路运行"

#: 回执里的两个原因码。**必须写清**：跳过是结论，空一行不是。
REASON_NO_INFLIGHT: str = "没有在飞的策略版本"
REASON_FUSED: str = "验证周期已熔断终止（circuit_breaker）"
REASON_FROZEN: str = "验证周期已冻结（frozen）"

#: 一步对整轮退出码的贡献。见模块 docstring 的取舍 1。
_STEP_EXIT: dict[str, int] = {
    config.STATUS_RAN: 0,
    config.STATUS_ALREADY: 0,
    config.STATUS_SKIPPED: 0,
    config.STATUS_REJECTED: 4,
}


def strategy_version_of(account_id: str) -> str:
    """`arm-agent-<策略版本>` → 策略版本（账户命名的唯一真源是 `m2/config.py`）。"""
    return str(account_id)[len(config.ACCOUNT_PREFIX):]


def channel_a_accounts(conn: sqlite3.Connection) -> list[str]:
    """通路 A 的账户 id。

    判据走**既有谓词** `paper/engine.py::external_executor`（`params.executor`），
    不靠账户名前缀猜 —— 前缀猜法会把 `arm-agent-random` 混进来，也会在改名那天
    悄悄漏掉另一批。同一个判据另有 `m2/channel_a.py::load_account` 与
    `labweb/m2_data.py::_channel_a_accounts` 两处调用点（**未收口**，见任务书
    §实施记录的存疑项），三处必须给出同一批账户。
    """
    out: list[str] = []
    for account in paper_store.load_accounts(conn):
        try:
            executor = engine.external_executor(account)
        except (ValueError, TypeError):     # `params_json` 坏行：当它不是在飞账户
            continue
        if executor == config.EXECUTOR_CHANNEL_A:
            out.append(str(account["account_id"]))
    return sorted(out)


def _cycles_of(conn: sqlite3.Connection, account_id: str) -> list[dict]:
    return [c for c in ledger.list_cycles(conn)
            if str(c["account_id"]) == account_id]


def _exclusion(conn: sqlite3.Connection, account_id: str) -> str | None:
    """该版本**当前**（最近一轮周期）是否已被排除；`None` = 在飞。

    「当前状态 = 最近开的那一轮（`cycle_id` 最大）」是本站的落地解读：一个版本
    可以有多轮周期（熔断后解冻重开、微调后新开），拿**任意一轮**被熔断过就永久
    排除，会把「重新开跑」的版本永远挡在门外。
    """
    cycles = _cycles_of(conn, account_id)
    if not cycles:
        return None                          # 还没开周期 ⇒ 在飞（账户在就是它在跑）
    latest = cycles[-1]                      # `list_cycles` 按 `cycle_id` 升序
    if ledger.find_event(conn, int(latest["cycle_id"]), "circuit_breaker"):
        return REASON_FUSED
    if lifecycle.script_state(conn, int(latest["script_id"])) == "frozen":
        return REASON_FROZEN
    return None


def last_run(conn: sqlite3.Connection) -> dict | None:
    """最近一次通路运行（`m2_channel_runs` 里 `run_id` 最大的那行）→ 页面那句。

    **只读**：页面把这一句摆出来，而不是自己去比「今天跑没跑」——
    「上次跑成什么样」在库里只有一处真源（那张台账），重算一遍就多一个会漂的读数。
    `None` = 一次都没跑过（页面写「从未」，**不写 0、不写今天的日期**）。
    """
    runs = m2_store.list_runs(conn)
    if not runs:
        return None
    row = runs[-1]                    # `list_runs` 按 `run_id` 升序
    return {"run_id": int(row["run_id"]), "asof": str(row["asof"]),
            "status": str(row["status"]), "channel": str(row["channel"]),
            "account_id": str(row["account_id"]),
            "reason": str(row["reason"])}


def versions(conn: sqlite3.Connection) -> dict:
    """账户现状 → `{"inflight": [...], "excluded": [{策略版本, 原因, ...}]}`。

    **查询期不写库**：只读 `paper_accounts` / `validation_cycles` /
    `validation_events` / `plugin_audit` 四处既有真源。
    """
    inflight: list[str] = []
    excluded: list[dict] = []
    for account_id in channel_a_accounts(conn):
        version = strategy_version_of(account_id)
        reason = _exclusion(conn, account_id)
        if reason is None:
            inflight.append(version)
        else:
            excluded.append({"strategy_version": version,
                             "account_id": account_id, "reason": reason})
    return {"inflight": inflight, "excluded": excluded}


def _skipped_a(versions_out: dict, *, asof: str) -> dict:
    """没有在飞版本时那一行 —— 写清**看了几个账户、哪一个为什么被排除**。"""
    n_accounts = len(versions_out["inflight"]) + len(versions_out["excluded"])
    detail = "；".join(f"{e['strategy_version']}={e['reason']}"
                       for e in versions_out["excluded"]) or "无被排除项"
    return {
        "step": STEP_CHANNEL_A, "status": config.STATUS_SKIPPED, "exit_code": 0,
        "asof": asof, "account_id": None, "strategy_version": None,
        "reason": (f"{REASON_NO_INFLIGHT}（`arm-agent-*` 账户 {n_accounts} 个，"
                   f"被排除 {len(versions_out['excluded'])} 个：{detail}）"
                   "—— 等有在飞版本后重跑本命令即可"),
    }


def _step(name: str, out: dict, *, asof: str, **extra) -> dict:
    """通路返回值 → 回执里的一行（步骤名 / 状态 / 它对整轮退出码的贡献）。"""
    status = str(out.get("status") or config.STATUS_REJECTED)
    return {
        "step": name, "status": status, "exit_code": _STEP_EXIT.get(status, 4),
        "asof": asof, "account_id": out.get("account_id"),
        "reason": _reason_text(out), **extra,
    }


def _reason_text(out: dict) -> str:
    """一句话说清这一步做了什么 / 为什么没做成（`rejected` 的码要点名）。"""
    status = out.get("status")
    if status == config.STATUS_ALREADY:
        return str(out.get("note") or "already：同 (通路, 账户, 日) 已经跑过，零写入")
    if status == config.STATUS_SKIPPED:
        return f"[{out.get('skip_code')}] {out.get('reason')}"
    if status == config.STATUS_REJECTED:
        return f"[{out.get('reject_code')}] {out.get('reason')}"
    return str(out.get("reason") or "")


def run_daily(conn: sqlite3.Connection, *, asof: str, now: str) -> dict:
    """跑一天的模块2 三条线并返回回执载荷（**退出码由调用方按 `status` 给**）。

    全程**进程内**（不起子进程）：三个子步骤都是本项目已有的函数，
    起子进程只会让「哪一步失败」变成读 stderr 猜。
    """
    inflight = versions(conn)
    steps: list[dict] = []

    # ① 通路 B：总是跑（镜像 `arm-now`，与人当天有没有下单无关）。
    steps.append(_step(STEP_CHANNEL_B,
                       channel_b.run(conn, asof=asof, now=now), asof=asof))

    # ② 通路 A：遍历在飞版本；一个都没有 ⇒ 如实 skipped（不是失败）。
    if inflight["inflight"]:
        for version in inflight["inflight"]:
            out = channel_a.run(conn, asof=asof, strategy_version=version, now=now)
            steps.append(_step(STEP_CHANNEL_A, out, asof=asof,
                               strategy_version=version))
    else:
        steps.append(_skipped_a(inflight, asof=asof))

    # ③ 打分：总是跑。
    counts = m2_score.score_all(conn, asof=asof, now=now)
    steps.append({
        "step": STEP_SCORE, "status": config.STATUS_RAN, "exit_code": 0,
        "asof": asof, "account_id": None, "counts": counts,
        "reason": (f"事后打分（截止 {asof}）：扫 {counts['scanned']} 条，"
                   f"新落 {counts['scored']} 条、不可评 {counts['unscorable']} 条、"
                   f"已有 {counts['already']} 条、目标日未到 {counts['pending']} 条"),
    })

    n_rejected = sum(1 for s in steps if s["status"] == config.STATUS_REJECTED)
    return {
        "asof": asof, "now": now,
        "status": config.STATUS_REJECTED if n_rejected else config.STATUS_RAN,
        "steps": steps,
        "n_steps": len(steps),
        "n_rejected": n_rejected,
        "n_skipped": sum(1 for s in steps if s["status"] == config.STATUS_SKIPPED),
        "n_already": sum(1 for s in steps if s["status"] == config.STATUS_ALREADY),
        "versions": inflight,
        "score": counts,
        "note": ("通路 A 的遍历在 CLI 里（`CLOSE_STEPS` 是静态元组）—— "
                 "链上只有 `m2_daily` 这一步，见 m2/daily.py 模块 docstring"),
    }


__all__ = ["LAST_RUN_LABEL", "REASON_FROZEN", "REASON_FUSED", "REASON_NO_INFLIGHT",
           "STEP_CHANNEL_A", "STEP_CHANNEL_B", "STEP_SCORE",
           "channel_a_accounts", "last_run", "run_daily", "strategy_version_of",
           "versions"]
