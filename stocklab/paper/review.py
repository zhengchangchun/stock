"""AI 操盘手的**复盘台账**与「已确认教训」回注（P85 / K1–K4）。

## 这个模块存在的唯一理由

P84 把「市场状态」与「自己的历史」写进了 PIT 上下文，AI 于是**看得见**自己做过
什么。看 ≠ 会总结。本站补的是另一半：让 AI 把「对上一笔决策的复盘」写进
append-only 台账，而且**每一句都要带能在库里核到的证据指针**。

## 「读数不许编」是硬校验，不是提示词礼貌

`K3` 四种指针（`decision` / `trade` / `metric` / `market`）在**写入口**逐条核对：
`decision_id` 得真存在且属于这条臂、`trade_id` 得真存在且账户相符、`metric.value`
得与**上下文展示的**读数（`round(库真值, DISPLAY_DP)`）逐值对得上（容差 1e-6）、
`market.field` 得真在**当日 `market` 块**里且值对得上。任何一条不成立 ⇒ 拒绝、**零写入**。

比对基准是**展示值**而不是未舍入的库真值（P105 / D1）：模型在上下文里只能看到按
`DISPLAY_DP` 位小数展示的读数（净值/成交见 `own_history._round4`、`market` 块见
`market_view` 的同值展示），拿 1e-6 去比未舍入的库值在数学上不可能满足 ——
4 位小数必带 ≤5e-5 的舍入误差 ⇒ 凡该列小数第 5 位非 0，这条复盘必被误拒。
`market` 腿一直按块里的展示值比对，`metric` 腿与它同构。

为什么值得这么严：复盘是**下一轮的输入**。一句「我这周回撤 3%」如果和
`paper_nav_daily` 对不上，它就会作为一个假事实进入模型自己的历史叙事 ——
而历史叙事一旦假了，后面每一轮的「复盘」都建立在它上面。**能编的读数比没有读数更糟。**

## 复盘是文字与读数，不是参数（K6）

载荷 schema 是 `additionalProperties: false` 的白名单：**结构上就写不进**
「把参数改成 X」这种字段。`lessons` 只以**文本**进入下一轮上下文（`own_history.facts`），
而参数改动的唯一通路是 `paper spec set` 的五字段白名单（D-18/D-34），两条路不交叉。

## PIT

复盘只能引用 `date/asof <= 复盘日` 的决策 / 成交 / 净值；`market` 块按复盘日现算
（它本身自带 PIT 过滤）。于是「在 `asof` 之后插一条复盘」⇒ 决策日的上下文与
`decision_context_sha256` **逐字节不变**（`derive_facts` / `load_reviews` 都带这一层过滤）。

## 确定性

`derive_facts` 是**纯函数**：输出只含 `key` / `text` / `seen_at` / `conflict`，
不含时间戳、不含自增 id ⇒ 同库两次调用逐字节相同（用例钉住）。

## append-only

本模块**不提供** UPDATE / DELETE（改错只能再写一版；`(arm, asof, kind)` 是幂等键，
重复写入即冲突）；schema 侧有两只 `BEFORE UPDATE/DELETE` 触发器兜底。
"""

from __future__ import annotations

import json
import re
import sqlite3

#: 本期唯一的 `kind`（K1 留的扩展位：以后有 'weekly' 之类，`(arm, asof, kind)` 仍幂等）。
KIND_DAILY = "daily"
KINDS: tuple[str, ...] = (KIND_DAILY,)

TABLE = "paper_agent_reviews"

#: K3 的 `metric` 白名单 → `paper_nav_daily` 的列名。
#: 白名单与列名的**唯一真源在这里** —— 两处各写一份，迟早会多出/少掉一个口径。
METRIC_COLUMNS: dict[str, str] = {
    "nav": "nav",
    "cash": "cash",
    "market_value": "market_value",
    "cum_return": "cum_return",
    "cum_cost": "cum_cost",
    "drawdown": "drawdown",
    "net_deposits": "net_deposits",
}

