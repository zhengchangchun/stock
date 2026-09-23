"""护栏：`stocklab/paper/` 不得碰模型预测与凯利（P19 纪律，源码扫描）＋
**按臂分作用域**（P52 / D-34）。

## P52 起这条护栏是「按臂」的，不再是「一刀切」

D-34 把 `arm-agent` 从「改纪律数字的臂」重构成「**每交易日一条决策的操盘手**」：
它**可以**表达方向与仓位、可以池内自由选标的。于是「模拟盘不许选股」这句话
对这条臂不再成立 —— 但护栏本身**不作废**，它换了个成立方式：

| 臂 | 方向/仓位 | 候选池 | 台账 |
|---|---|---|---|
| `arm-agent*` | **允许**（这是它的职责） | 允许（只读快照） | **每个成交日必须有一行**（缺行即判红） |
| `arm-now` / `arm-discipline-*` | **禁**（P19 的方向择时禁令原样有效） | 禁 | —— |

所以本文件有两组判据：
1. **源码扫描**（原有）：`paper/` 不 import 模型/验证/凯利/选股链路，不出现凯利标识符
   或 `MODEL_VERSION`。`arm-agent` 放开的是**决策空间**，不是**数据来源**。
2. **作用域**（P52 新增）：静态臂那条执行路径（`rules.py` 的 `plan_*` 与
   `engine._plan_steps`）不得出现「目标权重」这类操盘口径的构造；反过来，
   操盘口径只许出现在 `agent_decide.py` / `agent_pool.py` 这两个模块里
   （`engine.py` 只做分派 —— 它必须能同时提到两条路）。
   **外加一条运行期判据**：`arm-agent*` 的成交日若在台账里缺行，判红。

## 为什么源码扫描而不是子串匹配

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
from stocklab.paper import agent_decide, agent_pool, engine as paper_engine

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


# ---------- P52：护栏按臂分作用域 ----------

#: 允许出现「操盘口径」（目标权重 / 候选池读取）的模块 → **允许出现的词**。
#: `None` = 不设限（它就是那条路的实现）。给集合的，是「只许声明，不许使用」：
#: `config.py` 是口径常量的落点，它只该**声明**载荷的键名
#: （`DECISION_ITEM_KEYS`），不该出现任何「下单」的构造（`plan_target_weight` /
#: `portfolio_decision_on` / `random_payload` / `target_value`）。
#: `rules.py` 里只有 `plan_target_weight` 一个函数属于这条路（它是共用内核的一部分，
#: 与静态臂的 `plan_*` 并排放在同一个文件里）；`engine.py` 只做**分派**，
#: 所以它必须能同时提到两条路 —— 这几处就是全部，别处出现即视为越界。
AGENT_SCOPED_MODULES: dict[str, frozenset[str] | None] = {
    "agent_decide.py": None,
    "agent_pool.py": None,
    "rules.py": None,
    "engine.py": None,
    "config.py": frozenset({"target_weight_pct"}),
    # P56：对照表要能回答「这一格**有没有**决策」（页面上的「今日无决策」），
    # 所以它**只**被允许查询 `portfolio_decision_on` —— 方向与仓位
    # （`target_weight_pct` / `target_value` / `plan_target_weight`）在这里照旧越界。
    "comparison.py": frozenset({"portfolio_decision_on"}),
}

#: 「操盘口径」的标识符：出现它们 = 这个模块在表达方向与仓位。
AGENT_ORDER_IDENTIFIERS: tuple[str, ...] = (
    "target_weight_pct", "target_value", "plan_target_weight",
    "portfolio_decision_on", "random_payload",
)

#: 静态臂的**执行路径**（这些函数只许跑写死的纪律条文）。
STATIC_EXECUTION_FUNCS: tuple[str, ...] = ("_plan_steps", "_build_etf", "evaluate")


def _exact_strings(tree: ast.AST) -> set[str]:
    """**恰好等于**某个标识符名的字符串常量。

    为什么这里必须看字符串：操盘口径在代码里是以**字典键**的形式出现的
    （`item["target_weight_pct"]`），只看 Name/Attribute 会漏掉它们 ——
    而漏掉的后果是护栏看起来绿着、实际什么都没拦。取「恰好等于」而不是子串，
    是为了不把文档字符串判红（上面那几段话就写着这些词）。
    """
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _agent_terms(tree: ast.AST) -> set[str]:
    """一个 AST 里出现的操盘口径词（标识符 ＋ 恰好同名的字符串）。"""
    wanted = set(AGENT_ORDER_IDENTIFIERS)
    return (_identifiers(tree) | _exact_strings(tree)) & wanted


def _func_idents(tree: ast.AST) -> dict[str, set[str]]:
    """函数名 → 该函数体里出现的标识符（含嵌套）。"""
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
                elif isinstance(sub, ast.keyword) and sub.arg:
                    names.add(sub.arg)
            names |= _exact_strings(node)
            out[node.name] = names
    return out


@pytest.mark.parametrize("path", PAPER_SOURCES, ids=lambda p: p.name)
def test_agent_decision_space_stays_inside_the_agent_scoped_modules(path: Path):
    """操盘口径（目标权重 / 候选池）只许出现在 `AGENT_SCOPED_MODULES` 里。

    摘掉它以后：`config.py` 或 `store.py` 里能随手写一句「按 target_weight 下 10% 的单」，
    而静态臂的 `arm-hold` / `arm-now` 也会经过 `engine` —— 那时候「不许方向择时」
    就只剩文档里的一句话了。
    """
    hit = sorted(_agent_terms(ast.parse(path.read_text(encoding="utf-8"))))
    if not hit:
        return
    assert path.name in AGENT_SCOPED_MODULES, (
        f"{path.name} 出现了操盘口径的标识符 {hit} —— 方向与仓位只许在 "
        f"{sorted(AGENT_SCOPED_MODULES)} 里表达（D-34 放开的只有 arm-agent 的决策空间）")
    allowed = AGENT_SCOPED_MODULES[path.name]
    if allowed is not None:
        illegal = sorted(set(hit) - allowed)
        assert illegal == [], (
            f"{path.name} 只许**声明** {sorted(allowed)}，"
            f"但它出现了 {illegal} —— 声明形状与下单是两件事")


@pytest.mark.parametrize("path", PAPER_SOURCES, ids=lambda p: p.name)
def test_static_arm_execution_path_has_no_position_direction_terms(path: Path):
    """静态臂的执行函数（`_plan_steps` / `_build_etf` / `evaluate`）里不许出现操盘口径。

    这一条比上一条更细：`engine.py` 整体被允许提到两条路，但**跑写死条文的那几个
    函数**不许 —— 否则「静态臂只做纪律与分散」就只是注释。
    """
    funcs = _func_idents(ast.parse(path.read_text(encoding="utf-8")))
    for name in STATIC_EXECUTION_FUNCS:
        if name not in funcs:
            continue
        hit = sorted(set(AGENT_ORDER_IDENTIFIERS) & funcs[name])
        assert hit == [], (
            f"{path.name}::{name}（静态臂的执行路径）出现了 {hit} —— "
            f"方向与仓位是 arm-agent 的决策空间，不是纪律臂的")


def test_guard_is_not_satisfied_by_an_empty_scope_scan(tmp_path: Path):
    """反向自检：把操盘口径放进一个**不在白名单**的模块里，必须被判红。"""
    bad = tmp_path / "config.py"
    # 两种写法都要被抓到：**字典键**（真实代码的写法）与属性访问
    bad.write_text("def f(p):\n    return p['target_weight_pct'], p.target_value\n",
                   encoding="utf-8")
    hit = _agent_terms(ast.parse(bad.read_text(encoding="utf-8")))
    assert hit == {"target_weight_pct", "target_value"}, hit
    assert "config.py" not in {k for k, v in AGENT_SCOPED_MODULES.items() if v is None}
    # 白名单里 `config.py` 只许声明一个词 —— 多出来的必须判红
    assert set(hit) - (AGENT_SCOPED_MODULES["config.py"] or set()), "越界许可失效了"

    bad2 = tmp_path / "other.py"
    bad2.write_text("def evaluate(x):\n    return x.target_weight_pct\n",
                    encoding="utf-8")
    funcs = _func_idents(ast.parse(bad2.read_text(encoding="utf-8")))
    assert set(AGENT_ORDER_IDENTIFIERS) & funcs["evaluate"], "函数级扫描失效了"
    # 而这个坏模块落在白名单外 ⇒ 两条判据都应当判红
    assert "other.py" not in AGENT_SCOPED_MODULES


def test_the_agent_scoped_modules_actually_exist():
    """白名单里的模块名必须真的存在 —— 免得改名之后这条护栏静默空转。"""
    names = {p.name for p in PAPER_SOURCES}
    missing = [m for m in AGENT_SCOPED_MODULES if m not in names]
    assert missing == [], f"白名单指向了不存在的模块：{missing}"


def test_agent_arm_trades_without_a_ledger_row_are_red(tmp_path):
    """**台账缺行即判红**（T5 的运行期那一半）。

    构造一个「成交有、台账没有」的库：绕过写入口直接往 `paper_trades` 里插一笔
    属于 `arm-agent` 的成交 —— 那正是「编辑代执行」的形状。审计必须点名它。
    """
    import sqlite3

    from stocklab.paper.config import ARM_AGENT, ARM_AGENT_RANDOM
    from stocklab.store.migrate import SCHEMA_SQL

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO paper_trades (account_id, date, code, side, ref_price, fill_price,"
        " qty, commission, stamp_tax, transfer_fee, slippage_cost, fee_total,"
        " asset_class, rule_citation, reason, binding_json, price_source, price_asof,"
        " created_at) VALUES (?,'2026-09-22','510300','buy',4.5,4.5,100,5,0,0,0,5,"
        " 'etf','x','y','[]','bars','2026-09-22','t')", (ARM_AGENT,))
    audit = agent_decide.audit_decisions(
        conn, arms=(ARM_AGENT, ARM_AGENT_RANDOM))
    conn.close()
    assert audit["checked"] == 1
    assert audit["ok"] is False, "成交有、台账没有 ⇒ 必须判红"
    assert audit["violations"][0]["arm"] == ARM_AGENT
    assert "没有对应的操盘决策" in audit["violations"][0]["reason"]


def test_a_clean_store_reports_checked_zero_not_ok_by_luck(tmp_path):
    """反向自检：**零比对**不许被读成「检查通过」——`checked` 必须为 0 且写清楚。"""
    import sqlite3

    from stocklab.paper.config import ARM_AGENT
    from stocklab.store.migrate import SCHEMA_SQL

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    audit = agent_decide.audit_decisions(conn, arms=(ARM_AGENT,))
    conn.close()
    assert audit["checked"] == 0 and audit["violations"] == []
    assert "没有比对过" in audit["note"]


def test_agent_pool_reader_is_read_only_sql():
    """候选池读取**不 import 选股链路**（`stocklab.candidate`）—— 护栏不为它开洞。

    它读的是**已落库的快照行**（append-only 的历史），打分内核跑在别处。
    """
    mods = _imported_modules(ast.parse(Path(agent_pool.__file__).read_text(
        encoding="utf-8")))
    assert not [m for m in mods if m.startswith("stocklab.candidate")], sorted(mods)


def test_engine_dispatches_by_arm_and_keeps_one_decision_source():
    """`engine` 的分派：静态臂走条文、智能体臂走台账 —— 两条路各自只有一个入口。"""
    src = Path(paper_engine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert "trades_by_decision" in _identifiers(tree), \
        "「成交来自台账」这件事必须有名字，否则分派会散落成 if arm == ..."
    assert "replays_own_trades" in _identifiers(tree), \
        "「状态从哪来」与「会不会下单」必须分开表达（P52 的 random 臂会交易）"
