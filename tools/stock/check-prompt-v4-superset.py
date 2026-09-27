#!/usr/bin/env python3
"""P86 / G1 自检：`trader-v4.txt` 是不是 `trader-v3.txt` 的**严格超集**。

对照臂（v3 无复盘 / v4 有复盘）只能有**一个自变量** —— 提示词差异只许出现在四处：
① 顶部 v4 说明 ② 「第一步：复盘」整节 ③ 「怎么想」第 1 步 ④ 「输出格式」的 `review` 键。
本脚本做**逆向剥离**：把 v4 的这四处改回 v3 的写法，结果必须**逐字节等于** v3。

跑法：`.venv/bin/python tools/stock/check-prompt-v4-superset.py`
退出码：0 = 是超集；1 = 决策段与 v3 不一致（提示词已漂移 ⇒ 对照不再单变量）。
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent / "prompts"
V3 = PROMPTS / "trader-v3.txt"
V4 = PROMPTS / "trader-v4.txt"

ANCHOR_TOP = ("每个交易日收盘后，你收到一份 **PIT 上下文**（只含 ≤ 当日的信息），"
              "并给出**一次**当日的目标组合。\n")

NOTE = """> **v4 相对 v3 的唯一变化：先复盘、再决策。**
> 你必须在**同一次回答**里先交一份**复盘**（`review`），再交当日的目标组合 —— 复盘写的每一句话
> 都**必须带一条能在库里查到的证据**（`evidence`）：读数**照抄上下文**，不许自己算、不许估。
> 格式违规或证据核不到，当天这条复盘不落库（**不阻断**你的决策）。
>
> 复盘会进 append-only 台账，并作为「你自己的历史」回注到**以后**的上下文里
> （原话进 `own_history.recent_reviews`，反复出现、已被确认的教训进 `own_history.facts`）。
>
> 其余一切（读字段而不是心算、本金取实值、考核目标是扣成本后的净收益）与 v3 完全一致。
"""

REVIEW_SECTION = """## 第一步：复盘（`review`）

**在给组合之前**，先对上一段做一次复盘：看 `own_history`（你自己的净值序列、成交、最近决策、
历史复盘与已确认教训 `facts`）与 `market`（市场广度/估值/资金流/指数），写出若干条**有证据的观察**。

**证据只有四种，且每一句都必须在库里核得到**（写入口逐条回库核对，对不上整条复盘被拒）：

| `evidence.kind` | 形如 | 规则 |
|---|---|---|
| `decision` | `{"kind":"decision","decision_id":4}` | `decision_id` 从 `own_history.decisions[].decision_id` 照抄（只含**早于今天**的决策） |
| `trade` | `{"kind":"trade","trade_id":16}` | `trade_id` 从 `own_history.trades[].trade_id` 照抄（只含 ≤ 今天的成交） |
| `metric` | `{"kind":"metric","metric":"cum_return","date":"2026-09-24","value":-0.0216}` | `metric` ∈ `nav` / `cash` / `market_value` / `cum_return` / `cum_cost` / `drawdown` / `net_deposits`；读数只从 `own_history.nav_series[]` 里抄（`date` 与 `value` 都照抄那一行） |
| `market` | `{"kind":"market","field":"breadth.up_ratio","date":"2026-09-24","value":0.1372}` | `field` 是 `market` 块里的字段路径，形如 `index.sh000300.level` / `breadth.up_ratio` / `valuation.pe_ttm_median` / `money_flow.main_net_sum_yi`；`value` **照抄**块里的值 |

铁律：

- **读数逐字照抄上下文**，不许自己算、不许估、不许改精度（写 `-0.022` 而库里是 `-0.0216` 会被判不一致）。
- `null` 的字段**不许引用**（没有值就没有证据）。
- **不许引用 `asof` 之后的行**（`decision` / `trade` / `metric` 的日期必须 ≤ 今天）。
- `metric` 的历史读数只出现在 `own_history.nav_series` 里（`nav` / `cum_return` / `drawdown` /
  `cum_cost` / `net_deposits` 都抄得到）；`cash` 与 `market_value` 的历史序列**没有**放进上下文，
  所以不要拿它们当证据。
- 历史还空着的时候（你的第一条决策）：`items` 只用 `market` 类证据写 1–3 条对当天市场的观察，
  `lessons` 写 `[]` 也可以。
- 写 1–6 条就停：只写**事实与自我批评**，不写口号。

**`lessons`（教训，跨天累积）**：

