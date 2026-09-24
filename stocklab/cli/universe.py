"""`universe` 子命令：宇宙的**建 / 投影 / 对账**三件事（P71 / ADR-026）。

## 谁跑哪一条

| 命令 | 行为 | 什么时候由谁跑 |
|---|---|---|
| `universe build --source <s> --out <dir>` | 抓成分 ⇒ 写 `config/universes/<id>.{csv,meta.json}` | **联网**；nanobot 跑（站内测试一律注入假 fetcher） |
| `universe sync --universe <id>` | 文件 ⇒ DB 投影（`universe_memberships` 整体重写该 id）＋ 追加 `instruments` 行 ＋ 补**空** `sector` | **写真库**；nanobot 跑（站内只在 `/tmp` 副本上测） |
| `universe doctor --universe <id>` | **只读**对账：文件 sha vs 表投影 sha、`instruments` 覆盖、`sector` 非空率 | 任何时刻都只读 |

## sync 的三条红线

1. **幂等**：按 `universe_id` 整体重写该 id 的行（先 DELETE 再 INSERT），重入不新增行。
2. **既有 `instruments` 行只允许补空列**：`name`/`market`/`board`/`type` **一个字都不改**
   —— 那几列会进 `ctx`、进而进分数（与 P53 `instruments.sector` 的「只补空」同一条纪律）。
   `sector` 只在**原值为空**且文件里有值时才写；已经有值的一律不动（人工值优先）。
3. **新成员一律落 `active=0`**（P72 / ADR-027）：`instruments.active` 在本仓是**全局日更口径
   开关**（预测集合 / 回放 / 实验 / 复盘 / `session tick` / `ingest *` 的默认集合都读它），
   不是装饰位。把研究池（csi300-500 的 700+ 只）顺手写成 `active=1` ＝ **隐式换日更宇宙**：
   日链的 `ingest *` 会从 21 只放大到 821 只（`ops/chain.py` 整轮预算 900 s 当场爆）。
   ⇒ 研究池成员先进 `instruments`、不进日更口径；要进口径必须**显式**改这一列
   （本档**不提供** `--activate` 开关，见 ADR-027）。既有行的 `active` **一个字不动**。

## doctor 的「一致」判据（§9 接口裁决 4）

文件 `members_sha256` == 表里该 `universe_id` 行的 canonical sha；**表缺行也是 exit 2**
（不是「跳过」）。`instruments` 覆盖不全同样 exit 2；`sector` 非空率**只报不判红**
（`seed21` 在真库上本来就是 0/21，那是 P53 那条从未在生产执行过的回填路径的历史欠账，
不是宇宙扩容引入的）。

⚠️ P72：`instruments` **覆盖**判据只看「成员是否在表里」，**不看 `active`** —— `sync` 之后
研究池成员一律 `active=0`（ADR-027），那是**预期结果**、不是不一致。两者分布单独报一行
（`成员 active N / 非 active M`），只报数、不改判据。

## doctor 的只读姿势

`sqlite3.connect(f"file:{abs}?mode=ro&immutable=1", uri=True)`（硬约束 2 的规定姿势）。
⚠️ `immutable=1` 会**忽略 WAL**：`sync` 关闭连接时 SQLite 默认会把 WAL 落回主库
（最后一根连接），所以顺序执行「sync → doctor」看到的是落盘后的状态；但如果**别的进程**
正持有 WAL 句柄，doctor 可能读到略旧的一份 —— 它只做对账报告，不据此写任何东西。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from stocklab.config import paths
from stocklab.config.universes import UniverseError, build_universe, load_universe
from stocklab.data.errors import FetchError
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import ensure_schema

#: 只读连接参数（硬约束 2 的规定姿势：`mode=ro&immutable=1`，**必须绝对路径**）。
_RO_QUERY = "mode=ro&immutable=1"


def open_ro(db_path: Path | str) -> sqlite3.Connection:
    """真库**只读**连接。库不存在 / 打不开 → 抛 `sqlite3.Error`。"""
    abs_path = Path(db_path).resolve()
    if not abs_path.is_file():
        raise sqlite3.OperationalError(f"库不存在：{abs_path}")
    conn = sqlite3.connect(f"file:{abs_path}?{_RO_QUERY}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- build ----------

def cmd_universe_build(args) -> int:
    """抓成分 ⇒ 写 `<id>.csv` ＋ `<id>.meta.json`（**联网**，默认 fetcher 走东财）。"""
    out_dir = Path(args.out) if args.out else paths.UNIVERSE_DIR
    try:
        csv_p, meta_p = build_universe(args.source, out_dir=out_dir)
    except UniverseError as exc:
        print(f"❌ universe build：{exc}", file=sys.stderr)
        return 2
    except FetchError as exc:
        print(f"❌ universe build 抓取失败（源站）：{exc}", file=sys.stderr)
        return 1
    print(f"source={args.source} csv={csv_p} meta={meta_p}")
    return 0


# ---------- sync ----------

def cmd_universe_sync(args) -> int:
    """把宇宙文件投影进 DB（**写库**；幂等；既有 `instruments` 行只补空列）。"""
    try:
        universe = load_universe(args.universe)
    except UniverseError as exc:
        print(f"❌ universe sync：{exc}", file=sys.stderr)
        return 2

    db_path = Path(args.db) if args.db else paths.DB_PATH
    ensure_schema(db_path)                     # 建表/前滚（幂等；新表由 executescript 建出）
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(db_path)
    try:
        return _sync(conn, universe, now=now)
    finally:
        conn.close()


def _sync(conn: sqlite3.Connection, universe, *, now: str) -> int:
    existing = {r["code"]: r for r in
                conn.execute("SELECT code, sector FROM instruments")}
    added: list[str] = []
    sector_filled: list[str] = []
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM universe_memberships WHERE universe_id = ?",
                     (universe.universe_id,))
        conn.executemany(
            "INSERT INTO universe_memberships (universe_id, code, members_sha256,"
            " sector, index_membership, synced_at) VALUES (?,?,?,?,?,?)",
            [(universe.universe_id, i.code, universe.members_sha256,
              universe.sectors.get(i.code),
              universe.index_membership.get(i.code, ""), now)
             for i in universe.members])
        for inst in universe.members:
            sector = universe.sectors.get(inst.code)
            row = existing.get(inst.code)
            if row is None:
                # P72：新成员落 `active=0`（研究池不进日更口径，ADR-027）。
                conn.execute(
                    "INSERT INTO instruments (code, name, market, board, type, sector,"
                    " active, added_at) VALUES (?,?,?,?,?,?,0,?)",
                    (inst.code, inst.name, inst.market, inst.board,
                     inst.asset_type, sector, now))
                added.append(inst.code)
                continue
            # 既有行：只补空 `sector`。`active` 与 name/market/board/type **一个字不动**
            # —— 那几列是人工/既有口径，sync 无权改写（既有 21 只因此仍 active=1）。
            if sector and not (row["sector"] or "").strip():
                conn.execute("UPDATE instruments SET sector = ? WHERE code = ?",
                             (sector, inst.code))
                sector_filled.append(inst.code)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    repo.log_event(
        conn, "universe", "info",
        f"universe sync {universe.universe_id}：投影 {len(universe.members)} 行、"
        f"新增 instruments {len(added)} 行（active=0，不进日更口径）、"
        f"补空 sector {len(sector_filled)} 行"
        f"（members_sha256={universe.members_sha256}；只补空列，不改既有行）",
        context={"universe_id": universe.universe_id,
                 "members_sha256": universe.members_sha256,
                 "n_members": len(universe.members),
                 "instruments_added": added,
                 # P72 T1：新增的是**非 active** 成员 —— 事件流里一眼可查。
                 "instruments_added_inactive": True,
                 "sector_filled": sector_filled},
        now=now)
    print(f"universe={universe.universe_id} 投影={len(universe.members)} 行 "
          f"新增 instruments={len(added)} 只 补空 sector={len(sector_filled)} 只 "
          f"sha={universe.members_sha256}")
    return 0


# ---------- doctor ----------

def cmd_universe_doctor(args) -> int:
    """**只读**对账。一致 ⇒ exit 0；任一不一致 ⇒ exit 2 ＋ 逐条打印差异。"""
    try:
        universe = load_universe(args.universe)
    except UniverseError as exc:
        print(f"❌ universe doctor（文件侧）：{exc}", file=sys.stderr)
        return 2

    db_path = Path(args.db) if args.db else paths.DB_PATH
    try:
        conn = open_ro(db_path)
    except sqlite3.Error as exc:
        print(f"❌ universe doctor：打不开库（只读）{db_path}：{exc}", file=sys.stderr)
        return 2
    try:
        diffs, info = _doctor(conn, universe)
    finally:
        conn.close()

    for line in info:
        print(line)
    if diffs:
        print(f"❌ 不一致（{len(diffs)} 条）：")
        for d in diffs:
            print(f"  - {d}")
        return 2
    print("✅ 一致：文件 sha == 表投影 sha，instruments 覆盖完整")
    return 0


def _doctor(conn: sqlite3.Connection, universe) -> tuple[list[str], list[str]]:
    diffs: list[str] = []
    info: list[str] = [f"universe={universe.universe_id} 文件成员={len(universe.members)} "
                       f"文件 sha={universe.members_sha256}"]

    if not conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            " AND name='universe_memberships'").fetchone()[0]:
        diffs.append("表 universe_memberships 不存在 —— 先跑 `universe sync`")
        rows: list = []
    else:
        rows = list(conn.execute(
            "SELECT code, members_sha256 FROM universe_memberships"
            " WHERE universe_id = ? ORDER BY code", (universe.universe_id,)))
    if not rows:
        diffs.append(f"表里没有 universe_id={universe.universe_id!r} 的投影行"
                     "（**表缺行也是 exit 2**，不是跳过）")
    else:
        shas = sorted({str(r["members_sha256"]) for r in rows})
        if shas != [universe.members_sha256]:
            diffs.append(f"表投影 sha {shas} ≠ 文件 sha {universe.members_sha256}")
        if len(rows) != len(universe.members):
            diffs.append(f"表行数 {len(rows)} ≠ 文件成员数 {len(universe.members)}")
        got, want = {str(r["code"]) for r in rows}, set(universe.codes)
        if got != want:
            diffs.append(f"代码集合不同：表独有 {sorted(got - want)}、"
                         f"文件独有 {sorted(want - got)}")

    if not conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            " AND name='instruments'").fetchone()[0]:
        instr: dict[str, sqlite3.Row] = {}
    else:
        # P72 T3：「instruments 覆盖」判据＝成员**存在于表里**（`SELECT code`），
        # **不看 `active`** —— `sync` 之后研究池成员是 `active=0`，那是**预期**，
        # doctor 不能因此报红；但两边的分布要看得见（下面逐条报数）。
        instr = {str(r["code"]): r for r in
                 conn.execute("SELECT code, sector, active FROM instruments")}
    missing = [c for c in universe.codes if c not in instr]
    if missing:
        diffs.append(f"instruments 缺 {len(missing)} 只：{missing[:8]}"
                     f"{'…' if len(missing) > 8 else ''}")
    covered = len(universe.members) - len(missing)
    have_sector = sum(1 for c in universe.codes
                      if str((instr[c]["sector"] if c in instr else None) or "").strip())
    n_active = sum(1 for c in universe.codes
                   if c in instr and int(instr[c]["active"] or 0) == 1)
    info.append(f"表投影行={len(rows)}；instruments 覆盖 {covered}/{len(universe.members)}")
    info.append(f"成员 active {n_active} / 非 active {covered - n_active}"
                "（**只报，不判红**：研究池成员 `active=0` 是 `sync` 的预期结果）")
    info.append(f"sector 非空 {have_sector}/{len(universe.members)}"
                "（**只报，不判红**：seed21 在真库上本来就是 0/21，属 P53 回填的历史欠账）")
    return diffs, info
