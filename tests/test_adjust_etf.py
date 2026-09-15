"""P17 / T2：ETF 复权口径 —— 读取层**硬拒绝**（ADR-008）。

## 为什么这一组测试是「拒绝」而不是「算对」

探针实测（`docs/plans/2026-09-15-p17-*` §0）：
- ETF 的分红除息**真实存在**（510300 的 qfq 与不复权在 2023-06-01 差 7.4%，
  799 个交易日里 639 天不同）；
- 但腾讯 `fqkline` 对这四只 ETF **一条事件行都不返回**（2000 根 K 线内 0 条）。

事件不可见 → 枚举不出 `cqr` → 连 `adj_factor_blackout`（需要 `cqr`）都填不出来
→ 因子链**无法被证明完整**。此时写 `factor = 1.0` 就是拿未复权价冒充复权价，
跨界收益会凭空多出一段假跌幅。所以唯一诚实的动作是**拒绝服务**。

这组测试钉住三件事：
1. 读取层的两个入口都对 ETF 抛 `EtfChainUnsupported`；
2. 连绕开读取层、直接调 `repo.insert_adj_factors` 也写不进 ETF 的因子行
   （结构性防线，不是约定）；
3. **反证**：股票的行为一字不变 —— 拒绝机制不能误伤正常路径。
"""

import pytest

from stocklab.config.universe import Instrument
from stocklab.data import adjust
from stocklab.data.models import Bar, CorpAction
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T18:00:00+08:00"
ETF = "510300"
STOCK = "000333"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES (?,?,?,?,?,?)", (STOCK, "美的集团", "sz", "main", "stock", NOW))
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES (?,?,?,?,?,?)", (ETF, "沪深300ETF", "sh", "main", "etf", NOW))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(code, d, 10.0, 10.0, 10.0, 10.0, 1000, "none", "tencent", NOW)
         for code in (STOCK, ETF) for d in ("2026-09-11", "2026-09-14")])
    c.commit()
    yield c
    c.close()


def _chain_of_two_days():
    bars = [Bar(code=ETF, date=d, open=10.0, high=10.0, low=10.0, close=10.0,
                volume=1000, amount=10_000.0, turnover=1.0, source="tencent")
            for d in ("2026-09-11", "2026-09-14")]
    return adjust.build_chain(bars, [])


# ---------- ① 读取层两个入口都拒绝 ----------

def test_load_chain_refuses_etf(conn):
    with pytest.raises(adjust.EtfChainUnsupported) as exc:
        adjust.load_chain(conn, ETF)
    msg = str(exc.value)
    assert ETF in msg and "ADR-008" in msg
    assert "无法" in msg                       # 说明「为什么不能算」，不只是「不支持」


def test_load_bars_adjusted_refuses_etf(conn):
    with pytest.raises(adjust.EtfChainUnsupported):
        adjust.load_bars_adjusted(conn, ETF, "2026-09-14")


def test_assert_adjustable_refuses_etf_and_unknown(conn):
    with pytest.raises(adjust.EtfChainUnsupported):
        adjust.assert_adjustable(conn, ETF)
    with pytest.raises(adjust.EtfChainUnsupported):
        adjust.assert_adjustable(conn, "999999")     # 未登记 → 口径未知，不猜成股票
    adjust.assert_adjustable(conn, STOCK)            # 股票通过（不抛）


def test_adjustable_types_is_a_whitelist():
    """白名单语义：将来新增标的类型默认被拒，而不是默认被放行。"""
    assert adjust.ADJUSTABLE_TYPES == frozenset({"stock"})
    assert "etf" not in adjust.ADJUSTABLE_TYPES


# ---------- ② 结构性防线：连 repo 也写不进 ETF 因子行 ----------

def test_etf_never_gets_factor_rows(conn):
    with pytest.raises(ValueError) as exc:
        repo.insert_adj_factors(conn, ETF, _chain_of_two_days(), source="test", now=NOW)
    assert ETF in str(exc.value)
    n = conn.execute("SELECT COUNT(*) FROM adj_factors WHERE code=?", (ETF,)).fetchone()[0]
    assert n == 0, "ETF 一行因子都不许落 —— 全 1 的链就是「未复权冒充复权」"


def test_etf_never_gets_blackout_rows(conn):
    """blackout 也不能被写：`cqr` 都枚举不出来，写进去就是编造出来的缺口记录。"""
    with pytest.raises(ValueError):
        repo.insert_adj_factors(conn, ETF, _chain_of_two_days(), source="test", now=NOW)
    n = conn.execute("SELECT COUNT(*) FROM adj_factor_blackout WHERE code=?",
                     (ETF,)).fetchone()[0]
    assert n == 0


# ---------- ③ 反证：股票路径不受影响 ----------

def test_stock_path_still_works(conn):
    adjust.assert_adjustable(conn, STOCK)
    bars, chain = adjust.load_chain(conn, STOCK)
    assert len(bars) == 2
    assert chain.factors == {"2026-09-11": 1.0, "2026-09-14": 1.0}   # 无事件 → 全 1
    n = repo.insert_adj_factors(conn, STOCK, chain, source="test", now=NOW)
    assert n == 2
    out = adjust.load_bars_adjusted(conn, STOCK, "2026-09-14")
    assert [b.adj_mode for b in out] == ["qfq", "qfq"]


def test_stock_with_real_event_still_adjusts(conn):
    """带真实分红事件的股票仍然算得出复权价（拒绝机制不能误伤）。"""
    conn.execute(
        "INSERT INTO corp_actions (code, cqr, djr, fh_sh, content, source,"
        " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
        (STOCK, "2026-09-14", "2026-09-15", 5.0, "10派5元", "tencent", NOW, NOW))
    conn.commit()
    out = adjust.load_bars_adjusted(conn, STOCK, "2026-09-14")
    assert out[-1].close == pytest.approx(10.0)
    assert out[0].close < 10.0            # 除权日之前被按比例缩小
