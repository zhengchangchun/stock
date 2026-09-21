"""HTTP 路由 + 写路径（P15）：stdlib `ThreadingHTTPServer`，**零新增依赖**。

## 只绑回环

复用 `dashboard.server.assert_loopback`（同一份白名单、同一句报错，不重写）。
绑 `0.0.0.0` 会把本机账本暴露到局域网，所以它是**先于 bind 的显式检查**，
失败时一个 socket 都没建。有单测钉住。

## 写路径的三条纪律

1. **`_token` 先于一切**：校验失败 → 403，且**数据库一个字节都不动**
   （有测试钉住「库未变」）。Basic Auth 挡不住 CSRF，这层是必需的。
2. **POST / Redirect / GET（303）**：写成功后重定向回列表页，刷新不会重复提交。
3. **幂等**：每次渲染表单都会带一个 `_form_id`，它**就是**幂等键。
   - 同一份表单被提交两次（双击 / 刷新重发）→ 幂等命中，**不写第二行**；
   - 人**重新敲**一笔完全相同的成交（新的 `_form_id`）→ 进入「疑似重复」确认页，
     必须显式勾选「这是另一笔真成交」才写入（沿用 P12 的机制与文案）。

   这两件事必须分开：前者是重试，后者可能是另一笔真单。业务元组本身
   区分不了它们（同一天分两笔各买 100 股 @86.80 是完全正常的真单），
   所以幂等键只能由**渲染表单的那一刻**给出。

## 校验失败要回显**哪一条**没过

`TradeValidationError` / `CashFlowValidationError` 的原文直接渲染到页面上，
HTTP 400，**不写库、不吞错**。页面同时保留用户填过的值，重新打一遍不是惩罚。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from stocklab.dashboard.server import (LOOPBACK_HOSTS, NonLoopbackHost,
                                       assert_loopback)
from stocklab.labweb import SERVICE, VERSION
from stocklab.labweb import cand_data, cand_render, paper_data, paper_render
from stocklab.labweb.cand_data import CandLab
from stocklab.labweb.data import Lab, now_iso
from stocklab.labweb.render import (CASH_FIELDS, CSS_PATH, JS_PATH,
                                    TRADE_FIELDS, cash_page, cash_pane,
                                    data_page, duplicate_page, error_page,
                                    overview_page, receipt_block, risk_page,
                                    trade_detail_page, trades_page,
                                    trades_pane)
from stocklab.labweb.tokens import TokenSigner, new_form_id, new_secret
from stocklab.candidate import snapshot as cand_snapshot
from stocklab.candidate.run import RUN_KINDS
from stocklab.config import paths
from stocklab.portfolio.ledger import (CASH_KINDS, CashFlowValidationError,
                                       DuplicateTradeError, LedgerError,
                                       TradeValidationError,
                                       find_idem, find_identical_cash_flow,
                                       find_identical_trade, record_cash_flow,
                                       record_trade, reverse_trade,
                                       validate_trade)
from stocklab.store.migrate import ensure_schema

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
DEFAULT_BASE_PATH = "/lab"

#: POST body 上限。表单只有几个字段，超过一定不是正常用户在填。
MAX_BODY = 64 * 1024

#: 候选池「跑一次」的子进程超时（秒）。实测一次主流程 <1 秒（21 只种子），
#: 给足余量：卡住的是子进程，不是 HTTP 连接。
RUN_TIMEOUT_S = 300.0

#: 写路径（方法, 路径正则）→ 处理函数名。
_TRADE_ID = re.compile(r"^/trades/(\d+)$")
_TRADE_REVERSE = re.compile(r"^/trades/(\d+)/reverse$")


class BadBasePath(ValueError):
    """`--base-path` 不合法。**启动即失败**，不做静默兜底。"""


def normalize_base_path(value: str) -> str:
    """`/lab` → `/lab`；`/` → ``（挂在根）。非法值直接抛。

    静默兜底（比如非法就当作 `/`）会让「我以为挂在 /lab」和「实际挂在 /」
    这两种状态在**页面上看起来一模一样**，而对外链接全 404。
    """
    if value is None:
        raise BadBasePath("base-path 不能为空")
    v = value.strip()
    if v in ("", "/"):
        return ""
    if not v.startswith("/"):
        raise BadBasePath(f"base-path 必须以 / 开头，收到 {value!r}")
    if "?" in v or "#" in v or " " in v:
        raise BadBasePath(f"base-path 不能含 ? / # / 空格，收到 {value!r}")
    if ".." in v:
        raise BadBasePath(f"base-path 不能含 ..，收到 {value!r}")
    return v.rstrip("/")


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = field(default=())

    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class Context:
    lab: Lab
    signer: TokenSigner
    base_path: str = DEFAULT_BASE_PATH
    #: 模块1（候选池）的取数门面。**末位 + 有默认值** —— 现有构造点全是关键字形式，
    #: 插一个必填字段进去会把它们全部变成 `TypeError`（模块2 的测试不碰
    #: `/candidate`，`None` 不影响它们）。生产路径（`make_server` /
    #: `cmd_lab_serve`）显式注入。
    cand: "CandLab | None" = None


# ---------- 小工具 ----------

def html_response(status: int, doc: str) -> Response:
    return Response(status, "text/html; charset=utf-8", doc.encode("utf-8"))


def json_response(payload, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      indent=2).encode("utf-8")
    return Response(status, "application/json; charset=utf-8", body)


def redirect(location: str) -> Response:
    """303 See Other —— PRG 的标准答复（刷新不会重发 POST）。"""
    return Response(303, "text/plain; charset=utf-8", b"",
                    (("Location", location),))


def _parse_form(body: bytes) -> dict[str, str]:
    """`application/x-www-form-urlencoded` → `{name: 第一个值}`。"""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}


def _f(fields: dict, key: str) -> str:
    return (fields.get(key) or "").strip()


def _num_field(fields: dict, key: str) -> float:
    raw = _f(fields, key)
    if raw == "":
        raise TradeValidationError(f"{key} 不能为空")
    try:
        return float(raw)
    except ValueError as exc:
        raise TradeValidationError(f"{key} 必须是数字，收到 {raw!r}") from exc


def _int_field(fields: dict, key: str) -> int:
    raw = _f(fields, key)
    if raw == "":
        raise TradeValidationError(f"{key} 不能为空")
    try:
        return int(raw)
    except ValueError as exc:
        raise TradeValidationError(f"{key} 必须是整数，收到 {raw!r}") from exc


def _token_ok(ctx: Context, fields: dict) -> bool:
    return ctx.signer.verify(fields.get("_token"), today=ctx.lab.asof)


# ---------- 静态资产（本应用自己的 CSS/JS） ----------

#: 静态文件目录。**只有白名单里的两个文件**，不做目录遍历。
STATIC_DIR = Path(__file__).with_name("static")
STATIC_TYPES = {CSS_PATH: "text/css; charset=utf-8",
                JS_PATH: "text/javascript; charset=utf-8"}


def static_response(rel: str, if_none_match: str | None = None) -> Response | None:
    """`/lab/static/app.css` 这类请求。不在白名单 → `None`（交给路由去 404）。

    带 `ETag`：CSS/JS 每次导航都会重新校验，命中就是 304，
    省掉的是「每次点导航都重下 20KB 样式表」——这正是「流畅」的一部分。
    """
    # `rel` 带前导斜杠（`/static/app.css`），白名单键不带 —— 归一后再查。
    rel = rel.lstrip("/")
    ctype = STATIC_TYPES.get(rel)
    if ctype is None:
        return None
    path = STATIC_DIR / rel.rsplit("/", 1)[-1]
    try:
        raw = path.read_bytes()
        mtime = int(path.stat().st_mtime)
    except OSError:
        return None
    etag = f'"{len(raw):x}-{mtime:x}"'
    if if_none_match and if_none_match.strip() == etag:
        return Response(304, ctype, b"", (("ETag", etag),
                                          ("Cache-Control", "no-cache")))
    return Response(200, ctype, raw,
                    (("ETag", etag), ("Cache-Control", "no-cache"),
                     ("X-Content-Type-Options", "nosniff")))


# ---------- 局部更新（fetch） ----------

def _error_field(message: str, known: tuple[str, ...]) -> str:
    """消息开头的字段名若在白名单里，就当成**字段级**错误标到那一格旁边。

    认不出（例如「买入数量必须是 100 股整数倍」）返回空串 ——
    标错格子比不标更糟。
    """
    head = (message or "").strip().split(" ", 1)[0]
    return head if head in known else ""


def _fragment(*, pane_html: str, receipt_html: str, url: str) -> Response:
    return json_response({"ok": True, "pane_html": pane_html,
                          "receipt_html": receipt_html, "url": url})


def _frag_trade_error(ctx: Context, exc: Exception) -> Response:
    msg = str(exc)
    return json_response({"ok": False, "error": msg,
                          "field": _error_field(msg, TRADE_FIELDS)}, status=400)


def _frag_cash_error(ctx: Context, exc: Exception) -> Response:
    msg = str(exc)
    return json_response({"ok": False, "error": msg,
                          "field": _error_field(msg, CASH_FIELDS)}, status=400)


def _frag_trades(ctx: Context, trade_id: int, state: str) -> Response:
    """写完之后：把**回执**和**流水表**一起回给页面（同一份取数，口径同一处）。"""
    data = ctx.lab.trades()
    base = ctx.base_path
    return _fragment(
        pane_html=trades_pane(data, base=base),
        receipt_html=receipt_block({"id": trade_id, "state": state,
                                    "what": "成交"}, data["view"]),
        url=f"{base}/trades?receipt=trade:{trade_id}&state={state}")


def _frag_cash(ctx: Context, flow_id: int, state: str) -> Response:
    data = ctx.lab.cash()
    base = ctx.base_path
    return _fragment(
        pane_html=cash_pane(data, base=base),
        receipt_html=receipt_block({"id": flow_id, "state": state,
                                    "what": "现金流"}, data["view"]),
        url=f"{base}/cash?receipt=cash:{flow_id}&state={state}")


def _forbidden(ctx: Context, path: str) -> Response:
    """403：**不写库**、不透露 token 的任何信息。"""
    return html_response(403, error_page(
        base=ctx.base_path, status=403,
        message=("表单令牌缺失或无效，请求被拒绝，**账本没有任何改动**。<br>"
                 "请从页面上重新打开表单再提交（令牌随页面一起签发，"
                 "且只在当天有效）。"),
        asof=ctx.lab.asof, built_at=now_iso()))


# ---------- 写路径 ----------

def _post_trade(ctx: Context, fields: dict, *, fragment: bool = False) -> Response:
    if not _token_ok(ctx, fields):
        return _forbidden(ctx, "/trades")
    base = ctx.base_path
    form_id = _f(fields, "_form_id")
    values = {k: fields.get(k, "") for k in TRADE_FIELDS}
    confirm = _f(fields, "confirm_duplicate") == "1"
    try:
        kw = _trade_kwargs(fields)
    except TradeValidationError as exc:
        if fragment:
            return _frag_trade_error(ctx, exc)
        return html_response(400, trades_page(
            ctx.lab.trades(), base=base, built_at=now_iso(),
            token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
            error=str(exc), values=values,
            err_field=_error_field(str(exc), TRADE_FIELDS)))

    with ctx.lab.conn() as conn:
        try:
            validate_trade(conn, now=now_iso(), **_key(kw))
        except TradeValidationError as exc:
            if fragment:
                return _frag_trade_error(ctx, exc)
            return html_response(400, trades_page(
                ctx.lab.trades(), base=base, built_at=now_iso(),
                token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
                error=str(exc), values=values,
                err_field=_error_field(str(exc), TRADE_FIELDS)))
        # 重试（同一个 `_form_id` 已落过库）不弹「疑似重复」—— 那只是双击/刷新重发。
        # 不先问这一句的话，「疑似重复」预检查会先命中自己刚写下的那行，
        # 把一次无害的重试显示成「你手滑了」。
        retry = bool(form_id) and find_idem(conn, form_id, "trade") is not None
        if not confirm and not retry:
            same = find_identical_trade(conn, **_key(kw))
            if same is not None:
                return _duplicate(ctx, fields, kind="trade",
                                  existing_id=same, endpoint="/trades")
        try:
            res = record_trade(conn, now=now_iso(),
                               idem_key=form_id or None,
                               allow_duplicate=confirm, **kw)
        except DuplicateTradeError as exc:      # 竞态兜底（预检查与写入之间）
            return html_response(409, error_page(
                base=base, status=409, message=str(exc),
                asof=ctx.lab.asof, built_at=now_iso()))
        except TradeValidationError as exc:
            if fragment:
                return _frag_trade_error(ctx, exc)
            return html_response(400, trades_page(
                ctx.lab.trades(), base=base, built_at=now_iso(),
                token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
                error=str(exc), values=values,
                err_field=_error_field(str(exc), TRADE_FIELDS)))
    if fragment:
        return _frag_trades(ctx, res["trade_id"], res["state"])
    return redirect(f"{base}/trades?receipt=trade:{res['trade_id']}"
                    f"&state={res['state']}")


#: 成交的**业务元组**（不含 note）—— 重复判定的键，与 P12 的 CLI 一致。
TRADE_KEY = ("date", "code", "side", "price", "qty", "fee")


def _trade_kwargs(fields: dict) -> dict:
    try:
        fee = float(_f(fields, "fee")) if _f(fields, "fee") else 0.0
    except ValueError as exc:
        raise TradeValidationError(
            f"fee 必须是数字，收到 {_f(fields, 'fee')!r}") from exc
    return {
        "date": _f(fields, "date"),
        "code": _f(fields, "code"),
        "side": _f(fields, "side"),
        "price": _num_field(fields, "price"),
        "qty": _int_field(fields, "qty"),
        "fee": fee,
        "note": _f(fields, "note") or None,
    }


def _cash_kwargs(fields: dict) -> dict:
    return {
        "date": _f(fields, "date"),
        "kind": _f(fields, "kind"),
        "amount": _num_field(fields, "amount"),
        "note": _f(fields, "note") or None,
    }


def _key(kw: dict) -> dict:
    """取业务元组（喂 `validate_trade` / `find_identical_*`，**不含 note**）。"""
    return {k: kw[k] for k in TRADE_KEY}


def _duplicate(ctx: Context, fields: dict, *, kind: str, existing_id: int,
               endpoint: str) -> Response:
    """疑似重复确认页。**此时还没有写任何东西**。"""
    data = ctx.lab.trades() if kind == "trade" else ctx.lab.cash()
    clean = {k: v for k, v in fields.items()
             if k not in ("_token", "confirm_duplicate", "_form_id")}
    return html_response(409, duplicate_page(
        data, base=ctx.base_path, built_at=now_iso(),
        token=ctx.signer.mint(ctx.lab.asof),
        submitted=clean,
        existing={"id": existing_id,
                  "detail": ("另一笔成交" if kind == "trade" else "另一笔现金流")},
        endpoint=endpoint, fields=clean, label="疑似重复"))


def _post_cash(ctx: Context, fields: dict, *, fragment: bool = False) -> Response:
    if not _token_ok(ctx, fields):
        return _forbidden(ctx, "/cash")
    base = ctx.base_path
    form_id = _f(fields, "_form_id")
    values = {k: fields.get(k, "") for k in CASH_FIELDS}
    confirm = _f(fields, "confirm_duplicate") == "1"
    try:
        kw = _cash_kwargs(fields)
        if kw["kind"] not in CASH_KINDS:
            raise CashFlowValidationError(
                f"kind 必须是 {CASH_KINDS} 之一，收到 {kw['kind']!r}")
    except LedgerError as exc:
        if fragment:
            return _frag_cash_error(ctx, exc)
        return html_response(400, cash_page(
            ctx.lab.cash(), base=base, built_at=now_iso(),
            token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
            error=str(exc), values=values,
            err_field=_error_field(str(exc), CASH_FIELDS)))

    with ctx.lab.conn() as conn:
        retry = bool(form_id) and find_idem(conn, form_id, "cash") is not None
        if not confirm and not retry:
            same = find_identical_cash_flow(conn, **kw)
            if same is not None:
                return _duplicate(ctx, fields, kind="cash",
                                  existing_id=same, endpoint="/cash")
        try:
            res = record_cash_flow(conn, now=now_iso(), idem_key=form_id or None,
                                   allow_duplicate=confirm, **kw)
        except LedgerError as exc:
            if fragment:
                return _frag_cash_error(ctx, exc)
            return html_response(400, cash_page(
                ctx.lab.cash(), base=base, built_at=now_iso(),
                token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
                error=str(exc), values=values,
                err_field=_error_field(str(exc), CASH_FIELDS)))
    if fragment:
        return _frag_cash(ctx, res["flow_id"], res["state"])
    return redirect(f"{base}/cash?receipt=cash:{res['flow_id']}"
                    f"&state={res['state']}")


def _post_reverse(ctx: Context, fields: dict, trade_id: int) -> Response:
    if not _token_ok(ctx, fields):
        return _forbidden(ctx, "/trades")
    base = ctx.base_path
    reason = _f(fields, "reason")
    form_id = _f(fields, "_form_id")
    with ctx.lab.conn() as conn:
        try:
            res = reverse_trade(conn, trade_id, reason=reason, now=now_iso(),
                                idem_key=form_id or None)
        except Exception as exc:                # noqa: BLE001（必须变成可读页面）
            detail = ctx.lab.trade_detail(trade_id)
            if detail is None:
                return _not_found(ctx, f"{base}/trades/{trade_id}")
            return html_response(400, trade_detail_page(
                detail, base=base, built_at=now_iso(),
                token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
                error=str(exc)))
    return redirect(f"{base}/trades?receipt=trade:{res['trade_id']}"
                    f"&state={res['state']}")


# ---------- 模块1：候选池（只读页 + 一个写按钮） ----------

def _cand_required(ctx: Context) -> Response | None:
    """`ctx.cand` 未注入时的**显式**答复（`None` = 可以继续）。

    正常路径（`make_server(db_path=…)` / `cmd_lab_serve`）都会注入；这条
    分支只有手工构造 `Context` 才可达 —— 但它必须是一个可读页面，
    而不是 `AttributeError` 导致的空白 500。
    """
    if ctx.cand is not None:
        return None
    return html_response(500, error_page(
        base=ctx.base_path, status=500,
        message=("候选池页需要 `ctx.cand`（模块1 取数门面），本进程没有注入。"
                 "用 `lab serve` 起服务会自动注入。"),
        asof=ctx.lab.asof, built_at=now_iso()))


def _get_candidate(ctx: Context, query: dict, built_at: str) -> Response:
    """`GET /candidate?asof=…&run_kind=…`（两个参数缺省 → 最新一条）。"""
    missing = _cand_required(ctx)
    if missing is not None:
        return missing
    asof = (query.get("asof") or [""])[0].strip() or None
    run_kind = (query.get("run_kind") or [""])[0].strip() or None
    ran = (query.get("ran") or [""])[0]
    raw_sid = (query.get("sid") or [""])[0]
    return html_response(200, cand_render.candidate_page(
        ctx.cand.view(asof=asof, run_kind=run_kind),
        base=ctx.base_path, built_at=built_at,
        token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
        default_asof=ctx.lab.asof,
        ran=ran if ran in ("new", "exists") else "",
        sid=int(raw_sid) if raw_sid.isdigit() else None))


def _candidate_param_error(asof: str, run_kind: str) -> str:
    """表单参数校验（返回空串 = 通过）。**先于任何写库动作**。"""
    if not asof:
        return "asof 不能为空（YYYY-MM-DD）"
    try:
        date.fromisoformat(asof)
    except ValueError:
        return f"asof 必须是 YYYY-MM-DD 的合法日期，收到 {asof!r}"
    if run_kind not in RUN_KINDS:
        return f"run_kind 必须是 {list(RUN_KINDS)} 之一，收到 {run_kind!r}"
    return ""


def _post_candidate_run(ctx: Context, fields: dict) -> Response:
    """跑一次候选池 → 303 回列表页（PRG，刷新不会重发）。

    **token 先于一切**：校验失败 403 且库一个字节都不动（既有规矩，有测试钉住）。
    token 用 `ctx.lab.asof`（页面口径的今天）签发与校验，**不是**表单里的 asof ——
    那个 asof 是历史日期，用历史日期签的 token 在 `verify(today=今天)` 下恒 False
    （设计文档 §3.1 实测，有反向测试钉住）。

    ## 为什么是**子进程**跑 CLI，而不是在请求线程里直接 `run_candidate`

    插桩执行器的超时用 `signal.setitimer`，它**只在主线程有效**
    （`stocklab/plugin/runtime.py` 的模块 docstring 早写了这条约束）。
    而 labweb 是 `ThreadingHTTPServer`：请求都跑在**工作线程**里 ——
    实测在工作线程里直接调 `run_candidate`，第一个插桩调用就抛
    `ValueError: signal only works in main thread`（不是超时，是执行器压根进不去），
    连接直接断在客户端眼前（`RemoteDisconnected`，见 ERROR_DIARY 2026-09-21）。

    所以这里起一个子进程跑 CLI：子进程的主线程满足 signal 的前提，沙盒一行不改，
    两条路径的产出物也同源（有测试在两个独立库上比过成员逐行相同）。
    """
    if not _token_ok(ctx, fields):
        return _forbidden(ctx, "/candidate")
    missing = _cand_required(ctx)
    if missing is not None:
        return missing
    asof = _f(fields, "asof")
    run_kind = _f(fields, "run_kind") or "weekly"
    bad = _candidate_param_error(asof, run_kind)
    if bad:
        return html_response(400, cand_render.candidate_page(
            ctx.cand.view(), base=ctx.base_path, built_at=now_iso(),
            token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
            default_asof=ctx.lab.asof, error=bad,
            values={"asof": asof, "run_kind": run_kind}))

    ensure_schema(ctx.cand.db_path)          # 写库入口统一前滚（P33）
    key = cand_data.SnapshotKey(asof, run_kind)
    with ctx.cand.conn() as conn:            # 跑之前先问一句：新跑还是已存在
        existed = cand_snapshot.find_snapshot(conn, asof=asof,
                                              run_kind=run_kind) is not None
    out = ctx.cand.report_path(key)
    cmd = [sys.executable, "-m", "stocklab.cli.main", "candidate", "run",
           "--db", str(ctx.cand.db_path), "--asof", asof,
           "--run-kind", run_kind, "--out", str(out)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(paths.PROJECT_ROOT), capture_output=True, text=True,
            timeout=RUN_TIMEOUT_S,
            env={**os.environ, "PYTHONPATH": str(paths.PROJECT_ROOT)})
    except subprocess.TimeoutExpired:
        return _run_failed(ctx, asof, run_kind,
                           f"子进程超过 {RUN_TIMEOUT_S:.0f} 秒未结束，已终止")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return _run_failed(ctx, asof, run_kind,
                           f"candidate run 退出码 {proc.returncode}：{detail}")

    with ctx.cand.conn() as conn:
        sid = cand_snapshot.find_snapshot(conn, asof=asof, run_kind=run_kind)
    if sid is None:                          # 退出码 0 却没写入 —— 必须说出来
        return _run_failed(ctx, asof, run_kind,
                           "candidate run 返回 0，但库里没有这条快照")
    state = "exists" if existed else "new"
    return redirect(f"{ctx.base_path}/candidate?asof={quote(asof)}"
                    f"&run_kind={quote(run_kind)}&ran={state}&sid={sid}")


def _run_failed(ctx: Context, asof: str, run_kind: str, message: str) -> Response:
    """跑失败时的可读页面（把子进程的原话带出来，不吞）。"""
    return html_response(500, cand_render.candidate_page(
        ctx.cand.view(asof=asof, run_kind=run_kind), base=ctx.base_path,
        built_at=now_iso(), token=ctx.signer.mint(ctx.lab.asof),
        form_id=new_form_id(), default_asof=ctx.lab.asof, error=message,
        values={"asof": asof, "run_kind": run_kind}))


# ---------- 只读路径 ----------

def _receipt_of(query: dict[str, list[str]], data: dict, kind: str,
                id_key: str) -> dict | None:
    """把 `?receipt=trade:12&state=inserted` 变成一个回执 dict。

    **必须核对 id 真的存在** —— 手编一个 `?receipt=trade:999999` 就能让页面
    显示一条不存在的回执，那是在页面上造一笔不存在的记录。
    """
    raw = (query.get("receipt") or [""])[0]
    if not raw or ":" not in raw:
        return None
    got_kind, _, got_id = raw.partition(":")
    if got_kind != kind or not got_id.isdigit():
        return None
    target = int(got_id)
    # 核对回执用**未过滤**的全量行：`?code=XXX&receipt=trade:9` 这种组合下，
    # 刚写的那笔未必在当前筛选里，但它是真实存在的。
    rows = data.get("all_rows") or data["rows"]
    if not any(r[id_key] == target for r in rows):
        return None
    state = (query.get("state") or [""])[0]
    return {"id": target, "state": state if state in ("inserted", "identical")
            else "inserted",
            "what": "成交" if kind == "trade" else "现金流"}


def _not_found(ctx: Context, path: str) -> Response:
    return html_response(404, error_page(
        base=ctx.base_path, status=404,
        message=f"没有这个页面：{path}",
        asof=ctx.lab.asof, built_at=now_iso()))


def handle(method: str, target: str, *, body: bytes, ctx: Context,
           fragment: bool = False,
           if_none_match: str | None = None) -> Response:
    """路由（纯函数式：只经 `ctx.lab` / `ctx.cand` 读库；写路径见 `_post_*`）。

    `fragment=True`（请求头 `X-Lab-Fragment: 1`）时，写路径回 **JSON 片段**
    而不是 303 —— 页面自己把回执与流水表换掉，不整页刷新。
    """
    split = urlsplit(target)
    path = split.path
    query = parse_qs(split.query, keep_blank_values=True)
    base = ctx.base_path
    built_at = now_iso()

    if path == "/" and base:
        # 方便直接访问根：跳到子路径部署下的真实首页
        return redirect(base + "/")
    if base and not (path == base or path.startswith(base + "/")):
        return _not_found(ctx, path)
    rel = path[len(base):] if base else path
    if rel == "":
        rel = "/"

    if method in ("GET", "HEAD"):
        static = static_response(rel, if_none_match)
        if static is not None:
            return static
        return _get(ctx, rel, query, built_at)

    if method != "POST":
        return Response(405, "text/plain; charset=utf-8",
                        "只支持 GET / POST".encode(), (("Allow", "GET, POST"),))

    fields = _parse_form(body)
    if rel == "/trades":
        return _post_trade(ctx, fields, fragment=fragment)
    if rel == "/cash":
        return _post_cash(ctx, fields, fragment=fragment)
    if rel == "/candidate/run":
        return _post_candidate_run(ctx, fields)
    m = _TRADE_REVERSE.match(rel)
    if m:
        return _post_reverse(ctx, fields, int(m.group(1)))
    return _not_found(ctx, path)


def _get(ctx: Context, rel: str, query: dict, built_at: str) -> Response:
    base = ctx.base_path
    if rel == "/":
        return html_response(200, overview_page(
            ctx.lab.overview(), base=base, built_at=built_at))
    if rel == "/trades":
        code = (query.get("code") or [""])[0].strip() or None
        data = ctx.lab.trades(code=code)
        return html_response(200, trades_page(
            data, base=base, built_at=built_at,
            token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
            receipt=_receipt_of(query, data, "trade", "trade_id")))
    m = _TRADE_ID.match(rel)
    if m:
        detail = ctx.lab.trade_detail(int(m.group(1)))
        if detail is None:
            return _not_found(ctx, base + rel)
        return html_response(200, trade_detail_page(
            detail, base=base, built_at=built_at,
            token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id()))
    if rel == "/cash":
        data = ctx.lab.cash()
        return html_response(200, cash_page(
            data, base=base, built_at=built_at,
            token=ctx.signer.mint(ctx.lab.asof), form_id=new_form_id(),
            receipt=_receipt_of(query, data, "cash", "flow_id")))
    if rel == "/risk":
        return html_response(200, risk_page(ctx.lab.risk(), base=base,
                                            built_at=built_at))
    if rel == "/paper":
        return html_response(200, paper_render.paper_page(
            ctx.lab.paper_track(), base=base, built_at=built_at))
    if rel == "/candidate":
        return _get_candidate(ctx, query, built_at)
    if rel == "/data":
        return html_response(200, data_page(ctx.lab.data(), base=base,
                                            built_at=built_at))
    if rel == "/health":
        return json_response(ctx.lab.health())
    return _not_found(ctx, base + rel)


# ---------- HTTP 层 ----------

def make_handler() -> type[BaseHTTPRequestHandler]:
    class LabHandler(BaseHTTPRequestHandler):
        server_version = f"{SERVICE}/{VERSION}"
        protocol_version = "HTTP/1.1"

        def do_GET(self):       # noqa: N802（stdlib 命名）
            self._go("GET")

        def do_HEAD(self):      # noqa: N802
            self._go("HEAD")

        def do_POST(self):      # noqa: N802
            self._go("POST")

        def do_PUT(self):       # noqa: N802
            self._go("PUT")

        def do_DELETE(self):    # noqa: N802
            self._go("DELETE")

        def _go(self, method: str) -> None:
            resp = handle(method, self.path, body=self._read_body(),
                          ctx=self.server.ctx,   # type: ignore[attr-defined]
                          fragment=self.headers.get("X-Lab-Fragment") == "1",
                          if_none_match=self.headers.get("If-None-Match"))
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            for key, value in resp.headers:
                self.send_header(key, value)
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(resp.body)

        def _read_body(self) -> bytes:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return b""
            if n <= 0:
                return b""
            if n > MAX_BODY:
                return b""
            return self.rfile.read(n)

        def log_message(self, fmt, *args):   # noqa: A003（stdlib 命名）
            # 只记方法/路径/状态，**不记 body**（body 里可能有 token 与凭据）
            sys.stderr.write("[labweb] %s %s\n" % (self.address_string(),
                                                   fmt % args))

    return LabHandler


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
                ctx: Context | None = None, db_path: Path | str | None = None,
                base_path: str = DEFAULT_BASE_PATH, asof: str | None = None,
                signer: TokenSigner | None = None,
                secret: bytes | None = None) -> ThreadingHTTPServer:
    """建服务。**先检查 host，再检查 base-path，最后才 bind。**

    失败时一个 socket 都没建 —— 一个"已经占了端口但拒绝服务"的进程比直接
    退出更难排查。
    """
    assert_loopback(host)
    if ctx is None:
        if db_path is None:
            raise ValueError("必须给 ctx 或 db_path")
        ctx = Context(lab=Lab(db_path, asof=asof),
                      signer=signer or TokenSigner(secret or new_secret()),
                      base_path=normalize_base_path(base_path),
                      cand=CandLab(db_path))
    httpd = ThreadingHTTPServer((host, port), make_handler())
    httpd.daemon_threads = True
    httpd.ctx = ctx          # type: ignore[attr-defined]
    return httpd


def serve_forever(httpd: ThreadingHTTPServer) -> None:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def bound_url(httpd: ThreadingHTTPServer) -> str:
    """实际绑定地址（`port=0` 时端口由内核分配，必须读回来）。"""
    base = httpd.ctx.base_path          # type: ignore[attr-defined]
    host, port = httpd.server_address[0], httpd.server_address[1]
    return f"http://{host}:{port}{base}/"


__all__ = ["BadBasePath", "Context", "DEFAULT_BASE_PATH", "DEFAULT_HOST",
           "DEFAULT_PORT", "LOOPBACK_HOSTS", "MAX_BODY", "NonLoopbackHost",
           "Response", "assert_loopback", "bound_url", "handle", "html_response",
           "json_response", "make_server", "normalize_base_path", "redirect",
           "serve_forever"]
