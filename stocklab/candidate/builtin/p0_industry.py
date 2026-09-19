"""插桩0：行业特殊排雷。"""

SOURCE = '''
FINANCIAL_SECTORS = ("银行", "保险", "证券")

# 行业关键字表。**非 PIT**：用标的当前名称推断行业，历史某日的行业
# 归属无法还原，仅为近似 —— 与 screen.py 的 ST 判据是同一类近似。
SECTOR_KEYWORDS = {
    "家电": ("电器", "集团"),
    "银行": ("银行",),
    "保险": ("平安", "人寿", "太平"),
    "能源": ("石化", "神华", "石油"),
    "公用": ("电力",),
    "食品饮料": ("茅台", "五粮液"),
    "通信": ("移动", "联通", "电信"),
    "科技制造": ("时代", "海康"),
}


def _guess_sector(name):
    for sector, words in SECTOR_KEYWORDS.items():
        for w in words:
            if w in name:
                return sector
    return "未知"


def run(ctx):
    sector = _guess_sector(ctx["name"])
    risks = []
    if sector == "未知":
        risks.append("行业未能从名称推断，已按通用规则处理（非 PIT，仅为近似）")
    if sector == "银行":
        risks.append("银行业：高杠杆经营，通用排雷指标不完全适用")
    return {"pass_flag": True, "risk_note": risks}
'''.lstrip()
