"""`candidate` 子命令：候选池主流程（模块1）。"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from stocklab.candidate.report import write_report_file
from stocklab.candidate.run import recommend_optimization, run_candidate
from stocklab.config import paths
from stocklab.config.universes import UniverseError, resolve_universe
from stocklab.plugin import contract, guard, lifecycle, runtime
from stocklab.plugin_review import run_review
from stocklab.store.db import connect
from stocklab.store.migrate import ensure_schema


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
