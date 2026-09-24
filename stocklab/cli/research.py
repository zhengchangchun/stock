"""`research` 子命令：离线只读研究命令（P60 起）。

## 只读的落法

连库一律 `sqlite3.connect(f"file:{db}?mode=ro", uri=True)`，**不**调
`ensure_schema`（那会写库）、**不**用 `store.db.connect`（它会开 WAL/写 PRAGMA
并且允许写）。产物只落 `reports/`。

## fail-closed

`--prereg` 的预注册与命令行实参不一致 / 文件缺失 / 没有 json 块 → exit 2、
**零输出、不跑回放**。检查全部发生在打开库之前，所以连读都不读。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.candidate import pools as candidate_pools
from stocklab.config import paths
from stocklab.plugin import sandbox
from stocklab.research import xsec

TZ = ZoneInfo("Asia/Shanghai")


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def open_read_only(db_path: Path) -> sqlite3.Connection:
    """真库**只读**连接（`mode=ro`）。库不存在 / 打不开 → 抛 `sqlite3.Error`。"""
    if not Path(db_path).is_file():
        raise sqlite3.OperationalError(f"库不存在：{db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_research_xsec_topn(args) -> int:
    # ── ① 命令行实参（不碰库） ───────────────────────────────────────
    if args.pool != xsec.ONLY_POOL:
        print(f"--pool 只能是 {xsec.ONLY_POOL!r}（收到 {args.pool!r}）："
              f"mid 需 32.6 年、long 需 97.9 年才够 "
              f"{sandbox.MIN_VALID_PERIODS} 个验证周期",
              file=sys.stderr)
        return 2
    if args.start < xsec.MIN_START:
        print(f"--start 不得早于 {xsec.MIN_START}（收到 {args.start!r}）："
              "窗口即结论，短池在 3 年窗上会翻符号", file=sys.stderr)
        return 2

    # ── ② 预注册（纯文件 IO） ────────────────────────────────────────
    prereg_path = Path(args.prereg)
    try:
        data, _sha = xsec.load_prereg(prereg_path)
        xsec.validate_prereg(data, pool=args.pool, start=args.start,
                             topn=candidate_pools.POOL_TOPN[args.pool],
                             universe=getattr(args, "universe", None))
    except xsec.PreregError as exc:
        print(f"预注册校验失败：{exc}", file=sys.stderr)
        return 2

    end = args.end or _today()
    out_dir = Path(args.out) if args.out else xsec.default_out_dir()
    db_path = Path(args.db) if args.db else paths.DB_PATH

    # ── ③ 只读开库 → ④ 跑 → ⑤ 落盘 ─────────────────────────────────
    try:
        conn = open_read_only(db_path)
    except sqlite3.Error as exc:
        print(f"打不开库（只读）：{exc}", file=sys.stderr)
        return 2
    try:
        try:
            report = xsec.run_xsec_topn(
                conn, pool=args.pool, start=args.start, end=end,
                prereg_path=prereg_path, arm=args.arm,
                universe=getattr(args, "universe", None))
        except xsec.PreregError as exc:
            print(f"预注册校验失败：{exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    json_path, md_path = xsec.write_report(report, out_dir)
    d = report.get("delta")
    print(f"experiment={report['experiment']} pool={report['pool']} "
          f"{report['start']}~{report['end']} "
          f"marks={report['n_marks']} periods={report['n_periods']} "
          f"elapsed={report['elapsed_s']:.1f}s")
    if d is not None:
        print(f"verdict={d['verdict']} n_validate={d['n_validate']}")
        print(d["note"])
    print(f"json={json_path}")
    print(f"md={md_path}")
    print(xsec.summary_line(report))
    return 0
