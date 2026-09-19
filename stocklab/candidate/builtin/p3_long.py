"""插桩3：长期护城河打分。**本轮是占位实现**。"""

SOURCE = '''
STUB_NOTE = "财务因子未接（ROE / 自由现金流需财报，本分数为占位值）"


def run(ctx):
    bars = ctx["bars"]
    if len(bars) < 120:
        return {"score": 0.0, "pass_flag": False,
                "reason": "K 线不足 120 根", "risk_list": [STUB_NOTE]}

    # 真正的护城河打分应看 ROE、自由现金流、竞争力 —— 都需要财报。
    closes = [b["close"] for b in bars]
    long_ret = (closes[-1] - closes[-120]) / closes[-120] if closes[-120] else 0.0
    score = max(0.0, min(100.0, 50.0 + long_ret * 100.0))

    return {"score": score, "pass_flag": True,
            "reason": "占位：120 日累计涨幅 %.2f%%（非真实护城河打分）"
                      % (long_ret * 100),
            "risk_list": [STUB_NOTE]}
'''.lstrip()
