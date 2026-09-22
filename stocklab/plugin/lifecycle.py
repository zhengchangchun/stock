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

## 模块2 的两态（P44 / D-29）

`validating`（验证中）与 `frozen`（策略冻结）由 4 个新事件折叠而来：
`start_validation` / `finish_validation` / `freeze` / `unfreeze`。
「已回滚」仍然**不设独立态** —— 回滚 = 对 `archived` 版本 `approve`。

**未知事件一律拒绝**：折叠遇到白名单外的事件抛 `PluginStateError`，
而不是跳过。跳过会让「状态」与「事件流」不一致，且没有任何一层会发现。

**未定的口径（留给 P48 显式决策）**：`active_script_id` 只认 `active`，
所以脚本进入 `validating` 后不再被解析为在役版本。P44 没有任何生产调用者
触发这些事件（只有测试会），主干链因此不受影响；「验证期算不算在役」
是 P48 的问题 —— 本轮用一条测试把**当前**行为钉住，改动必须是显式决策。
"""

from __future__ import annotations

import sqlite3

from stocklab.plugin import runtime, store

#: 折叠 `plugin_audit` 得到的可能状态（P44 追加 `validating` / `frozen`）。
STATES: tuple[str, ...] = ("draft", "pending_review", "rejected", "active",
                           "archived", "validating", "frozen")

#: 事件白名单的唯一真源在 `store.ALLOWED_ACTIONS`（写入侧也用它）。
KNOWN_ACTIONS: frozenset[str] = store.ALLOWED_ACTIONS

#: 事件 → 折叠后的状态（`submit` 不改状态：沙盒还没跑完）。
_ACTION_TO_STATE: dict[str, str] = {
    "sandbox_pass": "pending_review",
    "sandbox_fail": "rejected",
    "approve": "active",
    "reject": "rejected",
    "archive": "archived",
    "start_validation": "validating",
    "finish_validation": "active",
    "freeze": "frozen",
    "unfreeze": "active",
}


class PluginStateError(Exception):
    """非法状态迁移，或状态本身自相矛盾。"""


class NoActivePlugin(LookupError):
    """该 plugin_id 下没有 active 版本。**不兜底**：不返回默认脚本。"""


def apply_event(action: str) -> str | None:
    """单个事件对状态的迁移；**不改状态**的事件（如 `submit`）返回 `None`。

    白名单外的 action 抛 `PluginStateError` —— **不静默忽略**：跳过一条事件
    会让折叠出的状态与事件流不一致，而下游（报告、审计）看到的仍是一个
    合法状态，没有任何一层会报警。同型教训见 `store.insert_audit`。
    """
    if action not in KNOWN_ACTIONS:
        raise PluginStateError(
            f"未知审计事件 {action!r} —— 拒绝折叠。事件类型必须先在 "
            "store.ALLOWED_ACTIONS 与 schema.sql 的 CHECK 里登记")
    return _ACTION_TO_STATE.get(action)


def script_state(conn: sqlite3.Connection, script_id: int) -> str:
    """折叠该脚本的全部审计事件，得到当前状态。无事件 = `draft`。"""
    state = "draft"
    for row in store.list_audit(conn, script_id=script_id):
        nxt = apply_event(row["action"])
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


# ---------- 模块2 状态事件（P44 / D-29）----------

#: 新事件的合法**前置状态**。非法前置一律拒绝 —— 与 `approve` / `reject` 同纪律。
_EVENT_PRE_STATES: dict[str, tuple[str, ...]] = {
    "start_validation": ("active",),
    "finish_validation": ("validating",),
    "freeze": ("active", "validating"),
    "unfreeze": ("frozen",),
}


def _record_event(conn: sqlite3.Connection, action: str, script_id: int, *,
                  actor: str, reason: str, now: str) -> str:
    """记一条状态事件并返回折叠后的新状态（共用前置状态与 reason 校验）。"""
    if not reason:
        raise ValueError(f"{action} 必须给 --reason —— 审计链要留下为什么")
    state = script_state(conn, script_id)
    allowed = _EVENT_PRE_STATES[action]
    if state not in allowed:
        raise PluginStateError(
            f"脚本 {script_id} 当前状态是 {state!r}，不允许 {action}"
            f"（只允许 {' 或 '.join(allowed)}）")
    store.insert_audit(conn, script_id=script_id, action=action, actor=actor,
                       reason=reason, now=now)
    return script_state(conn, script_id)


def start_validation(conn: sqlite3.Connection, script_id: int, *, actor: str,
                     reason: str, now: str) -> str:
    """`active` → `validating`（模块2 验证期开始）。"""
    return _record_event(conn, "start_validation", script_id, actor=actor,
                         reason=reason, now=now)


def finish_validation(conn: sqlite3.Connection, script_id: int, *, actor: str,
                      reason: str, now: str) -> str:
    """`validating` → `active`（验证期结束，策略仍在役）。"""
    return _record_event(conn, "finish_validation", script_id, actor=actor,
                         reason=reason, now=now)


def freeze(conn: sqlite3.Connection, script_id: int, *, actor: str,
           reason: str, now: str) -> str:
    """`active` / `validating` → `frozen`（策略冻结）。"""
    return _record_event(conn, "freeze", script_id, actor=actor,
                         reason=reason, now=now)


def unfreeze(conn: sqlite3.Connection, script_id: int, *, actor: str,
             reason: str, now: str) -> str:
    """`frozen` → `active`（人工解冻）。"""
    return _record_event(conn, "unfreeze", script_id, actor=actor,
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
