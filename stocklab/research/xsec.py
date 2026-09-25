"""P60 横截面单变量实验：持有集合「前 N」vs「池内全部合格」（方案 B 最小可用）。

## 它回答什么（**不是**「池等权持有一段时间赚不赚钱」）

那已经答过了：`candidate/replay.py::period_returns` 持有的就是 `select_top` 选出的
前 N 只。本实验只换**持有集合的定义**这一个变量：

| 臂 | 持有集合 |
|---|---|
| `topn`（现状） | `select_top` 前 N 只（`POOL_TOPN["short"]`） |
| `all`（对照） | 该池**全部合格标的**（`select_top` 截断之前，`PipelineResult.eligible`） |

判定**复用** `plugin/sandbox.py` 的既有常量与函数（`MIN_VALID_PERIODS`、
`_BOOTSTRAP_N`、`_BOOTSTRAP_SEED`、`_bootstrap_ci`、`overfit_flag`），
不新增第二份门槛 / CI / 成本 / 基准口径（模块2 需求书 §5 A5）。

## 自己不出任何信号

两臂的成员都来自既有的 `score_pipeline`（现算、只读）。本站是**基建**，
不是策略（设计稿 §4 #1）：一旦自己出选股信号，就会与通路 A 的 A1 形成第二套
选股口径，A1 的归因立刻失效。

## fail-closed 预注册

`--prereg` 指向一份**跑之前就提交**的 md，里面有一个 ```json``` 块。命令行实参
必须与它**逐字段**一致，否则 exit 2、**零输出、不跑回放**。理由：同一个短池在
11 年窗上是 +1.10%/期、3 年窗上是 −0.27%/期 ⇒ **窗口选择即结论**，不预注册就
等于自选区间（`CLAUDE.md` 反过拟合红线）。

## 为什么不复用 `_pools_for_test`

`candidate/replay.py:116` 明写那是**测试接缝，生产路径不传**。本站走的是产品
参数 `hold_override`（硬约束 1）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Mapping, Sequence

from stocklab.candidate import pools as candidate_pools
from stocklab.candidate import replay
from stocklab.plugin import sandbox
from stocklab.config import paths
from stocklab.config.universes import UniverseError, resolve_universe

#: 实验短名。与预注册 json 的 `experiment` 字段逐字相同。
EXPERIMENT = "xsec-topn"

#: 只跑短期池：`mid` 需 32.6 年、`long` 需 97.9 年才够 120 个验证周期（纯算术，
#: 设计稿 §2 Q2 结论三）。跑别的池不是「保守」，是白费机时且结论必是样本不足。
ONLY_POOL = "short"

#: 窗口起点下界。**必须显式传**且不得早于它（硬约束 4）。
MIN_START = "2015-01-01"

ARM_TOPN = "topn"
ARM_ALL = "all"
ARM_BOTH = "both"
ARM_CHOICES: tuple[str, ...] = (ARM_TOPN, ARM_ALL, ARM_BOTH)

#: 预注册里对两臂的命名（逐字，取自任务书 T1 的 json）。
HOLD_ARM = "topn"
HOLD_CONTROL = "all-eligible"

BENCHMARK = replay.BENCHMARK_CODE

#: 预注册 json 的**必填字段**。少一个即 exit 2 —— 缺字段的预注册不构成预注册。
#: `universe` 在**清单里**（D7：要能钉住「跑的是哪个宇宙」，否则同一份预注册换宇宙
#: 能跑出两个结论 = 静默换宇宙）但**不在「缺即拒」那一档**，理由见
#: `PREREG_OPTIONAL_FIELDS`。
PREREG_FIELDS: tuple[str, ...] = (
    "experiment", "pool", "start", "universe", "topn", "hold_arm", "hold_control",
    "min_periods", "bootstrap_n", "bootstrap_seed", "benchmark", "rule")

#: 允许**缺席**的预注册字段（缺席有明确语义，不是「没预注册」）。
#: 老预注册 `docs/experiments/2026-09-24-xsec-topn.md` 是 append-only 的台账，
#: **一字不许改**（P70 设计稿 §7.1）⇒ 它必然缺 `universe`。按 nanobot 的兼容裁决，
#: 缺席 ⇒ 语义视为 `seed21`（见 `DEFAULT_PREREG_UNIVERSE`）。**这不等于放行**：
#: `validate_prereg` 仍拿命令行实参去比对，所以「预注册推得 seed21、命令行传
#: csi300-500」照样 exit 2 —— 堵的正是「静默换宇宙」那类错误。
PREREG_OPTIONAL_FIELDS: tuple[str, ...] = ("universe",)

#: `universe` 字段缺席时的语义（nanobot 兼容裁决）。写进报告一行，让读者看得见。
DEFAULT_PREREG_UNIVERSE = "seed21"

_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.S)


class PreregError(ValueError):
    """预注册缺失 / 不合规 / 与命令行实参不一致。调用方一律 exit 2、零输出。"""


#: 设计稿 §Q3 的三条**已知非 PIT 项**。报告里必须并列写出（硬约束 6）。
NON_PIT_ITEMS: tuple[str, ...] = (
    "非 PIT ①：ST 判定取 `Instrument.name` 的**当前**名称"
    "（`candidate/screen.py:82-86` 源码自述「非 PIT…仅为近似」）。",
    "非 PIT ②：行业分类取 `instruments.sector` 的**当前**值"
    "（`candidate/score.py:96-97`）。",
    "非 PIT ③：种子宇宙是**事后挑选**的 21 只白马蓝筹"
    "（`candidate/seeds.py:30-59`），不是当日的全市场横截面。",
)

#: 设计稿 §Q4 的成本口径偏差（**本次修掉一半，另一半如实披露**）。
COST_CALIBER_NOTE = (
    "成本口径偏差：① 本次按 `instruments.type` **逐标的**取 `CostModel`"
    "（ETF 免印花税/过户费），修掉 `period_returns` 原来对 ETF 也按 stock 费率计的"
    "既知偏差（ADR-008 / 设计稿 §Q4）；② 价格侧停牌语义与 "
    "`paper/rules.py::check_no_lookahead` **不同**：守卫允许「停牌用上一交易日收盘」，"
    "回放口径 `_close_on` 在停牌日返回 `None`、该只被摈出 gross（等价于持现金）——"
    "回放更保守，但不是同一个语义（设计稿 §Q3）。"
)

#: 选择偏差的限定句。Δ 为正**也只能这么说**（设计稿 §4 #12）。
SELECTION_BIAS_SENTENCE = (
    "选择偏差：Δ 为正也只能说「**在这 21 只、这段历史上成立**」，"
    "不能说「选股有效」。"
)

#: §3 里那句「种子只有 21 只」。**对默认路径（`seed21`）逐字不变**；
#: 扩池后「种子」不再成立 ⇒ 按实际扫描宇宙陈述（见 `_seed_scope_note`）。
SEED_SCOPE_NOTE = "种子只有 21 只"


def _seed_scope_note(universe_id: str, n: int) -> str:
    """§3 的扫描范围陈述。宇宙不是 `seed21` 时按实参写 —— 否则报告会把
    800 只的扫描说成「只有 21 只」，与实参直接矛盾（那正是 D7 要堵的那类错）。"""
    if universe_id == DEFAULT_PREREG_UNIVERSE:
        return SEED_SCOPE_NOTE
    return f"扫描宇宙 `{universe_id}` 只有 {n} 只"


def _selection_bias_note(universe_id: str, n: int) -> str:
    """选择偏差限定句。`seed21` ⇒ 逐字用 `SELECTION_BIAS_SENTENCE`；
    扩池 ⇒ 把「这 21 只」换成实际宇宙（限定的是**同一个意思**，不是放宽口径）。"""
    if universe_id == DEFAULT_PREREG_UNIVERSE:
        return SELECTION_BIAS_SENTENCE
    return (f"选择偏差：Δ 为正也只能说「**在 `{universe_id}` 这 {n} 只、"
            "这段历史上成立**」，不能说「选股有效」。")

WINDOW_IS_CONCLUSION_NOTE = (
    "窗口即结论：同一个 `short` 池 11 年窗（2015-01-01 起）+1.10%/期、"
    "3 年窗（2023-09-24 起）−0.27%/期 ⇒ 窗口必须先预注册，跑完不许改。"
)

#: D1 的限定句：本档的宇宙**只有现成分**，报告一律打 `non_pit=true` 并逐条写出 N1/N2/N3。
#: ⚠️ 这三条**不与** `NON_PIT_ITEMS`（种子宇宙那三条）合并 —— 那三条是「21 只是事后挑选
#: 的白马蓝筹」，这三条是「成员表只有今天这一份」。扩池换掉的是偏差的**类型**，
#: 不是「有没有偏差」。
NON_PIT_UNIVERSE_ITEMS: tuple[str, ...] = (
    "non_pit=true（宇宙层面）：成员表是**今天**（`MAXTRADEDATE` 只有一天）的名单。"
    "N1 **双向**生存偏差：① 当时在指数里、现在已被剔除的标的**不在名单里**（取不到）；"
    "② 今天的成员被**回溯地**当成早年就在池里（名单里就有上市晚于窗口起点的次新股）。"
    "两个方向相反、**不会互相抵消**。",
    "N2 退市股缺席：退市**名单**拿不到（退市股 K 线技术上采得到 —— 缺的是名单，"
    "不是数据源能力）。",
    "N3 无历史成分：`RPT_INDEX_CONSTITUENT.TRADE_DATE` 只给「今天仍在成分里的成员"
    "各自的入选日」，给不出「剔除日」⇒ **没有 `asof` 切片**，不许假装有。",
    "N4 指数只到 800 只：中证800（沪深300 ∪ 中证500）之外，东财**同一个报表**还有"
    "更大的集合（`TYPE=7` / `TYPE=13` 等），但那些 `TYPE` 的**指数名未核实** ⇒ "
    "不许按行数把它们推断成某个更大的宽基指数名当事实用。本档不含，留接口、不预支"
    "（设计稿 §2.2 N4）。",
)


# ---------------------------------------------------------------------------
# 预注册
# ---------------------------------------------------------------------------

def load_prereg(path: Path) -> tuple[dict, str]:
    """读出预注册 json 与整份文件的 sha256。

    文件缺失 / 读不出 / 没有 ```json``` 块 / json 非法 / 缺**必填**字段 → `PreregError`
    （fail-closed：宁可跑不起来，也不要在「没预注册」的状态下跑出一个结论）。

    `PREREG_OPTIONAL_FIELDS` 里的字段（目前只有 `universe`）**允许缺席**，返回的
    `data` 原样不含它 —— 不注入、不补默认值（补了会让「这份预注册写过什么」失真）。
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
            "窗口/池/N/判据是否被改过")
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


