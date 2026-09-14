"""Task 26：统一策略接口（P5）—— 参数模式 / PIT 强制裁剪 / 特征白名单。

本文件的三条主线，每条都对应一个**结构性**保证（不是靠调用方自觉）：
  1. 参数越界 → 报错，**不 clamp**（静默夹紧会让单变量实验无法归因）；
  2. `generate` 派发**之前**裁剪 `<= date`（子类收不到未来数据）；
  3. 未声明特征读就抛（「缺失字段当 0/默认值」这条退路被堵死）。
"""

import pytest

from stocklab.data.models import Bar
from stocklab.strategies.base import (FeatureView, ParamError, ParamSpec, Strategy,
                                      UndeclaredFeature, clip_features, clip_history,
                                      make_feature_views,
                                      missing_declared_features, resolve_params)


def bars(code, dates, closes):
    return [Bar(code=code, date=d, open=c, high=c + 0.1, low=c - 0.1, close=c,
                volume=10_000, amount=c * 10_000, turnover=1.0,
                source="test", adj_mode="qfq")
            for d, c in zip(dates, closes)]


DATES = [f"2026-03-{d:02d}" for d in range(2, 12)]


class Spy(Strategy):
    """把 `_generate` 实际收到的数据录下来（用于证明裁剪发生在其之前）。"""

    strategy_id = "spy"
    PARAMS = (ParamSpec("window", int, 3, 1, 10, "窗口"),)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen: list[tuple] = []

    def _generate(self, date, pit_history, pit_features):
        self.seen.append((date,
                          {c: [b.date for b in v] for c, v in pit_history.items()},
                          {c: dict(v.raw()) for c, v in pit_features.items()}))
        return {}


class FeatSpy(Strategy):
    strategy_id = "feat_spy"
    required_features = ("close", "regime_label")

    def __init__(self, **kw):
        super().__init__(**kw)
        self.got: dict = {}

    def _generate(self, date, pit_history, pit_features):
        view = pit_features.get("000001")
        if view is not None:
            self.got = {"close": view["close"], "regime": view["regime_label"]}
        return {}


# ---------- 1. 参数模式：越界报错，不 clamp ----------

def test_param_defaults_are_explicit():
    assert resolve_params(Spy.PARAMS, None) == {"window": 3}
    assert resolve_params(Spy.PARAMS, {"window": 7}) == {"window": 7}


def test_param_out_of_range_raises_not_clamps():
    with pytest.raises(ParamError) as e:
        resolve_params(Spy.PARAMS, {"window": 999})
    msg = str(e.value)
    assert "999" in msg and "[1, 10]" in msg
    # 关键：错误信息必须点破「不做 clamp」的理由，否则调用方会以为是实现缺陷
    assert "clamp" in msg

    with pytest.raises(ParamError):
        resolve_params(Spy.PARAMS, {"window": 0})


def test_param_type_errors():
    with pytest.raises(ParamError):
        resolve_params(Spy.PARAMS, {"window": "3"})
    with pytest.raises(ParamError):
        resolve_params(Spy.PARAMS, {"window": 3.5})     # int 参数收到小数 → 拒绝
    with pytest.raises(ParamError):
        resolve_params(Spy.PARAMS, {"window": True})    # bool 是 int 子类，必须显式排除


def test_unknown_param_name_raises():
    with pytest.raises(ParamError) as e:
        resolve_params(Spy.PARAMS, {"windwo": 3})
    assert "windwo" in str(e.value)


def test_strategy_requires_strategy_id():
    class NoId(Strategy):
        def _generate(self, date, h, f):
            return {}

    with pytest.raises(ValueError):
        NoId()


# ---------- 2. PIT：裁剪发生在派发之前 ----------

def test_generate_clips_future_bars():
    spy = Spy()
    history = {"000001": bars("000001", DATES, [10.0] * 10)}
    spy.generate("2026-03-05", history)
    seen_dates = spy.seen[-1][1]["000001"]
    assert seen_dates == DATES[:4]                 # 只到 03-05
    assert all(d <= "2026-03-05" for d in seen_dates)


def test_generate_never_hands_over_future_rows():
    """把整个含未来的序列喂进去，策略侧一行未来数据都见不到。"""
    spy = Spy()
    history = {"000001": bars("000001", DATES, [10.0] * 10),
               "600690": bars("600690", DATES, [20.0] * 10)}
    spy.generate("2026-03-04", history)
    for code, dates in spy.seen[-1][1].items():
        assert dates, f"{code} 不该为空"
        assert max(dates) <= "2026-03-04", f"{code} 看到了未来数据：{max(dates)}"


