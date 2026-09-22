"""四支插桩的调用与契约校验（D-24 / D-33 / P45）。

## 通路只做三件事：解析版本 → 执行 → 校验

契约（形状、越界拒绝、引用校验）**已经在 `plugin/contract.py` 里定死**（P45）。
本模块不重复实现任何一条规则，只负责：

1. 解析该插桩的 **active 版本**（没有 active → `NoActivePlugin`，**不兜底**；
   兜底会让报告里的策略名与实际执行的东西脱钩）；
2. 执行（`plugin/lifecycle.py::call_active`：沙盒化的受限命名空间 + 超时）；
3. 按插件语义做**引用校验**（`m2_a1` 的允许集合 = 候选池，`m2_a2` 的 = 当前持仓）。

## 为什么把「脚本指纹」单独取一次

`plugin_audit` 记的是「哪一版被批上线」，`m2_channel_runs.plugins_json` 要记的是
「**这一天实际跑的是哪一版**」。两者在今天通常相同，但脚本可以在两次运行之间
被换掉 —— 事后读成交行的人必须能回答「这笔单照的是哪版脚本」。
指纹（`script_id` / `version` / `source_sha256`）因此随每次运行落进台账。
"""

from __future__ import annotations

import sqlite3

from stocklab.m2.config import PLUGIN_A1, PLUGIN_A2
from stocklab.plugin import contract, lifecycle
from stocklab.plugin import store as plugin_store


def script_fingerprint(conn: sqlite3.Connection, plugin_id: str) -> dict:
    """该插桩当前生效版本的指纹。没有 active 版本 → `NoActivePlugin`。"""
    script_id = lifecycle.active_script_id(conn, plugin_id, required=True)
    row = plugin_store.get_script(conn, script_id)
    return {"script_id": int(script_id), "version": str(row["version"]),
            "source_sha256": str(row["source_sha256"])}


def call_forecast(conn: sqlite3.Connection, plugin_id: str, ctx: dict) -> tuple[dict, dict]:
    """`m2_a3` / `m2_b1`：形状校验由执行器做（`validate_return`）。

    **不做引用校验** —— 这两支按定义不引用标的（它们预测的是 ctx 里那个持仓），
    调用 `validate_references` 会因「该插桩不引用标的」而报调用方编程错误。
    """
    fingerprint = script_fingerprint(conn, plugin_id)
    return contract.validate_return(plugin_id, lifecycle.call_active(conn, plugin_id, ctx)), \
        fingerprint


def pick(conn: sqlite3.Connection, ctx: dict, *,
         pool_codes) -> tuple[dict, dict]:
    """`m2_a1`（选股）：形状 + **池内引用** 双校验。越界抛 `PluginContractError`。"""
    fingerprint = script_fingerprint(conn, PLUGIN_A1)
    out = contract.validate_references(
        PLUGIN_A1, lifecycle.call_active(conn, PLUGIN_A1, ctx),
        allowed_codes=pool_codes)
    return out, fingerprint


def sell_orders(conn: sqlite3.Connection, ctx: dict, *,
                held_codes) -> tuple[dict, dict]:
    """`m2_a2`（卖出侧）：形状 + **持仓内引用** 双校验。

    允许集合是**当前持仓**而不是候选池：A2 的职责是止盈止损/调仓退出，
    它能卖的只有手里有的。给池子会允许它「卖掉一个没持有的标的」——
    那在 A 股是融券（见 `NO_SHORT_SIDE_MSG`）。
    """
    fingerprint = script_fingerprint(conn, PLUGIN_A2)
    out = contract.validate_references(
        PLUGIN_A2, lifecycle.call_active(conn, PLUGIN_A2, ctx),
        allowed_codes=held_codes)
    return out, fingerprint
