"""定时任务页（`/ops`）：取数与渲染。

本文件测的是**回执有没有被如实摆出来**，不是重算口径：

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 三条任务 == `schedule.JOB_NAMES` 且顺序一致 | 页面写死一份清单 |
| 摘要行 == `chain.summary_line` / `patrol.summary_line` 的同名字符串 | 页面自己拼一句摘要 |
| 回执从 `<报告根>/ops/latest-<job>.json` 读 | 回执目录写成 `<根>` 本身（少一层 `ops/`） |
| 没跑过 → `latest is None` 且页面写「还没有回执」 | 用 0 顶替 |
| 回执损坏 → 报原文，**不退回旧数字** | 读坏了就显示上一次的 |
| 历史来自 `job_runs`（新在前、按 limit 截断） | 自己造一份历史 |
| 库不在 → 任务清单照列、历史为空、页面不炸 | 直接抛 `sqlite3.OperationalError` |
| 下一次触发 == 从 `Job.calendar` 纯函数推算 | 写死一个时刻 |
| 「文件在位 / 还没写」只看**注入的** plist 目录 | 直读 `~/Library/LaunchAgents`（跟着开发机状态变，#56） |
| 页面不出现被二次转义的标签（`&lt;span`） | 把 HTML 喂给 `esc()` |

页面**只读**：这里所有用例都不建表、不写回执以外的文件；`job_runs` 也是用
`mode=ro` 连接的（数据层自证见 `OpsLab._ro`）。
"""

import http.client
import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from stocklab.labweb import app as labapp
from stocklab.labweb import ops_data, ops_render
from stocklab.labweb.ops_data import OpsLab
from stocklab.labweb.render import NAV_ITEMS
from stocklab.ops import chain, patrol, schedule
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-22T12:00:00+08:00"
#: 2026-09-22 是周二。
TZ_NOW = datetime.fromisoformat(NOW)


@pytest.fixture(autouse=True)
def launchd_dirs_are_tmp(tmp_path, monkeypatch):
    """plist 目录与日志目录**永不读这台机器的真状态**（错误日记 #56）。

    `OpsLab` 默认读 `~/Library/LaunchAgents/<label>.plist` 与 `data/logs/`。
    在开发机上 `ops schedule install` 过之后，那些文件就**真的在位**了：
    原来靠「plist 不存在所以页面出现 `s-fail`」而过关的断言会突然变红，
    而实现一点都没改。测试环境的 hermetic 要管到**读**，不只是写（#51/#55 是写）。

    显式传 `plist_dir` / `log_dir` 的用例不受影响。
    """
    from stocklab.config import paths

    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    return tmp_path / "LaunchAgents"


def _close_receipt(*, exit_code: int = 0, extra: dict | None = None) -> dict:
    payload = {
        "job": "close", "now": NOW, "db": "/tmp/x.db", "asof": "2026-09-22",
        "steps": [
            {"name": "ingest_index", "why": "日历的唯一来源", "exit_code": 0,
             "duration_s": 1.25, "stdout_tail": "ok", "stderr_tail": ""},
            {"name": "predict_run", "why": "明日预测落库", "exit_code": exit_code,
             "duration_s": 2.5, "stdout_tail": "", "stderr_tail": "boom"},
        ],
        "anomalies": ([] if exit_code == 0 else
                      [{"kind": "step_anomaly", "step": "predict_run",
                        "exit_code": exit_code, "detail": "boom"}]),
        "exit_code": exit_code, "ok": exit_code == 0,
    }
    payload.update(extra or {})
    return payload


def _db(tmp_path, *, runs: tuple = ()) -> Path:
    """一份最小的库：只有 `job_runs` 有用（页面显示历史靠它）。"""
    path = tmp_path / "ops-page.db"
    init_db(path)
    if runs:
        c = connect(path)
        for job_name, status, started, finished, detail in runs:
            c.execute(
                "INSERT INTO job_runs (job_name, scheduled_at, started_at, status,"
                " finished_at, detail) VALUES (?,?,?,?,?,?)",
                (job_name, started, started, status, finished, detail))
        c.commit()
        c.close()
    return path


def _write_receipt(root: Path, job_name: str, payload: dict) -> Path:
    path = root / "ops" / f"latest-{job_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _job(view: dict, name: str) -> dict:
    return next(j for j in view["jobs"] if j["name"] == name)


# ---------- 取数 ----------

