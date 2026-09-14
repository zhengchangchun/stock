"""回测与模拟盘的公共内核（P4）。

分层：
  `portfolio`  持仓推进（成交/成本/T+1/涨跌停/估值）—— 回测与模拟盘共用
  `engine`     事件驱动回测（T 日收盘出信号 → T+1 开盘成交）
  `metrics`    绩效指标（收益/回撤/波动/夏普）
  `benchmark`  基准对照（buy_and_hold / index_300，R5）

**输入口径铁律**：`engine.run_backtest` 只接受 `adj_mode == "qfq"` 的复权 K 线
（由 `stocklab.data.adjust` 的读取层产出），不复权价直接拒绝 —— 否则除权日
会留下假跌幅，把「分红」当成「暴跌」（见 ADR-004）。
"""
