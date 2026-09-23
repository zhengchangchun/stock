"""插桩5 报告渲染（P58 §1.6）：`reports/plugin-review/<asof>.md`。

## 报告落点**不是** `reports/<asof>-review.md`

那是 session review 的地盘，`ops patrol` ⑤ 按这个名字判存亡（`check_chain` 读
`<报告根>/<最新已收盘交易日>-review.md`）。撞名会让 patrol 读到错的文件 ——
所以这里另开一个目录，`patrol` 的判据一个字不动。

## 桩不许被包装成结论

现役 `v1.0.0` 返回 `{"status": "not_implemented"}`。报告**第 1 行**就得说这件事，
并且脚本输出的那一节原文照贴 —— 把桩的 `{}` 渲染成「本期无异常」是最危险的
一种润色：它看起来像一个结论。

## 逐字节可复现

正文里**没有墙上时钟**：给定同一份 `ctx` 与同一版脚本，渲染结果逐字节相同
（`now` 只进台账的 `created_at`，不进正文）。这样「报告变了」永远意味着
「输入或脚本变了」。
"""

from __future__ import annotations

import json

#: 报告目录名。与 `session review` 的 `<日期>-review.md` 刻意分开（见模块 docstring）。
REPORT_SUBDIR = "plugin-review"

#: 脚本还没实现时报告必须说的一句话（P58 §1.2）。
STUB_LINE = ("复盘脚本尚未实现（script_id={script_id} / {version} 桩），"
             "本报告只有输入侧事实")


def _range(value: list | None) -> str:
    """`[a, b]` → `a`（同一天）或 `a ~ b`；空 → `（空）`。"""
    if not value:
        return "（空）"
    return value[0] if value[0] == value[-1] else f"{value[0]} ~ {value[-1]}"


def header(*, asof: str, script_id: int, script_version: str, stub: bool) -> str:
    """报告第 1 行。桩状态与正常状态**长得不一样**，且桩的说明在最前面。"""
    if stub:
        return (f"# 插桩5 复盘 {asof} —— "
                + STUB_LINE.format(script_id=script_id, version=script_version))
    return f"# 插桩5 复盘 {asof}（script_id={script_id} / {script_version}）"


def render(*, asof: str, script_id: int, script_version: str, stub: bool,
           analysis: dict, bad_cases: list, ctx: dict, summary: dict) -> str:
    """`ctx` ＋ 脚本输出 → markdown 正文。**纯函数**（不读时钟、不碰文件）。"""
    out: list[str] = [
        header(asof=asof, script_id=script_id, script_version=script_version,
               stub=stub),
        "",
        "## 口径（写死，改它要改代码）",
        "",
        f"- **样本**：`candidate_snapshots` × `candidate_members` 里 `asof <= {asof}` "
        "的候选（带池别与 `adj_score`）",
        f"- **前视窗口**：其后 **{ctx['n_days']} 个交易日**（常量 `inputs.FORWARD_DAYS`）；"
        f"窗口末端必须 `<= {asof}`，走不完的样本整条剔除并记原因",
        "- **命中判据**：`ret_pct > 0`（0 与负都算未命中）",
        "- **价格口径**：`data/adjust.py::load_bars_adjusted`（ADR-004 复权链）。"
        "算不出的标的（ETF / 链有缺口）**拒绝服务**并剔除 —— 不拿不复权价冒充复权价",
        "- **分桶**：池别（short/mid/long）× `adj_score` 五分桶（[0,20) … [80,100]）",
        "- **样本不足**：`None` ＋ `na_reasons`（**不许给 0.0 冒充「胜率 0%」**）",
        "- **回测指标**：`plugin_backtests` 的已落库读数，**不重跑回放**",
        "- **错判案例**：`m2/cases.py::miss_cases`（模块2 回流的唯一口径，只读、"
        "不写表、归因恒空 D-31）",
        "",
        "## 输入侧事实",
        "",
        f"- 目标日：`{asof}`；前视窗口：{summary['n_days']} 个交易日",
        f"- 候选（`asof <= {asof}`）：{summary['n_candidates']} 条",
        f"- 可用样本：{summary['n_samples']} 条"
        f"；剔除：{summary['n_dropped']} 条",
        f"- 样本快照日范围：{_range(summary['sample_asof_range'])}",
        f"- 回测台账条目：{summary['n_backtests']} 条",
        f"- 模块2 错判案例：{summary['bad_cases']['n_cases']} 条"
        f"（窗口内方向错的共 {summary['bad_cases']['n_miss_total']} 条，"
        f"上限 {summary['bad_cases']['limit']}）",
    ]
    if summary["calendar_error"]:
        out.append(f"- ⚠️ 交易日历读取有问题：{summary['calendar_error']}")
    if summary["drop_reasons"]:
        out += ["", "### 剔除的原因（逐类计数，全部来自 PIT 与复权链的判据）", ""]
        out += [f"- {d['n']} 条：{d['reason']}" for d in summary["drop_reasons"]]

    out += [
        "",
        "## 脚本输出（原文照贴，不润色）",
        "",
        "```json",
        json.dumps(analysis, ensure_ascii=False, sort_keys=True, indent=2),
        "```",
        "",
        "## bad_case_list（脚本返回）",
        "",
        "```json",
        json.dumps(bad_cases, ensure_ascii=False, sort_keys=True, indent=2),
        "```",
        "",
    ]
    if stub:
        out += [
            "> ⚠️ 上面两段是**桩的输出**，不是复盘结论：现役脚本按设计（文档 01 §3"
            "「不做」）返回空结果。本报告这一刻的价值只有「输入侧事实」这一半 ——"
            " 真读数要等 v1.1.0 过人工闸门（`plugin approve`）之后。",
            "",
        ]
    return "\n".join(out)
