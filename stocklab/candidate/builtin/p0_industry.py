"""插桩0：行业特殊排雷。"""

SOURCE = '''
# 行业口径**只允许一套**：优先用 ctx["sector"]（东财 INDUSTRY_NAME，
# 如「白色家电」「银行Ⅱ」）。名称关键字仅在该字段缺失时兜底 ——
# 两套口径并存会让「家电」与「白色家电」同时出现，统计时对不上。
#
# ⚠️ sector 非 PIT（取的是当前行业归属，历史某日无法还原），与 screen.py
# 的 ST 判据同属近似，报告里要标注。

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

FINANCIAL_KEYWORDS = ("银行", "保险", "证券")


def _sector_of(ctx):
    """返回 (行业名, 是否来自 ctx)。"""
    s = ctx.get("sector")
    if s:
        return s, True
    name = ctx.get("name") or ""
    for sector, words in SECTOR_KEYWORDS.items():
        for w in words:
            if w in name:
                return sector, False
    return None, False


def run(ctx):
    sector, from_ctx = _sector_of(ctx)
    risks = []
    if sector is None:
        risks.append("行业未能判定，已按通用规则处理（非 PIT，仅为近似）")
    if not from_ctx:
        risks.append("行业由标的名称推断（sector 字段缺失），非 PIT，仅为近似")
    if sector and any(k in sector for k in FINANCIAL_KEYWORDS):
        risks.append("金融业（" + sector + "）：高杠杆经营，通用排雷指标不完全适用；"
                     "毛利率与存货周转对本报表格无意义")
    return {"pass_flag": True, "risk_note": risks}
'''.lstrip()
