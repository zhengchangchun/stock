"""插桩返回结构校验（设计文档 §6.2）：严格照文档 01 的约定结构体。

## 越界一律拒绝，绝不 clamp

`score = 150` 是脚本的 bug。把它悄悄压到 100 会让一个坏脚本看起来
工作正常 —— 那比直接报错危险得多，因为候选池会带着错误分数入库，
而报告里看不出任何异常。

## `bool` 不是 `float`

Python 里 `True` 是 `int` 的子类，所以 `isinstance(True, float)` 走的是
数值分支。`score=True` 会静默变成 1.0 分。必须显式排除。

## 两个时点各校验一次

`submit` 时校验一次、`approve`（上线）时再校验一次。脚本文本一旦落库
不可变，但本模块的校验规则会随项目演进收紧 —— 上线时再校验，
保证「现在能跑的」就是「当初认证过的」。

## 模块2 的四支插桩（P45 / D-33）

`m2_a1`（AI 模拟选股）/ `m2_a2`（AI 模拟卖出）/ `m2_a3`（AI 持仓收益预测）/
`m2_b1`（人工持仓收益预测）**不占用既有「插桩 0–5」编号** —— 0–5 的语义
已被模块1 钉住（需求 01），混用会让审计对不上号。

四条 fail-closed 规则，四支共同（需求书 D-24 的「拒绝而不是兜底」）：

1. 形状缺字段 / 类型错 / 越界 → 拒绝，**绝不 clamp、绝不补默认值**（`validate_return`）；
2. 引用了候选池外或账户里不存在的标的 → 拒绝，**不许静默丢弃**（`validate_references`）；
3. 概率不归一 / 区间 `lo > hi` → 拒绝（`_check_probs` / `_check_range`）；
4. 不可算的特征给 `None` + `na_reasons`，**不许给 0.0**（「不知道」≠「最差」）。

**规则 2 单独一个函数**：它需要候选池/账户作为入参，而形状校验不需要 ——
把两者混在一个签名里，就会有人以为「形状过了」等于「引用也过了」。
该函数**没有「跳过校验」的默认值**：没有允许集合就没有「通过」这回事。
（⏳ P46 通路层接线前，它只在测试里被调用。）

**m2_a3 / m2_b1 的字段名取预测载荷的真源**（`predict/model.py::CONTRACT_FIELDS`
的 `range_80` / `direction` / `invalidate_if`），**不是**库里 `predictions`
的列名（`range_lo` / `range_hi` / `direction_up`…）。两套名字并存会让
「同口径才可比」这句话失去可验证性（需求书 §5 A5 口径唯一）。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 模块2 四支的编号（D-33）。**不并入既有 0–5**。
M2_PLUGIN_IDS: tuple[str, ...] = ("m2_a1", "m2_a2", "m2_a3", "m2_b1")

#: 输出里会引用标的的插桩 —— 只有它们需要 `validate_references`。
_CODE_REFERENCING: frozenset[str] = frozenset({"m2_a1", "m2_a2"})

#: `m2_a1` 的权重合计 + 现金必须等于它（百分比口径）。
_PCT_TOTAL: float = 100.0

#: 浮点合计的比较容差。**不是给脚本的宽容度** —— 只吸收二进制浮点的表示误差
#: （`0.1+0.2+0.7 == 0.9999999999999999`）。真实的口径错误量级远大于它。
_TOL: float = 1e-6

#: `ctx["features"]` 必须齐全的键。**运行期与探针期都要满足** ——
#: 少一个键，插桩里 `ctx["features"]["roe_pct"]` 就会 KeyError，
#: 而沙盒探针会把它判成脚本 bug 从而误拒。
FEATURE_KEYS: tuple[str, ...] = (
    "period",
    "roe", "roe_pct", "roe_n",
    "gross_margin", "gross_margin_pct", "gross_margin_n",
    "gm_yoy_pp", "gm_yoy_pp_pct", "gm_yoy_pp_n",
    "inv_days", "inv_days_pct", "inv_days_n",
    "fcf_margin", "fcf_margin_pct", "fcf_margin_n",
    "dupont", "na_reasons", "period_mixed", "asof",
)

#: 契约预检用的探针上下文（设计文档 §6.1 的完整空形状）。
#:
#: 探针上下文：**结构完整但无数据**。它的职责是让一个写法正常的脚本
#: 能跑完并交出合规结果 —— 不是给它喂真实数据。
#:
#: ⚠️ 这里必须覆盖**所有调用点 ctx 键的并集**（`build_ctx` ∪
#: `risk_adjust.adjust` 追加的 `raw_score`/`risk_list` ∪ 模块2 三支的
#: `candidates`/`holdings`/`cash`）。少一个键，读它的正常脚本就会被探针误拒。
#:
#: ⏳ `candidates` / `holdings` / `cash` 是模块2 通路（P46）的输入形状，
#: 这里先给**空形状**占位。P46 定稿 ctx 时必须回来核对：键名对不上就等于
#: 探针在拿一份不存在的契约认证脚本。
#:
#: ✅ **P47 已核对**（这次核对的结果就长在下面）：模块2 的 ctx 由
#: `stocklab/m2/context.py::channel_ctx` 构造，顶层键与这里**逐一对齐**
#: （`tests/test_m2_channel_a.py::test_probe_ctx_keys_cover_the_real_ctx` 钉住）。
#: `candidates` / `holdings` 仍留空列表：探针的职责是「结构完整但**无数据**」，
#: 而它们的**有数据形状**（每一项是一份 `build_ctx` 产物 + 池内评分/持仓字段）
#: 在 `m2/context.py` 里定义 —— 把样例数据塞进探针会让「无数据分支」不再被走到。
PROBE_CTX: dict = {
    "code": "__probe__",
    "name": "__probe__",
    "asof": "1970-01-01",
    "pool": "short",
    "asset_type": "stock",
    "board": "main",
    "sector": "__probe__",
    "bars": [],
    "raw_score": 0.0,
    "risk_list": [],
    # 模块2：候选池成员（三池，空）、当前持仓（空）、现金。
    # 现金给 `None` 而不是 0.0 —— 探针**没有**账户数据，「不知道」≠「没钱」
    # （规则 4；同理 `raw_score=0.0` 是既有约定：探针确实算得出 0 分）。
    "candidates": {"short": [], "mid": [], "long": []},
    "candidates_excluded": {},
    "candidate_pool": {"asof": "1970-01-01", "available": False, "pools": {},
                       "codes": [], "missing_pools": ["short", "mid", "long"],
                       "reason": "探针：没有候选池数据"},
    "holdings": [],
    "cash": None,
    "channel": "__probe__",
    "account_id": "__probe__",
    "total_assets": None,
    "focus": None,
    "marks": {},
    "index_300": None,
    "guardrails": [],
    "disclosure": [],
    "non_goals": [],
    "features": {
        "period": None,
        "roe": None, "roe_pct": None, "roe_n": 0,
        "gross_margin": None, "gross_margin_pct": None, "gross_margin_n": 0,
        "gm_yoy_pp": None, "gm_yoy_pp_pct": None, "gm_yoy_pp_n": 0,
        "inv_days": None, "inv_days_pct": None, "inv_days_n": 0,
        "fcf_margin": None, "fcf_margin_pct": None, "fcf_margin_n": 0,
        "dupont": None, "na_reasons": [], "period_mixed": False,
        "asof": "1970-01-01",
    },
}

#: `m2_a3` / `m2_b1` 的形状**共用同一份定义** —— 两者的可比性靠的是
#: 「字段名与语义逐字相同」，写成两个字面量就等于给漂移留了门（需求书 D-30）。
#: 字段名取自 `predict/model.py::CONTRACT_FIELDS`（`range_80` / `direction` /
#: `invalidate_if`），`na_reasons` 是「不知道」的落点，`schema_version` 是形状版本。
_M2_FORECAST_SHAPE: tuple[tuple[str, str], ...] = (
    ("range_80", "range"), ("direction", "probs"),
    ("invalidate_if", "opt_str"), ("na_reasons", "str_list"),
    ("schema_version", "nonempty_str"),
)

#: plugin_id → (字段名, 种类) 的有序清单。种类见 `_check_field`。
SHAPES: dict[str, tuple[tuple[str, str], ...]] = {
    "0": (("pass_flag", "bool"), ("risk_note", "str_list")),
    "1": (("score", "score"), ("pass_flag", "bool"),
          ("reason", "str"), ("risk_list", "str_list")),
    "2": (("score", "score"), ("pass_flag", "bool"),
          ("reason", "str"), ("risk_list", "str_list")),
    "3": (("score", "score"), ("pass_flag", "bool"),
          ("reason", "str"), ("risk_list", "str_list")),
    "4": (("final_score", "score"), ("risk_out", "str_list")),
    "5": (("analysis_result", "dict"), ("bad_case_list", "list")),
    # ---- 模块2 四支（P45 / D-33）：不占 0–5 编号 ----
    "m2_a1": (("picks", "picks"), ("cash_pct", "pct"),
              ("schema_version", "nonempty_str")),
    "m2_a2": (("orders", "orders"), ("schema_version", "nonempty_str")),
    "m2_a3": _M2_FORECAST_SHAPE,
    "m2_b1": _M2_FORECAST_SHAPE,
}

KNOWN_PLUGIN_IDS: tuple[str, ...] = tuple(sorted(SHAPES))


class PluginContractError(Exception):
    """脚本返回结构不符合约定。"""


def _check_number(where: str, value: object) -> float:
    """数值字段的公共前置：**bool 先排除**（`isinstance(True, int)` 为真）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PluginContractError(
            f"{where} 必须是数值，实际是 {type(value).__name__}（{value!r}）")
    return float(value)


