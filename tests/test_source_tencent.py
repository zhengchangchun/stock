"""Task 9：腾讯行情适配器（快照 + 日K + 除权事件）。

全部基于 `scripts/record_tencent_fixtures.py` 录下的**真实响应**离线回放，
不联网（铁律②）；fixture 的 SHA256 在 `load_fixture` 里校验，手改即报错。

合并了计划里的 test_source_tencent_quote.py：现在同时覆盖日K与除权事件。
"""

import json

import pytest

from stocklab.config.paths import FIXTURE_DIR
from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.data.raw_cache import load_fixture
from stocklab.data.sources import tencent

QUOTE_FIXTURE = "tencent_quote_sz000333_sh600690"
KLINE_FIXTURE = "tencent_fqkline_day_sz000333"

# ADR-001 §1 的独立探针记录了这 5 个除权日；本项目录到的窗口（2024-01-22 起）
# 覆盖其中 4 个，另 1 个（2023-06-01）在窗口之外。两条独立探针互相印证。
ADR001_CQR = ["2023-06-01", "2024-05-15", "2025-06-12", "2025-11-18", "2026-06-29"]


@pytest.fixture(scope="module")
def quote_text() -> str:
    body, _ = load_fixture(FIXTURE_DIR, QUOTE_FIXTURE)
    return body.decode("gbk")


@pytest.fixture(scope="module")
def quote_meta() -> dict:
    _, meta = load_fixture(FIXTURE_DIR, QUOTE_FIXTURE)
    return meta


@pytest.fixture(scope="module")
def raw_quote_fields(quote_text) -> dict[str, list[str]]:
    """从真实文本里取出每行的字段表，供「期望值」与解析结果对照（不写死数字）。"""
    out = {}
    for line in quote_text.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        parts = line.split("=", 1)[1].strip().strip('"').split("~")
        out[parts[2]] = parts
    return out


@pytest.fixture(scope="module")
def kline_payload() -> dict:
    body, _ = load_fixture(FIXTURE_DIR, KLINE_FIXTURE)
    return json.loads(body.decode("utf-8"))


@pytest.fixture(scope="module")
def kline_meta() -> dict:
    _, meta = load_fixture(FIXTURE_DIR, KLINE_FIXTURE)
    return meta


# ---------- fixture 可信度 ----------

def test_quote_fixture_is_real_gbk_tencent_payload(quote_text, quote_meta):
    assert quote_meta["url"] == "https://qt.gtimg.cn/q=sz000333,sh600690"
    assert quote_meta["encoding"] == "gbk"
    assert quote_text.split(";")[0].strip().startswith("v_sz000333=")
    # 真实腾讯快照有 80+ 个字段；计划的合成样例只有 35 个（见 docs/tasks）
    assert len(quote_text.split("~")) > 80


def test_gbk_is_required_for_names(quote_meta):
    """证明 GBK 不是可选优化：按 UTF-8 解码拿不到中文名。"""
    body, _ = load_fixture(FIXTURE_DIR, QUOTE_FIXTURE)
    assert "美的集团" not in body.decode("utf-8", errors="replace")
    assert "美的集团" in body.decode("gbk")


# ---------- 快照解析 ----------

def test_parse_quote(quote_text, quote_meta):
    quotes = tencent.parse_quote(quote_text)
    assert [q.code for q in quotes] == ["000333", "600690"]
    assert [q.name for q in quotes] == ["美的集团", "海尔智家"]
    assert quote_meta["n_quotes"] == len(quotes) == 2


def test_quote_fields_match_raw_text(quote_text, raw_quote_fields):
    """逐字段对照原始文本，避免把索引写错却自洽通过。"""
    for q in tencent.parse_quote(quote_text):
        parts = raw_quote_fields[q.code]
        assert q.price == pytest.approx(float(parts[3]))
        assert q.pre_close == pytest.approx(float(parts[4]))
        assert q.open == pytest.approx(float(parts[5]))
        assert q.high == pytest.approx(float(parts[33]))
        assert q.low == pytest.approx(float(parts[34]))
        assert q.turnover == pytest.approx(float(parts[38]))
        assert q.pe_ttm == pytest.approx(float(parts[39]))
        assert q.pb == pytest.approx(float(parts[46]))
        assert q.ts == parts[30]


