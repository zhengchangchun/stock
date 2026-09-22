"""跑完的回执（P38）：每次「一次真正的运行」落两样东西。

1. `job_runs` 一行（短摘要：状态 / 起止 / detail）—— 页面拿它做**历史**；
2. `<报告根>/ops/latest-<job>.json`（完整载荷）—— 页面拿它做**最近一次的细节**；
   `report_dir` 参数是**报告根**（默认 `paths.REPORT_DIR`，即 `reports/`），
   `ops/` 这一层由本模块加 —— 见 `report_dir_of()`。

## 为什么用「文件 + 库」而不是只写库

`job_runs.detail` 是给别的消费者当短句看的（`21/21 ok, 0 issues` 那种），
把几 KB 的 JSON 塞进去会让每个读它的地方都得先判断「这行是不是 JSON」。
完整载荷放文件，库放摘要 —— 两边各自只有一个用途。

## 为什么单独开一条连接

体检走的是 `mode=ro`（结构上写不了库）。回执是**运行的回执**，不是体检的一部分，
所以它走自己的读写连接 —— 这样「只读」那句话仍然成立，而不是被回执悄悄破掉。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from stocklab.config import paths
from stocklab.store import repo
from stocklab.store.db import connect

def report_root(report_dir: Path | str | None = None) -> Path:
    """报告根目录（默认 `reports/`）。

    **每次调用重新算**，不缓成模块常量：`paths.REPORT_DIR` 是运行时属性
    （测试的 autouse 夹具会把它指到 `tmp_path`，见 ERROR_DIARY #51），
    在导入时算一次就等于把那个夹具绕过去了。
    """
    return Path(report_dir) if report_dir else paths.REPORT_DIR


def report_dir_of(report_dir: Path | str | None = None) -> Path:
    """回执目录 = **报告根目录下的 `ops/`**。

    与 ⑤ 复盘报告（`<根>/<date>-review.md`）共用同一个根：`ops close` 的
    `report_dir` 一个参数要回答两件事（去哪找复盘报告、回执写哪里），给它两个
    含义迟早出现「报告在 A、回执在 B，两边都以为对方看的是自己那一份」。
    """
    return report_root(report_dir) / "ops"


def report_path(job_name: str, report_dir: Path | str | None = None) -> Path:
    return report_dir_of(report_dir) / f"latest-{job_name}.json"


def record(*, db_path: Path | str, job_name: str, payload: dict,
           started_at: str, now: str, detail: str | None = None,
           report_dir: Path | str | None = None) -> dict:
    """写回执：JSON 载荷 + `job_runs` 一行。返回 `{report, run_id, status}`。"""
    path = report_path(job_name, report_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n", encoding="utf-8")

    status = "ok" if payload.get("ok") else "failed"
    conn = connect(db_path)
    try:
        run_id = repo.record_job(conn, job_name, status="running",
                                 started_at=started_at, detail=detail)
        repo.finish_job(conn, run_id, status=status, finished_at=now, detail=detail)
    finally:
        conn.close()
    return {"report": str(path), "run_id": run_id, "status": status}


def read_latest(job_name: str, report_dir: Path | str | None = None) -> dict | None:
    """读最近一次运行的完整载荷（没有 / 读坏了 → `None`，不炸页面）。"""
    path = report_path(job_name, report_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def seal(payload: dict, *, db_path: Path | str, job_name: str, stamp: str,
         detail: str | None = None, started_at: str | None = None,
         report_dir: Path | str | None = None) -> dict:
    """写回执并把结果塞回载荷（`payload["journal"]`）。

    回执写不进去（库只读 / 盘满）**不等于**链没跑：如实记 `journal_error` ——
    不让「记账失败」把「跑完了」改写成「跑挂了」。
    """
    try:
        payload["journal"] = record(
            db_path=db_path, job_name=job_name, payload=payload,
            started_at=started_at or stamp, now=stamp, detail=detail,
            report_dir=report_dir)
    except (sqlite3.Error, OSError) as exc:
        payload["journal_error"] = str(exc)
    return payload