def _check_pct(where: str, value: object) -> float:
    """百分比字段：`[0, 100]`，越界拒绝。"""
    v = _check_number(where, value)
    if not 0.0 <= v <= 100.0:
        raise PluginContractError(
            f"{where} 超出 [0,100] 范围：{v}。"
            "越界是脚本的 bug，本系统**拒绝而非截断** —— 静默 clamp "
            "会让坏脚本看起来工作正常")
    return v


def _check_str(where: str, value: object) -> str:
    if not isinstance(value, str):
        raise PluginContractError(
            f"{where} 必须是 str，实际是 {type(value).__name__}（{value!r}）")
    return value


def _check_picks(where: str, value: object) -> list[dict]:
    """`m2_a1` 的选股清单：每条 `{code, weight_pct, reason}`。

    多余键**丢弃**（与顶层字段同款），缺键**拒绝** —— 少一个字段就等于
    报告里少了一个数，而报告不会因此报错。
    """
    if not isinstance(value, list):
        raise PluginContractError(
            f"{where} 必须是 list，实际是 {type(value).__name__}（{value!r}）")
    out: list[dict] = []
    for i, item in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise PluginContractError(
                f"{at} 必须是 dict，实际是 {type(item).__name__}（{item!r}）")
        missing = [k for k in ("code", "weight_pct", "reason") if k not in item]
        if missing:
            raise PluginContractError(
                f"{at} 缺少必填字段 {missing}；实收到 {sorted(item)}")
        code = item["code"]
        if not isinstance(code, str) or not code.strip():
            raise PluginContractError(f"{at} 的 code 必须是非空字符串，实际是 {code!r}")
        out.append({
            "code": code,
            "weight_pct": _check_pct(f"{at} 的 weight_pct", item["weight_pct"]),
            "reason": _check_str(f"{at} 的 reason", item["reason"]),
        })
    return out