def validate_prereg(data: Mapping, *, pool: str, start: str,
                    topn: int, universe: str | None = None) -> None:
    """命令行实参 vs 预注册：**任一不一致即拒跑**（硬约束 T2）。

    `topn` 由调用方从 `POOL_TOPN` **读出**后传入（不许手抄）；这里同时钉住
    「预注册写的 N 就是代码要跑的 N」和「判定规则常量与预注册一致」——
    后者是**加强**：只比对 pool/start/topn 的话，预注册可以把 `min_periods`
    写成 30 而代码仍按 120 判，报告读起来却是「已预注册」。

    `universe`：预注册**没写**该字段 ⇒ 按 `DEFAULT_PREREG_UNIVERSE`（`seed21`）
    解释，再与命令行实参比对 —— 于是「老预注册（seed21 语义）+ 命令行
    `--universe csi300-500`」**拒跑**（exit 2），「静默换宇宙」被堵死。
    """
    def _mismatch(field: str, got, want) -> "PreregError":
        return PreregError(
            f"预注册不一致：{field} 预注册={want!r} 命令行/代码={got!r}"
            f" —— exit 2，零输出、不跑回放")

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
            f" —— exit 2，零输出、不跑回放：**换宇宙必须新预注册**，"
            "否则同一份预注册能跑出两个结论（静默换宇宙）")
    if data["topn"] != topn:
        raise _mismatch("topn", topn, data["topn"])
    if data["hold_arm"] != HOLD_ARM:
        raise _mismatch("hold_arm", HOLD_ARM, data["hold_arm"])
    if data["hold_control"] != HOLD_CONTROL:
        raise _mismatch("hold_control", HOLD_CONTROL, data["hold_control"])
    if data["min_periods"] != sandbox.MIN_VALID_PERIODS:
        raise _mismatch("min_periods", sandbox.MIN_VALID_PERIODS,
                        data["min_periods"])
    if data["bootstrap_n"] != sandbox._BOOTSTRAP_N:
        raise _mismatch("bootstrap_n", sandbox._BOOTSTRAP_N, data["bootstrap_n"])
    if data["bootstrap_seed"] != sandbox._BOOTSTRAP_SEED:
        raise _mismatch("bootstrap_seed", sandbox._BOOTSTRAP_SEED,
                        data["bootstrap_seed"])
    if data["benchmark"] != BENCHMARK:
        raise _mismatch("benchmark", BENCHMARK, data["benchmark"])