def test_view_lists_the_three_jobs_in_schedule_order(tmp_path):
    view = OpsLab(_db(tmp_path), report_dir=tmp_path / "reports").view(now=TZ_NOW)
    assert [j["name"] for j in view["jobs"]] == list(schedule.JOB_NAMES)
    for job in view["jobs"]:
        spec = schedule.JOB_BY_NAME[job["name"]]
        assert job["window"] == spec.window and job["label"] == spec.label
        assert job["argv"] == schedule.build_argv(spec)
        assert job["plist"] == str(schedule.plist_path(spec))


def test_receipt_is_read_from_the_report_root_ops_subdir(tmp_path):
    root = tmp_path / "reports"
    _write_receipt(root, "close", _close_receipt())
    view = OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW)
    job = _job(view, "close")
    assert job["receipt_present"] is True and job["latest_error"] is None
    assert job["receipt_path"] == str(root / "ops" / "latest-close.json")
    assert job["exit_code"] == 0 and job["ok"] is True
    assert [s["name"] for s in job["steps"]] == ["ingest_index", "predict_run"]
    # 摘要行与 launchd 日志里那一行**同源**（同一个函数，不另拼一句）
    assert job["summary"] == chain.summary_line(job["latest"])
    assert job["summary"].startswith("close: exit=0")


def test_patrol_receipt_uses_patrols_own_summary_line(tmp_path):
    root = tmp_path / "reports"
    payload = {"job": "patrol", "now": NOW, "exit_code": 1, "ok": False,
               "checks": {name: {"status": "ok"} for name in patrol.CHECK_ORDER},
               "calendar": {"status": "ok"}, "latest_closed_session": {"date": "2026-09-22"},
               "steps": [], "anomalies": [{"kind": "step_anomaly", "detail": "x"}]}
    _write_receipt(root, "patrol", payload)
    job = _job(OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW), "patrol")
    assert job["summary"] == patrol.summary_line(payload)
    assert job["summary"].startswith("patrol: exit=1")


def test_missing_receipt_is_not_a_zero(tmp_path):
    view = OpsLab(_db(tmp_path), report_dir=tmp_path / "reports").view(now=TZ_NOW)
    job = _job(view, "monthly")
    assert job["latest"] is None and job["latest_error"] is None
    assert job["receipt_present"] is False
    assert job["exit_code"] is None and job["ok"] is None
    assert job["summary"] is None
    assert job["steps"] == [] and job["anomalies"] == []


def test_broken_receipt_is_reported_instead_of_falling_back(tmp_path):
    root = tmp_path / "reports"
    path = _write_receipt(root, "close", _close_receipt())
    path.write_text("{ this is not json", encoding="utf-8")
    job = _job(OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW), "close")
    assert job["latest"] is None and job["receipt_present"] is True
    assert "JSONDecodeError" in job["latest_error"]
    assert job["exit_code"] is None           # 不退回上一次的数字


def test_history_comes_from_job_runs_newest_first_and_is_capped(tmp_path):
    runs = (("close", "ok", "2026-09-19T15:30:00+08:00", "2026-09-19T15:31:00+08:00", "close: exit=0"),
            ("patrol", "failed", "2026-09-22T10:00:00+08:00", "2026-09-22T10:00:20+08:00", "patrol: exit=2"))
    path = _db(tmp_path, runs=runs)
    view = OpsLab(path, report_dir=tmp_path / "reports", history_limit=1).view(now=TZ_NOW)
    close = _job(view, "close")
    assert [r["detail"] for r in close["history"]] == ["close: exit=0"]
    assert close["last_run"]["status"] == "ok"
    patrol_job = _job(view, "patrol")
    assert len(patrol_job["history"]) == 1 and patrol_job["history"][0]["status"] == "failed"
    # 全库历史是**全部作业**共用的表（含采集类作业），不是只有这三条链
    assert [r["run_id"] for r in view["runs"]] == sorted(
        [r["run_id"] for r in view["runs"]], reverse=True)


def test_missing_db_still_lists_jobs_and_an_empty_history(tmp_path):
    view = OpsLab(tmp_path / "nope.db", report_dir=tmp_path / "reports").view(now=TZ_NOW)
    assert view["db_exists"] is False
    assert [j["name"] for j in view["jobs"]] == list(schedule.JOB_NAMES)
    assert view["runs"] == [] and all(j["history"] == [] for j in view["jobs"])


# ---------- 下一次触发（纯函数） ----------

