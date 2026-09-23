"""模块2 自评估三分支判定 + 提前熔断的**判定**部分（P49 §1/§2，D-44/D-27）。

## 这个模块**一个字节都不写库**

判定与执行在这里被物理分开：本模块只把「输入 → 建议 + 依据」算出来，
**没有任何 INSERT / UPDATE / DELETE，也不 import 任何写入模块**
（`plugin.lifecycle::approve`、`plugin.store`、`paper.store` 的写函数）。
落库由 `m2/cycle.py` 在判定**之后**做，且只落两条东西：判定台账一行、
熔断事件一行 —— 策略版本状态、账户、参数**一个字都不动**。

「建议不是执行」是这一站最容易越权的一处（D-44 原文），所以它由
`tests/test_p49_selfeval.py` 的 AST 扫描钉住，而不是靠注释里的自觉：
判定模块里出现写路径 → 判红（带反向自检，喂违规样本必须判红）。

## 三分支规则表（D-44 原文 → 可执行的判据）

| 分支 | 判据 | 落地动作（**要人 `approve` 才发生**） |
|---|---|---|
| 冻结 `freeze` | 读数**未达标**（相对基准超额 ≤ 0）**且**声明无方向 | 停用该版本（`frozen`，≤90 天），台账一行不删 |
| 优化 `optimize` | 有可归因案例**且**声明需改脚本逻辑 | 新 script 版本（人工 `approve`）+ 新策略版本账户（D-26） |
| 微调 `tune` | 有可归因案例**且**声明只动参数 | 同 script 版本新参数集 ⇒ **必须新开验证周期** |
| 证据不足 `insufficient` | 样本 < 120 交易日 / 轮次 < 2 | **只给读数**，不出结论性判定 |

唯一无法从读数推出的一格是「要不要改脚本逻辑」—— 那是**提案的属性**，
所以由调用方声明（`fix_kind`），判定只负责核对它与证据是否自洽：
声明有方向却拿不出案例 ⇒ **拒绝**（依据为空）；声明无方向却读数达标 ⇒ **拒绝**
（冻结的前提不成立）。两处都拒绝而不是「就近挑一个分支」：静默二选一等于
替人做了决定，而人看不到它被做过。

## 越界即拒、不 clamp

轮次 / 天数 / 冻结期的边界一律走 `config/limits.py`（AI 不可改的主干常量），
越界抛 `limits.PlanOutOfBounds`。**「取最近的合法值」在这里是不存在的路径** ——
它会把一个不合规的提案悄悄变成合规的，实验因此无法归因（D-28）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from stocklab.config import limits
from stocklab.m2 import config as m2_config
from stocklab.paper.engine import drawdown


class Refused(ValueError):
    """判定请求与证据不自洽 —— **拒绝判定**，不写任何行（退出码 2）。"""


#: 主干边界的**只读视图**（P49 / D-32 的配置视图那一格）。
#: 单一真源仍然是 `config/limits.py` —— 这里只做「摆出来给人看」，没有任何
#: 写入路径；`labweb/` 因此可以显示这些数而不直接引用常量名
#: （ADR-021 的结构保证 3 要求 `labweb/` 对这 5 个名字**零引用**，
#: 而「只读配置视图」是 P49 才开的那扇窗 —— 开在**值**上，不开在**名**上）。
BOUNDARIES: dict = {
    "rounds_min": limits.VALIDATION_ROUNDS_MIN,
    "rounds_max": limits.VALIDATION_ROUNDS_MAX,
    "max_days": limits.VALIDATION_MAX_DAYS,
    "freeze_max_days": limits.FREEZE_MAX_DAYS,
    "circuit_breaker_drawdown": limits.CIRCUIT_BREAKER_DRAWDOWN,
}

#: `BOUNDARIES` 的中文名（页面与文本报告同一批列名，不各写一份）。
BOUNDARY_LABELS: dict[str, str] = {
    "rounds_min": "每周期最少轮次",
    "rounds_max": "每周期最多轮次",
    "max_days": "单轮/验证期最长天数",
    "freeze_max_days": "冻结最长天数",
    "circuit_breaker_drawdown": "熔断回撤阈值",
}


# ---------- 熔断（D-27） ----------


def nav_drawdown_series(rows: Sequence[Mapping]) -> dict:
    """净值序列的**最大回撤**（复用 `paper/engine.py::drawdown`，不另写算法）。

    `rows` = 窗口内按日期升序的净值行（每条要有 `date` 与 `nav`）。
    返回的 `at_value` 是窗口内**最深的那一次**回撤：对每一天算
    「该日相对**此前峰值**的回撤」，取最大 —— 与既有回撤列同一份实现，
    所以「页面上的回撤」与「触发熔断的那个数」永远是同一个数。

    **只读这条序列**：不读墙上时钟、不读「当前最新净值」以外的输入，
    同一份净值重放 ⇒ 逐位相同的 `at_value`（P49 §2 的可复现判据）。
    """
    best = 0.0
    best_date: str | None = None
    best_nav: float | None = None
    peak: float | None = None
    for i, row in enumerate(rows):
        nav = float(row["nav"])
        value = drawdown([float(r["nav"]) for r in rows[:i]], nav)
        if value > best:
            best, best_date, best_nav = value, str(row["date"]), nav
        peak = nav if peak is None else max(peak, nav)
    return {
        "at_value": round(best, 6),
        "trough_date": best_date,
        "trough_nav": best_nav,
        "peak_nav": peak,
        "window_start": (str(rows[0]["date"]) if rows else None),
        "window_end": (str(rows[-1]["date"]) if rows else None),
        "n_obs": len(rows),
    }


def fuse_verdict(rows: Sequence[Mapping]) -> dict:
    """熔断判定：`at_value >= CIRCUIT_BREAKER_DRAWDOWN` ⇒ 触发（D-27）。

    实测值与阈值**分开返回**（写入层把它们分列落库）：只留一个数，读者无法
    判断到底有没有越界。窗口内没有净值行 ⇒ **不猜**：`at_value=None`、
    `tripped=False`、原因写清楚（「判不了」与「没触发」不是一回事）。
    """
    threshold = float(limits.CIRCUIT_BREAKER_DRAWDOWN)
    if not rows:
        return {
            "at_value": None, "threshold": threshold, "tripped": False,
            "trough_date": None, "trough_nav": None, "peak_nav": None,
            "window_start": None, "window_end": None, "n_obs": 0,
            "reason": "窗口内没有净值行 —— 判不了（不拿 0 顶替：0 回撤是「没跌」，"
                      "与「不知道」不是一回事）",
            "criteria_text": m2_config.CIRCUIT_CRITERIA_TEXT,
        }
    dd = nav_drawdown_series(rows)
    tripped = dd["at_value"] >= threshold
    reason = (
        f"窗口 {dd['window_start']} ~ {dd['window_end']} 共 {dd['n_obs']} 个净值日，"
        f"最深回撤 {dd['at_value']:.6f}"
        + (f"（{dd['trough_date']}，净值 {dd['trough_nav']:.2f}）" if dd["trough_date"] else "")
        + f" vs 阈值 {threshold:.6f} ⇒ " + ("触发熔断" if tripped else "未触发"))
    return {**dd, "tripped": tripped, "threshold": threshold, "reason": reason,
            "criteria_text": m2_config.CIRCUIT_CRITERIA_TEXT}


# ---------- 三分支判定（D-44） ----------


def _step(*items: str) -> list[str]:
    return list(items)


def decide_branch(*, n_rounds: int, planned_rounds: int, planned_days: int,
                  gate: Mapping, metrics: Mapping, excess: float | None,
                  cases: Mapping, fix_kind: str, freeze_days: int | None) -> dict:
    """三分支判定：返回**建议 + 依据**（不返回任何执行动作的对象，只有文案）。

    `gate` = `paper_data.sample_gate(n_sessions)` 的原样返回（**同一把尺子**，
    不在这一层再定义门槛）；`metrics` = P41 五指标的逐字段复制；
    `cases` = 错判案例集的**汇总**（条数 + 来源指纹，见 `m2_data`）。
    """
    if fix_kind not in m2_config.FIX_KINDS:
        raise Refused(
            f"未知的改进方向 {fix_kind!r} —— 必须是 {list(m2_config.FIX_KINDS)}"
            " 之一（`none` = 声明没有可归因的方向）")
    if not (limits.VALIDATION_ROUNDS_MIN <= planned_rounds
            <= limits.VALIDATION_ROUNDS_MAX):
        raise Refused(
            f"台账里的周期轮次 {planned_rounds} 不在 "
            f"[{limits.VALIDATION_ROUNDS_MIN}, {limits.VALIDATION_ROUNDS_MAX}] 内 —— "
            "拒绝判定，**不 clamp**（把越界方案夹到合法值上，实验就无法归因）")

    met_target = None if excess is None else (float(excess) > 0.0)
    evidence = {
        "n_rounds": int(n_rounds),
        "planned_rounds": int(planned_rounds),
        "planned_days": int(planned_days),
        "gate": dict(gate),
        "metrics": {str(k): v for k, v in metrics.items()},
        "excess_vs_index_300": excess,
        "met_target": met_target,
        "cases": dict(cases),
        "fix_kind": fix_kind,
        "freeze_days": freeze_days,
        "thresholds": dict(BOUNDARIES),
        "steps": _step(
            "① 读 `validation_cycles`（判据原文 + 计划轮次/天数，原值，未改写）",
            f"② 读 `validation_rounds`：已落 {n_rounds} 轮（门槛 ≥ "
            f"{limits.VALIDATION_ROUNDS_MIN}）",
            "③ 调 `paper_data.performance(conn, asof)` 取该策略版本账户的五指标"
            "（P41 真源，本模块不重算）",
            "④ 门禁 = `paper_data.sample_gate(n_sessions)`（P41 同一把尺子）",
            "⑤ 读错判案例集汇总（P48 的取数，只读引用；归因恒空）",
            "⑥ 按 D-44 规则表选分支：门禁/轮次不足 → 证据不足；否则看"
            "「读数是否达标」+「声明有无方向/是否改逻辑」",
        ),
    }

    insufficient_reason = None
    if not gate.get("meets"):
        insufficient_reason = (
            f"样本不足（{gate.get('n_sessions')} < {gate.get('threshold')} 个交易日）—— "
            "只给读数，不出「优化 / 冻结」的结论性判定（CLAUDE.md 度量纪律 3）")
    elif n_rounds < limits.VALIDATION_ROUNDS_MIN:
        insufficient_reason = (
            f"已落轮次 {n_rounds} < {limits.VALIDATION_ROUNDS_MIN} —— "
            "轮次读数不足，按纪律不许出结论性判定")

    if insufficient_reason is not None:
        return {
            "branch": m2_config.BRANCH_INSUFFICIENT,
            "branch_label": m2_config.BRANCH_LABELS[m2_config.BRANCH_INSUFFICIENT],
            "conclusion": False, "reason": insufficient_reason,
            "evidence": evidence, "actions": [],
        }

    if fix_kind == m2_config.FIX_NONE:
        if freeze_days is None:
            raise Refused(
                "声明「无方向可归因」时必须给冻结天数（`--freeze-days`）—— "
                "冻结期是主干常量管的，缺了就无法校验是否越界")
        limits.check_freeze_days(days=int(freeze_days))   # 越界抛 PlanOutOfBounds（不 clamp）
        if met_target is None:
            raise Refused(
                "基准读数缺失（窗口内 `sh000300` 没有收盘价）⇒ 无法判定读数是否达标，"
                "而冻结的**前提**是读数未达标 —— 拒绝判定，不猜")
        if met_target:
            raise Refused(
                f"实测相对基准超额 {float(excess):+.2%} > 0 ⇒ 读数达标，"
                "冻结的前提（读数未达标）不成立 —— 拒绝判定（不替调用方改分支）")
        return {
            "branch": m2_config.BRANCH_FREEZE,
            "branch_label": m2_config.BRANCH_LABELS[m2_config.BRANCH_FREEZE],
            "conclusion": True,
            "reason": (f"读数未达标（相对基准超额 {float(excess):+.2%} ≤ 0）"
                       "且声明无方向可归因 ⇒ 建议冻结该策略版本，冻结期 "
                       f"{int(freeze_days)} 天（上限 {limits.FREEZE_MAX_DAYS}）"),
            "evidence": evidence,
            "actions": [
                "人工 `approve` 该版本 `freeze` 事件（本判定**不执行**）",
                f"冻结期 {int(freeze_days)} 天 ≤ {limits.FREEZE_MAX_DAYS} 天；"
                "到期解冻仍走人工",
                "台账一行不删（append-only，追加事件表达状态变化）",
            ],
        }

    if not cases.get("n_cases"):
        raise Refused(
            "声明有明确改进方向，但错判案例集为空（0 条方向判错的样本）—— "
            "**依据为空**，拒绝判定：编一个方向比留空危险得多，因为它看起来像个结论")

    branch = (m2_config.BRANCH_OPTIMIZE if fix_kind == m2_config.FIX_LOGIC
              else m2_config.BRANCH_TUNE)
    n_cases = int(cases["n_cases"])
    if branch == m2_config.BRANCH_OPTIMIZE:
        actions = [
            "人工 `approve` 新 script 版本（走既有 plugin 生命周期；本判定**不执行**）",
            "新策略版本 = 新模拟账户 + 独立 NAV（D-26），并新开验证周期",
        ]
        reason = (f"有可归因方向（{n_cases} 条方向判错样本）且声明需改脚本逻辑 ⇒ "
                  "建议出新 script 版本（执行要人）")
    else:
        actions = [
            "同 script 版本下出新参数集，**必须新开验证周期**（本判定**不执行**）",
            "新周期的 `params_json` 与**判据原文**一并落库（事后不许换口径）",
        ]
        reason = (f"有可归因方向（{n_cases} 条方向判错样本）且声明只动参数 ⇒ "
                  "建议同版本新参数集 + 新验证周期（执行要人）")
    return {
        "branch": branch, "branch_label": m2_config.BRANCH_LABELS[branch],
        "conclusion": True, "reason": reason, "evidence": evidence,
        "actions": actions,
    }
