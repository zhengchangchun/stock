"""Task 11：原始响应缓存与 fixture 录制（可复现性地基，评审 B3/C5）。

铁律②：真实响应落盘 → 测试与复现模式一律回放，不重新联网。
"""

import json

import pytest

from stocklab.data.errors import CacheCorrupt
from stocklab.data.raw_cache import (
    RawCache,
    load_fixture,
    record_fixture,
    sha256_bytes,
)

KEY = "sz000333:320:qfq"
URL = "https://web.ifzq.gtimg.cn/x"


# ---------- 哈希 ----------

def test_sha256_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")
    assert sha256_bytes(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


# ---------- 往返 ----------

def test_cache_roundtrip(tmp_path):
    c = RawCache(tmp_path)
    h = c.store("tencent", KEY, URL, b"body-bytes", encoding="gbk",
                fetched_at="2026-09-14T19:00:00+08:00")
    assert c.has("tencent", KEY)
    assert c.load("tencent", KEY) == b"body-bytes"
    assert h == sha256_bytes(b"body-bytes")


def test_cache_stores_metadata_and_verifies_hash(tmp_path):
    c = RawCache(tmp_path)
    c.store("tencent", KEY, URL, b"body-bytes", encoding="gbk",
            fetched_at="2026-09-14T19:00:00+08:00")
    meta = json.loads(c.meta_path("tencent", KEY).read_text(encoding="utf-8"))
    assert meta["url"] == URL
    assert meta["encoding"] == "gbk"
    assert meta["fetched_at"] == "2026-09-14T19:00:00+08:00"
    assert meta["sha256"] == sha256_bytes(b"body-bytes")


def test_cache_miss_returns_none(tmp_path):
    assert RawCache(tmp_path).load("tencent", "nope") is None
    assert RawCache(tmp_path).has("tencent", "nope") is False


# ---------- append-only ----------

def test_cache_is_append_only_on_key(tmp_path):
    """同一 key 重复写：保留首次（历史快照不可被后来的抓取覆盖）。"""
    c = RawCache(tmp_path)
    c.store("tencent", "k", "u", b"first", encoding="utf-8", fetched_at="t1")
    c.store("tencent", "k", "u", b"second", encoding="utf-8", fetched_at="t2")
    assert c.load("tencent", "k") == b"first"


def test_cache_conflict_is_recorded_not_silent(tmp_path):
    """重复写且内容不同 = 源站改写了历史数据，必须留痕（铁律③）。"""
    c = RawCache(tmp_path)
    c.store("tencent", "k", "u", b"first", encoding="utf-8", fetched_at="t1")
    c.store("tencent", "k", "u", b"second", encoding="utf-8", fetched_at="t2")
    meta = json.loads(c.meta_path("tencent", "k").read_text(encoding="utf-8"))
    assert meta["sha256"] == sha256_bytes(b"first")
    assert meta["conflicts"] == [
        {"sha256": sha256_bytes(b"second"), "fetched_at": "t2"}
    ]


def test_cache_rewrite_same_body_is_not_a_conflict(tmp_path):
    c = RawCache(tmp_path)
    c.store("tencent", "k", "u", b"same", encoding="utf-8", fetched_at="t1")
    c.store("tencent", "k", "u", b"same", encoding="utf-8", fetched_at="t2")
    meta = json.loads(c.meta_path("tencent", "k").read_text(encoding="utf-8"))
    assert meta.get("conflicts", []) == []
    assert c.load("tencent", "k") == b"same"


# ---------- 文件名安全 ----------

def test_cache_key_is_filesystem_safe(tmp_path):
    """params_key 含 / : 等字符时必须被转义，不能建出目录。"""
    c = RawCache(tmp_path)
    c.store("eastmoney", "0.000333/2015-2026", "u", b"x", encoding="utf-8",
            fetched_at="t")
    assert c.load("eastmoney", "0.000333/2015-2026") == b"x"
    files = sorted(p.name for p in (tmp_path / "eastmoney").iterdir())
    assert len([f for f in files if f.endswith(".bin")]) == 1
    assert len([f for f in files if f.endswith(".json")]) == 1
    assert [p for p in (tmp_path / "eastmoney").iterdir() if p.is_dir()] == []


def test_similar_keys_do_not_collide(tmp_path):
    """`a/b` 与 `a_b` 清洗后同为 `a_b`：必须靠哈希后缀区分，否则互相覆盖。"""
    c = RawCache(tmp_path)
    c.store("eastmoney", "a/b", "u", b"slash", encoding="utf-8", fetched_at="t")
    c.store("eastmoney", "a_b", "u", b"underscore", encoding="utf-8",
            fetched_at="t")
    assert c.load("eastmoney", "a/b") == b"slash"
    assert c.load("eastmoney", "a_b") == b"underscore"


def test_sources_are_isolated(tmp_path):
    c = RawCache(tmp_path)
    c.store("tencent", "k", "u", b"t", encoding="utf-8", fetched_at="t")
    c.store("eastmoney", "k", "u", b"e", encoding="utf-8", fetched_at="t")
    assert c.load("tencent", "k") == b"t"
    assert c.load("eastmoney", "k") == b"e"


# ---------- 损坏检测（铁律②③：不许静默用坏数据） ----------

def test_cache_corruption_raises(tmp_path):
    c = RawCache(tmp_path)
    c.store("tencent", "k", "u", b"good", encoding="utf-8", fetched_at="t")
    c.body_path("tencent", "k").write_bytes(b"tampered")
    with pytest.raises(CacheCorrupt) as exc:
        c.load("tencent", "k")
    assert "sha256" in str(exc.value).lower()


def test_cache_missing_metadata_raises(tmp_path):
    """正文在、元数据丢：无法校验来源与完整性，宁可报错。"""
    c = RawCache(tmp_path)
    c.store("tencent", "k", "u", b"good", encoding="utf-8", fetched_at="t")
    c.meta_path("tencent", "k").unlink()
    with pytest.raises(CacheCorrupt):
        c.load("tencent", "k")


# ---------- fixture 录制 ----------

def test_fixture_roundtrip(tmp_path):
    p = record_fixture(tmp_path, "tencent_kline_000333",
                       b"payload", {"url": "https://x", "encoding": "gbk"})
    assert p.exists()
    assert p.name == "tencent_kline_000333.bin"
    body, meta = load_fixture(tmp_path, "tencent_kline_000333")
    assert body == b"payload"
    assert meta["url"] == "https://x"
    assert meta["sha256"] == sha256_bytes(b"payload")
    assert json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))["encoding"] == "gbk"