EVIDENCE_KINDS: tuple[str, ...] = ("decision", "trade", "metric", "market")
LESSON_KINDS: tuple[str, ...] = ("fact", "habit")

#: K4：`facts` 的条数上限 与 `own_history.recent_reviews` 的条数上限（K5）。
FACT_LIMIT = 10
REVIEW_LIMIT = 3

#: 读数比对的容差（K3 逐字：1e-6）。**不放宽** —— 放宽会同时放过真编的读数。
TOL = 1e-6

#: 上下文里读数的**展示精度**（P105 / D1：口径真源就在这里）。
#:
#: 模型只能看到按本值（当前 4）位小数展示的读数 —— `own_history._round4`（净值/成交
#: 序列）与 `market_view` 的同值展示（`market` 块）都是这一口径。故 `metric` 腿的
#: 比对基准是**展示值** `round_display(库真值)`，与 `market` 腿同构。
#: 定义在此而非 `own_history`：后者已 import 本模块，反向不会成环。
DISPLAY_DP: int = 4

#: `lessons[].text` 的字符上限（K2）。
TEXT_MAX = 200

_LESSON_KEY_RE = re.compile(r"^[a-z0-9_]{3,48}$")

_TOP_KEYS: tuple[str, ...] = ("asof", "arm", "kind", "items", "lessons")
_ITEM_KEYS: tuple[str, ...] = ("claim", "evidence")
_LESSON_KEYS: tuple[str, ...] = ("key", "kind", "text")
_PTR_KEYS: dict[str, tuple[str, ...]] = {
    "decision": ("kind", "decision_id"),
    "trade": ("kind", "trade_id"),
    "metric": ("kind", "metric", "date", "value"),
    "market": ("kind", "field", "date", "value"),
}
_PTR_REQUIRED: dict[str, tuple[str, ...]] = {
    "decision": ("kind", "decision_id"),
    "trade": ("kind", "trade_id"),
    "metric": ("kind", "metric", "value"),
    "market": ("kind", "field", "value"),
}


class ReviewError(Exception):
    """本模块的错误基类（CLI 用它一次收口）。"""


class ReviewValidationError(ReviewError):
    """载荷不合法 / 证据指针在库里核不到。**拒绝，不夹紧、不静默**。

    与 `agent_decide.DecisionPayloadError` 同款三属性（`field` / `value` / `reason`）：
    可断言、可渲染 —— CLI 把 `str(exc)` 原样打到 stderr，调用方一眼看出改哪里。
    """

    def __init__(self, field: str, value: object, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"{field}={value!r} —— {reason}")

    def as_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "reason": self.reason}


class ReviewConflict(ReviewError):
    """`(arm, asof, kind)` 已经有一行 —— append-only，本模块不改写历史。

    退出码 1（与 `agent_spec.DecisionConflict` 同档）：你给的**合法**，
    但与库里已有的一版撞了；「输入不合法」是 2。
    """


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _fail(field: str, value: object, reason: str):
    raise ReviewValidationError(field, value, reason)


def _is_number(value: object) -> bool:
    """`bool` 不算数（`True` 是个整数，但把它当读数比对是错的 —— 同 `agent_decide._num`）。"""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def round_display(x: object) -> float | None:
    """模型在上下文里**实际看到的**读数：`round(x, DISPLAY_DP)`（`None` 透传）。

    这是 `metric` 腿的比对基准，也是 `own_history._round4` 的口径（同一 dp，P105 / D2）。
    """
    return None if x is None else round(float(x), DISPLAY_DP)


# ---------- K3：四种证据指针 ----------


