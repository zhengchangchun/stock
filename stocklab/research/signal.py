"""P78 信号有效性度量：逐调仓日 **rank IC** ＋ **分 5 层看前向收益**。

## 它回答什么（**不是**「换一个持有集合赚多少」）

那已经答过了：`research xsec-topn` 比较两个**组合**的收益序列 ⇒ 只对「取前 N 相对持全部」
有分辨率，看不到分数的中间段，一次只出一个数。本模块换一个**度量对象**：

- 每个调仓日跑一次 `score_pipeline`，取该池**全部已打分的合格标的**（不是只有 top-N）；
- 用**当日的分数**对**下一个调仓周期**的前向收益算 **Spearman 秩相关**（次读数 Pearson）；
- 按分数降序分 5 层，看每层的前向收益是否单调（`layer1 − layer5 = spread`）。

它回答的是「**打分本身**对前向收益有没有横截面排序能力」——**改打分函数之前的尺子**。

## 自己不出任何信号

交叉截面的分数**全部**来自既有 `score_pipeline`（现算、只读）。本站是**度量基建**，
不是策略：一旦自己出选股信号，就会与通路 A 的 A1 形成第二套选股口径，A1 的归因立刻失效
（与 `research/xsec.py` 开头那段自述同一个理由）。**不改任何阈值/公式/参数。**

## 只读

连库由调用方走 `stocklab/cli/research.py::open_read_only`（`file:…?mode=ro`）；
本模块**不写任何表、不写任何文件**（落盘交调用方）。产物只落 `reports/research/`。

## 口径不可比（P78 §0.4 的既有遗留，本站**不修**）

`candidate/replay.py::period_returns` 算组合收益用的是 `_close_on` ⇒ 直读 `bars_daily.close`
（全表 `adj_mode='none'` ⇒ **未复权价**），而本报告的 IC 目标收益一律走 **PIT 复权价**
（`data/adjust.py::load_bars_adjusted`，见 D3）⇒ **本报告的 IC 绝对水平与 `xsec-topn`
的组合收益绝对水平不同源、不可直接比**。

## fail-closed 预注册

`--prereg` 指向一份**跑之前就提交**的 md（内含 ```json``` 块），命令行实参必须与它
**逐字段**一致，否则 exit 2、**零输出**。判定门槛（`MIN_VALID_PERIODS` / bootstrap 次数与
种子 / CI 函数 / 过拟合标记）**全部 import 自 `plugin/sandbox.py`**，本模块一个数字都不抄。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Mapping, Sequence

from stocklab.candidate import replay
from stocklab.config import paths
from stocklab.config.universes import UniverseError, resolve_universe
from stocklab.data import adjust
from stocklab.plugin import sandbox
from stocklab.research import xsec

#: 实验短名。与预注册 json 的 `experiment` 字段逐字相同。
EXPERIMENT = "rank-ic"

#: 只跑短期池：`mid` 需 32.6 年、`long` 需 97.9 年才够 120 个验证周期（纯算术，
#: 同 `xsec.ONLY_POOL`）。跑别的池不是「保守」，是白费机时且结论必是样本不足。
ONLY_POOL = "short"

#: 窗口起点下界。**必须显式传**且不得早于它（窗口即结论）。
MIN_START = "2015-01-01"

#: 主读数的相关类型（D5）。预注册 `ic_type` 字段必须等于它。
IC_TYPE = "spearman"

#: 分层数（D6，预注册字段，改要新预注册）。
N_LAYERS = 5

#: 某日截面的**最小有效标的数**（D5）。同时有分数与有效前向收益的标的数低于它 ⇒
#: 该日 IC 记为 `None` 并计入 `n_dates_skipped` —— **不许**用 0 顶替：那是把
#: 「不知道」伪装成「没相关」。
#:
#: 取值理由：分 5 层后每层 n≈6，再小则层均值是少数几只的噪声；30 也是「一天里
#: 有效横截面」的量级下限（短池打分合格的标的数 P50 远高于它，见报告覆盖度）。
MIN_XSEC_N = 30

#: 预注册 json 的**必填字段**。少一个即 exit 2 —— 缺字段的预注册不构成预注册。
#: `universe` 在**清单里**（要能钉住「跑的是哪个宇宙」）但**不在「缺即拒」那一档**，
#: 理由见 `PREREG_OPTIONAL_FIELDS`（与 `xsec` 逐字同规则）。
PREREG_FIELDS: tuple[str, ...] = (
    "experiment", "pool", "start", "universe", "ic_type", "n_layers", "horizon",
    "min_periods", "bootstrap_n", "bootstrap_seed", "rule")

#: 允许**缺席**的预注册字段（缺席有明确语义，不是「没预注册」）。规则与
#: `xsec.PREREG_OPTIONAL_FIELDS` **逐字相同**：缺席 ⇒ 语义视为 `seed21`，
#: 但 `validate_prereg` 仍拿命令行实参去比对 ⇒「预注册推得 seed21、命令行传
#: csi300-500」照样 exit 2（堵「静默换宇宙」）。
PREREG_OPTIONAL_FIELDS: tuple[str, ...] = ("universe",)

#: `universe` 字段缺席时的语义（与 `xsec.DEFAULT_PREREG_UNIVERSE` 同值同规则）。
DEFAULT_PREREG_UNIVERSE = xsec.DEFAULT_PREREG_UNIVERSE

_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.S)

#: 「**这个标的的复权价算不出来**」这一类失败（数据侧不可用 ⇒ 回退未复权 + 计数）。
#: 与 `candidate/run.py::_ADJ_UNAVAILABLE` **同一集合**（三类）。刻意不是
#: `AdjustError` 全体：裸 `AdjustError`（`k > 1` / `pre_close <= 0`，属代码/数据缺陷）
#: **不在此列，原样抛** —— 数据脏与代码坏要分档（ERROR_DIARY #81）。
_ADJ_UNAVAILABLE = (adjust.EtfChainUnsupported, adjust.MissingFactor,
                    adjust.StaleFactorTable)

#: 复权不可用时的回退标签（只进计数与报告，不改数字 —— 因子恒 1 ⇒ 等价未复权价）。
FALLBACK_ETF = "etf_chain_unsupported"
FALLBACK_UNUSABLE_EVENT = "missing_factor"
FALLBACK_STALE_BLACKOUT = "stale_blackout_table"
FALLBACK_EMPTY_CHAIN = "empty_chain"

#: D4 的披露句。
FALLBACK_NOTE = (
    "复权不可用时的处置（D4，fail-open ＋ 可见）：复权层拒绝服务（ETF / 缺因子 / "
    "缺口表过期）或该只复权链为空（无 `corp_actions`）⇒ 该只回退到**未复权价**算前向"
    "收益（因子恒 1 ⇒ 数值等价），计入 `n_fwd_fallback`。这是度量路径不是下单路径，"
    "一只标的的因子不可用不该让整轮死掉；但**必须可见**。裸 `AdjustError`（代码/数据"
    "缺陷）不在此列，原样抛（ERROR_DIARY #81：数据脏与代码坏分档）。"
)

#: §0.4 那条**既有口径缺口**（本站不修，只记录）。写进报告正文。
REPLAY_RAW_PRICE_NOTE = (
    "口径不可比（P78 §0.4，**已知遗留、本站不修**）：`candidate/replay.py::period_returns` "
    "算组合收益用 `_close_on` ⇒ 直读 `bars_daily.close`（全表 `adj_mode='none'`，**未复权价**），"
    "除权日的缺口会被算成组合亏损；本报告的 IC 目标收益走 **PIT 复权价**（D3）"
    "⇒ **本报告的 IC 绝对水平与 `research xsec-topn` 的组合收益不同源、不可直接比**。"
    "改 `replay.py` 会同时动 `xsec-topn` 的臂定义与 `plugin/sandbox.py` 的回放路径"
    "（= 第二个变量），故另立任务书。"
)

#: 窗口即结论（同 xsec）。
WINDOW_IS_CONCLUSION_NOTE = (
    "窗口即结论：`--start` 必填且 ≥ 2015-01-01，预注册跑完不许改；"
    "换窗口/换宇宙/换门槛都不能用来把结果「救」回来（CLAUDE.md 度量纪律 6）。"
)

#: verdict 词汇的说明（D7：度量专用，不许读成 WIN/LOSE）。
VERDICT_VOCAB_NOTE = (
    "verdict 词汇是**度量专用**的：`IC_SIGNIFICANT` / `IC_NOT_SIGNIFICANT` 只说"
    "「IC 的 95% CI 含不含 0」，**IC 为负也叫 significant** —— 叫 `WIN` 会读歪成"
    "「策略赢了」。词表是**预注册的**判据，不是第二套阈值：门槛数字（"
    "`MIN_VALID_PERIODS` / bootstrap 次数与种子）全部 import 自 `plugin/sandbox.py`。"
)

#: 选择偏差的限定句。**本站自己的措辞（符号是 IC，不是 Δ）**：`xsec.py` 那句写的是
#: 「Δ 为正」，照搬会把两个不同度量的口径说明混在一起（ERROR_DIARY #59 同型）。
SELECTION_BIAS_SENTENCE = (
    "选择偏差：IC 显著也只能说「**在这 21 只、这段历史上成立**」，"
    "不能说「打分函数有效」。"
)
SEED_SCOPE_NOTE = "种子只有 21 只"


def _seed_scope_note(universe_id: str, n: int) -> str:
    """§5 的扫描范围陈述。宇宙不是 `seed21` 时按实参写 —— 否则报告会把
    800 只的截面说成「只有 21 只」，与实参直接矛盾。"""
    if universe_id == DEFAULT_PREREG_UNIVERSE:
        return SEED_SCOPE_NOTE
    return f"扫描宇宙 `{universe_id}` 只有 {n} 只"


def _selection_bias_note(universe_id: str, n: int) -> str:
    """选择偏差限定句。`seed21` ⇒ 逐字用 `SELECTION_BIAS_SENTENCE`；
    扩池 ⇒ 把「这 21 只」换成实际宇宙（限定的是**同一个意思**，不是放宽口径）。"""
    if universe_id == DEFAULT_PREREG_UNIVERSE:
        return SELECTION_BIAS_SENTENCE
    return (f"选择偏差：IC 显著也只能说「**在 `{universe_id}` 这 {n} 只、"
            "这段历史上成立**」，不能说「打分函数有效」。")


class PreregError(xsec.PreregError):
    """预注册缺失 / 不合规 / 与命令行实参不一致。

    **复用 `xsec.PreregError` 这个异常类型**（子类 ⇒ `except xsec.PreregError`
    照样捕获），CLI 才不用为本站再加一个 except 分支。
    """


# ---------------------------------------------------------------------------
# 纯函数层（无 DB、只用 stdlib；可单独测）
# ---------------------------------------------------------------------------

def _avg_ranks(values: Sequence[float]) -> list[float]:
    """平均名次（**0 基**）。并列取平均名次。

    与 `candidate/cross_section.py::_pct_of` 的排名法**同一套**：
    `rank = (比它小的个数) + (与它相等的个数 − 1) / 2`。这里用排序实现
    （O(n log n)），数值与那套逐个计数**逐位相同** —— 并列段的平均名次
    `(i + j) / 2` 恰好等于 `less + (equal − 1) / 2`。

    **复制**这套排名法、不 import 那个私有函数：`research/` 与 `candidate/`
    的依赖方向是 candidate → research，借私有名会把它变成公共契约。
    """
    n = len(values)
    order = sorted(range(n), key=lambda i: (values[i], i))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _aligned(scores: Mapping[str, float],
             fwd: Mapping[str, float]) -> tuple[list[str], list[float],
                                                list[float]]:
    """两边的**交集**（按 `code` 升序 ⇒ 求和顺序确定 ⇒ 逐位可复现）。"""
    keys = sorted(set(scores) & set(fwd))
    return (keys, [scores[c] for c in keys], [fwd[c] for c in keys])


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson 相关。n < 2 或任一侧方差为 0（全相等）⇒ `None`（「算不出」≠「不相关」）。"""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    # 夹到 [−1, 1]：末位浮点可能让严格单调的读数落到 1.0000000000000002，
    # 相关系数**在数学上**有界，夹紧不是改口径（测试要求 `== 1.0` 的精确断言）。
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def spearman_ic(scores: Mapping[str, float],
                fwd: Mapping[str, float]) -> float | None:
    """主读数（D5）：分数与**前向收益**的 Spearman 秩相关（并列取平均名次）。

    只取两边都有的 code（交集）；n < 2 或任一侧全相等 ⇒ `None`。
    `< MIN_XSEC_N` 的处置在**调用方**（`run_rank_ic`），不在纯函数里。
    """
    keys, xs, ys = _aligned(scores, fwd)
    if len(keys) < 2:
        return None
    return _pearson(_avg_ranks(xs), _avg_ranks(ys))


