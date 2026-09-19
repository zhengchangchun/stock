"""插桩1：短期因子打分（量价）。"""

SOURCE = '''
def run(ctx):
    bars = ctx["bars"]
    if len(bars) < 20:
        return {"score": 0.0, "pass_flag": False,
                "reason": "K 线不足 20 根，短期因子无法计算", "risk_list": []}

    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]

    # 20 日动量
    mom = (closes[-1] - closes[-20]) / closes[-20] if closes[-20] else 0.0
    # 量比：最近 5 日均量 / 前 20 日均量
    v5 = sum(vols[-5:]) / 5.0
    v20 = sum(vols[-25:-5]) / 20.0 if len(vols) >= 25 else v5
    vol_ratio = (v5 / v20) if v20 else 1.0

    score = 50.0 + mom * 200.0 + (vol_ratio - 1.0) * 20.0
    score = max(0.0, min(100.0, score))

    risks = []
    if vol_ratio > 2.0:
        risks.append("放量异常（量比 %.2f）" % vol_ratio)
    if mom < -0.10:
        risks.append("20 日跌幅超过 10%")

    return {"score": score, "pass_flag": True,
            "reason": "20 日动量 %.2f%%，量比 %.2f" % (mom * 100, vol_ratio),
            "risk_list": risks}
'''.lstrip()