def _check_decision(conn, *, arm: str, asof: str, ptr: dict, where: str) -> None:
    did = ptr["decision_id"]
    if not isinstance(did, int) or isinstance(did, bool):
        _fail(f"{where}.decision_id", did, "必须是整数（决策台账的自增主键）")
    if not _has_table(conn, "paper_agent_decisions"):
        _fail(f"{where}.decision_id", did, "库里没有 paper_agent_decisions 表")
    row = conn.execute(
        "SELECT arm, asof FROM paper_agent_decisions WHERE decision_id = ?",
        (int(did),)).fetchone()
    if row is None:
        _fail(f"{where}.decision_id", did,
              "决策台账里没有这一行 —— 指针必须指向**真的发生过**的决策")
    if str(row["arm"]) != arm:
        _fail(f"{where}.decision_id", did,
              f"那一条决策属于 {str(row['arm'])!r}，不是 {arm!r} —— "
              f"复盘只能引用**自己**的决策")
    if str(row["asof"]) > asof:
        _fail(f"{where}.decision_id", did,
              f"那一条决策的 asof={str(row['asof'])} 晚于复盘日 {asof} —— PIT 越界")


def _check_trade(conn, *, arm: str, asof: str, ptr: dict, where: str) -> None:
    tid = ptr["trade_id"]
    if not isinstance(tid, int) or isinstance(tid, bool):
        _fail(f"{where}.trade_id", tid, "必须是整数（成交台账的自增主键）")
    if not _has_table(conn, "paper_trades"):
        _fail(f"{where}.trade_id", tid, "库里没有 paper_trades 表")
    row = conn.execute(
        "SELECT account_id, date FROM paper_trades WHERE trade_id = ?",
        (int(tid),)).fetchone()
    if row is None:
        _fail(f"{where}.trade_id", tid,
              "成交台账里没有这一行 —— 指针必须指向**真的成交过**的那一笔")
    if str(row["account_id"]) != arm:
        _fail(f"{where}.trade_id", tid,
              f"那一笔成交记在 {str(row['account_id'])!r} 账上，不是 {arm!r} —— "
              f"复盘只能引用**自己**的成交")
    if str(row["date"]) > asof:
        _fail(f"{where}.trade_id", tid,
              f"那一笔成交的 date={str(row['date'])} 晚于复盘日 {asof} —— PIT 越界")


