#!/usr/bin/env python
"""一次性录制沪深300指数日线 fixture（评审 B4 / B3）。

用法:
    .venv/bin/python scripts/record_calendar_fixture.py

产物（提交进仓库，供离线测试回放）:
    tests/fixtures/index_bars_sh000300.raw.json       原始响应字节（sha256 可校验）
    tests/fixtures/index_bars_sh000300.calendar.json  归一化日期集合 + 溯源元数据

注意：这是**手工运行**的录制工具，不是定时任务；项目禁止 cron/守护进程（ADR-001 D-05）。
交易日历的唯一来源是这里录下的指数日线日期集合。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.config.paths import FIXTURE_DIR  # noqa: E402
from stocklab.config.universe import assert_host_allowed  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
CODE = "sh000300"
NAME = "沪深300"
URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
PARAMS = {"param": f"{CODE},day,,,900,"}
HEADERS = {"User-Agent": "Mozilla/5.0 (stocklab fixture recorder)"}


def extract_dates(payload: dict, code: str) -> list[str]:
    """从腾讯日K响应中取出日期列（升序去重）。"""
    node = payload["data"][code]
    rows = node.get("day") or node.get("qfqday") or []
    if not rows:
        raise RuntimeError(f"响应中没有日K数据: {code}")
    return sorted({row[0] for row in rows})


def main() -> int:
    assert_host_allowed(f"{URL}?param={PARAMS['param']}")  # R13：白名单校验
    resp = requests.get(URL, params=PARAMS, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    body = resp.content
    if not body.strip():
        raise RuntimeError("空响应（可能被限流），未写入 fixture")

    payload = json.loads(body.decode("utf-8"))
    dates = extract_dates(payload, CODE)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = FIXTURE_DIR / f"index_bars_{CODE}.raw.json"
    meta_path = FIXTURE_DIR / f"index_bars_{CODE}.calendar.json"
    raw_path.write_bytes(body)
    meta_path.write_text(
        json.dumps(
            {
                "source": "tencent_kline",
                "code": CODE,
                "name": NAME,
                "url": URL,
                "params": PARAMS,
                "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "raw_file": raw_path.name,
                "raw_sha256": hashlib.sha256(body).hexdigest(),
                "n_bars": len(dates),
                "first_date": dates[0],
                "last_date": dates[-1],
                "dates": dates,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"✅ 已录制 {len(dates)} 个交易日: {dates[0]} ~ {dates[-1]}")
    print(f"   {raw_path}")
    print(f"   {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
