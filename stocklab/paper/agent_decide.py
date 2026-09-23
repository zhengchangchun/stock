"""AI 操盘手（P52 / D-34）：**决策载荷**的校验、落库、执行。

## 决策在项目外产生，项目内只做四件事

`校验 → 落库 → 执行 → 计账`。载荷由外部编码 agent（Claude Code + 人复核）写成
JSON，经 `paper agent decide --asof <day> --file <f>` 送进来。理由（任务书 §1）：

1. 与 D-1「系统内不实现自动生成」同精神；
2. 项目保持**离线可测、零 API key**（`paper/` 不引入网络依赖）；
3. 台账里的 `model_id / prompt_sha256 / context_sha256 / seed` 足以复现与追责。

## 决策空间（D-34 / 覆盖 D-18）

```
{asof, decisions: [{code, side, target_weight_pct, reason}], cash_pct, rationale}
```

- `code` **必须在当日候选池**（短/中/长并集，`candidate` 快照）—— 池外即拒；
- `target_weight_pct ∈ [0, 100]`，`Σ target_weight_pct + cash_pct = 100`
  —— **不许杠杆、不许负权重**；
- **A 股无做空**：「看空」只能表达为**降低总仓位 / 清仓**。载荷里 `side` 是
  **意图的声明**，必须与「目标市值 vs 现市值」的方向一致；对未持有的标的写
  `side="sell"` 会被拒，并**原样**告诉调用方「那是融券」（`NO_SHORT_SIDE_MSG`）——
  否则「AI 想卖空」会被读成一个静默失败。

## 越界即拒，不夹紧、不静默

任何一条不满足 → `DecisionPayloadError`，**该日不下单**：账户与净值一个字节不变
（`paper agent decide` 在写库前校验，`paper step` 只在台账里读到合法载荷时才执行）。
`reject` 的理由必须点名**字段、值、为什么** —— 一句「参数不合法」等于没有信息。

## 与 P37 的 spec 路径的关系

P37 的 5 字段白名单**不再约束** AI 臂的决策（D-34 覆盖 D-18）：`arm-agent` 的执行
内核从「spec 参数化的纪律条文」换成「台账里当日那一条目标权重」。`paper spec set`
与其台账**保留**（阶段 1 的历史行不删，它是 A1/A2/A3 插桩的策略实现落点），
但它不再是 `arm-agent` 下不下单的依据。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
from dataclasses import replace
from typing import Iterable, Mapping, Sequence

from stocklab.config.costs import ASSET_ETF, ASSET_STOCK, CostModel
from stocklab.paper import agent_pool, agent_spec
from stocklab.paper.config import (
    CONTEXT_SHA256_KEY,
    DECISION_AGENT_KIND,
    DECISION_ITEM_KEYS,
    DECISION_KIND_PORTFOLIO,
    DECISION_PAYLOAD_KEYS,
    NO_SHORT_SIDE_MSG,
    RANDOM_MODEL_ID,
    RANDOM_N_CODES,
    RULE_CITATIONS_AGENT_DECISION,
    WEIGHT_SUM_TOLERANCE,
)
from stocklab.paper.rules import C_TARGET, Decision, RuleParams, plan_target_weight
from stocklab.portfolio.prices import Price

TABLE_DECISIONS = agent_spec.TABLE_DECISIONS

SIDE_BUY = "buy"
SIDE_SELL = "sell"
SIDES: tuple[str, ...] = (SIDE_BUY, SIDE_SELL)


class DecisionPayloadError(Exception):
    """载荷越界 / 缺字段 / 与持仓矛盾。**拒绝，不夹紧、不静默**。

    `field` / `value` / `reason` 三个属性是为了可断言、可渲染 ——
    `paper agent decide` 把它们原样打到 stderr，调用方一眼能看出改哪里。
    """

    def __init__(self, field: str, value: object, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"{field}={value!r} —— {reason}")

    def as_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "reason": self.reason}


def _num(value: object) -> float | None:
    """数字（`bool` 不算 —— `True` 是个整数，但把它当权重是错的）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def side_for(*, target_value: float, current_value: float) -> str | None:
    """目标市值相对现市值的**隐含方向**；两者相等 → `None`（不动）。"""
    if target_value > current_value:
        return SIDE_BUY
    if target_value < current_value:
        return SIDE_SELL
    return None


# ---------- 校验 ----------

