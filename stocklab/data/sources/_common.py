"""适配器共享的单位常量与解析小工具。

放在独立模块而非某个适配器里：腾讯与东财是**同级**数据源，
让东财 import 腾讯会造出假的依赖方向（计划里是从 tencent 导 `_to_float`，这里修正）。
"""

LOT = 100          # 1 手 = 100 股
WAN = 10_000       # 1 万 = 10_000 元
YI = 100_000_000   # 1 亿 = 100_000_000 元


def to_float(s: object) -> float | None:
    """宽松数值解析：非数值返回 None，由调用方决定「跳过并留痕」。"""
    try:
        return float(s)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
