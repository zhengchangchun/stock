"""P93 T3：`plugin/runtime.py` 的**两处资源闸门** ＋ `on_call` 读数出口。

要钉死的六件事：

1. **执行前**闸门（D3-1）：进程峰值已达/超过 `PLUGIN_PROCESS_RSS_LIMIT_BYTES`
   ⇒ 抛 `PluginResourceError` 且 `run` **一次都没被调用**（用计数器证明）；
2. **执行后**闸门（D3-2）：本次调用峰值增量 >= `PLUGIN_CALL_RSS_LIMIT_BYTES`
   ⇒ 结果作废、抛 `PluginResourceError`，消息带**实测增量与阈值**；
3. **不做中途 kill**：两条异常消息都写明「不声称能阻止那一次分配，只判废＋留痕＋挡后续」；
4. `PluginResourceError` 是 `PluginTimeout` 的**兄弟**（都直接继承 `Exception`）；
5. `on_call` 只回调三种结局（`ok` / `timeout` / `resource`），键集固定；
6. **不传 `on_call` 时行为与今天逐字相同**（返回值、异常路径、既有用例全绿）。

全程离线；峰值读数一律 monkeypatch（造序列），不靠真机内存。
"""

from __future__ import annotations

import pytest

from stocklab.config import limits
from stocklab.plugin import resources
from stocklab.plugin.runtime import (PluginResourceError, PluginTimeout,
                                     load_script)

#: 一个正常脚本：`ctx["n"]` 是调用计数器（给「run 有没有被调用」做证）。
COUNTING = (
    "def run(ctx):\n"
    "    ctx['n'] = ctx.get('n', 0) + 1\n"
    "    return {'score': 1.0, 'pass_flag': True, 'reason': 'r', 'risk_list': []}\n"
)

OVER_PROCESS = limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES
OVER_CALL = limits.PLUGIN_CALL_RSS_LIMIT_BYTES


def _const(monkeypatch, value: int) -> None:
    monkeypatch.setattr(resources, "peak_rss_bytes", lambda **kw: value)


def _seq(monkeypatch, values) -> None:
    it = iter(values)
    monkeypatch.setattr(resources, "peak_rss_bytes", lambda **kw: next(it))


# ---------- 异常树 ----------

def test_resource_error_is_a_sibling_of_timeout():
    """兄弟而不是父子：`except PluginTimeout` 不许顺带吞掉资源越界。"""
    assert issubclass(PluginResourceError, Exception)
    assert issubclass(PluginTimeout, Exception)
    assert not issubclass(PluginResourceError, PluginTimeout)
    assert not issubclass(PluginTimeout, PluginResourceError)


# ---------- 执行前闸门（G4） ----------

def test_pre_gate_rejects_without_running_a_single_byte(monkeypatch):
    _const(monkeypatch, OVER_PROCESS)
    fn = load_script(COUNTING, plugin_id="1")
    ctx = {"n": 0}
    with pytest.raises(PluginResourceError):
        fn(ctx)
    assert ctx["n"] == 0, "执行前闸门漏了：run 至少被调用了一次"


def test_pre_gate_fires_at_exactly_the_limit(monkeypatch):
    """边界是闭区间：== 上限也算超（`>=`，不是 `>`）。"""
    _const(monkeypatch, OVER_PROCESS)
    fn = load_script(COUNTING, plugin_id="1")
    with pytest.raises(PluginResourceError):
        fn({"n": 0})


def test_pre_gate_rejects_just_below_the_limit(monkeypatch):
    _const(monkeypatch, OVER_PROCESS - 1)
    fn = load_script(COUNTING, plugin_id="1")
    assert fn({"n": 0})["score"] == 1.0


def test_pre_gate_message_names_the_measured_peak_and_the_limit(monkeypatch):
    _const(monkeypatch, OVER_PROCESS + 4096)
    fn = load_script(COUNTING, plugin_id="1")
    with pytest.raises(PluginResourceError) as e:
        fn({})
    msg = str(e.value)
    assert str(OVER_PROCESS + 4096) in msg
    assert str(OVER_PROCESS) in msg


def test_pre_gate_message_admits_it_cannot_prevent_the_allocation(monkeypatch):
    """D3：**不假装能中断** —— 异常消息里必须写明本档只做三件事。"""
    _const(monkeypatch, OVER_PROCESS)
    fn = load_script(COUNTING, plugin_id="1")
    with pytest.raises(PluginResourceError) as e:
        fn({})
    msg = str(e.value)
    assert "判废" in msg and "留痕" in msg
    assert "不声称" in msg or "不可中断" in msg


# ---------- 执行后闸门（G3 的机制） ----------

def test_post_gate_invalidates_the_result(monkeypatch):
    # before 小 → 脚本跑 → after 大（增量 >= 512MiB）
    _seq(monkeypatch, [1000, 1000 + OVER_CALL, 1000 + OVER_CALL])
    fn = load_script(COUNTING, plugin_id="1")
    with pytest.raises(PluginResourceError):
        fn({"n": 0})


