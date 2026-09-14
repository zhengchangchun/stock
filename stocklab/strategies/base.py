"""统一策略接口（Task 26，P5）。

**这个模块存在的意义**：把「策略不许看未来」「参数不许静默夹紧」「没声明的字段不许读」
从三条口头纪律变成**结构上不可能违反**的接口约束。

三道结构性防线（每一道都配了变异反证）：

1. **PIT 裁剪在派发之前**：`Strategy.generate` 是模板方法，先 `clip_history` /
   `clip_features` 把数据裁到 `<= date`，再调用子类实现的 `_generate`。
   子类**收不到**未来数据 —— 前视在策略层不是「被测试挡住」，是无米可炊
   （与 Task 19 的教训同款：防线要放在结构上，不能指望每个调用点都记得裁）。
2. **子类禁止覆写 `generate`**：由 `__init_subclass__` 在**类定义时**报错。
   否则「覆写 generate 直接读 history」就能绕过第 1 条 —— 那是类型层面的漏洞，
   不是注释能挡住的。
3. **`FeatureView` 只放行声明过的字段**：未声明字段**读就抛**，
   连 `features.get("regime_label", 0)` 这种「缺失当默认值」的写法都写不出来。
   本项目当前 `regime_label` / `main_net_5d` / `pe_pct_3y` 全为 NULL，
   这条防线让「NULL 被当成 0」在策略层不可能发生。

`Signal` 复用 `backtest.engine` 的定义：成交语义只有一个真源，策略层不另立一套。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Iterable, Sequence

from stocklab.backtest.engine import Signal
from stocklab.data.models import Bar


class ParamError(ValueError):
    """参数未知 / 类型错 / 越界 —— 一律**报错**，绝不静默 clamp。

    静默 clamp 的后果不是「结果差一点」，而是**单变量实验无法归因**：
    你改了 `fast=300` 却按 `250` 跑，得到的结论属于另一个参数，
    而实验台账上写的是 300（ERROR_DIARY 2026-09-15 同款：
    未知参数名静默忽略会让整轮实验得出「改了参数但结果没变」的假结论）。
    """


@dataclass(frozen=True)
class ParamSpec:
    """一个策略参数的**模式**：类型、默认值、合法闭区间。"""

    name: str
    kind: type            # int | float
    default: float | int
    low: float | int
    high: float | int
    doc: str = ""

    def coerce(self, value) -> float | int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamError(
                f"参数 {self.name} 期望 {self.kind.__name__}，收到 "
                f"{type(value).__name__}({value!r})"
            )
        if self.kind is int:
            if float(value) != int(value):
                raise ParamError(
                    f"参数 {self.name} 期望整数，收到 {value!r}（小数会被隐式取整 → 拒绝）"
                )
            value = int(value)
        else:
            value = float(value)
        if not (self.low <= value <= self.high):
            raise ParamError(
                f"参数 {self.name}={value} 越界：合法区间 [{self.low}, {self.high}]。"
                "本接口**不做 clamp** —— 静默夹紧会让参数实验无法归因"
                "（你以为跑的是 300，实际跑的是 250）。"
            )
        return value


def resolve_params(specs: Sequence[ParamSpec], overrides: Mapping | None = None) -> dict:
    """把覆盖值合并进默认值，逐个过 `ParamSpec.coerce`。

    未知参数名 → `ParamError`（与 `features.registry.effective_params` 同一纪律）。
    """
    overrides = dict(overrides or {})
    known = {s.name: s for s in specs}
    unknown = sorted(set(overrides) - set(known))
    if unknown:
        raise ParamError(
            f"未知策略参数：{unknown}；合法参数：{sorted(known)}。"
            "写错名字若静默忽略，会让一整轮单变量实验得出假结论"
        )
    return {s.name: s.coerce(overrides.get(s.name, s.default)) for s in specs}


class UndeclaredFeature(LookupError):
    """读了策略**没有声明**的特征字段。

    刻意继承 `LookupError` 而**不是** `KeyError`：`Mapping.get` 只吞 `KeyError`，
    于是 `features.get("regime_label", 0)` 也会把本异常抛出去 ——
    「缺失字段当默认值」这条退路被结构性堵死。
    """


class FeatureView(Mapping):
    """按 `declared` 白名单放行的只读特征视图。

    - 未声明字段：`[]` / `.get()` / `in` 一律抛 `UndeclaredFeature`；
    - 已声明但当日缺失（或值为 NULL）：正常返回 `None`，由策略自己决定
      「缺数据就不出手」，**不得**由本层代填 0。
    """

    __slots__ = ("_data", "_declared", "_code")

    def __init__(self, data: Mapping, declared: Iterable[str], *, code: str = ""):
        self._data = dict(data or {})
        self._declared = frozenset(declared)
        self._code = code

    def _check(self, key):
        if key not in self._declared:
            raise UndeclaredFeature(
                f"{self._code or '?'} 读取了未声明的特征 {key!r}；"
                f"已声明：{sorted(self._declared) or '（无）'}。"
                "策略必须先在 required_features 里声明才能读 —— "
                "这条规则让「缺失字段被当成 0/默认值」在结构上不可能发生。"
            )

    def __getitem__(self, key):
        self._check(key)
        return self._data[key]

    def __contains__(self, key):
        self._check(key)
        return key in self._data

    def __iter__(self):
        return iter(sorted(self._declared & set(self._data)))

    def __len__(self):
        return sum(1 for k in self._data if k in self._declared)

    def raw(self) -> dict:
        """已声明字段的原始取值（报告用；不经过白名单之外的数据）。"""
        return {k: v for k, v in self._data.items() if k in self._declared}

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"FeatureView(code={self._code!r}, declared={sorted(self._declared)})"


def clip_history(date: str, history: Mapping[str, Sequence[Bar]]) -> dict[str, list[Bar]]:
    """**强制**裁剪：每个标的只保留 `date`（含）之前的 K 线，按日期升序。

    这是策略层自己的防线（引擎侧另有一道）。两道都留的理由：
    策略会被多个 harness 调用（回测 / P6 预测 / 单测），
    把「不许看未来」放在**策略接口本身**，才不依赖调用方记性。
    """
    out: dict[str, list[Bar]] = {}
    for code, bars in (history or {}).items():
        out[code] = sorted((b for b in bars if b.date <= date), key=lambda b: b.date)
    return out


def clip_features(date: str, features_by_date: Mapping | None) -> dict[str, dict]:
    """只取 `<= date` 的**最近一份**特征快照（快照是日期键控的）。

    取「最近一份 ≤ date」而不是「必须等于 date」：停牌或特征缺失时，
    策略拿到的是旧快照 —— 但那是**过去**的数据，不是未来，
    且 `required_history` 保证快照本身不含未来（见 `features.snapshot.usable_bars`）。
    """
    if not features_by_date:
        return {}
    usable = [d for d in features_by_date if d <= date]
    if not usable:
        return {}
    return dict(features_by_date[max(usable)] or {})


def make_feature_views(clipped: Mapping[str, Mapping], declared: Iterable[str]
                       ) -> dict[str, FeatureView]:
    declared = tuple(declared)
    return {code: FeatureView(row, declared, code=code)
            for code, row in (clipped or {}).items()}


def missing_declared_features(clipped: Mapping[str, Mapping], declared: Iterable[str]
                              ) -> dict[str, list[str]]:
    """「声明了但当日取不到值」的字段（含值为 NULL 的情形），按标的列出。

    报告里必须出现这个数字：策略声明了却不存在的特征，
    意味着它的决策建立在一部分空输入上 —— 这属于必须披露的口径，
    而不是「反正 None 也能跑」。
    """
    declared = tuple(declared)
    out: dict[str, list[str]] = {}
    for code, row in (clipped or {}).items():
        absent = [k for k in declared if row.get(k) is None]
        if absent:
            out[code] = sorted(absent)
    return out


class Strategy(ABC):
    """策略抽象基类：子类只实现 `_generate`，**不得**覆写 `generate`。

    - `PARAMS`：参数模式（`ParamSpec` 元组）。传参经 `resolve_params` 校验，
      结果放在 `self.params`。
    - `required_features`：允许读取的特征字段白名单（默认空 = 一个都不许读）。
    """

    #: 注册表主键（全局唯一）。
    strategy_id: str = ""
    #: 参数模式。
    PARAMS: tuple[ParamSpec, ...] = ()
    #: 允许读取的特征字段（`features_daily` 的列名 / json_payload 的键）。
    required_features: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "generate" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} 不得覆写 generate()：PIT 裁剪发生在基类 generate() "
                "派发之前，覆写它等于绕过裁剪（策略就能直接读未来的 K 线）。"
                "请实现 _generate()。"
            )

    def __init__(self, **overrides):
        if not self.strategy_id:
            raise ValueError(f"{type(self).__name__} 未声明 strategy_id（注册表主键）")
        self.params: dict = resolve_params(self.PARAMS, overrides)

    # ---------- 模板方法：裁剪 → 派发 ----------

    def generate(self, date: str, history: Mapping[str, Sequence[Bar]],
                 features_by_date: Mapping | None = None) -> dict[str, Signal]:
        """**不要覆写本方法**（`__init_subclass__` 会直接报错）。

        `history` / `features_by_date` 允许含任意日期的数据，
        本方法负责把它们裁到 `<= date` 再交给 `_generate`。
        """
        pit_history = clip_history(date, history)
        pit_features = clip_features(date, features_by_date)
        views = make_feature_views(pit_features, self.required_features)
        return dict(self._generate(date, pit_history, views) or {})

    @abstractmethod
    def _generate(self, date: str, pit_history: Mapping[str, list[Bar]],
                  pit_features: Mapping[str, FeatureView]) -> dict[str, Signal]:
        """出 `date` 日收盘后的信号。收到的数据**已被裁剪到 `<= date`**。"""

    # ---------- 元信息 ----------

    def resolved_params(self) -> dict:
        return dict(self.params)

    @classmethod
    def describe(cls) -> dict:
        """参数模式的可序列化描述（CLI 帮助 / 报告披露用）。"""
        return {
            "strategy_id": cls.strategy_id,
            "required_features": list(cls.required_features),
            "params": [
                {"name": s.name, "type": s.kind.__name__, "default": s.default,
                 "low": s.low, "high": s.high, "doc": s.doc}
                for s in cls.PARAMS
            ],
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"{type(self).__name__}({self.params})"
