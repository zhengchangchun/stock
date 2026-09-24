"""`m2_a1`（AI 模拟选股）**源文本** —— v1.0.4（`reserved` 一律 = 全部存量持仓）。

## v1.0.4 修的是什么：v1.0.3 的「细化」假设「picks 里的存量一减仓就变现金」

v1.0.3 的 step 3 把 `reserved` 从「全部存量」细化成「**不在本轮 picks 里**的存量」，
理由是「picks 里的存量本来就持有，不该再占它的额度」。那句理由隐含了一个假设：

> 计划里那部分被 picks 占用的存量市值，会通过**卖出**释放成现金。

**这个假设在执行层不成立**：`paper/rules.py::plan_target_weight` 的卖出是
**整手**向下取整的，差额 < 1 手 ⇒ `hold`（`C_LOT` 分支，P64 定的口径，本站不许动）。
真库实测（`arm-agent-random` @ 2026-09-23 的同一账户形态）：
`000333` 100 股占 42.1475%、一手 ¥8,242.88，任何 `0 < 目标 < ¥8,247.00` 都是
「减不动」⇒ 那 42% 一分钱都不会变成现金，而 step 3 把它当成了可部署的钱。

⇒ **v1.0.4 删除 step 3**：`reserved = Σ over **全部** ctx["holdings"] 的 weight_pct`，
不因 picks 而减免。语义回到 v1.0.3 docstring 自己写的那句话 ——
「A1 **只能部署真正空闲的钱**」—— 只是 v1.0.3 的实现恰好违背了它。

**保留下来的一样都不少**：v1.0.1 的跨池去重、v1.0.2 的 affordability 过滤与
下降式 `n` 循环、v1.0.3 的 `reserved`（含 `total_assets is None` / 任一 holding
缺 `weight_pct` ⇒ `reserved = 0.0` 的退化口径）、以及
`LIMIT_N` / `CAP_PCT` / `CASH_FLOOR` / `LOT` 四个常量的数值。

**怎么发现的**：P66（随机对照臂敞口口径）修完后真库 200 种子仍有 **1/200** 被
`CashShortfall` 拒；逐笔复现指出「计划里 42% 的现金会释放」在执行层落空。
同一晚复核 A1 v1.0.3 源码，发现它写着**同一条**假设 ⇒ **两条臂同型**
（ERROR_DIARY #75 的教训③：一条残余缺陷如果是两条臂共有的，就不该只修一条）。
v1.0.4 与随机臂的 v3 一起改，才让「同护栏」这句话重新成立。

## v1.0.3 修的是什么（历史，口径已被 v1.0.4 取代）：钱锁在存量持仓里

真库 2026-09-23 的实测（P64 §7.3）：账户 `total_assets = 19,567`，其中
**¥8,247（42.15%）是 `000333` 这笔存量持仓**，可用现金只有 ¥11,320。
`000333` 不在 5 条 pick 里（排名不够前），A2 也不卖它（它还在池内、无止损止盈）
⇒ 那 42% **既不被重算、也不被释放**。于是 5 只 × 18% = ¥17,610 的目标敞口
> ¥11,320 可用现金 ⇒ 成交后 `paper_nav_daily.cash = −4,490.01`（总资产的 −23%）。
**这就是杠杆。**

v1.0.3 的修法：Σw 的上限先扣掉存量持仓的当前占比
（`Σw ≤ (100 − CASH_FLOOR) − reserved`、`w = min(CAP_PCT, 该上限 / n)` 等权、
`cash_pct = 100 − w × len(picks)` 逐位自洽）。v1.0.4 只改 `reserved` 的**取值**，
这组公式一个字没动。

## ⚠️ v1.0.2 的「整手取整 ⇒ 0 股」只是**偶然**闸门，已被执行层的显式闸门取代

P64 的教训（ERROR_DIARY #73）：v1.0.1 把「按目标市值算股数、整手向下取整
⇒ 0 股」这件**副产物**当成了现金闸门（只成交 3 笔 ⇒ 现金仍 +¥2,055）。P64
修掉「买不起一手」= **拆掉**那道偶然闸门，当天就把一个此前看不见的杠杆缺陷
兑现成了真实持仓。⇒ 从 P65 起，资金闸门是
`paper/agent_decide.py::CashShortfall`（**执行层**、fail-closed、具名
`code="cash"`、整轮拒绝零写入），**不再依赖任何取整副产物**：本脚本的口径
就算再错一次，执行层也会整轮拒绝，而不是透支。

## 本版本的口径（确定性、纯函数、**一轮**）

1. `reserved = Σ over ctx["holdings"] of weight_pct` —— **全部**存量，不因 picks
   而减免（v1.0.4 删掉了 v1.0.3 的「细化」第二轮）；
2. 对 `n` 从 `min(LIMIT_N, 去重后只数)` 往下（沿用 v1.0.2 的下降式循环）：
   `w = round(min(CAP_PCT, ((100 − CASH_FLOOR) − reserved) / n), 2)`，取在
   `w` 下买得起的前 `n` 只；凑不齐就继续往下试；
3. 退化口径：`total_assets is None`、或任一 holding 缺 `weight_pct`
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
下面 `SOURCE` 是 **v1.0.4** 的种子文本，**不改库** —— 上线走 `plugin submit`
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
# 本站是**源版本 v1.0.4** 的种子文本：在 v1.0.3（只扣「不在本轮 picks 里」的存量）
# 之上，把 reserved 改成「**全部**存量持仓占比之和」—— 删掉了 v1.0.3 的 step 3（细化）。
#
# 为什么删：v1.0.3 的细化假设「picks 里那部分存量市值会通过卖出释放成现金」，
# 而执行层的卖出是**整手**向下取整的（paper/rules.py::plan_target_weight 的
# C_LOT 分支：差额 < 1 手 ⇒ hold）。真库实测（随机对照臂 @ 2026-09-23 的
# 同一账户形态）：000333 100 股占 42.1475%、一手 ¥8,242.88，任何 0 < 目标 < 8,247
# 都是「减不动」⇒ 那 42% 一分钱都不会变成现金，细化却把它当成了可部署的钱。
# 语义回到 v1.0.3 自己写的那句话：「A1 只能部署真正空闲的钱」。
#
# 保留下来的：v1.0.1 的跨池去重、v1.0.2 的 affordability ＋ 下降式 n 循环、
# v1.0.3 的 reserved（含 total_assets 为 None / 任一 holding 缺 weight_pct
# ⇒ reserved = 0 的退化口径）、四个常量（LIMIT_N/CAP_PCT/CASH_FLOOR/LOT）的数值。
#
# v1.0.3 曾经修的是：v1.0.2 按 total_assets × w 定目标敞口，而钱锁在存量持仓里
# （真库 2026-09-23：total_assets = 19,567、000333 占 42.15%、可用现金 ¥11,320
# ⇒ 5 只 × 18% = ¥17,610 > ¥11,320 ⇒ 成交后现金 −4,490.01）。修法
# Σw ≤ (100 − CASH_FLOOR) − reserved、等权 w = min(CAP_PCT, 该上限 / n)、
# cash_pct = 100 − w × len(picks)（逐位自洽）⇒ 总敞口自然回到 ≤ 100%。
#
# ⚠️ v1.0.2 的「整手取整 ⇒ 0 股」只是**偶然**闸门，已被执行层的显式闸门
# （paper/agent_decide.py::CashShortfall，具名 code="cash"、整轮拒绝零写入）
# 取代 —— 资金闸门不许依赖任何取整副产物（ERROR_DIARY #73：P64 拆掉那道偶然
# 闸门，当天就把一个此前看不见的杠杆缺陷兑现成了真实持仓）。
#
# 存量占比的算法（确定性、纯函数、**一轮**）：
#   reserved = Σ over ctx["holdings"] of weight_pct（**全部**存量，不因 picks 减免）；
#   对 n 从 min(LIMIT_N, 去重后只数) 往下：w = round(min(CAP_PCT,
#   ((100 − CASH_FLOOR) − reserved)/n), 2)，取 w 下买得起的前 n 只。
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
    # **「不知道」≠「买不起」**（规则 3）：账户总资产未知（探针 ctx）或这只
    # 标的没有 PIT 收盘价时**不过滤** —— 与 v1.0.1 的行为逐位相同。
    if total_assets is None:
        return True
    close = item.get("close")
    if close is None:
        return True
    return float(total_assets) * float(weight) / 100.0 >= float(close) * LOT


def _reserved_pct(holdings, total_assets):
    # **全部**存量持仓占比之和（占总资产 %）—— 口径 v1.0.4：不因 picks 而减免。
    # 为什么必须有这一项：目标市值 = total_assets × w，而总资产里有一部分是
    # **动不了**的存量持仓（既不重算、也不保证卖得出去）⇒ A1 只能部署真正空闲的钱。
    # 为什么**不**排除 picks 里的存量（v1.0.3 的细化在这里被删掉了）：
    # 执行层的卖出是整手向下取整的，差额 < 1 手 ⇒ hold ⇒ 那笔钱不会释放。
    # 退化口径（与 v1.0.2 逐位相同）：「账户总资产未知」或「任一 holding
    # 缺 weight_pct」（探针 ctx / 没有 PIT 价）⇒ **0.0**，退回「存量不占额度」
    # 的老口径 —— 「不知道」≠「存量把额度占满了」（与 _affordable 同一条规则 3）。
    if total_assets is None:
        return 0.0
    total = 0.0
    for item in holdings or []:
        weight = item.get("weight_pct")
        if weight is None:
            return 0.0
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
    #    v1.0.4 在权重上扣掉**全部**存量持仓占比：那些钱锁在存量持仓里，既不被
    #    本脚本重算、也不被 A2 释放（A2 只做止盈止损）⇒ A1 **只能部署真正空闲
    #    的钱**。v1.0.3 曾把「在 picks 里的」存量从扣减里排除掉，理由是「它本来
    #    就持有」；那个理由隐含「一减仓就变现金」，而执行层的卖出是**整手**的
    #    （差额 < 1 手 ⇒ hold）⇒ 那份现金根本不会释放。所以 v1.0.4 把细化删了。
    #    买不起的标的在契约载荷里**没有位置**（picks 每条只能 {code, weight_pct,
    #    reason}）⇒「这只因为买不起被跳过」在报告里看不见，只能从「只数少于
    #    LIMIT_N」反推（P64 §9 存疑项 1，本站不改契约）。
    total_assets = ctx.get("total_assets")
    holdings = ctx.get("holdings") or []
    cap = 100.0 - CASH_FLOOR
    reserved = _reserved_pct(holdings, total_assets)
    picked = None
    weight = None
    for n in range(min(LIMIT_N, len(deduped)), 0, -1):
        w = round(min(CAP_PCT, (cap - reserved) / n), 2)
        if w <= 0.0:
            # 上限被存量吃光（cap ≤ reserved）⇒ 一只也不选。**不许**产出负权重。
            continue
        ok = [r for r in deduped if _affordable(r["item"], w, total_assets)]
        if len(ok) < n:
            continue
        picked = ok[:n]
        weight = w
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