def test_post_gate_message_carries_delta_and_limit(monkeypatch):
    delta = OVER_CALL + 12345
    _seq(monkeypatch, [1000, 1000 + delta, 1000 + delta])
    fn = load_script(COUNTING, plugin_id="1")
    with pytest.raises(PluginResourceError) as e:
        fn({})
    msg = str(e.value)
    assert str(delta) in msg
    assert str(OVER_CALL) in msg
    assert "作废" in msg


def test_post_gate_message_admits_it_cannot_prevent_the_allocation(monkeypatch):
    _seq(monkeypatch, [0, OVER_CALL, OVER_CALL])
    fn = load_script(COUNTING, plugin_id="1")
    with pytest.raises(PluginResourceError) as e:
        fn({})
    msg = str(e.value)
    assert "不声称" in msg or "不可中断" in msg


def test_post_gate_does_not_fire_just_below_the_limit(monkeypatch):
    _seq(monkeypatch, [1000, 1000 + OVER_CALL - 1, 1000 + OVER_CALL - 1])
    fn = load_script(COUNTING, plugin_id="1")
    assert fn({"n": 0})["score"] == 1.0


def test_result_is_returned_when_nothing_trips(monkeypatch):
    _seq(monkeypatch, [1000, 2000, 2000])
    fn = load_script(COUNTING, plugin_id="1")
    out = fn({"n": 0})
    assert out == {"score": 1.0, "pass_flag": True, "reason": "r", "risk_list": []}


# ---------- on_call 读数出口（D4） ----------

def test_on_call_receives_ok_record(monkeypatch):
    _seq(monkeypatch, [1000, 2000, 2000])
    records: list[dict] = []
    fn = load_script(COUNTING, plugin_id="1", on_call=records.append)
    fn({"n": 0})
    assert len(records) == 1
    rec = records[0]
    assert set(rec) == {"plugin_id", "outcome", "duration_ms",
                        "rss_delta_bytes", "rss_peak_bytes"}
    assert rec["plugin_id"] == "1"
    assert rec["outcome"] == "ok"
    assert rec["rss_delta_bytes"] == 1000
    assert rec["rss_peak_bytes"] == 2000
    assert isinstance(rec["duration_ms"], float) and rec["duration_ms"] >= 0.0


def test_on_call_receives_timeout_record():
    records: list[dict] = []
    fn = load_script("def run(ctx):\n    while True:\n        pass\n",
                     plugin_id="1", timeout_s=0.05, on_call=records.append)
    with pytest.raises(PluginTimeout):
        fn({})
    assert [r["outcome"] for r in records] == ["timeout"]


def test_on_call_receives_resource_record_for_the_pre_gate(monkeypatch):
    _const(monkeypatch, OVER_PROCESS)
    records: list[dict] = []
    fn = load_script(COUNTING, plugin_id="1", on_call=records.append)
    with pytest.raises(PluginResourceError):
        fn({})
    assert [r["outcome"] for r in records] == ["resource"]


def test_on_call_receives_resource_record_for_the_post_gate(monkeypatch):
    _seq(monkeypatch, [0, OVER_CALL, OVER_CALL])
    records: list[dict] = []
    fn = load_script(COUNTING, plugin_id="1", on_call=records.append)
    with pytest.raises(PluginResourceError):
        fn({})
    assert [r["outcome"] for r in records] == ["resource"]
    assert records[0]["rss_delta_bytes"] == OVER_CALL


def test_on_call_is_called_once_per_call(monkeypatch):
    _seq(monkeypatch, [1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000])
    records: list[dict] = []
    fn = load_script(COUNTING, plugin_id="1", on_call=records.append)
    fn({"n": 0})
    fn({"n": 0})
    assert len(records) == 2


def test_on_call_is_not_called_for_a_non_resource_exception():
    """D4 的取值域只有三词：脚本自己抛的其它异常走既有通道，**不**记资源事件。"""
    records: list[dict] = []
    fn = load_script("def run(ctx):\n    return {'score': 999}\n",
                     plugin_id="1", on_call=records.append)
    with pytest.raises(Exception):
        fn({})
    assert records == []


# ---------- 不传 on_call ⇒ 行为逐字相同 ----------

def test_without_on_call_the_return_value_is_identical(monkeypatch):
    _seq(monkeypatch, [1000, 2000, 2000])
    fn = load_script(COUNTING, plugin_id="1")
    assert fn({"n": 0}) == {
        "score": 1.0, "pass_flag": True, "reason": "r", "risk_list": []}


def test_return_type_is_still_a_dict():
    fn = load_script(COUNTING, plugin_id="1")
    assert isinstance(fn({}), dict)


def test_on_call_does_not_change_the_returned_payload():
    plain = load_script(COUNTING, plugin_id="1")
    hooked = load_script(COUNTING, plugin_id="1", on_call=lambda rec: None)
    assert plain({"n": 0}) == hooked({"n": 0})
