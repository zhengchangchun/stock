"""只读配置视图（P50 §2 / D-32）：主干常量 + 当前生效策略参数 + **来源**。

## 三项内容，一个写入口都没有

| 组 | 内容 | 真源 |
|---|---|---|
| 主干常量 | 熔断阈值 / 自评估边界 / 门禁 120 / 账户命名 / 频率 | `config/limits.py`、`m2/config.py`、`verify/report.py` |
| 归因候选判据 | 四分类各自「用了哪个信号 + 阈值多少」 | `config/m2_signals.py` |
| 当前生效参数 | `validation_cycles` **最近一行**的 `params_json` | `store/validation.py` 的既有读函数 |

## 为什么不直接引用那 5 个常量名

ADR-021 的结构保证 3 要求 `labweb/` 对这 5 个主干常量名**零引用**
（`tests/test_p44_constants_guard.py` 用 AST 钉住）。只读配置视图要显示它们的**值**，
所以值走 `m2/selfeval.py::BOUNDARIES` 这个只读视图 —— 窗口开在**值**上，不开在**名**上。

## 为什么每一项都要有 `source`

「这一页显示的是不是真的」这个问题必须能机械地回答。`source` 是 `文件:符号`，
测试会去那个文件里找这个符号（`stocklab/m2/config_view.py` 说谎 = 判红），
页面与报告也只是**显示**它，不做任何二次解释。

## 渲染层不许重新格式化

每一项自带 `display`（已格式化好的那一串）。页面/报告原样输出 —— 两处各自
`f"{v*100:.2f}%"` 的话，改一处就会让页面与报告显示两个数（本项目的经典病）。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.config import m2_signals as S
from stocklab.m2 import attribution as m2_attr
from stocklab.m2 import config as m2_config
from stocklab.m2 import selfeval
from stocklab.store import validation as ledger
from stocklab.verify.report import MIN_DAYS

#: 页面/报告里的组标题（两处引用同一串）。
GROUP_LABELS: dict[str, str] = {
    "limits": "主干常量（AI 不可改：只能改源码或由人工在对话里拍板）",
    "criteria": "归因候选判据（信号 + 阈值：候选由程序给，结论由人工写）",
    "params": "当前生效的策略参数",
}

NOTES: tuple[str, ...] = (
    "只读视图（D-32）：本页与报告**没有任何写入口**，主干常量在网页端不可改 ——"
    "它们只由用户在对话里拍板后人工改源码（05 §业务边界 3）",
    "每一项的 `source` 是 `文件:符号`，指向**代码里的定义处**；"
    "渲染层只显示这一串，不复制数值逻辑",
    "账户命名与决策频率属于「口径标识符」：改名等于换口径，所以它们也在这里列出来",
)


def _fmt(key: str, value: object) -> str:
    """已格式化的显示形态（唯一的一处：页面与报告都原样用它）。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value * 100:.2f}%" if key.endswith(("drawdown", "threshold")) \
            else f"{value}"
    return str(value)


def _item(*, key: str, label: str, value: object, source: str, note: str = "",
          display_unit: str = "") -> dict:
    return {"key": key, "label": label, "value": value,
            "display": _fmt(key, value) + display_unit,
            "source": source, "note": note}


def _limit_items() -> list[dict]:
    """主干常量：值走 `selfeval.BOUNDARIES`（**不引用那 5 个名字**）。"""
    bounds = selfeval.BOUNDARIES
    labels = selfeval.BOUNDARY_LABELS
    return [
        _item(key="circuit_breaker_drawdown",
              label=labels["circuit_breaker_drawdown"], value=
              bounds["circuit_breaker_drawdown"],
              source="stocklab/config/limits.py:CIRCUIT_BREAKER_DRAWDOWN",
              note="验证期内账户净值自峰值回撤 ≥ 该值 ⇒ 熔断（D-27 / D-4）"),
        _item(key="rounds_min", label=labels["rounds_min"],
              value=bounds["rounds_min"],
              source="stocklab/config/limits.py:VALIDATION_ROUNDS_MIN",
              note="越界即拒、**不 clamp**（D-28）"),
        _item(key="rounds_max", label=labels["rounds_max"],
              value=bounds["rounds_max"],
              source="stocklab/config/limits.py:VALIDATION_ROUNDS_MAX",
              note="越界即拒、**不 clamp**（D-28）"),
        _item(key="max_days", label=labels["max_days"], value=bounds["max_days"],
              source="stocklab/config/limits.py:VALIDATION_MAX_DAYS",
              note="单轮/验证期最长天数（D-28）"),
        _item(key="freeze_max_days", label=labels["freeze_max_days"],
              value=bounds["freeze_max_days"],
              source="stocklab/config/limits.py:FREEZE_MAX_DAYS",
              note="策略冻结的最长天数（D-28）"),
        _item(key="sample_gate_threshold", label="样本量门禁（交易日）",
              value=MIN_DAYS,
              source="stocklab/verify/report.py:MIN_DAYS",
              note="`paper_data.PERFORMANCE_THRESHOLD` 与 m2 的门禁**同源**引用它"
                   "（不许另写一个 120）"),
        _item(key="account_prefix", label="通路 A 账户前缀",
              value=m2_config.ACCOUNT_PREFIX,
              source="stocklab/m2/config.py:ACCOUNT_PREFIX",
              note="策略版本账户 = `arm-agent-<版本>`（D-35；每版本一行 + 独立 NAV，D-26）"),
        _item(key="mirror_account", label="通路 B 镜像账户",
              value=m2_config.MIRROR_ACCOUNT,
              source="stocklab/m2/config.py:MIRROR_ACCOUNT",
              note="复用既有 `arm-now`（D-25）：不新建第二套镜像代码"),
        _item(key="executor_channel_a", label="通路 A 的 `executor` 声明",
              value=f"{m2_config.EXECUTOR_KEY}={m2_config.EXECUTOR_CHANNEL_A}",
              source="stocklab/m2/config.py:EXECUTOR_CHANNEL_A",
              note="`paper step` 据此让出该账户的日终（谁落净值是显式字段，不靠前缀猜）"),
        _item(key="decision_cadence", label="AI 臂决策频率",
              value=m2_config.DECISION_CADENCE,
              source="stocklab/m2/config.py:DECISION_CADENCE",
              note="每交易日一次（D-34）；与纪律臂的再平衡节奏刻意不统一（D-40）"),
        _item(key="case_limit", label="错判案例条数上限",
              value=m2_config.CASE_LIMIT,
              source="stocklab/m2/config.py:CASE_LIMIT",
              note="**不是可选参数**：P48 §2 明令没有「挑好看的」筛选参数"),
    ]


