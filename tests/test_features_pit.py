"""R2 的可执行证明（Task 19）：T 日特征只能由 T 日及之前的数据决定。

核心手法：用「截至 T 的全量数据」与「截至 T 的数据 + 大量未来数据（含极端值）」
分别计算 T 日特征，二者必须完全一致。任何前视（lookahead）都会让这个测试变红。

本文件末尾还有一个**永久负向对照**（`test_pit_assertion_has_teeth`）：
故意注入一个前视实现，断言特征确实发生变化 —— 用来证明上面这些断言
真的具备鉴别力，而不是「恰好都通过」。
"""

import pytest

from stocklab.data.models import Bar
from stocklab.features import registry, snapshot
from stocklab.features.snapshot import build_snapshot


def bars(n, start_close=10.0, step=0.1):
    out = []
    for i in range(n):
        c = start_close + i * step
        out.append(Bar(code="000333", date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                       open=c - 0.05, high=c + 0.15, low=c - 0.15, close=c,
                       volume=1000 + i, amount=(1000 + i) * c, turnover=1.0,
                       source="test"))
    return out


ASOF = "2026-03-20"


def test_future_data_does_not_change_past_features():
    past = [b for b in bars(80) if b.date <= ASOF]
    full = bars(80)
    assert past[-1].date == ASOF

    snap_past = build_snapshot("000333", ASOF, past)
    snap_full = build_snapshot("000333", ASOF, full)
    assert snap_past is not None
    assert snap_past.payload_hash == snap_full.payload_hash


def test_extreme_future_spike_does_not_leak():
    """未来暴涨 10 倍也不得改变 T 日特征 —— 这是 lookahead 最强的照妖镜。"""
    past = [b for b in bars(80) if b.date <= ASOF]
    future = [
        Bar(code="000333", date=f"2026-04-{d:02d}", open=1000.0, high=1100.0,
            low=900.0, close=1000.0, volume=10**7, amount=10**10, turnover=50.0,
            source="test")
        for d in range(1, 29)
    ]
    a = build_snapshot("000333", ASOF, past)
    b = build_snapshot("000333", ASOF, past + future)
    assert a.payload_hash == b.payload_hash


def test_future_low_does_not_change_past_atr():
    past = [b for b in bars(80) if b.date <= ASOF]
    future = [
        Bar(code="000333", date=f"2026-04-{d:02d}", open=0.01, high=0.02,
            low=0.01, close=0.01, volume=1, amount=0.01, turnover=0.0,
            source="test")
        for d in range(1, 29)
    ]
    a = build_snapshot("000333", ASOF, past)
    b = build_snapshot("000333", ASOF, past + future)
    assert a.payload_hash == b.payload_hash


def test_exact_asof_row_is_included():
    """asof 日当天的收盘数据必须参与计算（R2 允许用当日收盘）。

    计划原文拿 `past[:-1]`（少一根）对比 —— 那样 asof 当天就没有 K 线了，
    `build_snapshot` 会按「asof 无 K 线 → None」返回（这条守卫本身也是 PIT 要求：
    拿前一日数据贴当日标签正是最典型的前视污染）。故改为「只改当日数值」。
    """
    past = [b for b in bars(80) if b.date <= ASOF]
    snap = build_snapshot("000333", ASOF, past)
    assert snap.core["close"] == pytest.approx(past[-1].close)

    last = past[-1]
    tweaked = past[:-1] + [Bar(code=last.code, date=last.date, open=last.open,
                               high=last.high + 1.0, low=last.low,
                               close=last.close + 1.0, volume=last.volume,
                               amount=last.amount, turnover=last.turnover,
                               source=last.source)]
    assert build_snapshot("000333", ASOF, tweaked).payload_hash != snap.payload_hash


def test_bars_after_asof_are_ignored_even_if_unsorted():
    past = [b for b in bars(80) if b.date <= ASOF]
    future = bars(80)[-5:]
    a = build_snapshot("000333", ASOF, past)
    b = build_snapshot("000333", ASOF, future + past)   # 乱序输入
    assert a.payload_hash == b.payload_hash


def test_input_order_never_matters():
    """任意乱序输入都必须得到同一份快照（哈希是输入的纯函数）。"""
    past = [b for b in bars(80) if b.date <= ASOF]
    shuffled = past[::3] + past[1::3] + past[2::3]
    assert (build_snapshot("000333", ASOF, shuffled).payload_hash
            == build_snapshot("000333", ASOF, past).payload_hash)


def test_snapshot_only_uses_bars_of_its_own_code():
    """跨标的串味检查：混入别的 code 的 K 线不得改变本标的结果。

    注：`build_snapshot` 按调用方的 code 落库，`bars` 由调用方按 code 取。
    这里锁住「同样的入参 → 同样的结果，且数量/内容不被其它 code 影响」的契约：
    若哪天有人在 bars 里混了别的标的，`usable_bars` 按日期去重会把它当同一天冲突
    而报错（而不是静默算进去）。
    """
    past = [b for b in bars(80) if b.date <= ASOF]
    other = [Bar(code="600690", date=b.date, open=b.open, high=b.high, low=b.low,
                 close=b.close * 2, volume=b.volume, amount=b.amount,
                 turnover=b.turnover, source="test") for b in past]
    with pytest.raises(ValueError, match="取值不同"):
        build_snapshot("000333", ASOF, past + other)


# ---------- 负向对照：证明上面的断言真的有鉴别力 ----------

def test_pit_assertion_has_teeth():
    """反证：注入「不裁剪」（最经典的前视 bug）→ T 日特征**必须变化**。

    如果 `build_snapshot` 哪天丢了 `usable_bars` 的裁剪，指标就会吃到未来行，
    上面 `test_future_data_does_not_change_past_features` 立刻变红。
    这里直接把「不裁剪」的输入喂给同一套特征代码来证明这件事 ——
    若本测试变红，说明 PIT 断言已失去鉴别力，上面几条全部失去意义。
    """
    past = [b for b in bars(80) if b.date <= ASOF]
    full = bars(80)

    correct = build_snapshot("000333", ASOF, past).payload_hash
    # 同一份「只看历史」的输入直接过 compute_core，必须得到同一个 hash
    # （证明快照是输入序列的纯函数，没有藏在别处的状态/随机性）
    assert snapshot.payload_hash(registry.compute_core(past)) == correct
    # 不裁剪 → 未来行参与计算 → hash 必须不同
    leaky = snapshot.payload_hash(registry.compute_core(full))
    assert leaky != correct


def test_indicators_never_see_future_rows(monkeypatch):
    """结构性保证：前视在本层**结构上不可能**发生。

    `usable_bars` 先把未来行截掉，指标函数根本收不到未来数据 ——
    所以即使把 sma 换成 `center=True` 这种前视实现，也算不出未来。
    这条测试锁住这个顺序：先裁剪、后计算。
    """
    past = [b for b in bars(80) if b.date <= ASOF]
    full = bars(80)
    seen: dict = {}

    original = registry.compute_core

    def spy(bars_in, **kwargs):
        seen["dates"] = [b.date for b in bars_in]
        return original(bars_in, **kwargs)

    monkeypatch.setattr(registry, "compute_core", spy)
    build_snapshot("000333", ASOF, full)
    assert seen["dates"] == [b.date for b in past]
    assert max(seen["dates"]) == ASOF
