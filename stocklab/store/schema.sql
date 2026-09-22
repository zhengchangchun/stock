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

-- ---------- 已公告的休市安排（P30）----------
-- **语义与 trading_calendar 严格区分**：trading_calendar = 「某个日期是不是**已采集到的**
-- 交易日」（回看，只用已发生的指数日线日期集合构建）；本表 = 「交易所**已公告**的开/休市」
-- （前瞻，来自年度休市安排通知与单节公告）。两者混用会让 `Calendar.load` 把未来日期当成
-- 「已采集交易日」，直接污染回测轴与 predict 的 session_check，故**另起一张表**。
--
-- PIT 判断（ADR-013）：休市安排是交易所**提前公告的公开日历信息**，不是价格/成交数据；
-- 在 asof 时点它已经公开可得，因此允许用于决定 `target_date`。风险边界见 ADR-013。
--
-- 主键 (date, source_url) 而非 date：同一日期可能被「年度通知」与「单节公告」各写一条
-- （内容相同，无害）；**改期/临时休市**则是新公告=新行，读取侧取 `published_at` 最新者。
-- 于是全程只有 INSERT —— 不触发 append-only 触发器，也不会出现「INSERT OR IGNORE 把
-- 已改期的旧值静默留住」这种错误。
--
-- `covered_year` 是**覆盖判据**：只有年度通知（doc_kind='annual'）才代表「该年**全部**日期的
-- 开/休市都已公告」。单节公告只覆盖那一个节，不许拿它声称整年都知道。
CREATE TABLE IF NOT EXISTS market_holidays (
    date         TEXT NOT NULL,          -- YYYY-MM-DD
    is_open      INTEGER NOT NULL CHECK (is_open IN (0, 1)),
    source       TEXT NOT NULL,          -- 发布机构：'sse'
    doc_kind     TEXT NOT NULL CHECK (doc_kind IN ('annual', 'holiday')),
    covered_year INTEGER NOT NULL,       -- 这份公告所覆盖的年份（本行日期所在的年）
    source_url   TEXT NOT NULL,
    published_at TEXT NOT NULL,          -- 公告发布日（源站给出，非抓取时间）
    created_at   TEXT NOT NULL,
    PRIMARY KEY (date, source_url)
);

CREATE INDEX IF NOT EXISTS idx_market_holidays_date ON market_holidays (date);

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

-- 资金流（P28；append-only，见末尾触发器）。源 = 新浪 MoneyFlow.ssl_qsfx_zjlrqs。
-- 单位：close/ratio_amount = 元，change_ratio/turnover = %，main_net/xl_net = 元（净额，可负）。
-- ⚠️ `turnover` 是**新浪口径**（百分数值 ×100）：同一交易日 `bars_daily.turnover`（%）是它的
--    1/100 —— 两表差 100 倍，消费方不得直接对拍（P28 计划 §2.1 实测结论）。
-- PIT：下游只允许读 `date <= asof`；历史行一次写入后永不改写（触发器 + 首写保留）。
-- `resp_sha256` / `cache_key` 让每一行可复现、可溯源（指回 raw_cache 原始响应）。
-- 与 `store/migrate.py::MIGRATE_P28_DDL` 保持同文（空表重定义的单一真源需两处同步）。
CREATE TABLE IF NOT EXISTS money_flow_daily (
    code          TEXT NOT NULL,
    date          TEXT NOT NULL,      -- opendate（新浪原生 YYYY-MM-DD）
    close         REAL,               -- trade 收盘价 元
    change_ratio  REAL,               -- changeratio %
    turnover      REAL,               -- 换手率（新浪口径，见上）
    main_net      REAL,               -- netamount 主力净额 元（= 超大单 + 大单）
    xl_net        REAL,               -- r0_net 超大单净额 元
    ratio_amount  REAL,               -- ratioamount 元
    source        TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    resp_sha256   TEXT NOT NULL,
    cache_key     TEXT,
    PRIMARY KEY (code, date)
);

