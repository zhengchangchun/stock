"""CLI 连接护栏：拿过 `conn` 的函数，**每条**退出路径都必须关得掉。

## 为什么需要这条（P51 T1）

`cmd_paper_spec_set` 改之前共 5 条退出路径，其中两条（撞键 exit 1 / 幂等 exit 0）
直接 `return`，**没有** `conn.close()` —— 同一函数里三条出口关、两条不关。
CLI 进程随即退出，所以现实伤害 ≈ 0；但同进程复用（labweb 起子进程之前先调一次，
或这个函数被别处 import 调用）就是真的句柄 / 写锁泄漏。

## 为什么护栏落在这三件事上

1. **运行时可观测性拿不到**：`sqlite3.Connection` 被 GC 时照样回收连接，
   「泄漏了几个连接」在纯 Python 里数不出稳定数字。**无法机械观测泄漏本身**，
   所以要拦住的是「不关就 return」这个**源码形态**，不是「泄漏了多少」。
2. **`except` 里补 `close()` 会漏**：新增一条早退（或把某个 `return` 挪出
   `try`）不会有任何测试变红 —— 这正是那个 bug 能活下来的原因。
3. **`finally` 能把这条规则收成一句话**：本护栏因此只认 `try/finally` 里那次
   `conn.close()`，**不认**「在每个 `return` 前面各补一次 close」——
   后者每加一条早退就要再记得补一次，护栏会退化成「人看」。

风格与 `test_source_no_dead_code.py` 一致：只用 `ast`，不引入 linter。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "stocklab" / "cli" / "main.py"

#: 连接变量名（`conn, code = _paper_conn(args)`）与关闭方法名。
CONN = "conn"
CLOSE = "close"

#: 被判的函数。只判这个：`paper metrics` 等邻居已经是 `try/finally` 形态，
#: 把护栏铺满全文件会顺手改到别的任务书范围之外的函数（反目标：不顺手重构）。
GUARDED = ("cmd_paper_spec_set",)

#: 判据里那两条早退的行号锚（改实现后行号会动，所以只当作「找得到」的弱锚）。
_MIN_RETURNS = 3


def _source() -> str:
    return MAIN.read_text(encoding="utf-8")


def _closes_conn(stmts: list[ast.stmt]) -> bool:
    """这段语句里有没有 `conn.close()` 调用。"""
    for stmt in stmts:
        for node in ast.walk(stmt):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == CLOSE
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == CONN):
                return True
    return False


def _is_conn_is_none(node: ast.If) -> bool:
    """`if conn is None:` —— 这一支里 conn 根本不存在，没有东西可关。"""
    test = node.test
    return (isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name) and test.left.id == CONN
            and any(isinstance(op, ast.Is) for op in test.ops)
            and any(isinstance(c, ast.Constant) and c.value is None
                    for c in test.comparators))


def _find(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"main.py 里找不到 {name} —— 护栏扫错目标就是空转")


def _unclosed_exits(func: ast.FunctionDef) -> list[str]:
    """拿过 conn 却没关就 `return` 的退出点。

    判据只有一条：函数体里（**顶层**）必须有一个 `finally: conn.close()` 的 `try`，
    且每个 `return` 要么落在它里面，要么在它**之后**（那时 `finally` 已经跑过）。
    唯一的例外是 `if conn is None: return code` —— 那一支里连接压根没建起来。

    ⚠️ 它**不认**「在每个出口前面各补一次 `conn.close()`」：那种写法下加一条早退
    不会有任何东西变红（这正是 T1 那个 bug 存活的原因），所以护栏只认收口。
    """
    body = func.body
    guard_at = [i for i, stmt in enumerate(body)
                if isinstance(stmt, ast.Try) and _closes_conn(stmt.finalbody)]

    exempt: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.If) and _is_conn_is_none(node):
            exempt |= {id(n) for n in ast.walk(node) if isinstance(n, ast.Return)}

    # 没有收口 ⇒ 从第 0 条语句起「还没关」，所有非豁免的 return 一律判红。
    closed_from = guard_at[0] if guard_at else len(body)
    out: list[str] = []
    for i, stmt in enumerate(body):
        if i >= closed_from:
            break
        for node in ast.walk(stmt):
            if isinstance(node, ast.Return) and id(node) not in exempt:
                out.append(f"main.py:{node.lineno}: `{_snippet(node)}` 退出时 conn 没关")
    return out


def _snippet(node: ast.AST) -> str:
    text = " ".join(ast.unparse(node).split())
    return text[:60] + ("…" if len(text) > 60 else "")


def test_the_scanned_function_exists_and_has_exits() -> None:
    """护栏自身不能空转：目标函数找不到、或它根本没有几处退出，
    下面那条断言就是绿的 —— 那种绿是假的（ERROR_DIARY #40 家族）。"""
    func = _find(ast.parse(_source()), GUARDED[0])
    returns = [n for n in ast.walk(func) if isinstance(n, ast.Return)]
    assert len(returns) >= _MIN_RETURNS, (
        f"{GUARDED[0]} 只扫到 {len(returns)} 处 return —— 目标大概是找错了")


