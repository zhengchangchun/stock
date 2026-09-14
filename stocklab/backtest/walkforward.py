"""Walk-forward 切分（R7 自由度的可执行形式）+ 样本量纪律（R6 的可执行形式）。

**这个模块存在的意义**：让「样本外」从一句口号变成可执行的约束。
纯函数、不碰数据库、不读时间 —— 同一输入必然得到同一折分（可复现）。

不变量（每一条都配了反证测试）：

1. **绝不 shuffle**：会话轴必须严格升序。乱序/重复输入**报错**，
   而不是被内部 `sorted()` "修好" —— 偷偷排序会掩盖上游的日期错位。
2. **test 严格晚于 train**：禁止全样本调参。
3. **不同折的 test 不重叠**：同一段数据只能当一次测试集。
4. **purge/embargo**：训练窗末尾的 `embargo` 个交易日被剔除，
   降低「标签跨越切分点」带来的泄漏。被剔除的日期单独记录（`purged_dates`），
   可审计、不静默消失。
5. **数据不足显式报错**：会话数 < `train + test` 时抛 `InsufficientData`。
   静默返回空表会让调用方把「0 折」读成「跑完了、没有结论」。
6. **样本量单位是交易日**：`sample_size_report` 同时给出「交易日数」与
   「标的-日行数」，并明确 `effective_n = 交易日数`。
   按日聚类的理由：A 股同涨同跌，20 个标的同一天 ≠ 20 个独立样本。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Iterable, Mapping, Sequence

#: R6 硬门槛：样本外不足 120 个交易日的结论一律「样本不足，仅供观察」。
DEFAULT_MIN_OOS_DAYS = 120

#: A 股一年约 243 个交易日（用于把「还差多少交易日」换算成「还要多长历史」）。
SESSIONS_PER_YEAR = 243


class InsufficientData(ValueError):
    """数据不足以切出**至少一折** —— 显式失败，绝不静默少跑。

    属性带上算术依据（现有多少、最少要多少、差多少），
    让调用方能直接把它打印成人看得懂的结论。
    """

    def __init__(self, n_sessions: int, n_required: int, *, train: int, test: int):
        self.n_sessions = n_sessions
        self.n_required = n_required
        self.train = train
        self.test = test
        self.deficit = max(0, n_required - n_sessions)
        super().__init__(
            f"会话数不足，无法切出任何一折：现有 {n_sessions} 个交易日，"
            f"至少需要 train({train}) + test({test}) = {n_required} 个，"
            f"还差 {self.deficit} 个。"
            f"（本模块不会静默返回空表——0 折不是「没有结论」，是「没跑」）"
        )


@dataclass(frozen=True)
class Fold:
    """一折：训练窗 + 测试窗（都是**交易日**序列，严格升序）。

    端点（`train_start` / `train_end` / `test_start` / `test_end`）是
    `train_dates` / `test_dates` 的**派生属性**，不是独立入参 ——
    这样就不存在「端点与日期元组不一致」的 Fold（类型层面消除）。
    """

    index: int
    train_dates: tuple[str, ...]
    test_dates: tuple[str, ...]
    #: 因 embargo 被剔除的交易日（位于 train 末尾，紧邻 test 起点之前）。
    purged_dates: tuple[str, ...] = ()

    @property
    def train_start(self) -> str:
        return self.train_dates[0]

    @property
    def train_end(self) -> str:
        return self.train_dates[-1]

    @property
    def test_start(self) -> str:
        return self.test_dates[0]

    @property
    def test_end(self) -> str:
        return self.test_dates[-1]

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "n_train": len(self.train_dates),
            "n_test": len(self.test_dates),
            "n_purged": len(self.purged_dates),
        }


def _validate_axis(sessions: Sequence[str]) -> list[str]:
    """会话轴必须是**严格升序**且无重复 —— 这是「绝不 shuffle」的入口守卫。"""
    dates = list(sessions)
    for prev, cur in zip(dates, dates[1:]):
        if cur == prev:
            raise ValueError(
                f"会话轴存在重复日期 {cur!r}：重复会让同一个交易日被当成两个样本"
            )
        if cur < prev:
            raise ValueError(
                f"会话轴必须严格升序（绝不 shuffle）：{prev!r} 之后出现了 {cur!r}。"
                f"本模块不会替你排序——乱序意味着上游日期已经错位，排序只会掩盖它。"
            )
    return dates


def split_walk_forward(sessions: Sequence[str], *, train: int, test: int,
                       step: int | None = None, embargo: int = 0) -> list[Fold]:
    """把会话轴切成若干「训练窗 → 测试窗」折。

    - `train` / `test`：窗口长度（**交易日个数**）
    - `step`：相邻折训练起点的步进，默认 = `test`（测试窗首尾相接、不留缝）
    - `embargo`：训练窗末尾剔除的交易日数（purge），必须落在 `[0, train)`

    折数 = `floor((N - train - test) / step) + 1`（要求 `N >= train + test`）。
    """
    if train <= 0 or test <= 0:
        raise ValueError("train 与 test 必须为正整数")
    if step is not None and step <= 0:
        raise ValueError("step 必须为正整数")
    if embargo < 0 or embargo >= train:
        raise ValueError(f"embargo 必须在 [0, train) 内，收到 {embargo}")

    dates = _validate_axis(sessions)
    step = test if step is None else step

    n_required = train + test
    if len(dates) < n_required:
        raise InsufficientData(len(dates), n_required, train=train, test=test)

    folds: list[Fold] = []
    start = 0
    idx = 0
    while start + n_required <= len(dates):
        train_slice = dates[start:start + train]
        purged: tuple[str, ...] = ()
        if embargo:
            purged = tuple(train_slice[len(train_slice) - embargo:])
            train_slice = train_slice[:-embargo]
        test_slice = dates[start + train:start + train + test]
        folds.append(Fold(idx, tuple(train_slice), tuple(test_slice), purged))
        idx += 1
        start += step
    return folds


def unused_tail(folds: Sequence[Fold], sessions: Sequence[str]) -> tuple[str, ...]:
    """轴末尾**没有被任何折用到**的交易日。

    含在报告里的原因：walk-forward 天然用不满尾部（凑不出一个完整 test 窗），
    这属于「少跑了几天」——必须**报出来**而不是静默截断。
    尾部剩余量随参数变化（`test` 越大，浪费越多），读者据此判断窗口是否合理。
    """
    if not folds:
        return tuple(sessions)
    last = folds[-1].test_end
    return tuple(d for d in sessions if d > last)


# ---------- 样本量：交易日 ≠ 行数 ----------

def sample_size_report(folds: Sequence[Fold], *,
                       dates_by_code: Mapping[str, Iterable[str]]) -> dict:
    """统计样本外样本量。**主口径是交易日**，行数只作为参考量一并给出。

    `dates_by_code`：每个标的**有行情的日期**（可长于样本外窗口，本函数只取交集）。
    """
    sum_days = sum(len(f.test_dates) for f in folds)
    unique_days = {d for f in folds for d in f.test_dates}
    rows_by_code: dict[str, int] = {}
    for code in sorted(dates_by_code):
        in_oos = set(dates_by_code[code]) & unique_days
        rows_by_code[code] = len(in_oos)
    oos_rows = sum(rows_by_code.values())
    effective_n = len(unique_days)
    return {
        "unit": "trading_day",
        "effective_n": effective_n,
        "oos_trading_days": sum_days,
        "oos_unique_days": effective_n,
        "overlap_detected": sum_days != effective_n,
        "oos_rows": oos_rows,
        "oos_rows_by_code": rows_by_code,
        "n_codes": len(rows_by_code),
        "rows_per_day": (oos_rows / effective_n) if effective_n else 0.0,
        "note": (
            "有效样本量按**交易日**计（A 股同涨同跌，20 标的同一天 ≠ 20 个独立样本）；"
            "oos_rows 是「标的-日行数」，仅供核对数据覆盖，**不得**当作样本量上报。"
        ),
    }


def threshold_arithmetic(*, train: int, test: int, step: int | None = None,
                         n_sessions: int, oos_trading_days: int,
                         threshold: int = DEFAULT_MIN_OOS_DAYS,
                         sessions_per_year: int = SESSIONS_PER_YEAR) -> dict:
    """判断样本外交易日是否达到硬门槛，并把缺口换算成「需要多长历史」。

    关键算术（`step` 默认 = `test` 时）：
        oos ≈ (折数) × test，其中 折数 = floor((N - train - test) / step) + 1
        ⇒ 要 oos ≥ threshold，需要 N ≥ train + test + (ceil(threshold/test) - 1) × step
    """
    step = test if step is None else step
    min_folds = ceil(threshold / test)
    min_sessions = train + test + (min_folds - 1) * step
    extra = max(0, min_sessions - n_sessions)
    return {
        "threshold": threshold,
        "threshold_unit": "trading_day",
        "oos_trading_days": oos_trading_days,
        "meets": oos_trading_days >= threshold,
        "deficit_days": max(0, threshold - oos_trading_days),
        "min_folds_required": min_folds,
        "min_sessions_required": min_sessions,
        "current_sessions": n_sessions,
        "extra_sessions_needed": extra,
        "extra_years_needed": extra / sessions_per_year,
        "sessions_per_year": sessions_per_year,
        "note": (
            f"门槛按**交易日**计。增加标的**不增加**有效样本量（按日聚类），"
            f"只会增加高度相关的行数；要补足样本外交易日只能拉长历史，"
            f"或缩小 test 窗口（代价是每折估计更不稳）。"
        ),
    }


# ---------- 报告 ----------

def build_report(*, generated: str, axis: Sequence[str], dates_by_code: Mapping[str, Iterable[str]],
                 train: int, test: int, step: int | None = None, embargo: int = 0,
                 skipped: Mapping[str, str] | None = None,
                 threshold: int = DEFAULT_MIN_OOS_DAYS) -> dict:
    """组装报告字典（纯函数，不含任何 I/O）。

    数据不足时**向上抛** `InsufficientData`（不由本函数降级成空报告）。
    """
    step = test if step is None else step
    axis = _validate_axis(axis)          # 升序守卫在切分之前，错误信息更贴近根因
    folds = split_walk_forward(axis, train=train, test=test, step=step, embargo=embargo)
    dates_by_code = {c: list(d) for c, d in dates_by_code.items()}
    sample = sample_size_report(folds, dates_by_code=dates_by_code)
    tail = unused_tail(folds, axis)
    return {
        "generated": generated,
        "params": {"train": train, "test": test, "step": step, "embargo": embargo},
        "universe": sorted(dates_by_code),
        "skipped": dict(skipped or {}),
        "session_axis": {"start": axis[0], "end": axis[-1], "n_sessions": len(axis)},
        "unused_tail": {"n_days": len(tail), "first": tail[0] if tail else None,
                        "last": tail[-1] if tail else None},
        "folds": [f.as_dict() for f in folds],
        "sample_size": sample,
        "threshold": threshold_arithmetic(
            train=train, test=test, step=step, n_sessions=len(axis),
            oos_trading_days=sample["effective_n"], threshold=threshold),
        "disclosure": {
            "insample": False,
            "strategy_conclusion": None,
            "note": "只报折分与样本量；不含 in-sample 数字，不做策略结论。",
        },
    }


# ---------- 报告渲染 ----------

def render_markdown(report: dict) -> str:
    """把报告字典渲染成 Markdown（**纯函数**：同一输入 → 同一文本）。"""
    p = report["params"]
    ss = report["sample_size"]
    th = report["threshold"]
    axis = report["session_axis"]
    lines = [
        f"# Walk-forward 切分报告（{report['generated']}）",
        "",
        "> 本报告只回答「**样本外样本量够不够**」，不含任何策略绩效结论。",
        "> 折分可复现：同一输入 → 同一折分（切分器是纯函数）。",
        "",
        "## 参数",
        "",
        f"- 会话轴：{axis['start']} ~ {axis['end']}，共 **{axis['n_sessions']}** 个交易日"
        f"（口径：{axis.get('mode', 'intersection')} = 各标的公共交易日）",
        f"- 被公共轴裁掉的私有区间（条）：{axis.get('dropped_by_code') or '无'}",
        f"- train / test / step / embargo：{p['train']} / {p['test']} / {p['step']} / {p['embargo']}",
        f"- 标的（{len(report['universe'])}）：{', '.join(report['universe']) or '（无）'}",
        f"- 复权读取被拒（**不回退**）：{report['skipped'] or '无'}",
        "",
        "## 折分",
        "",
        f"折数：**{len(report['folds'])}**",
        "",
        f"- 轴末尾未被任何折用到：**{report['unused_tail']['n_days']}** 个交易日"
        f"（{report['unused_tail']['first']} ~ {report['unused_tail']['last']}）"
        f" —— walk-forward 天然用不满尾部（凑不出完整 test 窗），此处**如实报出**而非静默截断",
        "",
        "| 折 | train 区间 | train 天数 | test 区间 | test 天数 | embargo 剔除 |",
        "|---:|---|---:|---|---:|---:|",
    ]
    for f in report["folds"]:
        lines.append(
            f"| {f['index']} | {f['train_start']} ~ {f['train_end']} | {f['n_train']} "
            f"| {f['test_start']} ~ {f['test_end']} | {f['n_test']} | {f['n_purged']} |"
        )
    lines += [
        "",
        "## 样本量（口径：交易日）",
        "",
        f"- 样本外**交易日**（有效样本量）：**{ss['effective_n']}**",
        f"- 样本外标的-日行数（参考，**不得**当样本量）：{ss['oos_rows']}"
        f"（{ss['n_codes']} 个标的 → 平均 {ss['rows_per_day']:.1f} 行/日）",
        f"- 测试窗口重叠：{'⚠️ 有重叠（有效样本量已按去重计）' if ss['overlap_detected'] else '无'}",
        "",
        "## 120 交易日门槛判断",
        "",
        f"- 门槛：{th['threshold']} 个交易日（{th['threshold_unit']}）",
        f"- 实测样本外交易日：{th['oos_trading_days']}",
        f"- **结论：{'✅ 达标' if th['meets'] else '❌ 未达标'}**"
        f"{'（超出门槛 ' + str(th['oos_trading_days'] - th['threshold']) + ' 个交易日）' if th['meets'] else '（还差 ' + str(th['deficit_days']) + ' 个交易日）'}",
        f"- 达标所需最少历史：{th['min_sessions_required']} 个交易日"
        f"（= train {p['train']} + test {p['test']} + ({th['min_folds_required']} - 1) × step {p['step']}）",
        f"- 现有历史：{th['current_sessions']} 个交易日"
        f" → 还需 **{th['extra_sessions_needed']}** 个交易日"
        f" ≈ **{th['extra_years_needed']:.2f} 年**",
        "",
        "## 口径声明",
        "",
        f"- {ss['note']}",
        f"- {th['note']}",
        "- 本报告**不含** in-sample 成绩，也**不做**任何策略结论。",
        "",
    ]
    return "\n".join(lines)