def test_fixture_records_extra_metadata(tmp_path):
    record_fixture(tmp_path, "f", b"x", {"fetched_at": "2026-09-14T23:00:00+08:00",
                                         "n_records": 2})
    _, meta = load_fixture(tmp_path, "f")
    assert meta["name"] == "f"
    assert meta["fetched_at"] == "2026-09-14T23:00:00+08:00"
    assert meta["n_records"] == 2


def test_fixture_tampering_detected(tmp_path):
    """fixture 被手改后必须报错 —— 否则测试会基于假数据「通过」。"""
    record_fixture(tmp_path, "f", b"payload", {})
    (tmp_path / "f.bin").write_bytes(b"payload-edited")
    with pytest.raises(CacheCorrupt):
        load_fixture(tmp_path, "f")


# ---------- 与 HttpClient 的集成（回放命中 / 不联网 / 损坏即报错） ----------

class _ExplodingSession:
    """被调用即失败：用于证明缓存命中路径完全不联网。"""

    def get(self, *args, **kwargs):
        raise AssertionError("缓存命中时不应发起网络请求")


class _OnceSession:
    def __init__(self, body: bytes):
        self.body = body
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1

        class R:
            status_code = 200
            content = self.body

        return R()


def _client(tmp_path, session, **kw):
    from stocklab.data.http import HttpClient, RetryPolicy

    return HttpClient(
        RetryPolicy(attempts=1), session=session, sleep=lambda _: None,
        cache=RawCache(tmp_path), clock=lambda: 0.0,
        now=lambda: "2026-09-14T19:00:00+08:00", **kw
    )


def test_http_cache_miss_then_hit_replays_without_network(tmp_path):
    body = "v_sz000333=\"美的集团\";".encode("gbk")
    url = "https://qt.gtimg.cn/q=sz000333"
    first = _client(tmp_path, _OnceSession(body))
    assert "美的集团" in first.get_text(url, encoding="gbk", source="tencent",
                                        cache_key=KEY)

    replay = _client(tmp_path, _ExplodingSession())
    assert "美的集团" in replay.get_text(url, encoding="gbk", source="tencent",
                                         cache_key=KEY)


def test_http_corrupt_cache_raises_instead_of_refetching(tmp_path):
    """损坏时**不得**退化成重新联网 —— 那会把复现问题掩盖成偶发问题。"""
    body = b"good"
    url = "https://qt.gtimg.cn/q=sz000333"
    _client(tmp_path, _OnceSession(body)).get_text(url, source="tencent",
                                                   cache_key=KEY)
    RawCache(tmp_path).body_path("tencent", KEY).write_bytes(b"tampered")
    with pytest.raises(CacheCorrupt):
        _client(tmp_path, _ExplodingSession()).get_text(url, source="tencent",
                                                         cache_key=KEY)


# ---------- 当日时效（ADR-009）：盘中抓到的 body 不得冻结当日数据 ----------
#
# 事故：key 只带锚点 `end`、不含 `start`，同一 key 首写永久保留。
# 15 日 00:21 抓到的 body 最新一根是 14 日收盘 → 15:30 收盘链命中它，
# **当日 K 线当日不落库**；若抓取发生在盘中，还会把未完成的盘中价当收盘价。

