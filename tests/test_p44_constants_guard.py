"""P44 三条结构保证（任务书 T2）：单一真源 / 无外部覆盖路径 / 无写入口。

用 **AST** 扫描，不用子串匹配 —— 文档字符串里就会出现这些常量名
（ERROR_DIARY #50：子串匹配会把注释判红，于是只能放宽，最后什么都测不到）。

三条扫描各配一条**反向自检**：喂违规样本必须判红。否则「扫描零个文件」或
「作用域写错」都会全绿，而全绿的扫描等于没写。
"""

from __future__ import annotations

import ast
import pathlib

from stocklab.config import limits

ROOT = pathlib.Path(__file__).resolve().parents[1] / "stocklab"
LIMITS_REL = "config/limits.py"

CONSTANTS = (
    "CIRCUIT_BREAKER_DRAWDOWN",
    "VALIDATION_ROUNDS_MIN",
    "VALIDATION_ROUNDS_MAX",
    "VALIDATION_MAX_DAYS",
    "FREEZE_MAX_DAYS",
)


def _py_files(root: pathlib.Path) -> list[pathlib.Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _assign_targets(tree: ast.AST) -> list[ast.expr]:
    out: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            out.extend(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            out.append(node.target)
    return out


def _files_rebinding(root: pathlib.Path) -> list[str]:
    """在 `config/limits.py` 之外重新绑定这 5 个常量的文件（相对 root 的 posix 路径）。

    只认**赋值目标**（`X = ...` / `limits.X = ...`）—— `from ... import X` 是
    预期的消费方式，不是第二真源，故不在此列。
    """
    bad = []
    for path in _py_files(root):
        rel = path.relative_to(root).as_posix()
        if rel == LIMITS_REL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hit: set[str] = set()
        for t in _assign_targets(tree):
            if isinstance(t, ast.Name) and t.id in CONSTANTS:
                hit.add(t.id)
            elif isinstance(t, ast.Attribute) and t.attr in CONSTANTS:
                hit.add(t.attr)
        if hit:
            bad.append(f"{rel}: {sorted(hit)}")
    return bad


def _labweb_references(root: pathlib.Path) -> list[str]:
    """`labweb/` 里出现这 5 个常量名的文件（本任务要求为零 —— 只读配置视图是 P49）。"""
    hits = []
    for path in _py_files(root / "labweb"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        found = names & set(CONSTANTS)
        if found:
            hits.append(f"{path.relative_to(root).as_posix()}: {sorted(found)}")
    return hits


def _dynamic_write_sites(root: pathlib.Path) -> list[str]:
    """任何 `setattr(...)` / `__setattr__(...)` 调用里提到常量的地方。"""
    hits = []
    for path in _py_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name not in ("setattr", "__setattr__"):
                continue
            blob = ast.dump(node, include_attributes=False)
            if any(c in blob for c in CONSTANTS):
                hits.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return hits


# ---------- 保证 1：单一真源 ----------

def test_constants_are_defined_only_in_config_limits():
    """保证 1：这 5 个名字在整个 `stocklab/` 里只被绑定一次（在 limits.py）。"""
    assert _py_files(ROOT), "扫描零个文件 = 无效实验（ERROR_DIARY #50）"
    offenders = _files_rebinding(ROOT)
    assert offenders == [], f"这些常量只允许在 {LIMITS_REL} 里定义，违规：{offenders}"


# ---------- 保证 2：无外部覆盖路径 ----------

def test_no_dynamic_assignment_path():
    """保证 2：没有 `setattr`/`__setattr__` 这类运行期写常量的路径。"""
    assert _py_files(ROOT), "扫描零个文件 = 无效实验"
    assert _dynamic_write_sites(ROOT) == []


# ---------- 保证 3：Web 层无写入口 ----------

def test_labweb_does_not_touch_main_constants():
    """保证 3：本任务不给网页端任何接触主干常量的机会（D-32 的只读视图是 P49）。"""
    assert _py_files(ROOT / "labweb"), "扫描零个文件 = 无效实验"
    assert _labweb_references(ROOT) == []


# ---------- 反向自检：上面三条扫描必须真的能判红 ----------

def test_scanners_flag_a_planted_violation(tmp_path):
    """把违规样本喂给扫描函数 —— 全绿说明扫描没有区分力（ERROR_DIARY #50）。"""
    fake_root = tmp_path / "stocklab"
    (fake_root / "config").mkdir(parents=True)
    (fake_root / "config" / "limits.py").write_text(
        "CIRCUIT_BREAKER_DRAWDOWN = 0.10\n", encoding="utf-8")
    (fake_root / "labweb").mkdir(parents=True)
    (fake_root / "labweb" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (fake_root / "evil.py").write_text(
        "from stocklab.config import limits\n"
        "VALIDATION_MAX_DAYS = 365\n"                          # 裸名重绑定
        "limits.FREEZE_MAX_DAYS = 3650\n"                      # 属性重绑定
        "setattr(limits, 'CIRCUIT_BREAKER_DRAWDOWN', 0.9)\n",  # 运行期写
        encoding="utf-8")
    (fake_root / "labweb" / "sneaky.py").write_text(
        "from stocklab.config import limits\n"
        "print(limits.VALIDATION_ROUNDS_MAX)\n", encoding="utf-8")

    assert _files_rebinding(fake_root), "重绑定扫描没有区分力"
    assert _dynamic_write_sites(fake_root), "setattr 扫描没有区分力"
    assert _labweb_references(fake_root), "labweb 扫描没有区分力"


def test_scan_is_not_vacuous_on_the_real_tree():
    """反向自检的另一半：确认扫描真的走过了真实源码。"""
    files = _py_files(ROOT)
    assert len(files) > 50, f"只扫到 {len(files)} 个文件，扫描范围写错了？"
    assert LIMITS_REL in {p.relative_to(ROOT).as_posix() for p in files}
    assert limits.CIRCUIT_BREAKER_DRAWDOWN == 0.10