def validate_payload(*, asof: str, payload: object, pool_codes: set[str],
                     held_qty: Mapping[str, int], marks: Mapping[str, Price],
                     total_assets: float,
                     expected_context_sha256: str | None = None) -> dict:
    """校验 + 规范化一条决策载荷。**纯函数**：不写库、不读时钟、不取价。

    `marks` 只用于「目标市值 vs 现市值」的方向判定与目标市值的落地；
    缺价的标的**不许出现在载荷里**（否则那条目标权重没法执行，
    静默跳过等于少投而不报）。

    `expected_context_sha256` 给了就启用 **D-49 的 PIT 闸门**：载荷必须回传
    `context_sha256` 且与调用方**重算**出来的值逐字符相同，否则拒。
    不给（`None`）= 不启闸 —— 那是纯函数调用方（单元测试）的用法；
    CLI 的两个写入口**总是**传真值，所以真实路径上没有「不启闸」这个状态。
    """
    if not isinstance(payload, Mapping):
        raise DecisionPayloadError("<payload>", payload, "决策载荷必须是一个 JSON 对象")
    unknown = sorted(str(k) for k in payload if k not in DECISION_PAYLOAD_KEYS)
    if unknown:
        raise DecisionPayloadError(
            unknown[0], payload[unknown[0]],
            f"未知字段（载荷只有 {list(DECISION_PAYLOAD_KEYS)} 这几个；"
            f"杠杆/成本口径/PIT 判据/整手口径 都不在载荷里 —— "
            f"「AI 给自己发权限」在这里被拒）")
    got_asof = payload.get("asof")
    if got_asof != asof:
        raise DecisionPayloadError(
            "asof", got_asof,
            f"载荷里的 asof 必须等于本命令的 --asof（{asof}）—— "
            f"否则一条决策会落在它没看过的那个交易日上")
    context_sha256 = payload.get(CONTEXT_SHA256_KEY)
    if context_sha256 is not None and not (
            isinstance(context_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", context_sha256)):
        raise DecisionPayloadError(
            CONTEXT_SHA256_KEY, context_sha256,
            "必须是 64 位小写十六进制（直接从 `paper agent context` 的输出里抄，"
            "不要自己拼）")
    if expected_context_sha256 is not None:
        if context_sha256 is None:
            raise DecisionPayloadError(
                CONTEXT_SHA256_KEY, None,
                "**载荷必须回传 `context_sha256`**（D-49）：它证明这条决策是看着"
                "`paper agent context --asof` 的输出做的。取法：先跑 "
                "`paper agent context --asof <同一日> --arm <臂>`，把输出里的 "
                "`context_sha256` 原样放进载荷。缺它 ⇒ 拒（零写入）")
        if context_sha256 != expected_context_sha256:
            raise DecisionPayloadError(
                CONTEXT_SHA256_KEY, context_sha256,
                f"与 `--asof` 当日的上下文指纹不符（库里重算是 "
                f"{expected_context_sha256}）—— 说明这条载荷不是照着这一天的 "
                f"PIT 上下文做的（写错日子 / 池子或持仓已经变了）。**拒绝，零写入**")
    rationale = payload.get("rationale", "")
    if not isinstance(rationale, str):
        raise DecisionPayloadError("rationale", rationale, "必须是字符串（可以是空串）")
    cash_pct = _num(payload.get("cash_pct"))
    if cash_pct is None or not (0.0 <= cash_pct <= 100.0):
        raise DecisionPayloadError(
            "cash_pct", payload.get("cash_pct"),
            "必须是 0~100 的数字（负现金 = 借钱，>100 与「Σ权重 = 100 − 现金」矛盾）")
    raw_items = payload.get("decisions")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise DecisionPayloadError("decisions", raw_items, "必须是数组（可以是空数组 = 全现金）")

    decisions: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_items):
        decisions.append(_validate_item(
            i, item, asof=asof, pool_codes=pool_codes, held_qty=held_qty,
            marks=marks, total_assets=total_assets, seen=seen))
    weight_sum = round(sum(d["target_weight_pct"] for d in decisions), 6)
    if abs(weight_sum + cash_pct - 100.0) > WEIGHT_SUM_TOLERANCE:
        raise DecisionPayloadError(
            "target_weight_pct",
            weight_sum,
            f"Σ target_weight_pct ({weight_sum:g}) + cash_pct ({cash_pct:g}) = "
            f"{weight_sum + cash_pct:g} ≠ 100 —— 多出来的部分就是杠杆（借钱买入），"
            f"少掉的部分无法解释。两边都**不许**，也不夹紧")
    return {
        "asof": asof,
        "cash_pct": cash_pct,
        "cash_value": round(total_assets * cash_pct / 100.0, 4),
        "rationale": rationale,
        "sum_weight_pct": weight_sum,
        "total_assets": round(total_assets, 4),
        # 载荷里回传的 PIT 上下文指纹随载荷一起进 canonical JSON ⇒ 它是**载荷指纹
        # 的一部分**：同一天用两份不同的上下文各做一次决策，指纹不同、不是「重放」。
        "context_sha256": context_sha256,
        "decisions": decisions,
    }


def _validate_item(i: int, item: object, *, asof: str, pool_codes: set[str],
                   held_qty: Mapping[str, int], marks: Mapping[str, Price],
                   total_assets: float, seen: set[str]) -> dict:
    where = f"decisions[{i}]"
    if not isinstance(item, Mapping):
        raise DecisionPayloadError(where, item, "每一笔决策必须是一个 JSON 对象")
    unknown = sorted(str(k) for k in item if k not in DECISION_ITEM_KEYS)
    if unknown:
        raise DecisionPayloadError(
            f"{where}.{unknown[0]}", item[unknown[0]],
            f"未知字段（每笔只有 {list(DECISION_ITEM_KEYS)}；"
            f"加字段 = 扩权，一律在这里被拒）")
    code = item.get("code")
    if not isinstance(code, str) or not code:
        raise DecisionPayloadError(f"{where}.code", code, "必须是非空字符串")
    if code in seen:
        raise DecisionPayloadError(f"{where}.code", code,
                                   "同一标的在同一天出现了两次 —— 两个目标权重会互相矛盾")
    if code not in pool_codes:
        raise DecisionPayloadError(
            f"{where}.code", code,
            f"**池外标的**：{asof} 的候选池（短/中/长并集，共 {len(pool_codes)} 只）里"
            f"没有它。可投集合只有候选池 —— 池外一律拒绝（不静默忽略这一笔）")
    if code not in marks:
        raise DecisionPayloadError(
            f"{where}.code", code,
            f"{asof} 及之前取不到 {code} 的收盘价 → 目标权重无法落地；"
            f"不许用别的标的价格顶替，也不许静默跳过")
    side = item.get("side")
    if side not in SIDES:
        raise DecisionPayloadError(f"{where}.side", side,
                                   f"只能是 {list(SIDES)} 之一（A 股无做空，见护栏）")
    weight = _num(item.get("target_weight_pct"))
    if weight is None:
        raise DecisionPayloadError(f"{where}.target_weight_pct",
                                   item.get("target_weight_pct"), "必须是数字")
    if weight < 0:
        raise DecisionPayloadError(
            f"{where}.target_weight_pct", weight,
            "**负权重**：负权重 = 做空，A 股没有这条腿。" + NO_SHORT_SIDE_MSG)
    if weight > 100:
        raise DecisionPayloadError(
            f"{where}.target_weight_pct", weight,
            ">100% 就是杠杆（借钱买入），不许 —— 单标的上限也是 100")
    reason = item.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise DecisionPayloadError(
            f"{where}.reason", reason,
            "**缺理由**：台账是 append-only 的，事后读这一行的人只能靠 reason "
            "知道当时为什么这么定；留空的行等于不可审计，直接拒绝")
    qty = int(held_qty.get(code, 0) or 0)
    price = float(marks[code].price)
    current_value = round(price * qty, 4)
    target_value = round(total_assets * weight / 100.0, 4)
    implied = side_for(target_value=target_value, current_value=current_value)
    if side == SIDE_SELL and qty <= 0:
        raise DecisionPayloadError(f"{where}.side", side, NO_SHORT_SIDE_MSG)
    if implied is not None and side != implied:
        verb = "买入" if implied == SIDE_BUY else "卖出"
        raise DecisionPayloadError(
            f"{where}.side", side,
            f"`side` 与目标权重自相矛盾：目标市值 ¥{target_value:,.2f} 相对现市值 "
            f"¥{current_value:,.2f}（{qty} 股 × {price:.4f}）是**{verb}**，"
            f"但 side 写的是 {side!r}。两个说法只能留一个")
    seen.add(code)
    return {
        "code": code, "side": side, "target_weight_pct": weight,
        "reason": reason, "current_qty": qty, "price": price,
        "price_asof": str(marks[code].price_asof), "price_source": str(marks[code].source),
        "current_value": current_value, "target_value": target_value,
        "implied_side": implied,
    }


# ---------- 指纹 ----------

def canonical_payload(payload: Mapping[str, object]) -> str:
    """canonical JSON：`sort_keys` + 无空格 + 不转义中文。同载荷 ⇒ 同字节。"""
    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def payload_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def payload_tag(payload: Mapping[str, object]) -> str:
    """写进成交 `reason` 的溯源标签（成交行 append-only，必须自己说得清出处）。"""
    return f"decision {payload_sha256(payload)[:12]}"


# ---------- 随机对照臂（D-19 的精神：没有它，AI 的读数一律不可归因） ----------

def _seed_material(arm: str, asof: str, seed: int) -> int:
    blob = f"{arm}|{asof}|{int(seed)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(blob).digest(), "big")


def random_payload(*, arm: str, asof: str, pool_codes: set[str],
                   held_qty: Mapping[str, int], marks: Mapping[str, Price],
                   total_assets: float, seed: int) -> dict:
    """随机对照臂的载荷：**同护栏、同成本**，只是标的与权重随机抽。

    随机性是**种子驱动的**（`(arm, asof, seed)` 的 sha256），因此：
    - 同一天重跑逐字节一致 → 幂等成立、台账可复现；
    - 「随机」这件事本身也在台账里（`model_id = random-control`），
      不是「看起来像 AI 的输出」。

    抽到的标的若在 `asof` 取不到价会被跳过（它进不了载荷）—— 这不是静默：
    跳过的标的数会写进 `rationale`。
    """
    rng = random.Random(_seed_material(arm, asof, seed))
    usable = sorted(c for c in pool_codes if c in marks)
    if not usable:
        return {"asof": asof, "decisions": [], "cash_pct": 100.0,
                "rationale": f"随机对照臂：{asof} 没有可定价的池内标的 → 全现金"}
    lo, hi = RANDOM_N_CODES
    k = max(1, min(len(usable), rng.randint(lo, hi)))
    picks = sorted(rng.sample(usable, k))
    exposure = round(rng.uniform(0.0, 100.0), 2)
    raw = [rng.random() for _ in picks]
    total = sum(raw)
    weights = [round(exposure * w / total, 2) for w in raw] if total else \
        [0.0 for _ in picks]
    drift = round(exposure - sum(weights), 2)
    weights[-1] = round(weights[-1] + drift, 2)
    items: list[dict] = []
    for code, weight in zip(picks, weights):
        if weight <= 0:
            continue                      # 抽到 0 权重 = 不投它，下落成空仓
        qty = int(held_qty.get(code, 0) or 0)
        price = float(marks[code].price)
        target_value = round(total_assets * weight / 100.0, 4)
        side = side_for(target_value=target_value,
                        current_value=round(price * qty, 4)) or SIDE_BUY
        items.append({
            "code": code, "side": side, "target_weight_pct": weight,
            "reason": (f"随机对照臂：种子 ({arm}, {asof}, {seed}) 抽中 {code}，"
                       f"目标 {weight:g}%；**不构成任何判断**，"
                       f"取值仅为「同预算同护栏下的运气」提供落点"),
        })
    cash = round(100.0 - sum(d["target_weight_pct"] for d in items), 2)
    return {
        "asof": asof, "decisions": items, "cash_pct": cash,
        "rationale": (f"随机对照臂（model_id={RANDOM_MODEL_ID}）：从 {len(usable)} 只"
                      f"可定价池内标的里随机抽 {len(items)} 只，总敞口 {exposure:g}%，"
                      f"种子固定以便逐字节复现。它没有任何依据可自述 —— "
                      f"正因如此，AI 臂跑赢它才算「选对了」而不是「多试了几次」"),
    }


def random_prompt_sha256() -> str:
    """`prompt_sha256` 必填（换提示词 = 换口径）。随机臂没有提示词，
    这里如实写「无提示词」的指纹，而不是留空。"""
    return hashlib.sha256(b"random-control|no-prompt").hexdigest()


# ---------- 落库（只增） ----------

def _row_to_decision(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    for src, dst in (("spec_before_json", "spec_before"),
                     ("spec_after_json", "spec_after")):
        if src in d:
            d[dst] = json.loads(d.pop(src) or "{}")
    if "rejected_json" in d:
        d["rejected"] = json.loads(d.pop("rejected_json") or "[]")
    d["payload"] = json.loads(d.get("payload_json") or "{}")
    d["pool"] = json.loads(d.get("pool_json") or "{}")
    return d


def record_portfolio_decision(conn: sqlite3.Connection, *, arm: str, asof: str,
                              payload: Mapping[str, object], pool: Mapping[str, object],
                              agent_kind: str, model_id: str, prompt_sha256: str,
                              seed: int, context_sha256: str, now: str,
                              commit: bool = True) -> int:
    """落一行**操盘决策**（只增）。同 `(arm, asof)` 已有行 → `DecisionConflict`。

    先按 `(arm, asof)` **精确查一行**再写（ERROR_DIARY #25 的顺序）：
    先写再让唯一键兜底，会把「自己刚写下的那一行」当成重复。

    `spec_before_json` / `spec_after_json` 写 `{}`：这两列属于 P37 的 spec 口径，
    操盘决策不产生 spec。**不给它们编一个值**（那会让 `paper spec show` 读到
    一版根本没人提过的 spec）。
    """
    if agent_kind not in (agent_spec.AGENT_KIND_MANUAL, agent_spec.AGENT_KIND_LLM,
                          agent_spec.AGENT_KIND_RANDOM):
        raise agent_spec.SpecViolation(
            "agent_kind", agent_kind,
            f"只能是 {agent_spec.AGENT_KIND_MANUAL}/{agent_spec.AGENT_KIND_LLM}/"
            f"{agent_spec.AGENT_KIND_RANDOM} 之一")
    for field, value in (("model_id", model_id), ("prompt_sha256", prompt_sha256),
                         ("context_sha256", context_sha256)):
        if not value:
            raise agent_spec.SpecViolation(
                field, value,
                "不许留空（换模型/换提示词/换上下文 = 换口径，必须留痕）")
    existing = decision_on(conn, arm, asof)
    if existing is not None:
        raise agent_spec.DecisionConflict(arm, asof, existing)
    cur = conn.execute(
        f"INSERT INTO {TABLE_DECISIONS} (arm, asof, agent_kind, model_id,"
        " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
        " n_trials, rejected_json, rationale, decision_kind, payload_json,"
        " pool_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (arm, asof, agent_kind, model_id, prompt_sha256, int(seed), context_sha256,
         "{}", "{}", 1, "[]", str(payload.get("rationale") or ""),
         DECISION_KIND_PORTFOLIO, canonical_payload(payload),
         json.dumps(dict(pool), ensure_ascii=False, sort_keys=True), now),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def decision_on(conn: sqlite3.Connection, arm: str, asof: str) -> dict | None:
    """`(arm, asof)` 那一行（幂等判据要的是**精确同一格**，不是「最近一版」）。"""
    row = conn.execute(
        f"SELECT * FROM {TABLE_DECISIONS} WHERE arm = ? AND asof = ?",
        (arm, asof)).fetchone()
    return None if row is None else _row_to_decision(row)


def portfolio_decision_on(conn: sqlite3.Connection, arm: str,
                         asof: str) -> dict | None:
    """`(arm, asof)` 那一条**操盘决策**；是 spec 行或没有行 → `None`。

    为什么必须按 `decision_kind` 过滤：同一张台账里躺着两段历史（P37 的 spec 复审
    与 P52 的操盘决策），而只有后者带 `payload_json`。把 spec 行当成操盘决策读，
    会拿到一个 `{}` 载荷 —— 那会变成「这天不下单」（看起来像正常的空仓决定），
    而不是报错。**静默**的错比报错难查得多。
    """
    d = decision_on(conn, arm, asof)
    if d is None or str(d.get("decision_kind")) != DECISION_KIND_PORTFOLIO:
        return None
    return d


def load_decisions(conn: sqlite3.Connection, arm: str) -> list[dict]:
    """该臂的**全部**决定行（spec 与操盘两类都在里面，按 asof）。

    两类都留着是刻意的：`arm-agent` 在 P37 下是「改纪律数字」，在 P52 下是
    「当操盘手」—— 台账要能同时读出这两段历史，不能因为口径换了就把前面那段
    当成不存在。
    """
    return [_row_to_decision(r) for r in conn.execute(
        f"SELECT * FROM {TABLE_DECISIONS} WHERE arm = ?"
        " ORDER BY asof, decision_id", (arm,))]


def portfolio_decisions_only(rows: Iterable[dict]) -> list[dict]:
    return [r for r in rows if str(r.get("decision_kind")) == DECISION_KIND_PORTFOLIO]


# ---------- 执行 ----------

def params_for_agent_decision() -> RuleParams:
    """AI 臂执行期的条文参数：成本/整手/白名单只能是默认值，条文表换成操盘那张。"""
    return RuleParams(citations=tuple(RULE_CITATIONS_AGENT_DECISION.items()))


def rebase_payload(payload: Mapping[str, object], *, total_assets: float,
                   marks: Mapping[str, Price], positions: Mapping[str, int]) -> dict:
    """按**执行时**的总资产与持仓把目标市值重算一遍（载荷本身一字不改）。

    为什么要重算：载荷里的 `target_weight_pct` 是**占比**，而「占比 × 总资产」
    这个乘法在写载荷时与执行时各做一次。两次的输入若不同（例如有人在两天之间
    手动写了别的成交），沿用旧的目标市值就会让「¥ 与 % 对不上」，
    而这种不一致是静默的。方向也在这里复核：目标与现市值的方向若与 `side`
    矛盾 → 直接报错（事务回滚，该日不下单）。
    """
    out = dict(payload)
    items = []
    for item in list(payload.get("decisions") or []):
        code = str(item["code"])
        if code not in marks:
            raise DecisionPayloadError(
                "code", code, f"执行时取不到 {code} 的收盘价 → 目标权重无法落地")
        qty = int(positions.get(code, 0) or 0)
        price = float(marks[code].price)
        current_value = round(price * qty, 4)
        target_value = round(total_assets * float(item["target_weight_pct"]) / 100.0, 4)
        implied = side_for(target_value=target_value, current_value=current_value)
        if implied is not None and str(item["side"]) != implied:
            raise DecisionPayloadError(
                f"{item['code']}.side", item["side"],
                f"执行时目标市值 ¥{target_value:,.2f} 与现市值 ¥{current_value:,.2f} "
                f"的方向与载荷里的 `side` 矛盾（台账不该被执行环境改口径）")
        items.append({**item, "current_qty": qty, "price": price,
                      "price_asof": str(marks[code].price_asof),
                      "price_source": str(marks[code].source),
                      "current_value": current_value, "target_value": target_value,
                      "implied_side": implied})
    out["decisions"] = items
    out["total_assets"] = round(total_assets, 4)
    out["cash_pct"] = float(payload.get("cash_pct") or 0.0)
    return out


def audit_decisions(conn: sqlite3.Connection, *, arms: Sequence[str]) -> dict:
    """自审计（P52 护栏）：**`arm-agent*` 的每一笔成交都要有一条决策行兜着**。

    判据是逐 `(arm, date)` 比对：成交表里有、台账里没有 ⇒ **判红**。
    这条防的是「编辑代执行」——即某天有人绕过写入口直接给 AI 臂下单，
    或某次重构悄悄把执行路径接回纪律条文（那条路不写台账）。
    缺行的成交在读数上与「AI 的决定」 indistinguishable，而它其实不是。

    返回里带上 `checked`（比对了几个 `(arm, date)`）—— 「零违规」与「零比对」
    必须能分开，后者是**没检查**，不是**检查通过**。
    """
    pairs = [tuple(r) for r in conn.execute(
        "SELECT DISTINCT account_id, date FROM paper_trades WHERE account_id IN"
        f" ({','.join('?' * len(arms))}) ORDER BY account_id, date", tuple(arms))]
    violations = []
    for arm, date in pairs:
        if portfolio_decision_on(conn, str(arm), str(date)) is None:
            n = int(conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE account_id = ? AND date = ?",
                (arm, date)).fetchone()[0])
            violations.append({"arm": str(arm), "asof": str(date), "n_trades": n,
                               "reason": "该日有成交，但台账里没有对应的操盘决策行"})
    return {
        "arms": list(arms),
        "checked": len(pairs),
        "violations": violations,
        "ok": not violations,
        "note": ("零违规与零比对是两件事：`checked` 为 0 时说明**没有比对过**，"
                 "不是「检查通过」" if not pairs else
                 f"逐 (arm, date) 比对 {len(pairs)} 组，全部有台账兜底"),
    }


def asset_class_for(conn: sqlite3.Connection, code: str) -> str:
    """标的口径。`instruments.type` 是唯一真相；**未登记即报错**（不默认成股票）。"""
    row = conn.execute("SELECT type FROM instruments WHERE code = ?",
                       (code,)).fetchone()
    kind = None if row is None else str(row["type"])
    if kind not in (ASSET_STOCK, ASSET_ETF):
        raise DecisionPayloadError(
            "code", code,
            f"标的口径未登记（`instruments.type` = {kind!r}）—— "
            f"口径未知时不得退化成股票费率；先 `ingest instruments`")
    return kind


def plan_orders(*, decision: Mapping[str, object], cash: float,
                positions: Mapping[str, int], marks: Mapping[str, Price],
                total_assets: float, asset_classes: Mapping[str, str],
                params: RuleParams | None = None) -> tuple[list[Decision], list[Decision]]:
    """把一条**已校验**的决策载荷翻译成订单（纯函数）。

    顺序：**先卖后买**。卖是为买腾现金 —— 反过来的话，一笔「换仓」会因为
    「先买时现金不够」而买不到目标权重，而那不是 AI 的决定，是我们排的顺序。

    返回 `(orders, evaluations)`：`evaluations` 含「不动的理由」，
    与静态臂的 `evaluate` 同一个形状，报告里可以并排读。

    **纯**：订单的股数/金额全部在 `planned` 那一趟里按**传入的** `cash` /
    `positions` 算完，第二趟只做分类与排序 ⇒ 这里**不碰**调用方的状态。
    （P62 之前它偷偷原地改 `positions`、而 `cash` 是局部 float 改不回去，
    于是调用方拿到「现金是旧的、持仓是新的」混合状态 —— 见
    `execute_decision` 的 docstring。结算已收归那一个函数。）
    """
    p = params or params_for_agent_decision()
    orders: list[Decision] = []
    evals: list[Decision] = []
    items = list(decision["decisions"])
    planned: list[tuple[dict, Decision]] = []
    for item in items:
        d = plan_target_weight(
            code=str(item["code"]), side=str(item["side"]),
            target_value=float(item["target_value"]),
            price=float(item["price"]), qty_held=int(positions.get(str(item["code"]), 0)),
            cash=cash, total_assets=total_assets,
            asset_class=asset_classes[str(item["code"])], params=p)
        # 价格出处由**引擎层**盖章（规则层不认识数据源）—— 与静态臂的 `_finalize`
        # 同一条纪律：成交行 append-only，「这个价是哪来的」是事后审计的第一问。
        d = replace(d, price_source=str(item.get("price_source") or ""),
                    price_asof=str(item.get("price_asof") or ""))
        planned.append((item, d))
    for want in (SIDE_SELL, SIDE_BUY):
        for item, d in planned:
            if d.action == "hold":
                evals.append(d)
                continue
            if str(item["side"]) != want:
                continue
            orders.append(d)
    return orders, evals


def _settle(cash: float, positions: dict[str, int],
            orders: Sequence[Decision]) -> tuple[float, dict[str, int]]:
    """把订单**逐笔结算**到状态上（P62 的修法）。

    这是不变量「**写入 = 重放**」的实现半边；重放半边是
    `engine._ledger_arm_state`。两半必须逐字段相同，所以这里
    **一行算术都不另写**：

    - 持仓：懒 import `engine._apply`（模块成环：`engine` 顶部导入本模块）
      用的就是重放路径调的那一个函数 —— 「减到 0 就 pop」（零股条目不许存在）
      与超卖守卫都是它的；
    - 现金：用 `fill_price × qty ± fee_total`，其中 `fill_price` 与
      `fees["total"]` 就是 `store.insert_trade` 写进成交行的那两个数 ——
      与 `_ledger_arm_state` 的重放式**逐字相同**。

    为什么不用 `Decision.amount`：它是 `round(fill × qty ± fee, 2)`（**先**四舍
    五入到分），而重放用的是不四舍五入的 `fill_price × qty`（`fill_price` 本身
    已是 4 位小数）。两者每笔差 ≤ 0.005 —— 真库上实测到 ¥0.01/笔的分币级漂移
    （P62 实施记录 §9.5 点名，那是**既有**口径、不属本站）。净值行是给「重放」
    读的读数，所以这里跟重放走。
    """
    from stocklab.paper import engine      # 懒 import：避免模块成环
    for d in orders:
        engine._apply(positions, str(d.code), str(d.action), int(d.qty))
        gross = float(d.fill_price) * int(d.qty)
        fee = float(d.fees.get("total", 0.0))
        cash += gross - fee if d.action == SIDE_SELL else -(gross + fee)
    return round(cash, 4), positions


def execute_decision(conn: sqlite3.Connection, *, arm: str, asof: str,
                     decision: Mapping[str, object], cash: float,
                     positions: dict[str, int], marks: Mapping[str, Price],
                     total_assets: float) -> tuple[float, dict[str, int],
                                                  list[Decision], list[Decision]]:
    """把一条决策翻成订单，并**在给定状态上原地结算**（现金 / 持仓）。

    **不写库**：成交由调用方（`engine._step_all`）用同一个 `store.insert_trade`
    落盘 —— 与纪律臂的 `_plan_steps` 完全一样。这里如果也写一遍，同一天同一笔
    会被插两次，撞上 `paper_trades` 的 UNIQUE（实测踩过），而且那个失败会以
    「整日事务回滚」的形式出现，看起来像是别处的错。

    溯源标签写进 `reason`：成交行是 append-only 的，「这笔单照哪一条决策下的」
    必须在成交行里自己说得清（与 P37 的 `spec_tag` 同一个理由）。

    ## 返回的必须是**结算后**的状态（P62）

    调用方拿着返回值**直接**写净值（`cash` / `positions` / `nav = cash + mv`），
    所以这里少结算一次 = 净值行记的是**执行前**的现金。P62 就是这么发生的：
    本函数原样返回入参，而 `positions` 已被 `plan_orders` 原地改过 ⇒
    `arm-agent-ds-v1` 在 2026-09-23 的净值行写成了「现金 ¥11,320（旧）、持仓
    000333×0（新）」的混合状态，凭空少 ¥8,233.68。

    判据是不变量（`tests/test_paper_agent_nav_settlement.py`）：**净值行 ==
    `engine._ledger_arm_state` 的重放 + `mark_to_market`**，逐日、逐字段。
    """
    codes = {str(d["code"]) for d in decision["decisions"]}
    asset_classes = {c: asset_class_for(conn, c) for c in sorted(codes)}
    orders, evals = plan_orders(decision=decision, cash=cash, positions=positions,
                               marks=marks, total_assets=total_assets,
                               asset_classes=asset_classes)
    cash, positions = _settle(cash, positions, orders)
    if not decision["decisions"]:
        evals.append(_no_decision_hold(arm, asof))
    tag = payload_tag(decision)
    return cash, positions, [replace(d, reason=f"{d.reason}（{tag}）") for d in orders], evals


def _no_decision_hold(arm: str, asof: str) -> Decision:
    """载荷为空（全现金）时的留痕：**不是「没跑到」，是「决定空仓」**。"""
    return Decision(
        action="hold", code=None, qty=0,
        rule_citation=RULE_CITATIONS_AGENT_DECISION["target_weight"],
        reason=f"{arm} 在 {asof} 的决策是**全现金**（decisions 为空数组）—— "
               f"空仓是一个决定，不是没跑到")


def weight_table(decision: Mapping[str, object],
                 positions: Mapping[str, int],
                 marks: Mapping[str, Price]) -> list[dict]:
    """目标 vs 实际（报告/页面用）。**实际**按执行后的持仓与收盘价算。"""
    out = []
    total = float(decision.get("total_assets") or 0.0)
    for item in decision["decisions"]:
        code = str(item["code"])
        price = float(item["price"])
        actual_value = round(price * int(positions.get(code, 0) or 0), 4)
        out.append({
            "code": code, "side": str(item["side"]),
            "target_weight_pct": float(item["target_weight_pct"]),
            "target_value": float(item["target_value"]),
            "actual_value": actual_value,
            "actual_weight_pct": (round(actual_value / total * 100.0, 4)
                                  if total else None),
            "reason": str(item["reason"]),
        })
    return out


def pool_for(conn: sqlite3.Connection, asof: str) -> dict:
    return agent_pool.pool_snapshot(conn, asof)
