"""插桩2：中期景气 & 财务打分。**本轮是占位实现**。"""

SOURCE = '''
STUB_NOTE = "财务因子未接（无财报数据源，本分数为占位值）"


def run(ctx):
    bars = ctx["bars"]
    if len(bars) < 60:
        return {"score": 0.0, "pass_flag": False,
                "reason": "K 线不足 60 根", "risk_list": [STUB_NOTE]}

    # 真正的中期打分应做杜邦分解、库存系数、毛利率弹性 —— 都需要财报。
    # 本轮用价格波动率做占位，只为把接口打通。
    closes = [b["close"] for b in bars]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]
    if not rets:
        return {"score": 0.0, "pass_flag": False, "reason": "无有效收益序列",
                "risk_list": [STUB_NOTE]}
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    vol = var ** 0.5
    score = max(0.0, min(100.0, 50.0 + (0.02 - vol) * 1000.0))

    return {"score": score, "pass_flag": True,
            "reason": "占位：日波动率 %.4f（非真实景气打分）" % vol,
            "risk_list": [STUB_NOTE]}
'''.lstrip()
