"""模块1 展示层：候选池页面（`GET /candidate`、`POST /candidate/run`）。

设计文档 `docs/superpowers/specs/2026-09-21-候选池页面-design.md`（rev2）
＋评审 `docs/plans/2026-09-21-候选池页面设计评审.md` 的逐条落地。

真起服务 + `http.client` 打请求（同 `test_labweb_app.py`）：路由表自测只能证明
「函数返回了 200」，证明不了 HTTP 层真的把它发出去了。两条**例外**：

- 「库不存在」：`lab serve` 在启动期就检查库并 `return 2`，所以那条 UI 分支
  **只有直接调 `handle()` 可达**（评审 S5）；
- 「`ctx.cand` 未注入」：正常起服务一定会注入，只有手工构造 `Context` 能命中。

「空池 / 有淘汰 / 风险解码失败」三种形态**真实库里当前都不存在**，
只能由夹具构造 —— 这也是这里唯一写夹具不写真库的原因。
"""

import http.client
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from stocklab.candidate import snapshot
from stocklab.candidate.pools import POOL_TOPN
from stocklab.candidate.seeds import SEED_CODES
from stocklab.cli.candidate import cmd_candidate_run
from stocklab.labweb import cand_render
from stocklab.labweb.app import Context, handle, make_server
from stocklab.labweb.cand_data import CandLab
from stocklab.labweb.data import Lab
from stocklab.labweb.tokens import TokenSigner
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
ASOF = "2026-09-17"
TODAY = "2026-09-21"          # 页面口径的今天（`ctx.lab.asof`），token 的日期锚
SECRET = b"test-secret-not-a-real-key"
BASE = "/lab"

PLUGINS = {
    "0": "def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n",
    "1": "def run(ctx):\n    return {'score': 80.0, 'pass_flag': True,"
         " 'reason': '量价', 'risk_list': []}\n",
    "2": "def run(ctx):\n    return {'score': 60.0, 'pass_flag': True,"
         " 'reason': '景气', 'risk_list': []}\n",
    "3": "def run(ctx):\n    return {'score': 40.0, 'pass_flag': True,"
         " 'reason': '护城河', 'risk_list': []}\n",
    "4": "def run(ctx):\n    return {'final_score': ctx['raw_score'],"
         " 'risk_out': []}\n",
}

#: 夹具库里的三只标的（与 `test_candidate_run.py` 同一套，便于对照）。
SEED3 = ("000333", "600690", "600519")


# ---------- 夹具 ----------