def test_every_exit_closes_the_connection() -> None:
    tree = ast.parse(_source())
    offenders: list[str] = []
    for name in GUARDED:
        offenders.extend(_unclosed_exits(_find(tree, name)))
    assert not offenders, (
        "拿过 conn 却在没关连接的情况下 return（P51 T1）：\n  " + "\n  ".join(offenders))


def test_the_guard_catches_the_pre_fix_shape(tmp_path: Path) -> None:
    """反向自检（1）：**改之前那段真代码**喂进来必须被判红。

    形态取自 P51 任务书 T1 的引文：`try` 里有出口、`finally` 一个字都没有。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(args):\n"
        "    conn, code = _paper_conn(args)\n"
        "    if conn is None:\n"
        "        return code\n"
        "    try:\n"
        "        existing = look(conn)\n"
        "        if existing:\n"
        "            return _spec_conflict(existing)\n"
        "        return 0\n"
        "    except Violation as exc:\n"
        "        conn.close()\n"
        "        return _paper_fail(exc)\n",
        encoding="utf-8")
    hits = _unclosed_exits(_find(ast.parse(bad.read_text(encoding="utf-8")), "f"))
    assert len(hits) == 3, f"三条退出都该判红，实际 {hits}"
    # 两条早退是任务书点名的那两条；第三条 `return _paper_fail(exc)` 前面**有**
    # `conn.close()` —— 它体现的是护栏的第二层判断：**逐出口各补一次 close**
    # 不算收口（下次加早退的人多半会忘），只认 `finally`。这正是 T1 选 finally 的理由。
    assert all("没关" in h for h in hits)


def test_the_guard_does_not_flag_a_fully_guarded_function(tmp_path: Path) -> None:
    """反向自检（2）：全 `try/finally` 的形态不许误报（否则护栏只会被人关掉）。"""
    good = tmp_path / "good.py"
    good.write_text(
        "def f(args):\n"
        "    conn, code = _paper_conn(args)\n"
        "    if conn is None:\n"
        "        return code\n"
        "    try:\n"
        "        if hit(conn):\n"
        "            return 1\n"
        "        return 0\n"
        "    finally:\n"
        "        conn.close()\n",
        encoding="utf-8")
    assert _unclosed_exits(_find(ast.parse(good.read_text(encoding="utf-8")), "f")) == []


def test_removing_the_finally_from_the_real_source_turns_the_guard_red() -> None:
    """反向自检（3）：把**真源码**里的收口摘掉，护栏必须当场变红。

    前两条自检用的是自己写的样例 —— 它们证明不了「这条护栏盯得住今天这份
    `main.py`」。这里直接对真文件做变体：`finally:` 换成 `pass` 之后，
    原来被收口盖住的那几条退出必须全部暴露出来。
    """
    src = _source()
    tree = ast.parse(src)
    func = _find(tree, GUARDED[0])
    for stmt in func.body:
        if isinstance(stmt, ast.Try) and _closes_conn(stmt.finalbody):
            stmt.finalbody = []          # 变体：把收口摘掉，其余一字不动
            break
    else:
        raise AssertionError(
            "目标函数顶层没有 `finally: conn.close()` —— 要么实现被换了形态，"
            "要么这条自检已经指错了地方（两种都该让本用例红，不许静默通过）")
    hits = _unclosed_exits(_find(ast.parse(ast.unparse(tree)), GUARDED[0]))
    assert hits, "摘掉 finally 之后护栏还是绿的 —— 它拦不住真正的回归"