-- 估值（P28；append-only，见末尾触发器）。源 = 东财 datacenter RPT_VALUEANALYSIS_DET。
-- 单位：total_mv/close_price = 元，total_shares = 股，change_rate = %。
-- ⚠️ 非 PIT 风险：东财按**最新股本重算整条历史 PE/PB**。对策 = 首写保留（不覆盖），
--    重采检测到同 (code,date) 值变化时只记 warn 不覆盖（P28 计划 §4）。
-- `pe_pct_3y`（3 年分位）是**派生特征**，由特征层从本表历史算出，不作为原始列存。
CREATE TABLE IF NOT EXISTS valuation_daily (
    code          TEXT NOT NULL,
    date          TEXT NOT NULL,      -- TRADE_DATE 裁剪为 YYYY-MM-DD
    pe_ttm        REAL,               -- PE_TTM
    pb            REAL,               -- PB_MRQ（最新季报口径）
    ps_ttm        REAL,               -- PS_TTM
    total_mv      REAL,               -- TOTAL_MARKET_CAP 元
    total_shares  REAL,               -- TOTAL_SHARES 股
    close_price   REAL,               -- CLOSE_PRICE 元
    change_rate   REAL,               -- CHANGE_RATE %
    source        TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    resp_sha256   TEXT NOT NULL,
    cache_key     TEXT,
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

-- ---------- 盘中快照（P11，append-only） ----------
-- 一行 = 源站在**某一时刻**对某标的下发的截面。身份键取 (code, trade_date, ts)：
-- `ts` 是**源站自己报的时刻**，也就是「这份截面描述的是哪一刻」——不是我们抓取的时刻
-- （`fetched_at` 只是呈现，不进键，见 ADR-005）。
--
-- 由此得到一条免费的强性质：**非交易日重复抓取天然幂等**。非交易日源站返回的仍是
-- 上一交易日的最后一条 tick，键与之前那条相同 → `identical`，一行都不增。
-- 于是「今天是不是交易日」猜错也不会造出假行。
--
-- `amount` / `turnover` 是**当日累计**量（源站口径），收盘后的最后一条即全天值 ——
-- 收盘回填（`session/close.py`）正是取这一条写进 `bars_daily`。
CREATE TABLE IF NOT EXISTS quote_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    trade_date  TEXT NOT NULL,          -- 源站时间戳所属交易日 YYYY-MM-DD
    ts          TEXT NOT NULL,          -- 源站时间戳 YYYYMMDDHHMMSS（身份的一部分）
    price       REAL NOT NULL,
    pre_close   REAL,
    open        REAL,
    high        REAL,
    low         REAL,
    volume      INTEGER NOT NULL,       -- 股（当日累计）
    amount      REAL,                   -- 元（当日累计）
    turnover    REAL,                   -- %（当日累计）
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,          -- 我们抓到的时刻；**不进身份键**
    UNIQUE (code, trade_date, ts)
);

CREATE INDEX IF NOT EXISTS idx_quote_snapshots_date
    ON quote_snapshots (trade_date, code, ts);

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
    created_at          TEXT NOT NULL,
    -- P32：来源标记（回放 replay / 实时 live）。入库时就由写入路径确定，非事后推断。
    -- 历史行（加列前已存在的）保持 NULL：NULL 语义 = 「无来源标记，退回
    -- `created_at[:10] == asof_date` 推断」。NULL 通过 CHECK（NULL IN (...) → NULL 非
    -- FALSE），不冒充事实。列放在 created_at 之后，与 ALTER TABLE ADD COLUMN
    -- （append-only 老库的迁移路径）追加到尾部的顺序一致。
    origin              TEXT CHECK (origin IN ('live','replay'))
);

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
-- ---------- 实盘账本（P12，append-only） ----------
-- 成交。**刻意不加 UNIQUE(date,code,side,price,qty,fee)**：真实成交可能同价同量同费
-- （同一天分两笔各买 100 股 @86.80、佣金都是 5.09 是完全正常的真单），
-- 唯一约束会**静默吃掉真单**。重复录入防护改由 `ledger_idem` 幂等键 +
-- 显式 `--allow-duplicate` 二次确认承担，见 ADR-006。
-- 改错**只能冲正**（写一笔反向记录 + note），不许 UPDATE —— 触发器钉住。
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

