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
"""

from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import Any, Callable

from stocklab.plugin import contract, guard

#: 单次调用的默认超时（秒）。够任何纯计算脚本跑完，又不会让主流程卡死。
DEFAULT_TIMEOUT_S: float = 5.0

#: 脚本可见的内建函数白名单。**刻意窄** —— 不放 `type`/`object`/`dir`
#: 之外能通向模块系统的入口。
_ALLOWED_BUILTINS: dict[str, Any] = {
    name: __builtins__[name] if isinstance(__builtins__, dict)
    else getattr(__builtins__, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "dir", "enumerate", "filter", "float",
        "int", "len", "list", "map", "max", "min", "range", "round", "set",
        "sorted", "str", "sum", "tuple", "zip",
    )
}


class PluginTimeout(Exception):
    """脚本执行超时。"""


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


def load_script(source_text: str, *, plugin_id: str,
                timeout_s: float = DEFAULT_TIMEOUT_S) -> Callable[[dict], dict]:
    """预检 → 编译 → 执行 → 取出 `run`，返回「喂 ctx 得结构化结果」的可调用对象。

    返回的闭包每次调用都会：加超时 → 调 `run` → 校验返回结构。
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
        with _time_limit(timeout_s):
            result = run(ctx)
        return contract.validate_return(plugin_id, result)

    return _call
