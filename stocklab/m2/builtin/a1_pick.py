"""`m2_a1`（AI 模拟选股）**源文本** —— v1.0.3（扣掉「不在 picks 里的存量持仓」）。

## v1.0.3 修的是什么：v1.0.2 的「买得起」按总资产算，而钱锁在存量持仓里

真库 2026-09-23 的实测（P64 §7.3）：账户 `total_assets = 19,567`，其中
**¥8,247（42.15%）是 `000333` 这笔存量持仓**，可用现金只有 ¥11,320。
`000333` 不在 5 条 pick 里（排名不够前），A2 也不卖它（它还在池内、无止损止盈）
⇒ 那 42% **既不被重算、也不被释放**。于是 5 只 × 18% = ¥17,610 的目标敞口
> ¥11,320 可用现金 ⇒ 成交后 `paper_nav_daily.cash = −4,490.01`（总资产的 −23%）。
**这就是杠杆。**

修法：Σw 的上限先扣掉「**不在本轮 picks 里**的存量持仓当前占比」：

```
Σw ≤ (100 − CASH_FLOOR) − reserved        （reserved 的算法见下）
w   = min(CAP_PCT, 该上限 / n)            等权
cash_pct = 100 − w × len(picks)           逐位自洽
```

语义是「A1 只能部署**真正空闲的**钱」；分母仍是 `total_assets`（组合口径）——
于是**总敞口自然回到 ≤ 100%**，同时保住组合视角（存量持仓仍计入总仓位）。
`ctx["holdings"]` 每项已经带 `weight_pct`（`m2/context.py::holdings_ctx`）⇒
不需要新增契约字段，直接求和就行。

## ⚠️ v1.0.2 的「整手取整 ⇒ 0 股」只是**偶然**闸门，已被执行层的显式闸门取代

P64 的教训（ERROR_DIARY #73）：v1.0.1 把「按目标市值算股数、整手向下取整
⇒ 0 股」这件**副产物**当成了现金闸门（只成交 3 笔 ⇒ 现金仍 +¥2,055）。P64
修掉「买不起一手」= **拆掉**那道偶然闸门，当天就把一个此前看不见的杠杆缺陷
兑现成了真实持仓。⇒ 从 P65 起，资金闸门是
`paper/agent_decide.py::CashShortfall`（**执行层**、fail-closed、具名
`code="cash"`、整轮拒绝零写入），**不再依赖任何取整副产物**：本脚本的口径
就算再错一次，执行层也会整轮拒绝，而不是透支。

## 本版本的口径（确定性、纯函数、**至多两轮**）

1. `reserved0 = Σ over ctx["holdings"] of weight_pct` —— **保守**：先把全部
   存量当占用，于是这一步**不是循环依赖**；
2. 对 `n` 从 `min(LIMIT_N, 去重后只数)` 往下（沿用 v1.0.2 的下降式循环）：
   `w0 = round(min(CAP_PCT, ((100 − CASH_FLOOR) − reserved0) / n), 2)`，取在
   `w0` 下买得起的前 `n` 只；凑不齐就继续往下试；
3. **一次细化**：`reserved1 = Σ over holdings whose code ∉ picks`（picks 里的
   存量**本来就持有**，不该再占它的额度）；`w1 = round(min(CAP_PCT,
   ((100 − CASH_FLOOR) − reserved1) / n), 2)`。若 `w1 > w0` ⇒ 用 `w1` 重跑
   一次 affordability 并取最终 `(S, w1)`（w 变大只会让更多标的买得起 ⇒
   单调、必然收敛）；否则取 `(S, w0)`。**最多两轮，不迭代到不动点**。
4. 退化口径：`total_assets is None`、或任一 holding 缺 `weight_pct`
   （探针 ctx / 无 PIT 价）⇒ `reserved = 0.0`，**行为与 v1.0.2 逐位相同**；
   `holdings` 为空 ⇒ 与 **v1.0.1** 逐位相同。

`LIMIT_N` / `CAP_PCT` / `CASH_FLOOR` / `LOT` 四个常量的数值一个字都没改，
去重（v1.0.1）也不动。

## v1.0.2 修的是什么：选出来的标的在**这个账户上做不到**

v1.0.1 只按 `adj_score` 选股，不看账户买不买得起。¥19,547 的模拟盘上它选了
茅台（一手 ¥125,124）：目标市值 ¥3,518 连一手都不够 ⇒ 0 股、无订单。于是
「计划说 5 只 18%、现金 10%」落地成「成交 3 笔、现金 ~40%」—— 计划的
等权 18% × 5 只在 ¥19,547 上**根本做不到**，而报告里计划与执行长得一样。

修法：按**试算权重**算目标市值，不足**一手市值**（`close × LOT`）的标的不选，
沿去重后的排序继续往下取，只数与权重 `w = min(CAP_PCT, (100 − CASH_FLOOR)/n)`
**自洽**（从 `n = LIMIT_N` 往下试，取最大的能凑齐 `n` 只的解）。

**「不知道」≠「买不起」**：`total_assets is None`（探针 ctx）或收盘价缺失时
**不过滤**（v1.0.3 同理：「不知道存量占比」⇒ `reserved = 0`，不当作占满）。
被排除的标的在契约载荷里**没有位置**（`picks` 每条只能
`{code, weight_pct, reason}`）⇒ 只能从「只数少于 5」反推。

## 为什么源码是字符串常量

插桩的可执行版本来自**数据库**（`plugin_scripts.source_text`），不是文件。
这里存的是「`plugin submit` 用的种子版本」，写成字符串是为了让测试能
直接 `runtime.load_script(SOURCE, plugin_id="m2_a1")` 验它，不依赖文件系统
（形状照 `stocklab/candidate/builtin/`）。

## v1.0.1 修的是什么：v1.0.0 的选股清单**可重复**

模块1 的三池**不是互斥划分** —— 真库 2026-09-23 的池快照里长期池 5 只
**全部**也在中池里，19 个槽只对应 11 只。跨池重复是**常态**，不是数据事故。

v1.0.0 的 `run()` 把三池的排序结果打平后直接 `rows[:LIMIT_N]`：同一个 code
在两只池里各占一个槽，就产出**两条同 code 的 pick**。契约校验只查
`Σweight + cash == 100`、不查 code 唯一性，于是这份清单**校验全过**；执行层
把两条当两条独立订单，`rule_citation` 对买入侧又是同一个常量串 ⇒ 两条
`(code, side, qty, rule_citation)` 相同的成交行撞 `paper_trades` 的 append-only
唯一键，**整条 `m2 daily` 以 `sqlite3.IntegrityError` 崩掉**（不是具名拒绝）。

修法只有一句：跨池排序后**同一 code 只保留第一条**（= 它排名最好的那只池），
再取前 `LIMIT_N`。权重公式与 `LIMIT_N` / `CAP_PCT` / `CASH_FLOOR` 一个字都没改。
真库 `script_id=12` 存的是 v1.0.1 文本（现役）；P64 的 v1.0.2（`script_id=13`）
**只存在于副本、从未上线**（它的第四条判据实测不成立，见 P64 §7.3）。
下面 `SOURCE` 是 **v1.0.3** 的种子文本，**不改库** —— 上线走 `plugin submit`
＋ 人工 `approve`（D-24）。

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

#: A 股 1 手 = 100 股。**与账户 `paper_accounts.params_json.lot` 是同一条约定**
#: （账户那边也是 100）；插桩的 ctx 里看不到账户参数，所以只能写死在这里
#: —— 与「上限写死」是同一条约定。A 股没有半手：买不起一手就是买不起。
LOT: int = 100

SOURCE: str = '''# m2_a1 —— AI 模拟选股（模块2 通路 A 第 1 步；D-24 / D-33）
# 本站是**源版本 v1.0.3** 的种子文本：在 v1.0.2（只选买得起一手的标的）之上，
# Σw 的上限先扣掉「**不在本轮 picks 里**的存量持仓当前占比」。
#
# v1.0.2 按 total_assets × w 定目标敞口，而钱锁在存量持仓里：真库 2026-09-23
# 的账户 total_assets = 19,567，其中 ¥8,247（42.15%）是 000333 这笔存量持仓、
# 可用现金只有 ¥11,320。000333 不在 5 条 pick 里（排名不够前），A2 也不卖它
# （它还在池内、无止损止盈）⇒ 那 42% 既不被重算、也不被释放；5 只 × 18% =
# ¥17,610 的目标敞口 > ¥11,320 可用现金 ⇒ 成交后现金 −4,490.01（总资产 −23%）。
# 修法：Σw ≤ (100 − CASH_FLOOR) − reserved，等权 w = min(CAP_PCT, 该上限 / n)，
# cash_pct = 100 − w × len(picks)（逐位自洽）⇒ 总敞口自然回到 ≤ 100%。
#
# ⚠️ v1.0.2 的「整手取整 ⇒ 0 股」只是**偶然**闸门，已被执行层的显式闸门
# （paper/agent_decide.py::CashShortfall，具名 code="cash"、整轮拒绝零写入）
# 取代 —— 资金闸门不许依赖任何取整副产物（ERROR_DIARY #73：P64 拆掉那道偶然
# 闸门，当天就把一个此前看不见的杠杆缺陷兑现成了真实持仓）。
#
# 存量占比的算法（确定性、纯函数、至多两轮）：
#   reserved0 = Σ over ctx["holdings"] of weight_pct（保守：全部存量先当占用）；
#   对 n 从 min(LIMIT_N, 去重后只数) 往下：w0 = round(min(CAP_PCT,
#   ((100 − CASH_FLOOR) − reserved0)/n), 2)，取 w0 下买得起的前 n 只；
#   一次细化：reserved1 = Σ over holdings whose code ∉ picks 的 weight_pct
#   （picks 里的存量本来就持有，不占额度）⇒ w1 = round(min(CAP_PCT,
#   ((100 − CASH_FLOOR) − reserved1)/n), 2)；若 w1 > w0 就用 w1 重跑一次
#   affordability 取最终解。最多两轮，不迭代到不动点。
#   退化：total_assets 为 None 或任一 holding 缺 weight_pct ⇒ reserved = 0
#   （与 v1.0.2 逐位相同）；holdings 为空 ⇒ 与 v1.0.1 逐位相同。
#
# 纯函数：只读 ctx，不 import、不做 IO、不取时钟、不用随机。
# 两遍同 ctx ⇒ 逐字节同输出（由 tests/test_m2_builtin.py T3 钉住）。
#
# 上限写死在这里，**不是**读配置：可配置的上限会让「今天为什么只买了 3 只」
# 变成一句没人能复现的话。要改就走 plugin submit 一个新版本。
LIMIT_N = 5          # 持仓只数上限
CAP_PCT = 25.0       # 单票权重上限（%）
CASH_FLOOR = 10.0    # 现金下限（%）
# A 股 1 手 = 100 股，**与账户 paper_accounts.params_json.lot 是同一条约定**
# （账户那边也是 100）。插桩的 ctx 里看不到账户参数 ⇒ 只能写死在这里，
# 与「上限写死」同一条约定。A 股没有半手：买不起一手就是买不起。
LOT = 100
POOL_ORDER = ("short", "mid", "long")
POOL_LABEL = {"short": "短期", "mid": "中期", "long": "长期"}


def _affordable(item, weight, total_assets):
    # 买得起 ⟺ 目标市值 ≥ 一手市值：total_assets × weight/100 ≥ close × LOT。
    # **「不知道」≠「买不起」**（规则 4）：账户总资产未知（探针 ctx）或这只
    # 标的没有 PIT 收盘价时**不过滤** —— 与 v1.0.1 的行为逐位相同。
    if total_assets is None:
        return True
    close = item.get("close")
    if close is None:
        return True
    return float(total_assets) * float(weight) / 100.0 >= float(close) * LOT


def _reserved_pct(holdings, total_assets, exclude_codes):
    # 「**不在** exclude_codes 里」的存量持仓占比之和（占总资产 %）。
    # 退化口径（与 v1.0.2 逐位相同）：「账户总资产未知」或「任一 holding
    # 缺 weight_pct」（探针 ctx / 没有 PIT 价）⇒ **0.0**，退回「存量不占额度」
    # 的老口径 —— 「不知道」≠「存量把额度占满了」（与 _affordable 同一条规则 4）。
    if total_assets is None:
        return 0.0
    total = 0.0
    for item in holdings or []:
        weight = item.get("weight_pct")
        if weight is None:
            return 0.0
        if exclude_codes is not None and str(item.get("code")) in exclude_codes:
            continue
        total += float(weight)
    return total


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
    # 2) 跨池**去重后**按 adj_score 取前 LIMIT_N；同分按 code 定序（排序键必须是
    #    全序，否则「两遍同 ctx 同输出」会依赖字典/列表的偶然顺序）。
    #
    #    为什么必须先去重：模块1 的三池**不是互斥划分**（真库 2026-09-23 的快照里
    #    长期池 5 只全在中池里，19 个槽只对应 11 只），同一 code 跨池重复是**常态**。
    #    v1.0.0 在这里直接 rows[:LIMIT_N]，同一个 code 在两只池里各占一个槽就产出
    #    两条 pick；契约只查权重合计、不查 code 唯一性 ⇒ 整份清单校验全过，而执行层
    #    把它当两条独立订单，撞 paper_trades 的 append-only 唯一键、整条 `m2 daily`
    #    以 IntegrityError 崩掉（P63 / ERROR_DIARY #72）。
    #    同一 code 只保留**排序里的第一条**，即它排名最好的那只池。
    rows.sort(key=lambda r: (-r["score"], str(r["item"]["code"])))
    seen = set()
    deduped = []
    for r in rows:
        code = str(r["item"]["code"])
        if code in seen:
            continue
        seen.add(code)
        deduped.append(r)

    # 3) 只数与权重**自洽**：从 n = min(LIMIT_N, 去重后只数) 往下试，取**最大的、
    #    能在该 n 的等权权重下凑齐 n 只买得起的**解。n 越小 ⇒ 权重越大 ⇒ 越买得
    #    起（n=1 时权重 = CAP_PCT 是上限），所以「往下试」必然收敛。
    #
    #    v1.0.3 在权重上先扣掉「不在本轮 picks 里的存量持仓占比」：那些钱锁在
    #    存量持仓里，既不被本脚本重算、也不被 A2 释放（A2 只做止盈止损），
    #    所以 A1 **只能部署真正空闲的钱**。reserved0 保守地把全部存量当占用
    #    （于是第一步不循环依赖），再细化一次：picks 里的存量本来就持有，
    #    不占额度 ⇒ w 可能被抬高，此时重跑一次 affordability（单调、必然收敛）。
    #    买不起的标的在契约载荷里**没有位置**（picks 每条只能 {code, weight_pct,
    #    reason}）⇒「这只因为买不起被跳过」在报告里看不见，只能从「只数少于
    #    LIMIT_N」反推（P64 §9 存疑项 1，本站不改契约）。
    total_assets = ctx.get("total_assets")
    holdings = ctx.get("holdings") or []
    cap = 100.0 - CASH_FLOOR
    reserved0 = _reserved_pct(holdings, total_assets, None)
    picked = None
    weight = None
    for n in range(min(LIMIT_N, len(deduped)), 0, -1):
        w0 = round(min(CAP_PCT, (cap - reserved0) / n), 2)
        if w0 <= 0.0:
            # 上限被存量吃光（cap ≤ reserved0）⇒ 一只也不选。**不许**产出负权重。
            continue
        ok = [r for r in deduped if _affordable(r["item"], w0, total_assets)]
        if len(ok) < n:
            continue
        picked = ok[:n]
        weight = w0
        reserved1 = _reserved_pct(holdings, total_assets,
                                  {str(r["item"]["code"]) for r in picked})
        w1 = round(min(CAP_PCT, (cap - reserved1) / n), 2)
        if w1 > w0:
            picked = [r for r in deduped
                      if _affordable(r["item"], w1, total_assets)][:n]
            weight = w1
        break

    if not picked:
        # 空池 / 全都买不起（n=1 时单票权重已达 CAP_PCT 仍买不起一手）⇒ 空仓，
        # 必须由 cash_pct=100 表达：一份**空的 picks** 与「A1 没跑起来」在读数上
        # 无法区分（channel_a._weights_items 明文拒绝）。
        return {"picks": [], "cash_pct": 100.0, "schema_version": "1.0.0"}

    # 4) 等权，单票被上限截断；剩下的进现金（于是现金下限自动成立）。
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
