"""模块2 外围信号常量（P50 / D-31 / D-45）——**唯一真源**。

两件事共用一份信号定义，因为它们是**同一条事实**的两个用途：

| 用途 | 口径 | 谁用 |
|---|---|---|
| **归因候选**（D-31） | 由可计算信号推出「候选标签」，人工确认才是结论 | `m2/attribution.py` |
| **事件旁路触发**（D-45） | 同一批机械信号命中 ⇒ 不等验证周期结束即触发一次重打分 | `m2/bypass.py` |

## 为什么必须落在这里

任务书 §1/§4 明令：**候选标签的判据（信号 + 阈值）落成常量（`stocklab/config/`）**，
不许散落在页面/脚本里。散落的后果不是「不好看」——是同一个阈值在页面写 `-0.02`、
在脚本里写 `-0.03`，于是「候选为什么没出现」只能靠读代码回答，而两处代码不一样。

## 哪些不是常量

四分类的**语义归属**（哪条信号落到哪个标签）写在这里，但「某某事件算不算利空」
这类判断**不在这里** —— 那正是 D-31 说「代码判不了」的部分：程序只给候选，
结论由人工写进 `attribution_manual`。
"""

from __future__ import annotations

# `sh000300` 的符号是 `paper/engine.py` 的真源（不在这里再写一遍字面量：
# 两处写死会在改名那天漂开 —— ERROR_DIARY #47 的同一课）。
from stocklab.paper.engine import INDEX_300_SYMBOL

# ---------- 归因四分类（04 §P1-2 原文） ----------

LABEL_MARKET_SHOCK: str = "market_shock"          # 大盘冲击
LABEL_INDUSTRY_BLACKSWAN: str = "industry_blackswan"  # 行业黑天鹅
LABEL_STOCK_NEWS: str = "stock_news"              # 个股突发利空
LABEL_FACTOR_DECAY: str = "factor_decay"          # 因子失效

#: 枚举值的中文名（页面/报告/台账文案引用它，不各写一份）。
LABELS: dict[str, str] = {
    LABEL_MARKET_SHOCK: "大盘冲击",
    LABEL_INDUSTRY_BLACKSWAN: "行业黑天鹅",
    LABEL_STOCK_NEWS: "个股突发利空",
    LABEL_FACTOR_DECAY: "因子失效",
}

#: 展示顺序（**固定**，不是可选项：排序也能成为「挑好看的」的入口）。
LABEL_ORDER: tuple[str, ...] = (LABEL_MARKET_SHOCK, LABEL_INDUSTRY_BLACKSWAN,
                                LABEL_STOCK_NEWS, LABEL_FACTOR_DECAY)

#: 程序给出的候选在页面/报告/台账里的**标记**（D-31：候选不是结论）。
CANDIDATE_MARK: str = "候选"

#: 没有人工结论时结论字段的取值（既有口径：DATA 之外一律 UNDETERMINED）。
#: 它是**展示**用的标记；库里的形状是「没有那一行」（见 `m2_attributions`）。
UNDETERMINED: str = "UNDETERMINED"

# ---------- 信号名（唯一真源：页面、报告、台账都引用这些键） ----------

SIGNAL_INDEX_PCT_CHG: str = "index_pct_chg"
SIGNAL_STOCK_PCT_CHG: str = "stock_pct_chg"
SIGNAL_CORP_ACTION: str = "corp_action"
SIGNAL_SUSPENDED: str = "suspended"
SIGNAL_DATA_QUALITY: str = "data_quality"
SIGNAL_INDUSTRY_PEERS: str = "industry_peers"
SIGNAL_HIT_RATE: str = "hit_rate"

#: 每条信号的**人话原文**：候选必须写明「用了哪个信号」。
#: 页面/报告里出现的就是这一串（不许另写一句更顺口的说法 —— 那样两处会对不上）。
SIGNAL_TEXTS: dict[str, str] = {
    SIGNAL_INDEX_PCT_CHG: f"{INDEX_300_SYMBOL} 当日涨跌幅（`bars_daily` 指数收盘）",
    SIGNAL_STOCK_PCT_CHG: "该标的当日涨跌幅（`bars_daily` 收盘）",
    SIGNAL_CORP_ACTION: "`corp_actions` 除权日的 `content` 命中事件类型白名单",
    SIGNAL_SUSPENDED: "该标的当日 `bars_daily.is_suspended = 1`（停牌）",
    SIGNAL_DATA_QUALITY: "`data_quality` 当日该标的的异常类型命中白名单",
    SIGNAL_INDUSTRY_PEERS: "同行业同日跌破阈值的标的只数",
    SIGNAL_HIT_RATE: "该插桩版本同期方向命中率（`m2_forecast_scores`，P48 口径）",
}