def _criteria_items() -> list[dict]:
    """归因候选判据：四分类各自「信号 + 阈值」（真源 `config/m2_signals.py`）。"""
    by_label: dict[str, list[str]] = {}
    pairs = (
        (S.LABEL_MARKET_SHOCK, S.SIGNAL_INDEX_PCT_CHG, S.MARKET_DROP_THRESHOLD),
        (S.LABEL_STOCK_NEWS, S.SIGNAL_STOCK_PCT_CHG, S.SINGLE_DAY_DROP_THRESHOLD),
        (S.LABEL_STOCK_NEWS, S.SIGNAL_CORP_ACTION, None),
        (S.LABEL_STOCK_NEWS, S.SIGNAL_SUSPENDED, None),
        (S.LABEL_STOCK_NEWS, S.SIGNAL_DATA_QUALITY, None),
        (S.LABEL_INDUSTRY_BLACKSWAN, S.SIGNAL_INDUSTRY_PEERS,
         S.INDUSTRY_PEER_DROP_THRESHOLD),
        (S.LABEL_FACTOR_DECAY, S.SIGNAL_HIT_RATE, S.HIT_RATE_FLOOR),
    )
    out = []
    for label, signal, threshold in pairs:
        text = f"{S.LABELS[label]} ⇐ {S.SIGNAL_TEXTS[signal]}"
        if threshold is not None:
            text += f"（阈值 {threshold}）"
        if signal == S.SIGNAL_INDUSTRY_PEERS:
            text += f"；只数下限 {S.INDUSTRY_PEER_MIN}"
        if signal == S.SIGNAL_HIT_RATE:
            text += f"；观测数下限 {S.HIT_RATE_MIN_OBS}"
        by_label.setdefault(label, []).append(text)
        out.append({"key": f"{label}.{signal}", "label": text,
                    "value": threshold, "display": text,
                    "source": "stocklab/config/m2_signals.py:" + {
                        S.SIGNAL_INDEX_PCT_CHG: "MARKET_DROP_THRESHOLD",
                        S.SIGNAL_STOCK_PCT_CHG: "SINGLE_DAY_DROP_THRESHOLD",
                        S.SIGNAL_CORP_ACTION: "CORP_ACTION_TEXT_WHITELIST",
                        S.SIGNAL_SUSPENDED: "SIGNAL_SUSPENDED",
                        S.SIGNAL_DATA_QUALITY: "DATA_QUALITY_TYPE_WHITELIST",
                        S.SIGNAL_INDUSTRY_PEERS: "INDUSTRY_PEER_DROP_THRESHOLD",
                        S.SIGNAL_HIT_RATE: "HIT_RATE_FLOOR",
                    }[signal],
                    "note": (f"候选标记 = 「{S.CANDIDATE_MARK}」；"
                             "结论只能由人工确认（D-31）"
                             + ("；" + m2_attr.SUSPENDED_REACHABILITY_NOTE
                                if signal == S.SIGNAL_SUSPENDED else ""))})
    return out


def _effective_params(conn: sqlite3.Connection) -> dict:
    """`validation_cycles` **最近一行**的 `params_json`（当前生效的那一组）。"""
    cycles = ledger.list_cycles(conn)
    if not cycles:
        return {"params": None, "params_source": None, "params_criteria": None,
                "params_reason": ("库里还没有验证周期 —— 当前没有「生效的参数集」"
                                  "（`validation_cycles` 一行都没有）。"
                                  "**不是空参数集**，是这一格无从谈起")}
    cycle = cycles[-1]
    try:
        params = json.loads(cycle["params_json"] or "{}")
    except ValueError:
        params = None
    return {
        "params": params,
        "params_source": f"validation_cycles#{int(cycle['cycle_id'])}.params_json",
        "params_criteria": str(cycle["criteria_text"]),
        "params_reason": None,
        "params_cycle": {"cycle_id": int(cycle["cycle_id"]),
                         "script_id": int(cycle["script_id"]),
                         "account_id": str(cycle["account_id"]),
                         "start_date": str(cycle["start_date"]),
                         "created_at": str(cycle["created_at"])},
    }


def items(conn: sqlite3.Connection) -> dict:
    """配置视图的全部内容（页面与报告**同一个**取数函数）。"""
    effective = _effective_params(conn)
    return {
        "groups": [
            {"key": "limits", "label": GROUP_LABELS["limits"],
             "items": _limit_items()},
            {"key": "criteria", "label": GROUP_LABELS["criteria"],
             "items": _criteria_items()},
        ],
        **effective,
        "notes": list(NOTES),
        "query": "只读：本函数与它的调用方不含任何写语句（增 / 删 / 改一个都没有）",
    }
