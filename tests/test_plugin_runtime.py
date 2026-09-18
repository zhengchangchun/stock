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


def test_non_whitelisted_builtin_raises_name_error():
    """A builtin absent from _ALLOWED_BUILTINS must be unreachable inside a script.

    `type` is not on the whitelist and is not blocked by guard.check_source
    (guard only bans dangerous constructs, not all non-whitelisted names).
    Calling `type(42)` inside a plugin therefore raises NameError — the name
    is simply not defined in the restricted exec namespace.

    Discrimination: if `type` were added to _ALLOWED_BUILTINS, the script
    would return score=1.0 instead of raising, and this test would fail.
    """
    fn = load_script(
        "def run(ctx):\n    _ = type(42)\n"
        "    return {'score': 1.0, 'pass_flag': True, 'reason': 'r', 'risk_list': []}\n",
        plugin_id="1",
    )
    with pytest.raises(NameError):
        fn({})


def test_namespace_builtins_whitelist():
    """Namespace factory must expose whitelisted names and exclude dangerous ones.

    Asserts on the dict contents of _make_namespace().__builtins__ directly,
    not through a plugin script that cannot observe the exec namespace.
    """
    from stocklab.plugin.runtime import _make_namespace

    ns = _make_namespace()
    builtins = ns["__builtins__"]

    # Dangerous names must be absent
    for forbidden in ("open", "__import__", "eval", "exec", "compile", "dir"):
        assert forbidden not in builtins, f"forbidden builtin leaked: {forbidden!r}"

    # Whitelisted names must be present
    for allowed in ("len", "max", "sum", "min", "abs", "sorted", "range"):
        assert allowed in builtins, f"expected whitelisted builtin missing: {allowed!r}"


def test_host_module_names_unreachable_from_script():
    """Host-module globals must not leak into the plugin exec namespace.

    If isolation holds, accessing `runtime` (a name in the host module
    stocklab.plugin.runtime) from inside the script raises NameError — because
    the exec namespace contains only the whitelist, not the host's globals.
    That NameError propagates out of fn({}) and is caught by pytest.raises.

    If isolation were broken (e.g. host globals passed to exec), `runtime`
    would resolve, the script would return score=1.0, and then
    validate_return would be called with plugin_id="isolation-test" — an id
    that is not in SHAPES — raising PluginContractError instead of NameError.
    pytest.raises(NameError) would NOT be satisfied, so the test correctly
    fails when isolation is absent.
    """
    fn = load_script(
        "def run(ctx):\n"
        "    _ = runtime\n"          # must raise NameError if namespace is clean
        "    return {'score': 1.0, 'pass_flag': True,"
        " 'reason': 'r', 'risk_list': []}\n",
        plugin_id="isolation-test",
    )
    with pytest.raises(NameError):
        fn({})