def test_quote_volume_converted_from_lots_to_shares(quote_text, raw_quote_fields):
    """接口量单位是「手」，落库统一为「股」（铁律④）。"""
    for q in tencent.parse_quote(quote_text):
        assert q.volume == int(float(raw_quote_fields[q.code][36]) * 100)


def test_quote_amount_converted_from_wan_to_yuan(quote_text, raw_quote_fields):
    for q in tencent.parse_quote(quote_text):
        assert q.amount == pytest.approx(float(raw_quote_fields[q.code][37]) * 10000)


def test_quote_market_cap_converted_from_yi_to_yuan(quote_text, raw_quote_fields):
    for q in tencent.parse_quote(quote_text):
        assert q.float_mv == pytest.approx(float(raw_quote_fields[q.code][44]) * 1e8)
        assert q.total_mv == pytest.approx(float(raw_quote_fields[q.code][45]) * 1e8)


def test_quote_amount_consistent_with_price_times_volume(quote_text):
    """单位自洽断言（评审 C3）：amount ≈ price × volume，容差 1%。"""
    for q in tencent.parse_quote(quote_text):
        implied = q.price * q.volume
        assert abs(q.amount - implied) / implied < 0.01


def test_quote_timestamp_is_parseable(quote_text):
    for q in tencent.parse_quote(quote_text):
        assert len(q.ts) == 14 and q.ts.isdigit()
        assert q.ts.startswith("20260914")      # 录制当日


def test_parse_empty_text_returns_empty_list():
    assert tencent.parse_quote("") == []


def test_parse_malformed_line_is_skipped_not_crashed(quote_text):
    """单条坏数据不能让整批失败（R10：由调用方记录）。"""
    quotes = tencent.parse_quote('v_sz000333="garbage";' + quote_text)
    assert len(quotes) == 2


def test_parse_skips_line_with_too_few_fields(quote_text):
    short = "v_sz000333=\"" + "~".join(["0"] * 10) + "\";"
    assert tencent.parse_quote(short + quote_text) != []
    assert len(tencent.parse_quote(short)) == 0


def test_parse_skips_line_when_lhs_code_disagrees(quote_text):
    """`v_sz000333` 与字段 2 必须指向同一只股票，否则整行可能已错位。"""
    tampered = quote_text.replace("~000333~", "~999999~", 1)   # 只改字段 2
    assert [q.code for q in tencent.parse_quote(tampered)] == ["600690"]


def test_market_prefix_is_not_authoritative(quote_text):
    """lhs 的市场前缀只作一致性校验：股票身份以字段 2 为准。

    故 `v_sz000333=` 被改成 `v_sh000333=` 时仍解析出 000333 ——
    这是**刻意**行为（沪深同代码的极端情况下以字段 2 为准）。
    """
    tampered = quote_text.replace("v_sz000333=", "v_sh000333=", 1)
    assert [q.code for q in tencent.parse_quote(tampered)] == ["000333", "600690"]


# ---------- 日K 解析 ----------

def test_kline_url_matches_recorded_fixture_url(kline_meta):
    """URL 构造器必须与真实录制用的 URL 完全一致（fixture 元数据是真源）。"""
    assert tencent.kline_url("sz000333", 900, adj="qfq") == kline_meta["url"]


def test_kline_url_defaults_to_unadjusted():
    """铁律①：默认请求**不复权**序列，复权价绝不由抓取层下发。"""
    url = tencent.kline_url("sz000333", 320)
    assert url.endswith("sz000333,day,,,320,")
    assert "qfq" not in url and "hfq" not in url


