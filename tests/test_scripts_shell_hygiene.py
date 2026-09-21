"""`scripts/*.sh` 的可用性护栏（对应 ERROR_DIARY #46）。

背景：macOS 自带 bash 3.2.57 在 zh_CN.UTF-8 下解析 `$TASK（` 时，会把全角括号 `（`
的首字节 `0xEF` 并进变量名 ⇒ `set -u` 判未定义并退出。`scripts/verify.sh` 的
**失败分支**正踩了这个坑：真实库红线转红时，脚本自己先崩，既不打印
「❌ 验证失败: …」也不按「有 ❌ 项」返回。

本文件两道护栏：
1. 静态：`scripts/*.sh` 里 `$VAR` 后紧跟非 ASCII 字节即失败（必须写 `${VAR}`）；
2. 端到端：把 `verify.sh` 拷进空目录跑（必红且秒退），断言失败结论可见、退出码为 1、
   且没有 `unbound variable`。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))

# `$VAR` 后紧跟非 ASCII 字节（中文标点/汉字）—— 变量名会被吞掉一个字节
UNBRACED_BEFORE_NON_ASCII = re.compile(rb"\$[A-Za-z_][A-Za-z0-9_]*(?=[\x80-\xff])")


def test_scripts_present() -> None:
    """护栏自身不能空转：没有脚本可比对时，下面的断言毫无意义（见 ERROR_DIARY #40）。"""
    assert SCRIPTS, f"没有找到任何 shell 脚本：{ROOT / 'scripts'}"


def test_no_unbraced_var_before_multibyte_char() -> None:
    offenders: list[str] = []
    for path in SCRIPTS:
        text = path.read_bytes()
        for lineno, line in enumerate(text.split(b"\n"), start=1):
            if UNBRACED_BEFORE_NON_ASCII.search(line):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno}: {line.decode('utf-8', 'replace').strip()}"
                )
    assert not offenders, (
        "变量插值后紧跟非 ASCII 字符时必须写成 ${VAR}（ERROR_DIARY #46）：\n  "
        + "\n  ".join(offenders)
    )


def test_verify_sh_failure_branch_is_readable(tmp_path: Path) -> None:
    """把 verify.sh 放进空目录 ⇒ 第 1 步（文档结构）必红，正好走到失败分支。"""
    target = tmp_path / "verify.sh"
    target.write_bytes((ROOT / "scripts" / "verify.sh").read_bytes())

    proc = subprocess.run(
        ["bash", str(target)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr

    assert "unbound variable" not in out, f"失败分支自身崩了：\n{out}"
    assert "❌ 验证失败: all" in out, f"看不到失败结论行：\n{out}"
    assert proc.returncode == 1, f"退出码应为 1（有 ❌ 项），实际 {proc.returncode}：\n{out}"
