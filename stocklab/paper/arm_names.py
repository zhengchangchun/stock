"""账户 id → 中文显示名的**唯一真源**（P84 / K7）。

## 为什么要有这个模块

页面与 CLI 上到处是 `arm-agent-ds-v3` 这种机器名 —— 用户 2026-09-26 的原话是
「页面上认不出哪条是哪个」。显示名散落在渲染层各处的后果不是难看，是**同一个账户
在不同页面上有两个名字**（本仓已经吃过一次：ERROR_DIARY 的「同一页两个数」是同一个
病的数字版）。所以名字只在这里定义一次，渲染层一律来查。

## 只改显示，不改身份

`account_id` 是**身份**（台账主键、`params.executor` 的载体、成交与净值的关联键），
一个字都不改。本模块只回答「这条 id 该写成哪几个汉字」。

## 认不出的 id 原样返回，**绝不抛**

新版本账户（`arm-agent-ds-v9`）在映射表里还没有行时，页面**不许 500**、
也不许拿别的账户的名字顶替（`cand_render.STAGE_CN` 同一条规矩）。
家族前缀 `arm-agent-<后缀>` 有一条兜底规则：`AI操盘手·<后缀>` —— 于是新开的
版本账户自动有一个可读名，不必等这里加行。
"""

from __future__ import annotations

#: 家族前缀（与 `paper/config.AGENT_ARM_PREFIX` 同值；本模块不 import config，
#: 是为了让显示层可以被任何模块安全引用而不引入依赖）。
FAMILY_PREFIX = "arm-agent-"

#: 账户 id → `{"name", "short", "kind"}`。**逐字**照 P84 任务书 T1 的表。
#: `name` 给人看、`short` 给图例/窄列、`kind` 给「不靠名字判类」的逻辑
#: （`real` / `hold` / `discipline` / `ai` / `rule` / `random` / `index` / `unknown`）。
ARM_NAMES: dict[str, dict[str, str]] = {
    "arm-agent-ds-v3": {"name": "AI操盘手·三版", "short": "AI三版", "kind": "ai"},
    "arm-agent-ds-v2": {"name": "AI操盘手·二版", "short": "AI二版", "kind": "ai"},
    "arm-agent-ds-v1": {"name": "AI操盘手·一版", "short": "AI一版", "kind": "ai"},
    "arm-agent-v1": {"name": "规则臂·A1 选股", "short": "规则A1", "kind": "rule"},
    "arm-agent": {"name": "智能体臂 · 旧世代", "short": "旧智能体", "kind": "ai"},
    "arm-agent-random": {"name": "随机臂·对照组", "short": "随机", "kind": "random"},
    "arm-hold": {"name": "什么都不做", "short": "不动", "kind": "hold"},
    "arm-now": {"name": "你的实盘镜像", "short": "实盘", "kind": "real"},
    "arm-discipline-05": {"name": "纪律臂·ETF 目标 5%", "short": "纪律5%",
                          "kind": "discipline"},
    "arm-discipline-10": {"name": "纪律臂·ETF 目标 10%", "short": "纪律10%",
                          "kind": "discipline"},
    "arm-discipline-15": {"name": "纪律臂·ETF 目标 15%", "short": "纪律15%",
                          "kind": "discipline"},
    "sh000300": {"name": "沪深300（大盘）", "short": "沪深300", "kind": "index"},
}

#: 阅读顺序（**不是排名**）。页面「我 → 什么都不做 → 纪律三档」的先后由它定，
#: 渲染层照抄 —— 顺序这件事与名字同源，也放在这一份里。
DISPLAY_ORDER: tuple[str, ...] = ("arm-now", "arm-hold", "arm-discipline-05",
                                 "arm-discipline-10", "arm-discipline-15")

#: 未知 kind（认不出的 id）。
UNKNOWN_KIND = "unknown"


def _known(arm: object) -> dict[str, str] | None:
    return ARM_NAMES.get(str(arm))


def display_name(arm: object) -> str:
    """账户 id → 中文显示名。**认不出就原样返回 id**（绝不抛）。

    家族前缀兜底：`arm-agent-<后缀>` 没有映射行时给 `AI操盘手·<后缀>`。
    """
    aid = str(arm)
    row = _known(aid)
    if row is not None:
        return row["name"]
    if aid.startswith(FAMILY_PREFIX):
        return f"AI操盘手·{aid[len(FAMILY_PREFIX):]}"
    return aid


def short_name(arm: object) -> str:
    """账户 id → 短名（图例 / 窄列）。**认不出就原样返回 id**（绝不抛）。"""
    aid = str(arm)
    row = _known(aid)
    if row is not None:
        return row["short"]
    if aid.startswith(FAMILY_PREFIX):
        return f"AI·{aid[len(FAMILY_PREFIX):]}"
    return aid


def arm_kind(arm: object) -> str:
    """账户 id → 类别。**认不出返回 `"unknown"`**（绝不抛）。

    家族前缀兜底一律算 `"ai"` —— 它是「AI 操盘手家族的又一版」，
    不是「认不出的东西」。
    """
    aid = str(arm)
    row = _known(aid)
    if row is not None:
        return row["kind"]
    if aid.startswith(FAMILY_PREFIX):
        return "ai"
    return UNKNOWN_KIND