def test_next_trigger_close_is_the_next_weekday_1530():
    spec = schedule.JOB_BY_NAME["close"]
    assert ops_data.next_trigger(spec.calendar, TZ_NOW) == "2026-09-22T15:30:00+08:00"
    after = datetime.fromisoformat("2026-09-22T15:31:00+08:00")
    assert ops_data.next_trigger(spec.calendar, after) == "2026-09-23T15:30:00+08:00"
    friday_evening = datetime.fromisoformat("2026-09-25T23:59:00+08:00")
    assert ops_data.next_trigger(spec.calendar, friday_evening) == \
        "2026-09-28T15:30:00+08:00"           # 跳过周末


def test_next_trigger_patrol_and_monthly():
    patrol_cal = schedule.JOB_BY_NAME["patrol"].calendar
    # 12:00 整点是触发点本身 → 严格大于，所以是 12:30
    assert ops_data.next_trigger(patrol_cal, TZ_NOW) == "2026-09-22T12:30:00+08:00"
    assert ops_data.next_trigger(schedule.JOB_BY_NAME["monthly"].calendar, TZ_NOW) == \
        "2026-10-01T08:00:00+08:00"


def test_next_trigger_returns_none_when_the_shape_is_unknown():
    """认不出的形态**不猜**（缺 Minute 的条目由 launchd 解释，不归我）。"""
    assert ops_data.next_trigger(({"Hour": 9},), TZ_NOW) is None
    assert ops_data.next_trigger(({"Hour": 9, "Minute": 0, "Second": 5},), TZ_NOW) is None
    assert ops_data.next_trigger((), TZ_NOW) is None


# ---------- 渲染 ----------

def _html(tmp_path, *, runs: tuple = (), plist: bool = False) -> str:
    root = tmp_path / "reports"
    _write_receipt(root, "close", _close_receipt(exit_code=1))
    if plist:
        for spec in schedule.JOBS:
            # 写进**被夹具指到 tmp 的那个目录**（就是 `OpsLab` 默认读的那个）
            schedule.write_plist(spec, plist_dir=schedule.LAUNCH_AGENTS_DIR,
                                 logs=schedule.log_dir())
    view = OpsLab(_db(tmp_path, runs=runs), report_dir=root).view(now=TZ_NOW)
    return ops_render.ops_page(view, base="/lab", built_at=NOW)


def test_page_shows_the_three_windows_and_the_receipt(tmp_path):
    html = _html(tmp_path)
    for job in schedule.JOBS:
        assert job.window in html and job.name in html
    # 摘要行逐字出现；退出码 1 是**黄**（跑完了但有异常），2 才是红（没跑完/断链）
    assert chain.summary_line(_close_receipt(exit_code=1)) in html
    assert "exit=1" in html and "s-warn" in html
    # 逐步表在
    assert "ingest_index" in html and "predict_run" in html
    assert "1.25 s" in html
    # 没跑过的两条写「还没有回执」，而不是 0（统计的句子只出现在任务段里）
    assert html.count("还没有回执 —— 这条任务在本机还没跑过") == 2


def test_plist_pill_follows_the_injected_dir(tmp_path):
    """「文件在位 / 还没写」只看**注入的**目录，不看开发机真装没装（错误日记 #56）。"""
    empty = _html(tmp_path / "empty", plist=False)
    assert empty.count("还没写（跑 ops schedule install）") == 3
    assert "文件在位" not in empty

    installed = _html(tmp_path / "installed", plist=True)
    assert installed.count("文件在位") == 3
    assert "还没写" not in installed


def test_page_reports_a_broken_receipt_and_a_missing_db(tmp_path):
    root = tmp_path / "reports"
    _write_receipt(root, "close", _close_receipt()).write_text("nope", encoding="utf-8")
    view = OpsLab(tmp_path / "nope.db", report_dir=root).view(now=TZ_NOW)
    html = ops_render.ops_page(view, base="/lab", built_at=NOW)
    assert "回执读不出来" in html
    assert "库不在" in html
    assert "job_runs` 里还没有记录" in html


def test_page_does_not_double_escape_its_own_markup(tmp_path):
    html = _html(tmp_path)
    for token in ("&lt;span", "&lt;code", "&lt;table", "&lt;li"):
        assert token not in html


def test_nav_rail_links_the_page():
    assert ("/ops", "定时任务") in NAV_ITEMS


# ---------- 路由 ----------

def _server_ctx(path: Path, *, with_ops: bool = True):
    from stocklab.labweb.data import Lab
    from stocklab.labweb.tokens import TokenSigner

    return labapp.Context(
        lab=Lab(path, asof="2026-09-22"), signer=TokenSigner(b"x" * 32),
        base_path="/lab", ops=(OpsLab(path) if with_ops else None))


