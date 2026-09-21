"""`candidate` 子命令：候选池主流程（模块1）。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from stocklab.candidate.report import write_report_file
from stocklab.candidate.run import recommend_optimization, run_candidate
from stocklab.config import paths
from stocklab.store.db import connect
from stocklab.store.migrate import ensure_schema


def cmd_candidate_run(args) -> int:
    db_path = Path(args.db) if args.db else paths.DB_PATH
    ensure_schema(db_path)
    conn = connect(db_path)
    try:
        now = args.now or datetime.now(timezone.utc).isoformat()
        result = run_candidate(conn, asof=args.asof, run_kind=args.run_kind,
                               now=now)
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