def _nav_row(conn, *, arm: str, metric: str, date: object, asof: str, where: str):
    """该臂**这一列**的读数行：`date` 给了就精确取那一天，没给就取 `<= asof` 最近一行。"""
    if not _has_table(conn, "paper_nav_daily"):
        _fail(f"{where}.metric", metric, "库里没有 paper_nav_daily 表")
    col = METRIC_COLUMNS[metric]
    if date is None:
        return conn.execute(
            f"SELECT date, {col} AS reading FROM paper_nav_daily"
            " WHERE account_id = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (arm, asof)).fetchone()
    if not isinstance(date, str) or not date:
        _fail(f"{where}.date", date, "给了就必须是 YYYY-MM-DD 字符串（或整个省略）")
    if date > asof:
        _fail(f"{where}.date", date,
              f"晚于复盘日 {asof} —— PIT 越界（复盘只能引用复盘日及之前的读数）")
    return conn.execute(
        f"SELECT date, {col} AS reading FROM paper_nav_daily"
        " WHERE account_id = ? AND date = ? ORDER BY date DESC LIMIT 1",
        (arm, date)).fetchone()


def _check_metric(conn, *, arm: str, asof: str, ptr: dict, where: str) -> None:
    metric = ptr["metric"]
    if metric not in METRIC_COLUMNS:
        _fail(f"{where}.metric", metric,
              f"不在白名单 {sorted(METRIC_COLUMNS)} 里（名字必须是 paper_nav_daily 的读数列）")
    row = _nav_row(conn, arm=arm, metric=metric, date=ptr.get("date"),
                   asof=asof, where=where)
    if row is None:
        _fail(f"{where}.metric", metric,
              f"该臂在 {'所给的那一天' if ptr.get('date') else f'{asof} 及之前'}"
              f"没有 paper_nav_daily 行 —— 核不到读数")
    reading = row["reading"]
    value = ptr["value"]
    if not _is_number(value):
        _fail(f"{where}.value", value, "读数必须是数字")
    if reading is None:
        _fail(f"{where}.value", value,
              f"{row['date']} 那行的 {metric} 是 NULL —— 没有读数可引用（不拿 0 顶替）")
    shown = round_display(reading)
    if abs(float(value) - float(shown)) > TOL:
        _fail(f"{where}.value", value,
              f"与上下文里的读数不一致：{row['date']} 的 {metric} 展示为 {float(shown)!r}"
              f"（库真值 {float(reading)!r}）（差 "
              f"{abs(float(value) - float(shown)):.6g} > {TOL:g}）—— 读数不许编")


def _leaf_paths(obj, prefix: str = ""):
    """JSON 对象里**标量叶子**的字段路径（`a.b.c`）。列表不是可引用的字段。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(obj, list):
        return
    else:
        yield prefix, obj


def _owning_asof(block: dict, field: str) -> str | None:
    """该字段路径**归属的子块**自己报的数据日（没有 ⇒ `None`）。

    `breadth.up_ratio` 是「`breadth` 块那一天」的读数，而 `breadth.asof` 可能
    **早于**复盘日（长假后的第一天）—— 所以「数据日」不能一律按复盘日算。
    """
    head, _, rest = field.partition(".")
    if head == "index":
        code = rest.partition(".")[0]
        sub = (block.get("index") or {}).get(code) if code else None
        asof = sub.get("price_asof") if isinstance(sub, dict) else None
        return None if asof is None else str(asof)
    sub = block.get(head)
    asof = sub.get("asof") if isinstance(sub, dict) else None
    return None if asof is None else str(asof)


def _market_block(conn: sqlite3.Connection, *, asof: str) -> dict:
    """当日 `market` 块 —— 懒 import（`market_view` 模块级 import `engine`）。"""
    from stocklab.paper import market_view
    return market_view.market_block(conn, asof=asof)


def _check_market(conn, *, asof: str, ptr: dict, where: str) -> None:
    field = ptr["field"]
    if not isinstance(field, str) or not field:
        _fail(f"{where}.field", field, "必须是非空字符串（`market` 块里的字段路径）")
    block = _market_block(conn, asof=asof)
    # 白名单**动态枚举**自当日 market 块本身（不许手抄 —— 抄的那份迟早与块漂移）。
    paths = {p: v for p, v in _leaf_paths(block)}
    if field not in paths:
        _fail(f"{where}.field", field,
              f"不在当日 market 块的字段路径里（现有 {len(paths)} 条，如 "
              f"{sorted(paths)[:6]}…）—— 不许用块里没有的字段名")
    reading = paths[field]
    value = ptr["value"]
    if reading is None:
        _fail(f"{where}.value", value,
              f"{field} 在 {asof} 没有读数（None）—— 没有读数可引用（不拿 0 顶替）")
    if isinstance(reading, str):
        if not isinstance(value, str) or value != reading:
            _fail(f"{where}.value", value,
                  f"{field} 是字符串字段，库里读数是 {reading!r} —— 必须逐字相同")
    else:
        if not _is_number(value):
            _fail(f"{where}.value", value, "读数必须是数字")
        if abs(float(value) - float(reading)) > TOL:
            _fail(f"{where}.value", value,
                  f"与当日 market 块的读数不一致：{field} = {reading!r}"
                  f"（差 {abs(float(value) - float(reading)):.6g} > {TOL:g}）"
                  f"—— 读数不许编")
    date = ptr.get("date")
    if date is None:
        return
    if not isinstance(date, str) or not date:
        _fail(f"{where}.date", date, "给了就必须是 YYYY-MM-DD 字符串（或整个省略）")
    if date > asof:
        _fail(f"{where}.date", date,
              f"晚于复盘日 {asof} —— PIT 越界（复盘只能引用复盘日及之前的读数）")
    owns = _owning_asof(block, field)
    if owns is not None and date != owns:
        _fail(f"{where}.date", date,
              f"与 {field} 所属子块的数据日不一致：块里报的是 {owns}"
              f"（`date` 写的是这条读数**是哪一天**的，不是复盘日）")


_CHECKERS = {"decision": _check_decision, "trade": _check_trade,
             "metric": _check_metric, "market": _check_market}


def _check_evidence(conn, *, arm: str, asof: str, ptr: object, where: str) -> dict:
    if not isinstance(ptr, dict):
        _fail(f"{where}.evidence", ptr, "必须是对象")
    kind = ptr.get("kind")
    if kind not in EVIDENCE_KINDS:
        _fail(f"{where}.kind", kind,
              f"必须是 {list(EVIDENCE_KINDS)} 之一（四类指针各有各的核法）")
    allowed, required = _PTR_KEYS[kind], _PTR_REQUIRED[kind]
    unknown = [k for k in ptr if k not in allowed]
    if unknown:
        _fail(f"{where}.evidence", sorted(unknown),
              f"{kind} 指针只认键 {list(allowed)} —— 多一个都不许（越界即拒）")
    missing = [k for k in required if k not in ptr]
    if missing:
        _fail(f"{where}.evidence", sorted(missing),
              f"{kind} 指针缺必需键 {list(missing)}")
    if kind == "market":
        _check_market(conn, asof=asof, ptr=ptr, where=where)
    else:
        _CHECKERS[kind](conn, arm=arm, asof=asof, ptr=ptr, where=where)
    return {k: ptr[k] for k in allowed if k in ptr}


# ---------- K2：载荷结构（fail-closed 白名单） ----------


def validate_review(conn: sqlite3.Connection, *, arm: str, asof: str,
                    payload: object) -> dict:
    """校验一条复盘载荷（**纯校验，不写库**）。返回规范化后的载荷 ＋ 两个计数。

    结构是**白名单**：顶层只认 `asof` / `arm` / `kind` / `items` / `lessons`
    （五键**都要在**，且三件头必须与命令行给的一致 —— 「这条复盘说的是哪条臂的哪一天」
    不许靠上下文猜）；`items[]` 只认 `claim` / `evidence`；`lessons[]` 只认
    `key` / `kind` / `text`；`evidence` 的键按四种指针各自收窄。多一个键 ⇒ 拒。

    四种指针的**入库核对**在 K3（`_check_evidence`）—— 读数对不上就是这里拒绝，
    而不是写进去等以后再发现。
    """
    if not isinstance(payload, dict):
        _fail("payload", payload, "必须是 JSON 对象")
    unknown = [k for k in payload if k not in _TOP_KEYS]
    if unknown:
        _fail("payload", sorted(unknown),
              f"顶层只认 {list(_TOP_KEYS)} —— 多一个键都不许（K2 是白名单，"
              f"「改参数」这类字段在结构上就写不进来）")
    missing = [k for k in _TOP_KEYS if k not in payload]
    if missing:
        _fail("payload", sorted(missing), f"缺必需键（顶层五键都要在）")
    if payload["asof"] != asof:
        _fail("payload.asof", payload["asof"], f"与 --asof {asof} 不一致")
    if payload["arm"] != arm:
        _fail("payload.arm", payload["arm"], f"与 --arm {arm} 不一致")
    if payload["kind"] not in KINDS:
        _fail("payload.kind", payload["kind"], f"必须是 {list(KINDS)} 之一")

    items = payload["items"]
    if not isinstance(items, list):
        _fail("payload.items", items, "必须是数组")
    lessons = payload["lessons"]
    if not isinstance(lessons, list):
        _fail("payload.lessons", lessons, "必须是数组")

    norm_items: list[dict] = []
    for i, item in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(item, dict):
            _fail(where, item, "必须是对象")
        extra = [k for k in item if k not in _ITEM_KEYS]
        if extra:
            _fail(where, sorted(extra), f"只认 {list(_ITEM_KEYS)} —— 多一个键都不许")
        for k in _ITEM_KEYS:
            if k not in item:
                _fail(where, k, f"缺必需键 {k!r}")
        claim = item["claim"]
        if not isinstance(claim, str) or not claim.strip():
            _fail(f"{where}.claim", claim, "必须是非空字符串（一句话的结论）")
        norm_items.append({
            "claim": claim,
            "evidence": _check_evidence(conn, arm=arm, asof=asof,
                                        ptr=item["evidence"], where=where)})

    norm_lessons: list[dict] = []
    seen: set[str] = set()
    for i, lesson in enumerate(lessons):
        where = f"lessons[{i}]"
        if not isinstance(lesson, dict):
            _fail(where, lesson, "必须是对象")
        extra = [k for k in lesson if k not in _LESSON_KEYS]
        if extra:
            _fail(where, sorted(extra), f"只认 {list(_LESSON_KEYS)} —— 多一个键都不许")
        for k in _LESSON_KEYS:
            if k not in lesson:
                _fail(where, k, f"缺必需键 {k!r}")
        key = lesson["key"]
        if not isinstance(key, str) or not _LESSON_KEY_RE.match(key):
            _fail(f"{where}.key", key, r"必须匹配 [a-z0-9_]{3,48}")
        if key in seen:
            _fail(f"{where}.key", key,
                  "同一条复盘里同一个 key 出现了两次 —— 「最新一条的 text」就没有唯一答案了")
        seen.add(key)
        if lesson["kind"] not in LESSON_KINDS:
            _fail(f"{where}.kind", lesson["kind"], f"必须是 {list(LESSON_KINDS)} 之一")
        text = lesson["text"]
        if not isinstance(text, str) or not text.strip():
            _fail(f"{where}.text", text, "必须是非空字符串")
        if len(text) > TEXT_MAX:
            _fail(f"{where}.text", len(text), f"超过 {TEXT_MAX} 字上限")
        norm_lessons.append({"key": key, "kind": lesson["kind"], "text": text})

    return {"asof": asof, "arm": arm, "kind": payload["kind"],
            "items": norm_items, "lessons": norm_lessons,
            "n_items": len(norm_items), "n_lessons": len(norm_lessons)}


def canonical_payload(payload: dict) -> str:
    """落库用的 canonical JSON（排序键、紧凑分隔符）—— 同载荷 ⇒ 逐字节同串。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


# ---------- 写入口（append-only） ----------


def record_review(conn: sqlite3.Connection, *, arm: str, asof: str, payload: object,
                  model_id: str, prompt_sha256: str, context_sha256: str,
                  now: str, kind: str = KIND_DAILY) -> dict:
    """校验 → 查重 → 落一条复盘。返回回执（K7 的那一行 JSON 的原料）。

    幂等键是 `(arm, asof, kind)`：重复写入 ⇒ `ReviewConflict`（**exit 1**），
    **零写入** —— 校验失败与冲突都在 INSERT 之前判定，写下去的就是一条完整行。
    """
    validated = validate_review(conn, arm=arm, asof=asof, payload=payload)
    if kind not in KINDS:
        _fail("--kind", kind, f"必须是 {list(KINDS)} 之一")
    if validated["kind"] != kind:
        _fail("payload.kind", validated["kind"], f"与 --kind {kind} 不一致")
    existing = conn.execute(
        f"SELECT review_id, created_at FROM {TABLE}"
        " WHERE arm = ? AND asof = ? AND kind = ?",
        (arm, asof, kind)).fetchone() if _has_table(conn, TABLE) else None
    if existing is not None:
        raise ReviewConflict(
            f"{arm} 在 {asof} 已经有一条 {kind} 复盘（review_id="
            f"{int(existing['review_id'])}，{str(existing['created_at'])}）—— "
            f"append-only，本命令不改写历史（改错请换一天、或等下一版作业）")
    try:
        cur = conn.execute(
            f"INSERT INTO {TABLE} (arm, asof, kind, model_id, prompt_sha256,"
            " context_sha256, n_items, n_lessons, payload_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (arm, asof, kind, model_id, prompt_sha256, context_sha256,
             validated["n_items"], validated["n_lessons"],
             canonical_payload(validated), now))
    except sqlite3.IntegrityError as exc:      # 竞态：预检与落库之间被插了一行
        raise ReviewConflict(
            f"{arm} 在 {asof} 的 {kind} 复盘刚被别人写进去了 —— append-only"
            f"（{type(exc).__name__}: {exc}）") from None
    facts = derive_facts(conn, arm=arm, asof=asof)
    return {"written": True, "review_id": int(cur.lastrowid), "arm": arm,
            "asof": asof, "kind": kind, "n_items": validated["n_items"],
            "n_lessons": validated["n_lessons"],
            "facts": len(facts["facts"]),
            "n_facts_truncated": facts["n_facts_truncated"],
            "context_sha256": context_sha256}


