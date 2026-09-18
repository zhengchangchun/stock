"""插桩返回结构校验（设计文档 §6.2）：严格照文档 01 的约定结构体。

## 越界一律拒绝，绝不 clamp

`score = 150` 是脚本的 bug。把它悄悄压到 100 会让一个坏脚本看起来
工作正常 —— 那比直接报错危险得多，因为候选池会带着错误分数入库，
而报告里看不出任何异常。

## `bool` 不是 `float`

Python 里 `True` 是 `int` 的子类，所以 `isinstance(True, float)` 走的是
数值分支。`score=True` 会静默变成 1.0 分。必须显式排除。

## 两个时点各校验一次

`submit` 时校验一次、`approve`（上线）时再校验一次。脚本文本一旦落库
不可变，但本模块的校验规则会随项目演进收紧 —— 上线时再校验，
保证「现在能跑的」就是「当初认证过的」。
"""

from __future__ import annotations

#: plugin_id → (字段名, 种类) 的有序清单。种类见 `_check_field`。
SHAPES: dict[str, tuple[tuple[str, str], ...]] = {
    "0": (("pass_flag", "bool"), ("risk_note", "str_list")),
    "1": (("score", "score"), ("pass_flag", "bool"),
          ("reason", "str"), ("risk_list", "str_list")),
    "2": (("score", "score"), ("pass_flag", "bool"),
          ("reason", "str"), ("risk_list", "str_list")),
    "3": (("score", "score"), ("pass_flag", "bool"),
          ("reason", "str"), ("risk_list", "str_list")),
    "4": (("final_score", "score"), ("risk_out", "str_list")),
    "5": (("analysis_result", "dict"), ("bad_case_list", "list")),
}

KNOWN_PLUGIN_IDS: tuple[str, ...] = tuple(sorted(SHAPES))


class PluginContractError(Exception):
    """脚本返回结构不符合约定。"""


def _check_field(plugin_id: str, name: str, kind: str, value: object) -> object:
    where = f"插桩 {plugin_id} 的字段 {name!r}"
    if kind == "score":
        # bool 先排除：`isinstance(True, int)` 为真，不挡会静默变成 1.0 分
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PluginContractError(
                f"{where} 必须是数值，实际是 {type(value).__name__}（{value!r}）")
        value = float(value)
        if not 0.0 <= value <= 100.0:
            raise PluginContractError(
                f"{where} 超出 [0,100] 范围：{value}。"
                "越界是脚本的 bug，本系统**拒绝而非截断** —— 静默 clamp "
                "会让坏脚本看起来工作正常")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise PluginContractError(
                f"{where} 必须是 bool，实际是 {type(value).__name__}（{value!r}）")
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise PluginContractError(
                f"{where} 必须是 str，实际是 {type(value).__name__}（{value!r}）")
        return value
    if kind == "str_list":
        if not isinstance(value, list) or not all(
                isinstance(x, str) for x in value):
            raise PluginContractError(
                f"{where} 必须是 list[str]，实际是 {value!r}")
        return list(value)
    if kind == "dict":
        if not isinstance(value, dict):
            raise PluginContractError(
                f"{where} 必须是 dict，实际是 {type(value).__name__}（{value!r}）")
        return dict(value)
    if kind == "list":
        if not isinstance(value, list):
            raise PluginContractError(
                f"{where} 必须是 list，实际是 {type(value).__name__}（{value!r}）")
        return list(value)
    raise AssertionError(f"未知字段种类 {kind!r}")     # 代码 bug，不是脚本 bug


def validate_return(plugin_id: str, result: object) -> dict:
    """校验并**标准化**返回值（数值统一成 float）。违规抛 `PluginContractError`。

    返回新 dict（不是原对象）—— 调用方拿到的一定是校验过的副本。
    """
    if plugin_id not in SHAPES:
        raise PluginContractError(
            f"未知插桩编号 {plugin_id!r}；已知：{list(KNOWN_PLUGIN_IDS)}")
    if not isinstance(result, dict):
        raise PluginContractError(
            f"插桩 {plugin_id} 的 run() 必须返回 dict，实际是 "
            f"{type(result).__name__}（{result!r}）")

    out: dict = {}
    for name, kind in SHAPES[plugin_id]:
        if name not in result:
            raise PluginContractError(
                f"插桩 {plugin_id} 的返回缺少必填字段 {name!r}；"
                f"实收到 {sorted(result)}")
        out[name] = _check_field(plugin_id, name, kind, result[name])
    return out
