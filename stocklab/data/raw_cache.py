"""原始响应缓存与 fixture 录制（评审 B3/C5）。

契约：
  - 同一 key **保留首次写入**，后续抓取不覆盖 —— 否则「源站改写历史数据」
    会污染复现（评审 B3）；但改写会记进元数据的 `conflicts`（铁律③：不许静默）。
  - 读取时**总是校验 SHA256**，不匹配抛 `CacheCorrupt`；损坏时绝不退化成重新联网，
    否则复现问题会被掩盖成偶发问题。
  - 文件名由 `_safe()` 清洗 + 哈希后缀保证：既不含路径分隔符，又不会互相覆盖。

写入这些缓存的是手工运行的一次性脚本（`scripts/`），项目内无 cron/守护进程（ADR-001 D-05）。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from stocklab.data.errors import CacheCorrupt

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _safe(name: str) -> str:
    """把任意 key 转成安全文件名。

    清洗会把 `a/b` 与 `a_b` 映射成同一个前缀，故必须再拼哈希后缀区分，
    否则两个不同的 key 会互相覆盖。
    """
    cleaned = _SAFE.sub("_", name)[:80]
    return f"{cleaned}.{sha256_bytes(name.encode('utf-8'))[:12]}"


def _read_verified(body_path: Path, meta_path: Path, what: str) -> tuple[bytes, dict]:
    """读正文 + 元数据，并校验 SHA256。缺元数据同样视为损坏。"""
    if not meta_path.exists():
        raise CacheCorrupt(f"{what} 缺少元数据文件（无法校验完整性）: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    body = body_path.read_bytes()
    actual = sha256_bytes(body)
    if actual != meta.get("sha256"):
        raise CacheCorrupt(
            f"{what} sha256 不匹配（文件已损坏或被改写）: {body_path} "
            f"期望 {meta.get('sha256')} 实际 {actual}"
        )
    return body, meta


class RawCache:
    """原始响应落盘缓存（source 分目录，每个 key 一对 .bin/.json）。"""

    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)

    # ---------- 路径 ----------

    def body_path(self, source: str, params_key: str) -> Path:
        return self.dir / source / f"{_safe(params_key)}.bin"

    def meta_path(self, source: str, params_key: str) -> Path:
        return self.dir / source / f"{_safe(params_key)}.json"

    # ---------- 读写 ----------

    def has(self, source: str, params_key: str) -> bool:
        return self.body_path(source, params_key).exists()

    def store(self, source: str, params_key: str, url: str, body: bytes, *,
              encoding: str, fetched_at: str) -> str:
        """返回**已存版本**的 SHA256（重复写入时返回首次版本的）。"""
        p = self.body_path(source, params_key)
        meta_path = self.meta_path(source, params_key)
        if p.exists():
            stored, meta = _read_verified(p, meta_path, "缓存")
            if meta.get("sha256") != sha256_bytes(body):
                meta.setdefault("conflicts", []).append(
                    {"sha256": sha256_bytes(body), "fetched_at": fetched_at}
                )
                meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return meta["sha256"]

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        meta = {
            "source": source,
            "params_key": params_key,
            "url": url,
            "encoding": encoding,
            "fetched_at": fetched_at,
            "sha256": sha256_bytes(body),
            "conflicts": [],
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return meta["sha256"]

    def load(self, source: str, params_key: str) -> bytes | None:
        """未命中返回 None；命中但损坏抛 `CacheCorrupt`。"""
        p = self.body_path(source, params_key)
        if not p.exists():
            return None
        body, _ = _read_verified(p, self.meta_path(source, params_key), "缓存")
        return body


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
    """读 fixture 并校验 SHA256（防止手改 fixture 让测试假通过）。"""
    fixture_dir = Path(fixture_dir)
    return _read_verified(fixture_dir / f"{name}.bin",
                          fixture_dir / f"{name}.json", "fixture")
