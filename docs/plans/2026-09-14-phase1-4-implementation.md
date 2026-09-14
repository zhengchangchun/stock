# stock-lab P1–P4 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 stock-lab 的地基四层 —— 骨架/schema、数据层、特征层、回测引擎，每层可独立验证，为 P5 之后的策略/预测/验证/优化提供可信输入。

**Architecture:** 单向依赖分层：`config → store → calendar → data → quality → features → backtest`。所有落库经 `store/`，禁止模块直接写 SQLite（用代码审查 + 测试双重约束）。数据采集走「适配器纯函数（raw → 领域对象）」+「原始响应缓存」，保证离线可测与可复现。回测引擎用事件驱动 + 次日开盘成交，成本模型内建，模拟盘（P9）复用同一份持仓推进代码。

**Tech Stack:** Python 3.12、SQLite（stdlib `sqlite3`）、`requests`、`pandas`/`numpy`（仅用于特征与指标计算，不用于存储层）、`pytest`。配置用 TOML（stdlib `tomllib`），不引入额外配置依赖。

**Spec:** `docs/plans/2026-09-14-系统设计总纲.md`（需求基线）
**Review:** `docs/plans/2026-09-14-P0-设计评审.md`（本计划的取舍依据）

---

## Global Constraints

以下约束适用于**每一个** Task，不再逐条重复：

- **Python 解释器**：虚拟环境必须用 `/opt/nanobot-venv/bin/python -m venv .venv` 创建（系统 `python3` 是 3.14 且 `sys.executable` 为空，`python3 -m venv` 不可用）。venv 内为 Python 3.12.14。
- **依赖**：仅 `pandas numpy pytest requests`。**不得新增任何依赖**（含 `pydantic`、`sqlalchemy`、`akshare`、`ta-lib`）；需要的行为自己写。
- **网络白名单**：仅允许 `qt.gtimg.cn`、`web.ifzq.gtimg.cn`、`push2.eastmoney.com`、`push2his.eastmoney.com`。**禁止** IP 直连、数字子域（如 `1.push2.eastmoney.com`）、代理、内网地址。
- **单位固定**：价格 = 元（REAL）；成交量 = **股**（INTEGER，接口给的「手」必须 ×100）；成交额 = 元（REAL，接口给的「万」必须 ×10000）。转换只在适配器出口发生。
- **时间**：所有时间戳 ISO8601 **带时区**（`Asia/Shanghai`）。日期字段用 `YYYY-MM-DD` 字符串。
- **迁移**：**只前滚（forward-only）**，不做 down migration。回滚 = 迁移前自动备份 DB 文件。
- **append-only**：`features_daily`、`predictions`、`verifications`、`sim_trades`、`decisions` 禁止 UPDATE/DELETE，由 SQLite 触发器强制。
- **失败留痕**：任何抓取/计算失败必须写入 `data_quality` 或 `system_events`，**禁止静默 except**。
- **常量集中**：所有可调参数（阈值、费率、窗口）集中在 `stocklab/config/`，不得散落在业务代码里。
- **提交**：每个 Task 结束提交一次，提交信息用 `feat(p1): ...` / `test(p1): ...` 前缀。

### 开工前必须决策的三个阻塞项（来自评审 B1/B2/A2）

| 编号 | 决策 | 本计划的默认假设 | 若决策不同需改哪里 |
|---|---|---|---|
| **B1** | 复权方案 | 采用**方案 2**：落「不复权 OHLC」+ 每日 `adj_factor` 快照，历史因子链靠日积月累，缺口标记不可回测 | 若选方案 1/3，改 Task 11/12/14 与 `bars_daily.adj_mode` 取值 |
| **B2** | 长历史数据源 | **Task 12 的第一件事就是实测东财 `beg`/`end` 能回溯多久**，结论写入 `docs/decisions/` | 若东财也给不了长历史，Task 26 的 walk-forward 验收标准必须下调 |
| **A2** | 实盘账本结构 | v1 只建 `real_trades` **成交流水**，不建 `real_portfolio` 快照 | 改 Task 4 的 DDL |

> **执行者注意**：若这三个决策在上层未确认，**不要自行假设后一路做下去** —— 先把 Task 12 的实测结果和本表交给人类决策，再继续。

---

## 文件结构总览

```
stock-lab/
├── pyproject.toml                    # 项目元数据 + pytest 配置
├── pytest.ini                        # （二选一，本计划用 pyproject.toml）
├── stocklab/
│   ├── __init__.py
│   ├── config/
│   │   ├── __init__.py
│   │   ├── paths.py                  # 所有路径常量（唯一真源）
│   │   ├── settings.py               # Settings dataclass + TOML 加载
│   │   ├── costs.py                  # CostModel（费用 + 滑点）
│   │   └── universe.py               # 标的池定义 + ALLOWED_HOSTS
│   ├── store/
│   │   ├── __init__.py
│   │   ├── db.py                     # 连接、PRAGMA、事务上下文
│   │   ├── schema.sql                # 全量 DDL（幂等 CREATE IF NOT EXISTS）
│   │   ├── migrate.py                # 前滚迁移 + 备份 + 幂等校验
│   │   └── repo.py                   # 各表读写（唯一写入口）
│   ├── calendar/
│   │   ├── __init__.py
│   │   └── trading_calendar.py       # 由指数 bars 构建交易日历
│   ├── data/
│   │   ├── __init__.py
│   │   ├── errors.py                 # FetchError / HostNotAllowed / RateLimited
│   │   ├── http.py                   # 白名单 + 退避重试 + 缓存 + GBK
│   │   ├── raw_cache.py              # 原始响应落盘 + sha256
│   │   ├── models.py                 # Bar / Quote / MoneyFlow（领域对象）
│   │   ├── sources/
│   │   │   ├── __init__.py
│   │   │   ├── tencent.py            # 快照 + 日K 解析（纯函数）
│   │   │   └── eastmoney.py          # 日K + 资金流解析（纯函数）
│   │   └── ingest.py                 # 采集编排 → 校验 → 落库
│   ├── quality/
│   │   ├── __init__.py
│   │   └── checks.py                 # 纯函数校验 + 结果对象
│   ├── features/
│   │   ├── __init__.py
│   │   ├── indicators.py             # MA/ATR/RSI/量比（纯函数）
│   │   ├── registry.py               # 特征定义与版本
│   │   └── snapshot.py               # 快照生成 + canonical hash + 落库
│   ├── backtest/
│   │   ├── __init__.py
│   │   ├── portfolio.py              # 持仓/现金推进（模拟盘复用）
│   │   ├── engine.py                 # 事件驱动主循环
│   │   ├── metrics.py                # 收益/回撤/超额/胜率
│   │   └── walkforward.py            # walk-forward 切分器
│   └── cli/
│       ├── __init__.py
│       └── main.py                   # 命令行入口
├── tests/
│   ├── conftest.py
│   ├── fixtures/                     # 录制的真实响应（录一次跑无数次）
│   └── test_*.py
└── data/                             # .gitignore
```

---

# P1：骨架 + 配置 + Schema + 迁移 + 交易日历

**P1 验收（DoD）**：`pytest -q` 全绿；`stocklab db init` 能在空目录建库；迁移重跑幂等；append-only 触发器有测试证明 UPDATE/DELETE 会抛错；交易日历能从指数 bars 构建并正确跳过周末。

---

### Task 1: 项目骨架与运行环境

**Files:**
- Create: `pyproject.toml`, `stocklab/__init__.py`, `tests/__init__.py`, `tests/conftest.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: 可 import 的 `stocklab` 包；`pytest` 可发现 `tests/`。

- [ ] **Step 1: 创建虚拟环境（环境事实，不可用系统 python3）**

```bash
cd /root/.nanobot/workspace/projects/stock-lab
/opt/nanobot-venv/bin/python -m venv .venv
.venv/bin/python -V
```

Expected: `Python 3.12.14`

- [ ] **Step 2: 安装依赖**

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install pandas numpy pytest requests
.venv/bin/python -c "import pandas, numpy, pytest, requests; print('ok', pandas.__version__, numpy.__version__, pytest.__version__, requests.__version__)"
```

Expected: `ok 2.x.x 2.x.x 8.x.x 2.x.x`（版本号以实际为准，关键是打印出 `ok`）

- [ ] **Step 3: 写 `pyproject.toml`**

```toml
[project]
name = "stocklab"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
filterwarnings = ["error::DeprecationWarning"]
```

> `filterwarnings = error::DeprecationWarning` 是为了提前暴露 pandas 弃用 API，避免半年后集体返工。

- [ ] **Step 4: 写 `tests/conftest.py`（公共 fixture 骨架，后续 Task 会补）**

```python
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_db(tmp_path):
    """独立的临时 SQLite 数据库路径。"""
    return tmp_path / "test.db"
```

- [ ] **Step 5: 补 `.gitignore`**

在 `# Environment` 段落后追加：

```gitignore
# Python venv
.venv/
venv/

# 项目数据（SQLite / 缓存 / 备份）
data/
reports/
```

- [ ] **Step 6: 验证**

```bash
.venv/bin/python -m pytest -q
```

Expected: `no tests ran in 0.0Xs`（退出码 5 也算通过 —— 此刻还没有测试）。**注意**：`pytest` 对「没有收集到测试」返回退出码 5，这是预期的。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml .gitignore stocklab/__init__.py tests/
git commit -m "feat(p1): 项目骨架、venv 与 pytest 配置"
```

---

### Task 2: 路径与配置模块

**Files:**
- Create: `stocklab/config/__init__.py`, `stocklab/config/paths.py`, `stocklab/config/universe.py`, `stocklab/config/settings.py`
- Test: `tests/test_config_paths.py`, `tests/test_config_universe.py`

**Interfaces:**
- Produces:
  - `stocklab.config.paths`：`PROJECT_ROOT: Path`、`DATA_DIR`、`DB_PATH`、`BACKUP_DIR`、`RAW_CACHE_DIR`、`REPORT_DIR`、`FIXTURE_DIR`
  - `stocklab.config.universe`：`ALLOWED_HOSTS: frozenset[str]`、`assert_host_allowed(url: str) -> None`、`Instrument(code, name, market, board)`、`DEFAULT_UNIVERSE: tuple[Instrument, ...]`
  - `stocklab.config.settings`：`Settings` dataclass、`load_settings(path: Path | None = None) -> Settings`

- [ ] **Step 1: 写失败测试 `tests/test_config_universe.py`**

```python
import pytest

from stocklab.config.universe import ALLOWED_HOSTS, assert_host_allowed
from stocklab.data.errors import HostNotAllowed


def test_allowed_host_passes():
    assert_host_allowed("https://qt.gtimg.cn/q=sz000333")


def test_numeric_subdomain_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("https://1.push2.eastmoney.com/api/qt/clist/get")


def test_ip_literal_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("http://192.168.1.10/data")


def test_unknown_host_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("https://example.com/x")


def test_whitelist_is_exact():
    assert ALLOWED_HOSTS == frozenset(
        {
            "qt.gtimg.cn",
            "web.ifzq.gtimg.cn",
            "push2.eastmoney.com",
            "push2his.eastmoney.com",
        }
    )
```

> 这个测试同时是 R13 的**可执行形式**：白名单一旦被放松，测试立刻红。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_config_universe.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.config.universe'`

- [ ] **Step 3: 写 `stocklab/data/errors.py`（先建异常，它是被依赖方）**

```python
class StocklabError(Exception):
    """项目所有异常的基类。"""


class FetchError(StocklabError):
    """抓取失败（网络、超时、非 200）。"""


class HostNotAllowed(FetchError):
    """目标域名不在白名单内（含 IP 直连 / 数字子域）。"""


class RateLimited(FetchError):
    """被数据源限流（空响应 / 429 / 频繁失败）。"""


class DataQualityError(StocklabError):
    """数据未通过质量校验。"""
```

- [ ] **Step 4: 写 `stocklab/config/paths.py`**

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "stocklab.db"
BACKUP_DIR = DATA_DIR / "backups"
RAW_CACHE_DIR = DATA_DIR / "raw_cache"
REPORT_DIR = PROJECT_ROOT / "reports"
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
CONFIG_PATH = PROJECT_ROOT / "config.toml"

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "store" / "schema.sql"


