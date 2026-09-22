"""智能体动态编排臂（`arm-agent`）的**规格与校验**（P37 阶段 1）。

## 这个模块管什么

`arm-agent` 与静态三臂的差别只有一处：**条文里的数字从哪来**。
静态臂从 `paper/config.py` 的常量来（写死），`arm-agent` 从一张 append-only
台账（`paper_agent_decisions`）里「当前有效的那一版 spec」来。本模块是那版 spec 的
**定义、校验、序列化与落地**，不含任何 LLM 调用（阶段 2 才接）。

## 变更空间是一张白名单，不是自由发挥

只有 5 个字段（设计 §3.2）：`etf_target_pct` / `stop_loss_pct` / `max_single_pct` /
`rebalance_cadence` / `cash_floor_pct`。越界、未知字段、类型不符一律 `SpecViolation`
—— **不夹紧、不静默丢弃**。夹紧会把「智能体提出了一个非法改动」变成「系统批准了一个
它没提过的改动」，而这两件事在这张台账上必须能分开。

改白名单、改成本口径、改 PIT 判据、改整手口径、加方向信号，都只能表现为
「未知字段被拒」—— 这是刻意的：**扩大自己变更空间**的字段名不在 `SPEC_SCHEMA` 里。
最强的证据是 `test_schema_is_exactly_the_five_decided_fields`：白名单多一个字段就红。

## 默认 spec = `arm-discipline-10` 口径

默认值五个字段逐一对齐静态臂的**现值**，而且是从现值**推出来**的、不是另抄一份：

| 字段 | 默认值从哪来 |
|---|---|
| `etf_target_pct` | `AGENT_DEFAULT_ARM`（= `arm-discipline-10`）在 `ETF_TRANCHES` 里的那一档 |
| `stop_loss_pct` | `PER_CODE_LINES['000333'].stop_loss_close ÷ HOLD_COST_PRICE − 1` |
| `max_single_pct` | `DISCIPLINE['single_position_max_pct']` |
| `rebalance_cadence` | D-17 的「每 5 交易日 1 次」 |
| `cash_floor_pct` | `DISCIPLINE['cash_band_pct'][0]` |

`stop_loss_pct` 的默认值**不是**设计文档表格里印的 `−5.4`：那是它的两位近似，
用近似值会让「默认 spec 与静态臂同口径」从一句可验证的话退化成一个大概。
精确值 −5.368664 让默认 spec 推出的止损线与静态臂 `82.14` **逐位相同**
（`test_default_stop_loss_line_equals_the_written_line` 钉住）。

## 写入面只有两个函数

`record_decision`（只增）与 `current_spec` / `latest_decision`（只读）。
不提供 UPDATE / DELETE —— 改错只能再审一次，与 `paper_*` 三表同一套 append-only 纪律。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Mapping

from stocklab.paper.config import (
    AGENT_DEFAULT_ARM,
    DISCIPLINE_PREFIX,
    ETF_TRANCHES,
    HOLD_CODE,
    HOLD_COST_PRICE,
    MAX_TRIALS_PER_REVIEW,
    RULE_CITATIONS_AGENT,
)
from stocklab.paper.rules import RuleParams
from stocklab.portfolio.discipline import DISCIPLINE, lines_for

#: 「关掉这条规则」的字面值。**只有止损**允许关（见 `SPEC_SCHEMA`）。
SPEC_OFF = "off"

#: `agent_kind` 的三种取值。`manual` = 人手写的 spec（阶段 1 只有这一种），
#: `llm` = 智能体产出的（阶段 2），`random` = 随机抽的对照臂（阶段 3）。
AGENT_KIND_MANUAL = "manual"
AGENT_KIND_LLM = "llm"
AGENT_KIND_RANDOM = "random"

#: 手写 spec 的 model_id / prompt 指纹（阶段 2 换成真模型的）。
MANUAL_MODEL_ID = "manual"


def _manual_prompt_sha256() -> str:
    return hashlib.sha256(MANUAL_MODEL_ID.encode("utf-8")).hexdigest()


#: `paper spec set` 写进台账的固定指纹。**不是占位符**：它如实说明这版 spec 不是
#: 任何模型产出的，所以「同 prompt + 同 context 必须得同 spec」这条复现性判据
#: 对它天然成立（人手写的 spec 由人负责，不被当成模型证据）。
MANUAL_PROMPT_SHA256 = _manual_prompt_sha256()


class SpecViolation(Exception):
    """spec 越界 / 未知字段 / 类型不符 / 超试错预算。**拒绝，不夹紧**。

    三个属性是为了可断言、可渲染：`field` / `value` / `reason`。
    `rejected_json` 里落的也是这三个（一行一条被拒的试错）。
    """

    def __init__(self, field: str, value: object, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"{field}={value!r} —— {reason}")

    def as_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "reason": self.reason}


@dataclass(frozen=True)
class SpecField:
    """一个可被智能体改的字段。区间是**闭区间**，越界即拒。"""

    name: str
    kind: str                 # 'number' | 'number_or_off' | 'choice'
    unit: str
    default: object
    doc: str
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    #: True ⇒ 只许朝「更保守」的方向改（下界就是静态臂现值）。
    tighten_only: bool = False

    def range_text(self) -> str:
        if self.choices:
            return f"只能是 {list(self.choices)} 之一"
        off = "（或 'off'）" if self.kind == "number_or_off" else ""
        bound = f"{self.lo:g} ~ {self.hi:g}{off}"
        if self.tighten_only:
            bound += "，且**只许收紧**（放宽即拒）"
        return bound


def _default_etf_target_pct() -> float:
    """默认 ETF 目标占比 = `AGENT_DEFAULT_ARM` 那条静态臂的目标。

    `arm-discipline-10` 的 `10` 与 `ETF_TRANCHES` 里的 `10.0` 是**同一个数**，
    但代码里不该出现第二次字面量 —— 于是从 arm id 反查回去。
    """
    suffix = AGENT_DEFAULT_ARM[len(DISCIPLINE_PREFIX):]
    for t in ETF_TRANCHES:
        if f"{int(t):02d}" == suffix:
            return float(t)
    raise RuntimeError(
        f"AGENT_DEFAULT_ARM={AGENT_DEFAULT_ARM!r} 在 ETF_TRANCHES={ETF_TRANCHES} 里"
        f"找不到对应的档位 —— 默认 spec 与静态臂同口径这句话就不成立了")


def _default_stop_loss_pct() -> float:
    """默认止损 = 静态臂现值反推的百分比（精确值，不是 −5.4 那个近似）。"""
    lines = lines_for(HOLD_CODE)
    if lines is None:
        raise RuntimeError(
            f"{HOLD_CODE} 没有配置纪律线 —— 默认 spec 推不出止损百分比；"
            f"先补 `portfolio/discipline.PER_CODE_LINES`")
    return round((float(lines["stop_loss_close"]) / HOLD_COST_PRICE - 1.0) * 100.0, 6)


#: 变更空间白名单。**顺序即 `paper spec show` 的展示顺序**。
SPEC_SCHEMA: dict[str, SpecField] = {
    f.name: f for f in (
        SpecField(
            name="etf_target_pct", kind="number", unit="%",
            default=_default_etf_target_pct(), lo=5.0, hi=25.0,
            doc="分散工具（白名单 ETF）的目标占比。额度在两条腿之间**均分** —— "
                "给某一条腿定更高的权重就是「挑一个更看好」，而本臂不做方向判断。"),
        SpecField(
            name="stop_loss_pct", kind="number_or_off", unit="%（相对成本价）",
            default=_default_stop_loss_pct(), lo=-15.0, hi=-3.0,
            doc="收盘价跌破「成本价 ×(1+pct/100)」→ 整清。可取 'off' 关闭；"
                "关闭只影响判定，不会让风险消失。"),
        SpecField(
            name="max_single_pct", kind="number", unit="%",
            default=DISCIPLINE["single_position_max_pct"], lo=20.0, hi=50.0,
            doc="单票市值上限。超出即减到该上限（整手向下取整；不足 1 手则不动，"
                "并把「违规未消除」如实上报）。"),
        SpecField(
            name="rebalance_cadence", kind="choice", unit="交易日", default=5,
            choices=(3, 5, 10),
            doc="复审节奏：每 N 个交易日才允许产生新的一版 spec（阶段 2 的调度用它）。"
                "非复审日 `paper step` 直接沿用当前 spec。"),
        SpecField(
            name="cash_floor_pct", kind="number", unit="%",
            default=DISCIPLINE["cash_band_pct"][0],
            lo=DISCIPLINE["cash_band_pct"][0], hi=DISCIPLINE["cash_band_pct"][1],
            tighten_only=True,
            doc="现金下限（占总资产 %）。**只许收紧**：下界就是静态臂现值，"
                "理由与 P19 一致 —— 风险规则错的方向必须是**少投而不是多投**。"
                f"上界取静态现金带的上沿 {DISCIPLINE['cash_band_pct'][1]:g}%，"
                "再往上就等于「基本不投」，那是仓位判断，不是风险规则。"),
    )
}


#: 默认 spec = `arm-discipline-10` 口径（见模块 docstring 的表）。
AGENT_DEFAULT_SPEC: dict[str, object] = {
    name: f.default for name, f in SPEC_SCHEMA.items()}


# ---------- 校验 / 序列化 ----------

def _norm_value(field: SpecField, value: object) -> object:
    if field.kind == "number_or_off" and value == SPEC_OFF:
        return SPEC_OFF
    if field.kind == "choice":
        if isinstance(value, bool) or value not in field.choices:
            raise SpecViolation(field.name, value, field.range_text())
        return int(value)                                # type: ignore[arg-type]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecViolation(
            field.name, value,
            f"必须是数字{ '（或 \"off\"）' if field.kind == 'number_or_off' else '' }，"
            f"不接受 {type(value).__name__}；{field.range_text()}")
    v = float(value)
    if field.lo is not None and v < field.lo:
        raise SpecViolation(field.name, value,
                            f"低于下界：{field.range_text()}")
    if field.hi is not None and v > field.hi:
        raise SpecViolation(field.name, value,
                            f"高于上界：{field.range_text()}")
    return v


def validate_spec(spec: Mapping[str, object]) -> dict[str, object]:
    """校验**给到的那部分**字段，返回规范化后的子集（缺的字段不补）。

    未知字段 / 越界 / 类型不符 → `SpecViolation`。**不夹紧、不静默丢弃**。
    """
    if not isinstance(spec, Mapping):
        raise SpecViolation("<spec>", spec, "spec 必须是一个映射（JSON 对象）")
    unknown = sorted(str(k) for k in spec if k not in SPEC_SCHEMA)
    if unknown:
        raise SpecViolation(
            unknown[0], spec[unknown[0]],
            f"未知字段（变更空间只有 {sorted(SPEC_SCHEMA)} 这 5 个；"
            f"改白名单 / 成本口径 / PIT 判据 / 整手口径 / 加方向信号，"
            f"一律在这里被拒）")
    return {name: _norm_value(f, spec[name])
            for name, f in SPEC_SCHEMA.items() if name in spec}


def normalize_spec(spec: Mapping[str, object] | None = None) -> dict[str, object]:
    """补缺失字段（用默认值）→ 一个**完整**的 spec；字段顺序固定。"""
    merged = {**AGENT_DEFAULT_SPEC, **validate_spec(spec or {})}
    return {name: merged[name] for name in SPEC_SCHEMA}


def apply_spec(before: Mapping[str, object] | None,
               patch: Mapping[str, object] | None) -> dict[str, object]:
    """**增量合并**：`patch` 里只写要改的字段。返回完整 spec（已校验）。

    合并的是「给定值」而不是「规范化后的完整 spec」—— 否则一次 `{"etf_target_pct": 12}`
    会把 `before` 里那些**当时合法、现在越界**的字段一起重新校验并报错。
    这里只校验 patch 里出现的字段，语义是「在现状上改这一项」。
    落库前的那一次全量校验在 `record_decision`（写入路径照样 fail-closed，
    所以「只校验 patch」不会让一条越界的 spec 悄悄进台账）。
    """
    base = {**AGENT_DEFAULT_SPEC, **(before or {})}
    merged = {**base, **validate_spec(patch or {})}
    return {name: merged[name] for name in SPEC_SCHEMA}


def canonical_json(spec: Mapping[str, object]) -> str:
    """canonical JSON：`sort_keys` + 无空格 + 不转义中文。同 spec ⇒ 同字节。"""
    return json.dumps(dict(spec), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def spec_sha256(spec: Mapping[str, object]) -> str:
    """spec 的 sha256（对**补齐后的完整** canonical JSON 取）。键序不影响结果。"""
    return hashlib.sha256(canonical_json(normalize_spec(spec)).encode("utf-8")).hexdigest()


# ---------- spec → 执行内核的参数 ----------

def stop_loss_line(spec: Mapping[str, object]) -> float | None:
    """spec 推出的**绝对**止损线；`stop_loss_pct="off"` → `None`。

    相对成本价而不是相对现价：相对现价的止损线会随价格自己往上爬
    （价格跌了线也跌），于是「跌破」永远不发生。
    """
    s = normalize_spec(spec)
    pct = s["stop_loss_pct"]
    if pct == SPEC_OFF:
        return None
    return round(HOLD_COST_PRICE * (1.0 + float(pct) / 100.0), 4)


def spec_tag(spec: Mapping[str, object], *, source_asof: str | None = None) -> str:
    """写进成交 `reason` 的溯源标签：`spec <前12位> @ <台账 asof>`。

    成交行是 append-only 的，所以「这笔单是照哪一版 spec 下的」必须在成交行里
    自己说得清 —— 光有台账和外键关系，读单笔成交的人还得自己 JOIN 才看得见。
    """
    tag = f"spec {spec_sha256(spec)[:12]}"
    if source_asof:
        tag += f" @ {source_asof}"
    return tag


def params_for(spec: Mapping[str, object], *,
               source_asof: str | None = None) -> RuleParams:
    """spec → 执行内核参数。`arm-agent` 与静态臂**共用同一个内核**，只换参数。

    `etf_target_pct` / `stop_loss_*` / `max_single_pct` / `cash_floor_pct` 来自 spec；
    成本口径、整手、白名单**不在 spec 里**（设计 §3.2 明说不是变更空间）——
    它们只能是默认值。
    """
    s = normalize_spec(spec)
    line = stop_loss_line(s)
    return RuleParams(
        etf_target_pct=float(s["etf_target_pct"]),
        single_position_max_pct=float(s["max_single_pct"]),
        cash_floor_pct=float(s["cash_floor_pct"]),
        stop_loss_line=line,
        stop_loss_enabled=line is not None,
        citations=tuple(RULE_CITATIONS_AGENT.items()),
        spec_tag=spec_tag(s, source_asof=source_asof),
    )


# ---------- 台账读写（只增；不提供 UPDATE / DELETE） ----------

TABLE_DECISIONS = "paper_agent_decisions"


class DecisionConflict(Exception):
    """同 `(arm, asof)` 已经有一版了 —— **append-only，不许覆盖**。

    与「幂等」的区别：内容一致 → 调用方应当 exit 0 什么也不写；内容不一致 →
    那是两个不同的决定撞同一个键，必须由人决定怎么处理（再审一次、换日期，或接受
    现有那版）。这里只负责把已有那一行带出来给调用方比对。
    """

    def __init__(self, arm: str, asof: str, existing: dict) -> None:
        self.arm = arm
        self.asof = asof
        self.existing = existing
        super().__init__(f"{arm} 在 {asof} 已有一版 spec（decision_id="
                         f"{existing.get('decision_id')}）—— append-only，不许覆盖")


def _row_to_decision(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["spec_before"] = json.loads(d.pop("spec_before_json"))
    d["spec_after"] = json.loads(d.pop("spec_after_json"))
    d["rejected"] = json.loads(d.pop("rejected_json"))
    return d


def latest_decision(conn: sqlite3.Connection, arm: str,
                    asof: str) -> dict | None:
    """`arm` 在 `asof`（含）之前的**最近一版**决定行；没有 → `None`。

    `<= asof` 而不是 `== asof`：非复审日的 `paper step` 要沿用上一版，
    这不是「找不到就当默认」的兜底，而是「复审节奏天然稀疏」的定义。
    """
    row = conn.execute(
        f"SELECT * FROM {TABLE_DECISIONS} WHERE arm = ? AND asof <= ?"
        " ORDER BY asof DESC, decision_id DESC LIMIT 1", (arm, asof)).fetchone()
    return None if row is None else _row_to_decision(row)


def decision_on(conn: sqlite3.Connection, arm: str, asof: str) -> dict | None:
    """`(arm, asof)` 那一行（幂等判据要的是精确同一格，不是「最近一版」）。"""
    row = conn.execute(
        f"SELECT * FROM {TABLE_DECISIONS} WHERE arm = ? AND asof = ?",
        (arm, asof)).fetchone()
    return None if row is None else _row_to_decision(row)


def current_spec(conn: sqlite3.Connection, arm: str,
                 asof: str) -> dict[str, object]:
    """当前有效 spec = 台账里 `asof` 之前最近一版的 `spec_after`。

    用 `<= asof`（设计 §3.1）。注意 cron 的顺序是「先 `paper step` 再复审」⇒
    某天 T 复审出来的 spec，实际被执行内核用上是从 T+1 开始 —— 这不是 bug，
    而是「复审在收盘后做」的直接推论；手写时若想让它当天生效，
    就得在跑 `paper step --asof T` **之前**先 `paper spec set --asof T`。

    台账里一行都没有 → `AGENT_DEFAULT_SPEC`（= `arm-discipline-10` 口径），
    这样 `arm-agent` 从起跑日就有一组**写下来的**条文可跑，而不是「空规则」。
    """
    det = latest_decision(conn, arm, asof)
    if det is None:
        return dict(AGENT_DEFAULT_SPEC)
    return normalize_spec(det["spec_after"])


def spec_before(conn: sqlite3.Connection, arm: str,
                asof: str) -> dict[str, object]:
    """**严格早于** `asof` 的最近一版 spec（= 本次复审前的现状）。

    与 `current_spec` 的区别是 `<` 而不是 `<=`：`current_spec(asof)` 在同一天已有
    台账行时会返回**那一行**，而写下它的人需要的恰恰是「我改之前是什么」。
    """
    row = conn.execute(
        f"SELECT spec_after_json FROM {TABLE_DECISIONS} WHERE arm = ? AND asof < ?"
        " ORDER BY asof DESC, decision_id DESC LIMIT 1", (arm, asof)).fetchone()
    if row is None:
        return dict(AGENT_DEFAULT_SPEC)
    return normalize_spec(json.loads(row["spec_after_json"]))


def reproducibility(conn: sqlite3.Connection, arm: str) -> dict:
    """复现性判据（设计 §5.1）：同 `(context_sha256, model_id, prompt_sha256, seed)`
    必须得到同一个 spec。

    做法是**事后比对**而不是事前断言：把台账里那些四元组相同的行挑出来，
    看它们的 `spec_after` 是否一致。

    - `testing = false`：还没有任何一组四元组重复出现 ⇒ **无法判定**，
      而不是「可复现」。把这个区别写进返回值的 `verdict`，免得读成绿灯。
    - 不一致的行会进 `violations`，报告里**单列**（设计 §5.1：不许静默平均）。
    """
    groups: dict[tuple, list[dict]] = {}
    for d in load_decisions(conn, arm):
        key = (str(d["context_sha256"]), str(d["model_id"]),
               str(d["prompt_sha256"]), int(d["seed"]))
        groups.setdefault(key, []).append(d)
    tested = {k: v for k, v in groups.items() if len(v) > 1}
    violations = []
    for key, rows in sorted(tested.items()):
        specs = {canonical_json(r["spec_after"]) for r in rows}
        if len(specs) > 1:
            violations.append({
                "key": {"context_sha256": key[0], "model_id": key[1],
                        "prompt_sha256": key[2], "seed": key[3]},
                "asof": [str(r["asof"]) for r in rows],
                "n_distinct_specs": len(specs),
            })
    return {
        "arm": arm,
        "n_rows": sum(len(v) for v in groups.values()),
        "n_groups_tested": len(tested),
        "reproducible": (None if not tested else not violations),
        "verdict": ("无法判定（还没有一组 (context, model, prompt, seed) 重复出现过）"
                    if not tested else
                    ("可复现" if not violations else "**不可复现**，已单列")),
        "violations": violations,
    }


def load_decisions(conn: sqlite3.Connection, arm: str) -> list[dict]:
    """该臂的全部决定行（append-only 台账，按 asof）。"""
    return [_row_to_decision(r) for r in conn.execute(
        f"SELECT * FROM {TABLE_DECISIONS} WHERE arm = ?"
        " ORDER BY asof, decision_id", (arm,))]


def ledger_summary(conn: sqlite3.Connection, arm: str,
                   asof: str) -> dict[str, object]:
    """台账统计（`<= asof`）：复审次数 / 累计试错 / 被拒次数 / 试错预算状态。

    `n_trials_total` 与 `n_rejected` 是报告里**必须出现**的两个数：不报试错次数的
    `arm-agent` 读数一律不作数（设计 §11）—— 试了很多版选最好那版，那是上界不是期望。
    """
    rows = [r for r in load_decisions(conn, arm) if str(r["asof"]) <= asof]
    last = rows[-1] if rows else None
    return {
        "arm": arm,
        "asof": asof,
        "n_reviews": len(rows),
        "n_trials_total": sum(int(r["n_trials"]) for r in rows),
        "n_rejected": sum(len(r["rejected"]) for r in rows),
        "max_trials_per_review": MAX_TRIALS_PER_REVIEW,
        "n_trials_last_review": int(last["n_trials"]) if last else 0,
        "cadence": int(current_spec(conn, arm, asof)["rebalance_cadence"]),
        "first_asof": str(rows[0]["asof"]) if rows else None,
        "last_asof": str(last["asof"]) if last else None,
        "last_decision_id": int(last["decision_id"]) if last else None,
    }


def validate_trial_budget(n_trials: int, rejected: list) -> None:
    """试错预算（D-17「每次 ≤3 版」）与「被拒条目不许比试错次数还多」。

    这两条是防「智能体给自己扩预算」的形状：预算一旦不设防，`n_trials` 就不再是
    一个可比的量，第三条随机臂也就失去了「同预算」这个前提。
    """
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
        raise SpecViolation("n_trials", n_trials, "必须是 ≥1 的整数")
    if n_trials > MAX_TRIALS_PER_REVIEW:
        raise SpecViolation(
            "n_trials", n_trials,
            f"超出每次复审的试错预算 {MAX_TRIALS_PER_REVIEW}（D-17）—— "
            f"预算不是智能体可以自己改的东西")
    if len(rejected) > n_trials - 1:
        raise SpecViolation(
            "rejected", len(rejected),
            f"被拒的试错有 {len(rejected)} 条，但本次只试了 {n_trials} 版"
            f"（至少有一版被采纳）—— 计数对不上就不许落库")


def record_decision(conn: sqlite3.Connection, *, arm: str, asof: str,
                    spec_before: Mapping[str, object],
                    spec_after: Mapping[str, object],
                    agent_kind: str, model_id: str, prompt_sha256: str,
                    seed: int, context_sha256: str, now: str,
                    n_trials: int = 1, rejected: list | None = None,
                    rationale: str = "", commit: bool = True) -> int:
    """落一行决定（**只增**）。同 `(arm, asof)` 已有行 → `DecisionConflict`。

    先按 `(arm, asof)` **精确查一行**再写（ERROR_DIARY #25 的顺序）：
    先写再让唯一键兜底，会把「自己刚写下的那一行」当成重复。
    """
    spec_before = normalize_spec(spec_before)
    spec_after = normalize_spec(spec_after)
    if agent_kind not in (AGENT_KIND_MANUAL, AGENT_KIND_LLM, AGENT_KIND_RANDOM):
        raise SpecViolation("agent_kind", agent_kind,
                            f"只能是 {AGENT_KIND_MANUAL}/{AGENT_KIND_LLM}/"
                            f"{AGENT_KIND_RANDOM} 之一")
    if not model_id:
        raise SpecViolation("model_id", model_id, "不许留空（换模型 = 换口径，必须留痕）")
    if not prompt_sha256:
        raise SpecViolation("prompt_sha256", prompt_sha256,
                            "不许留空（换提示词/温度 = 换口径，必须留痕）")
    if not context_sha256:
        raise SpecViolation("context_sha256", context_sha256,
                            "不许留空（喂进去的 PIT 快照必须可复算）")
    rejected = list(rejected or [])
    validate_trial_budget(n_trials, rejected)

    existing = decision_on(conn, arm, asof)
    if existing is not None:
        raise DecisionConflict(arm, asof, existing)
    cur = conn.execute(
        f"INSERT INTO {TABLE_DECISIONS} (arm, asof, agent_kind, model_id,"
        " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
        " n_trials, rejected_json, rationale, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (arm, asof, agent_kind, model_id, prompt_sha256, int(seed), context_sha256,
         canonical_json(spec_before), canonical_json(spec_after), int(n_trials),
         json.dumps(rejected, ensure_ascii=False, sort_keys=True),
         rationale, now),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid)
