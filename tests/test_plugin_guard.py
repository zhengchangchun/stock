"""Task 2：AST 静态预检 —— 负面样本是重点（设计文档 §6.3、§11.1）。"""

import pytest

from stocklab.plugin.guard import PluginGuardError, check_source


def test_clean_script_passes():
    check_source(
        "def run(ctx):\n"
        "    score = max(0.0, min(100.0, ctx['x'] * 2))\n"
        "    return {'score': score, 'pass_flag': True, 'reason': 'ok',"
        " 'risk_list': []}\n"
    )


@pytest.mark.parametrize("source,label", [
    ("import os\ndef run(ctx): return {}", "import"),
    ("from os import path\ndef run(ctx): return {}", "from-import"),
    ("def run(ctx):\n    return __import__('os').getcwd()\n", "__import__"),
    ("def run(ctx):\n    return eval('1+1')\n", "eval"),
    ("def run(ctx):\n    return exec('x=1')\n", "exec"),
    ("def run(ctx):\n    return compile('1','<s>','eval')\n", "compile"),
    ("def run(ctx):\n    return open('/etc/passwd').read()\n", "open"),
    ("def run(ctx):\n    return globals()\n", "globals"),
    ("def run(ctx):\n    return locals()\n", "locals"),
    ("def run(ctx):\n    return vars()\n", "vars"),
    ("def run(ctx):\n    return getattr(ctx, 'x')\n", "getattr"),
    ("def run(ctx):\n    return setattr(ctx, 'x', 1)\n", "setattr"),
    ("def run(ctx):\n    return delattr(ctx, 'x')\n", "delattr"),
    ("def run(ctx):\n    return ().__class__.__bases__\n", "dunder-attr"),
    ("def run(ctx):\n    return ctx.__class__\n", "dunder-class"),
    ("def run(ctx):\n    x = 1\n    def inner():\n        global x\n"
     "        x = 2\n    inner()\n    return x\n", "global-decl"),
    ("def run(ctx):\n    x = 1\n    def inner():\n        nonlocal x\n"
     "        x = 2\n    inner()\n    return x\n", "nonlocal-decl"),
])
def test_dangerous_source_is_rejected(source, label):
    with pytest.raises(PluginGuardError) as e:
        check_source(source)
    # 所有违规路径的消息都包含「禁止」并带行号，便于定位
    assert "禁止" in str(e.value), label


def test_error_reports_line_number():
    with pytest.raises(PluginGuardError) as e:
        check_source("def run(ctx):\n    import os\n    return {}\n")
    assert "第 2 行" in str(e.value)


def test_syntax_error_is_rejected():
    with pytest.raises(PluginGuardError) as e:
        check_source("def run(ctx:\n")
    assert "禁止" in str(e.value)
