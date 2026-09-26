"""P84 / T1：`paper/arm_names.py` —— 显示名的**唯一真源**。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 12 条映射逐条正确 | 表抄错一个字（显示名是给人看的，抄错没人发现） |
| 未知 id 原样返回 / `"unknown"`，**不抛** | 新增账户让页面 500 |
| `arm-agent-ds-v9` 走前缀兜底 | 家族新版本账户没有可读名 |
| 家族后缀规则 | 兜底名把前缀也带进去（`AI操盘手·arm-agent-ds-v9`） |
"""

from stocklab.paper import arm_names

#: 任务书 §T1 的表（**逐字**冻结副本）—— 反向对照，不调用现役实现。
EXPECTED: dict[str, tuple[str, str, str]] = {
    "arm-agent-ds-v3": ("AI操盘手·三版", "AI三版", "ai"),
    "arm-agent-ds-v2": ("AI操盘手·二版", "AI二版", "ai"),
    "arm-agent-ds-v1": ("AI操盘手·一版", "AI一版", "ai"),
    "arm-agent-v1": ("规则臂·A1 选股", "规则A1", "rule"),
    "arm-agent": ("智能体臂 · 旧世代", "旧智能体", "ai"),
    "arm-agent-random": ("随机臂·对照组", "随机", "random"),
    "arm-hold": ("什么都不做", "不动", "hold"),
    "arm-now": ("你的实盘镜像", "实盘", "real"),
    "arm-discipline-05": ("纪律臂·ETF 目标 5%", "纪律5%", "discipline"),
    "arm-discipline-10": ("纪律臂·ETF 目标 10%", "纪律10%", "discipline"),
    "arm-discipline-15": ("纪律臂·ETF 目标 15%", "纪律15%", "discipline"),
    "sh000300": ("沪深300（大盘）", "沪深300", "index"),
}


def test_t1_the_table_is_verbatim_and_complete():
    assert set(arm_names.ARM_NAMES) == set(EXPECTED)
    for aid, (name, short, kind) in EXPECTED.items():
        assert arm_names.display_name(aid) == name, aid
        assert arm_names.short_name(aid) == short, aid
        assert arm_names.arm_kind(aid) == kind, aid
        assert set(arm_names.ARM_NAMES[aid]) == {"name", "short", "kind"}, aid


def test_t1_unknown_ids_are_returned_verbatim_and_never_raise():
    for aid in ("arm-whatever", "", "arm-agents", "ARM-NOW"):
        assert arm_names.display_name(aid) == aid
        assert arm_names.short_name(aid) == aid
        assert arm_names.arm_kind(aid) == arm_names.UNKNOWN_KIND


def test_t1_family_prefix_fallback_keeps_the_page_alive():
    """新版本账户（映射表里还没有行）也要有可读名 —— 且后缀只取前缀之后那段。"""
    assert arm_names.display_name("arm-agent-ds-v9") == "AI操盘手·ds-v9"
    assert arm_names.short_name("arm-agent-ds-v9") == "AI·ds-v9"
    assert arm_names.arm_kind("arm-agent-ds-v9") == "ai"
    # `arm-agent` 自己不带那个连字符 —— 它走映射表，不走兜底。
    assert arm_names.display_name("arm-agent") == "智能体臂 · 旧世代"


def test_t1_non_string_input_does_not_explode():
    for value in (None, 3):
        assert arm_names.display_name(value) == str(value)
        assert arm_names.arm_kind(value) == arm_names.UNKNOWN_KIND


def test_t1_display_order_is_the_reading_order_not_a_ranking():
    """阅读顺序从 `arm-now` 起（先看自己的线）—— 与任务书声明的顺序一致。"""
    assert arm_names.DISPLAY_ORDER == ("arm-now", "arm-hold",
                                      "arm-discipline-05",
                                      "arm-discipline-10",
                                      "arm-discipline-15")