# ---------- 阈值（P50 §1/§4 的判据） ----------

#: 大盘冲击看的那条指数（唯一真源是 `paper/engine.py`，这里只是取个短名字）。
INDEX_CODE: str = INDEX_300_SYMBOL

#: 大盘冲击：指数当日涨跌幅 ≤ 该值（**闭区间**：恰好等于也算 —— 边界可断言）。
MARKET_DROP_THRESHOLD: float = -0.02

#: 个股突发利空（跌幅这一条）：该标的当日涨跌幅 ≤ 该值。
SINGLE_DAY_DROP_THRESHOLD: float = -0.05

#: 行业黑天鹅：同行业同日跌破 `INDUSTRY_PEER_DROP_THRESHOLD` 的标的**只数**下限。
INDUSTRY_PEER_DROP_THRESHOLD: float = -0.03
INDUSTRY_PEER_MIN: int = 2

#: 因子失效：该组同期方向命中率 ≤ 该值 **且** 观测数 ≥ `HIT_RATE_MIN_OBS`。
#: ⚠️ 口径解读（见 `m2/attribution.py` 的 docstring 与任务书「未验证口子」）：
#: 本仓**没有**横截面 IC 的真源，也不许新造因子算法，所以这条用的是**既有命中率读数**
#: （P48 的 `win_rate`）。它是**绝对水平**而不是「相对自己的历史掉档」——
#: 「掉档」需要历史基线，而基线要另存一份读数序列（不在本任务授权范围内）。
HIT_RATE_FLOOR: float = 0.40
HIT_RATE_MIN_OBS: int = 5

# ---------- 白名单（① `corp_actions` 事件类型 ③ `data_quality` 异常类型） ----------

#: 除权除息事件里**算得上重大利空**的那一类：配股（摊薄 + 除权）。
#: `corp_actions` 只装除权除息，没有独立的事件类型列，所以判据是 `content` 原文匹配。
#:
#: ⚠️ 用**正则**而不是子串：源站原文是 `10配3股` 这种形态（见 `schema.sql` 的
#: `content` 注释），子串 `配股` 在 `10配3股` 里**不连续**——P50 首次实现就是这么错的，
#: 被 `test_t2_a_corp_action_in_the_whitelist_yields_a_stock_news_candidate` 判红。
#: 两个模式都要留：`配股`（无数字的写法）与 `配\d+股`（带数字的写法）。
CORP_ACTION_TEXT_WHITELIST: tuple[str, ...] = ("配股", r"配\s*\d+\s*股")

#: `data_quality` 里算「这条行情不可信」的异常类型（取值来自
#: `quality/checks.py::_issue` 的既有枚举，不新造类型）。
DATA_QUALITY_TYPE_WHITELIST: tuple[str, ...] = (
    "ohlc_inconsistent", "non_positive_price", "amount_volume_mismatch",
    "negative_volume", "duplicate_date", "dates_not_increasing",
)

# ---------- 事件旁路触发（D-45） ----------

#: 触发事件写进**既有台账** `system_events` 时用的 `module`（不新造事件表）。
BYPASS_MODULE: str = "m2_bypass"

#: 留痕级别。`warn` = 需要人看一眼，但不是链路故障（链路故障是 `error`）。
BYPASS_LEVEL: str = "warn"

#: 机械信号清单（D-45 原文的三条：① ② ③）。**顺序即报出的顺序**。
BYPASS_KINDS: tuple[str, ...] = (SIGNAL_CORP_ACTION, SIGNAL_STOCK_PCT_CHG,
                                 SIGNAL_DATA_QUALITY)

#: 指纹算法版本。指纹 = `sha256(版本|信号|标的|asof|语义键)`。
#: 改算法要升这个版本号 —— 否则新旧指纹混在一张台账里，读的人分不清。
FINGERPRINT_VERSION: str = "1"