def pearson_ic(scores: Mapping[str, float],
               fwd: Mapping[str, float]) -> float | None:
    """次读数（D5）：线性相关（并列报出，不是判据）。"""
    _keys, xs, ys = _aligned(scores, fwd)
    return _pearson(xs, ys)


def assign_layers(codes: Sequence[str], scores: Mapping[str, float],
                  n_layers: int) -> dict[int, list[str]]:
    """按分数**降序**等量分层；**第 1 层 = 最高分**（D6）。

    定序：`(−score, code)` —— 并列分数按 `code` 升序定序 ⇒ **确定性**
    （顺序不是数据里的偶然，是可复现的约定）。
    等量分桶用 `divmod` 语义：`n % k` 个多出来的先给**靠前的层**（第 1 层最宽）
    —— 与 `numpy.array_split` 同形，但不引入 numpy（本项目零 ML 依赖）。

    只对 `scores` 里**真的有分数**的 code 分层；`n_layers < 1` 或没有可分层标的 ⇒
    抛 `ValueError`（静默返回空层会把「没分层」伪装成「层里没标的」）。
    """
    if n_layers < 1:
        raise ValueError(f"n_layers 必须 >= 1，收到 {n_layers!r}")
    usable = [c for c in codes if scores.get(c) is not None]
    if not usable:
        raise ValueError("assign_layers：没有带分数的标的，无法分层")
    ordered = sorted(usable, key=lambda c: (-scores[c], c))
    base, extra = divmod(len(ordered), n_layers)
    out: dict[int, list[str]] = {}
    idx = 0
    for layer in range(1, n_layers + 1):
        size = base + (1 if layer <= extra else 0)
        out[layer] = ordered[idx:idx + size]
        idx += size
    return out