def _check_orders(where: str, value: object) -> list[dict]:
    """`m2_a2` 的卖出指令：每条 `{code, side, reason}`，`side` **只能是 `sell`**。

    买入侧不归 A2 管（它的职责是止盈止损/调仓退出）。允许 `side` 任意取值
    会让「这条指令是买还是卖」变成靠 reason 猜。
    """
    if not isinstance(value, list):
        raise PluginContractError(
            f"{where} 必须是 list，实际是 {type(value).__name__}（{value!r}）")
    out: list[dict] = []
    for i, item in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise PluginContractError(
                f"{at} 必须是 dict，实际是 {type(item).__name__}（{item!r}）")
        missing = [k for k in ("code", "side", "reason") if k not in item]
        if missing:
            raise PluginContractError(
                f"{at} 缺少必填字段 {missing}；实收到 {sorted(item)}")
        code = item["code"]
        if not isinstance(code, str) or not code.strip():
            raise PluginContractError(f"{at} 的 code 必须是非空字符串，实际是 {code!r}")
        if item["side"] != "sell":
            raise PluginContractError(
                f"{at} 的 side 只能是 'sell'（A2 是卖出侧：止盈止损 / 调仓退出），"
                f"实际是 {item['side']!r}")
        out.append({"code": code, "side": "sell",
                    "reason": _check_str(f"{at} 的 reason", item["reason"])})
    return out


