"""AST 静态预检（设计文档 §6.3）：在 `exec` 之前把危险构造挡掉。

## 这是「防出错」不是「防恶意」

威胁模型是「AI 生成的脚本会写错」，不是「有人要攻破沙盒」。它挡得住
手滑写的 `import os` 和 `().__class__.__bases__` 这类常见逃逸，但**挡不住
有决心的攻击者** —— 设计文档 §13 已把这条列为已知限制。真隔离是
文档 04 的 P3，不在本轮。

## 为什么必须同时用静态预检和超时

预检管「不让它碰到不该碰的东西」，超时管「别把主流程挂死」。两者
都不能省：`while True: pass` 没有危险构造，只有超时拦得住。

## 为什么禁 `__` 开头的属性

`().__class__.__bases__[0].__subclasses__()` 是 Python 沙盒逃逸的
经典路径 —— 单靠禁 `import` 拦不住它，因为逃逸后能直接拿到
`<class 'os._wrap_close'>` 之类的对象。
"""

from __future__ import annotations

import ast

#: 禁止直接出现的名字。
BANNED_NAMES: frozenset[str] = frozenset({
    "__import__", "eval", "exec", "compile", "open",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
})

#: 禁止出现的 AST 节点类型对应的错误消息文本（节点类名 → 人类可读的原因）。
#: 注意：实际拦截逻辑在 check_source 的 walk 循环中用 isinstance 实现，
#: 此 dict 仅供 _describe() 生成消息文本，新增条目不会自动启用拦截。
_BANNED_NODE_DESCRIPTIONS: dict[str, str] = {
    "Import": "禁止 import",
    "ImportFrom": "禁止 from ... import",
    "Global": "禁止 global 声明",
    "Nonlocal": "禁止 nonlocal 声明",
}


class PluginGuardError(Exception):
    """脚本未通过静态预检。"""


def _describe(node: ast.AST) -> str:
    kind = _BANNED_NODE_DESCRIPTIONS.get(type(node).__name__)
    if kind:
        return kind
    if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
        return f"禁止使用 {node.id}"
    if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
        return f"禁止访问双下划线属性 {node.attr}"
    return f"禁止的构造 {type(node).__name__}"


def check_source(source_text: str) -> None:
    """静态预检。不通过抛 `PluginGuardError`（带行号）。

    语法错误也归为预检失败 —— 一段编译不过的脚本没有任何理由进版本库。
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise PluginGuardError(f"禁止载入：脚本语法错误（第 {exc.lineno} 行）：{exc.msg}") from exc

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise PluginGuardError(f"脚本第 {node.lineno} 行：{_describe(node)}")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise PluginGuardError(f"脚本第 {node.lineno} 行：{_describe(node)}")
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise PluginGuardError(f"脚本第 {node.lineno} 行：{_describe(node)}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise PluginGuardError(f"脚本第 {node.lineno} 行：{_describe(node)}")
