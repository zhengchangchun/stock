"""`plugin` 子命令：插桩脚本的提交 / 审核 / 上线 / 查看（设计文档 §9.2）。

## `approve` 与 `reject` 只在这里出现

设计文档 §9.3 要求「approve 只能由人工 CLI 命令触发」。这条由
`tests/test_plugin_lifecycle.py::test_approve_is_not_reachable_from_non_cli_code`
源码扫描钉住 —— 除本文件与 `plugin/lifecycle.py` 自身外，任何模块出现
`lifecycle.approve(` 都会让测试变红。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from stocklab.config import paths
from stocklab.plugin import guard, lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import ensure_schema


def _now(override: str | None) -> str:
    if override:
        return override
    return datetime.now(timezone.utc).isoformat()


def _open(args):
    db_path = Path(args.db) if args.db else paths.DB_PATH
    ensure_schema(db_path)
    return connect(db_path)


def _run_sandbox_placeholder(source_text: str, plugin_id: str) -> tuple[bool, str]:
    """Task 16 之前的占位沙盒：只确认脚本加载得起来、跑得出合规结果。

    **返回 True 不等于「策略有效」** —— 这里还没有任何样本外证据。
    真沙盒在 Task 16/17 接入，届时本函数被替换。

    注意：guard.check_source 已经在 cmd_plugin_submit 里跑过一遍了。
    这里直接 compile+exec，不再重复调用 runtime.load_script（避免二次 guard）。
    """
    try:
        from stocklab.plugin import contract  # 局部 import，避免循环
        ns: dict = {"__builtins__": {}}
        code = compile(source_text, f"<plugin:{plugin_id}>", "exec")
        exec(code, ns)                         # noqa: S102 —— 已由调用方 guard 预检
        run_fn = ns.get("run")
        if not callable(run_fn):
            raise contract.PluginContractError("脚本未定义 run(ctx)")
        result = run_fn({"code": "__probe__", "asof": "1970-01-01"})
        contract.validate_return(plugin_id, result)
    except Exception as exc:                   # noqa: BLE001 —— 一律记原因
        return False, f"加载/执行失败：{type(exc).__name__}: {exc}"
    return True, "沙盒未接线（Task 16 前）：仅验证了可加载、可执行、契约通过"


def cmd_plugin_submit(args) -> int:
    source_path = Path(args.file)
    if not source_path.is_file():
        print(f"文件不存在：{source_path}", file=sys.stderr)
        return 2
    source_text = source_path.read_text(encoding="utf-8")

    try:
        guard.check_source(source_text)
    except guard.PluginGuardError as exc:
        print(f"静态预检未通过：{exc}", file=sys.stderr)
        return 1

    conn = _open(args)
    try:
        now = _now(args.now)
        try:
            script_id = store.insert_script(
                conn, plugin_id=args.plugin_id, version=args.version,
                source_text=source_text, note=args.note, now=now)
        except Exception as exc:                  # UNIQUE 冲突等
            print(f"落库失败：{exc}", file=sys.stderr)
            return 1

        lifecycle.record_submit(conn, script_id, actor=args.actor, now=now)
        passed, reason = _run_sandbox_placeholder(source_text, args.plugin_id)
        state = lifecycle.record_sandbox(conn, script_id, passed=passed,
                                         reason=reason, now=now)
        print(f"script_id={script_id} plugin_id={args.plugin_id} "
              f"version={args.version} 状态={state}")
        print(f"沙盒：{reason}")
        return 0 if state == "pending_review" else 1
    finally:
        conn.close()


def cmd_plugin_approve(args) -> int:
    conn = _open(args)
    try:
        script_id = int(args.script_id)
        row = store.get_script(conn, script_id)
        if row is None:
            print(f"script_id={args.script_id} 不存在", file=sys.stderr)
            return 1
        lifecycle.approve(conn, script_id, actor=args.actor,
                          reason=args.reason, now=_now(args.now))
        print(f"script_id={args.script_id} 已上线（plugin_id={row['plugin_id']}）")
        return 0
    except (lifecycle.PluginStateError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_plugin_reject(args) -> int:
    conn = _open(args)
    try:
        lifecycle.reject(conn, int(args.script_id), actor=args.actor,
                         reason=args.reason, now=_now(args.now))
        print(f"script_id={args.script_id} 已驳回")
        return 0
    except (lifecycle.PluginStateError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_plugin_list(args) -> int:
    conn = _open(args)
    try:
        rows = store.list_scripts(conn, plugin_id=args.plugin_id)
        if not rows:
            print("（无）")
            return 0
        active = lifecycle.active_script_id(conn, args.plugin_id) \
            if args.plugin_id else None
        for r in rows:
            state = lifecycle.script_state(conn, r["script_id"])
            mark = " ⬅ active" if r["script_id"] == active else ""
            print(f"{r['script_id']:>4}  {r['plugin_id']:>2}  {r['version']:<10}"
                  f"  {state:<15}{mark}")
        return 0
    finally:
        conn.close()
