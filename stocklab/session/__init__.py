"""调度链（P11）：盘中快照采集 → 收盘回填 → 到期验证 → 复盘报告。

## 本包负责的四件事

1. **盘中快照**（`quotes.py` + `store.py`）：交易日多次抓腾讯实时行情落
   `quote_snapshots`（append-only）。身份键取 `(code, trade_date, ts)` —— 源站自己报的
   时刻，故**非交易日重复抓取天然幂等**（源站返回的仍是上一交易日最后一条 tick）。
2. **收盘回填**（`close.py`）：当日收盘后把该日 `amount`/`turnover` 补进 `bars_daily`
   **对应那一行**。只在原值为 NULL 时写（`COALESCE`），作用域锁死单个 `date` ——
   历史 14164 行的 NULL **一行都不动**（NULL 的语义是「当日未采集」，不是 0）。
3. **`session tick`**（`tick.py`）：一次调用完成「采集 → 验证到期预测 → 落库」，
   输出机器可读 JSON 摘要。幂等：同 `ts` 的快照不重复写，已验证的预测不重复记分。
4. **`review daily`**（`review.py`）：15:30 的整体数据复盘，落 `reports/`。

## 本项目**不建** cron

调度归 nanobot 侧（ADR-001 D-05）。本包只提供「能被 cron 调用」的一次性命令，
见 `docs/scheduling.md`。这里没有常驻进程、没有 sleep 循环、没有后台线程。
"""
