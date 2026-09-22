"""死代码护栏：`return` / `raise` / `break` / `continue` 之后不得再有同层语句。

## 为什么需要这条（ERROR_DIARY #52）

2026-09-21 给 `/lab/paper` 加「智能体臂」小节时写出过这样一段：

```python
    return section("A", ...)
    + section("B", ...)      # ← 缩进与 return 同级 ⇒ 不可达语句
```

它**语法合法**：第二行被解析成一条独立的表达式语句（一元 `+` 作用在调用结果上），
于是 Python 不报错、测试不报错、页面也「能渲染」—— 只是 B 段被**静默丢掉**，
而这正是本次任务要新增的内容。发现它靠的是肉眼重读，不是工具。

同类的静默失效还有：`raise` 之后补的赋值、`continue` 之后补的清理逻辑。
这类 bug 的共同特征是**行为看起来正常**，所以必须在源码层拦，而不是等断言。

## 为什么不用 linter

本仓库的验证入口是 `pytest` + `scripts/verify.sh`（离线、零外部依赖）。
引入 ruff/flake8 需要新的工具链与配置面；这里只需要一条规则，
用 `ast` 写十几行更可控，也和 `test_paper_discipline_guard.py` 的扫描风格一致。

## 不做的事

只查**同一个语句块**内、且对**任何**路径都终止的语句之后的兄弟语句。
不做分支可达性分析（那需要真值求解，误报会淹掉信号）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 只看本项目自己的源码。`stocklab/` 是产品代码，`tests/` 是护栏本身
#: （测试里的死代码会让「测试通过」也失去意义）。
SCAN_DIRS = ("stocklab", "tests")

#: 对**所有**执行路径都终止的语句（`if`/`try` 不算：它们可能不进入分支）。
TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def _sources() -> list[Path]:
    out: list[Path] = []
    for name in SCAN_DIRS:
        out.extend(sorted((ROOT / name).rglob("*.py")))
    return [p for p in out if "__pycache__" not in p.parts]


def _label(path: Path) -> str:
    """仓库内文件显示相对路径；自检用的 tmp 文件显示绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _dead_after_terminator(tree: ast.AST, path: Path) -> list[str]:
    """返回「终止语句 + 其后的死语句」的描述列表。"""
    found: list[str] = []

    def walk(node: ast.AST) -> None:
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block:
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, TERMINATORS):
                        nxt = block[i + 1]
                        found.append(
                            f"{_label(path)}:{nxt.lineno}: "
                            f"{type(stmt).__name__} 之后仍有 "
                            f"{type(nxt).__name__}（'{_snippet(nxt)}'）不可达"
                        )
                        break  # 一个块只报第一处，避免级联噪声
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return found


def _snippet(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse 对合法 AST 几乎不会失败
        return "?"
    text = " ".join(text.split())
    return text[:60] + ("…" if len(text) > 60 else "")


def test_scan_targets_exist() -> None:
    """护栏自身不能空转：扫不到文件时下面的断言毫无意义（见 ERROR_DIARY #40）。"""
    files = _sources()
    assert len(files) > 50, f"只扫到 {len(files)} 个文件，路径大概是写错了"
    names = {p.name for p in files}
    assert "rules.py" in names and "paper_render.py" in names


def test_no_statements_after_return_or_raise() -> None:
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(_dead_after_terminator(tree, path))
    assert not offenders, (
        "检测到不可达语句（ERROR_DIARY #52 —— `return`/`raise` 之后同一层还有语句）：\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_actually_catches_dead_code(tmp_path: Path) -> None:
    """反向自检：把**当年那段真代码**喂进来必须被判红。

    这里刻意用 `return f(...)` + 下一行 `+ g(...)` 的形态 —— 它是合法的
    Python（第二行是一元 `+` 表达式语句），当年的 bug 正是这样溜过解读的。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def render():\n"
        "    return section('A')\n"
        "    + section('B')\n",
        encoding="utf-8",
    )
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    hits = _dead_after_terminator(tree, bad)
    assert hits, "反向自检失败：这种死代码都没抓到，护栏等于没有"
    assert "不可达" in hits[0]


def test_guard_does_not_flag_a_guarded_return(tmp_path: Path) -> None:
    """`if` 里的 `return` 之后**同层**还有语句是正常的，不许误报。"""
    good = tmp_path / "good.py"
    good.write_text(
        "def f(x):\n"
        "    if x:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    tree = ast.parse(good.read_text(encoding="utf-8"))
    assert _dead_after_terminator(tree, good) == []
