"""把种子标的的日K 分页回溯到**上市首日**（联网，手工运行，不属于测试）。

    .venv/bin/python scripts/extend_history.py [--db data/stocklab.db]

## 为什么要往更早拉

沙盒回放的观测单元是**调仓周期**，门槛 120 个周期。60 日调仓要凑够
120 个验证周期需约 24000 个交易日 —— 历史越深，越可能跨过门槛。

## ADR-003 的坑（必须遵守）

腾讯 qfq 上限 800 且 **801–2000 区间会静默降级回 640 且不报错** →
只采**不复权**（`adj_mode='none'`）；写入口对 `adj_mode != 'none'` 早已
硬拒绝，保持。用 `end` 锚点分页（`beg` 被服务端忽略）。

## fail-closed

翻页能力已内置在 `data/fetch.py::fetch_daily_bars`（docstring：
「翻页锚点从 `end` 开始，逐页向前推到返回空页或超出 `max_pages`」）——
本脚本不另写翻页，只把 `start` 放到足够早（1990-01-01），让它翻到源站
返回空页为止。翻页过程中任一页解析为 0 行但**未到最早可得日** → 抛错，
不静默截断。采完打印**每只的最早期与行数**，供报告核对是否真的回溯到
了上市首日。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.candidate.seeds import SEED_UNIVERSE        # noqa: E402
from stocklab.config import paths                         # noqa: E402
from stocklab.data.fetch import fetch_daily_bars          # noqa: E402
from stocklab.data.ingest import ingest_daily_bars        # noqa: E402
from stocklab.store.db import connect                     # noqa: E402
from stocklab.store.migrate import init_db                # noqa: E402

#: A 股最早开市年份；设为翻页的 start 下界，确保服务端翻到上市首日自然返回空页为止。
EARLIEST_START = "1990-01-01"


def extend_one(conn, client, code: str, *, now: str) -> tuple[str, int]:
    """把一只标的的历史拉到最早可得，返回 `(最早期, 新增行数)`。

    分页能力**已内置**在 `data/fetch.py::fetch_daily_bars`（docstring：
    「翻页锚点从 `end` 开始，逐页向前推到返回空页或超出 `max_pages`」），
    所以这里**不另写翻页** —— 只是把 `start` 放到足够早（1990-01-01，
    A 股最早的开市年），让它一直翻到源站返回空页为止。

    ADR-003：`adj=""`（不复权）是唯一允许的口径；腾讯 qfq 在 801–2000
    区间会**静默降级**，所以绝不能传 `adj="qfq"`。

    参数
    ----
    conn    : 已打开的 SQLite 连接（instruments 表必须已有该 code）。
    client  : get_text 接口的实现（生产用 HttpClient，测试注入假 client）。
    code    : 6 位代码（如 "000333"）；必须在 SEED_UNIVERSE 里，否则抛 StopIteration。
    now     : ISO 8601 时间戳字符串，用作入库的 created_at / fetched_at。
    """
    # StopIteration 向外传播：unknown code 是调用方错误，不应静默
    inst = next(i for i in SEED_UNIVERSE if i.code == code)

    # end 取 now 的日期部分（YYYY-MM-DD）
    end_date = now[:10]

    bars = fetch_daily_bars(
        client,
        code=inst.tencent_code,
        start=EARLIEST_START,
        end=end_date,
        adj="",                 # 不复权（ADR-003 铁律）
    )

    # fetch callable for ingest: takes 6-digit code, returns pre-fetched bars
    def _fetch(inst_code: str) -> list:
        return bars if inst_code == code else []

    report = ingest_daily_bars(
        conn,
        client,
        None,       # cache — safe: ingest_daily_bars never reads it when fetch= is injected
        [inst],
        start=EARLIEST_START,
        end=end_date,
        now=now,
        fetch=_fetch,
    )

    earliest = min((b.date for b in bars), default="")
    return earliest, report.bars_written


def _today() -> str:
    return datetime.date.today().isoformat()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把种子标的日K 分页回溯到上市首日（联网，手工运行）")
    ap.add_argument("--db", default=str(paths.DB_PATH))
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        init_db(db_path)

    conn = connect(db_path)
    from stocklab.data.http import HttpClient

    client = HttpClient()
    now = _today() + "T00:00:00+08:00"

    results: list[tuple[str, str, int]] = []
    errors: list[tuple[str, Exception]] = []

    for inst in SEED_UNIVERSE:
        try:
            earliest, written = extend_one(conn, client, inst.code, now=now)
            results.append((inst.code, earliest, written))
            print(f"  {inst.code} {inst.name}: earliest={earliest}, written={written}")
        except Exception as exc:                       # noqa: BLE001
            errors.append((inst.code, exc))
            print(f"  {inst.code} {inst.name}: ERROR {exc}")

    print()
    print(f"完成: {len(results)} 只成功，{len(errors)} 只失败")
    if errors:
        print("失败的标的：", [c for c, _ in errors])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
