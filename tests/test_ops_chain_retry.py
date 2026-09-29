"""P100：收盘链／月度链的采集步重试（2026-09-28 生产事故的护栏）。

那天 15:30 那一刻 DNS 挂了 ⇒ `ingest_index` exit 1 ⇒ **交易日历没有前滚**（`ingest
index` 是日历唯一的来源）⇒ `predict run` 判非交易日 ⇒ 下班链停在 9/14 步，那一天的
预测／验证／复盘／AI 决策全部丢掉，次日自愈也补不回那一天。本站要修的不是「DNS 会挂」，
而是「**一次瞬时失败就判死一整天、整条链一次重试都没有**」。

主张（每条对应一处实际损失）：

1. 缺省 `retry_plan=None` ⇒ **一次都不多重试**，结果里一个新键都没有 —— 既有调用方
   （巡检、全部旧用例）逐字节不变（L2）；
2. 失败一次、第二次成功 ⇒ 该步 `exit_code=0`／`attempts=2`／`retried=True`／
   `exit_codes=[1,0]`，整链**不**因此变红（L5／L6）；
3. 三次全失败 ⇒ `exit_code` 取最后一次、`attempts=3`、`exit_codes=[1,1,1]`，再按
   既有停止线收尾（1 继续、2 断链）；
4. 计划里**没有**的步骤（`session_tick` 这类）失败不重试；计划里写 `1` 等价于不重试；
5. 预算装不下这次等待（`remaining <= 0` 或 `retry_delay_s >= remaining`）⇒ 不重试，
   按 `budget_exhausted` 收尾（L4：sleep 不能把整轮拖过预算）；
6. `timeout` 不重试（§1：超时说明预算／挂死，重试只会更糟）；
7. 两次尝试之间 `sleep(RETRY_DELAY_S)`，测试里打桩隔离（**不许真睡 15 s**）；
8. `ops close` 端到端：回执里有 `retried_steps` 与 `step_retried` anomaly，
   日历没前滚时点名因果（T3），且全程不起真子进程、不碰真库。

测试全程走假执行器、不联网、不真睡。凡涉及 deadline 的用例用 `FakeClock`
（`monotonic` 由测试推进）—— 拿真表当真钟会让「预算不够」这类断言变成看运气。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from stocklab.config import paths
from stocklab.ops import chain
from stocklab.ops.runner import (RETRY_ATTEMPTS, RETRY_DELAY_S, Step,
                                 run_steps, worst_code)
from stocklab.store.db import connect
from tests.test_ops_close import CLOSE_NOW, TODAY, _green

NOW = CLOSE_NOW
#: 假采集步：`ingest` 是「可重试」的判据（链侧从 argv 第一段取，不手抄清单）。
STEPS = (
    Step("net_index", ("ingest", "index"), False, "假采集步（幂等、联网络）"),
    Step("net_bars", ("ingest", "bars", "--days", "30"), False, "假采集步"),
    Step("session_tick", ("session", "tick"), True, "假会话步（不可重试）"),
)
REG = {s.name: s for s in STEPS}
PLAN = {"net_index": RETRY_ATTEMPTS, "net_bars": RETRY_ATTEMPTS}
#: `build_argv` 只把它拼成字符串（`--db` 那一段），**不开文件** —— 所以这个路径
#: 不存在也没关系，而「测试不碰任何库」这件事由此是结构性的。
DB = Path("/tmp/p100-never-touched.db")


class ScriptedRunner:
    """按「第几次尝试」给退出码的假执行器（**绝不真起子进程**）。

    `FakeRunner` 只能表达「一个 step 一个码」，而重试的形状恰恰是「同一步的不同尝试给
    不同的码」—— 所以这里按下标取脚本；脚本用完就沿用最后一个码（幂等重跑的现实形态：
    ``[1, 0]`` 表示第一次失败、之后都好）。
    """

    def __init__(self, script=None, *, on_call=None, not_started=(),
                 timeout_steps=()):
        self.calls: list[dict] = []
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.on_call = on_call
        self.not_started = set(not_started)
        self.timeout_steps = set(timeout_steps)

    def __call__(self, step, argv, timeout):
        n = sum(1 for c in self.calls if c["step"] == step.name)
        self.calls.append({"step": step.name, "argv": list(argv),
                           "timeout": timeout, "attempt": n + 1})
        if self.on_call:
            self.on_call(step, argv)
        if step.name in self.not_started:
            return {"exit_code": None, "timeout": False,
                    "error": "解释器起不来（假）", "duration_s": 1.0,
                    "stdout_tail": "", "stderr_tail": ""}
        if step.name in self.timeout_steps:
            return {"exit_code": None, "timeout": True, "duration_s": 1.0,
                    "stdout_tail": "", "stderr_tail": ""}
        codes = self.script.get(step.name) or [0]
        code = codes[n] if n < len(codes) else codes[-1]
        return {"exit_code": code, "timeout": False, "duration_s": 1.0,
                "stdout_tail": f"{step.name} ok",
                "stderr_tail": f"{step.name} exit {code}"}

    def calls_of(self, name: str) -> list[dict]:
        return [c for c in self.calls if c["step"] == name]


class FakeClock:
    """假时钟：`monotonic` 由测试推进、`sleep` 只记账 —— 预算判定完全确定。"""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def slept(monkeypatch):
    """只把 `time.sleep` 打桩（**不推时钟**）：记下每次的秒数，一次都不真睡。"""
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(seconds))
    return calls


@pytest.fixture
def clock(monkeypatch):
    """`time.monotonic` ＋ `time.sleep` 都换成假时钟（sleep 会推它）。"""
    c = FakeClock()
    monkeypatch.setattr(time, "monotonic", c.monotonic)
    monkeypatch.setattr(time, "sleep", c.sleep)
    return c


def _run(runner, *, steps=("net_index", "net_bars", "session_tick"), plan=PLAN,
         timeout_s=900.0, delay=RETRY_DELAY_S, now=NOW):
    return run_steps(list(steps), registry=REG, runner=runner, db_path=DB,
                     now=now, timeout_s=timeout_s, retry_plan=plan,
                     retry_delay_s=delay)


# ---------- 组 1：缺省路径逐字节不变（L2） ----------

def test_default_plan_none_retries_nothing_and_adds_no_keys(slept):
    """缺省 `retry_plan=None`：**失败**也每步只调一次、不 sleep、结果无新键。

    这是本档最重要的一条护栏：巡逻与全部旧用例都走缺省路径，重试只能是**链自己
    声明**出来的，不能是执行器的新默认。
    """
    runner = ScriptedRunner({"net_index": [1, 0]})      # 第 2 次本来会成功
    out, aborted = _run(runner, plan=None)

    assert [c["step"] for c in runner.calls] == ["net_index", "net_bars",
                                                 "session_tick"]
    assert out[0]["exit_code"] == 1                     # 没有第二次尝试
    assert set(out[0]) == {
        "name", "why", "blocking", "args", "exit_code", "timeout",
        "duration_s", "stdout_tail", "stderr_tail"}
    assert aborted is None and worst_code(out, aborted) == 1
    assert slept == []                                  # 一次都没等


def test_plan_with_one_attempt_is_the_same_as_no_plan(slept):
    runner = ScriptedRunner({"net_index": [1, 0]})
    out, aborted = _run(runner, steps=("net_index",), plan={"net_index": 1})
    assert len(runner.calls_of("net_index")) == 1
    assert out[0]["exit_codes"] == [1] and out[0]["retried"] is False
    assert slept == []


# ---------- 组 2：失败一次、第二次成功 ----------

def test_first_attempt_fails_then_succeeds_and_the_chain_stays_green(slept):
    runner = ScriptedRunner({"net_index": [1, 0]})
    out, aborted = _run(runner)

    assert [c["attempt"] for c in runner.calls_of("net_index")] == [1, 2]
    step = out[0]
    assert step["exit_code"] == 0                       # 取**最后一次**
    assert step["attempts"] == 2
    assert step["retried"] is True
    assert step["exit_codes"] == [1, 0]
    # `duration_s` = 全部尝试的累计（每次 1.0），不是最后一次
    assert step["duration_s"] == 2.0
    assert aborted is None and worst_code(out, aborted) == 0
    assert slept == [RETRY_DELAY_S]
    # 没被重试的步：新键照出，但 attempts=1 / retried=False
    assert (out[1]["attempts"], out[1]["retried"]) == (1, False)


# ---------- 组 3：三次全失败 ----------

def test_three_soft_failures_keep_the_last_code_and_do_not_stop_the_chain(slept):
    """exit 1 三次：`exit_code` 是最后一次的值，且 1 **不停链**（既有停止线）。"""
    runner = ScriptedRunner({"net_index": [1, 1, 1]})
    out, aborted = _run(runner)

    assert len(runner.calls_of("net_index")) == 3
    assert (out[0]["attempts"], out[0]["exit_codes"], out[0]["exit_code"]) == (
        3, [1, 1, 1], 1)
    assert aborted is None
    assert worst_code(out, aborted) == 1
    assert slept == [RETRY_DELAY_S, RETRY_DELAY_S]
    assert [c["step"] for c in runner.calls][-1] == "session_tick"   # 后面的步照跑


def test_three_fatal_failures_stop_at_the_existing_stop_line(slept):
    """exit 2 三次：按既有 `step_fatal` 断链，链的退出码 2。"""
    runner = ScriptedRunner({"net_index": [2, 2, 2]})
    out, aborted = _run(runner)

    assert [c["step"] for c in runner.calls] == ["net_index"] * 3
    assert aborted == {"kind": "step_fatal", "step": "net_index", "exit_code": 2}
    assert out[0]["exit_codes"] == [2, 2, 2]
    assert worst_code(out, aborted) == 2


# ---------- 组 4：计划外的步骤不重试 ----------

def test_a_step_outside_the_plan_is_never_retried(slept):
    runner = ScriptedRunner({"session_tick": [1, 0]})
    out, aborted = _run(runner, steps=("session_tick",))

    assert len(runner.calls_of("session_tick")) == 1
    assert (out[0]["attempts"], out[0]["retried"], out[0]["exit_codes"]) == (
        1, False, [1])
    assert slept == []


# ---------- 组 5：deadline / 预算 ----------

def test_budget_spent_by_the_first_attempt_does_not_retry(clock):
    """第一次尝试就把预算吃光（`remaining <= 0`）⇒ 不重试、按 `budget_exhausted` 收尾。"""
    runner = ScriptedRunner({"net_index": [1, 0]},
                            on_call=lambda *_: clock.advance(0.7))
    out, aborted = _run(runner, steps=("net_index",), timeout_s=0.5)

    assert len(runner.calls_of("net_index")) == 1
    assert out[0]["exit_codes"] == [1]                  # 这一步的结果如实记下
    assert aborted["kind"] == "budget_exhausted"
    assert aborted["step"] == "net_index"
    assert clock.slept == []                            # 一次都没等
    assert worst_code(out, aborted) == 2                # 没跑完 = 2，不许报绿


def test_a_wait_that_does_not_fit_the_budget_is_not_taken(clock):
    """预算还剩一点、但不够 15 s 的等待 ⇒ 不睡（sleep 不能把整轮拖过预算，L4）。"""
    runner = ScriptedRunner({"net_index": [1, 0]},
                            on_call=lambda *_: clock.advance(0.45))
    out, aborted = _run(runner, steps=("net_index",), timeout_s=0.5)

    assert len(runner.calls_of("net_index")) == 1
    assert aborted["kind"] == "budget_exhausted"
    assert clock.slept == []


def test_timeout_is_not_retried(slept):
    """`timeout` 不是「失败形状」：超时说明预算／挂死，重试只会更糟（§1）。"""
    runner = ScriptedRunner(timeout_steps=("net_index",))
    out, aborted = _run(runner, steps=("net_index",))

    assert len(runner.calls_of("net_index")) == 1
    assert out[0]["attempts"] == 1 and out[0]["exit_codes"] == [None]
    assert aborted["kind"] == "step_timeout"
    assert slept == []


# ---------- 组 6：子进程没起来 ----------

def test_a_child_that_never_starts_is_retried_then_ends_as_step_error(slept):
    """`exit_code is None`（子进程没起来）按 §1 算**失败形状** ⇒ 会重试。

    三次之后仍走既有 `step_error`（L1：不改判据）。取舍与理由见任务书 §7.3 与 ADR-046：
    「起不来」在 15:30 那种并发压力下同样是**可能瞬时**的（fork／FD 耗尽），而重试
    的代价被 deadline 与尝试上限双重封顶；真要一直起不来，三次之后照旧如实报断链。
    """
    runner = ScriptedRunner(not_started=("net_index",))
    out, aborted = _run(runner, steps=("net_index",))

    assert len(runner.calls_of("net_index")) == RETRY_ATTEMPTS
    assert out[0]["exit_codes"] == [None] * RETRY_ATTEMPTS
    assert out[0]["exit_code"] is None
    assert aborted["kind"] == "step_error"
    assert worst_code(out, aborted) == 2


# ---------- 组 7：间隔常量与打桩 ----------

def test_the_two_constants_are_the_locked_ones():
    assert RETRY_DELAY_S == 15.0
    assert RETRY_ATTEMPTS == 3


def test_sleep_uses_the_configured_delay(slept):
    runner = ScriptedRunner({"net_index": [1, 0]})
    _run(runner, steps=("net_index",), delay=0.25)
    assert slept == [0.25]


# ---------- 组 8：ops close 端到端（回执 ＋ 不碰真库／不起子进程） ----------

def test_close_receipt_shows_the_retry_then_stays_green(tmp_path, monkeypatch,
                                                        slept):
    """`ingest_index` 第一次 exit 1、第二次 0 ⇒ 整链绿，但回执**看得出**它重试过。"""
    db = _green(tmp_path)
    monkeypatch.setattr(paths, "DB_PATH", db)      # 跨库守卫那条线要求「默认库」
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    runner = ScriptedRunner({"ingest_index": [1, 0]})

    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              backup_dir=tmp_path / "backups",
                              report_dir=tmp_path / "reports")

    assert payload["exit_code"] == 0 and payload["ok"] is True
    assert payload["retried_steps"] == [
        {"name": "ingest_index", "attempts": 2, "exit_codes": [1, 0]}]
    retried = [a for a in payload["anomalies"] if a["kind"] == "step_retried"]
    assert [a["step"] for a in retried] == ["ingest_index"]
    assert "ingest_index" in retried[0]["detail"]
    assert "1" in retried[0]["detail"] and "0" in retried[0]["detail"]
    # 重试后成功的步在 steps 里 exit_code=0（既有 `bad=` 语义不变）
    assert payload["steps"][0]["name"] == "ingest_index"
    assert payload["steps"][0]["exit_code"] == 0
    assert chain.summary_line(payload).endswith(" retried=1")


def test_a_close_run_without_retries_says_nothing_extra(tmp_path, monkeypatch,
                                                        slept):
    """一次都没重试 ⇒ `retried_steps == []`、没有 `step_retried`、摘要行不加尾巴。"""
    db = _green(tmp_path)
    monkeypatch.setattr(paths, "DB_PATH", db)
    monkeypatch.setattr(subprocess, "run", _no_subprocess)

    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=ScriptedRunner(),
                              backup_dir=tmp_path / "backups",
                              report_dir=tmp_path / "reports")

    assert payload["exit_code"] == 0
    assert payload["retried_steps"] == []
    assert "step_retried" not in [a["kind"] for a in payload["anomalies"]]
    assert "retried=" not in chain.summary_line(payload)


def test_close_receipt_names_the_calendar_cause(tmp_path, monkeypatch, slept):
    """T3.2：asof 不在交易日历里 ⇒ 回执点名「`ingest_index` 是日历的唯一来源」。

    与 2026-09-28 同形：日历 max 停在 09-24 而 asof 是 09-28。这里把夹具日历的**当天**
    那行删掉（模拟「`ingest index` 没跑成」），体检侧的说法是 `calendar_not_covered`。
    """
    db = _green(tmp_path)
    monkeypatch.setattr(paths, "DB_PATH", db)
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    c = connect(db)
    c.execute("DELETE FROM trading_calendar WHERE date=?", (TODAY,))
    c.commit()
    c.close()

    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=ScriptedRunner(),
                              backup_dir=tmp_path / "backups",
                              report_dir=tmp_path / "reports")

    assert payload["session_day"] == {"is_trading_day": None,
                                      "why": "calendar_not_covered"}
    causes = [a for a in payload["anomalies"]
              if a["kind"] == "calendar_not_forward_rolled"]
    assert len(causes) == 1
    detail = causes[0]["detail"]
    assert "ingest_index" in detail and "唯一" in detail
    assert "predict_run" in detail
    assert causes[0]["date"] == TODAY


def _no_subprocess(*_args, **_kwargs):
    """`subprocess.run` 的替身：真被调到就是「起了真子进程」—— 直接判失败。"""
    pytest.fail("这一轮不许起真子进程（假执行器已经注入）")
