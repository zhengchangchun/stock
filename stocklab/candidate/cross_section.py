"""横截面分位（设计 §6.4）。

## 为什么用分位而不是绝对阈值

`ROE > 15%` 这类数字编不出来源。分位让尺子来自数据本身，且随样本走。

## 极性必须写死

`inv_days` 越小越好，其余越大越好。搞反**不会报错、只会让池子选反** ——
所以 `POLARITY` 是显式常量，且有测试钉住。

## 样本太薄时给 None

21 只种子（除 ETF）落在约 10 个行业，某些因子（如 `inv_days` 对金融股）
只有个别样本。**n < 2 时分位无意义 → `None`**；`0.0` 会把「不知道」伪装成
「最差」，`1.0` 会伪装成「最好」，两者都在撒谎。
"""

from __future__ import annotations

#: 因子名 → 是否「越大越好」。**改这里必须同时改测试。**
POLARITY: dict[str, bool] = {
    "roe": True, "gross_margin": True, "gm_yoy_pp": True,
    "inv_days": False, "fcf_margin": True,
}


def _pct_of(values: list[float], v: float, *, higher_better: bool) -> float:
    """平均排名法分位（0–1）。并列取平均名次。"""
    n = len(values)
    less = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    rank = less + (equal - 1) / 2.0
    frac = rank / (n - 1)
    return frac if higher_better else 1.0 - frac


def build(rows: dict[str, dict], *, asof: str) -> dict:
    """`{code: {factor: value, period: str}}` → `{code: {factor_pct, ...}}`。

    输出**键永远齐全**：`<factor>_pct`（`None` 表示不可算或样本不足）、
    `<factor>_n`（该因子的有效样本数）、`period`、`period_mixed`。
    """
    out: dict[str, dict] = {code: {} for code in rows}
    periods = {r.get("period") for r in rows.values() if r.get("period")}
    mixed = len(periods) > 1

    for factor, higher_better in POLARITY.items():
        pairs = [(c, r[factor]) for c, r in rows.items()
                 if r.get(factor) is not None]
        values = [v for _c, v in pairs]
        n = len(values)
        for code, r in rows.items():
            out[code][f"{factor}_n"] = n
            v = r.get(factor)
            if v is None or n < 2:
                out[code][f"{factor}_pct"] = None
            else:
                out[code][f"{factor}_pct"] = _pct_of(
                    values, v, higher_better=higher_better)

    for code, r in rows.items():
        out[code]["period"] = r.get("period")
        out[code]["period_mixed"] = mixed
        out[code]["asof"] = asof
    return out
