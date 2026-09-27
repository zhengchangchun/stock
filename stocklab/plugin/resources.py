"""插桩执行的**内存量具**（P93）：只读进程自身的峰值 RSS。

## 单位：darwin 是字节，linux 是千字节

`resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` 的单位**随平台而变**：

- macOS / darwin：**字节**；
- Linux：**千字节**。

本模块按 `sys.platform` 归一，读出口一律是**字节**。把这件事写在 docstring 里，
是因为它是唯一会让那两条阈值静默错 1024 倍的地方（未识别的平台**抛错**而不是猜）。

## 量具只读自身：不 fork、不采样子进程（P93 / D1）

读数就是 `RUSAGE_SELF` 的一个字段 —— 没有子进程、没有轮询线程、没有 cgroup。
代价是**只能看峰值，看不到当前值**：`ru_maxrss` 是进程生命周期内的**高水位**，
一旦涨上去就不会回落。所以：

- `delta` 是「调用后的峰值 − 调用前的峰值」，恒 >= 0；
- 一个曾把峰值顶到 244MB 的调用之后，**后续小调用的 delta 会是 0** ——
  那不是「没用量」，是「没有超过历史高水位」。`rss_mark()` 把调用前后的峰值
  一并给出来，读的人能自己判断。

## 不假装能中断

本量具**不**做中途 kill：P61 §0.6 实测「一次性分配发生在单个 C 调用里，
`SIGALRM` 只在字节码之间投递」。闸门（`plugin/runtime.py`）因此只声称
「判废 ＋ 留痕 ＋ 后续调用被挡」，不声称能阻止那一次分配。
"""

from __future__ import annotations

import resource
import sys
from contextlib import contextmanager
from typing import Iterator

#: `ru_maxrss` 已经是字节的平台。linux 系是千字节，见 `rss_unit_bytes`。
_RAW_BYTES_PLATFORMS: frozenset[str] = frozenset({"darwin"})


def _raw_maxrss() -> int:
    """裸读数（**平台单位**）。单独成函数是为了让单测能 monkeypatch 出序列。"""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def rss_unit_bytes(platform: str | None = None) -> int:
    """`ru_maxrss` 的平台单位 → **字节乘数**（darwin 1、linux 1024）。

    `platform is None` ⇒ 用运行平台的 `sys.platform`。未识别的平台**抛
    `ValueError`**：猜一个单位就是静默错 1024 倍，而错的方向还取决于平台 ——
    宁可炸，也不要把一个错 1024 倍的读数喂给闸门。
    """
    name = sys.platform if platform is None else platform
    if name in _RAW_BYTES_PLATFORMS:
        return 1
    if name.startswith("linux"):
        return 1024
    raise ValueError(
        f"未知平台的 ru_maxrss 单位：{name!r} —— 本模块只认 darwin（字节）与 "
        "linux（千字节）；请先查清该平台的单位再加进来，不要猜")


def peak_rss_bytes(*, platform: str | None = None) -> int:
    """进程**峰值** RSS（字节）。只读自身，不做任何采样。"""
    return _raw_maxrss() * rss_unit_bytes(platform)


@contextmanager
def rss_mark(*, platform: str | None = None) -> Iterator[dict]:
    """一段代码前后的峰值读数：`{"before", "after", "delta"}`（全为字节）。

        with rss_mark() as mark:
            run(ctx)
        mark["delta"]        # 本次的峰值增量，>= 0

    读数在 `finally` 里填 —— 块里抛异常时 mark 也是填好的：闸门要判废的
    **正是**那个抛出来的调用（`plugin/runtime.py`）。
    """
    mark: dict = {"before": peak_rss_bytes(platform=platform), "after": None,
                  "delta": 0}
    try:
        yield mark
    finally:
        mark["after"] = peak_rss_bytes(platform=platform)
        # ru_maxrss 单调不降；平台怪癖让它掉下来时记 0，不记负号（delta 是增量）。
        mark["delta"] = max(0, mark["after"] - mark["before"])