def test_generate_sorts_clipped_history():
    """乱序输入也要按日期升序交给策略 —— 否则 MA 会算错且无人察觉。"""
    spy = Spy()
    shuffled = list(reversed(bars("000001", DATES, [10.0] * 10)))
    spy.generate("2026-03-11", {"000001": shuffled})
    assert spy.seen[-1][1]["000001"] == DATES


def test_subclass_cannot_override_generate():
    """覆写 `generate` 即可绕过裁剪 —— 必须在**类定义时**就报错（类型层面）。"""
    with pytest.raises(TypeError) as e:

        class Sneaky(Strategy):
            strategy_id = "sneaky"

            def generate(self, date, history, features=None):
                return {}

            def _generate(self, date, h, f):
                return {}

    assert "不得覆写 generate" in str(e.value)


def test_clip_history_keeps_same_day():
    out = clip_history("2026-03-05", {"000001": bars("000001", DATES, [10.0] * 10)})
    assert out["000001"][-1].date == "2026-03-05"


def test_clip_history_empty_input():
    assert clip_history("2026-03-05", {}) == {}
    assert clip_history("2026-03-05", None) == {}


# ---------- 3. features：日期键控 + 白名单 ----------

def test_clip_features_takes_latest_not_after_date():
    feats = {"2026-03-04": {"000001": {"close": 1.0}},
             "2026-03-06": {"000001": {"close": 9.9}}}
    assert clip_features("2026-03-04", feats) == {"000001": {"close": 1.0}}
    assert clip_features("2026-03-05", feats) == {"000001": {"close": 1.0}}   # 缺 03-05 → 取最近过去
    assert clip_features("2026-03-01", feats) == {}                          # 全在未来 → 空
    assert clip_features("2026-03-06", feats) == {"000001": {"close": 9.9}}


def test_feature_view_rejects_undeclared_read():
    view = FeatureView({"close": 10.0, "regime_label": None, "pe_pct_3y": 0.5},
                       ["close", "regime_label"], code="000001")
    assert view["close"] == 10.0
    assert view["regime_label"] is None            # 声明了但为 NULL → None，**不是 0**
    with pytest.raises(UndeclaredFeature):
        view["pe_pct_3y"]


def test_feature_view_get_does_not_swallow_undeclared():
    """`Mapping.get` 只吞 KeyError；本异常刻意继承 LookupError，
    于是 `features.get("pe_pct_3y", 0)` 也抛 —— 「缺失当默认值」写不出来。"""
    view = FeatureView({"close": 10.0}, ["close"], code="000001")
    with pytest.raises(UndeclaredFeature):
        view.get("pe_pct_3y", 0.0)
    with pytest.raises(UndeclaredFeature):
        "pe_pct_3y" in view
    assert view.get("close", 0.0) == 10.0


def test_declared_but_absent_returns_default_not_zero():
    view = FeatureView({}, ["close"], code="000001")
    assert view.get("close", None) is None


def test_strategy_never_receives_undeclared_features():
    spy = Spy()                                     # required_features = () 空
    feats = {"2026-03-05": {"000001": {"close": 10.0, "regime_label": None}}}
    spy.generate("2026-03-05", {"000001": bars("000001", DATES, [10.0] * 10)}, feats)
    view = spy.seen[-1][2]["000001"]
    assert view == {}                               # 白名单为空 → 什么都拿不到
    assert len(FeatureView({"close": 1.0}, (), code="x")) == 0


def test_feat_spy_reads_declared_only():
    spy = FeatSpy()
    feats = {"2026-03-05": {"000001": {"close": 12.5, "regime_label": None,
                                       "pe_pct_3y": 0.9}}}
    spy.generate("2026-03-05", {"000001": bars("000001", DATES, [10.0] * 10)}, feats)
    assert spy.got == {"close": 12.5, "regime": None}


def test_make_feature_views_and_missing_report():
    clipped = {"000001": {"close": 1.0, "ma20": None}}
    views = make_feature_views(clipped, ["close", "ma20", "regime_label"])
    assert views["000001"]["ma20"] is None
    assert missing_declared_features(clipped, ["close", "ma20", "regime_label"]) \
        == {"000001": ["ma20", "regime_label"]}
    assert missing_declared_features(clipped, ["close"]) == {}


# ---------- describe（报告/CLI 用的元信息） ----------

def test_describe_is_serializable():
    import json

    d = Spy.describe()
    assert d["strategy_id"] == "spy"
    assert d["params"][0]["default"] == 3
    json.dumps(d)                                   # 必须可直接进 JSON 报告
