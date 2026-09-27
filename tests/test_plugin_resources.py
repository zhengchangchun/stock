"""P93 T1/T2：插桩执行的**内存量具**（`plugin/resources.py`）与两条阈值（`config/limits.py`）。

要钉死的四件事：

1. **单位归一**：`ru_maxrss` 在 darwin 是**字节**、linux 是**千字节** —— 归一算错
   就是静默错 1024 倍，所以 darwin/linux 两条都要有用例，未知平台要**炸**而不是猜；
2. **增量算法**：`delta = 调用后峰值 − 调用前峰值`（`ru_maxrss` 是高水位，恒 >= 0）；
3. **量具只读自身**：没有 fork、没有子进程采样（源码扫描钉住）；
4. **两条阈值**在 `config/limits.py` 里、值逐位对（512MiB / 1536MiB）。

全程离线；`_raw_maxrss` 用 monkeypatch 造序列，真机读数只做「> 0」的存在性检查。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from stocklab.config import limits
from stocklab.plugin import resources

ROOT = pathlib.Path(__file__).resolve().parents[1] / "stocklab"


# ---------- 单位归一 ----------

def test_darwin_unit_is_bytes():
    assert resources.rss_unit_bytes("darwin") == 1


def test_linux_unit_is_kilobytes():
    assert resources.rss_unit_bytes("linux") == 1024


def test_linux_variants_share_the_kilobyte_unit():
    for name in ("linux2", "linux"):
        assert resources.rss_unit_bytes(name) == 1024


def test_unknown_platform_is_rejected_not_guessed():
    """未知平台宁可炸 —— 猜一个单位就是静默错 1024 倍（本仓的一贯纪律）。"""
    with pytest.raises(ValueError) as e:
        resources.rss_unit_bytes("win32")
    assert "win32" in str(e.value)


def test_peak_rss_converts_darwin_bytes(monkeypatch):
    monkeypatch.setattr(resources, "_raw_maxrss", lambda: 1000)
    assert resources.peak_rss_bytes(platform="darwin") == 1000


def test_peak_rss_converts_linux_kilobytes(monkeypatch):
    monkeypatch.setattr(resources, "_raw_maxrss", lambda: 1000)
    assert resources.peak_rss_bytes(platform="linux") == 1024 * 1000


def test_peak_rss_defaults_to_the_running_platform(monkeypatch):
    monkeypatch.setattr(resources, "_raw_maxrss", lambda: 7)
    assert resources.peak_rss_bytes() == 7 * resources.rss_unit_bytes()


def test_real_machine_reading_is_positive():
    """真机读数存在性检查（G2）：一个物理进程的峰值 RSS 不可能 <= 0。"""
    assert resources.peak_rss_bytes() > 0


# ---------- 增量算法 ----------

def _sequence(monkeypatch, values):
    it = iter(values)
    monkeypatch.setattr(resources, "_raw_maxrss", lambda: next(it))


def test_mark_delta_is_peak_difference(monkeypatch):
    _sequence(monkeypatch, [100, 250])         # 进入 → 退出
    with resources.rss_mark(platform="darwin") as mark:
        pass
    assert mark["before"] == 100
    assert mark["after"] == 250
    assert mark["delta"] == 150


def test_mark_delta_is_zero_when_the_peak_does_not_move(monkeypatch):
    """高水位不回落：第二个小调用在 `ru_maxrss` 上看不出变化 ⇒ delta = 0。"""
    _sequence(monkeypatch, [250, 250])
    with resources.rss_mark(platform="darwin") as mark:
        pass
    assert mark["delta"] == 0


def test_mark_delta_never_goes_negative(monkeypatch):
    """`ru_maxrss` 理论上单调；真掉下来的话（平台怪癖）delta 记 0，不记负号。"""
    _sequence(monkeypatch, [250, 100])
    with resources.rss_mark(platform="darwin") as mark:
        pass
    assert mark["delta"] == 0


def test_mark_is_filled_even_when_the_block_raises(monkeypatch):
    """越界的调用往往**就是**抛出来的那个 —— `finally` 里读数才有得判。"""
    _sequence(monkeypatch, [100, 900])
    with pytest.raises(RuntimeError):
        with resources.rss_mark(platform="darwin") as mark:
            raise RuntimeError("boom")
    assert mark["after"] == 900
    assert mark["delta"] == 800


def test_mark_normalises_linux_kilobytes(monkeypatch):
    _sequence(monkeypatch, [1000, 2000])
    with resources.rss_mark(platform="linux") as mark:
        pass
    assert mark["delta"] == 1024 * 1000


# ---------- 量具只读自身（源码扫描） ----------

def _module_tree() -> ast.AST:
    src = (ROOT / "plugin" / "resources.py").read_text(encoding="utf-8")
    return ast.parse(src)


def test_instrument_never_forks_or_spawns():
    """D1：量具只读进程自身 —— 不许 fork / Popen / 子进程采样。"""
    tree = _module_tree()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("fork", "Popen", "subprocess", "Process", "multiprocessing"):
        assert forbidden not in names, f"量具里出现了 {forbidden!r}：越过了 D1 的只读边界"


# ---------- T2：两条阈值 ----------

def test_call_limit_is_512_mib():
    assert limits.PLUGIN_CALL_RSS_LIMIT_BYTES == 512 * 1024 ** 2


def test_process_limit_is_1536_mib():
    assert limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES == 1536 * 1024 ** 2


def test_call_limit_is_below_process_limit():
    """单次上限必须**小于**进程上限，否则「先停」的语义就不成立。"""
    assert (limits.PLUGIN_CALL_RSS_LIMIT_BYTES
            < limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES)
