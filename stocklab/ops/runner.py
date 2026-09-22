"""补步执行器（P38）：**巡检**与**收盘链**共用的一套「把现成的 CLI 命令跑一遍」。

## 为什么要有这一层

两件事必须逐字一样，否则迟早分叉：

1. **子进程的启动方式**：`sys.executable -m stocklab.cli.main …`、`cwd=PROJECT_ROOT`、
   `PYTHONPATH=PROJECT_ROOT`（与 `labweb/app.py` 跑 `candidate run` 时同一种姿势）；
2. **停止线**：整轮预算用尽 / 单步超时 / 单步退出码 ≥ 2 各自怎么算。

## 三条停止线（都会写进返回值的 `aborted`，不静默）

- **整轮预算用尽** → 停（07 §调度约束 2：超时终止、记录告警、不继续执行）；
- **某步超时** → 停（单步超时说明链路卡住了，后面的步骤依赖它）；
- **某步退出码 ≥ 2** → 停（2 = 库不存在/用法错误这类**结构性**失败）。

退出码 **1 不停**：本仓库的约定里 1 = 「跑完了，但如实报了异常」（`session tick` 报验证冲突、
`verify pending` 有冲突都是 1）。为它中止，会让一个已知的非致命噪声把整条链的后半段全挡掉。

三条停止线在 `worst_code()` 里都算 **2**（哪怕一步都没跑成）：它们统一意味着「这一轮**没跑完**」，
而 1 的含义是「跑完了但有异常」—— 把没跑完读成 1，launchd 侧就会看到「绿了一天」
（真出过这个形状：预算用尽的链报 0，见 ERROR_DIARY #54）。

## 为什么走子进程而不是 import

「主干固定不可修改」（07 §调度约束 5）在这里是**结构性**的：本模块连调用业务函数的机会
都没有 —— 它只会拼 argv。副作用是每一步的输出格式与手工敲那一行完全一致，可直接照抄排查。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from stocklab.config import paths

#: 整轮预算（07 §调度约束 2：单轮别超过 8 分钟；卡住就记一行并停）。
DEFAULT_TIMEOUT_S = 480.0

#: 单条补步留多少字符进 JSON（够定位问题即可，不把整份报告塞进摘要）。
TAIL_CHARS = 800

EXIT_OK = 0
EXIT_ANOMALY = 1
EXIT_BLOCKED = 2

#: `ingest actions` 的回看天数（覆盖新除权事件；全历史不在日链里跑）。
ACTIONS_LOOKBACK_DAYS = 400


@dataclass(frozen=True)
class Step:
    """一条补步 = 一条现成的 CLI 命令 + 它接不接受 `--db`。"""

    name: str
    args: tuple[str, ...]
    #: 采集类命令（`ingest *`）**不接受** `--db`，固定写 `paths.DB_PATH`。
    supports_db: bool
    why: str


def tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= TAIL_CHARS else "…" + text[-TAIL_CHARS:]


def build_argv(step: Step, *, db_path: Path, asof: str | None = None,
               action_start: str | None = None) -> list[str]:
    """步骤 → 一条 argv（**不经 shell**：没有任何字符串被拼进 shell）。"""
    args = [a.format(db=str(db_path), asof=asof or "", action_start=action_start or "")
            for a in step.args]
    if step.supports_db:
        args += ["--db", str(db_path)]
    return [sys.executable, "-m", "stocklab.cli.main", *args]


def default_runner(step: Step, argv: list[str], timeout: float) -> dict:
    """默认执行器：子进程 + 预算（`timeout` = 本步还能用多少秒）。"""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=str(paths.PROJECT_ROOT), capture_output=True, text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONPATH": str(paths.PROJECT_ROOT)})
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timeout": True,
                "duration_s": round(time.monotonic() - started, 3),
                "stdout_tail": "", "stderr_tail": ""}
    except OSError as exc:                     # 解释器都起不来
        return {"exit_code": None, "timeout": False, "error": str(exc),
                "duration_s": round(time.monotonic() - started, 3),
                "stdout_tail": "", "stderr_tail": ""}
    return {"exit_code": proc.returncode, "timeout": False,
            "duration_s": round(time.monotonic() - started, 3),
            "stdout_tail": tail(proc.stdout), "stderr_tail": tail(proc.stderr)}


def actions_start(now: str, *, days: int = ACTIONS_LOOKBACK_DAYS) -> str:
    """`ingest actions --start` 的回看起点 = `now - 400d`（不是写死的日子）。"""
    return (date.fromisoformat(now[:10]) - timedelta(days=days)).isoformat()


def run_steps(steps: list[str], *, registry: dict[str, Step], runner,
              db_path: Path, now: str, timeout_s: float,
              asof: str | None = None) -> tuple[list[dict], dict | None]:
    """按给定顺序跑步骤；返回 `(每步结果, 中止原因)`。

    `registry` 由调用方给（巡检与收盘链各有一张表），本函数只管跑。
    """
    start = actions_start(now)
    deadline = time.monotonic() + float(timeout_s)
    out: list[dict] = []
    for name in steps:
        step = registry[name]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out, {"kind": "budget_exhausted", "limit_s": timeout_s,
                         "note": "整轮预算用尽 → 停在这里，下轮幂等重跑兜底"}
        argv = build_argv(step, db_path=db_path, asof=asof, action_start=start)
        res = runner(step, argv, remaining)
        out.append({"name": name, "why": step.why, "args": argv[3:], **res})
        if res.get("timeout"):
            return out, {"kind": "step_timeout", "step": name,
                         "limit_s": round(remaining, 3)}
        code = res.get("exit_code")
        if code is None:
            return out, {"kind": "step_error", "step": name,
                         "detail": res.get("error", "子进程没跑起来")}
        if code >= EXIT_BLOCKED:
            return out, {"kind": "step_fatal", "step": name, "exit_code": code}
    return out, None


def cross_db_refusal(db_path: Path | str, steps: list[str],
                     registry: dict[str, Step], *, what: str) -> str | None:
    """跨库守卫（模块 docstring 第 4 条）：计划里含「不接受 `--db`」的步骤而库不是
    默认库 → 返回拒绝理由；否则 `None`。

    采集类命令（`ingest *`）**不接受** `--db`（固定写 `paths.DB_PATH`），照跑就会把
    数据写进没打算写的库 —— 而这种错不会报警，它只会让「另一个库看起来也有数据了」。
    """
    if Path(db_path).resolve() == paths.DB_PATH.resolve():
        return None
    blocked = [name for name in steps if not registry[name].supports_db]
    if not blocked:
        return None
    return (f"`{what}` 拒绝在非默认库（{db_path}）上补步：计划里的 "
            f"{'、'.join(blocked)} 不接受 `--db`（固定写 {paths.DB_PATH}）。"
            "要么用默认库跑，要么单独手工跑那几条命令。")


def worst_code(steps_out: list[dict], aborted: dict | None) -> int:
    """补步结果 → 退出码（2 > 1 > 0）。

    三条停止线（预算用尽 / 单步超时 / 单步 ≥2）一律算 **2**：它们都是「这一轮没跑完」，
    与「跑完了但如实报了异常」（1）不是同一句话。**预算用尽时可能一步都没跑成**，
    这时若按「没有非 0 步骤」给 0，整条链就会以绿码收场（ERROR_DIARY #54）。
    """
    fatal = any(s.get("exit_code") is None or s["exit_code"] >= EXIT_BLOCKED
                for s in steps_out)
    soft = any(s.get("exit_code") == EXIT_ANOMALY for s in steps_out)
    if (aborted or {}).get("kind") in ("budget_exhausted", "step_timeout",
                                      "step_error", "step_fatal"):
        fatal = True
    return EXIT_BLOCKED if fatal else (EXIT_ANOMALY if soft else EXIT_OK)