INTRADAY_KEY = "kline:sz000333:2026-09-15:2000:"     # 第 3 段 = 锚点日 end
HIST_KEY = "kline:sz000333:2020-01-02:2000:"          # 历史锚点
INTRADAY_AT = "2026-09-15T09:35:00+08:00"             # 盘中
LEGACY_AT = "2026-09-15T00:21:33+08:00"               # 事故现场那份（收盘前）
CLOSED_AT = "2026-09-15T15:40:00+08:00"               # 收盘后


def _plant_legacy_entry(cache, key, body, fetched_at):
    """绕过写侧闸门，直接在盘上伪造一份修复前留下的条目（模拟事故现场）。"""
    p = cache.body_path("tencent", key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    cache.meta_path("tencent", key).write_text(json.dumps({
        "source": "tencent", "params_key": key, "url": URL, "encoding": "utf-8",
        "fetched_at": fetched_at, "sha256": sha256_bytes(body), "conflicts": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def test_legacy_intraday_snapshot_is_not_a_hit(tmp_path):
    """反证测试：修复前落下的「同日收盘前」条目，读侧必须视为**未命中**。

    不许静默 —— 必须在 meta 的 conflicts 里留下 intraday_snapshot 痕迹，
    否则「当日数据没到」会再次表现为一个查不出原因的哑巴故障。
    """
    c = RawCache(tmp_path)
    body = b'{"data":{"sz000333":{"day":[["2026-09-14","86.8"]]}}}'
    _plant_legacy_entry(c, INTRADAY_KEY, body, LEGACY_AT)

    assert c.load("tencent", INTRADAY_KEY) is None
    assert c.has("tencent", INTRADAY_KEY) is False, "has() 是「load 会不会命中」，不是「文件在不在」"

    meta = json.loads(c.meta_path("tencent", INTRADAY_KEY).read_text(encoding="utf-8"))
    assert meta["conflicts"], "盘中快照被忽略必须留痕，不许静默"
    assert meta["conflicts"][-1]["reason"] == "intraday_snapshot"
    assert meta["conflicts"][-1]["fetched_at"] == LEGACY_AT
    assert meta["sha256"] == sha256_bytes(body), "读侧的时效判定不得改动原有正文与校验值"


def test_intraday_conflict_trace_is_not_duplicated(tmp_path):
    """重复读同一条毒性条目不应把 conflicts 撑成无限长（读是高频动作）。"""
    c = RawCache(tmp_path)
    _plant_legacy_entry(c, INTRADAY_KEY, b"stale", INTRADAY_AT)
    for _ in range(3):
        assert c.load("tencent", INTRADAY_KEY) is None
    meta = json.loads(c.meta_path("tencent", INTRADAY_KEY).read_text(encoding="utf-8"))
    assert len(meta["conflicts"]) == 1


def test_intraday_write_does_not_enter_formal_cache(tmp_path):
    """写侧：盘中响应不落正式缓存 —— 否则「首写保留」会把它永久冻结。"""
    c = RawCache(tmp_path)
    h = c.store("tencent", INTRADAY_KEY, URL, b"intraday-body", encoding="utf-8",
                fetched_at=INTRADAY_AT)
    assert h == sha256_bytes(b"intraday-body")
    assert not c.body_path("tencent", INTRADAY_KEY).exists()
    assert not c.meta_path("tencent", INTRADAY_KEY).exists()
    assert c.load("tencent", INTRADAY_KEY) is None


def test_intraday_write_then_post_close_write_then_hit(tmp_path):
    """事故主路径：盘中抓过 → 收盘后再抓，必须拿到**含当日收盘**的那份。"""
    c = RawCache(tmp_path)
    c.store("tencent", INTRADAY_KEY, URL, b"intraday-body", encoding="utf-8",
            fetched_at=INTRADAY_AT)
    c.store("tencent", INTRADAY_KEY, URL, b"closed-body", encoding="utf-8",
            fetched_at=CLOSED_AT)
    assert c.load("tencent", INTRADAY_KEY) == b"closed-body"


def test_post_close_same_day_write_is_a_hit(tmp_path):
    """② 固定时钟 15:40 写入 → 同日再读必须命中（不重复联网）。"""
    c = RawCache(tmp_path)
    c.store("tencent", INTRADAY_KEY, URL, b"closed-body", encoding="utf-8",
            fetched_at=CLOSED_AT)
    assert c.load("tencent", INTRADAY_KEY) == b"closed-body"
    assert c.has("tencent", INTRADAY_KEY) is True


def test_close_time_boundary_is_exclusive(tmp_path):
    """收盘时刻本身（15:00）算已收盘；(时,分) 口径与 session.tick._closed_at 一致。"""
    c = RawCache(tmp_path)
    c.store("tencent", INTRADAY_KEY, URL, b"at-1500", encoding="utf-8",
            fetched_at="2026-09-15T15:00:00+08:00")
    assert c.load("tencent", INTRADAY_KEY) == b"at-1500"

    c2 = RawCache(tmp_path / "b")
    c2.store("tencent", INTRADAY_KEY, URL, b"at-1459", encoding="utf-8",
             fetched_at="2026-09-15T14:59:00+08:00")
    assert c2.load("tencent", INTRADAY_KEY) is None


def test_historical_anchor_is_unaffected_by_today_fetch(tmp_path):
    """③ 历史锚点（end=2020-01-02、今天抓）必须照常命中、首写保留、SHA 校验不放松。"""
    c = RawCache(tmp_path)
    c.store("tencent", HIST_KEY, URL, b"history-body", encoding="utf-8",
            fetched_at=INTRADAY_AT)
    assert c.load("tencent", HIST_KEY) == b"history-body"
    assert c.store("tencent", HIST_KEY, URL, b"rewritten", encoding="utf-8",
                   fetched_at=CLOSED_AT) == sha256_bytes(b"history-body")
    assert c.load("tencent", HIST_KEY) == b"history-body"


def test_non_date_anchor_key_keeps_old_semantics(tmp_path):
    """第 3 段不是日期（如 `sz000333:320:qfq`）→ 时效规则不适用，照旧命中。"""
    c = RawCache(tmp_path)
    c.store("tencent", "sz000333:320:qfq", URL, b"x", encoding="utf-8",
            fetched_at=INTRADAY_AT)
    assert c.load("tencent", "sz000333:320:qfq") == b"x"


def test_unparsable_fetched_at_keeps_old_semantics(tmp_path):
    """元数据里的 fetched_at 不可解析 → 判不了时效，按旧行为命中（不猜）。"""
    c = RawCache(tmp_path)
    _plant_legacy_entry(c, INTRADAY_KEY, b"x", "t1")
    assert c.load("tencent", INTRADAY_KEY) == b"x"


def test_intraday_snapshot_still_verifies_sha(tmp_path):
    """时效判定排在完整性校验之后：毒性条目被改写过，照样抛 CacheCorrupt。"""
    c = RawCache(tmp_path)
    _plant_legacy_entry(c, INTRADAY_KEY, b"stale", INTRADAY_AT)
    c.body_path("tencent", INTRADAY_KEY).write_bytes(b"tampered")
    with pytest.raises(CacheCorrupt):
        c.load("tencent", INTRADAY_KEY)


def test_http_refetches_instead_of_replaying_legacy_intraday_snapshot(tmp_path):
    """① 端到端：09:35 那份被冻结的 body → 15:30 读取必须**重新联网**拿当日收盘。

    事故正是发生在这条路径上：15:30 的 `ingest bars` 命中了 00:21 的 body，
    于是「当日 K 线当日不落库」。`_OnceSession` 只喂一次：命中缓存就发不出请求。
    """
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    _plant_legacy_entry(RawCache(tmp_path), INTRADAY_KEY, b"stale-intraday", LEGACY_AT)

    fresh = '{"day":[["2026-09-15","87.23"]]}'
    client = _client(tmp_path, _OnceSession(fresh.encode("utf-8")))
    text = client.get_text(url, source="tencent", cache_key=INTRADAY_KEY)
    assert "2026-09-15" in text, "必须真的联网拿到含当日收盘的 body"


def test_legacy_poison_is_never_replayed_even_after_a_fresh_fetch(tmp_path):
    """既有毒条目**不会被自动修复**（显式限制，见 ADR-009）：写侧依旧「首写保留」。

    代价：该 key 每轮多一次联网，直到人工删除那份文件。收益：读侧永远拿不到它，
    且它的 sha/fetched_at 被记进 conflicts —— 事故证据保留，故障不再复发。
    """
    c = RawCache(tmp_path)
    _plant_legacy_entry(c, INTRADAY_KEY, b"stale-intraday", LEGACY_AT)

    assert c.store("tencent", INTRADAY_KEY, URL, b"closed-body", encoding="utf-8",
                   fetched_at=CLOSED_AT) == sha256_bytes(b"stale-intraday")

    assert c.load("tencent", INTRADAY_KEY) is None, "旧毒条目不得因新写入而变成命中"
    meta = json.loads(c.meta_path("tencent", INTRADAY_KEY).read_text(encoding="utf-8"))
    reasons = [x.get("reason") for x in meta["conflicts"]]
    assert "intraday_snapshot" in reasons
    assert {"sha256": sha256_bytes(b"closed-body"), "fetched_at": CLOSED_AT} in meta["conflicts"]
