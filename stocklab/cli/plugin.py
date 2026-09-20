"""`plugin` 子命令：插桩脚本的提交 / 审核 / 上线 / 查看（设计文档 §9.2）。

## `approve` 与 `reject` 只在这里出现

设计文档 §9.3 要求「approve 只能由人工 CLI 命令触发」。这条由
`tests/test_plugin_lifecycle.py::test_approve_is_not_reachable_from_non_cli_code`
源码扫描钉住 —— 除本文件与 `plugin/lifecycle.py` 自身外，任何模块出现
`lifecycle.approve(` 都会让测试变红。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from stocklab.config import paths
from stocklab.plugin import contract, guard, lifecycle, runtime, sandbox, store
from stocklab.plugin.sandbox import SandboxDeps
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


def _sandbox_window(now: str) -> tuple[str, str]:
    """沙盒默认窗口：最近 3 年（设计文档 §8.2）。

    `now` 取自调用方透传的 `--now` 值（或当前 UTC 时间字符串），
    以便测试中通过固定的 `--now` 得到确定的 `window_end`。
    """
    end = date.fromisoformat(now[:10])
    start = end.replace(year=end.year - 3)
    return start.isoformat(), end.isoformat()


def _run_sandbox(conn, *, script_id: int, plugin_id: str, source_text: str,
                 now: str) -> tuple[bool, str]:
    """跑沙盒并把结果写入 `plugin_backtests`，返回 `(passed, reason)`。

    ## 契约预检（Finding A 修复）

    先用 `runtime.load_script` + 空探针 ctx 执行一次脚本，验证
    「能加载、能执行、返回结构合规」。这恢复了 Task 7 占位沙盒时的保证：
    只通过静态 AST 检查（`guard.check_source`）但违反契约的脚本，在这里
    被拒绝而不是带着无效 payload 进入 `pending_review`。

    ## `passed` 的判据**不是** verdict

    - 加载失败 / 契约不符 → `passed=False`（脚本有问题）
    - `LOSE` → `passed=False`（有证据表明更差）
    - `INCONCLUSIVE` → **`passed=True`**（样本不足不是脚本的错，
      把它当「有问题」会让所有首版脚本都被驳回）

    ## 失败时的审计落点（Finding B 说明）

    当探针或沙盒失败时，本函数直接返回 `(False, reason)` 而不写
    `plugin_backtests` —— 没有对比跑完就没有对比报告可写，这是刻意设计。
    审计链并不因此断掉：调用方 `cmd_plugin_submit` 随即调用
    `lifecycle.record_sandbox(passed=False, reason=...)` 写入
    `plugin_audit`（action='sandbox_fail'），任何时候都可以通过
    `store.list_audit(conn, script_id)` 查到失败原因。
    """
    # ── 契约预检：先探针，后沙盒 ────────────────────────────────────
    try:
        fn = runtime.load_script(source_text, plugin_id=plugin_id)
        fn(contract.PROBE_CTX)   # 契约定义的完整空探针 ctx；让读 ctx key 的正常脚本能运行
    except contract.PluginContractError as exc:
        return False, f"契约预检未通过：{exc}"
    except Exception as exc:                       # noqa: BLE001
        return False, f"脚本加载/执行失败：{type(exc).__name__}: {exc}"

    # ── 沙盒对比回测 ─────────────────────────────────────────────────
    baseline_id = lifecycle.active_script_id(conn, plugin_id)
    start, end = _sandbox_window(now)
    try:
        from stocklab.candidate import replay as _replay_mod
        _deps = SandboxDeps(
            replay=_replay_mod.replay_period_deltas,
            benchmark_excess=_replay_mod.benchmark_excess,
            rebalance_marks=_replay_mod.rebalance_marks,
        )
        verdict = sandbox.run_sandbox(
            conn, candidate_script_id=script_id,
            baseline_script_id=baseline_id, pool="short",
            window_start=start, window_end=end, now=now,
            deps=_deps)
    except Exception as exc:                       # noqa: BLE001
        return False, f"沙盒执行失败：{type(exc).__name__}: {exc}"

    flag = verdict.detail.get("overfit_flag")
    store.insert_backtest(
        conn, candidate_script_id=script_id, baseline_script_id=baseline_id,
        pool=verdict.pool, window_start=start, window_end=end,
        metrics=verdict.as_metrics(), verdict=verdict.verdict,
        overfit_flag=flag,
        report_sha256=store.source_sha256(verdict.note), now=now)

    parts = [f"verdict={verdict.verdict}", verdict.note]
    if flag == "suspected":
        parts.append("⚠️ 疑似过拟合")
    return verdict.verdict != "LOSE", "；".join(parts)


def cmd_plugin_sandbox(args) -> int:
    from stocklab.candidate import replay as _replay_mod
    from stocklab.plugin import sandbox

    conn = _open(args)
    try:
        sid = int(args.script_id)
        row = store.get_script(conn, sid)
        if row is None:
            # 首版（或 script_id 不存在）→ baseline=None → INCONCLUSIVE
            baseline = None
        else:
            baseline = lifecycle.active_script_id(conn, row["plugin_id"])
        _deps = SandboxDeps(
            replay=_replay_mod.replay_period_deltas,
            benchmark_excess=_replay_mod.benchmark_excess,
            rebalance_marks=_replay_mod.rebalance_marks,
        )
        verdict = sandbox.run_sandbox(
            conn, candidate_script_id=sid, baseline_script_id=baseline,
            pool=args.pool, window_start=args.window_start,
            window_end=args.window_end, now=_now(args.now),
            deps=_deps)
        print(f"verdict={verdict.verdict} pool={verdict.pool} "
              f"n_periods={verdict.n_periods}")
        print(verdict.note)
        d = verdict.detail
        if d.get("candidate_excess_index300") is not None:
            print(f"候选版本相对 index_300 超额："
                  f"{d['candidate_excess_index300']:+.4%}")
            print(f"基线版本相对 index_300 超额："
                  f"{d['baseline_excess_index300']:+.4%}")
        if d.get("overfit_flag") == "suspected":
            print("⚠️ 疑似过拟合（训练段明显好于验证段）")
        print(f"回放时插件版本：{d.get('scripts')}")
        return 0
    finally:
        conn.close()


def cmd_plugin_submit(args) -> int:
    source_path = Path(args.file)
    if not source_path.is_file():
        print(f"文件不存在：{source_path}", file=sys.stderr)
        return 2
    source_text = source_path.read_text(encoding="utf-8")

    if not args.actor or not args.actor.strip():
        print("--actor 不能为空字符串", file=sys.stderr)
        return 2

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
        passed, reason = _run_sandbox(conn, script_id=script_id,
                                      plugin_id=args.plugin_id,
                                      source_text=source_text, now=now)
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
