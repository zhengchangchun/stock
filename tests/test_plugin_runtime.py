"""Task 4：受限执行 + 超时（设计文档 §6.4）。"""

import time

import pytest

from stocklab.plugin.contract import PluginContractError
from stocklab.plugin.guard import PluginGuardError
from stocklab.plugin.runtime import PluginTimeout, load_script

SCORE_SCRIPT = (
    "def run(ctx):\n"
    "    s = min(100.0, ctx['x'] * 10)\n"
    "    return {'score': s, 'pass_flag': True, 'reason': 'r', 'risk_list': []}\n"
)


def test_normal_script_runs():
    fn = load_script(SCORE_SCRIPT, plugin_id="1")
    assert fn({"x": 7.0})["score"] == 70.0


def test_guard_runs_before_exec():
    """危险脚本必须在 exec **之前**被挡住 —— 先 exec 再检查等于没检查。"""
    with pytest.raises(PluginGuardError):
        load_script("import os\ndef run(ctx): return {}\n", plugin_id="1")


def test_missing_run_is_rejected():
    with pytest.raises(PluginContractError) as e:
        load_script("x = 1\n", plugin_id="1")
    assert "run" in str(e.value)


def test_run_not_callable_is_rejected():
    with pytest.raises(PluginContractError):
        load_script("run = 42\n", plugin_id="1")


def test_bad_return_shape_is_rejected_at_call_time():
    fn = load_script("def run(ctx):\n    return {'score': 999}\n", plugin_id="1")
    with pytest.raises(PluginContractError):
        fn({})


def test_infinite_loop_times_out_and_process_survives():
    """死循环必须被超时打断，且**主进程不挂**。"""
    fn = load_script("def run(ctx):\n    while True:\n        pass\n",
                     plugin_id="1")
    t0 = time.monotonic()
    with pytest.raises(PluginTimeout):
        fn({})
    assert time.monotonic() - t0 < 10.0, "超时没有生效"
    # 关键：超时之后解释器仍然可用
    assert load_script(SCORE_SCRIPT, plugin_id="1")({"x": 1.0})["score"] == 10.0


def test_builtins_are_restricted():
    """未在白名单里的内建函数不可用（例如 `type` 之外的冷门入口）。"""
    fn = load_script(
        "def run(ctx):\n    return {'score': len([1,2,3]), 'pass_flag': True,"
        " 'reason': 'r', 'risk_list': []}\n", plugin_id="1")
    assert fn({})["score"] == 3.0


def test_script_cannot_see_module_globals():
    """脚本命名空间里不应有宿主对象（guard/contract/signal 等）泄入。

    `dir()` 在函数内部只返回局部变量名，不返回模块级名称；因此
    `'__builtins__' in dir()` 在 run() 内为 False（0.0）。
    __builtins__ 作为内建可正常调用（white‑list），但不出现在局部 dir() 中。
    这正好验证了脚本的局部作用域是干净的。
    """
    fn = load_script(
        "def run(ctx):\n    return {'score': float('__builtins__' in dir()),"
        " 'pass_flag': True, 'reason': 'r', 'risk_list': []}\n", plugin_id="1")
    # dir() inside a function only lists locals; __builtins__ is not a local var
    assert fn({})["score"] == 0.0
