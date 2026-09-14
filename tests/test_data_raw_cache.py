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