# ---------- 读出口（只读） ----------


def _project_items(payload: object) -> list[dict]:
    """载荷里的 `items`（原样搬运那两个键；坏载荷 ⇒ `[]`，**不抛**）。"""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [{"claim": it.get("claim"), "evidence": it.get("evidence")}
            for it in items if isinstance(it, dict)]


def _project_lessons(payload: object) -> list[dict]:
    """载荷里的 `lessons`（只留 `key` 是字符串的那些；坏载荷 ⇒ `[]`，**不抛**）。"""
    lessons = payload.get("lessons") if isinstance(payload, dict) else None
    if not isinstance(lessons, list):
        return []
    return [{"key": le.get("key"), "kind": le.get("kind"), "text": le.get("text")}
            for le in lessons
            if isinstance(le, dict) and isinstance(le.get("key"), str)]


def _iter_reviews(conn: sqlite3.Connection, *, arm: str, asof: str | None = None,
                  kind: str | None = None, strict: bool = False):
    """该臂的复盘行，**升序**（`asof`, `review_id`）。表不存在 ⇒ 空。

    `asof` 给了 ⇒ 只取 `asof <= asof`（PIT：复盘只能引用复盘日及之前的行）。

    `strict=True`（P86 修订）⇒ 改取 `asof **<** asof`：**决策上下文专用的窗口**。
    理由（2026-09-27 实测）：`own_history` 拿的是「决策**日**」的 asof，而同一批
    运行里生成器要**先写当天的复盘、再写当天的决策** —— 当天的复盘若进当天决策的
    上下文，② 里取的 `context_sha256` 会立刻失效，`paper agent decide` 的 D-49
    指纹闸门**必拒**（逐条复现见 P86 任务书 §7），同日重放也会因指纹变化而报冲突。
    收紧到「**只看早于决策日的复盘**」后：当天的上下文在当天所有写入之后保持
    一字不变（可重放、闸门与写序无关），而复盘照旧在**次日**成为决策输入 ——
    这正是「复盘是**下一轮**的输入」（ADR-036 §2）的字面意思。

    列出口（`paper agent reviews` / `facts`）**不传** `strict`：那是「看到某日为止
    的全部复盘」的陈列语义，包含当天是应该的。
    """
    if not _has_table(conn, TABLE):
        return []
    sql = f"SELECT * FROM {TABLE} WHERE arm = ?"
    args: list = [arm]
    if asof is not None:
        sql += " AND asof < ?" if strict else " AND asof <= ?"
        args.append(asof)
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY asof, review_id"
    out: list[dict] = []
    for row in conn.execute(sql, args):
        rec = dict(row)
        try:
            payload = json.loads(rec["payload_json"])
        except (ValueError, TypeError):
            payload = None
        rec["payload"] = payload
        rec["items"] = _project_items(payload)
        rec["lessons"] = _project_lessons(payload)
        out.append(rec)
    return out


