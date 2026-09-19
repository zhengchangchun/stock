"""插桩4：风险权重修正。"""

SOURCE = '''
def run(ctx):
    score = float(ctx.get("raw_score", 0.0))
    risks = list(ctx.get("risk_list", []))
    out = []

    # 停牌与放量类风险扣分；财务未接类不扣分（那是数据问题，不是标的问题）
    for r in risks:
        if r.startswith("放量异常"):
            score -= 5.0
            out.append("放量风险扣 5 分")
        elif r.startswith("20 日跌幅"):
            score -= 8.0
            out.append("短期弱势扣 8 分")
        elif "财务因子未接" in r:
            out.append("财务数据缺失（不扣分，仅提示）")
        else:
            out.append("提示：" + r)

    return {"final_score": max(0.0, min(100.0, score)), "risk_out": out}
'''.lstrip()
