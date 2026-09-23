"""`m2_a1`（AI 模拟选股）**源文本** —— 首版 v1.0.0。

## 为什么源码是字符串常量

插桩的可执行版本来自**数据库**（`plugin_scripts.source_text`），不是文件。
这里存的是「首次 `plugin submit` 用的初始版本」，写成字符串是为了让测试能
直接 `runtime.load_script(SOURCE, plugin_id="m2_a1")` 验它，不依赖文件系统
（形状照 `stocklab/candidate/builtin/`）。

## 为什么上限写死在源码里

单票 ≤ 25% / 持仓 ≤ 5 只 / 现金 ≥ 10% 是**首版的口径**，不是可调参数：
上限一旦可配置，「今天为什么只买了 3 只」就变成一句没人能复现的话。
要改就得走 `plugin submit` 一个新版本（脚本落库不可变，审计链留痕）——
那正是 D-24「插桩脚本由外部编码 agent 产出、人工 `approve` 上线」想要的路径。

## 选序用模块1 的成品分数

`adj_score` 是候选池成员行里的**调整后分数**（模块1 打分 + 排雷调整后的成品）。
本脚本**不自己造因子** —— 再算一份就等于给「模块1 与模块2 看到的同一个标的
不是同一件事」留了门（`m2/context.py` 的模块 docstring 同款理由）。
"""

#: 持仓只数上限（写死的常量，不是读配置）。
LIMIT_N: int = 5

#: 单票权重上限（%）。
CAP_PCT: float = 25.0

#: 现金下限（%）。目标敞口 = `100 − CASH_FLOOR`，被单票上限截断后其余留给现金。
CASH_FLOOR: float = 10.0

SOURCE: str = '''# m2_a1 —— AI 模拟选股（模块2 通路 A 第 1 步；D-24 / D-33）
#
# 纯函数：只读 ctx，不 import、不做 IO、不取时间、不用随机。
# 两遍同 ctx ⇒ 逐字节同输出（由 tests/test_m2_builtin.py T3 钉住）。
#
# 上限写死在这里，**不是**读配置：可配置的上限会让「今天为什么只买了 3 只」
# 变成一句没人能复现的话。要改就走 plugin submit 一个新版本。
LIMIT_N = 5          # 持仓只数上限
CAP_PCT = 25.0       # 单票权重上限（%）
CASH_FLOOR = 10.0    # 现金下限（%）
POOL_ORDER = ("short", "mid", "long")
POOL_LABEL = {"short": "短期", "mid": "中期", "long": "长期"}


def run(ctx):
    # 1) 三池成员各自按 adj_score 排序（模块1 的成品分数，不另造因子）。
    rows = []
    for pool in POOL_ORDER:
        items = ctx["candidates"].get(pool) or []
        ranked = sorted(items,
                        key=lambda x: (-float(x["adj_score"]), str(x["code"])))
        for i in range(len(ranked)):
            item = ranked[i]
            rows.append({"pool": pool, "rank": i + 1, "item": item,
                         "score": float(item["adj_score"])})
    # 2) 跨池按 adj_score 取前 LIMIT_N；同分按 code 定序（排序键必须是全序，
    #    否则「两遍同 ctx 同输出」会依赖字典/列表的偶然顺序）。
    rows.sort(key=lambda r: (-r["score"], str(r["item"]["code"])))
    picked = rows[:LIMIT_N]

    if not picked:
        # 空池 ⇒ 空仓，必须由 cash_pct=100 表达：一份**空的 picks** 与
        # 「A1 没跑起来」在读数上无法区分（channel_a._weights_items 明文拒绝）。
        return {"picks": [], "cash_pct": 100.0, "schema_version": "1.0.0"}

    # 3) 等权，单票被上限截断；剩下的进现金（于是现金下限自动成立）。
    weight = round(min(CAP_PCT, (100.0 - CASH_FLOOR) / len(picked)), 2)
    picks = []
    for r in picked:
        item = r["item"]
        picks.append({
            "code": str(item["code"]),
            "weight_pct": weight,
            "reason": ("%s池第 %d 名（adj_score=%g）：%s"
                       % (POOL_LABEL[r["pool"]], r["rank"], r["score"],
                          str(item.get("pool_reason") or "候选池未给理由"))),
        })
    cash = round(100.0 - weight * len(picks), 2)
    return {"picks": picks, "cash_pct": cash, "schema_version": "1.0.0"}
'''