def test_kline_url_uses_allowed_host():
    url = tencent.kline_url("sz000333", 320)
    assert url.startswith("https://web.ifzq.gtimg.cn/")
    assert "320" in url


def test_parse_kline_row_order_is_open_close_high_low(kline_payload, kline_meta):
    """关键坑：腾讯日K 每行是 [日期,开,收,最高,最低,量]，不是 OHLC。"""
    bars = tencent.parse_kline(kline_payload, "000333", adj_mode="qfq")
    rows = kline_payload["data"]["sz000333"]["qfqday"]
    assert len(bars) == len(rows) == kline_meta["n_bars"]
    first_row, first_bar = rows[0], bars[0]
    assert first_bar.date == first_row[0] == kline_meta["first_date"]
    assert first_bar.open == pytest.approx(float(first_row[1]))
    assert first_bar.close == pytest.approx(float(first_row[2]))   # 第 2 列是收盘
    assert first_bar.high == pytest.approx(float(first_row[3]))
    assert first_bar.low == pytest.approx(float(first_row[4]))
    assert first_bar.volume == int(float(first_row[5]) * 100)      # 手 → 股


def test_parse_kline_ohlc_invariants_hold_on_real_data(kline_payload):
    """真实 641 根全部满足 low ≤ min(开,收) ≤ max(开,收) ≤ high（实测 0 违例）。"""
    bars = tencent.parse_kline(kline_payload, "000333", adj_mode="qfq")
    for b in bars:
        assert b.low <= min(b.open, b.close)
        assert max(b.open, b.close) <= b.high
        assert b.volume > 0
        assert b.low > 0


def test_parse_kline_dates_are_sorted_unique(kline_payload):
    bars = tencent.parse_kline(kline_payload, "000333", adj_mode="qfq")
    dates = [b.date for b in bars]
    assert dates == sorted(dates)
    assert len(set(dates)) == len(dates)


def test_parse_kline_defaults_to_unadjusted():
    """铁律①：默认口径是不复权，标 none。"""
    payload = {"data": {"sz000333": {"day": [
        ["2026-09-14", "10.50", "10.30", "10.60", "10.20", "234567"],
    ]}}}
    bars = tencent.parse_kline(payload, "000333")
    assert len(bars) == 1 and bars[0].adj_mode == "none"


def test_parse_kline_never_mislabels_qfq_as_unadjusted(kline_payload):
    """口径不匹配时返回空，**不得**把 qfq 价格贴成 adj_mode="none"。

    这是静默降级：下游会拿到「贴了不复权标签的复权价」，比拿不到数据危险得多。
    """
    assert tencent.parse_kline(kline_payload, "000333") == []
    assert tencent.parse_kline(kline_payload, "000333", adj_mode="qfq") != []


def test_parse_kline_never_mislabels_day_as_qfq():
    payload = {"data": {"sz000333": {"day": [
        ["2026-09-14", "10.50", "10.30", "10.60", "10.20", "234567"],
    ]}}}
    assert tencent.parse_kline(payload, "000333", adj_mode="qfq") == []


def test_parse_kline_missing_key_returns_empty():
    assert tencent.parse_kline({"data": {}}, "000333", adj_mode="qfq") == []
    assert tencent.parse_kline({}, "000333", adj_mode="qfq") == []


def test_parse_kline_skips_malformed_row():
    payload = {"data": {"sz000333": {"qfqday": [
        ["2026-09-11", "10.00"],                       # 残缺行
        ["2026-09-14", "10.50", "10.30", "10.60", "10.20", "234567"],
    ]}}}
    bars = tencent.parse_kline(payload, "000333", adj_mode="qfq")
    assert len(bars) == 1
    assert bars[0].date == "2026-09-14"


