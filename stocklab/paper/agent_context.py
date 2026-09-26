"""喂给智能体的 **PIT 上下文**与它的指纹（P37 阶段 1）。

## 这段代码存在的唯一理由

阶段 2 起，`arm-agent` 的每一版 spec 都由智能体产出；而「智能体看过了什么」决定了
那版 spec 是不是**只在当时可知的信息**上做出来的。所以上下文必须：

1. **只含 `date <= asof` 的行** —— 净值、持仓、指数收盘、台账，一个都不许越界；
   判据复用 `paper.rules.check_no_lookahead`，与执行内核同一条（不是另写一遍）。
2. **可复算** —— `context_sha256` 是 canonical JSON 的 sha256，不含生成时刻、
   不含自增 id、不含请求时刻。把 `asof` 之后的任何行塞进库，指纹**必须不变**
   （`tests/test_paper_agent_context.py` 钉住这一条）。
3. **自带「不许动什么」** —— 变更空间（5 个字段 + 区间）与禁止项写进上下文，
   于是「扩权」这件事在**输入侧**就不可表达，而不是只靠事后校验兜。

## 「喂进去」与「执行」是两条路

本模块只读：不写库、不改 spec、不调动执行内核。执行内核（`paper/rules.py`）从
`paper_agent_decisions` 取**已经落下**的那版 spec —— 于是「智能体当时看到了什么」
（`context_sha256`）与「后来实际跑了什么」（`spec_after_json`）在台账里是两列，
可以被分开检验。合成一列就再也回答不了「它是不是看了结果才改的」。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from stocklab.paper import agent_decide, agent_spec
from stocklab.paper.config import (
    ARM_AGENT_RANDOM,
    DISCLOSURE_ITEMS,
    HOLD_CODE,
    LOT,
    RULE_CITATIONS,
)
from stocklab.paper.engine import INDEX_300_SYMBOL, pit_close
from stocklab.paper.rules import check_no_lookahead, one_lot_cost

#: 上下文里**刻意不带**的东西（写下来，免得以后有人往里加）。
NON_GOALS: tuple[str, ...] = (
    "不做方向择时：上下文里没有、也不许加任何「涨跌预测」类字段"
    "（模型概率、资金流、均线方向、情绪分）",
    "不动成本口径 / PIT 判据 / 整手口径 / 白名单 / append-only 纪律",
    "不扩大变更空间本身（`SPEC_SCHEMA` 的区间不是可以改的字段）",
)


def _account_snapshot(conn: sqlite3.Connection, arm: str, asof: str) -> dict | None:
    """该臂在 `asof`（含）之前最近一行净值 —— **只查 `<= asof`**。没有 → `None`。"""
    row = conn.execute(
        "SELECT * FROM paper_nav_daily WHERE account_id = ? AND date <= ?"
        " ORDER BY date DESC LIMIT 1", (arm, asof)).fetchone()
    return None if row is None else dict(row)


def _position_codes(snapshot: dict | None) -> list[str]:
    codes = {HOLD_CODE}
    if snapshot is not None:
        codes |= {str(p["code"]) for p in json.loads(snapshot["positions_json"])}
    return sorted(codes)


def build_context(conn: sqlite3.Connection, *, arm: str, asof: str,
                  prices: Mapping[str, object] | None = None) -> dict:
    """构造该臂在 `asof` 的可审计快照。

    `prices` 只用于把「当日可用的价格」显式**注入**（与 `engine.step` 同一个注入点，
    同一个理由：价格敏感的判定只有参数化才钉得住）。注入的价格同样受 PIT 守卫检查。
    """
    spec = agent_spec.current_spec(conn, arm, asof)
    snapshot = _account_snapshot(conn, arm, asof)
    ledger = agent_spec.ledger_summary(conn, arm, asof)

    if prices is not None:
        marks = {str(k): v for k, v in prices.items()}
    else:
        marks = {c: p for c in _position_codes(snapshot)
                 if (p := pit_close(conn, c, asof)) is not None}
    check_no_lookahead(asof, marks)

    idx = marks.get(INDEX_300_SYMBOL) or pit_close(conn, INDEX_300_SYMBOL, asof)
    if idx is not None:
        check_no_lookahead(asof, {INDEX_300_SYMBOL: idx})

    return {
        "arm": arm,
        "asof": asof,
        "spec": spec,
        "spec_sha256": agent_spec.spec_sha256(spec),
        "ledger": ledger,
        "account": None if snapshot is None else {
            "date": str(snapshot["date"]),
            "cash": float(snapshot["cash"]),
            "positions": {str(p["code"]): int(p["qty"])
                          for p in json.loads(snapshot["positions_json"])},
            "market_value": float(snapshot["market_value"]),
            "nav": float(snapshot["nav"]),
            "cum_return": float(snapshot["cum_return"]),
            "cum_cost": float(snapshot["cum_cost"]),
            "net_deposits": float(snapshot["net_deposits"]),
            "drawdown": None if snapshot["drawdown"] is None
            else float(snapshot["drawdown"]),
        },
        "marks": {c: {"price": float(p.price), "price_asof": str(p.price_asof),
                      "source": str(p.source)} for c, p in sorted(marks.items())},
        "index_300": (None if idx is None else
                      {"level": float(idx.price), "price_asof": str(idx.price_asof),
                       "tradable": False,
                       "note": "指数不可直接交易 —— 与各臂对照时口径偏乐观"}),
        "change_space": {name: f.range_text()
                         for name, f in agent_spec.SPEC_SCHEMA.items()},
        "disclosure": list(DISCLOSURE_ITEMS),
        "non_goals": list(NON_GOALS),
        "citation_book": dict(RULE_CITATIONS),
        "counter_arm": ARM_AGENT_RANDOM,
    }


#: 指纹**覆盖**的键。加字段必须显式加到这里 —— 「上下文变了但指纹没变」是本模块
#: 最危险的一种失效：它会让「同输入同输出」这条判据悄悄失效，却一直显示为真。
HASHED_KEYS: tuple[str, ...] = ("arm", "asof", "spec", "spec_sha256", "ledger",
                                "account", "marks", "index_300", "change_space",
                                "disclosure", "non_goals", "citation_book",
                                "counter_arm")


def context_sha256(context: Mapping[str, object]) -> str:
    """上下文的 sha256（canonical JSON，不含时间戳）。同输入 ⇒ 逐字节同指纹。"""
    missing = [k for k in HASHED_KEYS if k not in context]
    if missing:
        raise KeyError(f"上下文缺字段 {missing} —— 指纹会漏掉它们，故直接报错")
    payload = {k: context[k] for k in HASHED_KEYS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------- P52：操盘决策的上下文（D-34） ----------

#: 操盘上下文里有、spec 上下文里没有的东西：**可投集合**与**护栏清单**。
#: 变更空间（5 字段白名单）不在里面 —— P52 的决策空间是「方向 ＋ 仓位 ＋ 池内选标的」，
#: 白名单不再约束它（D-34 覆盖 D-18）。
DECISION_HASHED_KEYS: tuple[str, ...] = (
    "arm", "asof", "account", "marks", "index_300", "pool", "guardrails",
    "tradability", "disclosure", "non_goals", "counter_arm",
)

#: 不许做的（写进上下文，让「越权」在**输入侧**就不可表达）。
GUARDRAILS: tuple[str, ...] = (
    "不许杠杆、不许负权重：Σ target_weight_pct + cash_pct 必须 = 100",
    "不许池外标的：code 必须在当日候选池（短/中/长并集）里",
    "不许做空：A 股无融券。「看空」只能表达为降低总仓位 / 清仓",
    "不许接券商下单：本项目只记账、不发单",
    "不许用 > asof 的数据：上下文与价格全是 PIT",
    "不许参数搜索：这一条决策就是这一条，试错次数必须与读数同时报",
    "不许改主干常量：熔断阈值 / 自评估边界 / approve 闸门都不在决策空间里",
)


def _tradability_block(*, prices: Mapping[str, object], total_assets: float,
                       asset_classes: Mapping[str, str],
                       lot: int = LOT) -> dict:
    """「一手（100 股）够不够」这条约束的**输入侧**表达（P79 / D2）。

    ## 为什么它必须出现在上下文里

    A 股一手成本跨标的差 10 倍以上（`601398` 一手约 ¥813 vs `600519` 一手 ¥14 万+），
    两万本金的账户上「目标权重 < 1.6%」就等于「不许买」。这条约束**不能靠模型自己悟**
    —— 09-23 的 `arm-agent-ds-v1` 四笔买入全部落在「目标市值 < 一手」上（P79 §0.1），
    而它的上下文里**一个字都没提一手多少钱**。把眼睛给足，是本站的一半。

    ## 数字一律从 `rules.one_lot_cost` 取（一行费用公式都不另写）

    `one_lot_cost = fill_price("buy", price) * lot + _fee_parts(...)["total"]`
    —— 与执行层**同一个** `CostModel.fill_price` 与同一个 `_fee_parts`（P79 / D2）。

    ## 键集 = `marks ∩ pool`

    「池内标的」＝ 决策**可能落在**的集合（`validate_payload` 同时要求
    `code ∈ pool_codes` 且 `code ∈ marks`）。所以这里只报这个交集：账户里的**池外**
    持仓不在 AI 的可投集合内（对它下单会被写入口拒），指数（`sh000300`）也不在。

    ## `min_weight_pct`

    单个标的的 `min_weight_pct = round(一手总成本 / 总资产 × 100, 4)` —— 「想买它，
    目标权重至少要这么大」。顶层那个是 `by_code` 里的**最小值**，即「这个账户上
    最小的一笔可成交建仓占总资产几个点」：任何低于它的目标权重都买不到任何东西。

    `total_assets ≤ 0` 时逐个标的一律 `None`、顶层也是 `None` —— 算不出占比时
    **不猜一个数**（与 `_reserved_pct` 的退化口径同款）。

    `asset_classes` 必须由调用方按 `agent_decide.asset_class_for`（`instruments.type`
    是唯一真相）解析好；缺键 ⇒ `KeyError`，不静默退化成股票费率。
    """
    total = float(total_assets)
    by_code: dict[str, dict] = {}
    for code in sorted(prices):
        cost = one_lot_cost(price=float(prices[code].price),
                            asset_class=asset_classes[str(code)], lot=lot)
        by_code[str(code)] = {
            "one_lot_cost": cost,
            "min_weight_pct": (round(cost / total * 100.0, 4) if total > 0 else None),
        }
    mins = [v["min_weight_pct"] for v in by_code.values()
            if v["min_weight_pct"] is not None]
    return {"lot": int(lot), "min_weight_pct": (min(mins) if mins else None),
            "by_code": by_code}


def build_decision_context(conn: sqlite3.Connection, *, arm: str, asof: str,
                           pool: Mapping[str, object], cash: float,
                           positions: Mapping[str, int],
                           marks: Mapping[str, object],
                           total_assets: float) -> dict:
    """喂给 AI 操盘手的 **PIT 上下文**（与 `build_context` 并列，键集不同）。

    只含 `<= asof` 的行（净值 / 持仓 / 收盘价 / 候选池快照）；候选池快照本身也按
    `asof <= 决策日` 取（`agent_pool.pool_snapshot`）。
    """
    check_no_lookahead(asof, marks)
    idx = marks.get(INDEX_300_SYMBOL) or pit_close(conn, INDEX_300_SYMBOL, asof)
    if idx is not None:
        check_no_lookahead(asof, {INDEX_300_SYMBOL: idx})
    # 可成交性只报**池内有价**的那些（见 `_tradability_block`）：口径按
    # `instruments.type` 解析（未登记即报错，不默认成股票）。
    tradable = sorted(set(marks) & {str(c) for c in (pool.get("codes") or [])})
    asset_classes = {c: agent_decide.asset_class_for(conn, c) for c in tradable}
    return {
        "arm": arm,
        "asof": asof,
        "account": {"cash": round(float(cash), 4),
                    "positions": {str(k): int(v) for k, v in sorted(positions.items())},
                    "market_value": round(total_assets - float(cash), 4),
                    "total_assets": round(float(total_assets), 4)},
        "marks": {c: {"price": float(p.price), "price_asof": str(p.price_asof),
                      "source": str(p.source)} for c, p in sorted(marks.items())},
        "index_300": (None if idx is None else
                      {"level": float(idx.price), "price_asof": str(idx.price_asof),
                       "tradable": False,
                       "note": "指数不可直接交易 —— 与各臂对照时口径偏乐观"}),
        "pool": {"asof": pool.get("asof"), "codes": list(pool.get("codes") or []),
                 "pools": pool.get("pools") or {},
                 "missing_pools": list(pool.get("missing_pools") or []),
                 "available": bool(pool.get("available"))},
        # ⚠️ 加了这一块之后，**同一个输入**在 P79 前后的 `decision_context_sha256`
        # 不再相同（它是 AI 看得见的输入，必须进指纹 —— 见 `DECISION_HASHED_KEYS`）。
        # 台账里**已有的**决策行的 sha 一律不改写（append-only）：D-49 的复核只对
        # 新写入的载荷生效，旧行读出来的是「它当时看到的那个输入」。
        "tradability": _tradability_block(
            prices={c: marks[c] for c in tradable}, total_assets=float(total_assets),
            asset_classes=asset_classes, lot=LOT),
        "guardrails": list(GUARDRAILS),
        "disclosure": list(DISCLOSURE_ITEMS),
        "non_goals": list(NON_GOALS),
        "counter_arm": ARM_AGENT_RANDOM,
    }


def decision_context_sha256(context: Mapping[str, object]) -> str:
    """操盘上下文的指纹（与 `context_sha256` 同一条纪律：同输入 ⇒ 同指纹）。"""
    missing = [k for k in DECISION_HASHED_KEYS if k not in context]
    if missing:
        raise KeyError(f"上下文缺字段 {missing} —— 指纹会漏掉它们，故直接报错")
    payload = {k: context[k] for k in DECISION_HASHED_KEYS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def context_summary(context: Mapping[str, object]) -> dict:
    """给报告/页面用的一行摘要（**不重算**任何上层数字）。"""
    account = context["account"] or {}
    ledger = context["ledger"] or {}
    return {
        "arm": context["arm"],
        "asof": context["asof"],
        "spec_sha256": context["spec_sha256"],
        "n_reviews": int(ledger.get("n_reviews") or 0),
        "account_date": account.get("date"),
        "nav": account.get("nav"),
        "context_sha256": context_sha256(context),
    }