def test_route_serves_the_page(tmp_path, loopback_http, monkeypatch):
    """真起服务打一次 `GET /lab/ops`（路由接线在 `app._get` 里）。"""
    root = tmp_path / "reports"
    monkeypatch.setattr("stocklab.config.paths.REPORT_DIR", root)
    _write_receipt(root, "close", _close_receipt())
    path = _db(tmp_path)
    server = labapp.make_server(host="127.0.0.1", port=0, ctx=_server_ctx(path))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/lab/ops")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        assert res.status == 200
        assert "定时任务" in body and "每交易日 15:30" in body
        # 导航条要能点到它（同页里的 rail 就是全站导航）
        assert 'href="/lab/ops"' in body
        assert 'href="/lab/paper"' in body
    finally:
        conn.close()
        server.shutdown()
        server.server_close()


def test_route_without_the_facade_is_a_readable_error(tmp_path, loopback_http):
    server = labapp.make_server(host="127.0.0.1", port=0,
                                ctx=_server_ctx(_db(tmp_path), with_ops=False))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/lab/ops")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        assert res.status == 500 and "ctx.ops" in body
    finally:
        conn.close()
        server.shutdown()
        server.server_close()


# ---------- T4：被跳过 / 被拒绝都要能一眼看出来（P46） ----------
#
# patrol 的「有意跳过」与 close 的「拒绝执行」是**两件不同的事**，但都属于
# 「没跑」。页面上必须各有一条可读文案，而且都**不许折进 `more()`** ——
# 折起来就等于用一个展开动作掩盖了异常（ERROR_DIARY #54 的教训）。

def _patrol_receipt_with_a_skip(reason: str = "asof=2026-09-22 就是**今天**：…") -> dict:
    return {
        "job": "patrol", "now": NOW, "exit_code": 1, "ok": False,
        "checks": {name: {"status": "ok"} for name in patrol.CHECK_ORDER},
        "calendar": {"status": "ok"}, "latest_closed_session": {"date": "2026-09-22"},
        "steps": [], "anomalies": [],
        "plan": {"steps": [], "skipped": [{"step": "predict_run", "reason": reason}]},
    }


def test_view_exposes_the_planned_skips(tmp_path):
    root = tmp_path / "reports"
    payload = _patrol_receipt_with_a_skip()
    _write_receipt(root, "patrol", payload)
    job = _job(OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW), "patrol")
    assert job["plan_skipped"] == [{"step": "predict_run", "reason": payload["plan"]["skipped"][0]["reason"]}]


def test_a_receipt_without_a_plan_shows_no_skips(tmp_path):
    """反面对照：没有 `plan.skipped` 的回执（close）不许凭空长出「跳过」块。"""
    root = tmp_path / "reports"
    _write_receipt(root, "close", _close_receipt())
    job = _job(OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW), "close")
    assert job["plan_skipped"] == []


def test_page_shows_the_skip_reason_without_expanding_anything(tmp_path):
    """跳过块**在折叠之外**：真实原因必须出现在 HTML 里，读者不用点开任何东西。"""
    root = tmp_path / "reports"
    _write_receipt(root, "patrol",
                   _patrol_receipt_with_a_skip("asof=2026-09-22 就是**今天**"))
    html = ops_render.ops_page(
        OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW),
        base="/lab", built_at=NOW)
    assert "有意跳过" in html
    assert "predict_run" in html
    assert "就是**今天**" not in html and "<b>今天</b>" in html   # 走 rich()，不双转义


def test_page_without_a_skip_does_not_claim_one(tmp_path):
    """反向自检（ERROR_DIARY #43）：整页扫描的断言要有「不该出现时会红」的对照。"""
    root = tmp_path / "reports"
    _write_receipt(root, "close", _close_receipt())
    html = ops_render.ops_page(
        OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW),
        base="/lab", built_at=NOW)
    assert "有意跳过" not in html


def test_page_shows_a_refused_round_as_refused(tmp_path):
    """「拒绝执行」已有文案，这里把它钉住 —— 与「跳过」共享同一条可见性纪律。"""
    root = tmp_path / "reports"
    _write_receipt(root, "close", {
        "job": "close", "now": NOW, "exit_code": 2, "ok": False, "steps": [],
        "refused": "今天（2026-09-22 14:07）还没收盘 → 拒绝执行",
        "anomalies": [{"kind": "before_close", "detail": "还没收盘"}],
    })
    html = ops_render.ops_page(
        OpsLab(_db(tmp_path), report_dir=root).view(now=TZ_NOW),
        base="/lab", built_at=NOW)
    assert "<b>拒绝执行</b>" in html and "还没收盘" in html
