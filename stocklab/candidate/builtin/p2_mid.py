"""插桩2：中期景气 & 财务质量打分。"""

SOURCE = '''
# 中期池：景气与财务质量。**注意本脚本的分数是「样本内横截面排序」，
# 未经验证** —— 不足 120 交易日/足够季报期数前，不构成任何结论。
#
# 按**可用因子**加权（权重按可用性归一），所以金融股（无营业成本/存货）
# 自动落在 ROE + 杜邦 + FCF 率上 —— 代码里不写行业特例。
WEIGHTS = {"roe": 0.35, "gross_margin": 0.20, "gm_yoy_pp": 0.15,
           "inv_days": 0.10, "fcf_margin": 0.20}

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
