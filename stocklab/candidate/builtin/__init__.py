"""6 个内置插桩脚本的**源文本**（文档 01 §插桩钩子清单）。

## 它们是「接线用的朴素实现」，不是策略

文档要求短期池看量价/资金/板块/事件，中期池做杜邦分解与库存系数，
长期池算 ROE 与自由现金流。**其中只有短期池的输入本轮真的存在** ——
中期与长期需要财报数据（资产负债表/利润表/现金流量表），而本轮
没有采集路径（设计文档 §3「不做」、§13.3）。

所以：
- 插桩1（短期）是真实现（纯用量价）；
- 插桩2/3（中/长期）返回**占位分数**，并在 `risk_list` 里带
  「财务因子未接」——这个标记会一路进到候选池记录与报告里，不会丢；
- 插桩0 按行业分支（但行业判定用的是名称关键字，非 PIT，见下）。

## 为什么源码是字符串常量而不是 .py 文件

插桩的可执行版本来自**数据库**（`plugin_scripts.source_text`），
不是文件。这里存的是「首次 submit 用的初始版本」，用字符串是为了
让测试能直接 `load_script(BUILTIN_PLUGINS["3"], ...)` 验它，
不依赖文件系统。
"""

from stocklab.candidate.builtin import (p0_industry, p1_short, p2_mid, p3_long,
                                        p4_risk, p5_review)

#: `plugin_id` → 初始版本源文本。
BUILTIN_PLUGINS: dict[str, str] = {
    "0": p0_industry.SOURCE,
    "1": p1_short.SOURCE,
    "2": p2_mid.SOURCE,
    "3": p3_long.SOURCE,
    "4": p4_risk.SOURCE,
    "5": p5_review.SOURCE,
}
