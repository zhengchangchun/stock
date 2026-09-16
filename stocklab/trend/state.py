"""趋势状态标签（双均线三态）的纯函数层。

## 口径来自预注册，本模块一个字都不许自己发明

`docs/experiments/2026-09-15-trend-state-hit-rate.md` §2 写死：

    UP   : close_t > MA20_t 且 MA20_t > MA60_t
    DOWN : close_t < MA20_t 且 MA20_t < MA60_t
    FLAT : 其余

`MA20 / MA60` 都是**含当日收盘**的简单均值；**无平滑、无迟滞、无 FLAT 阈值**
（FLAT 由两条均线关系直接定义，不引入任何分位阈值）。主视界 `N = 5`。

三条不等式全是**严格**的 —— `close == MA20` 不是 UP，`MA20 == MA60` 一定是 FLAT。
这一条有专门的边界测试（`tests/test_trend_state.py`）：差一个等号，FLAT 的占比
就会整体挪位，而所有下游数字都会跟着变，却看不出哪里错了。

## PIT（t 日状态只用 t 日及之前的收盘）

「只用过去」不能靠自觉。本模块提供 `assert_pit_state_fn()` —— 它用两种**错位输入**
去主动证伪一个状态函数：

  ① **截断自证**：只喂 `closes[:i+1]`，第 `i` 个状态必须与喂全序列时逐字相同；
  ② **未来扰动不变**：把 `i` 之后的任意一根收盘改成极端值，第 `i` 个状态必须不变。

故意写坏的实现（偷看最后一根、用全样本均值、中心窗口错位）会被这两条抓住 ——
对应的测试逐条验过：**守卫只拦一半 = 没拦**（ERROR_DIARY 2026-09-14）。

## 与既有实现的关系

`stocklab.risk.rules.trend_state` 已经按同一口径实现了这条标签（风险面板在用）。
本模块是**独立的一份**，两条理由：① 那条函数属于「风险规则」，把它变成实验的
度量口径会把两件事绑死；② 两份独立实现互为对照 ——
`tests/test_trend_state.py` 里有一条测试把两者在 300 个随机游走点上钉成相等，
口径漂移会当场变红。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

#: 均线窗口（预注册 §2 设计常量，看结果后不许改）。
MA_SHORT = 20
MA_LONG = 60

#: 主视界（预注册 §2：主视界 N = 5 交易日）。
HORIZON = 5

STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_FLAT = "FLAT"

#: 三个状态的固定顺序（报告与判定都按它）。
STATES: tuple[str, ...] = (STATE_UP, STATE_DOWN, STATE_FLAT)

#: 判据只对这两个「趋势态」生效；`FLAT` 是「其余」的兜底，只登记不判定。
TREND_STATES: tuple[str, ...] = (STATE_UP, STATE_DOWN)


class FutureLeakError(RuntimeError):
    """状态函数读了 `t` 之后的数据 —— PIT 被破坏，一切下游数字作废。"""


def sma(closes: Sequence[float], i: int, window: int) -> float | None:
    """`i` 日的 `window` 日均线（**含当日**）。不足 `window` 根 → `None`。

    `None` 而不是抛异常：调用方需要能区分「历史不够」与「算出来是 0」。
    """
    if window <= 0:
        raise ValueError(f"window={window!r} 必须为正")
    if i < window - 1:
        return None
    return sum(closes[i - window + 1:i + 1]) / window


def trend_state(closes: Sequence[float], i: int) -> str | None:
    """`i` 日的趋势状态；不足 `MA_LONG` 根 → `None`。

    **PIT**：只读 `closes[:i+1]`（`sma` 的两窗口都止于 `i`）。
    """
    if i < MA_LONG - 1 or i >= len(closes):
        return None
    c = closes[i]
    ma_s = sma(closes, i, MA_SHORT)
    ma_l = sma(closes, i, MA_LONG)
    assert ma_s is not None and ma_l is not None     # i >= MA_LONG-1 保证
    if c > ma_s and ma_s > ma_l:
        return STATE_UP
    if c < ma_s and ma_s < ma_l:
        return STATE_DOWN
    return STATE_FLAT


def state_series(closes: Sequence[float]) -> list[str | None]:
    """整条序列的状态标签（前 `MA_LONG-1` 个为 `None`）。

    用前缀和做到 O(1)/点：3000 根 × 3 标的在 CLI 里是热路径。
    """
    n = len(closes)
    out: list[str | None] = [None] * n
    if n < MA_LONG:
        return out
    prefix = [0.0] * (n + 1)
    for k, c in enumerate(closes):
        prefix[k + 1] = prefix[k] + c

    def mean(i: int, w: int) -> float:
        return (prefix[i + 1] - prefix[i + 1 - w]) / w

    for i in range(MA_LONG - 1, n):
        c = closes[i]
        ma_s = mean(i, MA_SHORT)
        ma_l = mean(i, MA_LONG)
        if c > ma_s and ma_s > ma_l:
            out[i] = STATE_UP
        elif c < ma_s and ma_s < ma_l:
            out[i] = STATE_DOWN
        else:
            out[i] = STATE_FLAT
    return out


# ---------- 前向收益 / 命中 ----------

def forward_return(closes: Sequence[float], i: int, n: int = HORIZON
                   ) -> float | None:
    """`t=i` → `t=i+n` 的**不复权价格收益**；越过序列末端 → `None`。

    预注册 §3：趋势状态只用价格序列的相对关系，不涉及跨除权日的收益计算 ——
    所以这里是价格收益（若落到交易则必须换复权口径，见预注册 §3）。
    """
    j = i + n
    if i < 0 or j >= len(closes) or i >= len(closes):
        return None
    base = closes[i]
    if base == 0:
        return None
    return (closes[j] - base) / base


def forward_hit(closes: Sequence[float], i: int, n: int = HORIZON) -> int | None:
    """`close_{i+n} > close_i` → 1，否则 0；越过末端 → `None`。

    价格**完全相等**时计 0（非命中）—— 调用方另有 `forward_is_tie()` 把它数出来，
    因为「平局计成什么」是一个会动数字的选择，必须能被读出来而不是埋在实现里。
    """
    j = i + n
    if i < 0 or j >= len(closes) or i >= len(closes):
        return None
    return 1 if closes[j] > closes[i] else 0


def forward_is_tie(closes: Sequence[float], i: int, n: int = HORIZON) -> bool | None:
    """前向价格与当日**完全相等**（平局）→ `True`。用于单独计数上报。"""
    j = i + n
    if i < 0 or j >= len(closes) or i >= len(closes):
        return None
    return closes[j] == closes[i]


def forward_state(closes: Sequence[float], i: int, n: int = HORIZON
                  ) -> str | None:
    """`i+n` 日的状态（延续率用）。越过末端 → `None`。"""
    return trend_state(closes, i + n)


# ---------- PIT 自证 ----------

#: 未来扰动用的两个极端探针：一个把价格推到天上，一个推到地下。
#: 两个方向都要试 —— 只试一个方向时，「读未来做比较」的实现可能两边的
#: 结果都不变，检查器就会漏掉它。
_PROBES: tuple[Callable[[float], float], ...] = (
    lambda v: v * 3.0 + 100.0,
    lambda v: v * 0.1,
)

#: 未来探针的取样上限：超过这个跨度就按步长取样（检查器不该比被测函数还慢）。
_MAX_PROBE_SPAN = 128


def _probe_indices(i: int, n: int) -> list[int]:
    tail = n - i - 1
    if tail <= 0:
        return []
    if tail <= _MAX_PROBE_SPAN:
        return list(range(i + 1, n))
    step = max(1, tail // 64)
    idx = list(range(i + 1, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


def assert_pit_state_fn(fn: Callable[[Sequence[float], int], str | None],
                        closes: Sequence[float], *, at: Sequence[int] | None = None
                        ) -> None:
    """证伪式 PIT 检查：`fn` 在 `at` 这些点上必须只用 `closes[:i+1]`。

    两条独立的检查（任一条不成立 → `FutureLeakError`）：

      ① **截断自证** —— `fn(closes[:i+1], i)` 必须等于 `fn(closes, i)`；
         在截断输入上直接**求不了值**（越界 / `None` 参与比较）同样是泄漏的
         直接证据，所以异常也记泄漏，不吞掉；
      ② **未来扰动不变** —— `i` 之后的每一根（取样后）取 ×3+100 与 ×0.1 两个极端，
         `fn` 在第 `i` 点的输出必须一字不变。

    `at=None` → 检查全部「有状态」的点（`≥ MA_LONG-1`）。
    """
    n = len(closes)
    points = list(at) if at is not None else list(range(MA_LONG - 1, n))
    for i in points:
        base = fn(closes, i)
        try:
            truncated = fn(list(closes[:i + 1]), i)
        except Exception as exc:                      # noqa: BLE001 —— 见 docstring ①
            raise FutureLeakError(
                f"i={i}：在**截断输入**（只有前 {i + 1} 根）上求值失败 "
                f"（{type(exc).__name__}: {exc}）—— "
                "能够通过的历史里求不出这个状态，只可能因为它读了更晚的数据"
            ) from exc
        if truncated != base:
            raise FutureLeakError(
                f"i={i}：截断输入上的状态 {truncated!r} ≠ 全序列上的 {base!r} —— "
                "第 i 个状态被 i 之后的数据改变了"
            )
        for k in _probe_indices(i, n):
            for probe in _PROBES:
                mutated = list(closes)
                mutated[k] = probe(closes[k])
                if fn(mutated, i) != base:
                    raise FutureLeakError(
                        f"i={i}：把第 {k} 根收盘改成 {probe(closes[k])!r} 后，"
                        f"第 {i} 个状态从 {base!r} 变成 {fn(mutated, i)!r} —— "
                        f"状态读了 t={i} 之后的数据（k={k} > i）"
                    )