- `key`：`[a-z0-9_]{3,48}` 的英文小写下划线短语（例：`lot_floor_small_account`、`cash_buffer_too_thin`）。
  **同一个道理必须复用同一个 key** —— 同一个 key 出现在 **≥2 个不同交易日**的复盘里之后，系统才会
  把它认定为「已确认的事实」，并在以后的上下文里以 `own_history.facts` 回注给你。
  每换一次写法就等于从头再来一次。
- `kind`：`"fact"`（客观事实）或 `"habit"`（我的行为倾向）。
- `text`：≤ 200 字的一句话。**同一个 key 在不同日子写不同的话会被标成冲突**，所以措辞要稳定。
- 写 0–4 条；没有新教训就写空数组。

⚠️ **复盘只写文字与读数。** 不许在复盘里提出「改公式 / 改参数 / 改阈值 / 调权重上下限」——
你的决策空间只有下面的「选标的 + 权重 + 现金」。复盘是用来长记性的，不是用来改规则的。

"""

OLD_HOWTO_1 = ("1. 先看 `objective`（为了什么）与 `account` / `tradability`（我现在有什么）；\n"
               "2. 看 `pool.pools`")
NEW_HOWTO_1 = ("1. **先复盘**（见上一节）：读 `own_history`（净值/成交/决策/历史复盘与 `facts`）＋ `market`，\n"
               "   写带证据的 `items` 与 `lessons`；\n"
               "2. 先看 `objective`（为了什么）与 `account` / `tradability`（我现在有什么）；\n"
               "3. 看 `pool.pools`")
RENUMBER = [("3. 决定**总风险敞口**", "4. 决定**总风险敞口**"),
            ("4. 在可下手的标的里分配权重", "5. 在可下手的标的里分配权重"),
            ("5. **逐个核对权重**", "6. **逐个核对权重**"),
            ("6. 检查你的数字", "7. 检查你的数字")]

OLD_OUT_HEAD = "```json\n{\n  \"decisions\": ["
NEW_OUT_HEAD = """```json
{
  "review": {
    "items": [
      {"claim": "今天净值回撤 1.2%，全部来自已持的银行腿，不是因为新买入",
       "evidence": {"kind": "metric", "metric": "drawdown", "date": "2026-09-24", "value": 0.0123}},
      {"claim": "全市场上涨家数占比只有 0.1372，是普跌日，我的高仓位吃了亏",
       "evidence": {"kind": "market", "field": "breadth.up_ratio", "date": "2026-09-24", "value": 0.1372}}
    ],
    "lessons": [
      {"key": "cash_buffer_too_thin", "kind": "habit",
       "text": "普跌日现金留太少会放大回撤；但我的问题不是仓位大，是加仓时总是先买最贵的那只，一手就吃掉大半现金。"}
    ]
  },
  "decisions": ["""

OLD_BULLET_DEC = "- `decisions` 可以是空数组（= 全现金）；"
NEW_BULLET_DEC = ("- `review.items` 至少 1 条；`review.lessons` 可以是空数组；\n"
                  "- `decisions` 可以是空数组（= 全现金）；")
OLD_BULLET_NUM = "- 数字用两位小数以内；`cash_pct` 用数字不用字符串；"
NEW_BULLET_NUM = ("- 数字用两位小数以内（`evidence.value` 例外 —— **必须原样照抄**）；\n"
                  "- `cash_pct` 用数字不用字符串；")


def strip_deltas(v4: str) -> str:
    """把 v4 的四处差异逆向改回 v3 的写法 —— 结果应当**逐字节等于** v3。"""
    out = v4.replace(ANCHOR_TOP + "\n" + NOTE, ANCHOR_TOP, 1)
    out = out.replace(REVIEW_SECTION, "", 1)
    out = out.replace(NEW_HOWTO_1, OLD_HOWTO_1, 1)
    for old, new in RENUMBER:
        out = out.replace(new, old, 1)
    out = out.replace(NEW_OUT_HEAD, OLD_OUT_HEAD, 1)
    out = out.replace(NEW_BULLET_DEC, OLD_BULLET_DEC, 1)
    out = out.replace(NEW_BULLET_NUM, OLD_BULLET_NUM, 1)
    return out


def main() -> int:
    v3 = V3.read_text(encoding="utf-8")
    v4 = V4.read_text(encoding="utf-8")
    stripped = strip_deltas(v4)
    if stripped == v3:
        print("G1 OK：剥离四处差异后与 trader-v3.txt 逐字节相同（v4 是 v3 的严格超集）")
        return 0
    print("G1 FAIL：决策段与 trader-v3.txt 不一致 —— 对照不再单变量。差异：")
    for line in list(difflib.unified_diff(v3.splitlines(), stripped.splitlines(),
                                          "trader-v3.txt", "v4-stripped",
                                          lineterm=""))[:200]:
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