def load_reviews(conn: sqlite3.Connection, *, arm: str, asof: str | None = None,
                 limit: int | None = None, strict: bool = False) -> list[dict]:
    """读该臂的复盘台账（**只读**）。

    `limit` 给了 ⇒ 取**最近** N 条，仍以**升序**返回（与 `own_history` 的
    「最近 ≤3 条、升序」同一口径：「最近」是选法，「升序」是呈现顺序）。
    `strict=True` ⇒ 只取 **严格早于** `asof` 的（决策上下文的窗口，见 `_iter_reviews`）。
    `payload_json` 坏掉的行照出（`payload=None`），**不抛** —— 读出口不替写入口兜底。
    """
    rows = _iter_reviews(conn, arm=arm, asof=asof, strict=strict)
    return rows[-int(limit):] if limit else rows


def derive_facts(conn: sqlite3.Connection, *, arm: str, asof: str,
                 strict: bool = False) -> dict:
    """K4 的**纯函数**：已确认教训（`facts`）。返回
    `{"facts": [...], "n_facts_truncated": N, "n_reviews": N}`。

    规则（逐字照 K4）：

    - 汇总范围 = 该臂 `kind='daily'` 且 `asof <= 复盘日` 的**全部**复盘
      （`strict=True` ⇒ `asof <`，决策上下文用的窗口，见 `_iter_reviews`）；
    - 同一个 `key` 出现在 **≥2 条不同 `asof`** 的复盘里 ⇒ 进 `facts`（出现 1 次不算数：
      单次陈述是**观察**，反复出现才是**模式**——这正是「确认」二字的可执行定义）；
    - `text` 取**最新**一条；`seen_at` 列**全部** `asof` 升序；
    - 同一 `key` 的 `text` 若不一致 ⇒ `conflict: true` 并把**各不相同的原文全部**
      列在 `texts`（按首次出现的 `asof` 升序，去重）—— **不取平均、不猜**；
    - ≤ `FACT_LIMIT` 条：超出按 `seen_at` 条数降序、再按 `key` 字典序截断，
      截断数进 `n_facts_truncated`。

    输出**不含**时间戳、不含自增 id ⇒ 同库两次调用逐字节相同。
    """
    rows = _iter_reviews(conn, arm=arm, asof=asof, kind=KIND_DAILY, strict=strict)
    agg: dict[str, dict] = {}
    for rec in rows:
        for lesson in rec["lessons"]:
            entry = agg.setdefault(lesson["key"],
                                   {"seen_at": [], "texts": []})
            entry["seen_at"].append(str(rec["asof"]))
            entry["texts"].append(lesson["text"])
    qualifying = [(key, e) for key, e in agg.items() if len(e["seen_at"]) >= 2]
    # 条数降序 → key 字典序：全序，不依赖 dict 插入顺序（确定性）。
    qualifying.sort(key=lambda kv: (-len(kv[1]["seen_at"]), kv[0]))
    n_truncated = max(0, len(qualifying) - FACT_LIMIT)
    facts: list[dict] = []
    for key, entry in qualifying[:FACT_LIMIT]:
        texts = list(dict.fromkeys(entry["texts"]))       # 去重且保序（= asof 升序）
        fact = {"key": key, "text": entry["texts"][-1],
                "seen_at": list(entry["seen_at"]), "conflict": len(texts) > 1}
        if fact["conflict"]:
            fact["texts"] = texts
        facts.append(fact)
    return {"facts": facts, "n_facts_truncated": n_truncated,
            "n_reviews": len(rows)}


__all__: list[str] = [
    "KIND_DAILY", "KINDS", "TABLE", "METRIC_COLUMNS", "EVIDENCE_KINDS",
    "LESSON_KINDS", "FACT_LIMIT", "REVIEW_LIMIT", "TOL", "TEXT_MAX",
    "DISPLAY_DP", "round_display",
    "ReviewError", "ReviewValidationError", "ReviewConflict",
    "validate_review", "canonical_payload", "record_review", "load_reviews",
    "derive_facts",
]