# ---------------------------------------------------------------------------
# 实验
# ---------------------------------------------------------------------------

def _scan_holds(conn: sqlite3.Connection, *, marks: list[str],
                pool: str, universe=None
                ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """逐调仓日跑一次 `score_pipeline`，取两个持有集合。

    一次扫描同时得到两臂 —— 两臂的成员来自**同一次**打分，不存在「两臂用了
    不同时点的分数」这种口径漂移。走的是生产路径（不传任何测试接缝）。

    `universe`：扫描宇宙（`Instrument` 序列）。`None` ⇒ `SEED_UNIVERSE`
    （默认路径逐位不变）；扩池时由 `run_xsec_topn` 解析宇宙 id 后传入。
    """
    from stocklab.candidate.run import score_pipeline

    topn: dict[str, list[str]] = {}
    eligible: dict[str, list[str]] = {}
    for day in marks:
        res = score_pipeline(conn, asof=day, universe=universe)
        topn[day] = sorted(m.code for m in res.members if m.pool == pool)
        eligible[day] = list(res.eligible.get(pool, []))
    return topn, eligible


def _verdict(validate: Sequence[float]) -> dict:
    """判定 —— token 与口径**完全复用** `plugin/sandbox.py::run_sandbox` 的尾部。

    `sandbox.py` 明确「不要改」（本站不改成抽函数），所以这里只能 import 它的
    常量与 CI 函数、重贴那三行组装逻辑。**判据本身仍只有一处真源**：
    `MIN_VALID_PERIODS` / `_BOOTSTRAP_N` / `_BOOTSTRAP_SEED` / `_bootstrap_ci`
    都是 import 来的，本模块一个数字都没抄。
    """
    n = len(validate)
    if n < sandbox.MIN_VALID_PERIODS:
        return {"verdict": "INCONCLUSIVE", "n_validate": n,
                "delta": None, "ci_low": None, "ci_high": None,
                "note": f"样本不足（{n} 个有效调仓周期 < "
                        f"{sandbox.MIN_VALID_PERIODS}），不构成结论"}
    delta = sum(validate) / n
    lo, hi = sandbox._bootstrap_ci(list(validate))
    return {"verdict": "WIN" if lo > 0 else "LOSE", "n_validate": n,
            "delta": delta, "ci_low": lo, "ci_high": hi,
            "note": f"验证段 Δ 周期均值 {delta:+.4%}，95% CI "
                    f"[{lo:+.4%}, {hi:+.4%}]，周期数 n={n}"}


def _arm_stats(series: list[float], train: list[float],
               validate: list[float], excess: float) -> dict:
    mean = (sum(series) / len(series)) if series else None
    return {
        "n_periods": len(series),
        "period_returns": series,
        "mean_all": mean,
        "mean_train": (sum(train) / len(train)) if train else None,
        "mean_validate": (sum(validate) / len(validate)) if validate else None,
        "excess_index300": excess,
    }


def run_xsec_topn(conn: sqlite3.Connection, *, pool: str, start: str,
                  end: str, prereg_path: Path,
                  arm: str = ARM_BOTH, universe: str | None = None) -> dict:
    """跑实验，返回报告 dict（**不写任何文件**，落盘交给调用方）。

    `universe`：宇宙 id（`None` ⇒ `seed21`，主干常量）。非 `seed21` 走
    `resolve_universe`（**fail-closed**：文件缺失即抛 `PreregError`，不回退）。

    失败一律 `PreregError`（调用方 exit 2、零输出）。
    """
    if pool != ONLY_POOL:
        raise PreregError(
            f"本实验只跑 {ONLY_POOL!r}（mid 需 32.6 年、long 需 97.9 年才够 "
            f"120 个验证周期）；收到 {pool!r}")
    if start < MIN_START:
        raise PreregError(
            f"窗口起点不得早于 {MIN_START}（收到 {start!r}）—— 窗口即结论")
    if arm not in ARM_CHOICES:
        raise PreregError(f"未知 --arm {arm!r}；已知 {list(ARM_CHOICES)}")

    try:
        universe_id, members, members_sha256 = resolve_universe(universe)
    except UniverseError as exc:
        raise PreregError(f"宇宙载入失败（{universe!r}）：{exc}") from exc

    prereg, prereg_sha = load_prereg(prereg_path)
    topn_n = candidate_pools.POOL_TOPN[pool]      # 读，不手抄
    validate_prereg(prereg, pool=pool, start=start, topn=topn_n,
                    universe=universe_id)

    marks = replay.rebalance_marks(conn, pool=pool, start=start, end=end)
    if len(marks) < 2:
        raise PreregError(
            f"窗口内调仓边界只有 {len(marks)} 个（需 ≥ 2 才能构成周期）："
            f"{start}~{end}")

    t0 = time.time()
    topn_holds, eligible_holds = _scan_holds(conn, marks=marks, pool=pool,
                                             universe=members)
    scan_s = time.time() - t0

    wanted = {ARM_TOPN: (ARM_TOPN, topn_holds),
              ARM_ALL: (ARM_ALL, eligible_holds)}
    arms: dict[str, dict] = {}
    for key, (name, holds) in wanted.items():
        if arm not in (name, ARM_BOTH):
            continue
        series = replay.period_returns(conn, asof_dates=marks, pool=pool,
                                       hold_override=holds)
        train, validate = replay.split_train_validate(series)
        excess = replay.benchmark_excess(conn, asof_dates=marks, pool=pool,
                                         hold_override=holds)
        arms[name] = _arm_stats(series, train, validate, excess)

    delta: dict | None = None
    if ARM_TOPN in arms and ARM_ALL in arms:
        diffs = [a - b for a, b in zip(arms[ARM_TOPN]["period_returns"],
                                       arms[ARM_ALL]["period_returns"])]
        train_d, validate_d = replay.split_train_validate(diffs)
        delta = {
            "definition": "Δ = 现状臂（topn）周期收益 − 对照臂（all-eligible）周期收益",
            "period_deltas": diffs,
            "n_periods": len(diffs),
            "n_train": len(train_d),
            "mean_all": (sum(diffs) / len(diffs)) if diffs else None,
            "mean_train": (sum(train_d) / len(train_d)) if train_d else None,
            "mean_validate": (sum(validate_d) / len(validate_d))
                             if validate_d else None,
            "overfit_flag": sandbox.overfit_flag(
                (sum(train_d) / len(train_d)) if train_d else None,
                (sum(validate_d) / len(validate_d)) if validate_d else None),
            **_verdict(validate_d),
        }

    elapsed = time.time() - t0
    return {
        "experiment": EXPERIMENT,
        "pool": pool,
        "start": start,
        "end": end,
        "arm": arm,
        "universe": universe_id,
        "universe_id": universe_id,
        "universe_n": len(members),
        "universe_members_sha256": members_sha256,
        "prereg_universe": prereg.get("universe", DEFAULT_PREREG_UNIVERSE),
        "topn": topn_n,
        "hold_arm": HOLD_ARM,
        "hold_control": HOLD_CONTROL,
        "min_periods": sandbox.MIN_VALID_PERIODS,
        "bootstrap_n": sandbox._BOOTSTRAP_N,
        "bootstrap_seed": sandbox._BOOTSTRAP_SEED,
        "benchmark": BENCHMARK,
        "rule": prereg["rule"],
        "prereg_path": str(prereg_path),
        "prereg_sha256": prereg_sha,
        "n_marks": len(marks),
        "n_periods": len(marks) - 1,
        "elapsed_s": elapsed,
        "scan_s": scan_s,
        "replay_s": elapsed - scan_s,
        "arms": arms,
        "delta": delta,
        "non_pit_items": list(NON_PIT_ITEMS),
        "non_pit_universe_items": list(NON_PIT_UNIVERSE_ITEMS),
        "universe_note": (
            f"宇宙：`{universe_id}`（{len(members)} 只，`members_sha256` "
            f"`{members_sha256[:12]}…`）。**本 Δ 的对照臂来自 `{universe_id}`，与 "
            f"`seed21` 版（21 只）的 Δ 不可直接比**：宇宙不同、候选池不同、"
            "「池内全部合格」的定义域也不同（设计稿 §7.1）。"),
        "cost_caliber_note": COST_CALIBER_NOTE,
        "selection_bias_note": _selection_bias_note(universe_id, len(members)),
        "seed_scope_note": _seed_scope_note(universe_id, len(members)),
        "window_note": WINDOW_IS_CONCLUSION_NOTE,
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.4%}"


def summary_line(report: Mapping) -> str:
    """md 结尾那一行 `summary:`（nanobot 直接贴给用户）。"""
    d = report.get("delta")
    if d is None:
        arms = "/".join(sorted(report["arms"]))
        return (f"summary: xsec-topn pool={report['pool']} "
                f"{report['start']}~{report['end']} arm={arms} "
                f"n_periods={report['n_periods']} —— 单臂跑，无 Δ 与 verdict")
    return (f"summary: xsec-topn pool={report['pool']} "
            f"{report['start']}~{report['end']} n_periods={report['n_periods']} "
            f"验证段 n={d['n_validate']} "
            f"Δ={_pct(d['mean_validate'])} CI[{_pct(d['ci_low'])},"
            f"{_pct(d['ci_high'])}] verdict={d['verdict']}"
            + ("" if d["verdict"] != "INCONCLUSIVE" else "（样本不足）"))


def render_md(report: Mapping) -> str:
    """人读报告。硬约束 6：三条非 PIT 项与成本口径偏差**并列写出**。"""
    arms = report["arms"]
    d = report.get("delta")
    lines: list[str] = [
        f"# xsec-topn 报告（{report['pool']} 池，{report['start']} ~ "
        f"{report['end']}）",
        "",
        "> 本报告由 `research xsec-topn` 只读生成：**未写任何表**，产物只在 "
        "`reports/`。两臂成员均来自 `score_pipeline`（现算、只读），"
        "本站**不出任何信号** —— 它是基建，不是策略。",
        "",
        "## 0. 预注册",
        "",
        f"- 预注册文件：`{report['prereg_path']}`",
        f"- `prereg_sha256` = `{report['prereg_sha256']}`",
        f"- 判据原文：{report['rule']}",
        f"- {report['window_note']}",
        "",
        "## 1. 两臂（各自绝对收益）",
        "",
        "| 臂 | 持有集合 | 周期数 | 全窗均值/期 | 训练段 | 验证段 | "
        "相对 `sh000300` 超额（周期均值） |",
        "|---|---|---|---|---|---|---|",
    ]
    labels = {ARM_TOPN: f"现状：`select_top` 前 {report['topn']} 只",
              ARM_ALL: "对照：该池**全部合格标的**"}
    for name in (ARM_TOPN, ARM_ALL):
        a = arms.get(name)
        if a is None:
            continue
        lines.append(
            f"| `{name}` | {labels[name]} | {a['n_periods']} | "
            f"{_pct(a['mean_all'])} | {_pct(a['mean_train'])} | "
            f"{_pct(a['mean_validate'])} | {_pct(a['excess_index300'])} |")
    lines += ["", f"- 调仓边界 {report['n_marks']} 个（短池 5 日一调），"
                  f"周期 {report['n_periods']} 个。", ""]

    lines += ["## 2. Δ（现状臂 − 对照臂）与判定", ""]
    if d is None:
        lines += ["> 本次只跑了单臂（`--arm`），没有 Δ，也就没有 verdict。", ""]
    else:
        lines += [
            f"- 定义：{d['definition']}",
            f"- 训练段 n={d['n_train']}，均值 {_pct(d['mean_train'])}",
            f"- **验证段 n={d['n_validate']}，均值 {_pct(d['mean_validate'])}**，"
            f"95% CI [{_pct(d['ci_low'])}, {_pct(d['ci_high'])}]",
            f"- verdict = **{d['verdict']}**；{d['note']}",
            f"- 过拟合标记：`{d['overfit_flag']}`",
        ]
        if d["verdict"] == "LOSE":
            crosses = d["ci_low"] <= 0.0 <= d["ci_high"]
            lines += [
                "",
                "> `LOSE` 的读法（ADR-017）：",
                ("> CI **跨 0** ⇒ 「**无可测的选股增量**」—— 不是「显著更差」，"
                 "更不是「选股无效」；"
                 if crosses else
                 "> CI **不跨 0** 且整体 < 0 ⇒ 有证据表明现状（选前 N）在这段"
                 "历史上**更差**；"),
                "> 不降门槛、不换窗口把结论救回来。",
            ]
        elif d["verdict"] == "INCONCLUSIVE":
            lines += ["", "> 样本不足：**不是**「没效果」，是「测不出」。"]
        lines += [""]

    lines += ["## 3. 必须并列披露的口径（三条非 PIT ＋ 成本偏差）", ""]
    lines += [f"- {item}" for item in report["non_pit_items"]]
    lines += [f"- {report['cost_caliber_note']}", ""]
    lines += [f"- {report['selection_bias_note']}", "",
              f"> {report.get('seed_scope_note') or SEED_SCOPE_NOTE}、短池 topn 只有 "
              f"{report['topn']}，所以「选前 {report['topn']} vs 持全部」"
              "之间的区分度**结构性地小** —— 这是本实验的固有上限。", ""]

    lines += ["## 3b. 宇宙（P71 的口径增量）", "",
              f"- {report['universe_note']}"]
    lines += [f"- {item}" for item in report.get("non_pit_universe_items", ())]
    lines += ["", f"- 预注册里的 `universe` 字段 = "
                  f"`{report.get('prereg_universe')}`"
                  "（缺席 ⇒ 语义为 `seed21`；命令行与它不一致则 exit 2，"
                  "**换宇宙必须新预注册**）。", ""]

    lines += [
        "## 4. 复现与耗时",
        "",
        "```bash",
        ".venv/bin/python -m stocklab.cli.main research xsec-topn \\",
        f"    --pool {report['pool']} --start {report['start']} \\",
        f"    --prereg {report['prereg_path']} --out reports/research/",
        "```",
        "",
        f"- 总耗时 {report['elapsed_s']:.1f} s"
        f"（扫描 `score_pipeline` {report['scan_s']:.1f} s"
        f" ＋ 两臂回放 {report['replay_s']:.1f} s）",
        f"- `MIN_VALID_PERIODS`={report['min_periods']}、bootstrap "
        f"n={report['bootstrap_n']} seed={report['bootstrap_seed']}"
        f"（**import 自 `plugin/sandbox.py`，未另定**）",
        "",
        summary_line(report),
        "",
    ]
    return "\n".join(lines)


def write_report(report: Mapping, out_dir: Path) -> tuple[Path, Path]:
    """落 `<out>/<end>-xsec-topn-<universe_id>.{json,md}`，返回两个路径。

    **文件名必须带宇宙 id**（P77 T7）：不带时「换宇宙重跑同一个 end」会**覆盖**
    上一份产物 —— 2026-09-25 的扩池重跑就是这样把 P60 的
    `reports/research/2026-09-24-xsec-topn.{md,json}` 覆盖掉的（已不可恢复）。
    `seed21` 也带上 id ⇒ 与旧名不同是**有意的**：旧名本身就是碰撞源。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report['end']}-xsec-topn-{report['universe_id']}"
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
