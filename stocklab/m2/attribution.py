"""模块2 误差归因的**候选**计算与扫描（P50 §1 / D-31）。

## 程序只给候选，结论只能人工写

四分类（大盘冲击 / 行业黑天鹅 / 个股突发利空 / 因子失效）**代码判不了**——
这是 D-31 的原文，也是这一站最容易越权的地方：给一个错判样本编一个听起来合理的
标签，比留空危险得多，因为它看起来像一个答案。所以：

- 本模块算出来的每一条都带 **用了哪个信号 + 阈值多少 + 算到哪一步**，标记是「候选」；
- **人工结论**（`attribution_manual`）只由 `m2 attribute confirm` 写 —— 那条路径
  **不在本模块里**（本模块没有 `source='manual'` 的写入分支，见 `scan`）；
- 本模块**一个字节都不写库的逻辑表**：落库调用 `m2/store.py::insert_attribution`
  （m2 家族唯一的写入口），没有任何 UPDATE/DELETE，也不 import 执行/上线路径。

## 信号与阈值全是常量（`stocklab/config/m2_signals.py`）

本模块只**引用**信号名与阈值，绝不重写数值 —— 页面里出现一个 `-0.03`、脚本里出现
另一个，就会出现「候选为什么没出现」只能靠读代码回答的局面。

## 因子失效这一条的已知口径口子（点名）

04 §P1-2 的措辞是「该因子同期横截面 IC／命中率**显著掉档**」。本仓**没有**横截面
IC 的真源（不许新造因子算法），所以这一条用的是**既有命中率读数**
（`m2_forecast_scores` 的 `hit_direction` 聚合，口径与 P48 的 `win_rate` 同一分子分母），
判据是**绝对水平**（≤ `HIT_RATE_FLOOR`）而不是「相对自己的历史的掉档」——
后者需要一条读数历史基线，属于另一轮的授权范围。这条解读写在任务书「未验证口子」里。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence

from stocklab.config import m2_signals as S
from stocklab.m2 import store as m2_store

#: `m2_attributions.source` 的两个取值（**与 schema.sql 的 CHECK 逐字相同**）。
SOURCE_AUTO: str = "auto"      # 程序给的候选
SOURCE_MANUAL: str = "manual"  # 人工写的结论

#: 停牌这条信号的**可达性**（实测结论，见任务书「未验证口子」）：
#: `verify/score.py::score_prediction` 对停牌的目标日判 `SUSPENDED`（不可评分），
#: 所以**错判案例里永远不会出现停牌的目标日** —— 归因候选这条分支因此只有
#: 「由调用方直接喂一条构造样本」时才可达；它在**旁路触发**路径上才是活的
#: （那条路径按标的×日扫全市场，不依赖有没有案例）。
SUSPENDED_REACHABILITY_NOTE = (
    "停牌信号的可达性：`verify/score.py` 对停牌的目标日判 `SUSPENDED`（不可评分）"
    "⇒ 错判案例里不会出现停牌日，本条候选只在**旁路触发**路径上可命中"
    "（`tests/test_p50_attribution.py` 用构造样本单独覆盖这一支）")

#: 行业归属的取数说明（页面/报告直接引用这一串，不各写一份）。
INDUSTRY_SOURCE_NOTE = (
    "行业归属取自既有真源 `financial_reports.industry_name`（取 `notice_date <= 信号日`"
    "的最近一行 —— 该列本身非 PIT，按 PIT 取用是这一层加的约束）；"
    "**不新造行业分类**，也不拿 `sector_daily` 的行业指数顶替个股归属")


def _candidate(*, label: str, signal: str, threshold: float | None,
               at_value: float | None, step: str, detail: dict) -> dict:
    """一条候选（形状唯一：标签 / 信号 / 阈值 / 实测 / 算到哪一步 / 原文）。"""
    signal_text = S.SIGNAL_TEXTS[signal]
    return {
        "label": label, "label_text": S.LABELS[label], "signal": signal,
        "signal_text": signal_text, "threshold": threshold, "at_value": at_value,
        "step": step, "mark": S.CANDIDATE_MARK,
        "text": (f"{S.CANDIDATE_MARK}：{signal_text}"
                 + (f"；阈值 {threshold}" if threshold is not None else "")
                 + (f"；实测 {at_value}" if at_value is not None else "")
                 + f"；{step}"),
        "detail": dict(detail),
    }


# ---------- 单条信号的取数（全部只读，真源都是既有表） ----------


def pct_chg(conn: sqlite3.Connection, code: str,
            date: str) -> tuple[float | None, str]:
    """某标的某日涨跌幅 `(值, 说明)`；取不到 ⇒ `(None, 说明)`（**不写 0**）。

    优先用当日 `pre_close`（交易所口径的**前收盘**，除权日也成立）；缺了才退回
    前一个交易日的收盘价 —— 「除权日按未调整的前一日收盘算跌幅」会把一次
    正常的除权读成一次暴跌。
    """
    row = conn.execute(
        "SELECT close, pre_close FROM bars_daily WHERE code = ? AND date = ?",
        (str(code), str(date))).fetchone()
    if row is None:
        return None, f"{code} 在 {date} 没有 K 线 —— 算不出涨跌幅（不拿 0 顶替）"
    close = float(row["close"])
    pre = row["pre_close"]
    if pre is None or float(pre) <= 0:
        prev = conn.execute(
            "SELECT close FROM bars_daily WHERE code = ? AND date < ?"
            " ORDER BY date DESC LIMIT 1", (str(code), str(date))).fetchone()
        if prev is None:
            return None, f"{code} 在 {date} 之前没有 K 线 —— 没有前收盘价"
        pre = prev["close"]
    if float(pre) <= 0:
        return None, f"{code} 在 {date} 的前收盘价非正 —— 算不出涨跌幅"
    return close / float(pre) - 1.0, f"close {close} / pre {float(pre)} − 1"


def industry_of(conn: sqlite3.Connection, code: str, asof: str) -> str | None:
    """某标的在 `asof` 时点已知的行业（PIT：`notice_date <= asof` 的最近一行）。"""
    row = conn.execute(
        "SELECT industry_name FROM financial_reports WHERE code = ?"
        " AND notice_date <= ? AND industry_name IS NOT NULL"
        " ORDER BY notice_date DESC, report_date DESC LIMIT 1",
        (str(code), str(asof))).fetchone()
    return None if row is None else str(row["industry_name"])


def industry_peers(conn: sqlite3.Connection, code: str, asof: str) -> dict:
    """同行业同日跌破阈值的**只数**（不含本标的：自己跌不算「行业」的事）。"""
    industry = industry_of(conn, code, asof)
    if industry is None:
        return {"industry": None, "n_peers": 0, "peers": [],
                "note": f"`{code}` 在 {asof} 之前没有 `financial_reports.industry_name`"
                        " 行 —— 行业归属取不到，这一条判不了（不猜行业）"}
    codes = [str(r["code"]) for r in conn.execute(
        "SELECT code FROM instruments WHERE type = 'stock' ORDER BY code")]
    peers = []
    for other in codes:
        if other == str(code):
            continue
        if industry_of(conn, other, asof) != industry:
            continue
        value, _ = pct_chg(conn, other, asof)
        if value is not None and value <= S.INDUSTRY_PEER_DROP_THRESHOLD:
            peers.append(other)
    return {"industry": industry, "n_peers": len(peers), "peers": peers,
            "note": f"行业 {industry}：{len(peers)} 只跌破 "
                    f"{S.INDUSTRY_PEER_DROP_THRESHOLD}"}


def corp_action_hits(conn: sqlite3.Connection, code: str,
                     date: str) -> list[dict]:
    """当日除权除息事件里命中**类型白名单**的那些（判据是 `content` 原文的正则匹配）。

    判据用**正则**（模式见 `config/m2_signals.py`）而不是子串：源站原文是
    `10配3股` 这种形态，子串 `配股` 在里面不连续。用 `cqr`（除权日）而不是
    `first_seen`：后者是**入库时刻**，随重采变化 —— 拿它当 PIT 判据，
    同一条历史事件会在每次重采时换一个日子。
    """
    out = []
    for row in conn.execute(
            "SELECT cqr, content FROM corp_actions WHERE code = ? AND cqr = ?",
            (str(code), str(date))):
        content = str(row["content"] or "")
        for pattern in S.CORP_ACTION_TEXT_WHITELIST:
            if re.search(pattern, content):
                out.append({"cqr": str(row["cqr"]), "content": content,
                            "keyword": pattern})
    return out


def data_quality_hits(conn: sqlite3.Connection, code: str,
                      date: str) -> list[dict]:
    """当日该标的命中的 `data_quality` 异常（类型白名单）。"""
    return [{"issue_type": str(r["issue_type"]), "severity": str(r["severity"]),
             "source": str(r["source"]), "detail": str(r["detail"] or "")}
            for r in conn.execute(
                "SELECT * FROM data_quality WHERE code = ? AND date = ?",
                (str(code), str(date)))
            if str(r["issue_type"]) in S.DATA_QUALITY_TYPE_WHITELIST]


def is_suspended(conn: sqlite3.Connection, code: str, date: str) -> bool | None:
    """当日是否停牌；当日没有 K 线行 ⇒ `None`（判不了，与「没停牌」分开）。"""
    row = conn.execute(
        "SELECT is_suspended FROM bars_daily WHERE code = ? AND date = ?",
        (str(code), str(date))).fetchone()
    return None if row is None else bool(int(row["is_suspended"]))


# ---------- 一条错判样本 → 候选清单 ----------


def candidates_for(conn: sqlite3.Connection, *, score: Mapping,
                   hit_rate: Mapping | None = None) -> list[dict]:
    """一条**错判样本** → 四分类的候选清单（可能为空：那就说明「没算出候选」）。

    `hit_rate` = 该案例所属分组（`plugin_id`+`script_version`+`account_id`）的
    P48 读数（`m2_data.forecast_readings` 的一行）—— **由取数层传进来**，
    因为「命中率」的口径真源在 P48 那边，本模块不重算它。
    """
    code = str(score["code"])
    date = str(score["target_date"])
    steps: list[str] = []
    out: list[dict] = []

    steps.append(f"① 读案例（`score_id={score['score_id']}`，标 {code}，"
                 f"决策日 {score['asof_date']} → 目标日 {date}）")

    # 大盘冲击
    index_pct, how = pct_chg(conn, S.INDEX_CODE, date)
    steps.append(f"② 读 {S.INDEX_CODE} 在 {date} 的涨跌幅（{how}）")
    if index_pct is not None and index_pct <= S.MARKET_DROP_THRESHOLD:
        out.append(_candidate(
            label=S.LABEL_MARKET_SHOCK, signal=S.SIGNAL_INDEX_PCT_CHG,
            threshold=S.MARKET_DROP_THRESHOLD, at_value=round(index_pct, 6),
            step=f"⑧ 指数跌幅 {index_pct:+.4%} ≤ {S.MARKET_DROP_THRESHOLD:+.2%} ⇒ 计入候选",
            detail={"index_code": S.INDEX_CODE, "how": how}))

    # 个股突发利空（四条机械信号各自成候选）
    stock_pct, stock_how = pct_chg(conn, code, date)
    steps.append(f"③ 读 {code} 在 {date} 的涨跌幅（{stock_how}）")
    if stock_pct is not None and stock_pct <= S.SINGLE_DAY_DROP_THRESHOLD:
        out.append(_candidate(
            label=S.LABEL_STOCK_NEWS, signal=S.SIGNAL_STOCK_PCT_CHG,
            threshold=S.SINGLE_DAY_DROP_THRESHOLD, at_value=round(stock_pct, 6),
            step=f"⑧ 标的跌幅 {stock_pct:+.4%} ≤ "
                 f"{S.SINGLE_DAY_DROP_THRESHOLD:+.2%} ⇒ 计入候选",
            detail={"how": stock_how}))

    actions = corp_action_hits(conn, code, date)
    steps.append(f"④ 读 `corp_actions` 当日事件，按白名单 "
                 f"{list(S.CORP_ACTION_TEXT_WHITELIST)} 匹配 `content`")
    for hit in actions:
        out.append(_candidate(
            label=S.LABEL_STOCK_NEWS, signal=S.SIGNAL_CORP_ACTION,
            threshold=None, at_value=None,
            step=f"⑧ 事件原文「{hit['content']}」命中关键词「{hit['keyword']}」"
                 " ⇒ 计入候选（是否算重大利空仍要人确认）",
            detail=hit))

    suspended = is_suspended(conn, code, date)
    steps.append(f"⑤ 读 `bars_daily.is_suspended`（{date}）")
    if suspended:
        out.append(_candidate(
            label=S.LABEL_STOCK_NEWS, signal=S.SIGNAL_SUSPENDED,
            threshold=None, at_value=1.0,
            step=("⑧ 当日停牌 ⇒ 计入候选（停牌日没有跌幅可比，这条信号自己成立）；"
                  + SUSPENDED_REACHABILITY_NOTE),
            detail={"is_suspended": 1,
                    "reachability": SUSPENDED_REACHABILITY_NOTE}))

    issues = data_quality_hits(conn, code, date)
    steps.append(f"⑥ 读 `data_quality` 当日异常，白名单 "
                 f"{list(S.DATA_QUALITY_TYPE_WHITELIST)}")
    for issue in issues:
        out.append(_candidate(
            label=S.LABEL_STOCK_NEWS, signal=S.SIGNAL_DATA_QUALITY,
            threshold=None, at_value=None,
            step=f"⑧ 命中异常类型 `{issue['issue_type']}`（{issue['severity']}）"
                 " ⇒ 计入候选（行情本身可能不可信）",
            detail=issue))

    # 行业黑天鹅
    peers = industry_peers(conn, code, date)
    steps.append(f"⑦ 同行业同日跌破 {S.INDUSTRY_PEER_DROP_THRESHOLD} 的只数"
                 f"（{peers['note']}）")
    if peers["industry"] is not None and peers["n_peers"] >= S.INDUSTRY_PEER_MIN:
        out.append(_candidate(
            label=S.LABEL_INDUSTRY_BLACKSWAN, signal=S.SIGNAL_INDUSTRY_PEERS,
            threshold=S.INDUSTRY_PEER_DROP_THRESHOLD,
            at_value=float(peers["n_peers"]),
            step=f"⑧ 同行业 {peers['industry']} 有 {peers['n_peers']} 只跌破 "
                 f"{S.INDUSTRY_PEER_DROP_THRESHOLD}（下限 {S.INDUSTRY_PEER_MIN}）"
                 " ⇒ 计入候选",
            detail=peers))

    # 因子失效（复用 P48 命中率读数，不重算）
    if hit_rate is not None and hit_rate.get("win_rate") is not None \
            and int(hit_rate.get("n_scored") or 0) >= S.HIT_RATE_MIN_OBS:
        rate = float(hit_rate["win_rate"])
        steps.append(f"⑨ 该组同期命中率读数（P48 口径）：{rate:.4f}"
                     f"（n={hit_rate['n_scored']}）")
        if rate <= S.HIT_RATE_FLOOR:
            out.append(_candidate(
                label=S.LABEL_FACTOR_DECAY, signal=S.SIGNAL_HIT_RATE,
                threshold=S.HIT_RATE_FLOOR, at_value=rate,
                step=f"⑩ 命中率 {rate:.4f} ≤ {S.HIT_RATE_FLOOR}"
                     f" 且观测 {hit_rate['n_scored']} ≥ {S.HIT_RATE_MIN_OBS}"
                     " ⇒ 计入候选（口径：绝对水平，见模块 docstring 的口子说明）",
                detail={"n_scored": int(hit_rate["n_scored"]),
                        "plugin_id": hit_rate.get("plugin_id"),
                        "script_version": hit_rate.get("script_version"),
                        "account_id": hit_rate.get("account_id")}))

    for item in out:
        item["steps"] = list(steps)
    return out


# ---------- 扫描（幂等落库：只写 `source='auto'`） ----------


def miss_scores(conn: sqlite3.Connection, asof: str) -> list[dict]:
    """方向判错的分数行（`≤ asof`，按目标日倒序）—— 与 P48 的错判案例同一判据。"""
    rows = [s for s in m2_store.list_scores(conn, asof=asof)
            if s["scorable"] and s["hit_direction"] == 0]
    rows.sort(key=lambda s: (str(s["target_date"]), str(s["code"]),
                             int(s["forecast_id"])), reverse=True)
    return rows


def scan(conn: sqlite3.Connection, *, asof: str,
         readings: Mapping[tuple, Mapping] | None = None,
         now: str) -> dict:
    """给 `≤ asof` 的错判样本算候选并落库（幂等）。**只写 `source='auto'`。**

    `readings` = `{(plugin_id, script_version, account_id): 读数}`，由取数层用
    P48 的 `forecast_readings` 算好传进来（本模块不 import `labweb`：那是取数层，
    反过来依赖会让「谁是哪一层」失效）。
    """
    readings = readings or {}
    counts = {"scanned": 0, "inserted": 0, "already": 0, "no_candidate": 0}
    for score in miss_scores(conn, asof):
        counts["scanned"] += 1
        key = (str(score["plugin_id"]), str(score["script_version"]),
               str(score["account_id"]))
        found = candidates_for(conn, score=score, hit_rate=readings.get(key))
        if not found:
            counts["no_candidate"] += 1
            continue
        for cand in found:
            existing = m2_store.find_attribution(
                conn, score_id=int(score["score_id"]), source=SOURCE_AUTO,
                label=cand["label"], signal=cand["signal"])
            if existing is not None:
                counts["already"] += 1
                continue
            m2_store.insert_attribution(conn, row={
                "score_id": int(score["score_id"]),
                "forecast_id": int(score["forecast_id"]),
                "code": str(score["code"]),
                "signal_date": str(score["target_date"]),
                "source": SOURCE_AUTO, "label": cand["label"],
                "signal": cand["signal"], "threshold": cand["threshold"],
                "at_value": cand["at_value"], "step": cand["step"],
                "text": cand["text"],
                "detail_json": {**cand["detail"], "steps": cand["steps"],
                                "label_text": cand["label_text"]},
            }, now=now, commit=False)
            counts["inserted"] += 1
    conn.commit()
    return {"asof": asof, **counts,
            "note": ("扫描只写**候选**（`source='auto'`）：人工结论 "
                     "(`attribution_manual`) 只能由 `m2 attribute confirm` 写，"
                     "本路径**没有**写它的分支（D-31）")}


def confirm(conn: sqlite3.Connection, *, score_id: int, label: str, reason: str,
            actor: str, now: str, signal: str | None = None) -> dict:
    """人工确认一条结论（`source='manual'`）。**唯一**能写结论的路径。

    未知标签 / 空理由 ⇒ 拒绝（`ValueError`，CLI 转 exit 2）。`signal` 用一个人工
    结论自己的键（`manual`）：结论不是某一条信号推出来的，它是人的判断。
    """
    if label not in S.LABELS:
        raise ValueError(
            f"未知归因标签 {label!r} —— 只能是 {list(S.LABEL_ORDER)} 之一"
            "（四分类是需求原文，不许自造第五类）")
    if not str(reason).strip():
        raise ValueError("确认理由不许为空 —— 归因是结论，结论必须写明依据")
    score = conn.execute(
        "SELECT * FROM m2_forecast_scores WHERE score_id = ?",
        (int(score_id),)).fetchone()
    if score is None:
        raise ValueError(f"没有 score_id={score_id} 的错判样本 —— 先跑 "
                         "`m2 score` / `m2 attribute scan`")
    row = {
        "score_id": int(score_id), "forecast_id": int(score["forecast_id"]),
        "code": str(score["code"]), "signal_date": str(score["target_date"]),
        "source": SOURCE_MANUAL, "label": label,
        "signal": (signal or SOURCE_MANUAL), "threshold": None, "at_value": None,
        "step": "人工确认（不是由某一条信号推出的）：理由见正文",
        "text": f"结论（人工）：{S.LABELS[label]}；理由：{reason}",
        "detail_json": {"actor": actor, "reason": reason},
    }
    m2_store.insert_attribution(conn, row=row, now=now)
    return {"score_id": int(score_id), "label": label, "label_text": S.LABELS[label],
            "actor": actor, "source": SOURCE_MANUAL}


def manual_conclusion(rows: Sequence[Mapping]) -> Mapping | None:
    """一批人工结论行 → **最近那一条**（append-only：改结论 = 再追加一行）。"""
    manual = [r for r in rows if r["source"] == SOURCE_MANUAL]
    return manual[-1] if manual else None
