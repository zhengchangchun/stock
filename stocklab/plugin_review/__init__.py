"""插桩5「定期复盘分析」的接线（P58）。

文档 01 的插桩清单第 5 行是「定期复盘分析脚本：AI优化子任务内部，计算因子胜率、
回测指标」，`docs/plans/2026-09-21-需求澄清录.md` D-10 的批注是「**已定义未接线**
…… 不要当成已完成」。本包就是那条调用路径：

- `inputs`  —— 输入装配（历史快照 / 回测台账 / 模块2 回流；全部只读、全部 `<= asof`）
- `service` —— 走 `plugin/lifecycle` 的既有生命周期跑在役脚本
- `render`  —— `reports/plugin-review/<asof>.md`（**不是** session review 那份）
- `store`   —— `plugin_reviews` 台账（append-only，幂等键 `(asof, script_id)`）

入口是 CLI 的 `candidate review --asof <日>`（独立命令：复盘不是每日动作，因此
不进 `candidate run` 的 12 步主干、不进 `CLOSE_STEPS`；月度链上那一步是**非阻断**的）。
"""

from stocklab.plugin_review.service import PLUGIN_ID, report_path, run_review

__all__ = ["PLUGIN_ID", "report_path", "run_review"]