def ensure_dirs() -> None:
    """创建所有运行时目录（幂等）。"""
    for d in (DATA_DIR, BACKUP_DIR, RAW_CACHE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 5: 写 `stocklab/config/universe.py`**

```python
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse

from stocklab.data.errors import HostNotAllowed

ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "qt.gtimg.cn",
        "web.ifzq.gtimg.cn",
        "push2.eastmoney.com",
        "push2his.eastmoney.com",
    }
)


def assert_host_allowed(url: str) -> None:
    """校验 URL 主机名在白名单内，且不是 IP 直连（R13）。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise HostNotAllowed(f"无法解析主机名: {url!r}")
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise HostNotAllowed(f"禁止 IP 直连: {host}")
    if host not in ALLOWED_HOSTS:
        raise HostNotAllowed(f"域名不在白名单: {host}")


@dataclass(frozen=True)
class Instrument:
    code: str          # 6 位代码，如 "000333"
    name: str
    market: str        # "sz" | "sh"
    board: str         # "main" | "gem" | "star" | "bse" —— 决定涨跌停幅度

    @property
    def secid(self) -> str:
        """东财 secid：1.=沪 0.=深。"""
        return f"{'1' if self.market == 'sh' else '0'}.{self.code}"

    @property
    def tencent_code(self) -> str:
        return f"{self.market}{self.code}"


DEFAULT_UNIVERSE: tuple[Instrument, ...] = (
    Instrument("000333", "美的集团", "sz", "main"),
    Instrument("600690", "海尔智家", "sh", "main"),
)
```

- [ ] **Step 6: 写 `stocklab/config/settings.py`**

```python
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from stocklab.config import paths
from stocklab.config.costs import CostModel


@dataclass(frozen=True)
class Settings:
    timezone: str = "Asia/Shanghai"
    backfill_days: int = 1200          # 日K 回补目标长度
    http_timeout: float = 10.0
    http_min_interval: float = 0.35    # 同域请求最小间隔（防限流）
    retry_attempts: int = 4
    retry_base_delay: float = 0.8
    retry_max_delay: float = 8.0
    cache_enabled: bool = True         # 命中 raw_cache 则不联网
    costs: CostModel = field(default_factory=CostModel)


def load_settings(path: Path | None = None) -> Settings:
    """从 TOML 加载配置；文件不存在时返回默认值。"""
    p = path or paths.CONFIG_PATH
    if not p.exists():
        return Settings()
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    flat = {k: v for k, v in raw.items() if not isinstance(v, dict)}
    costs_raw = raw.get("costs", {})
    costs = CostModel(**costs_raw) if costs_raw else CostModel()
    return Settings(**flat, costs=costs)
```

- [ ] **Step 7: 写 `stocklab/config/__init__.py`**

```python
from stocklab.config.paths import PROJECT_ROOT, DB_PATH, DATA_DIR
from stocklab.config.settings import Settings, load_settings
from stocklab.config.universe import ALLOWED_HOSTS, Instrument, assert_host_allowed

__all__ = [
    "PROJECT_ROOT",
    "DB_PATH",
    "DATA_DIR",
    "Settings",
    "load_settings",
    "ALLOWED_HOSTS",
    "Instrument",
    "assert_host_allowed",
]
```

- [ ] **Step 8: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_config_universe.py -q`
Expected: `5 passed`

- [ ] **Step 9: 提交**

```bash
git add stocklab/config/ stocklab/data/errors.py stocklab/data/__init__.py tests/test_config_universe.py
git commit -m "feat(p1): 配置模块与网络白名单（R13 可执行化）"
```

---

### Task 3: 成本模型

**Files:**
- Create: `stocklab/config/costs.py`
- Test: `tests/test_config_costs.py`

**Interfaces:**
- Produces: `Side = Literal["buy", "sell"]`；`CostModel`（frozen dataclass）字段 `commission_rate=0.00025`、`min_commission=5.0`、`stamp_tax_rate=0.0005`、`transfer_fee_rate=0.00001`、`slippage_bps=5.0`；方法 `fill_price(side, ref_price) -> float`、`fees(side, price, qty) -> float`、`total(side, ref_price, qty) -> tuple[float, float]`（返回 `(成交价, 费用)`）
- Consumed by: P4 回测引擎、P9 模拟盘

- [ ] **Step 1: 写失败测试 `tests/test_config_costs.py`**

```python
import pytest

from stocklab.config.costs import CostModel


@pytest.fixture
def cm():
    return CostModel()


def test_buy_fill_price_slips_up(cm):
    assert cm.fill_price("buy", 10.0) == pytest.approx(10.005)


def test_sell_fill_price_slips_down(cm):
    assert cm.fill_price("sell", 10.0) == pytest.approx(9.995)


def test_buy_fees_exclude_stamp_tax(cm):
    # 10.00 × 1000 = 10000 元
    # 佣金 max(10000*0.00025, 5) = max(2.5, 5) = 5.0
    # 过户费 10000*0.00001 = 0.1
    # 印花税 买入不收 = 0
    assert cm.fees("buy", 10.0, 1000) == pytest.approx(5.1)


def test_sell_fees_include_stamp_tax(cm):
    # 佣金 5.0 + 过户费 0.1 + 印花税 10000*0.0005 = 5.0
    assert cm.fees("sell", 10.0, 1000) == pytest.approx(10.1)


def test_min_commission_kicks_in_on_small_trade(cm):
    """小额成交必须按最低 5 元计（评审 C2）。"""
    # 1000 元成交额：佣金按比例是 0.25 元，但最低 5 元
    assert cm.fees("buy", 10.0, 100) == pytest.approx(5.0 + 0.01)


def test_large_trade_commission_is_proportional(cm):
    # 1_000_000 元：佣金 250 元（超过最低）
    assert cm.fees("buy", 100.0, 10000) == pytest.approx(250.0 + 10.0)


def test_small_trade_costs_more_than_proportional(cm):
    """核心性质：小额交易的实际费率远高于名义费率。"""
    small = cm.fees("buy", 10.0, 100) / 1000
    large = cm.fees("buy", 100.0, 10000) / 1_000_000
    assert small > large * 100


def test_costs_are_rounded_to_cent(cm):
    fee = cm.fees("buy", 3.333, 333)
    assert round(fee, 2) == fee


def test_total_returns_fill_and_fee(cm):
    price, fee = cm.total("buy", 10.0, 1000)
    assert price == pytest.approx(10.005)
    assert fee == pytest.approx(cm.fees("buy", price, 1000))


def test_cost_model_is_frozen(cm):
    with pytest.raises(Exception):
        cm.commission_rate = 0.1
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_config_costs.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.config.costs'`

- [ ] **Step 3: 写实现**

```python
from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class CostModel:
    """A 股交易成本模型（R4）。所有费率均为小数，滑点为基点。"""

    commission_rate: float = 0.00025    # 佣金 0.025%，双边
    min_commission: float = 5.0         # 单笔最低佣金（元）
    stamp_tax_rate: float = 0.0005      # 印花税 0.05%，仅卖出
    transfer_fee_rate: float = 0.00001  # 过户费 0.001%，双边
    slippage_bps: float = 5.0           # 滑点 5 个基点，买入上滑 / 卖出下滑

    def fill_price(self, side: Side, ref_price: float) -> float:
        sign = 1.0 if side == "buy" else -1.0
        return ref_price * (1.0 + sign * self.slippage_bps / 10_000.0)

    def fees(self, side: Side, price: float, qty: int) -> float:
        amount = price * qty
        commission = max(amount * self.commission_rate, self.min_commission)
        transfer = amount * self.transfer_fee_rate
        stamp = amount * self.stamp_tax_rate if side == "sell" else 0.0
        return round(commission + transfer + stamp, 2)

    def total(self, side: Side, ref_price: float, qty: int) -> tuple[float, float]:
        """返回 (实际成交价, 费用)。"""
        price = self.fill_price(side, ref_price)
        return price, self.fees(side, price, qty)
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_config_costs.py -q`
Expected: `10 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/config/costs.py tests/test_config_costs.py
git commit -m "feat(p1): 成本模型（含最低佣金 5 元与滑点）"
```

---

### Task 4: Schema DDL

**Files:**
- Create: `stocklab/store/__init__.py`, `stocklab/store/schema.sql`
- Test: `tests/test_store_schema.py`

**Interfaces:**
- Produces: `stocklab/store/schema.sql` —— 全量幂等 DDL，含 17 张表与 append-only 触发器。被 Task 5/6 消费。

**评审修正已落入本 DDL**：`features_daily` 用自增 `snapshot_id` + `UNIQUE(code,date,feature_version)`（A1）；`real_portfolio` 删除、只留 `real_trades` 流水（A2）；新增 `trading_calendar`（B4）、`raw_fetch_cache`（B3）、`job_runs`（C7）；`verifications` 拆分 `attribution_auto`/`attribution_manual`（A5）；`bars_daily` 增加 `adj_mode`/`is_suspended`（B1）；`data_quality` 增加 `severity`/`first_seen`/`last_seen`（去重）。

- [ ] **Step 1: 写失败测试 `tests/test_store_schema.py`**

```python
import sqlite3

import pytest

from stocklab.config.paths import SCHEMA_SQL

EXPECTED_TABLES = {
    "instruments", "bars_daily", "adj_factors", "money_flow_daily",
    "valuation_daily", "sector_daily", "market_state", "trading_calendar",
    "features_daily", "predictions", "verifications",
    "strategy_registry", "strategy_daily", "sim_portfolio", "sim_trades",
    "real_trades", "decisions", "data_quality", "system_events",
    "raw_fetch_cache", "job_runs",
}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    yield c
    c.close()


def test_schema_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert {r[0] for r in rows} == EXPECTED_TABLES


def test_schema_is_idempotent(conn):
    """重跑 DDL 不得报错（迁移幂等的前提）。"""
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))


def test_features_daily_allows_multiple_versions(conn):
    """A1：append-only 与'重算追加'必须共存。"""
    sql = ("INSERT INTO features_daily (code, date, feature_version, json_payload, payload_hash) "
           "VALUES (?,?,?,?,?)")
    conn.execute(sql, ("000333", "2026-09-14", "v1", "{}", "h1"))
    conn.execute(sql, ("000333", "2026-09-14", "v2", "{}", "h2"))
    n = conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0]
    assert n == 2


def test_features_daily_rejects_duplicate_version(conn):
    sql = ("INSERT INTO features_daily (code, date, feature_version, json_payload, payload_hash) "
           "VALUES (?,?,?,?,?)")
    conn.execute(sql, ("000333", "2026-09-14", "v1", "{}", "h1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("000333", "2026-09-14", "v1", "{}", "h1"))


def test_bars_daily_primary_key(conn):
    sql = ("INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode) "
           "VALUES (?,?,?,?,?,?,?,?)")
    conn.execute(sql, ("000333", "2026-09-14", 1, 2, 0.5, 1.5, 100, "none"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("000333", "2026-09-14", 1, 2, 0.5, 1.5, 100, "none"))


def test_real_portfolio_is_gone(conn):
    """A2：实盘只留流水，不留每日快照表。"""
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT 1 FROM real_portfolio")


def test_trading_calendar_unique(conn):
    sql = "INSERT INTO trading_calendar (date, source) VALUES (?, ?)"
    conn.execute(sql, ("2026-09-14", "index_bars"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("2026-09-14", "index_bars"))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_store_schema.py -q`
Expected: FAIL —— `FileNotFoundError` / `No such file`（`schema.sql` 还不存在）

- [ ] **Step 3: 写 `stocklab/store/schema.sql`**

```sql
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

CREATE TABLE IF NOT EXISTS adj_factors (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    factor      REAL NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (code, date)
);

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

-- ---------- append-only 触发器（R8） ----------
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

CREATE TRIGGER IF NOT EXISTS trg_decisions_no_delete
BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
```

> `verifications` **不设** append-only 触发器：`attribution_manual` 需要人工回填。这一点与总纲第 6 节略有出入，是刻意的 —— 见 Task 6 的说明。

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_store_schema.py -q`
Expected: `7 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/store/__init__.py stocklab/store/schema.sql tests/test_store_schema.py
git commit -m "feat(p1): SQLite schema（含 append-only 触发器与评审修正）"
```

---

### Task 5: 数据库连接层与迁移

**Files:**
- Create: `stocklab/store/db.py`, `stocklab/store/migrate.py`
- Test: `tests/test_store_migrate.py`

**Interfaces:**
- Produces:
  - `connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection`（设 `PRAGMA foreign_keys=ON`、`journal_mode=WAL`、`Row` 工厂）
  - `transaction(conn) -> ContextManager`（出错回滚）
  - `init_db(db_path: Path) -> Path`（建库 + 应用 schema，返回备份路径或 None）
  - `backup_db(db_path: Path, backup_dir: Path, tag: str) -> Path`

- [ ] **Step 1: 写失败测试 `tests/test_store_migrate.py`**

```python
import sqlite3

import pytest

from stocklab.store.db import connect, transaction
from stocklab.store.migrate import backup_db, init_db


def test_init_creates_db(tmp_db):
    assert not tmp_db.exists()
    init_db(tmp_db)
    assert tmp_db.exists()
    with connect(tmp_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='bars_daily'"
        ).fetchone()[0]
    assert n == 1


def test_init_is_idempotent(tmp_db):
    init_db(tmp_db)
    init_db(tmp_db)   # 第二次不得报错
    with connect(tmp_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0]
    assert n == 0


def test_init_does_not_backup_on_first_run(tmp_db, tmp_path):
    result = init_db(tmp_db)
    assert result is None


def test_init_backs_up_existing_db(tmp_db):
    init_db(tmp_db)
    backup = init_db(tmp_db)
    assert backup is not None
    assert backup.exists()


def test_foreign_keys_enabled(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_wal_mode(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_transaction_rolls_back_on_error(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute(
                    "INSERT INTO trading_calendar (date, source, created_at) VALUES (?,?,?)",
                    ("2026-09-14", "test", "t"),
                )
                raise RuntimeError("boom")
        n = conn.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]
    assert n == 0


def test_backup_creates_copy(tmp_db, tmp_path):
    init_db(tmp_db)
    bdir = tmp_path / "backups"
    out = backup_db(tmp_db, bdir, "test")
    assert out.exists()
    assert out.parent == bdir
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_store_migrate.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.store.db'`

- [ ] **Step 3: 写 `stocklab/store/db.py`**

```python
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """打开连接并设置所有必需 PRAGMA。read_only 不影响文件创建。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务；异常回滚并重抛。"""
    try:
        conn.execute("BEGIN")
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
```

> 注意：`connect()` 返回的连接**不自动提交**。所有写入必须包在 `transaction()` 里，否则会静默丢失。

- [ ] **Step 4: 写 `stocklab/store/migrate.py`**

```python
import shutil
from datetime import datetime, timezone
from pathlib import Path

from stocklab.config.paths import SCHEMA_SQL
from stocklab.store.db import connect

TZ = timezone.utc


def _now_tag() -> str:
    return datetime.now(TZ).strftime("%Y%m%dT%H%M%SZ")


def backup_db(db_path: Path, backup_dir: Path, tag: str) -> Path:
    """把现有 DB 复制到 backup_dir。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{db_path.stem}.{tag}.{_now_tag()}.db"
    shutil.copy2(db_path, dest)
    return dest


def init_db(db_path: Path, *, backup_dir: Path | None = None,
            schema_path: Path | None = None) -> Path | None:
    """建库 / 应用 schema。

    前滚迁移策略（评审 D4）：不做 down migration，
    若 DB 已存在则先备份，再幂等应用 schema。
    返回备份文件路径；首次创建返回 None。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if db_path.exists():
        bdir = backup_dir or (db_path.parent / "backups")
        backup = backup_db(db_path, bdir, "premigrate")

    sql = (schema_path or SCHEMA_SQL).read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
    return backup
```

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_store_migrate.py -q`
Expected: `8 passed`

- [ ] **Step 6: 提交**

```bash
git add stocklab/store/db.py stocklab/store/migrate.py tests/test_store_migrate.py
git commit -m "feat(p1): 数据库连接层与前滚迁移（含迁移前自动备份）"
```

---

### Task 6: append-only 约束的可执行验证

**Files:**
- Create: `tests/test_store_append_only.py`
- Modify: `docs/errors/ERROR_DIARY.md`（若本任务暴露新教训）

**Interfaces:**
- Consumes: `init_db`（Task 5）、schema 触发器（Task 4）
- Produces: 对 R8 的可执行证明

- [ ] **Step 1: 写测试 `tests/test_store_append_only.py`**

```python
import sqlite3

import pytest

from stocklab.store.db import connect, transaction
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _seed_feature(conn):
    conn.execute(
        "INSERT INTO features_daily (code, date, feature_version, json_payload,"
        " payload_hash, params_hash, data_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("000333", "2026-09-14", "v1", "{}", "h", "p", "d1", NOW),
    )


def test_features_update_rejected(conn):
    with transaction(conn):
        _seed_feature(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE features_daily SET close = 1.0")


def test_features_delete_rejected(conn):
    with transaction(conn):
        _seed_feature(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM features_daily")


def _seed_prediction(conn):
    conn.execute(
        "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
        " direction_flat, direction_down, action, size_pct, invalidate_if,"
        " strategy_mix_json, model_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("000333", "2026-09-14", "2026-09-15", 0.4, 0.3, 0.3, "hold", 50,
         "跌破 85.00", "{}", "v0.1.0", NOW),
    )


def test_predictions_update_rejected(conn):
    """R8 的核心：事后改预测 = 自我欺骗。"""
    with transaction(conn):
        _seed_prediction(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE predictions SET direction_up = 0.99")


def test_predictions_delete_rejected(conn):
    with transaction(conn):
        _seed_prediction(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM predictions")


def test_insert_still_works_after_failed_update(conn):
    """触发器必须是 ABORT 而非静默忽略。"""
    with transaction(conn):
        _seed_prediction(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE predictions SET size_pct = 0")
    with transaction(conn):
        _seed_prediction(conn)
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2


def test_verifications_are_updatable_for_manual_attribution(conn):
    """刻意例外：attribution_manual 需要人工回填，故不加 append-only 触发器。"""
    conn.execute(
        "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
        " direction_flat, direction_down, action, size_pct, invalidate_if,"
        " strategy_mix_json, model_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("000333", "2026-09-14", "2026-09-15", 0.4, 0.3, 0.3, "hold", 50,
         "x", "{}", "v0.1.0", NOW),
    )
    with transaction(conn):
        conn.execute(
            "INSERT INTO verifications (pred_id, target_date, created_at) VALUES (1,?,?)",
            ("2026-09-15", NOW),
        )
        conn.execute("UPDATE verifications SET attribution_manual = 'STRATEGY'")
    row = conn.execute("SELECT attribution_manual FROM verifications").fetchone()
    assert row["attribution_manual"] == "STRATEGY"
```

- [ ] **Step 2: 运行**

Run: `.venv/bin/python -m pytest tests/test_store_append_only.py -q`
Expected: `7 passed`

- [ ] **Step 3: 记录与总纲的刻意偏离**

在 `docs/decisions/` 新建 `ADR-001-verifications-允许更新.md`，内容为：总纲第 6 节要求 `verifications` append-only，但归因评审（A5）要求人工标注 `attribution_manual`，二者冲突；决定对 `verifications` 只加 DELETE 保护、允许 UPDATE 指定列，代价是失去 append-only 保证，补偿手段是 `created_at` 不变 + 在报告中显示标注时间。

- [ ] **Step 4: 提交**

```bash
git add tests/test_store_append_only.py docs/decisions/
git commit -m "test(p1): append-only 约束的可执行验证 + ADR-001"
```

---

### Task 7: 交易日历

**Files:**
- Create: `stocklab/calendar/__init__.py`, `stocklab/calendar/trading_calendar.py`
- Test: `tests/test_calendar.py`

**Interfaces:**
- Produces:
  - `Calendar` 类：`Calendar.from_dates(dates: Iterable[str]) -> Calendar`、`.is_open(d: str) -> bool`、`.next_trading_day(d: str) -> str`、`.prev_trading_day(d: str) -> str`、`.sessions(start: str, end: str) -> list[str]`、`.all_dates -> tuple[str, ...]`
  - `save_calendar(conn, dates: Iterable[str], source: str, now: str) -> int`
  - `load_calendar(conn) -> Calendar`

> 设计依据（评审 B4）：日历来源是**已采集的指数日线日期集合**，不手写节假日表，零额外数据源。

- [ ] **Step 1: 写失败测试 `tests/test_calendar.py`**

```python
import pytest

from stocklab.calendar.trading_calendar import Calendar
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

# 2026-09-11(五) 14(一) 15(二)；12/13 是周末
DATES = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]


@pytest.fixture
def cal():
    return Calendar.from_dates(DATES)


def test_is_open(cal):
    assert cal.is_open("2026-09-11")
    assert not cal.is_open("2026-09-12")   # 周六
    assert not cal.is_open("2026-09-13")   # 周日


def test_next_trading_day_skips_weekend(cal):
    """R11 的经典场景：周五的次日是下周一。"""
    assert cal.next_trading_day("2026-09-11") == "2026-09-14"


def test_next_trading_day_from_saturday(cal):
    assert cal.next_trading_day("2026-09-12") == "2026-09-14"


def test_prev_trading_day(cal):
    assert cal.prev_trading_day("2026-09-14") == "2026-09-11"


def test_next_beyond_range_raises(cal):
    with pytest.raises(IndexError):
        cal.next_trading_day("2026-09-15")


def test_sessions_inclusive(cal):
    assert cal.sessions("2026-09-10", "2026-09-14") == [
        "2026-09-10", "2026-09-11", "2026-09-14",
    ]


def test_from_dates_sorts_and_dedupes():
    cal = Calendar.from_dates(["2026-09-14", "2026-09-10", "2026-09-14"])
    assert cal.all_dates == ("2026-09-10", "2026-09-14")


def test_roundtrip_through_db(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        cal = Calendar.from_dates(DATES)
        n = cal.save(conn, source="index_bars", now="2026-09-14T20:00:00+08:00")
        assert n == 4
        loaded = Calendar.load(conn)
    assert loaded.all_dates == cal.all_dates


def test_load_empty_db_raises(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        with pytest.raises(ValueError, match="empty"):
            Calendar.load(conn)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_calendar.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.calendar'`

- [ ] **Step 3: 写实现**

```python
import bisect
import sqlite3
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Calendar:
    """交易日历（R11）。用指数日线的日期集合构建，不手写节假日表。"""

    _dates: tuple[str, ...]

    @classmethod
    def from_dates(cls, dates: Iterable[str]) -> "Calendar":
        return cls(tuple(sorted({d for d in dates if d})))

    @property
    def all_dates(self) -> tuple[str, ...]:
        return self._dates

    def is_open(self, d: str) -> bool:
        i = bisect.bisect_left(self._dates, d)
        return i < len(self._dates) and self._dates[i] == d

    def next_trading_day(self, d: str) -> str:
        i = bisect.bisect_right(self._dates, d)
        if i >= len(self._dates):
            raise IndexError(f"日历已到末尾，{d} 之后没有交易日")
        return self._dates[i]

    def prev_trading_day(self, d: str) -> str:
        i = bisect.bisect_left(self._dates, d)
        if i == 0:
            raise IndexError(f"日历已到开头，{d} 之前没有交易日")
        return self._dates[i - 1]

    def sessions(self, start: str, end: str) -> list[str]:
        lo = bisect.bisect_left(self._dates, start)
        hi = bisect.bisect_right(self._dates, end)
        return list(self._dates[lo:hi])

    def save(self, conn: sqlite3.Connection, *, source: str, now: str) -> int:
        rows = [(d, 1, source, now) for d in self._dates]
        conn.executemany(
            "INSERT OR IGNORE INTO trading_calendar (date, is_open, source, created_at)"
            " VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
        return len(rows)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Calendar":
        rows = conn.execute(
            "SELECT date FROM trading_calendar WHERE is_open = 1 ORDER BY date"
        ).fetchall()
        if not rows:
            raise ValueError("trading_calendar is empty; run ingest first")
        return cls(tuple(r["date"] for r in rows))
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_calendar.py -q`
Expected: `9 passed`

- [ ] **Step 5: 全量回归 + 提交**

```bash
.venv/bin/python -m pytest -q
git add stocklab/calendar/ tests/test_calendar.py
git commit -m "feat(p1): 交易日历（由指数 bars 构建，R11）"
```

**P1 完成标志**：`pytest -q` 全绿；`docs/decisions/ADR-001` 存在；`data/` 与 `.venv/` 已在 `.gitignore`。

---

# P2：数据层

**P2 验收（DoD）**：离线 fixture 单测全绿；真实抓取 ≥320 根日K 并落库；限流/断网/GBK/空响应均有降级测试；单位换算有断言；`raw_fetch_cache` 命中时不联网；`stocklab doctor` 能报告数据健康度。

---

### Task 8: HTTP 层（白名单 + 退避重试 + 限流）

**Files:**
- Create: `stocklab/data/http.py`
- Test: `tests/test_data_http.py`

**Interfaces:**
- Produces:
  - `RetryPolicy(attempts=4, base_delay=0.8, max_delay=8.0, min_interval=0.35)`
  - `HttpClient(policy, *, session=None, sleep=time.sleep, rng=None, cache=None)`
  - `HttpClient.get_text(url, *, params=None, encoding=None, headers=None, source="", cache_key="") -> str`
  - 行为契约：① 每次请求前 `assert_host_allowed`；② 空响应/5xx/超时按指数退避重试；③ 重试耗尽抛 `RateLimited`（空响应）或 `FetchError`；④ 同域请求间隔 ≥ `min_interval`；⑤ 缓存命中直接返回，不发请求。

- [ ] **Step 1: 写失败测试 `tests/test_data_http.py`**

```python
import pytest

from stocklab.config.universe import assert_host_allowed
from stocklab.data.errors import FetchError, HostNotAllowed, RateLimited
from stocklab.data.http import HttpClient, RetryPolicy

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


class FakeResponse:
    def __init__(self, text="", status_code=200, content=None):
        self.text = text
        self.status_code = status_code
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = {}


class FakeSession:
    """记录调用次数并依次返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if not self.responses:
            return FakeResponse(text="")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def no_sleep():
    slept = []
    return slept, (lambda s: slept.append(s))


def test_host_whitelist_enforced():
    c = HttpClient(RetryPolicy(attempts=1), session=FakeSession([]))
    with pytest.raises(HostNotAllowed):
        c.get_text("https://evil.example.com/x")


def test_successful_fetch_returns_text(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="ok")])
    c = HttpClient(RetryPolicy(attempts=3), session=s, sleep=sleep)
    assert c.get_text(KLINE_URL) == "ok"
    assert len(s.calls) == 1


def test_retries_on_empty_response_then_succeeds(no_sleep):
    """东财限流的真实表现就是返回空响应。"""
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text=""), FakeResponse(text="data")])
    c = HttpClient(RetryPolicy(attempts=3, base_delay=1.0), session=s, sleep=sleep)
    assert c.get_text(KLINE_URL) == "data"
    assert len(s.calls) == 2
    assert slept == [1.0]      # 第一次退避


def test_backoff_is_exponential(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="")] * 3 + [FakeResponse(text="data")])
    c = HttpClient(RetryPolicy(attempts=5, base_delay=1.0, max_delay=100), session=s,
                   sleep=sleep)
    c.get_text(KLINE_URL)
    assert slept == [1.0, 2.0, 4.0]


def test_backoff_capped(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="")] * 5)
    c = HttpClient(RetryPolicy(attempts=5, base_delay=1.0, max_delay=2.5), session=s,
                   sleep=sleep)
    with pytest.raises(RateLimited):
        c.get_text(KLINE_URL)
    assert slept == [1.0, 2.0, 2.5, 2.5]


def test_exhausted_retries_on_empty_raises_rate_limited(no_sleep):
    _, sleep = no_sleep
    c = HttpClient(RetryPolicy(attempts=2, base_delay=0.1), session=FakeSession(
        [FakeResponse(text=""), FakeResponse(text="")]), sleep=sleep)
    with pytest.raises(RateLimited):
        c.get_text(KLINE_URL)


def test_connection_error_retried_then_fetch_error(no_sleep):
    _, sleep = no_sleep
    s = FakeSession([ConnectionError("boom"), ConnectionError("boom")])
    c = HttpClient(RetryPolicy(attempts=2, base_delay=0.1), session=s, sleep=sleep)
    with pytest.raises(FetchError):
        c.get_text(KLINE_URL)


def test_http_500_retried(no_sleep):
    _, sleep = no_sleep
    s = FakeSession([FakeResponse(status_code=500), FakeResponse(text="ok")])
    c = HttpClient(RetryPolicy(attempts=3, base_delay=0.1), session=s, sleep=sleep)
    assert c.get_text(KLINE_URL) == "ok"


def test_404_is_not_retried(no_sleep):
    """4xx 是请求本身错了，重试没意义，直接失败。"""
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(status_code=404)])
    c = HttpClient(RetryPolicy(attempts=3, base_delay=0.1), session=s, sleep=sleep)
    with pytest.raises(FetchError):
        c.get_text(KLINE_URL)
    assert len(s.calls) == 1
    assert slept == []


def test_min_interval_between_requests(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="a"), FakeResponse(text="b")])
    clock = iter([100.0, 100.1, 200.0, 200.4])   # 第二次间隔 0.1s < 0.35s
    c = HttpClient(RetryPolicy(attempts=1, min_interval=0.35), session=s, sleep=sleep,
                   clock=lambda: next(clock))
    c.get_text(KLINE_URL)
    c.get_text(KLINE_URL)
    assert any(abs(x - 0.25) < 0.01 for x in slept)


def test_gbk_decoding():
    """腾讯返回 GBK；按 UTF-8 硬解码会乱码（第 5 节坑 2）。"""
    gbk_body = "v_sz000333=\"美的集团~10.00\";".encode("gbk")
    s = FakeSession([FakeResponse(content=gbk_body)])
    c = HttpClient(RetryPolicy(attempts=1), session=s, sleep=lambda _: None)
    text = c.get_text("https://qt.gtimg.cn/q=sz000333", encoding="gbk")
    assert "美的集团" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_data_http.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.data.http'`

- [ ] **Step 3: 写实现**

```python
import time
import random
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import requests

from stocklab.config.universe import assert_host_allowed
from stocklab.data.errors import FetchError, RateLimited


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.8
    max_delay: float = 8.0
    min_interval: float = 0.35
    timeout: float = 10.0

    def delay_for(self, attempt: int) -> float:
        """attempt 从 0 开始。指数退避 + 上限。"""
        return min(self.base_delay * (2 ** attempt), self.max_delay)


class HttpClient:
    """所有出网请求的唯一入口。

    契约：白名单校验 → 限流间隔 → 重试（空响应/5xx/超时）→ 统一异常。
    """

    def __init__(self, policy: RetryPolicy | None = None, *, session=None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None):
        self.policy = policy or RetryPolicy()
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random(0)   # 固定种子：R9 可复现
        self._last_call: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        last = self._last_call.get(host)
        now = self._clock()
        if last is not None:
            wait = self.policy.min_interval - (now - last)
            if wait > 0:
                self._sleep(round(wait, 4))
                now = self._clock()
        self._last_call[host] = now

    def get_text(self, url: str, *, params: dict | None = None, encoding: str | None = None,
                 headers: dict | None = None, source: str = "") -> str:
        assert_host_allowed(url)
        host = urlparse(url).hostname or ""
        empty_error: Exception = RateLimited(f"空响应（疑似限流）: {url}")
        last_error: Exception | None = None

        for attempt in range(self.policy.attempts):
            self._throttle(host)
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=self.policy.timeout)
            except requests.RequestException as exc:
                last_error = FetchError(f"请求异常: {exc}")
            else:
                code = resp.status_code
                if 400 <= code < 500 and code != 429:
                    raise FetchError(f"HTTP {code}（不重试）: {url}")
                if code == 200 and resp.content:
                    if encoding == "gbk":
                        return resp.content.decode("gbk", errors="replace")
                    return resp.text
                last_error = empty_error if code == 200 else FetchError(f"HTTP {code}")
            if attempt < self.policy.attempts - 1:
                self._sleep(self.policy.delay_for(attempt))
        raise last_error or FetchError(f"抓取失败: {url}")
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_data_http.py -q`
Expected: `11 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/data/http.py tests/test_data_http.py
git commit -m "feat(p2): HTTP 层（白名单/退避重试/限流间隔/GBK）"
```

---

### Task 9: 领域对象与腾讯快照解析

**Files:**
- Create: `stocklab/data/models.py`, `stocklab/data/sources/__init__.py`, `stocklab/data/sources/tencent.py`
- Test: `tests/test_source_tencent_quote.py`

**Interfaces:**
- Produces:
  - `Bar(code, date, open, high, low, close, volume, amount, turnover, source)`（frozen dataclass；volume 单位=股，amount 单位=元）
  - `Quote(code, name, price, pre_close, open, high, low, volume, amount, turnover, pe_ttm, float_mv, total_mv, pb, ts)`
  - `tencent.parse_quote(text: str) -> list[Quote]`
  - `tencent.parse_kline(payload: dict, code: str, *, adj_mode: str) -> list[Bar]`
  - `tencent.QUOTE_URL`、`tencent.kline_url(code, count)`

- [ ] **Step 1: 写失败测试 `tests/test_source_tencent_quote.py`**

```python
import pytest

from stocklab.data.sources import tencent

# 真实响应结构（GBK 解出的文本），字段用 ~ 分隔
RAW_QUOTE = (
    'v_sz000333="51~美的集团~000333~76.50~76.00~76.10~123456~789012~'
    '345678~76.55~76.49~76.49~76.20~76.40~76.50~0.50~0.66~76.55~76.00~'
    '76.50/123456/945000000~123456~78901~0.00~0~76.50~12.3~456.7~789.0~'
    '12.34~9.87~1.23~1.45~14:59:59~0.50~0.66";'
)


def test_parse_single_quote():
    quotes = tencent.parse_quote(RAW_QUOTE)
    assert len(quotes) == 1
    q = quotes[0]
    assert q.code == "000333"
    assert q.name == "美的集团"
    assert q.pre_close == pytest.approx(76.00)


def test_quote_volume_converted_from_lots_to_shares():
    """第 5 节坑：接口量单位是「手」，落库统一为「股」。"""
    q = tencent.parse_quote(RAW_QUOTE)[0]
    assert q.volume == 123456 * 100


def test_quote_amount_converted_from_wan_to_yuan():
    q = tencent.parse_quote(RAW_QUOTE)[0]
    assert q.amount == pytest.approx(78901 * 10000)


def test_parse_multiple_quotes():
    text = RAW_QUOTE + RAW_QUOTE.replace("sz000333", "sh600690").replace(
        "美的集团", "海尔智家")
    quotes = tencent.parse_quote(text)
    assert [q.code for q in quotes] == ["000333", "600690"]


def test_parse_empty_text_returns_empty_list():
    assert tencent.parse_quote("") == []


def test_parse_malformed_line_is_skipped_not_crashed():
    """单条坏数据不能让整批失败（R10：但要留痕，由调用方记录）。"""
    text = 'v_sz000333="garbage";' + RAW_QUOTE
    quotes = tencent.parse_quote(text)
    assert len(quotes) == 1


def test_parse_kline_row_order_is_open_close_high_low():
    """关键坑：腾讯日K 每行是 [日期,开,收,最高,最低,量]，不是 OHLC。"""
    payload = {
        "code": 0,
        "data": {
            "sz000333": {
                "qfqday": [
                    ["2026-09-11", "10.00", "10.50", "10.80", "9.90", "123456"],
                    ["2026-09-14", "10.50", "10.30", "10.60", "10.20", "234567"],
                ]
            }
        },
    }
    bars = tencent.parse_kline(payload, "000333", adj_mode="qfq")
    assert len(bars) == 2
    b = bars[0]
    assert b.date == "2026-09-11"
    assert b.open == pytest.approx(10.00)
    assert b.close == pytest.approx(10.50)   # 第 2 列是收盘，不是最高
    assert b.high == pytest.approx(10.80)
    assert b.low == pytest.approx(9.90)
    assert b.volume == 123456 * 100          # 手 → 股


def test_parse_kline_missing_key_returns_empty():
    assert tencent.parse_kline({"data": {}}, "000333", adj_mode="qfq") == []


def test_parse_kline_skips_malformed_row():
    payload = {"data": {"sz000333": {"qfqday": [
        ["2026-09-11", "10.00"],                       # 残缺行
        ["2026-09-14", "10.50", "10.30", "10.60", "10.20", "234567"],
    ]}}}
    bars = tencent.parse_kline(payload, "000333", adj_mode="qfq")
    assert len(bars) == 1
    assert bars[0].date == "2026-09-14"


def test_kline_url_uses_allowed_host():
    url = tencent.kline_url("sz000333", 320)
    assert url.startswith("https://web.ifzq.gtimg.cn/")
    assert "320" in url
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_source_tencent_quote.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写 `stocklab/data/models.py`**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int          # 股
    amount: float | None # 元
    turnover: float | None
    source: str
    adj_mode: str = "none"


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: int          # 股
    amount: float        # 元
    turnover: float | None
    pe_ttm: float | None
    float_mv: float | None
    total_mv: float | None
    pb: float | None
    ts: str
```

- [ ] **Step 4: 写 `stocklab/data/sources/tencent.py`**

```python
"""腾讯行情适配器（纯函数：raw → 领域对象）。

字段索引见设计总纲第 5 节。
注意两处坑：① 返回体是 GBK；② 日K 每行是 O,C,H,L 顺序而非 O,H,L,C。
"""
from stocklab.data.models import Bar, Quote

QUOTE_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
LOT = 100          # 1 手 = 100 股
WAN = 10_000       # 1 万 = 10000 元

_FIELDS = {
    "name": 1, "price": 3, "pre_close": 4, "open": 5, "high": 33, "low": 34,
    "volume": 36, "amount": 37, "turnover": 38, "pe_ttm": 39,
    "float_mv": 44, "total_mv": 45, "pb": 46, "ts": 30,
}


def kline_url(code: str, count: int = 320, adj: str = "qfq") -> str:
    return f"{KLINE_URL}?param={code},day,,,{count},{adj}"


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_quote(text: str) -> list[Quote]:
    """解析腾讯快照。坏行跳过（调用方负责记录）。"""
    out: list[Quote] = []
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        lhs, _, rhs = line.partition("=")
        body = rhs.strip().strip('"')
        parts = body.split("~")
        if len(parts) < 47:
            continue
        code = parts[2]
        name = parts[_FIELDS["name"]]
        price = _to_float(parts[_FIELDS["price"]])
        pre_close = _to_float(parts[_FIELDS["pre_close"]])
        if not code or price is None or pre_close is None:
            continue
        vol_lots = _to_float(parts[_FIELDS["volume"]]) or 0.0
        amount_wan = _to_float(parts[_FIELDS["amount"]]) or 0.0
        out.append(
            Quote(
                code=code,
                name=name,
                price=price,
                pre_close=pre_close,
                open=_to_float(parts[_FIELDS["open"]]) or 0.0,
                high=_to_float(parts[_FIELDS["high"]]) or 0.0,
                low=_to_float(parts[_FIELDS["low"]]) or 0.0,
                volume=int(vol_lots * LOT),
                amount=amount_wan * WAN,
                turnover=_to_float(parts[_FIELDS["turnover"]]),
                pe_ttm=_to_float(parts[_FIELDS["pe_ttm"]]),
                float_mv=(_to_float(parts[_FIELDS["float_mv"]]) or 0.0) * 1e8,
                total_mv=(_to_float(parts[_FIELDS["total_mv"]]) or 0.0) * 1e8,
                pb=_to_float(parts[_FIELDS["pb"]]),
                ts=parts[_FIELDS["ts"]],
                # 说明：lhs 里的 sz000333 也是可用的市场前缀，此处以字段 2 为准
            )
        )
        _ = lhs
    return out


def parse_kline(payload: dict, code: str, *, adj_mode: str = "qfq") -> list[Bar]:
    """解析腾讯日K。行格式 [日期, 开, 收, 最高, 最低, 成交量(手)]。"""
    data = (payload or {}).get("data") or {}
    node = data.get(f"sz{code}") or data.get(f"sh{code}") or {}
    key = "qfqday" if adj_mode == "qfq" else "day"
    rows = node.get(key) or node.get("day") or []
    out: list[Bar] = []
    for row in rows:
        if len(row) < 6:
            continue
        o, c, h, low = (_to_float(row[1]), _to_float(row[2]),
                        _to_float(row[3]), _to_float(row[4]))
        v = _to_float(row[5])
        if None in (o, c, h, low, v):
            continue
        out.append(
            Bar(code=code, date=str(row[0]), open=o, high=h, low=low, close=c,
                volume=int(v * LOT), amount=None, turnover=None,
                source="tencent", adj_mode=adj_mode)
        )
    return out
```

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_source_tencent_quote.py -q`
Expected: `10 passed`

- [ ] **Step 6: 提交**

```bash
git add stocklab/data/models.py stocklab/data/sources/ tests/test_source_tencent_quote.py
git commit -m "feat(p2): 腾讯行情适配器（快照 + 日K，含单位换算）"
```

---

### Task 10: 东财适配器（日K 长历史 + 资金流）

**Files:**
- Create: `stocklab/data/sources/eastmoney.py`
- Test: `tests/test_source_eastmoney.py`

**Interfaces:**
- Produces:
  - `eastmoney.KLINE_URL`、`eastmoney.kline_url(secid, *, beg, end, adjust=1)`
  - `eastmoney.parse_kline(payload: dict, code: str, *, adj_mode: str, adjust: int = 1) -> list[Bar]`
  - `eastmoney.parse_money_flow(payload: dict) -> list[dict]`
  - `eastmoney.HEADERS`（必须带 Referer，见总纲第 5 节）

> 东财 kline 的 `klines` 是逗号分隔字符串：`日期,开,收,高,低,成交量(手),成交额(元),振幅,涨跌幅,涨跌额,换手率` —— **同样是 O,C,H,L 顺序**。

- [ ] **Step 1: 写失败测试 `tests/test_source_eastmoney.py`**

```python
import pytest

from stocklab.data.sources import eastmoney

KLINE_PAYLOAD = {
    "rc": 0,
    "data": {
        "code": "000333",
        "klines": [
            "2026-09-11,10.00,10.50,10.80,9.90,123456,129000000.00,9.0,5.0,0.50,1.20",
            "2026-09-14,10.50,10.30,10.60,10.20,234567,241000000.00,4.0,-1.9,-0.20,2.30",
        ],
    },
}


def test_parse_kline():
    bars = eastmoney.parse_kline(KLINE_PAYLOAD, "000333", adj_mode="none")
    assert len(bars) == 2
    b = bars[0]
    assert b.date == "2026-09-11"
    assert b.open == pytest.approx(10.00)
    assert b.close == pytest.approx(10.50)
    assert b.high == pytest.approx(10.80)
    assert b.low == pytest.approx(9.90)
    assert b.volume == 123456 * 100            # 手 → 股
    assert b.amount == pytest.approx(129000000.00)


def test_parse_kline_turnover():
    assert eastmoney.parse_kline(KLINE_PAYLOAD, "000333", adj_mode="none")[0].turnover \
        == pytest.approx(1.20)


def test_parse_kline_empty():
    assert eastmoney.parse_kline({"data": None}, "000333", adj_mode="none") == []


def test_parse_kline_skips_short_row():
    payload = {"data": {"klines": ["2026-09-11,10.00", "2026-09-14,10.5,10.3,10.6,10.2,"
                                   "2345,2410000.0,4,1,1,2"]}}
    assert len(eastmoney.parse_kline(payload, "000333", adj_mode="none")) == 1


def test_kline_url_builder_has_range():
    """B2：必须能指定 beg/end 才能拿到长历史。"""
    url = eastmoney.kline_url("0.000333", beg="20150101", end="20260914")
    assert "beg=20150101" in url or "beg=20150101" in url.replace("%3D", "=")
    assert "secid=0.000333" in url


def test_referer_header_present():
    assert "Referer" in eastmoney.HEADERS


def test_parse_money_flow():
    payload = {"data": {"klines": [
        "2026-09-11,1000.0,-200.0,300.0,400.0,600.0",
        "2026-09-14,-500.0,100.0,-100.0,-200.0,-300.0",
    ]}}
    rows = eastmoney.parse_money_flow(payload)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-09-11"
    assert rows[0]["main_net"] == pytest.approx(1000.0)
    # 校验自洽：主力 = 大单 + 超大单
    assert rows[0]["main_net"] == pytest.approx(
        rows[0]["big_net"] + rows[0]["xl_net"])
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_source_eastmoney.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
"""东方财富适配器（纯函数）。

注意：① 必须带 Referer；② klines 行是 O,C,H,L 顺序；
③ 成交量单位「手」需 ×100；④ 成交额单位「元」不需换算。
"""
from stocklab.data.models import Bar
from stocklab.data.sources.tencent import LOT, _to_float

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FFLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"

HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; stocklab/0.1)",
}


def kline_url(secid: str, *, beg: str, end: str, adjust: int = 0,
              klt: int = 101) -> str:
    return (
        f"{KLINE_URL}?secid={secid}&klt={klt}&fqt={adjust}"
        f"&beg={beg}&end={end}"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    )


def parse_kline(payload: dict, code: str, *, adj_mode: str = "none",
                adjust: int = 1) -> list[Bar]:
    data = (payload or {}).get("data") or {}
    rows = data.get("klines") or []
    out: list[Bar] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 7:
            continue
        o, c, h, low = (_to_float(parts[1]), _to_float(parts[2]),
                        _to_float(parts[3]), _to_float(parts[4]))
        v, amt = _to_float(parts[5]), _to_float(parts[6])
        if None in (o, c, h, low, v):
            continue
        out.append(
            Bar(code=code, date=parts[0], open=o, high=h, low=low, close=c,
                volume=int(v * LOT), amount=amt,
                turnover=_to_float(parts[10]) if len(parts) > 10 else None,
                source="eastmoney", adj_mode=adj_mode)
        )
    return out


def parse_money_flow(payload: dict) -> list[dict]:
    data = (payload or {}).get("data") or {}
    rows = data.get("klines") or []
    out: list[dict] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 6:
            continue
        vals = [_to_float(p) for p in parts[1:6]]
        if any(v is None for v in vals):
            continue
        out.append({
            "date": parts[0],
            "main_net": vals[0],
            "small_net": vals[1],
            "mid_net": vals[2],
            "big_net": vals[3],
            "xl_net": vals[4],
        })
    return out
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_source_eastmoney.py -q`
Expected: `7 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/data/sources/eastmoney.py tests/test_source_eastmoney.py
git commit -m "feat(p2): 东财适配器（长历史日K + 资金流）"
```

---

### Task 11: 原始响应缓存与 fixture 录制（可复现性地基）

**Files:**
- Create: `stocklab/data/raw_cache.py`
- Test: `tests/test_data_raw_cache.py`

**Interfaces:**
- Produces:
  - `sha256_bytes(b: bytes) -> str`
  - `RawCache(cache_dir: Path)`：`.store(source, params_key, url, body: bytes, encoding, fetched_at) -> str`（返回 hash）、`.load(source, params_key) -> bytes | None`、`.has(source, params_key) -> bool`
  - `record_fixture(fixture_dir: Path, name: str, body: bytes, meta: dict) -> Path`
  - `load_fixture(fixture_dir: Path, name: str) -> tuple[bytes, dict]`

> 依据评审 B3/C5：**复现模式与测试一律读缓存/fixture，不重新联网。**

- [ ] **Step 1: 写失败测试 `tests/test_data_raw_cache.py`**

```python
import json

from stocklab.data.raw_cache import RawCache, load_fixture, record_fixture, sha256_bytes


def test_sha256_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")


def test_cache_roundtrip(tmp_path):
    c = RawCache(tmp_path)
    h = c.store("tencent", "sz000333:320:qfq", "https://web.ifzq.gtimg.cn/x",
                b"body-bytes", encoding="gbk", fetched_at="2026-09-14T19:00:00+08:00")
    assert c.has("tencent", "sz000333:320:qfq")
    assert c.load("tencent", "sz000333:320:qfq") == b"body-bytes"
    assert h == sha256_bytes(b"body-bytes")


def test_cache_miss_returns_none(tmp_path):
    assert RawCache(tmp_path).load("tencent", "nope") is None


def test_cache_is_append_only_on_key(tmp_path):
    """同一 key 重复写：保留首次（历史快照不可被后来的抓取覆盖）。"""
    c = RawCache(tmp_path)
    c.store("tencent", "k", "u", b"first", encoding="utf-8", fetched_at="t1")
    c.store("tencent", "k", "u", b"second", encoding="utf-8", fetched_at="t2")
    assert c.load("tencent", "k") == b"first"


def test_cache_key_is_filesystem_safe(tmp_path):
    """params_key 含 / : 等字符时必须被转义，不能建出目录。"""
    c = RawCache(tmp_path)
    c.store("eastmoney", "0.000333/2015-2026", "u", b"x", encoding="utf-8", fetched_at="t")
    assert c.load("eastmoney", "0.000333/2015-2026") == b"x"
    files = list((tmp_path / "eastmoney").iterdir())
    assert len(files) == 1


def test_fixture_roundtrip(tmp_path):
    p = record_fixture(tmp_path, "tencent_kline_000333",
                       b"payload", {"url": "https://x", "encoding": "gbk"})
    assert p.exists()
    body, meta = load_fixture(tmp_path, "tencent_kline_000333")
    assert body == b"payload"
    assert meta["url"] == "https://x"
    assert meta["sha256"] == sha256_bytes(b"payload")
    assert json.loads(p.with_suffix(".json").read_text())["encoding"] == "gbk"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_data_raw_cache.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
import hashlib
import json
import re
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _safe(name: str) -> str:
    """把任意 key 转成安全的文件名（保持可逆性靠 hashlib 后缀）。"""
    cleaned = _SAFE.sub("_", name)[:80]
    return f"{cleaned}.{sha256_bytes(name.encode('utf-8'))[:12]}"


class RawCache:
    """原始响应落盘缓存。

    契约：同一 key **保留首次写入**，后续抓取不覆盖 ——
    否则「历史数据被源站改写」会污染复现（评审 B3）。
    """

    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)

    def _path(self, source: str, params_key: str) -> Path:
        return self.dir / source / f"{_safe(params_key)}.bin"

    def _meta_path(self, source: str, params_key: str) -> Path:
        return self.dir / source / f"{_safe(params_key)}.json"

    def has(self, source: str, params_key: str) -> bool:
        return self._path(source, params_key).exists()

    def store(self, source: str, params_key: str, url: str, body: bytes, *,
              encoding: str, fetched_at: str) -> str:
        p = self._path(source, params_key)
        digest = sha256_bytes(body)
        if p.exists():
            return sha256_bytes(p.read_bytes())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        self._meta_path(source, params_key).write_text(
            json.dumps({"source": source, "params_key": params_key, "url": url,
                        "encoding": encoding, "fetched_at": fetched_at,
                        "sha256": digest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return digest

    def load(self, source: str, params_key: str) -> bytes | None:
        p = self._path(source, params_key)
        return p.read_bytes() if p.exists() else None


def record_fixture(fixture_dir: Path, name: str, body: bytes, meta: dict) -> Path:
    """把真实响应录成测试 fixture（评审 C5：录一次，跑无数次）。"""
    fixture_dir = Path(fixture_dir)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    bin_path = fixture_dir / f"{name}.bin"
    meta_path = fixture_dir / f"{name}.json"
    bin_path.write_bytes(body)
    payload = dict(meta)
    payload["sha256"] = sha256_bytes(body)
    payload["name"] = name
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return bin_path


def load_fixture(fixture_dir: Path, name: str) -> tuple[bytes, dict]:
    fixture_dir = Path(fixture_dir)
    body = (fixture_dir / f"{name}.bin").read_bytes()
    meta = json.loads((fixture_dir / f"{name}.json").read_text(encoding="utf-8"))
    return body, meta
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_data_raw_cache.py -q`
Expected: `6 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/data/raw_cache.py tests/test_data_raw_cache.py
git commit -m "feat(p2): 原始响应缓存与 fixture 录制（可复现性地基）"
```

---

### Task 12: 长历史数据源实测（阻塞项 B2 的解法）

**Files:**
- Create: `scripts/probe_history.py`
- Modify: `docs/decisions/ADR-002-长历史数据源.md`

**Interfaces:**
- Consumes: `HttpClient`（Task 8）、`eastmoney.kline_url`（Task 10）、`RawCache`（Task 11）
- Produces: `docs/decisions/ADR-002-长历史数据源.md` —— **决定 P4 walk-forward 是否可行**

> **这是 P2 的第一个动作，不是最后一个。** 结论不确定前不要往下做 Task 26。

- [ ] **Step 1: 写探针脚本 `scripts/probe_history.py`**

```python
"""实测东财/腾讯能回溯多久的日K —— 决定 walk-forward 是否可行（评审 B2）。

用法: .venv/bin/python scripts/probe_history.py
输出: 每个源的最早可用日期、总条数、耗时
"""
import json
import sys
import time
from datetime import datetime, timezone

from stocklab.config.paths import RAW_CACHE_DIR, ensure_dirs
from stocklab.data.http import HttpClient, RetryPolicy
from stocklab.data.raw_cache import RawCache
from stocklab.data.sources import eastmoney, tencent

TZ = timezone(timedelta_hours := 8)


def probe_eastmoney(client, cache, secid="0.000333"):
    url = eastmoney.kline_url(secid, beg="19900101", end="20260914", adjust=0)
    t0 = time.time()
    text = client.get_text(url, headers=eastmoney.HEADERS, source="eastmoney")
    payload = json.loads(text)
    bars = eastmoney.parse_kline(payload, secid.split(".")[1], adj_mode="none")
    return {"source": "eastmoney", "url": url, "count": len(bars),
            "first": bars[0].date if bars else None,
            "last": bars[-1].date if bars else None,
            "seconds": round(time.time() - t0, 2)}


def probe_tencent(client, cache, code="sz000333"):
    url = tencent.kline_url(code, 320, "qfq")
    t0 = time.time()
    text = client.get_text(url, source="tencent")
    payload = json.loads(text)
    bars = tencent.parse_kline(payload, code[2:], adj_mode="qfq")
    return {"source": "tencent", "url": url, "count": len(bars),
            "first": bars[0].date if bars else None,
            "last": bars[-1].date if bars else None,
            "seconds": round(time.time() - t0, 2)}


def main() -> int:
    ensure_dirs()
    client = HttpClient(RetryPolicy(attempts=3))
    cache = RawCache(RAW_CACHE_DIR)
    results = []
    for fn in (probe_eastmoney, probe_tencent):
        try:
            results.append(fn(client, cache))
        except Exception as exc:                      # noqa: BLE001 — 探针要报告一切失败
            results.append({"source": fn.__name__, "error": repr(exc)})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 运行探针（需联网）**

```bash
.venv/bin/python scripts/probe_history.py
```

Expected（东财可用时）：
```json
[
  {"source": "eastmoney", "count": 1500, "first": "2004-06-25", "last": "2026-09-12", ...},
  {"source": "tencent", "count": 320, "first": "2025-06-05", "last": "2026-09-12", ...}
]
```
若东财 `count` < 400 或报错 → **B2 未解决**，停下来，把结果写进 ADR-002 并上报人类决策。

- [ ] **Step 3: 写 `docs/decisions/ADR-002-长历史数据源.md`**

必含：实测日期、两个源的 `count/first/last`、原始响应 hash、结论（P4 walk-forward 能否跑）、若不行的备选方案（降低验收标准 / 缩短训练窗口 / 换源）。

- [ ] **Step 4: 提交**

```bash
git add scripts/probe_history.py docs/decisions/ADR-002-长历史数据源.md
git commit -m "feat(p2): 长历史数据源探针 + ADR-002（B2 决策依据）"
```

---

### Task 13: 数据质量校验

**Files:**
- Create: `stocklab/quality/__init__.py`, `stocklab/quality/checks.py`
- Test: `tests/test_quality_checks.py`

**Interfaces:**
- Produces:
  - `Issue(severity, issue_type, code, date, detail)`（frozen dataclass）
  - `check_bars(bars: Sequence[Bar], *, calendar=None) -> list[Issue]`
  - `check_quote(quote: Quote) -> list[Issue]`
  - 规则：OHLC 关系、非正价格、负成交量、日期重复、日期未递增、量额一致性（±1%）、日历缺口、停牌标记

- [ ] **Step 1: 写失败测试 `tests/test_quality_checks.py`**

```python
import pytest

from stocklab.calendar.trading_calendar import Calendar
from stocklab.data.models import Bar
from stocklab.quality.checks import check_bars

CAL = Calendar.from_dates(["2026-09-10", "2026-09-11", "2026-09-14"])


def bar(date="2026-09-11", o=10.0, h=10.8, low=9.9, c=10.5, v=1000, amt=None):
    return Bar(code="000333", date=date, open=o, high=h, low=low, close=c,
               volume=v, amount=amt, turnover=None, source="test")


def test_valid_bar_has_no_issues():
    assert check_bars([bar()]) == []


def test_high_below_open_is_error():
    issues = check_bars([bar(o=10.0, h=9.5, low=9.0, c=9.8)])
    assert any(i.issue_type == "ohlc_inconsistent" and i.severity == "error"
               for i in issues)


def test_low_above_close_is_error():
    issues = check_bars([bar(o=10.0, h=10.8, low=10.6, c=10.5)])
    assert any(i.issue_type == "ohlc_inconsistent" for i in issues)


def test_non_positive_price_is_error():
    issues = check_bars([bar(o=0.0, h=0.0, low=0.0, c=0.0)])
    assert any(i.issue_type == "non_positive_price" for i in issues)


def test_negative_volume_is_error():
    issues = check_bars([bar(v=-1)])
    assert any(i.issue_type == "negative_volume" for i in issues)


def test_duplicate_date_is_error():
    issues = check_bars([bar("2026-09-11"), bar("2026-09-11")])
    assert any(i.issue_type == "duplicate_date" for i in issues)


def test_out_of_order_dates_is_error():
    issues = check_bars([bar("2026-09-11"), bar("2026-09-10")])
    assert any(i.issue_type == "dates_not_increasing" for i in issues)


def test_amount_volume_consistency_within_tolerance():
    """C3：amount ≈ close × volume（±1%）。这是单位错误的唯一防线。"""
    b = bar(o=10.0, h=10.5, low=9.9, c=10.0, v=1000, amt=10_000.0)
    assert [i for i in check_bars([b]) if i.issue_type == "amount_volume_mismatch"] == []


def test_amount_volume_mismatch_detected():
    # 若把「手」当「股」或把「万」当「元」，偏差是 100 倍
    b = bar(o=10.0, h=10.5, low=9.9, c=10.0, v=1000, amt=1_000_000.0)
    issues = check_bars([b])
    assert any(i.issue_type == "amount_volume_mismatch" for i in issues)


def test_amount_none_skips_consistency_check():
    b = bar(amt=None)
    assert [i for i in check_bars([b]) if i.issue_type == "amount_volume_mismatch"] == []


def test_calendar_gap_detected():
    bars = [bar("2026-09-10"), bar("2026-09-14")]     # 缺 09-11
    issues = check_bars(bars, calendar=CAL)
    assert any(i.issue_type == "calendar_gap" for i in issues)


def test_no_gap_when_complete():
    bars = [bar("2026-09-10"), bar("2026-09-11"), bar("2026-09-14")]
    assert [i for i in check_bars(bars, calendar=CAL) if i.issue_type == "calendar_gap"] == []


def test_zero_volume_flagged_as_possible_suspension():
    b = bar(v=0, amt=0.0)
    issues = check_bars([b])
    assert any(i.issue_type == "zero_volume" and i.severity == "info" for i in issues)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_quality_checks.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
from dataclasses import dataclass
from typing import Sequence

from stocklab.data.models import Bar, Quote

AMOUNT_TOLERANCE = 0.01     # ±1%


@dataclass(frozen=True)
class Issue:
    severity: str       # info | warn | error
    issue_type: str
    code: str
    date: str
    detail: str


def _issue(sev, typ, code, date, detail) -> Issue:
    return Issue(severity=sev, issue_type=typ, code=code, date=date, detail=detail)


def check_bars(bars: Sequence[Bar], *, calendar=None) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[str] = set()
    prev: str | None = None

    for b in bars:
        if min(b.open, b.high, b.low, b.close) <= 0:
            issues.append(_issue("error", "non_positive_price", b.code, b.date,
                                 f"open={b.open} high={b.high} low={b.low} close={b.close}"))
        if b.high < max(b.open, b.close) or b.low > min(b.open, b.close):
            issues.append(_issue("error", "ohlc_inconsistent", b.code, b.date,
                                 f"O={b.open} H={b.high} L={b.low} C={b.close}"))
        if b.volume < 0:
            issues.append(_issue("error", "negative_volume", b.code, b.date,
                                 f"volume={b.volume}"))
        if b.volume == 0:
            issues.append(_issue("info", "zero_volume", b.code, b.date,
                                 "成交量为 0，疑似停牌"))
        if b.date in seen:
            issues.append(_issue("error", "duplicate_date", b.code, b.date, ""))
        elif prev is not None and b.date <= prev:
            issues.append(_issue("error", "dates_not_increasing", b.code, b.date,
                                 f"{b.date} <= {prev}"))
        seen.add(b.date)
        prev = b.date

        if b.amount is not None and b.volume > 0 and b.close > 0:
            expected = b.close * b.volume
            if expected > 0:
                dev = abs(b.amount - expected) / expected
                if dev > AMOUNT_TOLERANCE:
                    issues.append(_issue(
                        "error", "amount_volume_mismatch", b.code, b.date,
                        f"amount={b.amount:.0f} vs close*volume={expected:.0f} "
                        f"偏差 {dev:.1%}（疑似单位错误）"))

    if calendar is not None and bars:
        start, end = bars[0].date, bars[-1].date
        have = {b.date for b in bars}
        for d in calendar.sessions(start, end):
            if d not in have:
                issues.append(_issue("warn", "calendar_gap", bars[0].code, d,
                                     "交易日历有该日但无行情"))
    return issues


def check_quote(q: Quote) -> list[Issue]:
    issues: list[Issue] = []
    if min(q.price, q.pre_close) <= 0:
        issues.append(_issue("error", "non_positive_price", q.code, q.ts,
                             f"price={q.price} pre_close={q.pre_close}"))
    if q.volume < 0:
        issues.append(_issue("error", "negative_volume", q.code, q.ts, ""))
    if q.high and q.low and q.high < q.low:
        issues.append(_issue("error", "ohlc_inconsistent", q.code, q.ts,
                             f"high={q.high} < low={q.low}"))
    return issues
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_quality_checks.py -q`
Expected: `13 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/quality/ tests/test_quality_checks.py
git commit -m "feat(p2): 数据质量校验（含量额一致性防线）"
```

---

### Task 14: 仓储层（唯一写入口）

**Files:**
- Create: `stocklab/store/repo.py`
- Test: `tests/test_store_repo.py`

**Interfaces:**
- Produces:
  - `upsert_instruments(conn, instruments: Sequence[Instrument], now: str) -> int`
  - `insert_bars(conn, bars: Sequence[Bar], *, now: str) -> int`（`INSERT OR REPLACE` 仅对 `bars_daily`；**唯一允许覆盖的表**，理由：行情会被源站修订，需可更新；`adj_mode` 不同视为不同记录）
  - `insert_quality_issues(conn, issues: Sequence[Issue], *, source: str, now: str) -> int`（按 `UNIQUE(date,source,code,issue_type)` 去重累加 `occurrences`）
  - `log_event(conn, module, level, message, *, context=None, now=None) -> int`
  - `record_job(conn, job_name, *, status, started_at, finished_at=None, detail=None) -> int`
  - `latest_bar_date(conn, code) -> str | None`

> `bars_daily` 是 append-only 规则的**刻意例外**：行情数据必须允许源站修订。这一点与总纲 R8 有出入，需在 ADR-001 的补充里写明。

- [ ] **Step 1: 写失败测试 `tests/test_store_repo.py`**

```python
import pytest

from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.quality.checks import Issue
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def bar(date="2026-09-11", c=10.5):
    return Bar(code="000333", date=date, open=10.0, high=10.8, low=9.9, close=c,
               volume=1000, amount=10_500.0, turnover=1.0, source="test")


def test_upsert_instruments(conn):
    n = repo.upsert_instruments(conn, [Instrument("000333", "美的集团", "sz", "main")],
                                now=NOW)
    assert n == 1
    n = repo.upsert_instruments(conn, [Instrument("000333", "美的集团A", "sz", "main")],
                                now=NOW)
    row = conn.execute("SELECT name FROM instruments WHERE code='000333'").fetchone()
    assert row["name"] == "美的集团A"


def test_insert_bars(conn):
    assert repo.insert_bars(conn, [bar()], now=NOW) == 1
    assert repo.latest_bar_date(conn, "000333") == "2026-09-11"


def test_insert_bars_is_idempotent_on_pk(conn):
    repo.insert_bars(conn, [bar()], now=NOW)
    repo.insert_bars(conn, [bar()], now=NOW)
    n = conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0]
    assert n == 1


def test_insert_bars_allows_revision(conn):
    """行情可被源站修订，bars_daily 允许覆盖（刻意例外）。"""
    repo.insert_bars(conn, [bar(c=10.5)], now=NOW)
    repo.insert_bars(conn, [bar(c=10.7)], now=NOW)
    row = conn.execute("SELECT close FROM bars_daily").fetchone()
    assert row["close"] == pytest.approx(10.7)


def test_quality_issue_deduped_and_counted(conn):
    issue = Issue("error", "ohlc_inconsistent", "000333", "2026-09-11", "x")
    repo.insert_quality_issues(conn, [issue], source="test", now=NOW)
    repo.insert_quality_issues(conn, [issue], source="test", now=NOW)
    rows = conn.execute("SELECT occurrences FROM data_quality").fetchall()
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2


def test_log_event(conn):
    repo.log_event(conn, "ingest", "error", "抓取失败", now=NOW)
    row = conn.execute("SELECT message, level FROM system_events").fetchone()
    assert row["level"] == "error"


def test_record_job_lifecycle(conn):
    rid = repo.record_job(conn, "ingest_daily", status="running", started_at=NOW)
    repo.finish_job(conn, rid, status="ok", finished_at=NOW, detail="20/20")
    row = conn.execute("SELECT status, detail FROM job_runs WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == "ok"
    assert row["detail"] == "20/20"


def test_latest_bar_date_none_when_empty(conn):
    assert repo.latest_bar_date(conn, "999999") is None


def test_insert_bars_normalizes_unit_mismatch_before_write(conn):
    """写入前必须已归一化：volume 是股、amount 是元。"""
    repo.insert_bars(conn, [bar()], now=NOW)
    row = conn.execute("SELECT volume, amount FROM bars_daily").fetchone()
    assert row["volume"] == 1000
    assert row["amount"] == pytest.approx(10_500.0)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_store_repo.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.store.repo'`

- [ ] **Step 3: 写实现**

```python
import json
import sqlite3
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.quality.checks import Issue

TZ = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def upsert_instruments(conn: sqlite3.Connection, instruments: Sequence[Instrument],
                       *, now: str) -> int:
    rows = [(i.code, i.name, i.market, i.board, "stock", now) for i in instruments]
    conn.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
        " market=excluded.market, board=excluded.board",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_bars(conn: sqlite3.Connection, bars: Sequence[Bar], *, now: str) -> int:
    """写入日线。bars_daily 允许覆盖（源站会修订历史）。"""
    rows = [(b.code, b.date, b.open, b.high, b.low, b.close, b.volume, b.amount,
             b.turnover, b.adj_mode, 1 if b.volume == 0 else 0, b.source, now)
            for b in bars]
    conn.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, amount,"
        " turnover, adj_mode, is_suspended, source, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(code, date) DO UPDATE SET"
        " open=excluded.open, high=excluded.high, low=excluded.low,"
        " close=excluded.close, volume=excluded.volume, amount=excluded.amount,"
        " turnover=excluded.turnover, adj_mode=excluded.adj_mode,"
        " is_suspended=excluded.is_suspended, source=excluded.source,"
        " fetched_at=excluded.fetched_at",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_quality_issues(conn: sqlite3.Connection, issues: Sequence[Issue], *,
                          source: str, now: str) -> int:
    rows = [(i.date, source, i.code, i.issue_type, i.severity, i.detail, now, now)
            for i in issues]
    conn.executemany(
        "INSERT INTO data_quality (date, source, code, issue_type, severity, detail,"
        " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(date, source, code, issue_type) DO UPDATE SET"
        " last_seen=excluded.last_seen, occurrences=data_quality.occurrences+1",
        rows,
    )
    conn.commit()
    return len(rows)


def log_event(conn: sqlite3.Connection, module: str, level: str, message: str, *,
              context: dict | None = None, now: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO system_events (ts, module, level, message, context_json)"
        " VALUES (?,?,?,?,?)",
        (now or now_iso(), module, level, message,
         json.dumps(context or {}, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_job(conn: sqlite3.Connection, job_name: str, *, status: str,
               started_at: str, scheduled_at: str | None = None,
               detail: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO job_runs (job_name, scheduled_at, started_at, status, detail)"
        " VALUES (?,?,?,?,?)",
        (job_name, scheduled_at, started_at, status, detail),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_job(conn: sqlite3.Connection, run_id: int, *, status: str,
               finished_at: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE job_runs SET status=?, finished_at=?, detail=? WHERE run_id=?",
        (status, finished_at, detail, run_id),
    )
    conn.commit()


def latest_bar_date(conn: sqlite3.Connection, code: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM bars_daily WHERE code=?", (code,)
    ).fetchone()
    return row["d"] if row and row["d"] else None
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_store_repo.py -q`
Expected: `9 passed`

- [ ] **Step 5: 在 `docs/decisions/ADR-001` 追加「bars_daily 允许覆盖」的理由**

- [ ] **Step 6: 提交**

```bash
git add stocklab/store/repo.py tests/test_store_repo.py docs/decisions/
git commit -m "feat(p2): 仓储层（唯一写入口 + 质量问题去重）"
```

---

### Task 15: 采集编排（离线可测）

**Files:**
- Create: `stocklab/data/ingest.py`
- Test: `tests/test_data_ingest.py`

**Interfaces:**
- Consumes: `HttpClient`(T8)、`RawCache`(T11)、`repo`(T14)、`checks`(T13)、`Calendar`(T7)
- Produces:
  - `IngestResult(code, ok, bars_written, issues, error)`（frozen dataclass）
  - `IngestReport(date, results)`：`.ok_count`、`.failed_codes`、`.total_issues`
  - `ingest_daily_bars(conn, client, cache, instruments, *, start, end, now, calendar=None, fetch=None) -> IngestReport`
  - `fetch` 参数是**注入点**：`fetch(code) -> list[Bar]`，测试与复现模式传入缓存读取函数

> **关键设计**：所有联网动作通过 `fetch` 注入。离线测试与复现模式（B3）用同一个入口，代码路径完全一致。

- [ ] **Step 1: 写失败测试 `tests/test_data_ingest.py`**

```python
import pytest

from stocklab.config.universe import Instrument
from stocklab.data.ingest import ingest_daily_bars
from stocklab.data.models import Bar
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"
UNIVERSE = (Instrument("000333", "美的集团", "sz", "main"),
            Instrument("600690", "海尔智家", "sh", "main"))


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    repo.upsert_instruments(c, UNIVERSE, now=NOW)
    yield c
    c.close()


def good_bars(code, n=3):
    dates = ["2026-09-10", "2026-09-11", "2026-09-14"][:n]
    return [Bar(code=code, date=d, open=10.0, high=10.8, low=9.9, close=10.5,
                volume=1000, amount=10_500.0, turnover=1.0, source="test")
            for d in dates]


def test_ingest_writes_bars(conn):
    report = ingest_daily_bars(conn, None, None, UNIVERSE, start="2026-09-10",
                               end="2026-09-14", now=NOW,
                               fetch=lambda code: good_bars(code))
    assert report.ok_count == 2
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 6


def test_ingest_records_failure_without_aborting_others(conn):
    """C4：失败的标的必须留痕，且计入分母，不能静默跳过。"""
    def fetch(code):
        if code == "600690":
            raise RuntimeError("boom")
        return good_bars(code)

    report = ingest_daily_bars(conn, None, None, UNIVERSE, start="2026-09-10",
                               end="2026-09-14", now=NOW, fetch=fetch)
    assert report.ok_count == 1
    assert report.failed_codes == ["600690"]
    events = conn.execute(
        "SELECT COUNT(*) FROM system_events WHERE level='error'").fetchone()[0]
    assert events >= 1


def test_ingest_records_quality_issues(conn):
    def fetch(code):
        bars = good_bars(code)
        bars[0] = Bar(code=code, date="2026-09-10", open=10.0, high=9.0, low=9.5,
                      close=10.5, volume=1000, amount=10_500.0, turnover=None,
                      source="test")
        return bars

    report = ingest_daily_bars(conn, None, None, UNIVERSE[:1], start="2026-09-10",
                               end="2026-09-14", now=NOW, fetch=fetch)
    assert report.total_issues >= 1
    n = conn.execute("SELECT COUNT(*) FROM data_quality").fetchone()[0]
    assert n >= 1


def test_ingest_does_not_write_bars_when_quality_fails(conn):
    """硬错误（OHLC 不一致）的数据不得落库 —— 脏数据比没数据更危险。"""
    def fetch(code):
        return [Bar(code=code, date="2026-09-10", open=10.0, high=9.0, low=9.5,
                    close=10.5, volume=1000, amount=10_500.0, turnover=None,
                    source="test")]

    ingest_daily_bars(conn, None, None, UNIVERSE[:1], start="2026-09-10",
                      end="2026-09-14", now=NOW, fetch=fetch)
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_ingest_records_job_run(conn):
    ingest_daily_bars(conn, None, None, UNIVERSE, start="2026-09-10",
                      end="2026-09-14", now=NOW, fetch=lambda code: good_bars(code))
    row = conn.execute("SELECT status FROM job_runs ORDER BY run_id DESC").fetchone()
    assert row["status"] == "ok"


def test_ingest_marks_job_failed_when_all_fail(conn):
    def fetch(code):
        raise RuntimeError("boom")

    ingest_daily_bars(conn, None, None, UNIVERSE, start="2026-09-10",
                      end="2026-09-14", now=NOW, fetch=fetch)
    row = conn.execute("SELECT status FROM job_runs ORDER BY run_id DESC").fetchone()
    assert row["status"] == "failed"


def test_ingest_result_reports_error_message(conn):
    def fetch(code):
        raise RuntimeError("限流了")

    report = ingest_daily_bars(conn, None, None, UNIVERSE[:1], start="2026-09-10",
                               end="2026-09-14", now=NOW, fetch=fetch)
    assert "限流了" in report.results[0].error
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_data_ingest.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
from dataclasses import dataclass, field
from typing import Callable, Sequence

from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.quality.checks import Issue, check_bars
from stocklab.store import repo

HARD_SEVERITIES = {"error"}


@dataclass(frozen=True)
class IngestResult:
    code: str
    ok: bool
    bars_written: int
    issues: tuple[Issue, ...] = ()
    error: str = ""


@dataclass
class IngestReport:
    date: str
    results: list[IngestResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_codes(self) -> list[str]:
        return [r.code for r in self.results if not r.ok]

    @property
    def total_issues(self) -> int:
        return sum(len(r.issues) for r in self.results)


def ingest_daily_bars(
    conn,
    client,
    cache,
    instruments: Sequence[Instrument],
    *,
    start: str,
    end: str,
    now: str,
    calendar=None,
    fetch: Callable[[str], list[Bar]] | None = None,
) -> IngestReport:
    """采集日线并落库。

    fetch 是注入点：生产传真实抓取函数，测试与复现模式传缓存读取函数。
    失败一律留痕（R10），失败标的计入分母（C4）。
    """
    report = IngestReport(date=end)
    run_id = repo.record_job(conn, "ingest_daily_bars", status="running", started_at=now)

    for inst in instruments:
        try:
            bars = fetch(inst.code) if fetch is not None else []
        except Exception as exc:                       # noqa: BLE001 — 必须留痕
            msg = f"{type(exc).__name__}: {exc}"
            repo.log_event(conn, "ingest", "error",
                           f"{inst.code} 采集失败: {msg}",
                           context={"code": inst.code}, now=now)
            report.results.append(IngestResult(inst.code, False, 0, (), msg))
            continue

        bars = [b for b in bars if start <= b.date <= end]
        issues = check_bars(bars, calendar=calendar)
        if issues:
            repo.insert_quality_issues(conn, issues, source="ingest", now=now)

        if any(i.severity in HARD_SEVERITIES for i in issues):
            repo.log_event(conn, "ingest", "error",
                           f"{inst.code} 质量校验未通过，已跳过写入",
                           context={"n_issues": len(issues)}, now=now)
            report.results.append(IngestResult(inst.code, False, 0, tuple(issues),
                                               "quality check failed"))
            continue

        written = repo.insert_bars(conn, bars, now=now) if bars else 0
        report.results.append(IngestResult(inst.code, True, written, tuple(issues)))

    ok = report.ok_count > 0
    repo.finish_job(conn, run_id, status="ok" if ok else "failed", finished_at=now,
                    detail=f"{report.ok_count}/{len(instruments)} ok, "
                           f"{report.total_issues} issues")
    return report
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_data_ingest.py -q`
Expected: `7 passed`

- [ ] **Step 5: 全量回归 + 提交**

```bash
.venv/bin/python -m pytest -q
git add stocklab/data/ingest.py tests/test_data_ingest.py
git commit -m "feat(p2): 采集编排（fetch 注入 + 失败留痕 + 脏数据不落库）"
```

---

### Task 16: CLI 与真实抓取端到端验证

**Files:**
- Create: `stocklab/cli/__init__.py`, `stocklab/cli/main.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `stocklab` CLI 子命令：`db init`、`ingest bars --days N`、`doctor`、`fixture record`

- [ ] **Step 1: 写测试 `tests/test_cli.py`**

```python
from stocklab.cli.main import build_parser


def test_parser_has_subcommands():
    p = build_parser()
    for cmd in ("db", "ingest", "doctor", "fixture"):
        assert p.parse_args([cmd, "init"] if cmd == "db" else [cmd]).command == cmd


def test_db_init_args():
    args = build_parser().parse_args(["db", "init"])
    assert args.command == "db"
    assert args.db_action == "init"


def test_ingest_bars_defaults():
    args = build_parser().parse_args(["ingest", "bars"])
    assert args.days == 1200
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from stocklab.config import paths
from stocklab.config.settings import load_settings
from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

TZ = ZoneInfo("Asia/Shanghai")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stocklab")
    sub = p.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db")
    db.add_argument("db_action", choices=["init", "check"])

    ing = sub.add_parser("ingest")
    ing.add_argument("target", choices=["bars", "quote"])
    ing.add_argument("--days", type=int, default=1200)
    ing.add_argument("--code", action="append", default=None)

    doc = sub.add_parser("doctor")

    fx = sub.add_parser("fixture")
    fx.add_argument("fx_action", choices=["record"])
    fx.add_argument("--name", required=True)

    return p


def cmd_db_init() -> int:
    paths.ensure_dirs()
    backup = init_db(paths.DB_PATH)
    print(json.dumps({"db": str(paths.DB_PATH),
                      "backup": str(backup) if backup else None}, ensure_ascii=False))
    return 0


def cmd_doctor() -> int:
    """报告数据健康度：各表行数、最新日期、未解决质量问题（C7）。"""
    if not paths.DB_PATH.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False))
        return 2
    out: dict = {}
    with connect(paths.DB_PATH) as conn:
        for table in ("instruments", "bars_daily", "features_daily", "predictions",
                      "verifications", "data_quality", "system_events"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        row = conn.execute("SELECT MAX(date) AS d FROM bars_daily").fetchone()
        out["latest_bar_date"] = row["d"] if row else None
        out["open_issues"] = conn.execute(
            "SELECT COUNT(*) FROM data_quality WHERE resolved=0").fetchone()[0]
        last_job = conn.execute(
            "SELECT job_name, status, finished_at FROM job_runs"
            " ORDER BY run_id DESC LIMIT 1").fetchone()
        out["last_job"] = dict(last_job) if last_job else None
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_ingest_bars(days: int, codes: list[str] | None) -> int:
    """真实抓取（联网）。这是 P2 的端到端验收命令。"""
    import json as _json

    from stocklab.data.http import HttpClient, RetryPolicy
    from stocklab.data.ingest import ingest_daily_bars
    from stocklab.data.raw_cache import RawCache
    from stocklab.data.sources import eastmoney, tencent

    paths.ensure_dirs()
    settings = load_settings()
    now = datetime.now(TZ).isoformat(timespec="seconds")
    universe = tuple(i for i in DEFAULT_UNIVERSE if not codes or i.code in codes)

    client = HttpClient(RetryPolicy(attempts=settings.retry_attempts,
                                    base_delay=settings.retry_base_delay,
                                    max_delay=settings.retry_max_delay,
                                    min_interval=settings.http_min_interval,
                                    timeout=settings.http_timeout))
    cache = RawCache(paths.RAW_CACHE_DIR)

    def fetch(code: str):
        inst = next(i for i in universe if i.code == code)
        url = eastmoney.kline_url(inst.secid, beg="19900101", end="20991231",
                                  adjust=0)
        key = f"{inst.secid}:{days}"
        cached = cache.load("eastmoney", key) if settings.cache_enabled else None
        text = cached.decode("utf-8") if cached else client.get_text(
            url, headers=eastmoney.HEADERS, source="eastmoney")
        if not cached:
            cache.store("eastmoney", key, url, text.encode("utf-8"),
                        encoding="utf-8", fetched_at=now)
        bars = eastmoney.parse_kline(_json.loads(text), code, adj_mode="none")
        if bars:
            return bars
        # 降级到腾讯（总纲第 5 节坑 1）
        t_url = tencent.kline_url(inst.tencent_code, min(days, 320), "qfq")
        t_text = client.get_text(t_url, source="tencent")
        cache.store("tencent", f"{inst.tencent_code}:{days}", t_url,
                    t_text.encode("utf-8"), encoding="utf-8", fetched_at=now)
        return tencent.parse_kline(_json.loads(t_text), code, adj_mode="qfq")

    with connect(paths.DB_PATH) as conn:
        repo.upsert_instruments(conn, universe, now=now)
        report = ingest_daily_bars(conn, client, cache, universe,
                                   start="1990-01-01", end=now[:10], now=now,
                                   fetch=fetch)
    print(json.dumps({"ok": report.ok_count, "failed": report.failed_codes,
                      "issues": report.total_issues,
                      "bars": sum(r.bars_written for r in report.results)},
                     ensure_ascii=False))
    return 0 if report.ok_count else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "db":
        return cmd_db_init() if args.db_action == "init" else cmd_doctor()
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "ingest" and args.target == "bars":
        return cmd_ingest_bars(args.days, args.code)
    print(f"未实现: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行单测**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: `3 passed`

- [ ] **Step 5: 端到端真实抓取（联网，P2 的核心验收）**

```bash
.venv/bin/python -m stocklab.cli.main db init
.venv/bin/python -m stocklab.cli.main ingest bars --days 1200
.venv/bin/python -m stocklab.cli.main doctor
```

Expected（示意，数字以实测为准）：
```
{"db": ".../data/stocklab.db", "backup": null}
{"ok": 2, "failed": [], "issues": 0, "bars": 2xxx}
{
  "instruments": 2,
  "bars_daily": 2xxx,
  "latest_bar_date": "2026-09-11",
  ...
}
```
**要求**：`bars_daily` ≥ 800 行（每只 ≥400 根）。若东财未按 Task 12 预期返回长历史，此处会只有 ~320 行 —— 那就是 B2 仍未解决，必须回头处理 ADR-002 而不是降低验收。

- [ ] **Step 6: 提交**

```bash
git add stocklab/cli/ tests/test_cli.py
git commit -m "feat(p2): CLI（db init/ingest bars/doctor）+ 端到端抓取验证"
```

**P2 完成标志**：`pytest -q` 全绿；`doctor` 显示 ≥800 根日K；`data/raw_cache/` 有原始响应；`ADR-002` 有结论。

---

# P3：特征层

**P3 验收（DoD）**：指标值与手工计算一致；快照 hash 稳定可复现；**point-in-time 测试通过**（追加未来数据不改变历史特征）；同一 (code,date) 重算产生同 hash。

---

### Task 17: 技术指标（纯函数）

**Files:**
- Create: `stocklab/features/__init__.py`, `stocklab/features/indicators.py`
- Test: `tests/test_features_indicators.py`

**Interfaces:**
- Produces（全部为纯函数，输入 `pandas.Series` 或 `numpy.ndarray`，输出同长度 Series）：
  - `sma(values, window) -> pd.Series`
  - `true_range(high, low, close) -> pd.Series`
  - `atr(high, low, close, window=14) -> pd.Series`
  - `rolling_max(values, window)`、`rolling_min(values, window)`
  - `vol_ratio(volume, short=5, long=20) -> pd.Series`
  - `pct_change_n(close, n) -> pd.Series`
  - `percentile_rank(values, window) -> pd.Series`

- [ ] **Step 1: 写失败测试 `tests/test_features_indicators.py`**

```python
import numpy as np
import pandas as pd
import pytest

from stocklab.features import indicators as ind


def test_sma_matches_hand_calculation():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.sma(s, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)   # (1+2+3)/3
    assert out.iloc[4] == pytest.approx(4.0)   # (3+4+5)/3


def test_true_range_first_row_uses_high_low():
    high = pd.Series([10.0, 11.0])
    low = pd.Series([9.0, 9.5])
    close = pd.Series([9.5, 10.5])
    tr = ind.true_range(high, low, close)
    assert tr.iloc[0] == pytest.approx(1.0)              # 首行无前收
    assert tr.iloc[1] == pytest.approx(max(11.0 - 9.5,   # H-L
                                           abs(11.0 - 9.5),
                                           abs(9.5 - 9.5)))


def test_atr_simple_average_of_tr():
    high = pd.Series([10.0] * 5)
    low = pd.Series([9.0] * 5)
    close = pd.Series([9.5] * 5)
    out = ind.atr(high, low, close, window=3)
    assert out.iloc[3] == pytest.approx(1.0)


def test_atr_nan_before_window():
    high = pd.Series([10.0] * 3)
    low = pd.Series([9.0] * 3)
    close = pd.Series([9.5] * 3)
    out = ind.atr(high, low, close, window=14)
    assert out.isna().all()


def test_vol_ratio():
    vol = pd.Series([100.0] * 20 + [200.0])
    out = ind.vol_ratio(vol, short=5, long=20)
    # 近 5 日均量 = (100*4 + 200)/5 = 120；20 日均量 = (100*19+200)/20 = 105
    assert out.iloc[20] == pytest.approx(120.0 / 105.0)


def test_pct_change_n():
    close = pd.Series([10.0, 11.0, 12.0])
    out = ind.pct_change_n(close, 1)
    assert out.iloc[1] == pytest.approx(0.1)
    assert out.iloc[2] == pytest.approx(12.0 / 11.0 - 1)


def test_percentile_rank():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.percentile_rank(s, 5)
    assert out.iloc[4] == pytest.approx(1.0)     # 最大值 → 100 分位
    assert out.iloc[0] == pytest.approx(0.2)     # 最小值 → 20 分位


def test_rolling_max_min():
    s = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0])
    assert ind.rolling_max(s, 3).iloc[2] == pytest.approx(4.0)
    assert ind.rolling_min(s, 3).iloc[2] == pytest.approx(1.0)


def test_no_lookahead_sma():
    """SMA 在 t 时刻的值只依赖 t 及之前 —— 追加未来值不改变历史。"""
    s = pd.Series([1.0, 2.0, 3.0])
    before = ind.sma(s, 2).iloc[2]
    after = ind.sma(pd.Series([1.0, 2.0, 3.0, 100.0, 100.0]), 2).iloc[2]
    assert before == pytest.approx(after)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_features_indicators.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
"""技术指标（纯函数）。

铁律：所有指标在 t 时刻的值只能依赖 t 及之前的数据（R2）。
本模块不使用 shift(-n) / center=True / rolling 的前视参数。
"""
import numpy as np
import pandas as pd


def sma(values: pd.Series, window: int) -> pd.Series:
    return values.rolling(window=window, min_periods=window).mean()


def rolling_max(values: pd.Series, window: int) -> pd.Series:
    return values.rolling(window=window, min_periods=window).max()


def rolling_min(values: pd.Series, window: int) -> pd.Series:
    return values.rolling(window=window, min_periods=window).min()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat([high - low,
                        (high - prev_close).abs(),
                        (low - prev_close).abs()], axis=1)
    tr = ranges.max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]     # 首行无前收
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        window: int = 14) -> pd.Series:
    return true_range(high, low, close).rolling(
        window=window, min_periods=window).mean()


def vol_ratio(volume: pd.Series, short: int = 5, long: int = 20) -> pd.Series:
    return sma(volume, short) / sma(volume, long).replace(0, np.nan)


def pct_change_n(close: pd.Series, n: int) -> pd.Series:
    return close / close.shift(n) - 1.0


def percentile_rank(values: pd.Series, window: int) -> pd.Series:
    """当前值在过去 window 期中的分位（0~1），只看历史。"""
    return values.rolling(window=window, min_periods=window).apply(
        lambda w: float((w <= w[-1]).sum()) / len(w), raw=True)
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_features_indicators.py -q`
Expected: `9 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/features/ tests/test_features_indicators.py
git commit -m "feat(p3): 技术指标纯函数（无前视）"
```

---

### Task 18: 特征快照与稳定哈希

**Files:**
- Create: `stocklab/features/registry.py`, `stocklab/features/snapshot.py`
- Test: `tests/test_features_snapshot.py`

**Interfaces:**
- Produces:
  - `registry.FEATURE_VERSION = "v1"`、`registry.CORE_COLUMNS: tuple[str, ...]`、`registry.PARAMS: dict`
  - `snapshot.canonical_json(obj) -> str`（`sort_keys=True, separators=(",",":"), ensure_ascii=False`）
  - `snapshot.payload_hash(payload: dict) -> str`
  - `snapshot.params_hash(params: dict) -> str`
  - `FeatureSnapshot(code, date, feature_version, feature_set, core, payload, payload_hash, params_hash, data_version)`
  - `snapshot.build_snapshot(code, date, bars, *, params=None, data_version="", extra=None) -> FeatureSnapshot | None`
  - `snapshot.save_snapshot(conn, snap, *, now) -> int`（返回 `snapshot_id`）

- [ ] **Step 1: 写失败测试 `tests/test_features_snapshot.py`**

```python
import pytest

from stocklab.data.models import Bar
from stocklab.features import registry, snapshot
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T20:00:00+08:00"


def make_bars(n=80, base=10.0):
    out = []
    for i in range(n):
        close = base + i * 0.1
        out.append(Bar(code="000333", date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                       open=close - 0.05, high=close + 0.15, low=close - 0.15,
                       close=close, volume=1000 + i, amount=(1000 + i) * close,
                       turnover=1.0, source="test"))
    return out


def test_canonical_json_is_key_order_independent():
    a = snapshot.canonical_json({"b": 1, "a": 2})
    b = snapshot.canonical_json({"a": 2, "b": 1})
    assert a == b
    assert a == '{"a":2,"b":1}'


def test_payload_hash_stable():
    p = {"ma20": 10.5, "atr14": 0.3}
    assert snapshot.payload_hash(p) == snapshot.payload_hash(dict(p))
    assert snapshot.payload_hash(p) != snapshot.payload_hash({"ma20": 10.6})


def test_build_snapshot_produces_core_columns():
    snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80))
    assert snap is not None
    for col in registry.CORE_COLUMNS:
        assert col in snap.core


def test_snapshot_is_none_when_history_too_short():
    """样本不足时必须返回 None，由调用方记录为缺失，不能算出一个假值。"""
    assert snapshot.build_snapshot("000333", "2026-01-05", make_bars(10)) is None


def test_build_snapshot_is_deterministic():
    bars = make_bars(80)
    a = snapshot.build_snapshot("000333", "2026-03-20", bars)
    b = snapshot.build_snapshot("000333", "2026-03-20", bars)
    assert a.payload_hash == b.payload_hash
    assert a.core == b.core


def test_params_hash_changes_with_params():
    bars = make_bars(80)
    a = snapshot.build_snapshot("000333", "2026-03-20", bars, params={"ma": 20})
    b = snapshot.build_snapshot("000333", "2026-03-20", bars, params={"ma": 30})
    assert a.params_hash != b.params_hash


def test_save_snapshot_returns_id_and_is_append_only(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80))
        sid = snapshot.save_snapshot(conn, snap, now=NOW)
        assert sid > 0
        snap2 = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80),
                                        params={"ma": 30})
        sid2 = snapshot.save_snapshot(conn, snap2, now=NOW)
        assert sid2 != sid
        n = conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0]
        assert n == 2


def test_extra_features_land_in_json_payload():
    snap = snapshot.build_snapshot("000333", "2026-03-20", make_bars(80),
                                   extra={"custom": 42})
    assert snap.payload["custom"] == 42
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_features_snapshot.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写 `stocklab/features/registry.py`**

```python
"""特征定义与版本。

feature_version 变更规则：任何会改变数值的改动都必须升版本号，
否则 append-only 的 UNIQUE(code,date,feature_version) 会阻止重算（A1）。
"""
from stocklab.features import indicators as ind

FEATURE_VERSION = "v1"
FEATURE_SET = "core"

PARAMS: dict = {
    "ma_short": 20,
    "ma_long": 60,
    "atr_window": 14,
    "vol_short": 5,
    "vol_long": 20,
    "pe_pct_window": 750,
}

CORE_COLUMNS: tuple[str, ...] = (
    "close", "ma20", "ma60", "atr14", "vol_ratio_5_20",
    "ret_1d", "ret_5d", "main_net_5d", "pe_pct_3y", "regime_label",
)

MIN_HISTORY = 61      # ma60 需要 60 期 + 当期


def compute_core(bars, *, params: dict | None = None) -> dict:
    """由日线序列计算核心特征。

    bars 必须是**按日期升序、且最后一行是 asof 日**的序列（R2）。
    调用方负责裁剪 —— 本函数不做任何日期过滤。
    """
    import pandas as pd

    p = {**PARAMS, **(params or {})}
    df = pd.DataFrame([
        {"date": b.date, "open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": float(b.volume), "amount": b.amount}
        for b in bars
    ])
    close = df["close"]
    return {
        "close": float(close.iloc[-1]),
        "ma20": float(ind.sma(close, p["ma_short"]).iloc[-1]),
        "ma60": float(ind.sma(close, p["ma_long"]).iloc[-1]),
        "atr14": float(ind.atr(df["high"], df["low"], close, p["atr_window"]).iloc[-1]),
        "vol_ratio_5_20": float(ind.vol_ratio(df["volume"], p["vol_short"],
                                              p["vol_long"]).iloc[-1]),
        "ret_1d": float(ind.pct_change_n(close, 1).iloc[-1]),
        "ret_5d": float(ind.pct_change_n(close, 5).iloc[-1]),
        "main_net_5d": None,     # 由资金流模块填充（P3 之后）
        "pe_pct_3y": None,       # 由估值模块填充（P3 之后）
        "regime_label": None,    # 由 Task 20 填充
    }
```

- [ ] **Step 4: 写 `stocklab/features/snapshot.py`**

```python
import hashlib
import json
from dataclasses import dataclass

from stocklab.features import registry


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def params_hash(params: dict) -> str:
    return hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FeatureSnapshot:
    code: str
    date: str
    feature_version: str
    feature_set: str
    core: dict
    payload: dict
    payload_hash: str
    params_hash: str
    data_version: str

    def db_row(self, now: str) -> dict:
        row = {c: self.core.get(c) for c in registry.CORE_COLUMNS}
        row.update({
            "code": self.code, "date": self.date,
            "feature_version": self.feature_version,
            "feature_set": self.feature_set,
            "json_payload": canonical_json(self.payload),
            "payload_hash": self.payload_hash,
            "params_hash": self.params_hash,
            "data_version": self.data_version,
            "created_at": now,
        })
        return row


def build_snapshot(code: str, date: str, bars, *, params: dict | None = None,
                   data_version: str = "", extra: dict | None = None,
                   feature_version: str = registry.FEATURE_VERSION
                   ) -> FeatureSnapshot | None:
    """构建快照。历史不足 MIN_HISTORY 时返回 None（调用方须记录缺失）。"""
    usable = [b for b in bars if b.date <= date]
    if len(usable) < registry.MIN_HISTORY:
        return None
    core = registry.compute_core(usable, params=params)
    payload = {**core, **(extra or {})}
    return FeatureSnapshot(
        code=code, date=date, feature_version=feature_version,
        feature_set=registry.FEATURE_SET, core=core, payload=payload,
        payload_hash=payload_hash(payload),
        params_hash=params_hash(params or registry.PARAMS),
        data_version=data_version,
    )


CORE_INSERT_SQL = """
INSERT INTO features_daily
 (code, date, feature_version, feature_set, close, ma20, ma60, atr14,
  vol_ratio_5_20, ret_1d, ret_5d, main_net_5d, pe_pct_3y, regime_label,
  json_payload, payload_hash, params_hash, data_version, created_at)
 VALUES (:code, :date, :feature_version, :feature_set, :close, :ma20, :ma60,
  :atr14, :vol_ratio_5_20, :ret_1d, :ret_5d, :main_net_5d, :pe_pct_3y,
  :regime_label, :json_payload, :payload_hash, :params_hash, :data_version,
  :created_at)
"""


def save_snapshot(conn, snap: FeatureSnapshot, *, now: str) -> int:
    cur = conn.execute(CORE_INSERT_SQL, snap.db_row(now))
    conn.commit()
    return int(cur.lastrowid)


def latest_snapshot_id(conn, code: str, date: str,
                       feature_version: str = registry.FEATURE_VERSION) -> int | None:
    row = conn.execute(
        "SELECT snapshot_id FROM features_daily WHERE code=? AND date=?"
        " AND feature_version=? ORDER BY snapshot_id DESC LIMIT 1",
        (code, date, feature_version),
    ).fetchone()
    return int(row["snapshot_id"]) if row else None
```

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_features_snapshot.py -q`
Expected: `8 passed`

- [ ] **Step 6: 提交**

```bash
git add stocklab/features/registry.py stocklab/features/snapshot.py tests/test_features_snapshot.py
git commit -m "feat(p3): 特征快照与稳定哈希（canonical JSON）"
```

---

### Task 19: Point-in-time 不变量测试（R2 的可执行证明）

**Files:**
- Create: `tests/test_features_pit.py`
- Modify: `stocklab/features/snapshot.py`（若测试暴露问题）

**Interfaces:**
- Consumes: `build_snapshot`（T18）
- Produces: 对 R2 的可执行证明 —— **本层最重要的测试**

- [ ] **Step 1: 写测试 `tests/test_features_pit.py`**

```python
"""R2 的可执行证明：T 日特征只能由 T 日及之前的数据决定。

核心手法：用「截至 T 的全量数据」与「截至 T 的数据 + 大量未来数据（含极端值）」
分别计算 T 日特征，二者必须完全一致。任何前视（lookahead）都会让这个测试变红。
"""
import pytest

from stocklab.data.models import Bar
from stocklab.features.snapshot import build_snapshot


def bars(n, start_close=10.0, step=0.1):
    out = []
    for i in range(n):
        c = start_close + i * step
        out.append(Bar(code="000333", date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                       open=c - 0.05, high=c + 0.15, low=c - 0.15, close=c,
                       volume=1000 + i, amount=(1000 + i) * c, turnover=1.0,
                       source="test"))
    return out


ASOF = "2026-03-20"


def test_future_data_does_not_change_past_features():
    past = [b for b in bars(80) if b.date <= ASOF]
    full = bars(80)
    assert past[-1].date == ASOF

    snap_past = build_snapshot("000333", ASOF, past)
    snap_full = build_snapshot("000333", ASOF, full)
    assert snap_past is not None
    assert snap_past.payload_hash == snap_full.payload_hash


def test_extreme_future_spike_does_not_leak():
    """未来暴涨 10 倍也不得改变 T 日特征 —— 这是 lookahead 最强的照妖镜。"""
    past = [b for b in bars(80) if b.date <= ASOF]
    future = [
        Bar(code="000333", date=f"2026-04-{d:02d}", open=1000.0, high=1100.0,
            low=900.0, close=1000.0, volume=10**7, amount=10**10, turnover=50.0,
            source="test")
        for d in range(1, 29)
    ]
    a = build_snapshot("000333", ASOF, past)
    b = build_snapshot("000333", ASOF, past + future)
    assert a.payload_hash == b.payload_hash


def test_future_low_does_not_change_past_atr():
    past = [b for b in bars(80) if b.date <= ASOF]
    future = [
        Bar(code="000333", date=f"2026-04-{d:02d}", open=0.01, high=0.02,
            low=0.01, close=0.01, volume=1, amount=0.01, turnover=0.0,
            source="test")
        for d in range(1, 29)
    ]
    a = build_snapshot("000333", ASOF, past)
    b = build_snapshot("000333", ASOF, past + future)
    assert a.payload_hash == b.payload_hash


def test_exact_asof_row_is_included():
    """asof 日当天的收盘数据必须参与计算（R2 允许用当日收盘）。"""
    past = [b for b in bars(80) if b.date <= ASOF]
    without_last = past[:-1]
    a = build_snapshot("000333", ASOF, past)
    b = build_snapshot("000333", ASOF, without_last)
    assert a.payload_hash != b.payload_hash


def test_bars_after_asof_are_ignored_even_if_unsorted():
    past = [b for b in bars(80) if b.date <= ASOF]
    future = bars(80)[-5:]
    a = build_snapshot("000333", ASOF, past)
    b = build_snapshot("000333", ASOF, future + past)   # 乱序输入
    assert a.payload_hash == b.payload_hash
```

- [ ] **Step 2: 运行**

Run: `.venv/bin/python -m pytest tests/test_features_pit.py -q`
Expected: `5 passed`

若 `test_bars_after_asof_are_ignored_even_if_unsorted` 失败 → `build_snapshot` 里 `usable` 未按日期排序。修复：把 `usable = [b for b in bars if b.date <= date]` 改为 `usable = sorted((b for b in bars if b.date <= date), key=lambda b: b.date)`。**这正是本测试存在的意义。**

- [ ] **Step 3: 提交**

```bash
git add tests/test_features_pit.py stocklab/features/snapshot.py
git commit -m "test(p3): point-in-time 不变量测试（R2 可执行化）"
```

---

### Task 20: 市场状态（regime）与快照落库命令

**Files:**
- Create: `stocklab/features/regime.py`
- Modify: `stocklab/cli/main.py`（新增 `features build` 子命令）
- Test: `tests/test_features_regime.py`, `tests/test_cli_features.py`

**Interfaces:**
- Produces:
  - `regime.classify(close, *, ma_short=20, ma_long=60, vol_window=20) -> pd.Series`，取值 `{"trend_up","trend_down","range","unknown"}`
  - `regime.build_market_state(conn, index_code, calendar, *, start, end, now) -> int`
  - CLI：`stocklab features build --date YYYY-MM-DD`

- [ ] **Step 1: 写失败测试 `tests/test_features_regime.py`**

```python
import pandas as pd
import pytest

from stocklab.features import regime


def test_uptrend_labeled():
    close = pd.Series([float(i) for i in range(1, 121)])   # 单调上升
    labels = regime.classify(close)
    assert labels.iloc[-1] == "trend_up"


def test_downtrend_labeled():
    close = pd.Series([float(i) for i in range(120, 0, -1)])
    labels = regime.classify(close)
    assert labels.iloc[-1] == "trend_down"


def test_flat_market_is_range():
    close = pd.Series([10.0 + (0.01 if i % 2 else -0.01) for i in range(120)])
    assert regime.classify(close).iloc[-1] == "range"


def test_insufficient_history_is_unknown():
    close = pd.Series([10.0] * 10)
    assert regime.classify(close).iloc[-1] == "unknown"


def test_no_lookahead():
    close = pd.Series([float(i) for i in range(1, 121)])
    before = regime.classify(close).iloc[80]
    after = regime.classify(pd.concat([close, pd.Series([999.0] * 10)],
                                      ignore_index=True)).iloc[80]
    assert before == after
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_features_regime.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.features.regime'`

- [ ] **Step 3: 写实现**

```python
"""市场状态判定（regime）。

v1 用最简单的规则：MA20/MA60 相对位置 + 均线斜率。
不要一上来就上机器学习（总纲第 7 节）。
"""
import numpy as np
import pandas as pd

from stocklab.features.indicators import sma

SLOPE_LOOKBACK = 5
FLAT_THRESHOLD = 0.005      # MA20 相对 MA60 偏离 < 0.5% 视为区间


def classify(close: pd.Series, *, ma_short: int = 20, ma_long: int = 60,
             vol_window: int = 20) -> pd.Series:
    _ = vol_window
    fast = sma(close, ma_short)
    slow = sma(close, ma_long)
    spread = (fast - slow) / slow
    slope = fast / fast.shift(SLOPE_LOOKBACK) - 1.0

    labels = pd.Series("unknown", index=close.index, dtype=object)
    ready = spread.notna() & slope.notna()
    up = ready & (spread > FLAT_THRESHOLD) & (slope > 0)
    down = ready & (spread < -FLAT_THRESHOLD) & (slope < 0)
    flat = ready & ~up & ~down
    labels[up] = "trend_up"
    labels[down] = "trend_down"
    labels[flat] = "range"
    return labels
```

- [ ] **Step 4: 写 `tests/test_cli_features.py`**

```python
from stocklab.cli.main import build_parser


def test_features_build_subcommand():
    args = build_parser().parse_args(["features", "build", "--date", "2026-09-14"])
    assert args.command == "features"
    assert args.date == "2026-09-14"
```

- [ ] **Step 5: 在 `stocklab/cli/main.py` 增加子命令**

```python
# 在 build_parser() 内追加：
    feat = sub.add_parser("features")
    feat.add_argument("feat_action", choices=["build"])
    feat.add_argument("--date", required=True)
    feat.add_argument("--code", action="append", default=None)
```

```python
# 新增函数：
def cmd_features_build(date: str, codes: list[str] | None) -> int:
    """为指定日期构建全部标的的特征快照（离线，只读 bars_daily）。"""
    import json as _json

    from stocklab.calendar.trading_calendar import Calendar
    from stocklab.data.models import Bar
    from stocklab.features import snapshot
    from stocklab.store.migrate import init_db

    paths.ensure_dirs()
    init_db(paths.DB_PATH)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    written, skipped = 0, []
    with connect(paths.DB_PATH) as conn:
        cal = Calendar.load(conn)
        rows = conn.execute(
            "SELECT code FROM instruments WHERE active=1 ORDER BY code").fetchall()
        for row in rows:
            code = row["code"]
            if codes and code not in codes:
                continue
            bars = [Bar(code=code, date=r["date"], open=r["open"], high=r["high"],
                        low=r["low"], close=r["close"], volume=r["volume"],
                        amount=r["amount"], turnover=r["turnover"],
                        source=r["source"], adj_mode=r["adj_mode"])
                    for r in conn.execute(
                        "SELECT * FROM bars_daily WHERE code=? AND date<=?"
                        " ORDER BY date", (code, date))]
            snap = snapshot.build_snapshot(code, date, bars,
                                           data_version=f"bars:{date}")
            if snap is None:
                skipped.append(code)
                repo.log_event(conn, "features", "warn",
                               f"{code} 历史不足，跳过特征快照",
                               context={"date": date}, now=now)
                continue
            snapshot.save_snapshot(conn, snap, now=now)
            written += 1
        _ = cal
    print(_json.dumps({"date": date, "written": written, "skipped": skipped},
                      ensure_ascii=False))
    return 0
```

```python
# 在 main() 内追加分支：
    if args.command == "features" and args.feat_action == "build":
        return cmd_features_build(args.date, args.code)
```

- [ ] **Step 6: 运行测试 + 全量回归**

```bash
.venv/bin/python -m pytest tests/test_features_regime.py tests/test_cli_features.py -q
.venv/bin/python -m pytest -q
```
Expected: 前者 `6 passed`；后者全绿。

- [ ] **Step 7: 端到端验证可复现性（R9）**

```bash
.venv/bin/python -m stocklab.cli.main features build --date 2026-09-11
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('data/stocklab.db'); c.row_factory = sqlite3.Row
for r in c.execute('SELECT code, date, payload_hash FROM features_daily ORDER BY code'):
    print(dict(r))
"
# 删掉刚写入的快照再重跑，hash 必须完全一致
```
Expected: 两次运行产出的 `payload_hash` **完全相同**。若不同 → 存在未固定的随机性或不稳定排序，必须修复后再进入 P4。

- [ ] **Step 8: 提交**

```bash
git add stocklab/features/regime.py stocklab/cli/main.py tests/test_features_regime.py tests/test_cli_features.py
git commit -m "feat(p3): 市场状态判定 + features build 命令 + 可复现性验证"
```

**P3 完成标志**：`pytest -q` 全绿；PIT 测试通过；同日期重算 hash 一致。

---

# P4：回测引擎

**P4 验收（DoD）**：对构造的已知序列能算出**手算一致**的收益；成本扣减有单测；涨跌停/停牌不可成交有测试；基准对比可产出；walk-forward 能切分且**无数据泄漏**（测试区间严格在训练区间之后）；模拟盘可重放。

---

### Task 21: 持仓推进（回测与模拟盘的公共内核）

**Files:**
- Create: `stocklab/backtest/__init__.py`, `stocklab/backtest/portfolio.py`
- Test: `tests/test_backtest_portfolio.py`

**Interfaces:**
- Produces:
  - `Position(code, qty, cost_price)`、`Trade(date, code, side, price, qty, fee, reason)`
  - `Portfolio(cash, positions: dict[str, Position], costs: CostModel)`
    - `.market_value(prices: dict[str,float]) -> float`
    - `.nav(prices) -> float`
    - `.buy(date, code, price, qty, reason) -> Trade | None`（现金不足返回 None）
    - `.sell(date, code, price, qty, reason) -> Trade | None`（持仓不足返回 None）
    - `.can_trade(code, bar) -> bool`（停牌 / 涨跌停检查）
  - `Portfolio.replay(initial_cash, trades, prices_by_date, costs) -> list[NavPoint]`（**重放校验**，评审 D1）

- [ ] **Step 1: 写失败测试 `tests/test_backtest_portfolio.py`**

```python
import pytest

from stocklab.backtest.portfolio import Portfolio, replay
from stocklab.config.costs import CostModel
from stocklab.data.models import Bar

CM = CostModel(slippage_bps=0.0, min_commission=0.0)
NOW = "2026-09-14"


def bar(close=10.0, high=None, low=None, vol=1000):
    return Bar(code="000333", date=NOW, open=close, high=high or close,
               low=low or close, close=close, volume=vol, amount=close * vol,
               turnover=1.0, source="test")


def test_buy_reduces_cash_and_adds_position():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    t = p.buy(NOW, "000333", 10.0, 1000, "test")
    assert t is not None
    assert p.cash == pytest.approx(100_000.0 - 10_000.0 - t.fee)
    assert p.positions["000333"].qty == 1000


def test_buy_rejected_when_cash_insufficient():
    p = Portfolio(cash=100.0, positions={}, costs=CM)
    assert p.buy(NOW, "000333", 10.0, 1000, "test") is None
    assert p.cash == pytest.approx(100.0)


def test_sell_increases_cash_and_removes_position():
    p = Portfolio(cash=0.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed") if p.cash >= 10_000 else None
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    cash_before = p.cash
    t = p.sell(NOW, "000333", 12.0, 1000, "exit")
    assert t is not None
    assert p.cash == pytest.approx(cash_before + 12_000.0 - t.fee)
    assert "000333" not in p.positions


def test_sell_rejected_when_position_insufficient():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    assert p.sell(NOW, "000333", 10.0, 100, "x") is None


def test_sell_rejected_when_t_plus_1_locked():
    """A 股 T+1：当日买入当日不可卖。"""
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    assert p.sell(NOW, "000333", 11.0, 1000, "same-day") is None


def test_nav_uses_market_prices():
    p = Portfolio(cash=0.0, positions={}, costs=CostModel(min_commission=0.0,
                                                          slippage_bps=0.0))
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    # 现金 90000，持仓 1000 股 @12 = 12000 → 102000
    assert p.nav({"000333": 12.0}) == pytest.approx(102_000.0)


def test_cannot_trade_suspended():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    assert p.can_trade("000333", bar(vol=0)) is False


def test_cannot_buy_at_limit_up():
    """涨停不可买入（买不到）。"""
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    b = Bar(code="000333", date=NOW, open=11.0, high=11.0, low=11.0, close=11.0,
            volume=1000, amount=11000.0, turnover=1.0, source="test")
    assert p.can_trade("000333", b, pre_close=10.0, board="main") is False


def test_cannot_sell_at_limit_down():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    b = Bar(code="000333", date=NOW, open=9.0, high=9.0, low=9.0, close=9.0,
            volume=1000, amount=9000.0, turnover=1.0, source="test")
    assert p.can_trade("000333", b, pre_close=10.0, board="main", side="sell") is False


def test_gem_board_limit_is_20_percent():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    b = Bar(code="300750", date=NOW, open=11.0, high=11.0, low=11.0, close=11.0,
            volume=1000, amount=11000.0, turnover=1.0, source="test")
    # 主板 10% 会判涨停，创业板 20% 不会
    assert p.can_trade("300750", b, pre_close=10.0, board="gem") is True


def test_replay_reproduces_nav_from_trades():
    """评审 D1：净值必须能由成交流水完整重放得到。"""
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    trades = [p.buy(NOW, "000333", 10.0, 1000, "seed")]
    navs = replay(100_000.0, trades, {NOW: {"000333": 12.0}}, CM)
    assert navs[-1].nav == pytest.approx(p.nav({"000333": 12.0}))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_backtest_portfolio.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
"""持仓推进内核。

回测（历史）与模拟盘（增量）共用本模块 —— 避免两套成交逻辑不一致（评审 D1）。
"""
from dataclasses import dataclass, field

from stocklab.config.costs import CostModel

LIMIT_BY_BOARD = {"main": 0.10, "gem": 0.20, "star": 0.20, "bse": 0.30}
LIMIT_TOLERANCE = 1e-6


@dataclass
class Position:
    code: str
    qty: int
    cost_price: float


@dataclass(frozen=True)
class Trade:
    date: str
    code: str
    side: str
    price: float
    qty: int
    fee: float
    reason: str = ""


@dataclass
class NavPoint:
    date: str
    nav: float


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    costs: CostModel = field(default_factory=CostModel)
    _bought_today: set[str] = field(default_factory=set, repr=False)

    # ---------- 交易约束 ----------
    def can_trade(self, code: str, bar, *, pre_close: float | None = None,
                  board: str = "main", side: str = "buy") -> bool:
        if bar.volume <= 0:
            return False
        if pre_close is None or pre_close <= 0:
            return True
        limit = LIMIT_BY_BOARD.get(board, 0.10)
        if side == "buy":
            return bar.close < pre_close * (1 + limit) - LIMIT_TOLERANCE
        return bar.close > pre_close * (1 - limit) + LIMIT_TOLERANCE

    # ---------- 成交 ----------
    def buy(self, date: str, code: str, ref_price: float, qty: int,
            reason: str = "") -> Trade | None:
        if qty <= 0:
            return None
        price = self.costs.fill_price("buy", ref_price)
        fee = self.costs.fees("buy", price, qty)
        total = price * qty + fee
        if total > self.cash:
            return None
        self.cash -= total
        pos = self.positions.get(code)
        if pos is None:
            self.positions[code] = Position(code, qty, (price * qty + fee) / qty)
        else:
            new_qty = pos.qty + qty
            old_cost = pos.cost_price * pos.qty
            pos.cost_price = (old_cost + price * qty + fee) / new_qty
            pos.qty = new_qty
        self._bought_today.add(code)
        return Trade(date, code, "buy", price, qty, fee, reason)

    def sell(self, date: str, code: str, ref_price: float, qty: int,
             reason: str = "") -> Trade | None:
        pos = self.positions.get(code)
        if pos is None or qty <= 0 or qty > pos.qty:
            return None
        if code in self._bought_today:
            return None                     # T+1
        price = self.costs.fill_price("sell", ref_price)
        fee = self.costs.fees("sell", price, qty)
        self.cash += price * qty - fee
        pos.qty -= qty
        if pos.qty == 0:
            del self.positions[code]
        return Trade(date, code, "sell", price, qty, fee, reason)

    def settle(self, date: str) -> None:
        """每个交易日结束时调用，解除 T+1 锁定。"""
        _ = date
        self._bought_today.clear()

    # ---------- 估值 ----------
    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p.qty * prices.get(c, 0.0) for c, p in self.positions.items())

    def nav(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)


def replay(initial_cash: float, trades, prices_by_date: dict[str, dict[str, float]],
           costs: CostModel) -> list[NavPoint]:
    """由成交流水重放净值（评审 D1 的可执行形式）。"""
    p = Portfolio(cash=initial_cash, positions={}, costs=costs)
    out: list[NavPoint] = []
    for t in trades:
        if t is None:
            continue
        if t.side == "buy":
            p.cash -= t.price * t.qty + t.fee
            pos = p.positions.get(t.code)
            if pos is None:
                p.positions[t.code] = Position(t.code, t.qty,
                                               (t.price * t.qty + t.fee) / t.qty)
            else:
                pos.qty += t.qty
        else:
            p.cash += t.price * t.qty - t.fee
            pos = p.positions.get(t.code)
            if pos:
                pos.qty -= t.qty
                if pos.qty == 0:
                    del p.positions[t.code]
        prices = prices_by_date.get(t.date, {})
        out.append(NavPoint(t.date, p.nav(prices)))
    return out
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_backtest_portfolio.py -q`
Expected: `12 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/backtest/ tests/test_backtest_portfolio.py
git commit -m "feat(p4): 持仓推进内核（T+1/涨跌停/停牌 + 净值重放）"
```

---

### Task 22: 事件驱动回测引擎

**Files:**
- Create: `stocklab/backtest/engine.py`
- Test: `tests/test_backtest_engine.py`

**Interfaces:**
- Produces:
  - `Strategy` 协议：`name: str`、`generate(date, history: dict[str, list[Bar]], features) -> dict[str, Signal]`
  - `Signal(action: str, size_pct: float, reason: str)`，`action ∈ {"buy","sell","hold"}`
  - `BacktestResult(nav_points, trades, metrics)`
  - `run_backtest(bars_by_code, strategy, *, start, end, initial_cash, costs, calendar, universe) -> BacktestResult`
  - **成交时点**：T 日收盘后出信号 → **T+1 日开盘价成交**（避免用 T 日收盘价成交的前视）

- [ ] **Step 1: 写失败测试 `tests/test_backtest_engine.py`**

```python
import pytest

from stocklab.backtest.engine import Signal, run_backtest
from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.costs import CostModel
from stocklab.data.models import Bar

DATES = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
CAL = Calendar.from_dates(DATES)
FREE = CostModel(slippage_bps=0.0, min_commission=0.0, commission_rate=0.0,
                 transfer_fee_rate=0.0, stamp_tax_rate=0.0)


def mk_bars(code, closes, opens=None):
    out = []
    for i, (d, c) in enumerate(zip(DATES, closes)):
        o = opens[i] if opens else c
        out.append(Bar(code=code, date=d, open=o, high=max(o, c), low=min(o, c),
                       close=c, volume=10_000, amount=c * 10_000, turnover=1.0,
                       source="test"))
    return out


class BuyFirstDayThenHold:
    name = "test_buy_hold"

    def generate(self, date, history, features):
        if date == "2026-09-01":
            return {"000333": Signal("buy", 100.0, "entry")}
        return {}


def test_buy_and_hold_return_matches_hand_calculation():
    """首日信号 → 次日开盘买入，持有到结束。

    开盘价序列 10,10,11,12；次日开盘(=09-02 开盘 10)买入，
    期末 09-04 收盘 12 → 收益 = 12/10 - 1 = 20%
    """
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0],
                              opens=[10.0, 10.0, 11.0, 12.0])}
    res = run_backtest(bars, BuyFirstDayThenHold(), start=DATES[0], end=DATES[-1],
                       initial_cash=100_000.0, costs=FREE, calendar=CAL,
                       universe=["000333"])
    assert res.nav_points[-1].nav == pytest.approx(120_000.0)
    assert len(res.trades) == 1
    assert res.trades[0].side == "buy"
    assert res.trades[0].date == "2026-09-02"
    assert res.trades[0].price == pytest.approx(10.0)


def test_costs_reduce_return():
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0],
                              opens=[10.0, 10.0, 11.0, 12.0])}
    real = CostModel()
    res = run_backtest(bars, BuyFirstDayThenHold(), start=DATES[0], end=DATES[-1],
                       initial_cash=100_000.0, costs=real, calendar=CAL,
                       universe=["000333"])
    assert res.nav_points[-1].nav < 120_000.0
    assert res.trades[0].fee > 0


def test_no_lookahead_signal_uses_next_open():
    """T 日信号绝不能用 T 日收盘价成交（这是回测作弊的头号来源）。"""
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0],
                              opens=[9.0, 10.0, 11.0, 12.0])}
    res = run_backtest(bars, BuyFirstDayThenHold(), start=DATES[0], end=DATES[-1],
                       initial_cash=100_000.0, costs=FREE, calendar=CAL,
                       universe=["000333"])
    assert res.trades[0].price == pytest.approx(10.0)   # 09-02 开盘，不是 09-01 的 9.0


def test_nav_points_cover_all_sessions():
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0])}
    res = run_backtest(bars, BuyFirstDayThenHold(), start=DATES[0], end=DATES[-1],
                       initial_cash=100_000.0, costs=FREE, calendar=CAL,
                       universe=["000333"])
    assert [p.date for p in res.nav_points] == DATES


def test_flat_price_keeps_nav_at_initial():
    bars = {"000333": mk_bars("000333", [10.0] * 4)}
    res = run_backtest(bars, BuyFirstDayThenHold(), start=DATES[0], end=DATES[-1],
                       initial_cash=100_000.0, costs=FREE, calendar=CAL,
                       universe=["000333"])
    assert res.nav_points[-1].nav == pytest.approx(100_000.0)


def test_missing_bars_for_a_day_does_not_crash():
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0])[:2]}
    res = run_backtest(bars, BuyFirstDayThenHold(), start=DATES[0], end=DATES[-1],
                       initial_cash=100_000.0, costs=FREE, calendar=CAL,
                       universe=["000333"])
    assert len(res.nav_points) == 4
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_backtest_engine.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
"""事件驱动回测引擎。

时点约定（关键，防前视）：
  T 日收盘 → 生成信号 → **T+1 日开盘价成交** → T+1 日收盘估值。
"""
from dataclasses import dataclass, field
from typing import Protocol

from stocklab.backtest.portfolio import Portfolio, Trade
from stocklab.config.costs import CostModel


@dataclass(frozen=True)
class Signal:
    action: str        # buy | sell | hold
    size_pct: float    # 目标仓位占净值百分比
    reason: str = ""


class Strategy(Protocol):
    name: str

    def generate(self, date: str, history: dict[str, list["Bar"]],
                 features: dict) -> dict[str, Signal]:
        ...


@dataclass
class BacktestResult:
    nav_points: list = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def run_backtest(bars_by_code: dict, strategy, *, start: str, end: str,
                 initial_cash: float, costs: CostModel, calendar,
                 universe: list[str]) -> BacktestResult:
    sessions = calendar.sessions(start, end)
    bars_by_date: dict[str, dict[str, object]] = {}
    for code, bars in bars_by_code.items():
        for b in bars:
            bars_by_date.setdefault(b.date, {})[code] = b

    portfolio = Portfolio(cash=initial_cash, positions={}, costs=costs)
    result = BacktestResult()
    pending: dict[str, Signal] = {}
    history: dict[str, list] = {c: [] for c in universe}

    for i, date in enumerate(sessions):
        todays = bars_by_date.get(date, {})

        # 1) 执行昨日信号（今日开盘价）
        for code, sig in pending.items():
            bar = todays.get(code)
            if bar is None:
                continue
            if sig.action == "buy":
                nav = portfolio.nav({c: b.close for c, b in todays.items()})
                target_value = nav * sig.size_pct / 100.0
                qty = int(target_value / bar.open / 100) * 100     # 整手
                if qty > 0 and portfolio.can_trade(code, bar, board="main"):
                    t = portfolio.buy(date, code, bar.open, qty, sig.reason)
                    if t:
                        result.trades.append(t)
            elif sig.action == "sell":
                pos = portfolio.positions.get(code)
                if pos and portfolio.can_trade(code, bar, board="main", side="sell"):
                    t = portfolio.sell(date, code, bar.open, pos.qty, sig.reason)
                    if t:
                        result.trades.append(t)
        pending = {}

        # 2) 收盘后更新历史并生成今日信号
        for code in universe:
            bar = todays.get(code)
            if bar is not None:
                history[code].append(bar)
        signals = strategy.generate(date, history, {}) if i < len(sessions) - 1 else {}
        pending = {c: s for c, s in (signals or {}).items() if s.action != "hold"}

        # 3) 收盘估值
        prices = {c: b.close for c, b in todays.items()}
        result.nav_points.append(_nav_point(date, portfolio.nav(prices)))

        portfolio.settle(date)

    result.metrics = compute_metrics(result.nav_points, initial_cash)
    return result


def _nav_point(date: str, nav: float):
    from stocklab.backtest.portfolio import NavPoint
    return NavPoint(date=date, nav=nav)


def compute_metrics(nav_points, initial_cash: float) -> dict:
    from stocklab.backtest.metrics import summarize
    return summarize(nav_points, initial_cash)
```

- [ ] **Step 4: 运行测试（会因 metrics 未实现而失败）**

Run: `.venv/bin/python -m pytest tests/test_backtest_engine.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'stocklab.backtest.metrics'`（下一步补）

---

### Task 23: 性能指标与基准对比

**Files:**
- Create: `stocklab/backtest/metrics.py`
- Test: `tests/test_backtest_metrics.py`

**Interfaces:**
- Produces:
  - `summarize(nav_points, initial_cash) -> dict`，键：`total_return`、`max_drawdown`、`volatility`、`sharpe`（无风险利率 0）、`n_sessions`
  - `excess_return(strategy_return, benchmark_return) -> float`
  - `buy_and_hold_nav(bars, initial_cash, costs) -> list[NavPoint]`（R5 的基准）
  - `win_rate(returns) -> float`

- [ ] **Step 1: 写失败测试 `tests/test_backtest_metrics.py`**

```python
import pytest

from stocklab.backtest.metrics import (buy_and_hold_nav, excess_return, summarize,
                                       win_rate)
from stocklab.backtest.portfolio import NavPoint
from stocklab.config.costs import CostModel
from stocklab.data.models import Bar

DATES = ["2026-09-01", "2026-09-02", "2026-09-03"]
FREE = CostModel(slippage_bps=0.0, min_commission=0.0, commission_rate=0.0,
                 transfer_fee_rate=0.0, stamp_tax_rate=0.0)


def test_total_return():
    navs = [NavPoint(DATES[0], 100.0), NavPoint(DATES[1], 110.0),
            NavPoint(DATES[2], 121.0)]
    m = summarize(navs, 100.0)
    assert m["total_return"] == pytest.approx(0.21)


def test_max_drawdown():
    navs = [NavPoint(DATES[0], 100.0), NavPoint(DATES[1], 120.0),
            NavPoint(DATES[2], 90.0)]
    m = summarize(navs, 100.0)
    assert m["max_drawdown"] == pytest.approx(-0.25)     # 90/120 - 1


def test_max_drawdown_zero_when_monotonic():
    navs = [NavPoint(DATES[0], 100.0), NavPoint(DATES[1], 110.0)]
    assert summarize(navs, 100.0)["max_drawdown"] == pytest.approx(0.0)


def test_excess_return():
    assert excess_return(0.10, 0.04) == pytest.approx(0.06)


def test_win_rate():
    assert win_rate([0.1, -0.05, 0.2, -0.01]) == pytest.approx(0.5)
    assert win_rate([]) == 0.0


def test_buy_and_hold_nav_without_costs():
    bars = [Bar(code="000333", date=d, open=c, high=c, low=c, close=c, volume=100,
                amount=c * 100, turnover=1.0, source="test")
            for d, c in zip(DATES, [10.0, 11.0, 12.0])]
    navs = buy_and_hold_nav(bars, 100_000.0, FREE)
    assert navs[-1].nav == pytest.approx(120_000.0)


def test_buy_and_hold_nav_with_costs_is_lower():
    bars = [Bar(code="000333", date=d, open=c, high=c, low=c, close=c, volume=100,
                amount=c * 100, turnover=1.0, source="test")
            for d, c in zip(DATES, [10.0, 11.0, 12.0])]
    navs = buy_and_hold_nav(bars, 100_000.0, CostModel())
    assert navs[-1].nav < 120_000.0


def test_summarize_empty_nav():
    assert summarize([], 100.0)["total_return"] == 0.0
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_backtest_metrics.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
import math

from stocklab.backtest.portfolio import NavPoint, Portfolio
from stocklab.config.costs import CostModel

TRADING_DAYS = 252


def summarize(nav_points: list[NavPoint], initial_cash: float) -> dict:
    if not nav_points:
        return {"total_return": 0.0, "max_drawdown": 0.0, "volatility": 0.0,
                "sharpe": 0.0, "n_sessions": 0}
    navs = [p.nav for p in nav_points]
    total_return = navs[-1] / initial_cash - 1.0

    peak = navs[0]
    max_dd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)

    rets = [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs))
            if navs[i - 1] > 0]
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
        sharpe = (mean * TRADING_DAYS) / vol if vol > 0 else 0.0
    else:
        vol, sharpe = 0.0, 0.0

    return {"total_return": total_return, "max_drawdown": max_dd,
            "volatility": vol, "sharpe": sharpe, "n_sessions": len(navs)}


def excess_return(strategy_return: float, benchmark_return: float) -> float:
    """R5：跑不赢躺平就没有存在价值。"""
    return strategy_return - benchmark_return


def win_rate(returns) -> float:
    rs = list(returns)
    if not rs:
        return 0.0
    return sum(1 for r in rs if r > 0) / len(rs)


def buy_and_hold_nav(bars, initial_cash: float, costs: CostModel) -> list[NavPoint]:
    """基准：首日开盘买入并持有到末日收盘（R5）。"""
    if not bars:
        return []
    first = bars[0]
    p = Portfolio(cash=initial_cash, positions={}, costs=costs)
    qty = int(initial_cash / (first.open * (1 + costs.slippage_bps / 10_000)) / 100) * 100
    if qty > 0:
        p.buy(first.date, first.code, first.open, qty, "buy_and_hold")
    return [NavPoint(b.date, p.nav({first.code: b.close})) for b in bars]
```

- [ ] **Step 4: 运行测试 + 全量回测测试**

```bash
.venv/bin/python -m pytest tests/test_backtest_metrics.py tests/test_backtest_engine.py -q
```
Expected: `16 passed`

- [ ] **Step 5: 提交**

```bash
git add stocklab/backtest/metrics.py tests/test_backtest_metrics.py
git commit -m "feat(p4): 性能指标与 buy_and_hold 基准（R5）"
```

---

### Task 24: Walk-forward 切分器

**Files:**
- Create: `stocklab/backtest/walkforward.py`
- Test: `tests/test_backtest_walkforward.py`

**Interfaces:**
- Produces:
  - `split_walk_forward(sessions: Sequence[str], *, train: int, test: int, step: int | None = None, embargo: int = 0) -> list[Fold]`
  - `Fold(index, train_start, train_end, test_start, test_end, train_dates, test_dates)`
  - 不变量：**test 区间严格在 train 区间之后**；任意两折的 test 不重叠；`embargo` 用于剔除训练/测试边界泄漏

- [ ] **Step 1: 写失败测试 `tests/test_backtest_walkforward.py`**

```python
import pytest

from stocklab.backtest.walkforward import split_walk_forward


def sessions(n=20):
    return [f"2026-{i:02d}" for i in range(1, n + 1)]


def test_basic_split():
    folds = split_walk_forward(sessions(20), train=5, test=3)
    assert len(folds) > 0
    f = folds[0]
    assert len(f.train_dates) == 5
    assert len(f.test_dates) == 3


def test_test_always_after_train():
    for f in split_walk_forward(sessions(20), train=5, test=3):
        assert max(f.train_dates) < min(f.test_dates)


def test_no_overlap_between_test_windows():
    folds = split_walk_forward(sessions(20), train=5, test=3)
    seen = set()
    for f in folds:
        assert not (seen & set(f.test_dates)), "测试窗口重叠 = 样本泄漏"
        seen |= set(f.test_dates)


def test_step_defaults_to_test_size():
    folds = split_walk_forward(sessions(20), train=5, test=3)
    assert folds[1].train_start == folds[0].train_start + 3 if len(folds) > 1 else True


def test_folds_are_contiguous():
    folds = split_walk_forward(sessions(20), train=5, test=3, step=3)
    for a, b in zip(folds, folds[1:]):
        assert b.train_start == a.train_start + 3


def test_insufficient_data_returns_empty():
    assert split_walk_forward(sessions(5), train=5, test=3) == []


def test_embargo_removes_boundary_dates():
    folds = split_walk_forward(sessions(20), train=5, test=3, embargo=1)
    for f in folds:
        # embargo 后 train 少一天，且 train_end 与 test_start 之间有间隔
        assert len(f.train_dates) == 4


def test_total_coverage_reasonable():
    folds = split_walk_forward(sessions(20), train=8, test=4)
    assert len(folds) == 3      # 8+4=12, +4=16, +4=20
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_backtest_walkforward.py -q`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
"""Walk-forward 切分（R7/R10 的可执行形式）。

不变量：
  1. 每一折的 test 区间严格晚于 train 区间（禁止全样本调参）
  2. 不同折的 test 区间不重叠（避免同一段数据被反复当测试集）
  3. embargo 用于剔除紧邻边界的样本，降低泄漏
"""
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Fold:
    index: int
    train_dates: tuple[str, ...]
    test_dates: tuple[str, ...]

    @property
    def train_start(self) -> str:
        return self.train_dates[0]

    @property
    def train_end(self) -> str:
        return self.train_dates[-1]

    @property
    def test_start(self) -> str:
        return self.test_dates[0]

    @property
    def test_end(self) -> str:
        return self.test_dates[-1]


def split_walk_forward(sessions: Sequence[str], *, train: int, test: int,
                       step: int | None = None, embargo: int = 0) -> list[Fold]:
    if train <= 0 or test <= 0:
        raise ValueError("train 与 test 必须为正整数")
    if embargo < 0 or embargo >= train:
        raise ValueError("embargo 必须在 [0, train) 内")
    step = step or test
    if step <= 0:
        raise ValueError("step 必须为正整数")

    dates = list(sessions)
    folds: list[Fold] = []
    start = 0
    idx = 0
    while start + train + test <= len(dates):
        train_slice = dates[start:start + train]
        if embargo:
            train_slice = train_slice[:-embargo]
        test_slice = dates[start + train:start + train + test]
        folds.append(Fold(idx, tuple(train_slice), tuple(test_slice)))
        idx += 1
        start += step
    return folds
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_backtest_walkforward.py -q`
Expected: `8 passed`

- [ ] **Step 5: 用真实数据跑一次（P4 端到端验收）**

新增 `stocklab/cli/main.py` 子命令 `backtest run`：

```python
def cmd_backtest_run(start: str, end: str, code: str | None) -> int:
    """用真实数据跑 buy_and_hold 基准回测 —— P4 的端到端验收。"""
    import json as _json

    from stocklab.backtest.engine import run_backtest
    from stocklab.backtest.metrics import buy_and_hold_nav, summarize
    from stocklab.backtest.portfolio import Portfolio
    from stocklab.calendar.trading_calendar import Calendar
    from stocklab.config.settings import load_settings
    from stocklab.data.models import Bar

    paths.ensure_dirs()
    init_db(paths.DB_PATH)
    settings = load_settings()
    with connect(paths.DB_PATH) as conn:
        cal = Calendar.load(conn)
        codes = [code] if code else [r["code"] for r in conn.execute(
            "SELECT code FROM instruments WHERE active=1")]
        out = {}
        for c in codes:
            bars = [Bar(code=c, date=r["date"], open=r["open"], high=r["high"],
                        low=r["low"], close=r["close"], volume=r["volume"],
                        amount=r["amount"], turnover=r["turnover"],
                        source=r["source"], adj_mode=r["adj_mode"])
                    for r in conn.execute(
                        "SELECT * FROM bars_daily WHERE code=? AND date BETWEEN ? AND ?"
                        " ORDER BY date", (c, start, end))]
            navs = buy_and_hold_nav(bars, 1_000_000.0, settings.costs)
            out[c] = summarize(navs, 1_000_000.0)
    print(_json.dumps(out, ensure_ascii=False, indent=2))
    return 0
```

```bash
.venv/bin/python -m stocklab.cli.main backtest run --start 2024-01-01 --end 2026-09-11
```
Expected: 输出每只标的的 `total_return` / `max_drawdown` / `sharpe` / `n_sessions`，且 `n_sessions` 与 `bars_daily` 行数一致。

- [ ] **Step 6: 提交**

```bash
git add stocklab/backtest/walkforward.py stocklab/cli/main.py tests/test_backtest_walkforward.py
git commit -m "feat(p4): walk-forward 切分器 + backtest run 端到端验收"
```

---

### Task 25: 全量回归与交付验证

- [ ] **Step 1: 跑全量测试**

```bash
.venv/bin/python -m pytest -q
```
Expected: 全部通过，0 failed。**把完整输出贴进交付报告。**

- [ ] **Step 2: 跑项目验证脚本**

```bash
bash scripts/verify.sh
```
Expected: `✅ 验证通过`。若第 4 步报「未发现测试命令」，把 `scripts/verify.sh` 的 Python 分支改为优先用 `.venv/bin/python -m pytest -q`（**不要用 `python3`** —— 系统 python3 不可用）。

- [ ] **Step 3: 端到端串联验证**

```bash
.venv/bin/python -m stocklab.cli.main doctor
.venv/bin/python -m stocklab.cli.main features build --date 2026-09-11
.venv/bin/python -m stocklab.cli.main backtest run --start 2024-01-01 --end 2026-09-11
```
Expected: 三条命令均退出码 0；`doctor` 显示 `bars_daily`、`features_daily` 均 > 0。

- [ ] **Step 4: 更新文档**

- `docs/architecture/`：写入分层图与依赖方向
- `docs/decisions/`：ADR-001（append-only 例外）、ADR-002（长历史源）
- `docs/errors/ERROR_DIARY.md`：把本阶段踩到的坑逐条写入

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "chore(p4): P1-P4 全量回归与文档同步"
```

---

## 自检（Self-Review）

**Spec 覆盖检查**

| 总纲要求 | 覆盖任务 | 状态 |
|---|---|---|
| R1 预测可证伪 | P6 范围，本计划不含 | ➖ 计划外 |
| R2 point-in-time | Task 19 | ✅ |
| R3 复权规则 | Task 12 ADR-002 + `bars_daily.adj_mode` | ⚠️ **依赖 B1 决策** |
| R4 成本内建 | Task 3 | ✅ |
| R5 必须有基准 | Task 23 | ✅ |
| R6 样本量纪律 | 本计划不涉及统计；门槛口径需在 P7 落地 | ⚠️ 见评审 C1 |
| R7 自由度限制 | Task 24（walk-forward）部分覆盖 | ⚠️ 需 P5 补「策略注册参数个数断言」 |
| R8 append-only | Task 4/6 | ✅（含 ADR-001 例外） |
| R9 可复现 | Task 11/18/20 | ✅ |
| R10 失败留痕 | Task 14/15 | ✅ |
| R11 交易日历 | Task 7 | ✅ |
| R12 变更留痕 | `decisions` 表已建（Task 4） | ➖ P5+ 使用 |
| R13 只读公网源 | Task 2/8 | ✅ |
| R14 合规 | P10 范围 | ➖ 计划外 |

**已知缺口（不掩盖）**

1. `main_net_5d` 与 `pe_pct_3y` 两个核心特征当前为 `None` 占位 —— 资金流/估值采集在 P2 有表但未接入特征，需在 P3 之后单独补一个 Task。
2. `sim_portfolio` 表已建但本计划未产出写入逻辑（模拟盘属 P9）。
3. `R6/R7` 的可执行化（策略参数个数断言、按日聚类的统计）留给 P5/P7。
4. Task 22 的 `board` 参数目前硬编码 `"main"`，接入创业板/科创板标的时必须从 `instruments.board` 取，否则涨跌停判定错误。

---

## 执行方式

计划已落盘至 `docs/plans/2026-09-14-phase1-4-implementation.md`。两种执行方式：

1. **Subagent-Driven（推荐）** —— 每个 Task 派一个全新的 subagent，Task 之间做两段式复查，迭代快、上下文干净
2. **Inline Execution** —— 在本会话内用 executing-plans 批量执行，带检查点

**但在开工前，必须先解决 Global Constraints 里的三个阻塞决策（B1/B2/A2）。**