-- 本金与现金流（append-only）。`amount` **有符号**：正 = 流入组合，负 = 流出组合。
-- 符号与 `kind` 必须一致（`deposit`/`dividend` > 0，`withdraw`/`fee`/`tax` < 0），
-- 由写入口 `portfolio/ledger.py` 强制 —— 存绝对值再靠 kind 推方向，
-- 迟早会在某处被加错符号，且错了看不出来。
CREATE TABLE IF NOT EXISTS cash_flows (
    flow_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN
                 ('deposit','withdraw','dividend','fee','tax','other')),
    amount     REAL NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cash_flows_date ON cash_flows (date, flow_id);

-- 幂等键台账（append-only）：把「命令重试/补跑」与「真重复成交」分开。
-- 键由调用方给（`--idempotency-key`），同一 key 第二次进来 = 幂等命中，不写第二行。
-- 刻意**不**用业务元组当键：业务元组相同既可能是重试也可能是真单，无法区分；
-- 只有调用方知道自己是不是在重试。见 ADR-006。
-- 键 = **(idem_key, scope)**：同一个 key 字符串用在成交与用在现金流上是两回事，
-- 各查各的表。scope 因此不是装饰列，它就在主键里。
CREATE TABLE IF NOT EXISTS ledger_idem (
    idem_key   TEXT NOT NULL,
    scope      TEXT NOT NULL CHECK (scope IN ('trade','cash')),
    row_id     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (idem_key, scope)
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

-- 实盘账本三表：全保护（UPDATE + DELETE 皆禁）。
-- 账本是「我真做过什么」的唯一记录，事后改写会让所有归因失效。
-- 改错的唯一合法路径是**冲正**（追加一笔反向记录），它会作为新的一行出现、
-- 带着 note 说明为什么 —— 历史因此仍然可读，而不是被抹掉。
CREATE TRIGGER IF NOT EXISTS trg_real_trades_no_update
BEFORE UPDATE ON real_trades
BEGIN SELECT RAISE(ABORT, 'real_trades is append-only (改错请冲正)'); END;

CREATE TRIGGER IF NOT EXISTS trg_real_trades_no_delete
BEFORE DELETE ON real_trades
BEGIN SELECT RAISE(ABORT, 'real_trades is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_cash_flows_no_update
BEFORE UPDATE ON cash_flows
BEGIN SELECT RAISE(ABORT, 'cash_flows is append-only (改错请冲正)'); END;

CREATE TRIGGER IF NOT EXISTS trg_cash_flows_no_delete
BEFORE DELETE ON cash_flows
BEGIN SELECT RAISE(ABORT, 'cash_flows is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_idem_no_update
BEFORE UPDATE ON ledger_idem
BEGIN SELECT RAISE(ABORT, 'ledger_idem is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_idem_no_delete
BEFORE DELETE ON ledger_idem
BEGIN SELECT RAISE(ABORT, 'ledger_idem is append-only'); END;

-- quote_snapshots：全保护（UPDATE + DELETE 皆禁）。盘中截面是**历史事实**，
-- 源站事后修订某个 tick 不构成改写历史记录的理由 —— 修订会以 `conflict` 上报并留痕，
-- 交给人决定，而不是让代码静默覆盖（ERROR_DIARY「取不到就回退」的同型错误）。
CREATE TRIGGER IF NOT EXISTS trg_quote_snapshots_no_update
BEFORE UPDATE ON quote_snapshots
BEGIN SELECT RAISE(ABORT, 'quote_snapshots is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_quote_snapshots_no_delete
BEFORE DELETE ON quote_snapshots
BEGIN SELECT RAISE(ABORT, 'quote_snapshots is append-only'); END;

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
-- 幂等键 = (variant_id, split, metric, metric_version, gate_status, delta,
-- ci_low, ci_high) —— **取语义（决策内容），不取呈现（报告文本）**（P9-b / ADR-005）。
-- 同一串闸门数字重跑 → 同一行 → 幂等跳过；换了区间/配置 → 数字变 → **追加新行**
-- （历史结论不被改写）。`report_sha256` 仍照写、照可查（指回报告文件），但不进键 ——
-- 它哈希的是整份报告字典，连给人看的原因串都在内，拿它当键则「改一个错别字
-- 就多判一条决策」（ERROR_DIARY #17，实际污染过 4 行）。
--
-- **老库不迁移**（append-only：改 UNIQUE 要重建表 = 改写历史）。P9-b 之前建的库
-- 保留老约束 `UNIQUE(..., report_sha256)`，其中同语义重复的行是新键生效前的历史遗留。
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
    UNIQUE (variant_id, split, metric, metric_version,
            gate_status, delta, ci_low, ci_high)
);

CREATE TRIGGER IF NOT EXISTS trg_experiment_decisions_no_update
BEFORE UPDATE ON experiment_decisions
BEGIN SELECT RAISE(ABORT, 'experiment_decisions is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_experiment_decisions_no_delete
BEFORE DELETE ON experiment_decisions
BEGIN SELECT RAISE(ABORT, 'experiment_decisions is append-only'); END;

-- ---------- 模拟盘（P19，append-only） ----------
-- 三臂并行、每日一行净值，口径见 docs/plans/2026-09-15-p19-模拟盘骨架.md
-- 与 ADR-010。**不是预测账本**：本组表与 predictions / model_version 无关，
-- 生产模型的方向能力 ≈ 0（行级 38.14% / Brier 0.6581），模拟盘对照的是
-- **纪律与分散本身**，因此这里没有、也不许有「模型信号」列。
--
-- 账户 ≠ 组合：一个 account_id 就是一条独立的机械臂。
-- `arm-hold` 冻结、`arm-now` 镜像实盘账本、`arm-discipline-{05,10,15}` 各一档 ETF 目标。
CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id     TEXT PRIMARY KEY,     -- 'arm-hold' | 'arm-now' | 'arm-discipline-05' …
    arm            TEXT NOT NULL
                   -- 与 paper/config.py 的 ARM_KIND_* **同文**（改一处须同步另一处）。
                   -- 'agent' = 条文数字来自 paper_agent_decisions 当前有效的 spec；
                   -- 'agent_random' = 必需的随机对照臂（阶段 3 才有交易，此前只记净值）。
                   CHECK (arm IN ('hold', 'now', 'discipline', 'agent', 'agent_random')),
    etf_target_pct REAL,                 -- 纪律臂的 ETF 目标占比 %；hold/now 为 NULL
    start_date     TEXT NOT NULL,        -- 起跑日（收盘口径）YYYY-MM-DD
    initial_cash   REAL NOT NULL,
    initial_positions_json TEXT NOT NULL,-- [{"code","qty","cost_price"}]
    initial_nav    REAL NOT NULL,
    params_json    TEXT NOT NULL,        -- 冻结的口径参数（成本/整手/白名单…）
    created_at     TEXT NOT NULL
);

-- 模拟成交。成本**分列**存：佣金 / 印花税 / 过户费 / 滑点。
-- 存一个合计就查不出「ETF 免印花税」这条口径（ADR-008），口径错是静默错误。
-- UNIQUE 是防重复下单的结构性第二道防线（第一道是 paper_nav_daily 的存在性判据，
-- 顺序见 ADR-010：先查 nav 再决定要不要下单，否则会把自己的痕迹当成重复）。
CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,         -- 决策日（PIT）
    code          TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    ref_price     REAL NOT NULL,         -- 当日 PIT 收盘（参考价）
    fill_price    REAL NOT NULL,         -- 含滑点的实际成交价
    qty           INTEGER NOT NULL CHECK (qty > 0),
    commission    REAL NOT NULL,
    stamp_tax     REAL NOT NULL,
    transfer_fee  REAL NOT NULL,
    slippage_cost REAL NOT NULL,
    fee_total     REAL NOT NULL,
    asset_class   TEXT NOT NULL,         -- 'stock' | 'etf'（成本口径按它取）
    rule_citation TEXT NOT NULL,         -- 触发的规则条文（不许留空）
    reason        TEXT NOT NULL,         -- 决策原文（人读；含「为什么是这个股数」）
    binding_json  TEXT NOT NULL,         -- 生效的约束代号 JSON 数组（哪条把订单压小了）
    price_source  TEXT NOT NULL,         -- 价格出处：'snapshot' | 'bars'
    price_asof    TEXT NOT NULL,         -- 价格实际所属日期（PIT 可审计）
    created_at    TEXT NOT NULL,
    UNIQUE (account_id, date, code, side, qty, rule_citation)
);

-- 每日净值（每账户每日一行）。drawdown 由本行之前的净值序列算出，
-- 故必须**按日累积**才能读 —— 这也是它 append-only 的理由之一。
-- net_deposits 单独一列：arm-now 镜像实盘账本，出入金会改变 NAV 但不是收益，
-- 没有这一列就无法把「入金」与「赚了」分开（ADR-010）。
CREATE TABLE IF NOT EXISTS paper_nav_daily (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    cash          REAL NOT NULL,
    positions_json TEXT NOT NULL,
    market_value  REAL NOT NULL,
    nav           REAL NOT NULL,
    drawdown      REAL,                  -- 相对历史峰值；首行 = 0
    cum_cost      REAL NOT NULL,
    cum_return    REAL NOT NULL,         -- 相对**累计净入金**
    net_deposits  REAL NOT NULL,         -- 累计净入金（本金 + 存入 − 取出）
    index_300_level     REAL,            -- 沪深300 收盘点位（同一 asof）
    index_300_asof      TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (account_id, date)
);

CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_update
BEFORE UPDATE ON paper_accounts
BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_delete
BEFORE DELETE ON paper_accounts
BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_trades_no_update
BEFORE UPDATE ON paper_trades
BEGIN SELECT RAISE(ABORT, 'paper_trades is append-only (改错请冲正)'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_trades_no_delete
BEFORE DELETE ON paper_trades
BEGIN SELECT RAISE(ABORT, 'paper_trades is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_nav_daily_no_update
BEFORE UPDATE ON paper_nav_daily
BEGIN SELECT RAISE(ABORT, 'paper_nav_daily is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_nav_daily_no_delete
BEFORE DELETE ON paper_nav_daily
BEGIN SELECT RAISE(ABORT, 'paper_nav_daily is append-only'); END;

-- ---------- 智能体动态编排臂的决定台账（P37，append-only） ----------
-- 一次「复审」= 一行：改了什么（spec_before/after）、试了几版（n_trials）、
-- 被拒了哪些（rejected_json）、喂进去的 PIT 快照指纹（context_sha256）、
-- 用的什么模型与提示词（model_id / prompt_sha256 / seed）。
--
-- 为什么这些字段必须落地，而不是留在结果里：一个能反复改规则、又能看结果的
-- 智能体，天然会把历史噪声调成「策略」。试错次数、变更前后的原文、输入快照，
-- 是事后区分「编排带来了信息」与「多试几次的好运」的唯一依据。
-- 所以 `agent_kind='manual'`（人手写的 spec）也必须走同一张表。
--
-- 幂等键 `UNIQUE(arm, asof)`：同一天同一臂只允许一版。**它不是用来静默覆盖的**
-- —— 写入前先按这个键精确查一行，内容一致就当作已存在返回，不一致就报错让人处理
-- （ERROR_DIARY #25：先写再让唯一键兜底，会把自己刚写的行当成重复）。
--
-- `arm` 列在这里存的是**账户 id**（如 'arm-agent'），与 `paper_accounts.arm`
-- （存的是一类臂的 kind，如 'agent'）同名不同义 —— 台账的主语是「哪条臂在改」。
CREATE TABLE IF NOT EXISTS paper_agent_decisions (
    decision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    arm              TEXT NOT NULL,        -- 账户 id：'arm-agent' | 'arm-agent-random'
    asof             TEXT NOT NULL,        -- 复审日（PIT：只许喂 <= 这天的数据）
    agent_kind       TEXT NOT NULL
                     CHECK (agent_kind IN ('manual', 'llm', 'random')),
    model_id         TEXT NOT NULL,        -- 'manual' / '<模型名>'（换模型 = 换口径）
    prompt_sha256    TEXT NOT NULL,        -- 提示词（含温度）指纹（换提示词 = 换口径）
    seed             INTEGER NOT NULL DEFAULT 0,
    context_sha256   TEXT NOT NULL,        -- 喂进去的 PIT 快照指纹（同输入应得同 spec）
    spec_before_json TEXT NOT NULL,
    spec_after_json  TEXT NOT NULL,
    n_trials         INTEGER NOT NULL DEFAULT 1,  -- 本次试了几版（预算 K=3）
    rejected_json    TEXT NOT NULL DEFAULT '[]',  -- 被拒的版本与理由
    rationale        TEXT NOT NULL DEFAULT '',    -- 智能体自述（只作展示，不作证据）
    created_at       TEXT NOT NULL,
    UNIQUE (arm, asof)
);

CREATE TRIGGER IF NOT EXISTS trg_paper_agent_decisions_no_update
BEFORE UPDATE ON paper_agent_decisions
BEGIN SELECT RAISE(ABORT, 'paper_agent_decisions is append-only (改错请再审一版)'); END;

CREATE TRIGGER IF NOT EXISTS trg_paper_agent_decisions_no_delete
BEFORE DELETE ON paper_agent_decisions
BEGIN SELECT RAISE(ABORT, 'paper_agent_decisions is append-only'); END;

-- P28：估值 / 资金流原始表 append-only（历史行一次写入后永不改写；源站重算不覆盖）。
CREATE TRIGGER IF NOT EXISTS trg_valuation_daily_no_update
BEFORE UPDATE ON valuation_daily
BEGIN SELECT RAISE(ABORT, 'valuation_daily is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_valuation_daily_no_delete
BEFORE DELETE ON valuation_daily
BEGIN SELECT RAISE(ABORT, 'valuation_daily is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_money_flow_daily_no_update
BEFORE UPDATE ON money_flow_daily
BEGIN SELECT RAISE(ABORT, 'money_flow_daily is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_money_flow_daily_no_delete
BEFORE DELETE ON money_flow_daily
BEGIN SELECT RAISE(ABORT, 'money_flow_daily is append-only'); END;

-- P30：已公告休市安排 append-only（改期/临时休市 = 追加新公告行，不改旧行）。
CREATE TRIGGER IF NOT EXISTS trg_market_holidays_no_update
BEFORE UPDATE ON market_holidays
BEGIN SELECT RAISE(ABORT, 'market_holidays is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_market_holidays_no_delete
BEFORE DELETE ON market_holidays
BEGIN SELECT RAISE(ABORT, 'market_holidays is append-only'); END;

-- ---------- 插桩脚本版本库（模块1 骨架）----------
-- 语义见 docs/superpowers/specs/2026-09-18-插桩候选池骨架-design.md §5。
-- 状态不存列、由 plugin_audit 事件流推导 —— 审计链必须能回答「谁在什么时候批的」。
CREATE TABLE IF NOT EXISTS plugin_scripts (
    script_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_id     TEXT NOT NULL,
    version       TEXT NOT NULL,
    source_text   TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    note          TEXT,
    UNIQUE (plugin_id, version)
);

CREATE TABLE IF NOT EXISTS plugin_audit (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id  INTEGER NOT NULL,
    action     TEXT NOT NULL CHECK (action IN
                 ('submit','sandbox_pass','sandbox_fail','approve','reject','archive',
                  'start_validation','finish_validation','freeze','unfreeze')),
    actor      TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plugin_audit_script
    ON plugin_audit (script_id, audit_id);

CREATE TABLE IF NOT EXISTS plugin_backtests (
    backtest_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_script_id INTEGER NOT NULL,
    baseline_script_id  INTEGER,            -- 首版无 baseline，允许 NULL
    pool               TEXT NOT NULL CHECK (pool IN ('short','mid','long')),
    window_start       TEXT NOT NULL,
    window_end         TEXT NOT NULL,
    metrics_json       TEXT NOT NULL,
    verdict            TEXT NOT NULL CHECK (verdict IN
                         ('WIN','LOSE','INCONCLUSIVE')),
    overfit_flag       TEXT CHECK (overfit_flag IS NULL OR overfit_flag = 'suspected'),
    report_sha256      TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

-- ---------- 候选池（模块1 骨架）----------

CREATE TABLE IF NOT EXISTS candidate_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof        TEXT NOT NULL,
    run_kind    TEXT NOT NULL CHECK (run_kind IN ('light','weekly','quarterly')),
    params_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (asof, run_kind)                  -- 幂等键
);

-- status 是**该快照时点**的值，行本身不可变；当前状态 = 最新快照里的 status。
CREATE TABLE IF NOT EXISTS candidate_members (
    snapshot_id INTEGER NOT NULL,
    code        TEXT NOT NULL,
    pool        TEXT NOT NULL CHECK (pool IN ('short','mid','long')),
    raw_score   REAL NOT NULL,
    adj_score   REAL NOT NULL,
    reason      TEXT NOT NULL,
    risk_json   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
                  ('观察中','等待买点','已建仓','逻辑证伪移出')),
    entered_at  TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, code, pool)
);

CREATE TABLE IF NOT EXISTS candidate_rejects (
    snapshot_id INTEGER NOT NULL,
    code        TEXT NOT NULL,
    stage       TEXT NOT NULL CHECK (stage IN
                  ('pre_screen','industry_screen','score')),
    reason      TEXT NOT NULL,
    plugin_id   TEXT,
    PRIMARY KEY (snapshot_id, code, stage)
);

-- ---------- 新表的 append-only 触发器 ----------
CREATE TRIGGER IF NOT EXISTS trg_plugin_scripts_no_update
BEFORE UPDATE ON plugin_scripts
BEGIN SELECT RAISE(ABORT, 'plugin_scripts is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_plugin_scripts_no_delete
BEFORE DELETE ON plugin_scripts
BEGIN SELECT RAISE(ABORT, 'plugin_scripts is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_plugin_audit_no_update
BEFORE UPDATE ON plugin_audit
BEGIN SELECT RAISE(ABORT, 'plugin_audit is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_plugin_audit_no_delete
BEFORE DELETE ON plugin_audit
BEGIN SELECT RAISE(ABORT, 'plugin_audit is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_plugin_backtests_no_update
BEFORE UPDATE ON plugin_backtests
BEGIN SELECT RAISE(ABORT, 'plugin_backtests is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_plugin_backtests_no_delete
BEFORE DELETE ON plugin_backtests
BEGIN SELECT RAISE(ABORT, 'plugin_backtests is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_candidate_snapshots_no_update
BEFORE UPDATE ON candidate_snapshots
BEGIN SELECT RAISE(ABORT, 'candidate_snapshots is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_candidate_snapshots_no_delete
BEFORE DELETE ON candidate_snapshots
BEGIN SELECT RAISE(ABORT, 'candidate_snapshots is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_candidate_members_no_update
BEFORE UPDATE ON candidate_members
BEGIN SELECT RAISE(ABORT, 'candidate_members is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_candidate_members_no_delete
BEFORE DELETE ON candidate_members
BEGIN SELECT RAISE(ABORT, 'candidate_members is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_candidate_rejects_no_update
BEFORE UPDATE ON candidate_rejects
BEGIN SELECT RAISE(ABORT, 'candidate_rejects is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_candidate_rejects_no_delete
BEFORE DELETE ON candidate_rejects
BEGIN SELECT RAISE(ABORT, 'candidate_rejects is append-only'); END;

-- ---------- 财报（本轮的采集层）----------
-- 单位一律：元。PIT 锚点是 `notice_date`（公告日），**不是** `report_date`（报告期）。
--
-- `notice_date_source` 区分实测与推定：
--   'f10'       —— 取自 F10 报表的 NOTICE_DATE（实测与真实公告日吻合）
--   'statutory' —— F10 取不到或合理性检查不过，回退法定披露截止日（保守，最多晚约一个月）
--
-- 为什么主键带 `notice_date`：财报被更正/重述时是一条新公告，同一报告期可有多个版本，
-- 读取侧取「notice_date <= asof 中最晚的那个」。主键不含它就只能靠覆盖，而覆盖是禁区。
--
-- `total_equity` 是**含少数股东权益的所有者权益合计**（实测 == 总资产 − 总负债），
-- **不是归母**。归母权益在 `parent_equity`。两者混用会让 roe 系统性低估且 dupont 恒等式不成立。
--
-- `raw_refs_json` 是溯源数组：一行来自多个端点（DMSK 三表 + F10 三变体），单列装不下。
CREATE TABLE IF NOT EXISTS financial_reports (
    code                 TEXT NOT NULL,
    report_date          TEXT NOT NULL,
    notice_date          TEXT NOT NULL,
    notice_date_source   TEXT NOT NULL
                         CHECK (notice_date_source IN ('f10', 'statutory')),
    report_type          TEXT NOT NULL
                         CHECK (report_type IN ('一季报', '中报', '三季报', '年报')),
    total_assets         REAL,
    parent_equity        REAL,      -- 归母股东权益
    total_equity         REAL,      -- 所有者权益合计（含少数股东权益）
    total_liabilities    REAL,
    inventory            REAL,
    total_operate_income REAL,      -- 年内累计
    operate_cost         REAL,      -- 年内累计
    parent_netprofit     REAL,      -- 年内累计
    netcash_operate      REAL,      -- 年内累计
    construct_long_asset REAL,      -- 年内累计
    industry_name        TEXT,      -- 东财 INDUSTRY_NAME（**非 PIT**）
    source               TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    raw_refs_json        TEXT NOT NULL,
    cache_key            TEXT,
    unit                 TEXT NOT NULL DEFAULT 'CNY',
    PRIMARY KEY (code, report_date, notice_date)
);

CREATE INDEX IF NOT EXISTS idx_financial_reports_code_notice
    ON financial_reports (code, notice_date);

CREATE TRIGGER IF NOT EXISTS trg_financial_reports_no_update
BEFORE UPDATE ON financial_reports
BEGIN SELECT RAISE(ABORT, 'financial_reports is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_financial_reports_no_delete
BEFORE DELETE ON financial_reports
BEGIN SELECT RAISE(ABORT, 'financial_reports is append-only'); END;

-- ---------- 模块2 验证周期台账（P44，append-only）----------
-- 口径：D-26（每策略版本一个隔离账户）/ D-27（熔断）/ D-28（自评估边界）。
-- 三张表 + `plugin_audit` 共同回答：「这一轮用的是**哪个策略版本**、
-- **哪组参数**、**判据原文**是什么」（任务书 T3）。
-- 状态变化一律靠**追加事件**，不许 UPDATE 任何一列（触发器 RAISE(ABORT)）。
CREATE TABLE IF NOT EXISTS validation_cycles (
    cycle_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id      INTEGER NOT NULL,          -- 策略版本（plugin_scripts.script_id）
    account_id     TEXT NOT NULL,             -- 该版本对应的隔离模拟账户（D-26）
    planned_rounds INTEGER NOT NULL,          -- AI 给的轮次（原值落库，越界在写入前已被拒）
    planned_days   INTEGER NOT NULL,          -- 单轮天数（同上）
    params_json    TEXT NOT NULL,             -- 该版本**生效的参数集**（口径可追溯）
    criteria_text  TEXT NOT NULL,             -- 判据**原文**（D-31：不许事后换口径）
    start_date     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_rounds (
    round_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id     INTEGER NOT NULL,
    round_no     INTEGER NOT NULL CHECK (round_no >= 1),
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    metrics_json TEXT NOT NULL,               -- 该轮观测指标（口径同 P41 指标集）
    note         TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (cycle_id, round_no)               -- 同一周期内轮序唯一（补跑不许静默覆盖）
);

CREATE TABLE IF NOT EXISTS validation_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id      INTEGER NOT NULL,
    script_id     INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('circuit_breaker','freeze','unfreeze','validation_end')),
    at_value      REAL,                       -- 触发时的**实测数值**（如回撤 -0.137）
    threshold     REAL,                       -- 判据阈值（如 0.10）——与 at_value 分开存
    criteria_text TEXT NOT NULL,              -- 判据原文（同上，不许事后改写）
    reason        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_validation_cycles_no_update
BEFORE UPDATE ON validation_cycles
BEGIN SELECT RAISE(ABORT, 'validation_cycles is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_validation_cycles_no_delete
BEFORE DELETE ON validation_cycles
BEGIN SELECT RAISE(ABORT, 'validation_cycles is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_validation_rounds_no_update
BEFORE UPDATE ON validation_rounds
BEGIN SELECT RAISE(ABORT, 'validation_rounds is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_validation_rounds_no_delete
BEFORE DELETE ON validation_rounds
BEGIN SELECT RAISE(ABORT, 'validation_rounds is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_validation_events_no_update
BEFORE UPDATE ON validation_events
BEGIN SELECT RAISE(ABORT, 'validation_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_validation_events_no_delete
BEFORE DELETE ON validation_events
BEGIN SELECT RAISE(ABORT, 'validation_events is append-only'); END;