def test_parse_kline_shanghai_code_uses_sh_key():
    payload = {"data": {"sh600690": {"day": [
        ["2026-09-14", "21.00", "21.12", "21.30", "20.90", "230458"],
    ]}}}
    bars = tencent.parse_kline(payload, "600690")
    assert len(bars) == 1 and bars[0].code == "600690"


def test_parse_kline_source_and_units(kline_payload):
    b = tencent.parse_kline(kline_payload, "000333", adj_mode="qfq")[0]
    assert b.source == "tencent"
    assert isinstance(b.volume, int)
    assert DEFAULT_UNIVERSE[0].tencent_code == "sz000333"   # 与 universe 约定一致


# ---------- 除权事件（ADR-001 D-01 的 PIT 地基） ----------

def test_parse_corp_actions_from_real_fixture(kline_payload, kline_meta):
    """旧 fixture 只有 `qfqday` 节点：显式指定 qfq 口径仍应解析出同一批事件。

    ADR-004 起默认口径改为 `none`（不复权流也带事件，且不受 800 根上限限制），
    故此处**显式**传 `adj_mode="qfq"` 以继续覆盖这条历史路径。
    """
    actions = tencent.parse_corp_actions(kline_payload, "000333", adj_mode="qfq")
    assert len(actions) == kline_meta["n_events"] == 4
    assert [a.cqr for a in actions] == [
        d for d in ADR001_CQR if d >= kline_meta["first_date"]
    ]
    assert actions[0].fh_sh == pytest.approx(30.0)
    assert actions[0].djr == "2024-05-14"
    assert actions[0].content == "10派30元"
    assert actions[0].code == "000333"


def test_corp_action_rows_carry_the_event_dict(kline_payload):
    """事件是 kline 行上的第 7 个元素（dict）—— 不是独立的顶层字段。"""
    rows = kline_payload["data"]["sz000333"]["qfqday"]
    with_event = [r for r in rows if len(r) > 6 and isinstance(r[6], dict)]
    assert [r[6]["cqr"] for r in with_event] == [
        a.cqr for a in tencent.parse_corp_actions(kline_payload, "000333",
                                                  adj_mode="qfq")]


def test_corp_action_cqr_is_a_real_bar_date(kline_payload):
    """除权日必须落在日K 序列里，否则事件无法与行情对齐。"""
    dates = {r[0] for r in kline_payload["data"]["sz000333"]["qfqday"]}
    assert all(a.cqr in dates for a in tencent.parse_corp_actions(
        kline_payload, "000333", adj_mode="qfq"))


def test_corp_action_never_has_null_factor_inputs(kline_payload):
    """除权日与登记日必须有值；`fh_sh` 允许缺失（送转-only 事件源站就是空串）。"""
    for a in tencent.parse_corp_actions(kline_payload, "000333", adj_mode="qfq"):
        assert a.fh_sh is not None and a.fh_sh >= 0
        assert a.cqr and a.djr


def test_parse_corp_actions_empty_returns_empty_list():
    assert tencent.parse_corp_actions({"data": {}}, "000333") == []
    assert tencent.parse_corp_actions({}, "000333") == []


def test_parse_corp_actions_requires_cqr_only():
    """事件的存在性**只以 `cqr` 判定**：缺 `fh_sh` 不得丢弃（ADR-004）。

    改为「要求 fh_sh 非空」的旧实现，会把送转-only 事件整条丢掉 ——
    600690 实测被丢 9/36 条（含 `10送3股`），复权链随即失效。
    """
    payload = {"data": {"sz000333": {"day": [
        ["2026-09-11", "10.00", "10.50", "10.80", "9.90", "123", {"cqr": "2026-09-11"}],
        ["2026-09-14", "10.50", "10.30", "10.60", "10.20", "234",
         {"cqr": "2026-09-14", "djr": "2026-09-13", "fh_sh": "5", "FHcontent": "10派5元"}],
    ]}}}
    actions = tencent.parse_corp_actions(payload, "000333")
    assert [a.cqr for a in actions] == ["2026-09-11", "2026-09-14"]
    assert actions[0].fh_sh is None                       # 缺失如实为 None
    assert actions[0].content == ""                       # 原文缺失也不编造


