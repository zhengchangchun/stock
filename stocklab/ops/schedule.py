"""`ops schedule`（P38）：把时钟交给系统（launchd），项目只负责 plist。

## 为什么调度不写在项目里

用户 2026-09-22 拍板：定时任务不用 nanobot cron，也不在项目里写常驻进程 ——
**时钟交给操作系统**，项目只回答「跑什么」（`ops/chain.py` / `ops/patrol.py`）。
三件事因此有了各自的唯一责任人：

| 问题 | 谁回答 | 在哪 |
|---|---|---|
| 什么时候跑 | launchd（macOS 自带） | `~/Library/LaunchAgents/com.stocklab.*.plist` |
| 跑什么、按什么顺序 | 项目 | `ops/chain.py` / `ops/patrol.py` |
| 跑了什么结果 | 项目 | `reports/ops/latest-*.json` + `job_runs` |

好处是**可测**：plist 是纯文本、由 `render_plist()` 生成（纯函数，测试直接断言内容），
安装只是把系统命令跑一遍（`runner` 可注入，测试不起真进程）。

## 为什么是 `StartCalendarInterval` 数组

launchd 没有 cron 表达式，`StartCalendarInterval` **一次只能描述一个（集）触发点**；
给一个数组则是「命中任一条即触发」（缺的键 = 通配）。所以要表达
「工作日 09:00–14:30 每 30 分钟」必须展开成 `12 槽 × 5 天 = 60` 条 —— 数字大，
但语义与 `*/30 9-14 * * 1-5` 逐点等价，且**没有 shell 参与**。

**为什么去掉 15:30**（P42）：15:30 那一槽与 `close` **撞在同一分钟**，而两个任务都会
真起子进程写同一个 SQLite 库（`patrol --fix` 补步、`close` 跑整条 12 步链）→ 撞
`database is locked` → 某步非 0 → 收盘链**断链（exit 2）**，留下最难收拾的当日半截
状态。收盘后补缺口的职责本来就归 `close`（15:30 整条链），那一槽的巡逻是冗余的。
`tests/test_ops_close.py::test_no_two_jobs_fire_at_the_same_moment` 把这条钉死成
通用护栏：将来再加任务，撞点当场红。

**为什么连 15:00 也去掉**（P46）：15:30 只是撞点，15:00 是**更贵的错**。15:00 那一刻
`is_trade_date_closed(今天, now)` 已经为真 ⇒ `latest_closed_session` 变成**今天**
⇒ `patrol` 会补 `predict run --asof 今天`，而当天的 K 线只能来自 `ingest bars`，
那是 15:30 收盘链的第一步 —— 于是写出来的是**基于盘中半截 bar 的当日 LIVE 预测**。
实测 2026-09-22 15:00 那一槽就这么落下 17 条预测（`created_at=15:05:22`），15:30 收盘链
把当日 K 线覆盖成真收盘价后重算，17 条载荷全变 → append-only 全部拒绝 → 收盘链 exit 1，
当天预测永久停在**收盘前口径**（`docs/errors/ERROR_DIARY.md` #60）。

所以这里的边界不是「避开撞点」而是**职责边界**：**盘中归 patrol，收盘后归 close**。
`patrol` 的任何一槽都必须**严格早于 15:00**，这条由
`tests/test_ops_close.py::test_patrol_plist_covers_0900_to_1430_and_no_slot_reaches_the_close`
逐槽断言（`all((H, M) < (15, 0))`），改时刻表时当场红。

`Weekday` 的取值是 launchd 的约定：`0` 与 `7` 都是周日，`1` = 周一 … `5` = 周五。

## 机器睡了怎么办

`StartCalendarInterval` 错过的触发点**会在唤醒后补跑一次**（launchd 自己合并）。
这正是要的行为：补跑的一轮是幂等的，而 `ops close` 的 asof 取「运行当天」，
所以唤醒当天补跑仍然只写当天 —— 不会因为晚了几小时就写错日期。
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from stocklab.config import paths

#: LaunchAgent 的落地目录（用户级，不需要 root）。
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

#: 三条任务的 label 前缀（反查/清理时靠它识别「是本项目的」）。
LABEL_PREFIX = "com.stocklab"

#: launchd 的星期约定：1 = 周一 … 5 = 周五。
WEEKDAYS = (1, 2, 3, 4, 5)

#: 巡检的触发时刻（`(Hour, Minute)`，共 12 槽）：09:00–14:30 每 30 分钟。
#: **任一槽严格早于 15:00** —— 15:00 之后 `latest_closed_session` 会变成「今天」，
#: 而当天 K 线要到 15:30 收盘链才定型，那时补 `predict run --asof 今天` 写的是
#: 半截 bar 的预测（见模块 docstring「为什么连 15:00 也去掉」）。
#: 15:30 那一槽还与 `close` 撞点。两条都由测试逐槽断言。
PATROL_TIMES: tuple[tuple[int, int], ...] = (
    tuple((h, m) for h in range(9, 15) for m in (0, 30))
)

#: `launchctl` 的调用超时（它只跟本机 launchd 说话，不应超过几秒）。
LAUNCHCTL_TIMEOUT_S = 20.0


def log_dir() -> Path:
    """stdout/stderr 落盘目录（`data/` 整个在 `.gitignore` 里）。

    每次调用重新算，理由同 `ops/journal.ops_report_dir()`。
    """
    return paths.DATA_DIR / "logs"


def patrol_calendar() -> tuple[dict[str, int], ...]:
    """`PATROL_TIMES` × 工作日 = 12 × 5 = **60 条**（见模块 docstring）。"""
    return tuple({"Hour": h, "Minute": m, "Weekday": d}
                 for h, m in PATROL_TIMES for d in WEEKDAYS)


@dataclass(frozen=True)
class Job:
    """一条定时任务 = label + argv + 触发点（全部由项目定义、可断言）。"""

    name: str                      # 也是 `ops <name>` 的子命令与回执里的 job_name
    label: str
    args: tuple[str, ...]          # 传给 `python -m stocklab.cli.main` 的参数
    window: str                    # 人话：什么时候跑
    why: str                       # 为什么要它
    calendar: tuple[dict[str, int], ...]

    @property
    def stdout_log(self) -> Path:
        return log_dir() / f"{self.name}.out.log"

    @property
    def stderr_log(self) -> Path:
        return log_dir() / f"{self.name}.err.log"


#: 三条任务（顺序 = 页面/`status` 的展示顺序）。
JOBS: tuple[Job, ...] = (
    Job(
        name="close", label=f"{LABEL_PREFIX}.close", args=("ops", "close"),
        window="每交易日 15:30",
        why="收盘链：库备份 → 12 步（ingest→快照→预测→验证→复盘→模拟盘）→ 事后体检",
        calendar=tuple({"Hour": 15, "Minute": 30, "Weekday": d} for d in WEEKDAYS),
    ),
    Job(
        name="patrol", label=f"{LABEL_PREFIX}.patrol",
        args=("ops", "patrol", "--fix"),
        window="工作日 09:00–14:30 每 30 分钟",
        why="巡检：7 项体检（只读）+ 缺哪步补哪步。只负责**盘中**（任一槽严格早于 "
            "15:00）—— 收盘后那一轮归 close，理由见模块 docstring",
        calendar=patrol_calendar(),
    ),
    Job(
        name="monthly", label=f"{LABEL_PREFIX}.monthly",
        args=("ops", "monthly"),
        window="每月 1 日 08:00",
        why="月度刷新：休市公告 → 长窗行情回补 → 财报复核 → 体检",
        calendar=({"Day": 1, "Hour": 8, "Minute": 0},),
    ),
)

JOB_BY_NAME: dict[str, Job] = {j.name: j for j in JOBS}
JOB_NAMES: tuple[str, ...] = tuple(j.name for j in JOBS)


def plist_path(job: Job, *, plist_dir: Path | None = None) -> Path:
    return (plist_dir or LAUNCH_AGENTS_DIR) / f"{job.label}.plist"


def build_argv(job: Job, *, python: Path | str | None = None) -> list[str]:
    return [str(python or sys.executable), "-m", "stocklab.cli.main", *job.args]


def render_plist(job: Job, *, project_root: Path | str | None = None,
                 python: Path | str | None = None,
                 logs: Path | str | None = None) -> bytes:
    """`Job` → plist 字节（**纯函数**：不读时钟、不碰文件系统）。

    `logging` 落盘路径显式给出来（launchd 自己不会创建目录，所以 `ensure_log_dir`
    负责先建）—— 否则 launchd 会静默把输出丢掉，而「日志没有内容」与
    「任务没跑」在页面上长得一样。
    """
    root = Path(project_root) if project_root else paths.PROJECT_ROOT
    log = Path(logs) if logs else log_dir()
    doc = {
        "Label": job.label,
        "ProgramArguments": build_argv(job, python=python),
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PYTHONPATH": str(root)},
        "StartCalendarInterval": list(job.calendar),
        # 不设 RunAtLoad：登录/bootout 之后不该立刻跑一条收盘链（今天可能还没收盘）。
        "RunAtLoad": False,
        "StandardOutPath": str(log / f"{job.name}.out.log"),
        "StandardErrorPath": str(log / f"{job.name}.err.log"),
    }
    return plistlib.dumps(doc, sort_keys=True)


def write_plist(job: Job, *, plist_dir: Path | None = None,
                project_root: Path | str | None = None,
                python: Path | str | None = None,
                logs: Path | str | None = None) -> Path:
    path = plist_path(job, plist_dir=plist_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    log = Path(logs) if logs else log_dir()
    log.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_plist(job, project_root=project_root, python=python,
                                  logs=log))
    return path


# ---------- launchctl ----------

def default_sys_runner(argv: list[str], timeout: float) -> dict:
    """把一条系统命令跑一遍（**只给 launchctl 用**；测试注入假 runner）。"""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timeout": True, "stdout": "", "stderr": ""}
    except OSError as exc:
        return {"exit_code": None, "timeout": False, "error": str(exc),
                "stdout": "", "stderr": ""}
    return {"exit_code": proc.returncode, "timeout": False,
            "stdout": proc.stdout, "stderr": proc.stderr}


def _domain() -> str:
    """`gui/<uid>`：LaunchAgent 属于当前登录用户。"""
    return f"gui/{os.getuid()}"


def install(job: Job, *, plist_dir: Path | None = None,
            project_root: Path | str | None = None,
            python: Path | str | None = None,
            logs: Path | str | None = None,
            runner=None) -> dict:
    """写 plist → （若已加载先 bootout）→ `launchctl bootstrap`。

    `bootout` 失败**不算错**：那条路正是「第一次安装」的常态（还没有加载过它）。
    只有 `bootstrap` 失败才让整次安装失败 —— 那时 plist 已落盘，报错里给出路径，
    便于手工重试。
    """
    runner = runner or default_sys_runner
    path = write_plist(job, plist_dir=plist_dir, project_root=project_root,
                       python=python, logs=logs)
    bootout = runner(["launchctl", "bootout", _domain(), str(path)],
                     LAUNCHCTL_TIMEOUT_S)
    boot = runner(["launchctl", "bootstrap", _domain(), str(path)],
                  LAUNCHCTL_TIMEOUT_S)
    return {"job": job.name, "label": job.label, "plist": str(path),
            "bootout_exit": bootout.get("exit_code"),
            "exit_code": boot.get("exit_code"),
            "stdout": (boot.get("stdout") or "").strip(),
            "stderr": (boot.get("stderr") or "").strip(),
            "ok": boot.get("exit_code") == 0}


def uninstall(job: Job, *, plist_dir: Path | None = None, runner=None) -> dict:
    """`launchctl bootout` + 删 plist。没加载过时 bootout 报错 → 照删，仍算成功。"""
    runner = runner or default_sys_runner
    path = plist_path(job, plist_dir=plist_dir)
    out = runner(["launchctl", "bootout", _domain(), str(path)],
                 LAUNCHCTL_TIMEOUT_S)
    existed = path.exists()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return {"job": job.name, "label": job.label, "plist": str(path),
                "exit_code": 1, "stderr": str(exc), "ok": False}
    return {"job": job.name, "label": job.label, "plist": str(path),
            "bootout_exit": out.get("exit_code"), "removed": existed,
            "exit_code": 0, "ok": True}


_LAST_EXIT = re.compile(r'"LastExitStatus"\s*=\s*(-?\d+);')
_PID = re.compile(r'"PID"\s*=\s*(\d+);')


def status(job: Job, *, plist_dir: Path | None = None, runner=None) -> dict:
    """加载了没 / 上次退出码。/ 与 `launchctl list <label>` 同源（不自己造口径）。"""
    runner = runner or default_sys_runner
    path = plist_path(job, plist_dir=plist_dir)
    out = runner(["launchctl", "list", job.label], LAUNCHCTL_TIMEOUT_S)
    text = out.get("stdout") or ""
    loaded = out.get("exit_code") == 0
    last = _LAST_EXIT.search(text)
    pid = _PID.search(text)
    return {"job": job.name, "label": job.label, "plist": str(path),
            "plist_present": path.exists(), "loaded": loaded,
            "last_exit_status": int(last.group(1)) if last else None,
            "pid": int(pid.group(1)) if pid else None,
            "window": job.window, "why": job.why,
            "stdout_log": str(job.stdout_log), "stderr_log": str(job.stderr_log),
            "detail": (out.get("stderr") or "").strip() if not loaded else ""}


def status_all(*, plist_dir: Path | None = None, runner=None) -> list[dict]:
    return [status(j, plist_dir=plist_dir, runner=runner) for j in JOBS]


def kickstart(job: Job, *, runner=None) -> dict:
    """立刻跑一次（`launchctl kickstart -k gui/<uid>/<label>`）—— 安装后的自证。"""
    runner = runner or default_sys_runner
    out = runner(["launchctl", "kickstart", "-k", f"{_domain()}/{job.label}"],
                 LAUNCHCTL_TIMEOUT_S)
    return {"job": job.name, "exit_code": out.get("exit_code"),
            "stdout": (out.get("stdout") or "").strip(),
            "stderr": (out.get("stderr") or "").strip(),
            "ok": out.get("exit_code") == 0}


__all__ = ["JOB_BY_NAME", "JOB_NAMES", "JOBS", "LABEL_PREFIX", "Job",
           "build_argv", "default_sys_runner", "install", "kickstart",
           "log_dir", "patrol_calendar", "plist_path", "render_plist", "status",
           "status_all", "uninstall", "write_plist"]