def _seed(db_path: Path) -> None:
    """建一个够跑完整候选池主流程的库：标的 + 日历 + 300 天 K 线 + 5 个 active 插桩。

    确定性：K 线全是常数、`now` 固定 ⇒ 两次 `_seed` 产出逐字节相同的库，
    所以「同源」那条用例可以在**两个独立库**上比成员（评审 §5）。
    """
    init_db(db_path)
    c = connect(db_path)
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, f"标的{code}", NOW) for code in SEED3])
    days = []
    cur = date(2025, 6, 1)
    while len(days) < 300:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'tencent',?)",
                  [(d, NOW) for d in days])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, 10.0, 10.0, 10.0, 10.0, NOW)
         for code in SEED3 for d in days])
    for pid, text in PLUGINS.items():
        sid = store.insert_script(c, plugin_id=pid, version="1.0.0",
                                  source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
    c.commit()
    c.close()


def _member(code, pool, raw, adj, *, reason="量价配合", risk="[]",
            status="观察中") -> dict:
    return {"code": code, "pool": pool, "raw_score": raw, "adj_score": adj,
            "reason": reason, "risk_json": risk, "status": status}


def _reject(code, stage, reason, plugin_id=None) -> dict:
    return {"code": code, "stage": stage, "reason": reason,
            "plugin_id": plugin_id}


def _snap(db_path: Path, *, asof: str, run_kind: str, created_at: str,
          members: list[dict], rejects: list[dict] | None = None,
          params: dict | None = None) -> int:
    """直接写一条快照（渲染类用例用它，比真跑一遍主流程更可控）。"""
    conn = connect(db_path)
    try:
        return snapshot.write_snapshot(
            conn, asof=asof, run_kind=run_kind,
            params=params or {"seed_count": len(SEED_CODES),
                              "topn": dict(POOL_TOPN)},
            members=[snapshot.MemberRow(**m) for m in members],
            rejects=[snapshot.RejectRow(**r) for r in (rejects or [])],
            now=created_at)
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "seed.db"
    _seed(path)
    return path


def _ctx(db_path: Path, *, out_dir: Path, asof: str = TODAY) -> Context:
    return Context(lab=Lab(db_path, asof=asof), signer=TokenSigner(SECRET),
                   base_path=BASE,
                   cand=CandLab(db_path, out_dir=out_dir))


@pytest.fixture
def ctx(db, tmp_path) -> Context:
    return _ctx(db, out_dir=tmp_path / "reports")


@pytest.fixture
def server(ctx, loopback_http):
    httpd = make_server("127.0.0.1", 0, ctx=ctx)
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def request(server, method, path, body=None, *, headers=None):
    """真 HTTP 请求。返回 `(status, text, headers_dict)`。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                      timeout=20)
    try:
        payload = urlencode(body).encode() if isinstance(body, dict) else body
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, raw, dict(resp.getheaders())
    finally:
        conn.close()


def snapshots(db_path: Path) -> list[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM candidate_snapshots ORDER BY snapshot_id")]
    finally:
        conn.close()


def token(ctx) -> str:
    """表单 token：**必须**用 `ctx.lab.asof` 签（§3.1，用快照 asof 签 = 永久 403）。"""
    return ctx.signer.mint(ctx.lab.asof)


def section_of(html: str, title: str) -> str:
    """取某个 `<section>` 的 HTML（从它的 `<h2>` 切到下一个 `</section>`）。

    必须带上 `<h2 class="sec__h">` 前缀：正文里也会提到「未出现」「淘汰记录」
    这些词，只按词找会切到上半段去。
    """
    start = html.index(f'<h2 class="sec__h">{title}')
    return html[start:html.index("</section>", start)]


# ---------- 渲染 ----------

def test_three_pools_and_utc_converted(server, db):
    _snap(db, asof=ASOF, run_kind="weekly",
          created_at="2026-09-20T08:38:06.386144+00:00",
          members=[_member("000333", "short", 82.14, 77.14),
                   _member("600690", "mid", 61.50, 61.50),
                   _member("600519", "long", 70.00, 68.25)])
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    for pool in ("短期池", "中期池", "长期池"):
        assert pool in html
    # 分数原始值（2 位小数）与代码、名称都在表里
    assert "000333" in html and "标的000333" in html
    assert "82.14" in html and "77.14" in html
    # UTC → Asia/Shanghai（评审 S3：不转就会出现同屏差 8 小时的两个时间）
    assert "2026-09-20 16:38" in html
    assert "库中 UTC 2026-09-20T08:38:06.386144+00:00" in html


def test_empty_pool_says_so_and_has_no_zero(server, db):
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14),
                   _member("600690", "mid", 61.50, 61.50)])
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    long_sec = section_of(html, "长期池")
    assert "本池为空" in long_sec
    assert "0 只" not in long_sec          # 不写 0（既有规矩）


def test_rejects_grouped_by_stage(server, db):
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14)],
          rejects=[_reject("600690", "pre_screen", "consecutive_limit_down"),
                   _reject("600519", "pre_screen", "no_bars"),
                   _reject("600036", "score",
                           "short池打分未通过：动量不足", "1")])
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    assert "盘前排雷 2 条" in html
    assert "打分未过 1 条" in html
    assert "consecutive_limit_down" in html and "no_bars" in html
    # 机器名与中文名同时可查
    assert "pre_screen" in section_of(html, "淘汰记录")
    # 未认出的环节原样显示机器名（只换标签、不改判据）
    assert cand_render.stage_label("mystery_stage") == "mystery_stage"
    assert cand_render.stage_label("pre_screen") == "盘前排雷"


def test_unknown_stage_shows_raw_machine_name(server, db):
    """库的 CHECK 只允许三种 stage，所以「认不出」只能在渲染层构造（评审 S2）。"""
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14)])
    data = CandLab(db, out_dir=db.parent).view()
    data["rejects"] = [_reject("000858", "mystery_stage", "?")]
    data["n_rejects"] = 1
    html = cand_render.candidate_page(
        data, base=BASE, built_at=NOW, token="t", form_id="f",
        default_asof=TODAY)
    assert "mystery_stage" in html
    assert "库中机器名 mystery_stage" in html


def test_missing_diff_count_and_members(server, db):
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14)],
          rejects=[_reject("600690", "pre_screen", "no_bars")])
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    expected = sorted(set(SEED_CODES) - {"000333", "600690"})
    assert f"未出现（{len(expected)} 只）" in html
    block = section_of(html, "未出现")
    assert "000333" not in block and "600690" not in block
    assert "600519" in block and "518880" in block


def test_risk_empty_list_and_null_plugin(server, db):
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14, risk="[]"),
                   _member("600690", "mid", 61.50, 61.50,
                           risk='["提示：样本内"]')],
          rejects=[_reject("600519", "pre_screen", "no_bars", None)])
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    short_sec = section_of(html, "短期池")
    assert '<span class="mut">无</span>' in short_sec      # risk_json == [] → 「无」
    assert "0 条" not in short_sec
    assert "1 条" in section_of(html, "中期池")
    assert "未知" in section_of(html, "淘汰记录")          # plugin_id IS NULL


def test_risk_decode_failure_is_stated(server, db):
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14, risk="{oops")])
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    assert "风险明细解析失败" in html


def test_no_snapshot_yet(server, db):
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    assert "还没有跑过候选池" in html
    assert "本池为空" not in html          # 不显示空表格
    assert "短期池" not in html


def test_db_missing_is_not_500(tmp_path):
    """该分支正常跑不到（`lab serve` 启动即退出），只能直接调 `handle()`。"""
    ghost = tmp_path / "nope.db"
    ctx = _ctx(ghost, out_dir=tmp_path)
    resp = handle("GET", f"{BASE}/candidate", body=b"", ctx=ctx)
    assert resp.status == 200
    assert "数据库文件不存在" in resp.text()
    assert str(ghost) in resp.text()


def test_ctx_without_cand_is_explicit_500(db, tmp_path):
    """正常路径一定注入；手工构造漏了时要给可读页面，不是 AttributeError。"""
    ctx = Context(lab=Lab(db, asof=TODAY), signer=TokenSigner(SECRET),
                  base_path=BASE)
    resp = handle("GET", f"{BASE}/candidate", body=b"", ctx=ctx)
    assert resp.status == 500
    assert "ctx.cand" in resp.text()


def test_selector_lists_each_asof_kind_pair(server, db):
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14)])
    _snap(db, asof=ASOF, run_kind="light", created_at=NOW,
          members=[_member("600690", "short", 61.50, 61.50)])
    _snap(db, asof="2026-06-15", run_kind="weekly", created_at=NOW,
          members=[_member("600519", "short", 70.00, 70.00)])
    status, html, _ = request(server, "GET", f"{BASE}/candidate")
    assert status == 200
    assert f"asof={ASOF}&amp;run_kind=weekly" in html
    assert f"asof={ASOF}&amp;run_kind=light" in html
    assert "asof=2026-06-15&amp;run_kind=weekly" in html
    # 两个参数各取各的：选 light 不会拿到 weekly 的快照
    _, light, _ = request(server, "GET",
                          f"{BASE}/candidate?asof={ASOF}&run_kind=light")
    assert "600690" in light and "82.14" not in light
    _, weekly, _ = request(server, "GET",
                           f"{BASE}/candidate?asof={ASOF}&run_kind=weekly")
    assert "000333" in weekly and "61.50" not in weekly


def test_unknown_pair_falls_back_and_says_so(server, db):
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14)])
    status, html, _ = request(
        server, "GET", f"{BASE}/candidate?asof=2020-01-01&run_kind=weekly")
    assert status == 200
    assert "已回落到最新一条快照" in html
    assert "000333" in html


# ---------- 写路径 ----------

def _run_form_body(ctx, *, asof=ASOF, run_kind="weekly", tok=None):
    return {"_token": token(ctx) if tok is None else tok,
            "_form_id": "form-1", "asof": asof, "run_kind": run_kind}


def test_post_without_token_is_403_and_writes_nothing(server, db, ctx):
    body = _run_form_body(ctx, tok="")
    status, html, _ = request(server, "POST", f"{BASE}/candidate/run", body)
    assert status == 403
    assert "账本没有任何改动" in html
    assert snapshots(db) == []


def test_post_with_snapshot_asof_token_is_403(server, db, ctx):
    """钉死 §3.1：用**快照 asof**签的 token 必须被拒（历史日期恒不通过）。"""
    _snap(db, asof=ASOF, run_kind="weekly", created_at=NOW,
          members=[_member("000333", "short", 82.14, 77.14)])
    body = _run_form_body(ctx, tok=ctx.signer.mint(ASOF))
    status, _, _ = request(server, "POST", f"{BASE}/candidate/run", body)
    assert status == 403
    assert len(snapshots(db)) == 1        # 只有夹具那条，没多写


def test_post_runs_and_redirects(server, db, ctx):
    status, _, headers = request(server, "POST", f"{BASE}/candidate/run",
                                 _run_form_body(ctx))
    assert status == 303
    rows = snapshots(db)
    assert len(rows) == 1 and rows[0]["asof"] == ASOF
    assert rows[0]["run_kind"] == "weekly"
    location = headers["Location"]
    assert f"asof={ASOF}" in location and "run_kind=weekly" in location
    assert "ran=new" in location
    # PRG：跟回列表页，看到的是新快照的结果（入池行数 > 0）
    conn = connect(db)
    try:
        n_members = conn.execute("SELECT COUNT(*) FROM candidate_members"
                                 " WHERE snapshot_id = ?",
                                 (rows[0]["snapshot_id"],)).fetchone()[0]
        n_rejects = conn.execute("SELECT COUNT(*) FROM candidate_rejects"
                                 " WHERE snapshot_id = ?",
                                 (rows[0]["snapshot_id"],)).fetchone()[0]
    finally:
        conn.close()
    assert n_members > 0 and n_rejects > 0
    status, html, _ = request(server, "GET", location)
    assert status == 200
    assert "已跑出新快照" in html


def test_post_is_idempotent_and_says_exists(server, db, ctx):
    body = _run_form_body(ctx)
    assert request(server, "POST", f"{BASE}/candidate/run", body)[0] == 303
    status, _, headers = request(server, "POST", f"{BASE}/candidate/run", body)
    assert status == 303
    assert "ran=exists" in headers["Location"]
    assert len(snapshots(db)) == 1        # 幂等：只有一行
    _, html, _ = request(server, "GET", headers["Location"])
    assert "未重跑" in html and "已有快照" in html


def test_two_run_kinds_same_asof(server, db, ctx):
    assert request(server, "POST", f"{BASE}/candidate/run",
                   _run_form_body(ctx, run_kind="weekly"))[0] == 303
    assert request(server, "POST", f"{BASE}/candidate/run",
                   _run_form_body(ctx, run_kind="light"))[0] == 303
    rows = snapshots(db)
    assert [(r["asof"], r["run_kind"]) for r in rows] == [
        (ASOF, "weekly"), (ASOF, "light")]
    _, weekly, _ = request(server, "GET",
                           f"{BASE}/candidate?asof={ASOF}&run_kind=weekly")
    _, light, _ = request(server, "GET",
                          f"{BASE}/candidate?asof={ASOF}&run_kind=light")
    assert f"#{rows[0]['snapshot_id']}　{ASOF} · weekly" in weekly
    assert f"#{rows[1]['snapshot_id']}　{ASOF} · light" in light


def test_post_writes_report_file(server, db, ctx, tmp_path):
    assert request(server, "POST", f"{BASE}/candidate/run",
                   _run_form_body(ctx))[0] == 303
    out = tmp_path / "reports" / f"{ASOF}-weekly.md"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith(f"# 候选池报告 · {ASOF}")
    assert "已知限制" in text
    assert "淘汰清单" in text


def test_bad_params_is_400_and_writes_nothing(server, db, ctx):
    status, html, _ = request(server, "POST", f"{BASE}/candidate/run",
                              _run_form_body(ctx, asof="2026-13-99"))
    assert status == 400
    assert "asof 必须是 YYYY-MM-DD" in html
    status, html, _ = request(server, "POST", f"{BASE}/candidate/run",
                              _run_form_body(ctx, run_kind="hourly"))
    assert status == 400
    assert "run_kind 必须是" in html
    assert snapshots(db) == []


def test_page_and_cli_produce_same_members(tmp_path):
    """**两个独立库**、同一 asof、两条路径各跑一次再比成员。

    同一个库跑两次比的是幂等加载器，证伪不了「页面另写一套打分」
    （评审 §5 对原设计的修正）。
    """
    db_a, db_b = tmp_path / "a.db", tmp_path / "b.db"
    _seed(db_a)
    _seed(db_b)

    rc = cmd_candidate_run(SimpleNamespace(
        db=str(db_a), asof=ASOF, run_kind="weekly",
        out=str(tmp_path / "cli.md"), now=NOW))
    assert rc == 0

    ctx_b = _ctx(db_b, out_dir=tmp_path / "reports")
    resp = handle("POST", f"{BASE}/candidate/run", body=urlencode(
        _run_form_body(ctx_b)).encode(), ctx=ctx_b)
    assert resp.status == 303

    def rows(path):
        conn = connect(path)
        try:
            return {
                "members": sorted(
                    (r["code"], r["pool"], r["raw_score"], r["adj_score"],
                     r["status"]) for r in conn.execute(
                        "SELECT * FROM candidate_members")),
                "rejects": sorted(
                    (r["code"], r["stage"], r["reason"], r["plugin_id"])
                    for r in conn.execute("SELECT * FROM candidate_rejects")),
            }
        finally:
            conn.close()

    a, b = rows(db_a), rows(db_b)
    assert a["members"] and a["rejects"]
    assert a == b
