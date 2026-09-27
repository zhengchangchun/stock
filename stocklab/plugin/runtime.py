"""插桩执行器（设计文档 §6.4）：受限命名空间 `exec` + 强制超时。

## 超时只能用 signal，因此**只能在主线程调用**

`signal.setitimer` 只对主线程有效；从工作线程调用会抛 `ValueError`。
CLI 与 pytest 都跑在主线程，满足。**将来若有人想把插桩塞进线程池并行**，
必须先把执行器换成子进程方案（设计文档方案 B），否则会得到
`ValueError: signal only works in main thread` 而不是超时 —— 那种错误
比超时难查得多，因为脚本本身没问题。

## 超时之后解释器必须仍然可用

`_time_limit` 在 finally 里恢复旧的 signal handler 并关掉定时器。
不恢复的话，后续任何 `sleep`/`recv` 都可能被这个残留的 alarm 打断，
表现为「莫名其妙的行为」，极难定位。

## 资源闸门：判废 ＋ 留痕 ＋ 挡后续，**不假装能中断**（P93）

两道闸门（阈值在 `config/limits.py`，唯一真源）：

1. **执行前**：进程峰值 RSS 已达 `PLUGIN_PROCESS_RSS_LIMIT_BYTES` ⇒ 抛
   `PluginResourceError`，**一个字节的脚本都不执行**；
2. **执行后**：本次调用峰值增量 >= `PLUGIN_CALL_RSS_LIMIT_BYTES` ⇒ 该次结果
   **作废**，抛 `PluginResourceError`。

**刻意不做中途 kill。** P61 §0.6 实测：`bomb = [0] * 30000000` 的分配发生在
**单个 C 调用**里，`signal.setitimer` 只在字节码之间投递 ⇒ 拦不住。所以本档
**不声称**能阻止那一次分配；它声称的是三件能做的事 —— ① 结果作废（不把越界
的 payload 交给调用方）；② 留一条 `plugin_resource_events`（谁、什么时候、
增量多少）；③ 后续调用被**执行前**闸门挡住（进程峰值已高，下一轮连跑都不跑）。
把「不假装」写进异常消息，是为了让读到它的人不必去翻 P61 才知道边界在哪。

`PluginResourceError` 是 `PluginTimeout` 的**兄弟**（都直接继承 `Exception`），
不是父子：既有的精确 `except PluginTimeout` 点必须**同时**接住两者，否则资源
越界会从「脚本跑不出来」那条通道漏出去、被当成未知异常。

## 读数出口 `on_call`（P93 / D4）

`load_script(..., on_call=...)` 每次调用结束回调一条固定形状的读数；**不传时
行为与今天逐字相同**。`outcome` 的取值域只有 `{"ok","timeout","resource"}`
三词 —— 脚本自己抛的其它异常（契约校验、`NameError`）由既有通道报错，**不**回调：
D4 的取值域里没有第四个词，硬塞一个「其它」会让这张表变成什么都往里装。
"""

from __future__ import annotations

import signal
import time
from contextlib import contextmanager
from typing import Any, Callable

from stocklab.config import limits
from stocklab.plugin import contract, guard, resources

#: 单次调用的默认超时（秒）。够任何纯计算脚本跑完，又不会让主流程卡死。
DEFAULT_TIMEOUT_S: float = 5.0

#: 脚本可见的内建函数白名单。**刻意窄** —— 不放 `type`/`object`/`dir`
#: 之外能通向模块系统的入口。
_ALLOWED_BUILTINS: dict[str, Any] = {
    name: __builtins__[name] if isinstance(__builtins__, dict)
    else getattr(__builtins__, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
        "int", "len", "list", "map", "max", "min", "range", "round", "set",
        "sorted", "str", "sum", "tuple", "zip",
    )
}

#: `on_call` 回调的键集（D4）：`plugin_id` / `outcome` / `duration_ms` /
#: `rss_delta_bytes` / `rss_peak_bytes`。每次都整份给全 —— 少一个键的读数是
#: 「看起来正常」的空洞，比多一个键难查得多。


class PluginTimeout(Exception):
    """脚本执行超时。"""