def _check_range(where: str, value: object) -> list[float] | None:
    """`range_80`：`[lo, hi]` 且 `lo <= hi`，或 `None`（不知道，见 `na_reasons`）。"""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PluginContractError(
            f"{where} 必须是 [lo, hi] 两元序列或 None，实际是 {value!r}")
    lo = _check_number(f"{where}[0]", value[0])
    hi = _check_number(f"{where}[1]", value[1])
    if lo > hi:
        raise PluginContractError(
            f"{where} 区间倒挂：lo={lo} > hi={hi}。"
            "拒绝而非交换两端 —— 交换会把脚本的方向错误伪装成一个合法区间")
    return [lo, hi]


def _check_probs(where: str, value: object) -> dict | None:
    """三分类概率：`{up, flat, down}` 各在 `[0,1]` 且**合计为 1**，或 `None`。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PluginContractError(
            f"{where} 必须是 dict 或 None，实际是 {type(value).__name__}（{value!r}）")
    missing = [k for k in ("up", "flat", "down") if k not in value]
    if missing:
        raise PluginContractError(
            f"{where} 缺少必填键 {missing}；实收到 {sorted(value)}")
    out = {}
    for k in ("up", "flat", "down"):
        p = _check_number(f"{where} 的 {k}", value[k])
        if not 0.0 <= p <= 1.0:
            raise PluginContractError(f"{where} 的 {k} 超出 [0,1]：{p}")
        out[k] = p
    total = out["up"] + out["flat"] + out["down"]
    if abs(total - 1.0) > _TOL:
        raise PluginContractError(
            f"{where} 三个概率之和应为 1，实际 {total}（{out}）。"
            "拒绝而非归一化 —— 归一化会把脚本算错的概率救成一份看起来合法的载荷")
    return out


def _check_field(plugin_id: str, name: str, kind: str, value: object) -> object:
    where = f"插桩 {plugin_id} 的字段 {name!r}"
    if kind == "score":
        # bool 先排除：`isinstance(True, int)` 为真，不挡会静默变成 1.0 分
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PluginContractError(
                f"{where} 必须是数值，实际是 {type(value).__name__}（{value!r}）")
        value = float(value)
        if not 0.0 <= value <= 100.0:
            raise PluginContractError(
                f"{where} 超出 [0,100] 范围：{value}。"
                "越界是脚本的 bug，本系统**拒绝而非截断** —— 静默 clamp "
                "会让坏脚本看起来工作正常")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise PluginContractError(
                f"{where} 必须是 bool，实际是 {type(value).__name__}（{value!r}）")
        return value
    if kind == "str":
        return _check_str(where, value)
    if kind == "nonempty_str":
        text = _check_str(where, value)
        if not text.strip():
            raise PluginContractError(f"{where} 不能是空字符串（{value!r}）")
        return text
    if kind == "opt_str":
        if value is None:
            return None
        text = _check_str(where, value)
        if not text.strip():
            raise PluginContractError(f"{where} 不能是空字符串（{value!r}）")
        return text
    if kind == "pct":
        return _check_pct(where, value)
    if kind == "picks":
        return _check_picks(where, value)
    if kind == "orders":
        return _check_orders(where, value)
    if kind == "range":
        return _check_range(where, value)
    if kind == "probs":
        return _check_probs(where, value)
    if kind == "str_list":
        if not isinstance(value, list) or not all(
                isinstance(x, str) for x in value):
            raise PluginContractError(
                f"{where} 必须是 list[str]，实际是 {value!r}")
        return list(value)
    if kind == "dict":
        if not isinstance(value, dict):
            raise PluginContractError(
                f"{where} 必须是 dict，实际是 {type(value).__name__}（{value!r}）")
        return dict(value)
    if kind == "list":
        if not isinstance(value, list):
            raise PluginContractError(
                f"{where} 必须是 list，实际是 {type(value).__name__}（{value!r}）")
        return list(value)
    raise AssertionError(f"未知字段种类 {kind!r}")     # 代码 bug，不是脚本 bug


def _check_cross(plugin_id: str, out: dict) -> None:
    """跨字段不变量。**逐字段检查表达不了它们** —— 这是单独一层的原因。"""
    if plugin_id == "m2_a1":
        total = sum(p["weight_pct"] for p in out["picks"]) + out["cash_pct"]
        if abs(total - _PCT_TOTAL) > _TOL:
            raise PluginContractError(
                f"插桩 m2_a1 的权重合计 + 现金 = {total}，必须等于 {_PCT_TOTAL}。"
                "拒绝而非缩放 —— 缩放会把一个算错的仓位方案伪装成合规的，"
                "而下游按权重下单时不会有人发现")
    elif plugin_id in ("m2_a3", "m2_b1"):
        fields = (out["range_80"], out["direction"], out["invalidate_if"])
        reasons = out["na_reasons"]
        if reasons and any(v is not None for v in fields):
            raise PluginContractError(
                f"插桩 {plugin_id} 报了 na_reasons={reasons} 却仍给出部分数值 —— "
                "「不知道」与「知道」不许混在一份载荷里（半真半假等于编数）")
        if not reasons and any(v is None for v in fields):
            raise PluginContractError(
                f"插桩 {plugin_id} 有字段是 None 却没有 na_reasons —— "
                "缺数必须逐条说明为什么（「不知道」≠ 0，也≠ 沉默）")


def validate_return(plugin_id: str, result: object) -> dict:
    """校验并**标准化**返回值（数值统一成 float）。违规抛 `PluginContractError`。

    返回新 dict（不是原对象）—— 调用方拿到的一定是校验过的副本。
    **只做形状与跨字段不变量**；「引用的标的在不在允许集合里」是
    `validate_references` 的事（它需要候选池/账户作为入参）。
    """
    if plugin_id not in SHAPES:
        raise PluginContractError(
            f"未知插桩编号 {plugin_id!r}；已知：{list(KNOWN_PLUGIN_IDS)}")
    if not isinstance(result, dict):
        raise PluginContractError(
            f"插桩 {plugin_id} 的 run() 必须返回 dict，实际是 "
            f"{type(result).__name__}（{result!r}）")

    out: dict = {}
    for name, kind in SHAPES[plugin_id]:
        if name not in result:
            raise PluginContractError(
                f"插桩 {plugin_id} 的返回缺少必填字段 {name!r}；"
                f"实收到 {sorted(result)}")
        out[name] = _check_field(plugin_id, name, kind, result[name])
    _check_cross(plugin_id, out)
    return out


def validate_references(plugin_id: str, result: object, *,
                        allowed_codes: Iterable[str]) -> dict:
    """fail-closed 规则 2：输出引用的标的必须落在**允许集合**里。

    `allowed_codes` 由调用方按插件语义给：`m2_a1` 给候选池成员，
    `m2_a2` 给账户当前持仓。引用集合外的标的 → 拒绝。

    **不许静默丢弃**：丢掉越界标的后，报告会显示一份「有效」的仓位方案，
    而实际少了一条腿 —— 少的那条腿在报告里没有任何痕迹（需求书 §2 规则 2）。

    **没有「跳过」的默认值**：`allowed_codes` 是必填关键字参数，传空集合时
    任何引用都算越界。fail-open 的默认值（`None` → 跳过）会让「忘记传」
    与「校验通过」在返回值上长得一样。

    ⏳ P46（双通路与镜像）接线时由通路层传入；P45 只提供并测试本函数。
    """
    if plugin_id not in _CODE_REFERENCING:
        raise ValueError(
            f"插桩 {plugin_id} 不引用标的，不该调用 validate_references —— "
            "这是调用方的编程错误，不是脚本 bug")
    out = validate_return(plugin_id, result)
    field = "picks" if plugin_id == "m2_a1" else "orders"
    allowed = frozenset(allowed_codes)
    offenders = sorted({item["code"] for item in out[field]
                        if item["code"] not in allowed})
    if offenders:
        raise PluginContractError(
            f"插桩 {plugin_id} 引用了允许集合外的标的 {offenders}。"
            "拒绝而非丢弃 —— 丢弃会让报告显示「有效」而实际少了腿")
    return out
