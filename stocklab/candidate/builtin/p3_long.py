"""插桩3：长期护城河打分。"""

SOURCE = '''
# 长期池：护城河。**样本内横截面排序，未经验证**（同 p2 的口径声明）。
# 权重体现「长期靠 ROE 与自由现金流」，毛利率同比的短期波动权重最低。
WEIGHTS = {"roe": 0.40, "fcf_margin": 0.30, "gross_margin": 0.15,
           "gm_yoy_pp": 0.05, "inv_days": 0.10}

MIN_FACTORS = 2


def run(ctx):
    f = ctx.get("features") or {}
    if not f.get("period"):
        return {"score": 0.0, "pass_flag": False,
                "reason": "该期财报尚未公告或不可算（期数不足）",
                "risk_list": ["横截面分位排序，样本内，未经验证"]}

    usable = [k for k in WEIGHTS if f.get(k + "_pct") is not None]
    if len(usable) < MIN_FACTORS:
        return {"score": 0.0, "pass_flag": False,
                "reason": "可用财务因子不足 %d 个（缺：%s）"
                          % (MIN_FACTORS, "、".join(f.get("na_reasons") or [])),
                "risk_list": ["横截面分位排序，样本内，未经验证"]}

    total_w = sum(WEIGHTS[k] for k in usable)
    score = 100.0 * sum(f[k + "_pct"] * WEIGHTS[k] for k in usable) / total_w

    risks = ["横截面分位排序，样本内，未经验证"]
    if f.get("period_mixed"):
        risks.append("本期横截面含不同报告期（口径不完全同质）")
    if len(usable) < len(WEIGHTS):
        risks.append("部分因子不可算（%d/%d），已按可用因子归一"
                     % (len(usable), len(WEIGHTS)))
    for r in (f.get("na_reasons") or []):
        risks.append(r)

    return {"score": max(0.0, min(100.0, score)), "pass_flag": True,
            "reason": "本期 %s；可用因子 %d/%d"
                      % (f.get("period"), len(usable), len(WEIGHTS)),
            "risk_list": risks}
'''.lstrip()
