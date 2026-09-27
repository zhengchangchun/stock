"""`candidate` 子命令：候选池主流程（模块1）。"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from stocklab.candidate import status as cand_status
from stocklab.candidate.report import write_report_file
from stocklab.candidate.run import recommend_optimization, run_candidate
from stocklab.candidate.snapshot import STATUSES
from stocklab.config import paths
from stocklab.config.universes import UniverseError, resolve_universe
from stocklab.plugin import contract, guard, lifecycle, runtime
from stocklab.plugin_review import run_review
from stocklab.store.db import connect
from stocklab.store.migrate import ensure_schema

#: `--status` 的 choices（与 `candidate/snapshot.py::STATUSES` **同一份**元组）。
STATUS_CHOICES: tuple[str, ...] = STATUSES

#: 6 位数字代码。与本仓其它写入口同一条口径（`--code 333` 这种必须当场拒）。
_CODE_RE = re.compile(r"\d{6}")


def cmd_candidate_run(args) -> int:
    db_path = Path(args.db) if args.db else paths.DB_PATH
    try:
        # `--universe` 缺省 ⇒ seed21（主干常量）；非默认 id 走 `load_universe`（fail-closed）。
        # 先解析宇宙**再**碰库：用法错误必须零写入（`ensure_schema` 会写库）。
        universe_id, members, members_sha256 = resolve_universe(
            getattr(args, "universe", None))
    except UniverseError as exc:
        print(f"❌ --universe：{exc}", file=sys.stderr)
        return 2
    ensure_schema(db_path)
    conn = connect(db_path)
    try:
        now = args.now or datetime.now(timezone.utc).isoformat()
        result = run_candidate(conn, asof=args.asof, run_kind=args.run_kind,
                               now=now, universe=members,
                               universe_id=universe_id,
                               members_sha256=members_sha256)
        if result.skipped:
            print(f"⏭ 快照已存在（snapshot_id={result.snapshot_id}），跳过重跑")

        if args.out:
            # `--out` 是**显式文件路径**（可覆盖文件名），不走命名规则
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(result.report_md, encoding="utf-8")
        else:
            # 默认路径与文件名规则走共享函数（页面也调它，两处产出物同源）
            out = write_report_file(result, paths.REPORT_DIR / "candidate")

        counts = {}
        for m in result.members:
            counts[m.pool] = counts.get(m.pool, 0) + 1
        print(f"snapshot_id={result.snapshot_id} asof={result.asof} "
              f"kind={result.run_kind}")
        print(f"入池：短期 {counts.get('short', 0)} / 中期 {counts.get('mid', 0)}"
              f" / 长期 {counts.get('long', 0)}；淘汰 {len(result.rejects)}")
        if recommend_optimization(result):
            print("⚠️ 建议触发优化子任务（本轮只记标志，不生成脚本）")
        print(f"报告：{out}")
        return 0
    except Exception as exc:                       # noqa: BLE001 —— CLI 顶层
        print(f"❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_candidate_review(args) -> int:
    """`candidate review --asof <日>`：插桩5「定期复盘分析」的唯一调用路径（P58）。

    退出码：
    - 0 = 跑完（**含现役桩**：桩的输出是事实，台账照记、报告第 1 行自报「尚未实现」）；
    - 2 = 结构性拒绝（库不存在 / asof 不是 YYYY-MM-DD / **插桩5 没有 active 版本**）
      —— **零写入**：没有台账行、没有报告文件；
    - 1 = 有 active 版本但脚本跑不出来（预检 / 超时 / 契约校验不过）—— 同样零写入。

    复盘**不是每日动作**，所以这条命令不进 `candidate run` 的 12 步主干、不进
    `ops close`；月度链上那一步是非阻断的。
    """
    db_path = Path(args.db) if args.db else paths.DB_PATH
    try:
        date.fromisoformat(args.asof)
    except ValueError:
        print(f"❌ --asof 必须是 YYYY-MM-DD，实际是 {args.asof!r}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"❌ 库不存在（{db_path}）；先跑 `stocklab db init`", file=sys.stderr)
        return 2

    ensure_schema(db_path)
    conn = connect(db_path)
    try:
        now = args.now or datetime.now(timezone.utc).isoformat()
        result = run_review(conn, asof=args.asof, now=now,
                            report_dir=getattr(args, "report_dir", None))
    except lifecycle.NoActivePlugin as exc:
        # **点名**：不许静默跑空。没有在役版本时「跑了个寂寞」与「脚本返回空」
        # 长得一模一样，而前者是配置问题、后者是设计选择。
        print(f"❌ 插桩5 没有 active 版本，拒绝执行：{exc}", file=sys.stderr)
        return 2
    except (guard.PluginGuardError, runtime.PluginTimeout,
            contract.PluginContractError) as exc:
        print(f"❌ 插桩5 的执行没跑出来（{type(exc).__name__}）：{exc}",
              file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"asof={result['asof']} script_id={result['script_id']} "
          f"script_version={result['script_version']}")
    print(f"review_id={result['review_id']} "
          f"{'（已存在同键行，未新增）' if not result['inserted'] else ''}".rstrip())
    print(f"report_path={result['report_path']}")
    print(f"样本 {result['n_samples']}/{result['n_candidates']} 条"
          f"（剔除 {result['n_dropped']}）；回测台账 {result['n_backtests']} 条；"
          f"错判案例 {result['n_bad_cases']} 条")
    if result["stub"]:
        print("⚠️ 复盘脚本尚未实现（现役桩），本报告只有输入侧事实")
    return 0


# ---------- P91（B7）：标的状态流转 ----------

def _status_usage_error(what: str) -> None:
    print(f"❌ {what}", file=sys.stderr)


def _code_error(code, *, required: bool = True) -> str:
    """`--code` 必须是 6 位数字。返回**错误文案**（空串 = 通过）。

    `required=False`（`show` 的过滤参数）：`None` = 不过滤 ⇒ 通过。
    """
    if code is None and not required:
        return ""
    if not _CODE_RE.fullmatch(code or ""):
        return f"--code 必须是 6 位数字，实际是 {code!r}"
    return ""


def _asof_error(asof, *, required: bool) -> str:
    """`--asof` 校验。`required=True` 时 `None` 也算错（写路径必须给）。"""
    if asof is None:
        return "--asof 必须给出 YYYY-MM-DD" if required else ""
    try:
        date.fromisoformat(asof)
    except (TypeError, ValueError):
        return f"--asof 必须是 YYYY-MM-DD，实际是 {asof!r}"
    return ""


def cmd_candidate_status_set(args) -> int:
    """`candidate status set`：**追加**一条状态事件（append-only 的写入口）。

    退出码：
    - 0 = 已写入；
    - 0 = 同 `(code, asof_date, status)` 已有行 —— 打印 `already`、**零写入**
      （脚本可重跑；这也是「不要再手工查一遍」的落点）；
    - 2 = 用法错误（代码非 6 位 / asof 非法 / reason 或 actor 为空）—— 零写入。
      **必须先于 `ensure_schema` 判完** —— 它写库，用法错误却在它之后才报，
      会让「命令写错了、库文件却多出来一个」。

    **同日换状态 = 追加新行**（`UNIQUE` 只管三元组），读侧按
    `(asof_date, event_id)` 取最后一行 ⇒ 后写赢。**没有** delete/update 子命令：
    append-only 的意义就是「写错了也能看见写过什么」。
    """
    db_path = Path(args.db) if args.db else paths.DB_PATH
    bad = _code_error(args.code) or _asof_error(args.asof, required=True)
    if bad:
        _status_usage_error(bad)
        return 2
    if not (args.reason or "").strip():
        _status_usage_error("--reason 不许为空串（留空就没人知道为什么改）")
        return 2
    if not (args.actor or "").strip():
        _status_usage_error("--actor 不许为空串（状态变更必须有人/程序负责）")
        return 2

    ensure_schema(db_path)
    conn = connect(db_path)
    try:
        now = args.now or datetime.now(timezone.utc).isoformat()
        found = conn.execute(
            "SELECT event_id FROM candidate_status_events"
            " WHERE code = ? AND asof_date = ? AND status = ?",
            (args.code, args.asof, args.status)).fetchone()
        if found is not None:
            print(f"⏭ already：event_id={found[0]} code={args.code} "
                  f"asof={args.asof} status={args.status}（未写入）")
            return 0
        try:
            cur = conn.execute(
                "INSERT INTO candidate_status_events (code, asof_date, status,"
                " reason, actor, created_at) VALUES (?,?,?,?,?,?)",
                (args.code, args.asof, args.status, args.reason, args.actor,
                 now))
        except sqlite3.IntegrityError:
            # 结构性第二道防线：UNIQUE 撞了（并发/绕过上面那次查重）⇒ 幂等命中。
            row = conn.execute(
                "SELECT event_id FROM candidate_status_events"
                " WHERE code = ? AND asof_date = ? AND status = ?",
                (args.code, args.asof, args.status)).fetchone()
            if row is None:
                raise
            print(f"⏭ already：event_id={row[0]} code={args.code} "
                  f"asof={args.asof} status={args.status}（未写入）")
            return 0
        print(f"✅ 已记录：event_id={int(cur.lastrowid)} code={args.code} "
              f"asof={args.asof} status={args.status}"
              f"（reason={args.reason} actor={args.actor}）")
        return 0
    finally:
        conn.close()


def cmd_candidate_status_show(args) -> int:
    """`candidate status show`：看事件流水（只读，一行一条，最新在前）。

    - 0 = 打印完成（**没有记录也是 0**：空流水是事实，不是错误）；
    - 2 = 用法错误（代码非 6 位 / asof 非法）或库不存在。

    **不建库**（与 `candidate review` 同款）：读命令不该因为敲错一个库路径
    就凭空造出一个空库来。
    """
    db_path = Path(args.db) if args.db else paths.DB_PATH
    bad = _code_error(args.code, required=False) or _asof_error(
        args.asof, required=False)
    if bad:
        _status_usage_error(bad)
        return 2
    if not db_path.exists():
        _status_usage_error(f"库不存在（{db_path}）；先跑 `stocklab db init`")
        return 2

    conn = connect(db_path)
    try:
        rows = cand_status.history(conn, code=args.code, limit=args.limit,
                                   asof=args.asof)
    finally:
        conn.close()
    if not rows:
        print("（无状态变更记录）")
        return 0
    for r in rows:
        print(f'{r["asof_date"]}\t{r["code"]}\t{r["status"]}\t'
              f'{r["reason"]}\t{r["actor"]}')
    return 0