def test_parse_corp_actions_skips_rows_without_cqr():
    """真正该跳过的畸形行：事件 dict 里没有 `cqr`（无法定位除权日）。"""
    payload = {"data": {"sz000333": {"day": [
        ["2026-09-14", "10.50", "10.30", "10.60", "10.20", "234", {"fh_sh": "5"}],
        ["2026-09-15", "10.50", "10.30", "10.60", "10.20", "234", "not-a-dict"],
        ["2026-09-16", "10.50", "10.30", "10.60", "10.20", "234"],
    ]}}}
    assert tencent.parse_corp_actions(payload, "000333") == []


# ---------- ADR-004：真实**不复权**响应里的事件行（探针录下的原始响应） ----------

BFQ_FIXTURE = "tencent_fqkline_bfq_sz000333"
BFQ_OLD_FIXTURE = "tencent_fqkline_bfq_sh600690_old"


@pytest.fixture(scope="module")
def bfq_payload() -> dict:
    body, _ = load_fixture(FIXTURE_DIR, BFQ_FIXTURE)
    return json.loads(body.decode("utf-8"))


@pytest.fixture(scope="module")
def bfq_old_payload() -> dict:
    body, _ = load_fixture(FIXTURE_DIR, BFQ_OLD_FIXTURE)
    return json.loads(body.decode("utf-8"))


def test_bfq_stream_carries_events(bfq_payload):
    """ADR-004 P1：**不复权**响应同样带事件行，条数与 qfq 口径一致。

    这是「事件采集不继承 qfq 800 根上限」的前提 ——
    该 fixture 本身就是一个 2000 根的 `day` 节点，请求量是 qfq 路径的 1/2~1/3。
    """
    node = bfq_payload["data"]["sz000333"]
    assert "day" in node and "qfqday" not in node      # 确认这是不复权响应
    rows = node["day"]
    with_event = [r for r in rows if len(r) > 6 and isinstance(r[6], dict)]
    actions = tencent.parse_corp_actions(bfq_payload, "000333")
    assert len(rows) == 2000                           # 不复权单次上限
    assert len(with_event) == len(actions) == 10
    assert [r[6]["cqr"] for r in with_event] == [a.cqr for a in actions]


def test_bfq_events_have_all_terms_for_the_chain(bfq_payload):
    """事件必须带 `cqr` 与原文（`FHcontent`），否则因子链无法计算条款。"""
    for a in tencent.parse_corp_actions(bfq_payload, "000333"):
        assert a.cqr and a.djr
        assert a.content.startswith("10")              # 形如 "10派35元"
        assert "派" in a.content


def test_old_events_without_fh_sh_are_not_dropped(bfq_old_payload):
    """ADR-004 的核心回归：送转-only 事件的 `fh_sh` 是**空串**，不得被丢掉。

    改为「要求 fh_sh 非空」的旧实现，在 600690 上会静默丢弃 9/36 条事件
    （实测，见 ADR-004 证据 JSON），本用例锁死这个失效模式。
    """
    actions = tencent.parse_corp_actions(bfq_old_payload, "600690")
    blank = [a for a in actions if a.fh_sh is None]
    assert blank, "该 fixture 必须包含 fh_sh 缺失的事件，否则本测试没有鉴别力"
    # 缺 fh_sh 的事件仍然带原文/除权日 → 条款仍可解析
    assert all(a.cqr for a in actions)
    assert any(a.fh_sh is None and a.content for a in actions), \
        "既有缺 fh_sh 又有原文的事件（如 10送3股）必须保留"
    assert any(a.fh_sh is None and not a.content for a in actions), \
        "既缺 fh_sh 又缺原文的事件同样不得静默丢弃 —— 交给链层显式报不可定价"
