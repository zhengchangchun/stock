"""插桩版本状态机（设计文档 §9）。

## 状态不存列，由事件流推导

`plugin_scripts` 的每一行都不可变，状态 = 对 `plugin_audit` 按 `audit_id`
折叠求值。理由（设计文档 §5.7）：版本库是「AI 生成代码 → 人工审核 →
上线」的**审计链**，「这版是谁在什么时候批的」必须永久可查；一个可
UPDATE 的 status 列会把这信息抹掉。

代价是每次查状态要扫一遍 audit 表。该表极小（一轮迭代几行），且
`idx_plugin_audit_script` 已覆盖。

## 非法迁移在这里拒绝

`draft → active`（跳过沙盒）、`rejected → active` 一律抛
`PluginStateError`。「回滚」不是独立命令 —— 对 `archived` 版本执行
`approve` 即可，这样回滚走的是与上线完全相同的已审计路径，不会成为
绕过审核的后门。
"""

from __future__ import annotations

import sqlite3

from stocklab.plugin import runtime, store

#: 折叠 `plugin_audit` 得到的可能状态。
STATES: tuple[str, ...] = ("draft", "pending_review", "rejected", "active",
                           "archived")

#: 事件 → 折叠后的状态（`submit` 不改状态：沙盒还没跑完）。
_ACTION_TO_STATE: dict[str, str] = {
    "sandbox_pass": "pending_review",
    "sandbox_fail": "rejected",
    "approve": "active",
    "reject": "rejected",
    "archive": "archived",
}


class PluginStateError(Exception):
    """非法状态迁移，或状态本身自相矛盾。"""


class NoActivePlugin(LookupError):
    """该 plugin_id 下没有 active 版本。**不兜底**：不返回默认脚本。"""


def script_state(conn: sqlite3.Connection, script_id: int) -> str:
    """折叠该脚本的全部审计事件，得到当前状态。无事件 = `draft`。"""
    state = "draft"
    for row in store.list_audit(conn, script_id=script_id):
        nxt = _ACTION_TO_STATE.get(row["action"])
        if nxt:
            state = nxt
    return state


def active_script_id(conn: sqlite3.Connection, plugin_id: str,
                     *, required: bool = False) -> int | None:
    """该 plugin_id 下状态为 `active` 的脚本 id。

    允许 0 个（返回 None）或 1 个。**出现 2 个是结构错误**，必须抛
    `PluginStateError` —— 随手挑一个会让报告里的数字回答不了
    「这是哪版脚本跑出来的」。
    """
    active = [s["script_id"] for s in store.list_scripts(conn, plugin_id=plugin_id)
              if script_state(conn, s["script_id"]) == "active"]
    if len(active) > 1:
        raise PluginStateError(
            f"插桩 {plugin_id} 下有 {len(active)} 个 active 版本"
            f"（{active}）—— 状态机被破坏了，请人工处理")
    if not active:
        if required:
            raise NoActivePlugin(
                f"插桩 {plugin_id} 没有 active 版本，拒绝执行。"
                "（本系统刻意不兜底：兜底会让报告里的策略名与实际执行的"
                "东西脱钩）")
        return None
    return active[0]


def baseline_for(conn: sqlite3.Connection, script_id: int) -> int | None:
    """给沙盒对比挑一个**默认基线**版本（`plugin sandbox <id>` 不传 `--baseline` 时用）。

    规则（顺序敏感）：

    1. 该插件有 active 版本、且**不是** `script_id` 本身 → 用 active
       （拿现役版本当基线，对比待审 / 归档版本）。
    2. 否则（`script_id` 就是 active，或该插件无 active）→ 同插件下
       `script_id` 更小的最大者，即**上一个版本**。
    3. 都不存在（首版）→ `None`。

    **必须避开「自己」**：`run_sandbox` 对 candidate == baseline 会短路成
    `INCONCLUSIVE(self_comparison)`。而 `plugin sandbox <active_id>`（拿现役
    版本去比）恰恰是最常见的用法 —— 选了它自己就等于跑了个寂寞。
    """
    row = store.get_script(conn, script_id)
    if row is None:
        return None
    pid = row["plugin_id"]
    active = active_script_id(conn, pid)
    if active is not None and active != script_id:
        return active
    older = [s["script_id"] for s in store.list_scripts(conn, plugin_id=pid)
             if s["script_id"] < script_id]
    return max(older) if older else None


def record_submit(conn: sqlite3.Connection, script_id: int, *, actor: str,
                  now: str) -> None:
    store.insert_audit(conn, script_id=script_id, action="submit", actor=actor,
                       reason=None, now=now)


def record_sandbox(conn: sqlite3.Connection, script_id: int, *, passed: bool,
                   reason: str, now: str) -> str:
    store.insert_audit(conn, script_id=script_id,
                       action="sandbox_pass" if passed else "sandbox_fail",
                       actor="sandbox", reason=reason, now=now)
    return script_state(conn, script_id)


def approve(conn: sqlite3.Connection, script_id: int, *, actor: str,
            reason: str, now: str) -> None:
    """人工上线（或回滚）。合法前置状态：`pending_review` 或 `archived`。"""
    if not reason:
        raise ValueError("approve 必须给 --reason —— 审计链要留下为什么批")
    state = script_state(conn, script_id)
    if state not in ("pending_review", "archived"):
        raise PluginStateError(
            f"脚本 {script_id} 当前状态是 {state!r}，不允许 approve"
            "（只允许 pending_review 或 archived）。"
            "跳过沙盒直接上线是被禁止的")

    old = active_script_id(conn, store.get_script(conn, script_id)["plugin_id"])
    if old is not None and old != script_id:
        store.insert_audit(conn, script_id=old, action="archive",
                           actor=actor, reason=f"被 {script_id} 取代", now=now)
    store.insert_audit(conn, script_id=script_id, action="approve", actor=actor,
                       reason=reason, now=now)


def reject(conn: sqlite3.Connection, script_id: int, *, actor: str,
           reason: str, now: str) -> None:
    if not reason:
        raise ValueError("reject 必须给 --reason")
    state = script_state(conn, script_id)
    if state != "pending_review":
        raise PluginStateError(
            f"脚本 {script_id} 当前状态是 {state!r}，不允许 reject"
            "（只允许 pending_review）")
    store.insert_audit(conn, script_id=script_id, action="reject", actor=actor,
                       reason=reason, now=now)


def call_active(conn: sqlite3.Connection, plugin_id: str, ctx: dict,
                *, script_id: int | None = None,
                timeout_s: float = runtime.DEFAULT_TIMEOUT_S) -> dict:
    """执行插桩。

    `script_id is None`（默认）→ 解析该 plugin_id 的 active 版本，没有则
    `NoActivePlugin`（**不兜底**）。

    `script_id` 指定时 → **用该版本，且不要求它是 active** —— 沙盒要比的
    正是待审/归档版本。但该版本**必须属于同一个 plugin_id**：拿插桩1 的
    版本去跑插桩3，会让「比的是哪个插件」这件事失去意义，直接拒绝。
    """
    if script_id is None:
        script_id = active_script_id(conn, plugin_id, required=True)

    row = store.get_script(conn, script_id)
    if row is None:
        raise LookupError(f"脚本 {script_id} 不存在")
    if row["plugin_id"] != plugin_id:
        raise ValueError(
            f"脚本 {script_id} 的 plugin_id 是 {row['plugin_id']!r}，"
            f"与请求的 {plugin_id!r} 不符 —— 拒绝跨插件执行")
    fn = runtime.load_script(row["source_text"], plugin_id=plugin_id,
                             timeout_s=timeout_s)
    return fn(ctx)