def layer_means(layers: Mapping[int, Sequence[str]],
                fwd: Mapping[str, float]) -> dict[int, float | None]:
    """每层的**当日**平均前向收益。层里没有有效前向收益 ⇒ `None`（不许填 0）。

    「先按日算层均值、再对日平均」的第一步（D6）；跨日聚合在 `run_rank_ic`。
    """
    out: dict[int, float | None] = {}
    for layer in sorted(layers):
        vals = [fwd[c] for c in layers[layer] if c in fwd]
        out[layer] = (sum(vals) / len(vals)) if vals else None
    return out


def ascending_steps(means: Sequence[float | None]) -> int:
    """相邻层之间「上一层均值 > 下一层」的个数（满值 = `n_layers − 1`）。

    传入顺序 = 第 1 层 → 最后一层。任一相邻对里缺读数 ⇒ 那一对**不计**
    （缺读数不是「没上升」）。
    """
    steps = 0
    for a, b in zip(means, means[1:]):
        if a is not None and b is not None and a > b:
            steps += 1
    return steps


# ---------------------------------------------------------------------------
# 预注册
# ---------------------------------------------------------------------------

def load_prereg(path: Path) -> tuple[dict, str]:
    """读出预注册 json 与整份文件的 sha256。

    文件缺失 / 读不出 / 没有 ```json``` 块 / json 非法 / 缺**必填**字段 →
    `PreregError`（fail-closed）。规则与 `xsec.load_prereg` 逐条相同，字段表是本站的。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PreregError(f"预注册文件读不到：{path}（{exc}）") from exc
    text = raw.decode("utf-8")
    m = _JSON_FENCE.search(text)
    if m is None:
        raise PreregError(
            f"预注册文件里没有 ```json 块：{path} —— 没有它就无从校验"
            "窗口/池/层数/判据是否被改过")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise PreregError(f"预注册 json 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise PreregError("预注册 json 不是对象")
    missing = [f for f in PREREG_FIELDS
               if f not in data and f not in PREREG_OPTIONAL_FIELDS]
    if missing:
        raise PreregError(f"预注册 json 缺字段：{missing}")
    return data, hashlib.sha256(raw).hexdigest()


def validate_prereg(data: Mapping, *, pool: str, start: str, horizon: int,
                    universe: str | None = None) -> None:
    """命令行实参 ＋ 代码常量 vs 预注册：**任一不一致即拒跑**（fail-closed）。

    `universe`：预注册**没写**该字段 ⇒ 按 `DEFAULT_PREREG_UNIVERSE`（`seed21`）
    解释，再与命令行实参比对 —— 「老预注册（seed21 语义）+ 命令行
    `--universe csi300-500`」**拒跑**（exit 2），「静默换宇宙」被堵死。

    `horizon`：由调用方从 `config/replay.py::REBALANCE_DAYS` **读出**后传入
    （不许手抄）；这里钉住「预注册写的持有期就是代码要跑的持有期」。
    判定门槛（`min_periods` / `bootstrap_n` / `bootstrap_seed`）与
    `plugin/sandbox.py` 的常量比对 —— 否则预注册可以把 `min_periods` 写成 30
    而代码仍按 120 判，报告读起来却是「已预注册」。
    """
    def _mismatch(field: str, got, want) -> PreregError:
        return PreregError(
            f"预注册不一致：{field} 预注册={want!r} 命令行/代码={got!r}"
            f" —— exit 2，零输出、不跑度量")

    if data["experiment"] != EXPERIMENT:
        raise _mismatch("experiment", EXPERIMENT, data["experiment"])
    if data["pool"] != pool:
        raise _mismatch("pool", pool, data["pool"])
    if data["start"] != start:
        raise _mismatch("start", start, data["start"])
    want_universe = data.get("universe", DEFAULT_PREREG_UNIVERSE)
    got_universe = universe or DEFAULT_PREREG_UNIVERSE
    if want_universe != got_universe:
        raise PreregError(
            f"预注册不一致：universe 预注册={want_universe!r} 命令行={got_universe!r}"
            f"（预注册没写该字段时语义为 {DEFAULT_PREREG_UNIVERSE!r}）"
            f" —— exit 2，零输出、不跑度量：**换宇宙必须新预注册**，"
            "否则同一份预注册能跑出两个结论（静默换宇宙）")
    if data["ic_type"] != IC_TYPE:
        raise _mismatch("ic_type", IC_TYPE, data["ic_type"])
    if data["n_layers"] != N_LAYERS:
        raise _mismatch("n_layers", N_LAYERS, data["n_layers"])
    if data["horizon"] != horizon:
        raise _mismatch("horizon", horizon, data["horizon"])
    if data["min_periods"] != sandbox.MIN_VALID_PERIODS:
        raise _mismatch("min_periods", sandbox.MIN_VALID_PERIODS,
                        data["min_periods"])
    if data["bootstrap_n"] != sandbox._BOOTSTRAP_N:
        raise _mismatch("bootstrap_n", sandbox._BOOTSTRAP_N, data["bootstrap_n"])
    if data["bootstrap_seed"] != sandbox._BOOTSTRAP_SEED:
        raise _mismatch("bootstrap_seed", sandbox._BOOTSTRAP_SEED,
                        data["bootstrap_seed"])


# ---------------------------------------------------------------------------
# 前向收益（D3/D4）
# ---------------------------------------------------------------------------

def _fallback_reason(exc: adjust.AdjustError) -> str:
    if isinstance(exc, adjust.EtfChainUnsupported):
        return FALLBACK_ETF
    if isinstance(exc, adjust.MissingFactor):
        return FALLBACK_UNUSABLE_EVENT
    return FALLBACK_STALE_BLACKOUT


class _ForwardPrices:
    """`code → {调仓日: 收盘价}`，**一次/只**（D3）惰性载入并缓存。

    走 `data/adjust.py::load_bars_adjusted(conn, code, as_of=FWD_ASOF)`
    （**不自己实现一份复权、也不改 `adjust.py`**），只保留落在 `marks` 里的日期。
    比值 `adj(d1)/adj(d0)` 只取决于 `(d0, d1]` 区间内的除权事件 ⇒ 取任意
    `as_of ≥ d1` 的前向收益逐位相同，且 `d0/d1 ≤ as_of` ⇒ **无未来数据**（D3）。

    复权层拒绝服务 / 复权链为空 ⇒ 该只回退到**未复权价**（D4），`n_fallback` 计数
    **每只至多一次**（缓存在前，计数在后）。裸 `AdjustError` 原样抛。
    """

    def __init__(self, conn: sqlite3.Connection, *, as_of: str,
                 dates: set[str]) -> None:
        self._conn = conn
        self._as_of = as_of
        self._dates = dates
        self._cache: dict[str, dict[str, float]] = {}
        self.n_fallback = 0
        #: 真正花在「读复权序列」上的秒数（**惰性载入发生在调用点**，所以必须在这里
        #: 累加 —— 光量构造函数会永远读到 0.0，那是个看着像读数的假数）。
        self.load_s = 0.0

    def _raw_closes(self, code: str) -> dict[str, float]:
        """未复权收盘价（`bars_daily.close`，与 `candidate/run.py::_load_bars` 同源）。"""
        rows = self._conn.execute(
            "SELECT date, close FROM bars_daily WHERE code = ? AND date <= ?",
            (code, self._as_of)).fetchall()
        return {r[0]: r[1] for r in rows if r[0] in self._dates}

    def closes(self, code: str) -> dict[str, float]:
        if code in self._cache:
            return self._cache[code]
        t0 = time.time()
        try:
            bars = adjust.load_bars_adjusted(self._conn, code, self._as_of)
        except _ADJ_UNAVAILABLE as exc:
            closes = self._raw_closes(code)
            fallback: str | None = _fallback_reason(exc)
        else:
            if self._conn.execute(
                    "SELECT 1 FROM corp_actions WHERE code = ? LIMIT 1",
                    (code,)).fetchone() is None:
                # 无事件 ⇒ 链上因子恒 1 ⇒ 复权价逐位等于未复权价（同 run.py）。
                closes = self._raw_closes(code)
                fallback = FALLBACK_EMPTY_CHAIN
            else:
                closes = {b.date: b.close for b in bars if b.date in self._dates}
                fallback = None
        self.load_s += time.time() - t0
        if fallback is not None:
            self.n_fallback += 1
        self._cache[code] = closes
        return closes

    def fwd_return(self, code: str, d0: str, d1: str) -> float | None:
        """`adj_close(c, d1) / adj_close(c, d0) − 1`；任一端缺价（或 0）⇒ `None`。"""
        closes = self.closes(code)
        c0 = closes.get(d0)
        c1 = closes.get(d1)
        if not c0 or c1 is None:
            return None
        return c1 / c0 - 1.0


# ---------------------------------------------------------------------------
# 判定（D7：门槛全部 import，不新定）
# ---------------------------------------------------------------------------

def _ic_stats(series: list[float]) -> dict:
    """IC 序列的训练/验证切分与汇总读数（切分复用 `replay.split_train_validate`）。"""
    train, validate = replay.split_train_validate(list(series))

    def _stats(seg: list[float]) -> dict:
        n = len(seg)
        mean = (sum(seg) / n) if n else None
        std = statistics.stdev(seg) if n >= 2 else None
        ir = (mean / std) if (mean is not None and std) else None
        return {"n": n, "mean": mean, "std": std, "ir": ir,
                "t": (ir * math.sqrt(n)) if ir is not None else None}

    t_all = _stats(series)
    t_train = _stats(train)
    t_val = _stats(validate)
    return {
        "series": list(series),
        "n_dates": len(series),
        "mean_all": t_all["mean"],
        "std_all": t_all["std"],
        "train": t_train,
        "validate": t_val,
        "mean_train": t_train["mean"],
        "overfit_flag": sandbox.overfit_flag(t_train["mean"], t_val["mean"]),
        **_verdict(validate),
    }


def _verdict(validate: Sequence[float]) -> dict:
    """判定（D7，**预注册**的判据，不是第二套阈值）。

    验证段有效日期数 < `sandbox.MIN_VALID_PERIODS` ⇒ `INCONCLUSIVE`（「测不出」，
    不是「没效果」）；否则 IC 序列的 `sandbox._BOOTSTRAP_N` 次按日期重采样 95% CI
    **不含 0** ⇒ `IC_SIGNIFICANT`（**IC 为负也叫 significant** —— 词汇是度量专用的，
    叫 `WIN` 会读歪），含 0 ⇒ `IC_NOT_SIGNIFICANT`。
    """
    n = len(validate)
    mean = (sum(validate) / n) if n else None
    if n < sandbox.MIN_VALID_PERIODS:
        # 样本不足时**不计算 CI**（那正是「不构成结论」），但**不把实测点估计抹成 None**：
        # 抹掉会让报告里那一格与「一个数都没有」不可区分（读者会以为没数据）。
        return {"verdict": "INCONCLUSIVE", "n_validate": n,
                "mean_validate": mean, "ci_low": None, "ci_high": None,
                "note": f"样本不足（{n} 个有效日期 < "
                        f"{sandbox.MIN_VALID_PERIODS}），不构成结论 —— "
                        "「测不出」不是「没效果」；点估计是实测值但**不给 CI**"}
    lo, hi = sandbox._bootstrap_ci(list(validate))
    significant = lo > 0.0 or hi < 0.0
    return {"verdict": "IC_SIGNIFICANT" if significant else "IC_NOT_SIGNIFICANT",
            "n_validate": n, "mean_validate": mean, "ci_low": lo, "ci_high": hi,
            "note": f"验证段 IC 均值 {mean:+.4f}，95% CI [{lo:+.4f}, {hi:+.4f}]，"
                    f"有效日期 n={n} —— "
                    + ("CI 不含 0 ⇒ 有可测的横截面排序能力（方向看符号）"
                       if significant else "CI 跨 0 ⇒ 无可测的横截面排序能力")}

# ---------------------------------------------------------------------------
# 实验主体
# ---------------------------------------------------------------------------

def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def run_rank_ic(conn: sqlite3.Connection, *, pool: str, start: str, end: str,
                prereg_path: Path, universe: str | None = None) -> dict:
    """跑度量，返回报告 dict（**不写任何文件**，落盘交给调用方）。

    `universe`：宇宙 id（`None` ⇒ `seed21` 主干常量）。非 `seed21` 走
    `resolve_universe`（**fail-closed**：文件缺失即抛，不回退）。
    失败一律 `PreregError`（调用方 exit 2、零输出）。
    """
    if pool != ONLY_POOL:
        raise PreregError(
            f"本实验只跑 {ONLY_POOL!r}（mid 需 32.6 年、long 需 97.9 年才够 "
            f"120 个验证周期）；收到 {pool!r}")
    if start < MIN_START:
        raise PreregError(
            f"窗口起点不得早于 {MIN_START}（收到 {start!r}）—— 窗口即结论")
    horizon = replay.REBALANCE_DAYS[pool]          # 读，不手抄

    try:
        universe_id, members, members_sha256 = resolve_universe(universe)
    except UniverseError as exc:
        raise PreregError(f"宇宙载入失败（{universe!r}）：{exc}") from exc

    prereg, prereg_sha = load_prereg(prereg_path)
    validate_prereg(prereg, pool=pool, start=start, horizon=horizon,
                    universe=universe_id)

    marks = replay.rebalance_marks(conn, pool=pool, start=start, end=end)
    if len(marks) < 2:
        raise PreregError(
            f"窗口内调仓边界只有 {len(marks)} 个（需 ≥ 2 才能构成周期）："
            f"{start}~{end}")

    from stocklab.candidate.run import score_pipeline

    fwd_asof = marks[-1]        # D3：最后一个**调仓日**（不是 end，end 可能非交易日）
    t0 = time.time()
    scan_s = 0.0
    #: 每个调仓日跑一次（D2）；取该池**全部已打分**标的（不是只有 top-N）。
    scored_by_mark: dict[str, list[dict]] = {}
    for day in marks:
        ts = time.time()
        res = score_pipeline(conn, asof=day, universe=members)
        scan_s += time.time() - ts
        scored_by_mark[day] = list(res.scored.get(pool, []))

    tf = time.time()
    provider = _ForwardPrices(conn, as_of=fwd_asof, dates=set(marks))
    fwd_s = time.time() - tf

    ic_adj: list[float | None] = []
    ic_raw: list[float | None] = []
    sizes: list[int] = []
    per_day_layer_means: dict[int, list[float]] = {k: [] for k in range(1, N_LAYERS + 1)}
    layer_sizes: dict[int, list[int]] = {k: [] for k in range(1, N_LAYERS + 1)}
    spread_series: list[float] = []
    steps_per_day: list[int] = []
    n_dates_skipped = 0
    n_no_fwd_ret = 0

    for d0, d1 in zip(marks, marks[1:]):
        rows = scored_by_mark[d0]
        scores_adj = {r["code"]: r["adj_score"] for r in rows
                      if r["adj_score"] is not None}
        scores_raw = {r["code"]: r["raw_score"] for r in rows
                      if r["raw_score"] is not None}
        fwd: dict[str, float] = {}
        for code in sorted(set(scores_adj) | set(scores_raw)):
            r = provider.fwd_return(code, d0, d1)
            if r is None:
                n_no_fwd_ret += 1        # 该 (日期, code) 从该日截面剔除（D4）
            else:
                fwd[code] = r

        usable = sorted(set(scores_adj) & set(fwd))
        sizes.append(len(usable))
        if len(usable) < MIN_XSEC_N:
            # 截面太薄 ⇒ 该日**不出任何读数**（IC 与分层都记不上）并计数 ——
            # **不许**用 0 顶替（D5）：那是把「不知道」伪装成「没相关」。
            # 门槛对 IC/分层/spread 是**同一个**：否则「n=5 的一天」进得了层均值
            # 却进不了 IC 序列，两个读数对「哪些天算数」各说各话。
            ic_adj.append(None)
            ic_raw.append(None)
            n_dates_skipped += 1
            continue

        ic_adj.append(spearman_ic(scores_adj, fwd))
        ic_raw.append(spearman_ic(scores_raw, fwd)
                      if len(set(scores_raw) & set(fwd)) >= MIN_XSEC_N else None)

        layers = assign_layers(usable, scores_adj, N_LAYERS)
        means = layer_means(layers, fwd)
        for k in range(1, N_LAYERS + 1):
            layer_sizes[k].append(len(layers[k]))
            if means[k] is not None:
                per_day_layer_means[k].append(means[k])
        steps_per_day.append(ascending_steps([means[k] for k in range(1, N_LAYERS + 1)]))
        if means[1] is not None and means[N_LAYERS] is not None:
            spread_series.append(means[1] - means[N_LAYERS])

    def _series(xs: list[float | None]) -> list[float]:
        return [x for x in xs if x is not None]

    adj_stats = _ic_stats(_series(ic_adj))
    raw_stats = _ic_stats(_series(ic_raw))

    layer_mean = {k: (sum(v) / len(v)) if v else None
                  for k, v in sorted(per_day_layer_means.items())}
    ordered_means = [layer_mean[k] for k in range(1, N_LAYERS + 1)]
    if spread_series:
        spread = {"n_days": len(spread_series), "series": list(spread_series),
                  "mean": sum(spread_series) / len(spread_series)}
        lo, hi = sandbox._bootstrap_ci(list(spread_series))
        spread.update({"ci_low": lo, "ci_high": hi})
    else:
        spread = {"n_days": 0, "series": [], "mean": None,
                  "ci_low": None, "ci_high": None}

    fwd_s += provider.load_s      # 惰性载入都发生在上面那个循环里（见 load_s 的注释）
    elapsed = time.time() - t0
    return {
        "experiment": EXPERIMENT,
        "pool": pool,
        "start": start,
        "end": end,
        "universe": universe_id,
        "universe_id": universe_id,
        "universe_n": len(members),
        "universe_members_sha256": members_sha256,
        "prereg_universe": prereg.get("universe", DEFAULT_PREREG_UNIVERSE),
        "ic_type": IC_TYPE,
        "n_layers": N_LAYERS,
        "horizon": horizon,
        "min_periods": sandbox.MIN_VALID_PERIODS,
        "bootstrap_n": sandbox._BOOTSTRAP_N,
        "bootstrap_seed": sandbox._BOOTSTRAP_SEED,
        "min_xsec_n": MIN_XSEC_N,
        "fwd_asof": fwd_asof,
        "rule": prereg["rule"],
        "prereg_path": str(prereg_path),
        "prereg_sha256": prereg_sha,
        "n_marks": len(marks),
        "n_periods": len(marks) - 1,
        "elapsed_s": elapsed,
        "scan_s": scan_s,
        "fwd_load_s": fwd_s,
        "ic": {
            "adj_score": {"label": "主读数：adj_score（select_top 实际排序用的那个）",
                          **adj_stats},
            "raw_score": {"label": "次读数：raw_score（插桩1 的原始输出）",
                          **raw_stats},
        },
        "pearson_ic": {
            "adj_score": _pearson_series(scored_by_mark, provider, marks, "adj_score"),
            "raw_score": _pearson_series(scored_by_mark, provider, marks, "raw_score"),
        },
        "layers": {
            "n_layers": N_LAYERS,
            "mean_by_layer": {str(k): layer_mean[k] for k in range(1, N_LAYERS + 1)},
            "size_p50_by_layer": {
                str(k): _median(layer_sizes[k]) for k in range(1, N_LAYERS + 1)},
            "n_ascending_steps": ascending_steps(ordered_means),
            "n_ascending_steps_per_day_mean": (
                sum(steps_per_day) / len(steps_per_day)) if steps_per_day else None,
            "spread": spread,
        },
        "coverage": {
            "n_periods": len(marks) - 1,
            "n_dates_with_ic": len(_series(ic_adj)),
            "n_dates_skipped": n_dates_skipped,
            "xsec_size_p50": _median(sizes),
            "xsec_size_min": min(sizes) if sizes else None,
            "xsec_size_max": max(sizes) if sizes else None,
            "n_fwd_fallback": provider.n_fallback,
            "n_no_fwd_ret": n_no_fwd_ret,
        },
        "non_pit_items": list(xsec.NON_PIT_ITEMS),
        "non_pit_universe_items": list(xsec.NON_PIT_UNIVERSE_ITEMS),
        "universe_note": (
            f"宇宙：`{universe_id}`（{len(members)} 只，`members_sha256` "
            f"`{members_sha256[:12]}…`）。**本 IC 的横截面来自 `{universe_id}`，与 "
            f"`seed21` 版（21 只）的 IC 不可直接比**：宇宙不同、候选池不同、"
            "「池内全部合格」的定义域也不同。"),
        "selection_bias_note": _selection_bias_note(universe_id, len(members)),
        "seed_scope_note": _seed_scope_note(universe_id, len(members)),
        "window_note": WINDOW_IS_CONCLUSION_NOTE,
        "verdict_vocab_note": VERDICT_VOCAB_NOTE,
        "fallback_note": FALLBACK_NOTE,
        "replay_raw_price_note": REPLAY_RAW_PRICE_NOTE,
    }


def _pearson_series(scored_by_mark: Mapping[str, list[dict]],
                    provider: _ForwardPrices, marks: Sequence[str],
                    field: str) -> list[float | None]:
    """Pearson 次读数序列（与主读数**同一批**截面与门槛）。

    单独走一遍是因为两个相关系数的**分母**不同（秩 vs 原值），不是两套口径。
    """
    out: list[float | None] = []
    for d0, d1 in zip(marks, marks[1:]):
        scores = {r["code"]: r[field] for r in scored_by_mark[d0]
                  if r[field] is not None}
        fwd = {}
        for code in sorted(scores):
            r = provider.fwd_return(code, d0, d1)
            if r is not None:
                fwd[code] = r
        if len(set(scores) & set(fwd)) < MIN_XSEC_N:
            out.append(None)
        else:
            out.append(pearson_ic(scores, fwd))
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _num(x: float | None, fmt: str = "{:+.4f}") -> str:
    return "—" if x is None else fmt.format(x)


def _no_data(report: Mapping) -> bool:
    """一条有效日期都没有 —— 报告里凡是有「读数」的地方一律写 `—`，**不许写 0**
    （0 会被读成「测出来是 0」，那是把「不知道」伪装成读数）。"""
    return report["coverage"]["n_dates_with_ic"] == 0


def summary_line(report: Mapping) -> str:
    """md 结尾那一行 `summary:`（nanobot 直接贴给用户）。"""
    adj = report["ic"]["adj_score"]
    cov = report["coverage"]
    return (f"summary: rank-ic pool={report['pool']} "
            f"{report['start']}~{report['end']} universe={report['universe_id']} "
            f"n_dates={adj['n_dates']} 验证段n={adj['n_validate']} "
            f"meanIC={_num(adj['mean_validate'])} "
            f"CI[{_num(adj['ci_low'])},{_num(adj['ci_high'])}] "
            f"verdict={adj['verdict']} "
            f"(截面P50={cov['xsec_size_p50']} 跳过日={cov['n_dates_skipped']} "
            f"回退={cov['n_fwd_fallback']} 缺价={cov['n_no_fwd_ret']})")


def render_md(report: Mapping) -> str:
    """人读报告。披露项（非 PIT、回退、口径不可比）**并列写出**。"""
    cov = report["coverage"]
    layers = report["layers"]
    adj = report["ic"]["adj_score"]
    raw = report["ic"]["raw_score"]
    lines: list[str] = [
        f"# rank-ic 报告（{report['pool']} 池，{report['start']} ~ "
        f"{report['end']}，宇宙 `{report['universe_id']}`）",
        "",
        "> 本报告由 `research rank-ic` 只读生成：**未写任何表**，产物只在 "
        "`reports/`。交叉截面的分数**全部**来自 `score_pipeline`（现算、只读），"
        "本站**不出任何信号、不改任何阈值/公式/参数** —— 它是度量基建。",
        "",
        "## 0. 预注册",
        "",
        f"- 预注册文件：`{report['prereg_path']}`",
        f"- `prereg_sha256` = `{report['prereg_sha256']}`",
        f"- 判据原文：{report['rule']}",
        f"- {report['window_note']}",
        f"- {report['verdict_vocab_note']}",
        "",
        "## 1. 主/次读数（逐调仓日 rank IC）",
        "",
        "| 读数 | 有效日期 n | 全窗均值 | 训练段均值 | 验证段均值 | "
        "验证段 std | 验证段 IR | 验证段 t | 过拟合标记 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, s in (("adj_score", adj), ("raw_score", raw)):
        v = s["validate"]
        lines.append(
            f"| {s['label']} | {s['n_dates']} | {_num(s['mean_all'], '{:+.5f}')} | "
            f"{_num(s['train']['mean'], '{:+.5f}')} | "
            f"{_num(v['mean'], '{:+.5f}')} | {_num(v['std'], '{:.5f}')} | "
            f"{_num(v['ir'], '{:+.3f}')} | {_num(v['t'], '{:+.3f}')} | "
            f"`{s['overfit_flag']}` |")
    lines += [
        "",
        f"- Pearson 次读数（并列报出、不作判据）：验证段均值 adj "
        f"{_num(_tail_mean(report['pearson_ic']['adj_score']))}、raw "
        f"{_num(_tail_mean(report['pearson_ic']['raw_score']))}",
        f"- 训练/验证切分复用 `candidate/replay.py::split_train_validate`"
        f"（周期序号 70/30，`SPLIT_TRAIN_RATIO`）",
        "",
        "## 2. 分层（分数降序、等量、并列按 code 定序；第 1 层 = 最高分）",
        "",
        "| 层 | 该层标的数（日 P50） | 平均前向收益（先按日算层均值、再对日平均） |",
        "|---|---|---|",
    ]
    for k in range(1, report["n_layers"] + 1):
        size = layers["size_p50_by_layer"][str(k)]
        lines.append(f"| {k} | {'—' if size is None else size} | "
                     f"{_num(layers['mean_by_layer'][str(k)], '{:+.5f}')} |")
    sp = layers["spread"]
    nothing = _no_data(report)
    lines += [
        "",
        f"- `spread = layer1 − layer{report['n_layers']}`：n={sp['n_days']} 天，"
        f"均值 {_num(sp['mean'], '{:+.5f}')}，95% CI "
        f"[{_num(sp['ci_low'])}, {_num(sp['ci_high'])}]",
        "- 单调性 `n_ascending_steps` = "
        + ("**—**（无有效日期，不是「测出 0 步」）"
           if nothing else f"**{layers['n_ascending_steps']}**")
        + f" / {report['n_layers'] - 1}（相邻层「上一层 > 下一层」的个数）；"
        f"按日平均 {_num(layers['n_ascending_steps_per_day_mean'], '{:.2f}')}",
        "",
        "## 3. 判定（预注册的判据）",
        "",
        f"- verdict = **{adj['verdict']}**；{adj['note']}",
        f"- 门槛：`MIN_VALID_PERIODS`={report['min_periods']}、bootstrap "
        f"n={report['bootstrap_n']} seed={report['bootstrap_seed']}"
        f"（**import 自 `plugin/sandbox.py`，未另定**）、"
        f"`MIN_XSEC_N`={report['min_xsec_n']}",
        "",
        "## 4. 覆盖度（可见性读数）",
        "",
        f"- 调仓边界 {report['n_marks']} 个（短池 {report['horizon']} 日一调）、"
        f"周期 {cov['n_periods']} 个；有 IC 的日期 {cov['n_dates_with_ic']} 个、"
        f"跳过 {cov['n_dates_skipped']} 个（截面 < `MIN_XSEC_N`）",
        f"- 每日截面有效标的数：P50 = {cov['xsec_size_p50']}、"
        f"min = {cov['xsec_size_min']}、max = {cov['xsec_size_max']}",
        f"- 前向收益口径：`load_bars_adjusted(as_of={report['fwd_asof']})`（D3，"
        f"一次/只）；回退未复权价 {cov['n_fwd_fallback']} 只、"
        f"缺价剔除 {(cov['n_no_fwd_ret'])} 个 (日期, code)",
        "",
        f"- {report['fallback_note']}",
        "",
        "## 5. 必须并列披露的口径",
        "",
    ]
    lines += [f"- {item}" for item in report["non_pit_items"]]
    lines += [f"- {item}" for item in report.get("non_pit_universe_items", ())]
    lines += ["", f"- {report['selection_bias_note']}", "",
              f"> {report.get('seed_scope_note') or ''}", "",
              f"- {report['universe_note']}", "",
              f"- {report['replay_raw_price_note']}", "",
              "## 6. 复现与耗时", "",
              "```bash",
              ".venv/bin/python -m stocklab.cli.main research rank-ic \\",
              f"    --pool {report['pool']} --start {report['start']} \\",
              f"    --universe {report['universe_id']} \\",
              f"    --prereg {report['prereg_path']} --out reports/research/",
              "```",
              "",
              f"- 总耗时 {report['elapsed_s']:.1f} s（逐调仓日 `score_pipeline` "
              f"{report['scan_s']:.1f} s ＋ 前向收益载入 {report['fwd_load_s']:.1f} s）",
              "",
              summary_line(report),
              ""]
    return "\n".join(lines)


def _tail_mean(series: Sequence[float | None]) -> float | None:
    """Pearson 序列的验证段均值（与主读数同一套 70/30 切分）。"""
    vals = [x for x in series if x is not None]
    _train, validate = replay.split_train_validate(vals)
    return (sum(validate) / len(validate)) if validate else None


def write_report(report: Mapping, out_dir: Path) -> tuple[Path, Path]:
    """落 `<out>/<end>-rank-ic-<universe_id>.{json,md}`，返回两个路径。

    **文件名必须带宇宙 id**：不带时「换宇宙重跑同一个 end」会**覆盖**上一份产物
    （P77 T7 的教训 —— 2026-09-25 的扩池重跑就是这样把 P60 的产物覆盖掉的）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report['end']}-rank-ic-{report['universe_id']}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")
    return json_path, md_path


def default_out_dir() -> Path:
    """默认产物目录 = `reports/research/`（读 `paths.REPORT_DIR`，调用时取）。"""
    return paths.REPORT_DIR / "research"
