"""原始响应缓存与 fixture 录制（评审 B3/C5）。

契约：
  - 同一 key **保留首次写入**，后续抓取不覆盖 —— 否则「源站改写历史数据」
    会污染复现（评审 B3）；但改写会记进元数据的 `conflicts`（铁律③：不许静默）。
  - 读取时**总是校验 SHA256**，不匹配抛 `CacheCorrupt`；损坏时绝不退化成重新联网，
    否则复现问题会被掩盖成偶发问题。
  - 文件名由 `_safe()` 清洗 + 哈希后缀保证：既不含路径分隔符，又不会互相覆盖。
  - **当日盘中快照不算命中**（ADR-009）：`params_key` 只带锚点 `end`、不含 `start`，
    「首写保留」会把一份缺当日收盘（或含未完成盘中价）的 body 永久冻结。
    判据：`fetched_at` 的本地日 == 锚点日，且时刻早于收盘（复用 `session.close`）。
    读侧视为未命中并在 `conflicts` 留痕；写侧**不落正式缓存**；历史锚点行为不变。

写入这些缓存的是手工运行的一次性脚本（`scripts/`），项目内无 cron/守护进程（ADR-001 D-05）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

from stocklab.data.errors import CacheCorrupt
from stocklab.session import close as close_mod

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


def _anchor_date(params_key: str) -> date | None:
    """取 `params_key` 第 3 段作为请求锚点日（`kline:{code}:{end}:{page}:{adj}`、
    `actions:{code}:{end}:{page}`）。不是 ISO 日期 → None（时效规则不适用）。"""
    parts = params_key.split(":")
    if len(parts) < 3:
        return None
    try:
        return date.fromisoformat(parts[2])
    except ValueError:
        return None


def _fetched_local_dt(fetched_at: str) -> datetime | None:
    """`fetched_at` 是带 +08:00 的本地时刻（`http.now_iso()`）。解析不了 → None。"""
    try:
        return datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return None


def is_intraday_snapshot(params_key: str, fetched_at: str) -> bool:
    """这份响应是不是「锚点日当天、收盘前」抓到的**未完成**快照。

    口径与 `session.tick._closed_at` 一致：`(时, 分) >= (CLOSE_HOUR, CLOSE_MINUTE)`
    才算已收盘。解析不出锚点日 / 抓取时刻时返回 False —— **判不了就不猜**，维持旧行为。
    """
    anchor = _anchor_date(params_key)
    dt = _fetched_local_dt(fetched_at)
    if anchor is None or dt is None or dt.date() != anchor:
        return False
    return (dt.hour, dt.minute) < (close_mod.CLOSE_HOUR, close_mod.CLOSE_MINUTE)


def _record_intraday_conflict(meta_path: Path, meta: dict, fetched_at: str) -> None:
    """把「这份当日快照被读侧忽略」写进 `conflicts`（铁律③：不许静默）。

    幂等：同一个 `fetched_at` 只记一次 —— 读是高频动作，不能把 conflicts 撑爆。
    """
    conflicts = meta.setdefault("conflicts", [])
    entry = {"reason": "intraday_snapshot", "fetched_at": fetched_at}
    if entry in conflicts:
        return
    conflicts.append(entry)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")


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
        """`load` 会不会命中 —— **不是**「文件在不在」。

        （ADR-009 之后两者不再等价：盘中快照的正文还在盘上，但读侧忽略它。）
        完整性不在这里判：元数据读不了就返回 True，让 `load()` 去抛 `CacheCorrupt`。
        """
        if not self.body_path(source, params_key).exists():
            return False
        try:
            meta = json.loads(
                self.meta_path(source, params_key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        return not is_intraday_snapshot(params_key, meta.get("fetched_at") or "")

    def store(self, source: str, params_key: str, url: str, body: bytes, *,
              encoding: str, fetched_at: str) -> str:
        """返回**已存版本**的 SHA256（重复写入时返回首次版本的）。

        当日收盘前抓到的响应**不入正式缓存**：它缺当日收盘（或含未完成的盘中价），
        一旦因「首写保留」被冻结，收盘链就再也拿不到当日 K 线（ADR-009）。
        返回 body 自己的哈希，调用方语义不变（本就不消费返回值）。
        """
        if is_intraday_snapshot(params_key, fetched_at):
            return sha256_bytes(body)

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
        """未命中返回 None；命中但损坏抛 `CacheCorrupt`。

        **先校验完整性，再判时效** —— 损坏的条目照旧抛错，不许被时效规则吞掉。
        """
        p = self.body_path(source, params_key)
        if not p.exists():
            return None
        meta_path = self.meta_path(source, params_key)
        body, meta = _read_verified(p, meta_path, "缓存")
        fetched_at = meta.get("fetched_at") or ""
        if is_intraday_snapshot(params_key, fetched_at):
            # 视为未命中 → 调用方重新联网；但绝不静默：conflicts 里留痕
            _record_intraday_conflict(meta_path, meta, fetched_at)
            return None
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
