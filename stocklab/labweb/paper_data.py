"""模拟盘对照页取数（P19 展示层）：「我 vs AI 纪律臂 vs 大盘」。

## 四条线各是什么

| 线 | 状态来源 | 它回答的问题 |
|---|---|---|
| 我 · 实盘账本镜像（`arm-now`） | `real_trades` + `cash_flows` 逐笔重放 | 真人这笔钱现在值多少 |
| AI 纪律臂（`arm-discipline-{05,10,15}`） | 自身 `paper_trades` | 写死的规则跑出来是多少 |
| AI 智能体臂（`arm-agent` / `arm-agent-random`） | 自身 `paper_trades` | 条文数字**可改**时（台账里的当前 spec）跑出来是多少 |
| 什么都不做（`arm-hold`） | `init` 时**冻结**的快照 | 一动不动是什么结果 |
| 大盘（`sh000300`） | `bars_daily` 收盘 | 市场本身涨了多少 |

## 「AI」这个词的边界（页面文案也必须守）

`arm-discipline-*` 里**没有模型方向预测**：它执行的是写死的纪律条文
（`paper/config.RULE_CITATIONS`），选哪只 ETF 由白名单决定。生产模型的方向能力
≈ 0（`pit-rw-v1.0.2`，区间 2013-12-23 → 2026-09-14：行级命中 37.883%、Brier 0.66070
对随机 0.667），所以「拿涨的概率当买入信号」
被 `test_paper_never_imports_model_or_kelly` 源码扫描钉死。口径是
「AI 纪律臂（规则执行，不含方向预测）」，不是「AI 操盘手」。

`arm-agent` 同样**不用模型**：它只是把同一条纪律的 5 个数字搬到台账里
（`paper_agent_decisions`），阶段 1–2 的 spec 由人手写或由人复核后落库。
所以它回答的是「条文数字可变之后会怎样」，不是「AI 会操盘」。这一点在页面上
必须写清楚，否则两条 AI 线会被读成「有模型在里面」。

## 「自己编排的东西用上了没有」（`ai_evidence`）

对照页还要回答一个问题：**页面上那条「AI」线，究竟用没用上我们自己编排的东西**
（插桩脚本、候选池、模型预测）。这三段全部**用计数回答**：

| 段 | 来源 | 回答 |
|---|---|---|
| `accuracy` | `session.review.rolling_accuracy`（与「数据」页、`review` 报告同源） | AI 自己的准确率是多少 |
| `scripts` / `backtests` / `candidate` / `routing` | `plugin_*`、`candidate_*` 表 + `candidate.score.POOL_PLUGIN` | 自己编排的产出有哪些、谁在调 |
| `consumption` | `paper_trades.rule_citation` 对表 `paper.config.RULE_CITATIONS` **＋** `RULE_CITATIONS_AGENT` ＋ `paper_accounts.params_json` | 模拟盘**实际**消费了几条 |

第三段是**实测**而不是描述：落在两张条文表之外的触发理由会被逐条列出（空 = 没有
任何一笔成交由模型或插桩脚本触发）。将来真接了模型臂，这里会自己变成非空 ——
所以它同时是一块看板和一个钩子。

`arm-agent` 的成交引用的是 `RULE_CITATIONS_AGENT`（第二张表）。它与写死条文表
**并列而不合并**：合并之后「有几笔成交由 spec 触发」就再也数不出来了。所以这里
分开计数（`n_trades_by_spec`），而 `unknown_rules` **仍然为空** —— 阶段 1 的 spec
是人手写的，不是模型信号。

## 本模块不产生新口径

- 各臂的净值 / 累计收益 / 最大回撤 / 累计成本 / 相对大盘超额 → **直接调
  `paper.engine.build_report`**（与 `paper show` 同源，页面上不会出现第二套数）；
- 逐日序列 → 读库里的 `paper_nav_daily.cum_return` 列，**不重算**；
- 大盘的累计收益 → 一律以 `paper_accounts.start_date` 那天的 `sh000300` 收盘为基
  （与 `build_report` 的 `return_since_start` **同一个基**），所以同一页面上
  「大盘涨了多少」只能有一个值；
- 起跑日锚点 → `paper_accounts.initial_nav ÷ params_json.initial_capital`
  （两列都在库里，不是补出来的）。

指标缺值时一律 `None`：折线在那里**断开**，不插值、不用前一日收盘滚动填充、
不用 0 顶替。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from stocklab.backtest import metrics as bt
from stocklab.backtest.portfolio import NavPoint
from stocklab.paper import agent_decide
from stocklab.paper import agent_spec
from stocklab.paper import store as paper_store
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, ARM_KIND_AGENT,
                                   ARM_KIND_AGENT_RANDOM, DECISION_KIND_SPEC,
                                   EXECUTOR_AGENT_DECISION, EXECUTOR_CHANNEL_A,
                                   EXECUTOR_KEY, LIVE_KEY, PAPER_START_DATE,
                                   PREREGISTERED_KEY, RULE_CITATIONS,
                                   RULE_CITATIONS_AGENT,
                                   RULE_CITATIONS_AGENT_DECISION)
from stocklab.paper.engine import (INDEX_300_SYMBOL, agent_block, build_report,
                                   live_of, net_deposits_at)
from stocklab.plugin import lifecycle as plugin_lifecycle
from stocklab.plugin import store as plugin_store
from stocklab.session.review import rolling_accuracy
from stocklab.verify.report import MIN_DAYS

#: 大盘显示名。指数**不可直接交易**，所以它没有成本 —— 对照时口径偏乐观，
#: 这句话在报告（`build_report`）与页面上各出现一次，措辞同源。
INDEX_LABEL = "沪深300 指数"

#: 认得出「我」的那条臂（`paper/config.ARM_NOW` 的 row 值）。
_ARM_NOW = "now"
_ARM_HOLD = "hold"

#: 智能体臂的两种 row 值（`paper_accounts.arm`）。
_ARM_AGENT = ARM_KIND_AGENT
_ARM_AGENT_RANDOM = ARM_KIND_AGENT_RANDOM


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """表在不在。新表在老库上可能还没前滚 —— 页面**只读**，不外滚 schema，
    所以缺表时要能给一个「不知道怎么答」，而不是 500。
    """
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()[0] > 0


def arm_descriptor(account: Mapping) -> dict:
    """账户行 → 页面/报告要读的那几条**声明字段**（`params_json` 的原样投影）。

    一处定义、多处消费（`track` 的 arms 与 `performance` 的 rows 共用），因为
    「贴哪个标签、上哪条线」现在要按**执行者**分档（P69 §T3），而执行者写在
    `params_json` 里。页面不许自己 `json.loads` 一遍 —— 两份投影迟早漂。

    **只投影，不派生**：`executor` / `live` / `model_id` / `plugin_hooks` /
    `strategy_version` 都是从账户行里**读出来的值**，没有一个新口径。
    `live` 走 `engine.live_of`（缺省 `true`，写错类型点名报错）—— 与认领侧同一个判据，
    所以「页面说它停飞了」与「`paper agent run` 不认领它」不可能分叉。
    """
    params = json.loads(account["params_json"] or "{}")
    prereg = params.get(PREREGISTERED_KEY) or {}
    model = prereg.get("model_id") if isinstance(prereg, Mapping) else None
    return {
        "executor": params.get(EXECUTOR_KEY),
        "live": live_of(params),
        "model_id": (str(model) if model else None),
        "prompt_sha256": (str(prereg.get("prompt_sha256"))
                          if isinstance(prereg, Mapping) and prereg.get("prompt_sha256")
                          else None),
        "plugin_hooks": [str(h) for h in (params.get("plugin_hooks") or [])],
        "strategy_version": (str(params["strategy_version"])
                             if params.get("strategy_version") else None),
    }


def _anchor_cum_return(account: dict) -> float | None:
    """起跑日锚点的累计收益 = `initial_nav / initial_capital − 1`。

    两个数都在 `paper_accounts` 里：`initial_nav` 是 `init` 当天按收盘价
    mark-to-market 的结果，`initial_capital` 在 `params_json`。任一项缺失或
    为 0 → `None`（锚点不画，而不是画成 0）。
    """
    try:
        capital = float(json.loads(account["params_json"])["initial_capital"])
        initial_nav = float(account["initial_nav"])
    except (KeyError, TypeError, ValueError):
        return None
    if not capital:
        return None
    return round(initial_nav / capital - 1.0, 6)


def _index_levels(conn: sqlite3.Connection, dates: list[str], *,
                  symbol: str = INDEX_300_SYMBOL) -> dict[str, float]:
    """`dates` 里每个日期的**该指数**收盘价（`adj_mode='none'`，与 `engine.pit_close` 同口径）。

    **只按精确日期取**，不做「取最近的前一个交易日」——那会让「那天没开盘」
    看起来像「那天指数没动」。缺的日期就是不返回，由调用方断线。

    `symbol` 有默认值：既有调用方（`/lab/paper`、P41 的用例）口径逐位不变，
    而 P55 的第二个基准（`sh000905`）复用**同一段取数逻辑** —— 复制一份
    「按日期取收盘」才是真的会漂。
    """
    if not dates:
        return {}
    marks = ",".join("?" * len(dates))
    sql = ("SELECT date, close FROM bars_daily WHERE code = ?"
           " AND adj_mode = 'none' AND date IN (" + marks + ")")
    return {str(r["date"]): float(r["close"])
            for r in conn.execute(sql, (symbol, *dates))}


def _real_trades(conn: sqlite3.Connection, asof: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM real_trades WHERE date <= ? ORDER BY date, trade_id",
        (asof,))]


def _paper_trades(conn: sqlite3.Connection, asof: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM paper_trades WHERE date <= ? ORDER BY date, trade_id",
        (asof,))]


# ---------- 「自己编排的东西用上了没有」 ----------

#: 「谁在消费自己编排的产出」这一段的判据：模拟盘账户参数里出现这些键，
#: 就说明它接了模型 / 插桩。当前实测为空 —— 空就是空，不写「应该是空」。
_CONSUMPTION_MARKERS: tuple[str, ...] = ("plugin", "plugin_id", "script_id",
                                        "model_version", "model")


def _plugin_routing(conn: sqlite3.Connection) -> list[dict]:
    """哪个 plugin 管哪一池，以及它**当前生效**的是哪一版。

    池 → plugin 的映射从 `candidate.score.POOL_PLUGIN` / `INDUSTRY_SCREEN_PLUGIN`
    **反查**，不在这里再抄一份：抄出来的那份会和打分内核漂移，页面就会指错脚本。
    生效版本走 `plugin.lifecycle.active_script_id`（与打分内核同一个函数）——
    认不出 active 版本时返回 `None` 并原样显示，不猜。
    """
    from stocklab.candidate import score   # 懒 import：展示层不反向依赖打分内核
    pairs = [("行业排雷", score.INDUSTRY_SCREEN_PLUGIN)]
    pairs += [(f"{pool} 池打分", pid)
              for pool, pid in sorted(score.POOL_PLUGIN.items())]
    out = []
    for label, pid in pairs:
        sid = plugin_lifecycle.active_script_id(conn, str(pid))
        version = None
        if sid is not None:
            row = plugin_store.get_script(conn, int(sid))
            version = str(row["version"]) if row else None
        out.append({"label": label, "plugin_id": str(pid),
                    "active_script_id": (None if sid is None else int(sid)),
                    "version": version})
    return out


def ai_evidence(conn: sqlite3.Connection, asof: str) -> dict:
    """「AI 自己编排的东西，用上了没有」—— 三段，全部用计数回答。

    `accuracy` 与「数据」页同源（`rolling_accuracy`），这里不重算；
    `consumption` 是实测：各臂成交的 `rule_citation` 与本模块 import 的
    `paper.config.RULE_CITATIONS` 逐条对表，表外的理由单独列出。
    """
    acc = rolling_accuracy(conn, end_date=asof)

    scripts: list[dict] = []
    for s in plugin_store.list_scripts(conn):
        sid = int(s["script_id"])
        scripts.append({
            "script_id": sid, "plugin_id": str(s["plugin_id"]),
            "version": str(s["version"]),
            "state": plugin_lifecycle.script_state(conn, sid),
            "created_at": str(s["created_at"]),
            "note": str(s["note"] or ""),
        })
    by_state: dict[str, int] = {}
    for s in scripts:
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1

    def _count(table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    snaps = [dict(r) for r in conn.execute(
        "SELECT snapshot_id, asof, run_kind, created_at FROM candidate_snapshots"
        " ORDER BY asof")]

    known = set(RULE_CITATIONS.values())
    agent_known = set(RULE_CITATIONS_AGENT.values())
    decision_known = set(RULE_CITATIONS_AGENT_DECISION.values())
    rows = conn.execute("SELECT rule_citation FROM paper_trades").fetchall()
    cited = sorted({str(r["rule_citation"] or "") for r in rows})
    registered = known | agent_known | decision_known
    unknown = [c for c in cited if c not in registered]
    spec_cited = [c for c in cited if c in agent_known]
    decision_cited = [c for c in cited if c in decision_known]
    n_by_spec = sum(1 for r in rows if str(r["rule_citation"] or "") in agent_known)
    n_by_decision = sum(1 for r in rows
                        if str(r["rule_citation"] or "") in decision_known)
    blobs = [str(r["params_json"] or "") for r in conn.execute(
        "SELECT params_json FROM paper_accounts")]
    param_keys = sorted({k for blob in blobs for k in json.loads(blob or "{}")})
    param_refs = sorted({m for m in _CONSUMPTION_MARKERS
                         if any(m in blob for blob in blobs)})

    return {
        "accuracy": acc,
        "counts": {
            "predictions": _count("predictions"),
            "verifications": _count("verifications"),
            "plugin_scripts": len(scripts),
            "plugin_backtests": _count("plugin_backtests"),
            "candidate_snapshots": len(snaps),
            "candidate_members": _count("candidate_members"),
        },
        "scripts": scripts,
        "by_state": by_state,
        "backtests": [dict(b) for b in plugin_store.load_backtests(conn)],
        "routing": _plugin_routing(conn),
        "candidate": [{"snapshot_id": int(r["snapshot_id"]),
                       "asof": str(r["asof"]),
                       "run_kind": str(r["run_kind"]),
                       "created_at": str(r["created_at"])} for r in snaps],
        "consumption": {
            "n_accounts": _count("paper_accounts"),
            "n_trades": len(rows),
            "cited_rules": cited,
            "unknown_rules": unknown,
            # `arm-agent` 的成交：条文来自 spec 台账（第二张表），**单列计数**。
            # 它不是「表外」—— 表外为空与这里有数，两件事必须能同时成立。
            "spec_rules": spec_cited,
            "n_trades_by_spec": n_by_spec,
            # P52：条文来自**当日决策台账**的成交（AI 操盘手与它的随机对照）。
            # 与 spec 单列是同一个理由：合成一列就再也分不开「改纪律数字」与「当操盘手」。
            "decision_rules": decision_cited,
            "n_trades_by_decision": n_by_decision,
            "param_keys": param_keys,
            "param_refs": param_refs,
        },
    }


def _empty(asof: str, start: str, *, db_missing: bool = False,
           ai: dict | None = None, agent: dict | None = None) -> dict:
    return {"asof": asof, "available": False, "db_missing": db_missing,
            # 空库也要有这一个键：缺键与「没有对照臂」在页面上长得一样，
            # 而它们不是一回事（前者是页面坏了，后者是真话）。
            "comparison": {},
            "start_date": start, "date": None, "dates": [], "n_sessions": 0,
            "arms": [], "index": None, "now_account_id": None,
            "mirror_equals_hold": None, "real_trades": [],
            "real_trades_after_start": None, "paper_trades": [],
            "ai": ai or {}, "agent": agent or {},
            "performance": _empty_performance(asof, start=start),
            # P69 §T4：空库也要有这个键（缺键与「没有 AI 臂」长得一样，不是一回事）。
            "agent_ops": {"asof": asof, "arms": [], "records": [], "caliber": {},
                          "notes": []},
            "disclosure": [], "disclaimer": "", "sample_note": ""}


# ---------- 绩效对比（模块2 §4 的五个指标 + 样本量门禁） ----------

#: 需求 02 §4「绩效对比评估模块（固定）」点名的五个指标。**名字与顺序定死** ——
#: 超出这五个的（Sharpe / Calmar / 波动率）一律不加：需求只点名五个，多出来的
#: 每个都是「事后换指标救结果」的入口。
METRIC_KEYS: tuple[str, ...] = ("total_return", "annualized_return",
                                "max_drawdown", "profit_loss_ratio", "win_rate")

#: 样本量门槛。与 `verify.report.MIN_DAYS` **同源同值**，不另写一个 120
#: （`trend/evaluate.py` 同一条做法）。
PERFORMANCE_THRESHOLD: int = MIN_DAYS

#: 门禁没过时的**唯一**措辞（`CLAUDE.md` 度量纪律第 3 条的结构化落地）。
PERFORMANCE_INSUFFICIENT = "样本不足，仅供观察；不得据此选策略或改口径"

#: 指标中文名。放在数据层是因为 CLI 的文本表与页面表都要用**同一批**列名，
#: 各写一份必然漂移（页面写「年化」、CLI 写「年化收益」那种）。
METRIC_LABELS: dict[str, str] = {
    "total_return": "总收益", "annualized_return": "年化",
    "max_drawdown": "最大回撤", "profit_loss_ratio": "盈亏比", "win_rate": "胜率",
}

_KIND_BENCHMARK = "benchmark"


def _metrics(values: list[float], *, missing_reason: str | None = None) -> dict:
    """一条净值（或指数点位）序列 → 五个指标。

    **唯一一处算法**：`total_return` / `max_drawdown` 走 `bt.summarize`，
    `annualized_return` 走 `bt.annualize`，`win_rate` / `profit_loss_ratio`
    吃同一批日收益。`paper` 侧不另写公式（需求 02 §4 与任务书 T1 的共同要求）。

    `values` 的第一项是**期初基准**（账户 = 净入金 `net_deposits`，
    基准 = 起跑日的指数收盘），所以 `values` 长度 = 日收益个数 + 1。
    """
    if missing_reason is not None or len(values) < 2:
        reason = missing_reason or "窗口内不足两行净值 —— 算不出收益序列"
        return {**{k: None for k in METRIC_KEYS},
                "missing": {k: reason for k in METRIC_KEYS},
                "n_sessions": (None if missing_reason is not None
                               else len(values) - 1)}

    points = [NavPoint(str(i), v) for i, v in enumerate(values)]
    s = bt.summarize(points, values[0])
    returns = bt.daily_returns(points)
    plr = bt.profit_loss_ratio(returns)
    out = {
        "total_return": s["total_return"],
        "annualized_return": bt.annualize(s["total_return"], len(returns)),
        "max_drawdown": s["max_drawdown"],
        "profit_loss_ratio": plr,
        "win_rate": bt.win_rate(returns),
        "missing": {},
        "n_sessions": len(returns),
    }
    if plr is None:
        out["missing"]["profit_loss_ratio"] = (
            f"窗口内 {len(returns)} 个日收益里没有亏损日（或没有盈利日）—— "
            f"盈亏比算不出。**不是 0，也不是无穷大**：分母那一侧还没出现")
    return out


def _empty_performance(asof: str, *, start: str, reason: str | None = None) -> dict:
    """`available: False` 的绩效块 —— 数字一个都不编，门禁照报。"""
    return {
        "asof": asof, "available": False,
        "reason": reason or (f"`paper_nav_daily` 里没有任何 ≤ {asof} 的净值行 —— "
                             f"没有净值就没有绩效，本块不拿 0 顶替"),
        "start_date": start, "window": [start, None],
        "n_sessions": 0, "n_folds": None,
        "sample_gate": sample_gate(0),
        "metric_keys": list(METRIC_KEYS),
        "rows": [], "excess_vs_index_300": {}, "excess_vs_hold": {},
        "notes": [PERFORMANCE_INSUFFICIENT],
    }


def sample_gate(n_sessions: int) -> dict:
    """样本量门禁。`n_sessions` 一律是**交易日**数（`CLAUDE.md` 度量纪律第 2 条）。

    公开而不是下划线私有：P48 的预测校验读数要用**同一把尺子**（同一套
    `ok`/`insufficient` 词表与同一个阈值），再写一份就等于给「两个门禁慢慢漂」
    留门 —— 与 `paper/engine.py::drawdown` 正名是同一个理由。
    """
    ok = n_sessions >= PERFORMANCE_THRESHOLD
    return {
        "threshold": PERFORMANCE_THRESHOLD,
        "n_sessions": n_sessions,
        "meets": ok,
        "gate_status": "ok" if ok else "insufficient",
        "label": "样本充足" if ok else PERFORMANCE_INSUFFICIENT,
    }


def _row_metrics(account: dict, rows: list[dict], *, conn: sqlite3.Connection,
                 start: str) -> dict:
    """一个账户的五个指标：期初 = **净入金**（不是 0、不是现金、也不是 `initial_nav`）。

    与既有「累计收益」列（`paper_nav_daily.cum_return`）**同一个基**，所以新节的
    「总收益」与那一列逐位一致 —— 见 `performance` 的「期初口径」与 ADR-023 修正段
    （D-37）。取 `paper_accounts.initial_nav` 的话，起跑日那笔浮盈会被算进每一臂，
    同一个页面上就会有两个差一个常数（真实库 0.2155%）的「总收益」。

    ## 窗口内一行净值都没有时：向**引擎**要净入金（P80 / D8）

    改前这里是 `params_json.initial_capital` —— 那是**同一个数第二份实现**。
    入金事件（`paper_capital_events`）落地后它会与引擎分叉：引擎的净入金是
    「起跑本金 ＋ Σ(带符号事件 ≤ asof)」，而这里的字面量永远是 2 万。
    同一个数两个实现，迟早分叉（本项目的既有教训），所以收口到
    引擎的 `net_deposits_at`（**那一份**，含资本事件）。
    期初锚点用窗口起点 `start`，与上面 `rows[0]["net_deposits"]` 同一天。
    """
    try:
        if rows:
            initial = float(rows[0]["net_deposits"])
        else:
            initial = float(net_deposits_at(conn, account, start))
    except (KeyError, TypeError, ValueError):
        return _metrics([])
    return _metrics([initial, *[float(r["nav"]) for r in rows]])


def performance(conn: sqlite3.Connection, asof: str, *,
                benchmarks: tuple[str, ...] = (INDEX_300_SYMBOL,)) -> dict:
    """模块2 §4 的绩效对比：每臂一行 × 五指标 + **每个基准一行** + 样本量门禁。

    ## 期初口径

    每条线的期初 = 它**起跑日**的值：账户取**净入金**（`paper_nav_daily.net_deposits`），
    基准取起跑日 `sh000300` 收盘。五个指标全部由这一条序列推出，
    所以「总收益 == Π(1+日收益) − 1」恒成立 —— 指标集内部只有一套口径。

    账户的期初取净入金而**不是** `paper_accounts.initial_nav`：后者含起跑日种子买入的
    浮动（实测 2026-09-15：20043 ÷ 20000），会让本节的「总收益」比既有「累计收益」列
    低一个**对全部臂相同**的常数（0.2155%）—— 同一个页面上的两个「总收益」差一个常数，
    读者只会读成「真差异」。ADR-023 的修正段（D-37）拍板改取净入金，两列逐位一致；
    落地见 P51 T4，`test_the_new_section_matches_the_existing_cum_return_column`
    钉住这条不许再漂。**未变**：`paper_nav_daily.nav` 与 `cum_return` 的基、
    成本口径（ADR-008）、门禁 120 交易日。

    ## 基准

    `benchmarks` 里**每个**指数各一行，与各臂**同一区间、同一交易日轴**，
    只按精确日期取收盘价。窗口内任一日期缺收盘价 → 该行五个指标全部 `None`
    + 原因，**绝不用相邻日顶替**（顶替出来的斜率是编的）。

    `benchmarks` 默认只有 `sh000300` ⇒ `/lab/paper` 与既有调用方的载荷逐位不变。
    模块2 传的是 `m2_config.BENCHMARKS`（P55 起两个）。**每个基准各自一行**，
    不许相减/取平均/合成「综合基准」；`excess_vs_index_300` 这个键名已经把
    「相对量只对 `sh000300`」写死了，第二个基准不参与减法。

    ## 门禁

    `n_sessions` 是窗口内的交易日数。不足 `threshold`（120）时 `gate_status`
    为 `insufficient`，措辞在 `label` / `notes` 里 —— 结论性措辞由渲染层同源消费。
    """
    accounts = paper_store.load_accounts(conn)
    start = str(accounts[0]["start_date"]) if accounts else PAPER_START_DATE
    nav_dates = [str(r["date"]) for r in conn.execute(
        "SELECT DISTINCT date FROM paper_nav_daily WHERE date <= ? ORDER BY date",
        (asof,))]
    if not accounts or not nav_dates:
        return _empty_performance(asof, start=start)

    n_sessions = len(nav_dates)
    gate = sample_gate(n_sessions)

    rows: list[dict] = []
    #: 每个「缺收盘价 ⇒ 整行不可判定」的基准各留一条原因（P55 起可能不止一条）
    reasons: list[str] = []
    for account in accounts:
        aid = str(account["account_id"])
        own = paper_store.load_nav(conn, aid, asof=asof)
        m = _row_metrics(account, own, conn=conn, start=start)
        rows.append({
            "account_id": aid, "arm": str(account["arm"]),
            "kind": (_ARM_HOLD if str(account["arm"]) == _ARM_HOLD
                     else "arm"),
            "etf_target_pct": account["etf_target_pct"],
            **arm_descriptor(account),
            **{k: m[k] for k in METRIC_KEYS},
            "n_sessions": m["n_sessions"], "missing": m["missing"],
        })

    # 基准：与各臂同一条交易日轴。任一日期缺价 → 整行不可判定。
    # 起跑日**可能**自己就有一行净值（`paper step --asof <起跑日>`）：那时它只能占
    # 一个位置 —— 重复放进轴里会凭空多出一个 0% 的「交易日」，把基准的
    # `n_sessions` / 胜率 / 回撤一起带偏（真库与既有夹具都没触发，是潜在错）。
    axis = [start, *[d for d in nav_dates if d != start]]
    for symbol in benchmarks:
        levels = _index_levels(conn, axis, symbol=symbol)
        absent = [d for d in axis if d not in levels]
        if absent:
            reason = (f"基准 {symbol} 在窗口内 {len(absent)} 个日期上"
                      f"没有收盘价：{'、'.join(absent)} —— 不用相邻日顶替")
            bench = _metrics([], missing_reason=reason)
            reasons.append(reason)
        else:
            bench = _metrics([levels[d] for d in axis])
        rows.append({
            "account_id": symbol, "arm": "index",
            "kind": _KIND_BENCHMARK, "etf_target_pct": None,
            **{k: bench[k] for k in METRIC_KEYS},
            "n_sessions": bench["n_sessions"], "missing": bench["missing"],
        })

    by_id = {r["account_id"]: r for r in rows}
    # 相对量始终对 `sh000300` —— **一个**基准的差值，不是「两个基准合成的基准」。
    # 载荷键名已经把这一点写死（`excess_vs_index_300`），第二个基准不参与减法。
    idx_ret = by_id[INDEX_300_SYMBOL]["total_return"]
    hold_row = next((r for r in rows if r["kind"] == _ARM_HOLD), None)
    hold_ret = hold_row["total_return"] if hold_row is not None else None

    def _excess(base: float | None, *, skip_kind: str) -> dict[str, float | None]:
        """相对某一类线（基准 / 「不动」臂）的差值 —— 一次减法，不是新口径。

        **不 round**：四舍五入到 6 位会让「差值」≠「两个显示出来的数相减」，
        而这一段存在的意义正是「它只是一次减法」。显示端自己按位数截。
        """
        return {r["account_id"]: (
            None if r["kind"] == skip_kind or r["total_return"] is None
            or base is None else r["total_return"] - base)
            for r in rows}

    excess_idx = _excess(idx_ret, skip_kind=_KIND_BENCHMARK)
    excess_hold = _excess(hold_ret, skip_kind=_ARM_HOLD)

    notes = [
        f"窗口 {start} ~ {asof}，共 {n_sessions} 个交易日"
        f"（净值行数 = 日收益数；横轴另外含起跑日锚点一个点）。",
        f"期初口径：账户 = **净入金**（`paper_nav_daily.net_deposits`，ADR-023 修正段 "
        f"D-37）、基准 = 起跑日 `{INDEX_300_SYMBOL}` 收盘；"
        f"五个指标全部由这一条序列推出，所以本节的「总收益」与既有「累计收益」列"
        f"**逐位一致**。",
        "本载荷的 `max_drawdown` 为**负值**（`backtest/metrics` 口径）；"
        "渲染层按页面既有列的正值口径显示同一个数。",
    ]
    if not gate["meets"]:
        notes.append(f"{PERFORMANCE_INSUFFICIENT}（{n_sessions} < "
                     f"{PERFORMANCE_THRESHOLD} 个交易日）—— 只给读数："
                     f"不出任何「谁更好」的结论，也不改口径。")
    notes.extend(reasons)

    return {
        "asof": asof, "available": True, "reason": None,
        "start_date": start, "window": [start, nav_dates[-1]],
        "n_sessions": n_sessions, "n_folds": None,
        "sample_gate": gate, "metric_keys": list(METRIC_KEYS),
        "rows": rows,
        "excess_vs_index_300": excess_idx,
        "excess_vs_hold": excess_hold,
        "notes": notes,
    }


def agent_track(conn: sqlite3.Connection, asof: str) -> dict:
    """智能体臂（P37）的页面取数：台账现状 + **直接复用** `engine.agent_block`。

    规格、试错计数、`delta_vs_random` 的口径全部只在一个地方写（`engine`），
    页面不再拼第二套 —— 否则 `paper show` 与页面上会出现两个「试了几版」。
    台账表缺失（老库未前滚）时返回 `available: False` + 原因，**不报 500**。
    """
    if not _has_table(conn, agent_spec.TABLE_DECISIONS):
        return {"available": False,
                "reason": f"库里没有 `{agent_spec.TABLE_DECISIONS}` 表 —— "
                          f"这份库还没前滚到 P37；页面不编数",
                "default_spec": dict(agent_spec.AGENT_DEFAULT_SPEC),
                "change_space": {name: f.range_text()
                                 for name, f in agent_spec.SPEC_SCHEMA.items()}}
    block = agent_block(conn, asof)
    block["available"] = True
    block["default_spec"] = dict(agent_spec.AGENT_DEFAULT_SPEC)
    block["counter_arm_n_reviews"] = agent_spec.ledger_summary(
        conn, ARM_AGENT_RANDOM, asof)["n_reviews"]
    block["reproducibility"] = agent_spec.reproducibility(conn, ARM_AGENT)
    block["present"] = next((a for a in (
        dict(r) for r in conn.execute(
            "SELECT account_id, arm FROM paper_accounts"))
        if str(a["arm"]) == _ARM_AGENT), None) is not None
    return block


def agent_ops(conn: sqlite3.Connection, asof: str, *, accounts: list[dict],
              arms: list[dict], performance: dict,
              paper_trades: list[dict]) -> dict:
    """「**AI 操盘手专节**」的取数（P69 §T4 的三块）。

    | 块 | 回答 | 读数来自 |
    |---|---|---|
    | `records` | 逐日：谁产出了什么、成交几笔、当天净值多少、有没有缺决策 | `paper_agent_decisions` ＋ `paper_nav_daily`（`store.load_nav`）＋ `paper_trades` |
    | `arms` | 每臂：净值 / 累计收益 / 相对大盘 / 相对「不动」/ 回撤 / 持仓 / 成本 / **Δ vs 随机** | **入参** `arms` / `performance`（`track` 已建好的那两块） |
    | `caliber` | 口径史：哪条臂的口径哪天变过、改成什么 | 账户行的预注册 ＋ `plugin_scripts`/`plugin_audit` ＋ spec 台账 |

    ## 入参为什么是「已建好的」而不是自己再查一遍

    `arms` / `performance` 由 `track` **先**算好再传进来：页面上同一个数只能有一份来源。
    自己再查一遍 `paper_nav_daily` 会造出第二个「累计收益」——ADR-023 修正段（D-37）就是
    因为同一页上两个「总收益」差一个常数才被拍板改口径的。所以本函数**一列新口径都不造**：
    - 净值 / 累计收益 / 回撤 / 成本 / 持仓 / 相对大盘 → 入参 `arms`（= `build_report`）；
    - 相对「不动」→ 入参 `performance`（`excess_vs_hold`）；
    - **`delta_vs_random`** → 两条 `cum_return` **相减**（一次减法，与 `excess_vs_hold`
      同一个形状；不是新指标），随机臂自身写 `None` + 「基准自身」；
    - 逐日净值 / 累计收益 → `store.load_nav`（**读库里的列**，不重算）；
    - 决策摘要 → `payload_json` **原样读**（方向 / 标的 / 目标权重 / 理由）；
    - 成交笔数 → 入参 `paper_trades`（`track` 已按 `date <= asof` 取好）；
    - 「有没有缺决策」→ `engine.is_trading_day` ＋ 台账有没有那一行（**不新建判据**）；
    - 通路 A 的版本链 → `plugin_store.list_scripts` / `list_audit` ＋ `script_state`。
    """
    ai_accounts = [a for a in accounts if account_params(a)]
    ai_ids = [str(a["account_id"]) for a in ai_accounts]
    arm_by_id = {str(a["account_id"]): a for a in arms}
    #: 相对「不动」在 `performance` 的**顶层**（一次减法，键 = account_id）。
    hold_excess = performance.get("excess_vs_hold") or {}
    random_id = ARM_AGENT_RANDOM
    random_ret = (arm_by_id.get(random_id) or {}).get("cum_return")
    # 交易日判据每天只算一次（`is_trading_day` 要 load 一遍日历，逐行调会白跑）。
    trading_cache: dict[str, bool] = {}

    def trading_on(date: str) -> bool:
        if date not in trading_cache:
            trading_cache[date] = engine_is_trading_day(conn, date)
        return trading_cache[date]

    rows: list[dict] = []
    for account in ai_accounts:
        aid = str(account["account_id"])
        nav = paper_store.load_nav(conn, aid, asof=asof)
        nav_by_date = {str(r["date"]): r for r in nav}
        decisions = {str(d["asof"]): d for d in agent_decide.load_decisions(conn, aid)}
        trades_by_date: dict[str, int] = {}
        for t in paper_trades:
            if str(t["account_id"]) == aid:
                trades_by_date[str(t["date"])] = trades_by_date.get(str(t["date"]), 0) + 1
        live = (arm_by_id.get(aid) or {}).get("live", True)
        # 「缺决策」只对**台账驱动**的臂成立（`executor=agent_decision`）：通路 A 的
        # 决策不在 `paper_agent_decisions` 里（它在 m2 通路的池子/预测表）。拿台账那条
        # 判据去量通路 A 是**口径错配**，会凭空多报一条「缺决策」。
        ledger_driven = (arm_by_id.get(aid) or {}).get("executor") \
            == EXECUTOR_AGENT_DECISION
        for date in sorted({*nav_by_date, *decisions}, reverse=True):
            if date > asof:
                continue
            decision = decisions.get(date)
            payload = (decision or {}).get("payload") or {}
            in_flight = bool(live) and date >= str(account["start_date"])
            missing = (ledger_driven and in_flight and date not in decisions
                       and trading_on(date))
            nav_row = nav_by_date.get(date) or {}
            rows.append({
                "date": date, "account_id": aid, "live": bool(live),
                "executor": (arm_by_id.get(aid) or {}).get("executor"),
                "ledger_driven": ledger_driven,
                "producer": (None if decision is None else str(decision["model_id"])),
                "agent_kind": (None if decision is None else str(decision["agent_kind"])),
                "decision_id": (None if decision is None
                                else int(decision["decision_id"])),
                "cash_pct": (None if not payload
                             else float(payload.get("cash_pct") or 0.0)),
                "weights": [{"code": str(d["code"]), "side": str(d["side"]),
                             "target_weight_pct": float(d["target_weight_pct"]),
                             "reason": str(d.get("reason") or "")}
                            for d in (payload.get("decisions") or [])],
                "rationale": str(payload.get("rationale") or ""),
                "n_trades": trades_by_date.get(date, 0),
                "nav": nav_row.get("nav"),
                "cum_return": nav_row.get("cum_return"),
                "has_nav": date in nav_by_date,
                # `None` = 这条臂**不适用**台账那条判据（通路 A），不是「没有缺」。
                # 「不适用」与「缺」写同一种值就是口径错配。
                "missing_decision": (bool(missing) if ledger_driven else None),
            })

    table: list[dict] = []
    for account in ai_accounts:
        aid = str(account["account_id"])
        arm = arm_by_id.get(aid) or {}
        cum = arm.get("cum_return")
        delta = (None if aid == random_id or cum is None or random_ret is None
                 else round(cum - random_ret, 6))
        table.append({
            "account_id": aid, "arm": arm.get("arm"), "live": arm.get("live", True),
            "executor": arm.get("executor"), "model_id": arm.get("model_id"),
            "prompt_sha256": arm.get("prompt_sha256"),
            "plugin_hooks": arm.get("plugin_hooks") or [],
            "strategy_version": arm.get("strategy_version"),
            "nav": arm.get("nav"), "cum_return": cum,
            # P80 / D8：净收益(元) 从 `track` 已算好的那一条原样传下去
            # （**不重算**：净值行两列相减只有一个地方做，就是 `_account_entry`）。
            "profit_cny": arm.get("profit_cny"),
            "cum_cost": arm.get("cum_cost"), "max_drawdown": arm.get("max_drawdown"),
            "n_positions": arm.get("n_positions"),
            "latest_nav_date": arm.get("latest_nav_date"),
            "excess_vs_index_300": arm.get("excess_vs_index_300"),
            "excess_vs_hold": hold_excess.get(aid),
            "delta_vs_random": delta,
            "delta_vs_random_note": (
                "基准自身（随机对照臂）" if aid == random_id else
                (None if delta is not None else
                 f"差分**不存在**（不是 0）：`{random_id}` 或这条臂还没有累计收益 —— "
                 f"缺它的时候「选对了」与「多试了几次」分不开（P52 / D-19）")),
        })

    return {
        "asof": asof,
        "arms": table,
        "records": rows,
        "caliber": _caliber(conn, ai_accounts, asof),
        "notes": [
            "三块都**只读**：净值/盈亏读 `paper_nav_daily` 的列或复用 `build_report`，"
            "决策摘要读 `payload_json` 原样 —— 本页不重算任何口径、不给它编理由。",
            "`Δ(AI 臂 − 随机对照)` 是一次**减法**（两个累计收益相减），与「相对大盘」"
            "「相对不动」同款；它不是新指标，也不是成绩单。",
            "样本不足 120 交易日 ⇒ 这些数只是**读数**，不能当结论、更不能据此选臂。",
        ],
    }


def account_params(account: Mapping) -> str | None:
    """账户行的 `params.executor`（**声明字段**原样读，不做任何推断）。"""
    return json.loads(account["params_json"] or "{}").get(EXECUTOR_KEY)


def engine_is_trading_day(conn: sqlite3.Connection, date: str) -> bool:
    """`date` 是不是交易日（`engine.is_trading_day` 的布尔化；判不出 ⇒ `False`）。

    「判不出」不当成缺决策：`is_trading_day` 返回 `None` 时这里给 `False`，
    页面就不会把「日历没覆盖」显示成「那天该有决策却没有」（P56 §2 的原文纪律）。
    """
    from stocklab.paper.engine import is_trading_day

    return bool((is_trading_day(conn, date) or {}).get("is_trading_day"))


def _caliber(conn: sqlite3.Connection, ai_accounts: list[dict],
             asof: str) -> dict:
    """「哪条臂的口径哪天变过、改成什么」（P69 §T4 第三块）。

    三档**并列不合并**，因为它们是三种不同的变更：
    - `llm_versions`：LLM 臂换模型 / 换提示词 ⇒ **开新版本账户**（D-48），
      所以「变过」＝账户行的预注册不同（`model_id` / `prompt_sha256`）；
    - `channel_a_versions`：通路 A 的插桩脚本版本链（`plugin_scripts` ＋
      `plugin_audit` 的 submit / sandbox_pass / approve）；
    - `spec_diffs`：`arm-agent` 家族的 spec 台账（`spec_before → spec_after`
      **逐键对照**）——它是「改纪律数字」，与上面两条不是一回事。
    """
    llm: list[dict] = []
    for account in ai_accounts:
        aid = str(account["account_id"])
        params = json.loads(account["params_json"] or "{}")
        prereg = params.get(PREREGISTERED_KEY) or {}
        if account_params(account) != EXECUTOR_AGENT_DECISION or not prereg:
            continue
        portfolio = agent_decide.portfolio_decisions_only(
            agent_decide.load_decisions(conn, aid))
        llm.append({
            "account_id": aid,
            "model_id": str(prereg.get("model_id") or ""),
            "prompt_sha256": str(prereg.get("prompt_sha256") or ""),
            "created_at": str(account.get("created_at") or ""),
            "live": live_of(params),
            "n_decisions": len(portfolio),
            "first_asof": (str(portfolio[0]["asof"]) if portfolio else None),
            "last_asof": (str(portfolio[-1]["asof"]) if portfolio else None),
        })
    llm.sort(key=lambda r: (r["created_at"], r["account_id"]))

    channel: list[dict] = []
    for account in ai_accounts:
        if account_params(account) != EXECUTOR_CHANNEL_A:
            continue
        aid = str(account["account_id"])
        params = json.loads(account["params_json"] or "{}")
        for hook in (params.get("plugin_hooks") or []):
            versions = []
            for s in plugin_store.list_scripts(conn, plugin_id=str(hook)):
                sid = int(s["script_id"])
                versions.append({
                    "script_id": sid, "version": str(s["version"]),
                    "created_at": str(s["created_at"]),
                    "note": str(s["note"] or ""),
                    "state": plugin_lifecycle.script_state(conn, sid),
                    "events": [{"action": str(e["action"]), "actor": str(e["actor"]),
                                "reason": str(e["reason"] or ""),
                                "created_at": str(e["created_at"])}
                               for e in plugin_store.list_audit(conn, script_id=sid)],
                })
            channel.append({"account_id": aid, "hook": str(hook),
                            "strategy_version": params.get("strategy_version"),
                            "active_script_id": plugin_lifecycle.active_script_id(
                                conn, str(hook)),
                            "versions": versions})

    spec_diffs: list[dict] = []
    for account in ai_accounts:
        aid = str(account["account_id"])
        for d in agent_decide.load_decisions(conn, aid):
            if str(d.get("decision_kind") or "") != DECISION_KIND_SPEC:
                continue
            before, after = d.get("spec_before") or {}, d.get("spec_after") or {}
            changes = [{"key": k, "before": before.get(k), "after": after.get(k)}
                       for k in sorted({*before, *after})
                       if before.get(k) != after.get(k)]
            spec_diffs.append({
                "account_id": aid, "asof": str(d["asof"]),
                "decision_id": int(d["decision_id"]),
                "agent_kind": str(d["agent_kind"]), "model_id": str(d["model_id"]),
                "changes": changes, "rationale": str(d.get("rationale") or ""),
            })
    spec_diffs.sort(key=lambda r: (r["asof"], r["account_id"]))

    return {"llm_versions": llm, "channel_a_versions": channel,
            "spec_diffs": spec_diffs}


def track(conn: sqlite3.Connection, asof: str) -> dict:
    """对照页的全部取数。同一库 + 同一 asof → 同一结果（不含生成时刻）。"""
    accounts = paper_store.load_accounts(conn)
    start = str(accounts[0]["start_date"]) if accounts else PAPER_START_DATE
    ai = ai_evidence(conn, asof)
    agent = agent_track(conn, asof)

    # `date <= asof`：**不取全表 MAX(date)** —— 那会让 `--asof` 的历史截图
    # 显示未来某天的净值（`data._paper` 同一条纪律）。
    session_dates = [str(r["date"]) for r in conn.execute(
        "SELECT DISTINCT date FROM paper_nav_daily WHERE date <= ? ORDER BY date",
        (asof,))]
    if not accounts or not session_dates:
        return _empty(asof, start, ai=ai, agent=agent)

    display_date = session_dates[-1]
    # 锚点（起跑日）永远在横轴上：没有它，「相对大盘」就没有公共起点。
    axis = sorted({start, *session_dates})

    rows = conn.execute(
        "SELECT account_id, date, cum_return FROM paper_nav_daily WHERE date <= ?",
        (asof,))
    by_account: dict[str, dict[str, float]] = {}
    for r in rows:
        by_account.setdefault(str(r["account_id"]), {})[str(r["date"])] = \
            float(r["cum_return"])

    report = build_report(conn, display_date)
    summary = {str(a["account_id"]): a for a in report["accounts"]}
    index_report = report.get("index_300") or {}

    levels = _index_levels(conn, axis)
    base_level = levels.get(start)
    index_points = [None if base_level is None or d not in levels
                    else round(levels[d] / base_level - 1.0, 6) for d in axis]

    arms: list[dict] = []
    for account in accounts:
        aid = str(account["account_id"])
        series = dict(by_account.get(aid, {}))
        anchor = _anchor_cum_return(account)
        if anchor is not None:
            series.setdefault(start, anchor)
        s = summary.get(aid) or {}
        arms.append({
            "account_id": aid, "arm": str(account["arm"]),
            "etf_target_pct": (None if account["etf_target_pct"] is None
                               else float(account["etf_target_pct"])),
            **arm_descriptor(account),
            "anchor_cum_return": anchor,
            "points": [series.get(d) for d in axis],
            "latest_nav_date": max(by_account.get(aid, {}), default=None),
            "has_nav_on_display_date": display_date in by_account.get(aid, {}),
            "nav": s.get("nav"), "cash": s.get("cash"),
            "market_value": s.get("market_value"),
            "net_deposits": s.get("net_deposits"),
            # P80 / D8：**净收益(元)**（= 净值 − 累计净入金）。来源是 `build_report`
            # 的账户条目（`engine._account_entry` 由净值行两列相减），本模块不重算 ——
            # 入金会让「累计收益率」的分母变大，只有净收益(元) 与它并列才能看出
            # 「是赚了还是只是加钱了」。
            "profit_cny": s.get("profit_cny"),
            "cum_return": s.get("cum_return"),
            "max_drawdown": s.get("max_drawdown"),
            "cum_cost": s.get("cum_cost"),
            "excess_vs_index_300": s.get("excess_vs_index_300"),
            "excess_vs_now": None,
            "n_positions": (len(s.get("positions") or {})
                            if "positions" in s else None),
            "n_trades": (len(s.get("trades") or []) if "trades" in s else None),
            "discipline": list(s.get("discipline") or []),
            # P81 / D4：AI 臂才有这个键（`build_report` 只给 AI 臂加）。透传，
            # **不重算** —— 页面上「池内可下手 X/Y 只」与报告里那一行必须是同一个数。
            "tradable_domain": s.get("tradable_domain"),
        })

    now = next((a for a in arms if a["arm"] == _ARM_NOW), None)
    hold = next((a for a in arms if a["arm"] == _ARM_HOLD), None)
    for a in arms:
        if now is None or a["cum_return"] is None or now["cum_return"] is None:
            continue
        if a is not now:
            a["excess_vs_now"] = round(a["cum_return"] - now["cum_return"], 6)

    idx_ret = index_report.get("return_since_start")
    index = {
        "code": INDEX_300_SYMBOL, "label": INDEX_LABEL,
        "base_date": start, "base_level": base_level,
        "base_level_missing": base_level is None,
        "points": index_points,
        "n_missing": sum(1 for v in index_points if v is None),
        "level": index_report.get("level"),
        "price_asof": index_report.get("price_asof"),
        "return_since_start": idx_ret,
        "excess_vs_now": (None if idx_ret is None or now is None
                          or now["cum_return"] is None
                          else round(idx_ret - now["cum_return"], 6)),
        "note": index_report.get("note"),
    }

    real_trades = _real_trades(conn, asof)
    paper_trades = _paper_trades(conn, asof)
    perf = performance(conn, asof)
    return {
        "asof": asof, "available": True, "db_missing": False,
        "start_date": start, "date": display_date,
        "dates": axis, "n_sessions": len(session_dates),
        "arms": arms, "index": index,
        "now_account_id": now["account_id"] if now else None,
        # 「我」现在还等于「什么都不做」吗（账本自起跑日以来有没有新成交）。
        # 这是**事实判断**，不是渲染细节 —— 页面据此决定要不要解释两条线重合。
        "mirror_equals_hold": (None if now is None or hold is None
                               else now["points"] == hold["points"]),
        "comparison": report.get("comparison") or {},
        "real_trades": real_trades,
        "real_trades_after_start": sum(1 for t in real_trades
                                       if str(t["date"]) > start),
        "paper_trades": paper_trades,
        "ai": ai,
        "agent": agent,
        "performance": perf,
        # P69 §T4：AI 操盘手专节。**吃的就是上面这几块已经算好的东西** ——
        # 再查一遍库里同样的列会在同一页造出第二个「累计收益」（ADR-023 D-37）。
        "agent_ops": agent_ops(conn, asof, accounts=accounts, arms=arms,
                              performance=perf, paper_trades=paper_trades),
        "disclosure": list(report.get("disclosure") or []),
        "disclaimer": report.get("disclaimer", ""),
        "sample_note": report.get("sample_note", ""),
    }
