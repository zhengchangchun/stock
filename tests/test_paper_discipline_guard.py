"""护栏：`stocklab/paper/` 不得碰模型预测与凯利（P19 纪律，源码扫描）。

## 为什么这条护栏必须存在

三个模块的文档字符串都写着「`test_paper_never_imports_model_or_kelly` 用源码
扫描钉住这条纪律」（`paper/config.py`、`paper/rules.py`、`labweb/paper_data.py`），
但**在 2026-09-21 之前这个测试并不存在** —— 于是「钉住」只是一句注释里的
君子协定：模拟盘里真有人 `from stocklab.predict import ...`，CI 不会红。

生产模型的方向能力 ≈ 0（行级命中 38.14%、按日聚类 0.3819±0.0070、Brier 0.6581
对随机 0.667），所以「拿涨的概率 > 跌的概率当买入信号」在模拟盘里是被**禁止**的
做法，不是「暂时没接」。本文件把这句话变成可执行的东西。

## 为什么是 AST 扫描而不是子串匹配

子串匹配会把**文档字符串自己**判红（上面几段话就写着 `kelly` 和 `model`）。
AST 扫描只看代码：import 的模块名、Name/Attribute/keyword 标识符、函数与类名，
字符串常量（含 docstring）一律不看。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import stocklab.paper as paper_pkg

#: 模拟盘不得 import 的包前缀。前三个是「模型 / 验证 / 凯利」，后三个是
#: 「选股链路」（插桩脚本、候选池、实验）—— 模拟盘只做**纪律与分散**，
#: 选股是另一条链路，接了它就是在换一个问题（见 `/paper` 页的 routing 段）。
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "stocklab.predict", "stocklab.verify", "stocklab.risk",
    "stocklab.plugin", "stocklab.candidate", "stocklab.experiments",
)

PAPER_DIR = Path(paper_pkg.__file__).parent
PAPER_SOURCES = sorted(PAPER_DIR.glob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    """顶层（非相对）import 的模块全名，`from x import y` 记 `x` 与 `x.y`。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif (isinstance(node, ast.ImportFrom) and node.level == 0
              and node.module):
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def _identifiers(tree: ast.AST) -> set[str]:
    """代码里出现的标识符（**不含**字符串与文档字符串）。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            out.add(node.name)
    return out


def test_paper_package_has_sources():
    """扫描对象非空 —— 免得路径写错时「零个文件全绿」。"""
    names = [p.name for p in PAPER_SOURCES]
    assert "engine.py" in names and "rules.py" in names and "config.py" in names


@pytest.mark.parametrize("path", PAPER_SOURCES, ids=lambda p: p.name)
def test_paper_never_imports_model_or_kelly(path: Path):
    """`stocklab/paper/*.py` 不得 import 模型 / 验证 / 凯利 / 选股链路。"""
    mods = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
    for mod in sorted(mods):
        hit = next((p for p in FORBIDDEN_PREFIXES
                    if mod == p or mod.startswith(p + ".")), None)
        assert hit is None, (
            f"{path.name} import 了 {mod}（命中 {hit}）—— 模拟盘只做纪律与分散，"
            "不许用模型方向预测或选股链路")
    assert not [m for m in mods if "kelly" in m.lower()], (
        f"{path.name} import 了凯利相关模块：{sorted(mods)}")


@pytest.mark.parametrize("path", PAPER_SOURCES, ids=lambda p: p.name)
def test_paper_source_has_no_model_or_kelly_identifier(path: Path):
    """代码里不得出现凯利标识符，也不得引用 `MODEL_VERSION`。"""
    ids = _identifiers(ast.parse(path.read_text(encoding="utf-8")))
    kelly = sorted(i for i in ids if "kelly" in i.lower())
    assert not kelly, f"{path.name} 出现凯利标识符 {kelly} —— 模拟盘不做仓位优化"
    model = sorted(i for i in ids
                   if "model_version" in i.lower() or i.upper() == "MODEL_VERSION")
    assert not model, (f"{path.name} 引用了 {model} —— 模拟盘不按模型版本决策；"
                       "要接模型请另外加一条臂，并显式改本护栏")


def test_the_guard_is_not_satisfied_by_an_empty_scan(tmp_path: Path):
    """反向自检：把一段真的违规代码喂给扫描逻辑，必须能被判红。"""
    bad = tmp_path / "bad.py"
    bad.write_text("from stocklab.predict.model import MODEL_VERSION\n"
                   "def f():\n    return kelly_fraction(MODEL_VERSION)\n",
                   encoding="utf-8")
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    mods = _imported_modules(tree)
    assert any(m.startswith("stocklab.predict") for m in mods)
    assert "MODEL_VERSION" in _identifiers(tree)
    assert any("kelly" in i.lower() for i in _identifiers(tree))