class PluginResourceError(Exception):
    """插桩调用越过资源上限（P93）。**是 `PluginTimeout` 的兄弟**。

    **不做中途 kill、不假装能中断**：单次 C 调用不可中断（P61 §0.6）。本异常
    声称的只是「① 该次结果作废 ；② 留了一条 `plugin_resource_events`；
    ③ 后续调用被执行前闸门挡住」。
    """


@contextmanager
def _time_limit(seconds: float):
    def _on_alarm(_signum, _frame):
        raise PluginTimeout(f"脚本执行超过 {seconds} 秒，已中断")

    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *old_timer)


def _make_namespace() -> dict:
    return {"__builtins__": dict(_ALLOWED_BUILTINS), "__name__": "<plugin>"}


def _pre_gate_message(plugin_id: str, measured: int) -> str:
    return (f"插桩 {plugin_id} 未执行：进程峰值 RSS {measured} 字节 已达/超过上限 "
            f"{limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES} 字节 —— 一个字节的脚本都没跑。"
            "本档只做判废＋留痕＋挡后续调用；**不声称**能阻止已经发生的那一次分配"
            "（P61 §0.6：单次 C 调用不可中断，没有任何中途 kill）")


def _post_gate_message(plugin_id: str, delta: int) -> str:
    return (f"插桩 {plugin_id} 的这一次调用峰值增量 {delta} 字节 >= 上限 "
            f"{limits.PLUGIN_CALL_RSS_LIMIT_BYTES} 字节 —— 该次结果作废。"
            "本档只做判废＋留痕＋挡后续调用；**不声称**能阻止这一次分配"
            "（P61 §0.6：单次 C 调用不可中断，没有任何中途 kill）")


def load_script(source_text: str, *, plugin_id: str,
                timeout_s: float = DEFAULT_TIMEOUT_S,
                on_call: Callable[[dict], None] | None = None,
                ) -> Callable[[dict], dict]:
    """预检 → 编译 → 执行 → 取出 `run`，返回「喂 ctx 得结构化结果」的可调用对象。

    返回的闭包每次调用都会：过执行前闸门 → 加超时 → 调 `run` → 校验返回结构 →
    过执行后闸门。两道闸门见模块 docstring；**不做中途 kill**。

    `on_call`（可选）：每次调用结束回调一条固定形状的读数（`ok`/`timeout`/
    `resource` 三种结局）。**不传时行为与今天逐字相同**，返回类型也一字不改
    （仍是 `_call(ctx) -> dict`）。
    """
    guard.check_source(source_text)

    namespace = _make_namespace()
    code = compile(source_text, f"<plugin:{plugin_id}>", "exec")
    exec(code, namespace)                       # noqa: S102 —— 预检已挡住危险构造

    run = namespace.get("run")
    if not callable(run):
        raise contract.PluginContractError(
            f"插桩 {plugin_id} 的脚本未定义可调用的 run(ctx)；"
            f"实收到 {type(run).__name__ if run is not None else '（缺失）'}"
        )

    def _call(ctx: dict) -> dict:
        started = time.monotonic()
        before = resources.peak_rss_bytes()
        outcome: str | None = None
        try:
            # 执行前闸门：进程峰值已经这么高了，跑什么都是在赌（D3-1）。
            if before >= limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES:
                outcome = "resource"
                raise PluginResourceError(_pre_gate_message(plugin_id, before))
            with _time_limit(timeout_s):
                result = run(ctx)
            result = contract.validate_return(plugin_id, result)
            # 执行后闸门：这一次的增量越界 ⇒ 结果作废（D3-2）。
            delta = max(0, resources.peak_rss_bytes() - before)
            if delta >= limits.PLUGIN_CALL_RSS_LIMIT_BYTES:
                outcome = "resource"
                raise PluginResourceError(_post_gate_message(plugin_id, delta))
            outcome = "ok"
            return result
        except PluginTimeout:
            outcome = "timeout"
            raise
        finally:
            # `outcome is None` = 脚本自己抛的其它异常（契约/NameError）：
            # 走既有通道报错，**不**记资源事件（D4 的取值域只有三个词）。
            if on_call is not None and outcome is not None:
                peak = resources.peak_rss_bytes()
                on_call({
                    "plugin_id": plugin_id, "outcome": outcome,
                    "duration_ms": (time.monotonic() - started) * 1000.0,
                    "rss_delta_bytes": max(0, peak - before),
                    "rss_peak_bytes": peak,
                })

    return _call
