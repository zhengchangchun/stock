-- ============================================================
-- stock-lab schema（前滚迁移；所有 CREATE 必须幂等）
-- 单位：价格=元 REAL，成交量=股 INTEGER，成交额=元 REAL
-- 时间：ISO8601 带时区字符串
-- ============================================================

-- ---------- 标的与日历 ----------
CREATE TABLE IF NOT EXISTS instruments (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    market      TEXT NOT NULL CHECK (market IN ('sz', 'sh', 'bj')),
    board       TEXT NOT NULL CHECK (board IN ('main', 'gem', 'star', 'bse')),
    type        TEXT NOT NULL DEFAULT 'stock',
    sector      TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    listed_at   TEXT,
    delisted_at TEXT,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trading_calendar (
    date        TEXT PRIMARY KEY,
    is_open     INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- ---------- 行情 ----------
CREATE TABLE IF NOT EXISTS bars_daily (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    pre_close   REAL,
    volume      INTEGER NOT NULL,      -- 股
    amount      REAL,                  -- 元，部分源无此字段
    turnover    REAL,                  -- 换手率 %
    adj_mode    TEXT NOT NULL DEFAULT 'none' CHECK (adj_mode IN ('none', 'qfq', 'hfq')),
    is_suspended INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (code, date)
);

-- 除权除息事件（ADR-001 D-01 自建因子链的输入；ADR-004 记录实测依据）
-- PK (code, cqr)：同一除权日同一标的在实测数据里只出现一条。
-- `content` 是**唯一可信**的条款来源；`fh_sh` 实测有两处不可信（税后值 / 空串），
-- 只作交叉校验用，故允许为 NULL。
CREATE TABLE IF NOT EXISTS corp_actions (
    code        TEXT NOT NULL,
    cqr         TEXT NOT NULL,          -- 除权日
    djr         TEXT,
    fh_sh       REAL,                   -- 每 10 股派息（元）；源站此字段不可信，见 ADR-004
    content     TEXT NOT NULL DEFAULT '',  -- 源站原文，如 "10派20元转15股"
    source      TEXT NOT NULL,
    first_seen  TEXT NOT NULL,          -- 首次入库时间，重采不重置
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (code, cqr)
);

CREATE TABLE IF NOT EXISTS adj_factors (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    factor      REAL NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (code, date)
);

-- ---------- 复权链的「不可用区间」（ADR-004 §无法定价事件）----------
-- 一行 = 一个**无法定价**的除权事件（`FHcontent` 为空 / 含配股 → 算不出复权系数 k）。
-- 这类事件的 k 不进因子链，于是 `adj_factors` 在它之后的行**缺乘了一个 k**
-- （数值非 NULL、看着正常，但拿它算跨越该事件的收益就是假的）。
-- 600690 有 5 个此类事件，导致其 2001-01-15 之前的 1745 根 K 线不可用于收益计算。
--
-- 可用性判据（与 `adjust.adjust_bars` 共用同一语义，**不是**逐行标记）：
--   窗口 [t_min, base] 可用  ⟺  不存在 blackout.cqr ∈ (t_min, base]
-- 逐行打标无法表达这个判据（第 500 行的可用性取决于窗口起点），所以这里存
-- **事件**而不是存「行的好坏」——语义精确，且不会出现「标记看着对、算法另算」。
CREATE TABLE IF NOT EXISTS adj_factor_blackout (
    code        TEXT NOT NULL,
    cqr         TEXT NOT NULL,          -- 无法定价的除权日
    reason      TEXT NOT NULL,          -- 为什么算不出来（原文/原因）
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (code, cqr)
);

-- 每个标的的可用下界：usable_from 之前的行情不可用于收益计算。
CREATE VIEW IF NOT EXISTS v_adj_usable AS
SELECT code, MAX(cqr) AS usable_from, COUNT(*) AS n_unusable
FROM adj_factor_blackout GROUP BY code;

CREATE TABLE IF NOT EXISTS money_flow_daily (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    main_net    REAL,                  -- 元
    small_net   REAL,
    mid_net     REAL,
    big_net     REAL,
    xl_net      REAL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS valuation_daily (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    pe_ttm      REAL,
    pb          REAL,
    total_mv    REAL,                  -- 元
    div_yield   REAL,
    pe_pct_3y   REAL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS sector_daily (
    sector_code TEXT NOT NULL,
    date        TEXT NOT NULL,
    name        TEXT NOT NULL,
    pct_chg     REAL,
    main_net    REAL,
    rank        INTEGER,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (sector_code, date)
);

CREATE TABLE IF NOT EXISTS market_state (
    date          TEXT NOT NULL,
    index_code    TEXT NOT NULL,
    close         REAL NOT NULL,
    ma_state      TEXT,
    vol_state     TEXT,
    breadth_up    INTEGER,
    breadth_down  INTEGER,
    regime_label  TEXT,
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (date, index_code)
);

-- ---------- 特征快照（append-only） ----------
-- A1：自增主键 + (code,date,version) 唯一，使「重算追加」与 append-only 共存
CREATE TABLE IF NOT EXISTS features_daily (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,
    date            TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    feature_set     TEXT NOT NULL DEFAULT 'core',
    -- 核心特征宽表列（可直接 SQL 查询 / join）
    close           REAL,
    ma20            REAL,
    ma60            REAL,
    atr14           REAL,
    vol_ratio_5_20  REAL,
    ret_1d          REAL,
    ret_5d          REAL,
    main_net_5d     REAL,
    pe_pct_3y       REAL,
    regime_label    TEXT,
    -- 长尾 / 实验性特征
    json_payload    TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    data_version    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (code, date, feature_version, feature_set)
);

CREATE INDEX IF NOT EXISTS idx_features_code_date ON features_daily (code, date);

-- ---------- 预测与验证（append-only） ----------
CREATE TABLE IF NOT EXISTS predictions (
    pred_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT NOT NULL,
    asof_date           TEXT NOT NULL,
    target_date         TEXT NOT NULL,
    direction_up        REAL NOT NULL,
    direction_flat      REAL NOT NULL,
    direction_down      REAL NOT NULL,
    range_lo            REAL,
    range_hi            REAL,
    key_levels_json     TEXT,
    action              TEXT NOT NULL CHECK (action IN ('hold','trim','add','exit','wait')),
    size_pct            REAL NOT NULL,
    invalidate_if       TEXT NOT NULL,
    strategy_mix_json   TEXT NOT NULL,
    regime_label        TEXT,
    feature_snapshot_id INTEGER REFERENCES features_daily (snapshot_id),
    model_version       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','failed')),
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_predictions_target ON predictions (target_date);

-- 预测的身份键（P6）：同 (code, asof_date, model_version) 只能有一条。
-- 这是**结构性**防线而不是代码检查：即便将来有别的写入方绕开
-- `predict.store.insert_prediction` 直接写 SQL，「同一天同一模型被静默覆盖」
-- 仍然做不到 —— 配合 append-only 的 DELETE 触发器，连
-- `INSERT OR REPLACE`（隐式删行重插）也会被 ABORT。
-- 要改预测就升 model_version（与 features_daily 要改就升 feature_version 同款纪律）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_predictions_identity
    ON predictions (code, asof_date, model_version);

CREATE TABLE IF NOT EXISTS verifications (
    verification_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    pred_id           INTEGER NOT NULL REFERENCES predictions (pred_id),
    target_date       TEXT NOT NULL,
    actual_close      REAL,
    actual_pct        REAL,
    benchmark_pct     REAL,            -- 基准同期涨跌，用于算超额
    hit_direction     INTEGER,
    hit_range         INTEGER,
    hit_levels        INTEGER,
    sim_pnl           REAL,
    score_direction   REAL,
    score_range       REAL,
    score_level       REAL,
    score_action      REAL,
    total_score       REAL,
    invalidated       INTEGER NOT NULL DEFAULT 0,
    attribution_auto  TEXT,            -- 程序可判定：DATA / NOISE
    attribution_manual TEXT,           -- 人工标注：SIGNAL / STRATEGY / MODEL
    notes             TEXT,
    created_at        TEXT NOT NULL
);

-- 结构性第二道防线（P7）：一个 `pred_id` 只能有**一条**验证记录。
-- `verifications` 是准确率的账本：同一份预测同一根 bar 出现两行，
-- 统计时就得去重，而「当前成绩是什么」会变成谁也说不清的事。
-- 补分（不可评分 → 可评分）走 UPDATE 结果列、**不新增行**，故与唯一索引不冲突
-- （ADR-002：身份列不可变 + 结果列可更新 + DELETE 全禁）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_verifications_identity
    ON verifications (pred_id);

-- ---------- 策略 ----------
CREATE TABLE IF NOT EXISTS strategy_registry (
    strategy_id   TEXT NOT NULL,
    version       TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (status IN ('candidate','active','downweighted','retired')),
    stage         TEXT,
    added_at      TEXT NOT NULL,
    promoted_at   TEXT,
    retired_at    TEXT,
    retire_reason TEXT,
    PRIMARY KEY (strategy_id, version)
);

CREATE TABLE IF NOT EXISTS strategy_daily (
    strategy_id     TEXT NOT NULL,
    date            TEXT NOT NULL,
    signal          TEXT,
    sim_return      REAL,
    benchmark_return REAL,
    n_obs           INTEGER,
    PRIMARY KEY (strategy_id, date)
);

-- ---------- 模拟盘 / 实盘 ----------
CREATE TABLE IF NOT EXISTS sim_trades (
    trade_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    code        TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy','sell')),
    price       REAL NOT NULL,
    qty         INTEGER NOT NULL,
    fee         REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_portfolio (
    date          TEXT NOT NULL,
    strategy_id   TEXT NOT NULL,
    cash          REAL NOT NULL,
    positions_json TEXT NOT NULL,
    nav           REAL NOT NULL,
    drawdown      REAL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (date, strategy_id)
);

-- A2：实盘只存成交流水，持仓与净值一律由流水推导
CREATE TABLE IF NOT EXISTS real_trades (
    trade_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    code       TEXT NOT NULL,
    side       TEXT NOT NULL CHECK (side IN ('buy','sell')),
    price      REAL NOT NULL,
    qty        INTEGER NOT NULL,
    fee        REAL NOT NULL DEFAULT 0,
    note       TEXT,
    created_at TEXT NOT NULL
);

-- ---------- 治理 ----------
CREATE TABLE IF NOT EXISTS decisions (
    dec_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    scope         TEXT NOT NULL,
    change        TEXT NOT NULL,
    evidence      TEXT,
    oos_result    TEXT,
    rollback_plan TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_quality (
    issue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    source      TEXT NOT NULL,
    code        TEXT,
    issue_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'warn' CHECK (severity IN ('info','warn','error')),
    detail      TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    resolved    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (date, source, code, issue_type)
);

CREATE TABLE IF NOT EXISTS system_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    module      TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('debug','info','warn','error')),
    message     TEXT NOT NULL,
    context_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_events_ts ON system_events (ts);

CREATE TABLE IF NOT EXISTS raw_fetch_cache (
    cache_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    url          TEXT NOT NULL,
    params_key   TEXT NOT NULL,
    body         BLOB NOT NULL,
    content_sha256 TEXT NOT NULL,
    encoding     TEXT NOT NULL DEFAULT 'utf-8',
    fetched_at   TEXT NOT NULL,
    UNIQUE (source, params_key)
);

CREATE TABLE IF NOT EXISTS job_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name     TEXT NOT NULL,
    scheduled_at TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running','ok','failed')),
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_runs_name ON job_runs (job_name, started_at);

-- ---------- append-only 触发器（R8 / ADR-001 §5） ----------
-- 全保护（UPDATE + DELETE 皆禁）：features_daily / predictions / sim_trades / decisions
CREATE TRIGGER IF NOT EXISTS trg_features_no_update
BEFORE UPDATE ON features_daily
BEGIN SELECT RAISE(ABORT, 'features_daily is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_features_no_delete
BEFORE DELETE ON features_daily
BEGIN SELECT RAISE(ABORT, 'features_daily is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_predictions_no_update
BEFORE UPDATE ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_predictions_no_delete
BEFORE DELETE ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_sim_trades_no_update
BEFORE UPDATE ON sim_trades
BEGIN SELECT RAISE(ABORT, 'sim_trades is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_sim_trades_no_delete
BEFORE DELETE ON sim_trades
BEGIN SELECT RAISE(ABORT, 'sim_trades is append-only'); END;

-- decisions 同样属于 append-only 集合（ADR-001 §5），UPDATE 亦禁
CREATE TRIGGER IF NOT EXISTS trg_decisions_no_update
BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_decisions_no_delete
BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;

-- verifications：刻意例外（ADR-002）。
-- 允许 UPDATE 结果列与 attribution_manual（人工回填），但身份列不可变；DELETE 全禁。
CREATE TRIGGER IF NOT EXISTS trg_verifications_no_update_identity
BEFORE UPDATE ON verifications
WHEN OLD.verification_id IS NOT NEW.verification_id
  OR OLD.pred_id IS NOT NEW.pred_id
  OR OLD.target_date IS NOT NEW.target_date
  OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'verifications identity columns are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_verifications_no_delete
BEFORE DELETE ON verifications
BEGIN SELECT RAISE(ABORT, 'verifications is append-only (delete)'); END;

-- ---------- 实验决策台账（P8 Task 43，append-only） ----------
-- 「哪个变体在哪一段是 WIN/LOSE」从此可 SQL 查。
--
-- **只落决策，不落预测**：变体预测仍然一行都不写 `predictions` / `verifications`
-- （P8 §1.1 的架构决策不变）。本表记的是「这次评估判了什么」，不是「模型算了什么」。
--
-- 一行的粒度 = (变体, 分段, 指标)。`metric` ∈ {direction, brier}：
-- 两者的 `delta` / `ci_low` / `ci_high` 各自独立，而 `gate_status` 是**分段级**的
-- （`metrics.gate` 同时看两个指标才给 `WIN`/`LOSE`/`FLAT`/`INSUFFICIENT`），
-- 所以同段的两行共享一个 `gate_status`。`decision` 是**整场实验**的结论
-- （`promoted` / `falsified` / `inconclusive`），同一次运行的所有行相同。
--
-- 幂等键 = (variant_id, split, metric, metric_version, report_sha256)。
-- 报告 sha256 覆盖了全部闸门数字，所以「同一份报告重跑」= 同一行 → 幂等跳过；
-- 换了区间/配置 → 新 sha256 → **追加新行**（历史结论不被改写）。
CREATE TABLE IF NOT EXISTS experiment_decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id      TEXT NOT NULL,
    split           TEXT NOT NULL CHECK (split IN ('train', 'validate', 'test')),
    metric          TEXT NOT NULL CHECK (metric IN ('direction', 'brier')),
    metric_version  TEXT NOT NULL,
    delta           REAL,
    ci_low          REAL,
    ci_high         REAL,
    gate_status     TEXT NOT NULL
                    CHECK (gate_status IN ('WIN', 'LOSE', 'FLAT', 'INSUFFICIENT')),
    decision        TEXT NOT NULL
                    CHECK (decision IN ('promoted', 'falsified', 'inconclusive')),
    report_sha256   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (variant_id, split, metric, metric_version, report_sha256)
);

CREATE TRIGGER IF NOT EXISTS trg_experiment_decisions_no_update
BEFORE UPDATE ON experiment_decisions
BEGIN SELECT RAISE(ABORT, 'experiment_decisions is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_experiment_decisions_no_delete
BEFORE DELETE ON experiment_decisions
BEGIN SELECT RAISE(ABORT, 'experiment_decisions is append-only'); END;
