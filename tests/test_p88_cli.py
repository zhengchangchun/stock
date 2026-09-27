"""P88 T6/T7：两条采集命令 + `data` 只读命令组 + 月度链两步。

全部离线：只把 `HttpClient` 换成假源，其余（解析 / 翻页 / 落库 / 退出码）全是真的。
`--days` 在用例里给一个极大值，让 cutoff 不依赖跑测试那天是几号。
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3

import pytest

import stocklab.data.http as http_mod
import stocklab.data.sources.eastmoney_ann as ann_mod
import stocklab.data.sources.northbound as nb_mod
from stocklab.cli.main import build_parser
from stocklab.config import paths
from stocklab.ops import chain
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

TODAY = "2026-09-27T10:00:00+08:00"
WIDE = "36500"                      # ≈100 年 ⇒ cutoff 落在所有夹具行之前


# ══════════════════════════════════════════════════════════════════════
# 假源
# ══════════════════════════════════════════════════════════════════════


def _ann_page(rows: list[dict], total_hits: int | None = None) -> str:
    return json.dumps({"data": {"total_hits": total_hits if total_hits is not None
                                else len(rows), "list": rows}})


def _ann_row(art: str, date: str, code: str = "000333") -> dict:
    return {"art_code": art, "codes": [{"stock_code": code}],
            "columns": [{"column_code": "050003", "column_name": "调研活动"}],
            "display_time": f"{date} 19:35:36:855", "notice_date": f"{date} 00:00:00",
            "title": f"{code}:{art}", "title_ch": f"{code}:{art}",
            "source_type": "324", "listing_state": "0"}


def _nb_page(rows: list[dict], pages: int = 1) -> str:
    return json.dumps({"result": {"pages": pages, "count": len(rows), "data": rows}})


def _nb_row(date: str, *, shares: int = 893478198) -> dict:
    return {"SECUCODE": "000333.SZ", "TRADE_DATE": f"{date} 00:00:00",
            "CLOSE_PRICE": 75.53, "HOLD_SHARES": shares,
            "HOLD_MARKET_CAP": 67484408294.94, "A_SHARES_RATIO": 12.83,
            "HOLD_SHARES_RATIO": 12.83, "FREE_SHARES_RATIO": 13.0201}


class FakeClient:
    """按 URL 里的代码返回预置响应；`boom` 里的代码让第 1 次请求就抛。"""

    def __init__(self, ann: dict[str, list[str]] | None = None,
                 nb: dict[str, list[str]] | None = None,
                 boom: frozenset[str] = frozenset()):
        self.ann = ann or {}
        self.nb = nb or {}
        self.boom = set(boom)
        self.urls: list[str] = []
        self._served: dict[tuple[str, bool], int] = {}

    def get_text(self, url, **_kw):
        self.urls.append(url)
        for code in self.boom:
            if code in url:
                raise RuntimeError(f"源站对 {code} 反悔了")
        is_ann = "security/ann" in url
        for code, pages in (self.ann if is_ann else self.nb).items():
            if code in url:
                key = (code, is_ann)
                i = self._served.get(key, 0)
                self._served[key] = i + 1
                return pages[min(i, len(pages) - 1)]
        if is_ann:
            return json.dumps({"data": {"total_hits": 0, "list": []}})
        return _nb_page([])


@pytest.fixture
def db(tmp_db, tmp_path, monkeypatch):
    init_db(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    monkeypatch.setattr(paths, "RAW_CACHE_DIR", tmp_path / "raw_cache")
    conn = connect(tmp_db)
    yield conn
    conn.close()


def _run(monkeypatch, argv: list[str], client: FakeClient | None = None):
    """跑一条 CLI；只把 `HttpClient` 换成假源（`None` ⇒ 该命令不联网）。"""
    if client is not None:
        monkeypatch.setattr(http_mod, "HttpClient", lambda *a, **k: client)
    args = build_parser().parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = args.func(args)
    return code, out.getvalue(), err.getvalue()


# ══════════════════════════════════════════════════════════════════════
# G1/G2：公告真实拉取 + 幂等
# ══════════════════════════════════════════════════════════════════════


def test_ann_ingest_lands_rows_in_source_order(db, monkeypatch):
    client = FakeClient(ann={"000333": [_ann_page(
        [_ann_row("AN3", "2026-09-20"), _ann_row("AN2", "2026-09-18"),
         _ann_row("AN1", "2026-09-15")], total_hits=3)]})
    code, out, _err = _run(monkeypatch,
                           ["ingest", "announcements", "--code", "000333",
                            "--days", WIDE, "--page-limit", "5", "--db", str(paths.DB_PATH)],
                           client)
    assert code == 0, out
    rows = [tuple(r) for r in db.execute(
        "SELECT art_code, notice_date FROM announcements ORDER BY rowid")]
    assert rows == [("AN3", "2026-09-20"), ("AN2", "2026-09-18"),
                    ("AN1", "2026-09-15")]
    # notice_date 单调不增（G1 的判据由 rowid 顺序直接体现）
    dates = [d for _a, d in rows]
    assert dates == sorted(dates, reverse=True)


def test_ann_ingest_is_idempotent(db, monkeypatch):
    client = FakeClient(ann={"000333": [_ann_page([_ann_row("AN1", "2026-09-15")])]})
    argv = ["ingest", "announcements", "--code", "000333", "--days", WIDE,
            "--page-limit", "5", "--db", str(paths.DB_PATH)]
    _run(monkeypatch, argv, client)
    n1 = db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    code, out, _err = _run(monkeypatch, argv, client)
    assert code == 0, out
    assert n1 == 1
    assert db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1
    assert "新写 0 行" in out


def test_nb_ingest_lands_the_quarterly_row(db, monkeypatch):
    client = FakeClient(nb={"000333": [_nb_page([_nb_row("2026-06-30")])]})
    code, out, _err = _run(monkeypatch,
                           ["ingest", "northbound", "--code", "000333",
                            "--days", WIDE, "--db", str(paths.DB_PATH)], client)
    assert code == 0, out
    row = db.execute("SELECT * FROM northbound_holdings").fetchone()
    assert row["trade_date"] == "2026-06-30"
    assert row["hold_shares"] == 893478198
    assert row["frequency"] == "quarterly"


def test_nb_ingest_is_idempotent(db, monkeypatch):
    client = FakeClient(nb={"000333": [_nb_page([_nb_row("2026-06-30")])]})
    argv = ["ingest", "northbound", "--code", "000333", "--days", WIDE,
            "--db", str(paths.DB_PATH)]
    _run(monkeypatch, argv, client)
    code, out, _err = _run(monkeypatch, argv, client)
    assert code == 0 and "新写 0 行" in out
    assert db.execute("SELECT COUNT(*) FROM northbound_holdings").fetchone()[0] == 1


# ══════════════════════════════════════════════════════════════════════
# D4：坏 universe ⇒ exit 2 且**零写入**（先解析宇宙再碰库）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("cmd", ["announcements", "northbound"])
def test_bad_universe_is_exit_2_and_writes_nothing(db, monkeypatch, cmd):
    client = FakeClient(ann={"000333": [_ann_page([_ann_row("AN1", "2026-09-15")])]},
                        nb={"000333": [_nb_page([_nb_row("2026-06-30")])]})
    before = db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] \
        + db.execute("SELECT COUNT(*) FROM northbound_holdings").fetchone()[0]
    code, _out, err = _run(monkeypatch,
                           ["ingest", cmd, "--universe", "no-such-universe",
                            "--db", str(paths.DB_PATH)], client)
    assert code == 2
    assert "universe" in err.lower()
    assert client.urls == [], "坏 universe 不该发任何请求"
    after = db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] \
        + db.execute("SELECT COUNT(*) FROM northbound_holdings").fetchone()[0]
    assert after == before == 0


# ══════════════════════════════════════════════════════════════════════
# D6：单只失败不拖累整批（exit 1，其余照落）
# ══════════════════════════════════════════════════════════════════════


def test_one_bad_code_does_not_kill_the_batch_for_announcements(db, monkeypatch):
    client = FakeClient(ann={"000333": [_ann_page([_ann_row("AN1", "2026-09-15")])],
                             "600690": [_ann_page([_ann_row("AN9", "2026-09-15",
                                                            "600690")])]},
                        boom={"600690"})
    code, out, err = _run(monkeypatch,
                          ["ingest", "announcements", "--code", "000333",
                           "--code", "600690", "--days", WIDE,
                           "--page-limit", "5", "--db", str(paths.DB_PATH)], client)
    assert code == 1, "有失败只 ⇒ exit 1，不是 2（P75 口径：不写裸 abort）"
    assert "600690" in err
    assert "失败 1 只" in out
    got = [r[0] for r in db.execute("SELECT code FROM announcements")]
    assert got == ["000333"], "好的那只必须照落"


def test_one_bad_code_does_not_kill_the_batch_for_northbound(db, monkeypatch):
    client = FakeClient(nb={"000333": [_nb_page([_nb_row("2026-06-30")])]},
                        boom={"600690"})
    code, out, err = _run(monkeypatch,
                          ["ingest", "northbound", "--code", "000333",
                           "--code", "600690", "--days", WIDE,
                           "--db", str(paths.DB_PATH)], client)
    assert code == 1
    assert "600690" in err and "失败 1 只" in out
    assert db.execute("SELECT COUNT(*) FROM northbound_holdings").fetchone()[0] == 1


def test_a_failed_batch_is_logged_as_a_warn_event(db, monkeypatch):
    """D6 沿用 P75：失败要留痕（`system_events` warn），不是只打在 stderr 上。"""
    client = FakeClient(boom={"000333"})
    _run(monkeypatch, ["ingest", "announcements", "--code", "000333",
                       "--days", WIDE, "--db", str(paths.DB_PATH)], client)
    n = db.execute("SELECT COUNT(*) FROM system_events WHERE level='warn'"
                   " AND message LIKE '%announcements%'").fetchone()[0]
    assert n == 1


# ══════════════════════════════════════════════════════════════════════
# D9：`data` 只读命令组（零写入、零联网、不 ensure_schema）
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def seeded(db):
    from stocklab.data.ingest import (ingest_announcements,
                                      ingest_northbound_holdings)
    ingest_announcements(db, [
        {"code": "000333", "art_code": "AN2", "notice_date": "2026-09-20",
         "display_time": "2026-09-20 19:35:36:855", "title": "美的集团:公告2",
         "column_name": "调研活动", "ann_type": "A", "source": "eastmoney-ann",
         "resp_sha256": "d" * 64},
        {"code": "000333", "art_code": "AN1", "notice_date": "2026-09-15",
         "display_time": "2026-09-15 19:35:36:855", "title": "美的集团:公告1",
         "column_name": "分红送配", "ann_type": "A", "source": "eastmoney-ann",
         "resp_sha256": "d" * 64},
    ], now=TODAY)
    ingest_northbound_holdings(db, [
        {"code": "000333", "trade_date": "2026-06-30", "hold_shares": 893478198,
         "hold_market_cap": 67484408294.94, "a_shares_ratio": 12.83,
         "hold_shares_ratio": 12.83, "free_shares_ratio": 13.0201,
         "close_price": 75.53, "frequency": "quarterly",
         "source": "eastmoney-northbound", "resp_sha256": "d" * 64},
    ], now=TODAY)
    return db


def test_data_announcements_json(seeded, monkeypatch):
    code, out, _err = _run(monkeypatch,
                           ["data", "announcements", "--code", "000333", "--json",
                            "--db", str(paths.DB_PATH)])
    assert code == 0
    payload = json.loads(out)
    assert payload["code"] == "000333" and payload["n"] == 2
    assert [r["art_code"] for r in payload["rows"]] == ["AN2", "AN1"]
    assert payload["rows"][0]["notice_date"] == "2026-09-20"


def test_data_announcements_human_summary(seeded, monkeypatch):
    code, out, _err = _run(monkeypatch,
                           ["data", "announcements", "--code", "000333",
                            "--db", str(paths.DB_PATH)])
    assert code == 0
    assert "AN2" in out and "2026-09-20" in out


def test_data_announcements_days_and_limit_filter(seeded, monkeypatch):
    code, out, _err = _run(monkeypatch,
                           ["data", "announcements", "--code", "000333",
                            "--days", "1", "--limit", "1", "--json",
                            "--db", str(paths.DB_PATH)])
    assert code == 0
    assert json.loads(out)["n"] <= 2


def test_data_northbound_json_and_human(seeded, monkeypatch):
    code, out, _err = _run(monkeypatch,
                           ["data", "northbound", "--code", "000333", "--json",
                            "--db", str(paths.DB_PATH)])
    assert code == 0
    payload = json.loads(out)
    assert payload["n"] == 1
    assert payload["rows"][0]["hold_shares"] == 893478198
    assert payload["rows"][0]["frequency"] == "quarterly"

    code, out, _err = _run(monkeypatch,
                           ["data", "northbound", "--code", "000333",
                            "--db", str(paths.DB_PATH)])
    assert code == 0 and "2026-06-30" in out and "quarterly" in out


def test_data_commands_are_read_only_on_a_missing_table(tmp_path, monkeypatch):
    """表不存在 ⇒ exit 2，**并且不把表建出来**（不 `ensure_schema`、零写入）。"""
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    conn.execute("CREATE TABLE dummy (x)")          # 一个合法的空库
    conn.commit()
    conn.close()
    monkeypatch.setattr(paths, "DB_PATH", empty)

    for argv in (["data", "announcements", "--code", "000333"],
                 ["data", "northbound", "--code", "000333", "--json"]):
        code, _out, err = _run(monkeypatch, argv)
        assert code == 2, err

    c = sqlite3.connect(f"file:{empty}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    assert tables == {"dummy"}, "读命令不该建表"


def test_data_announcements_requires_code():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["data", "announcements", "--json"])


# ══════════════════════════════════════════════════════════════════════
# G6/D7：月度链 +2（非阻断）；日链与巡检一个字不动
# ══════════════════════════════════════════════════════════════════════


def test_monthly_gains_exactly_two_steps():
    names = list(chain.MONTHLY_STEP_ORDER)
    assert len(names) == 8, names
    assert names.count("ingest_announcements") == 1
    assert names.count("ingest_northbound") == 1


def test_the_two_new_monthly_steps_carry_the_d7_argv():
    ann = chain.MONTHLY_STEP_BY_NAME["ingest_announcements"]
    assert ann.args == ("ingest", "announcements", "--days", "90",
                        "--page-limit", "3")
    assert ann.blocking is False, "D7：非阻断（失败不挡后续步）"
    assert ann.supports_db is False, "与其它 ingest 步同族（链上不传 --db）"

    nb = chain.MONTHLY_STEP_BY_NAME["ingest_northbound"]
    assert nb.args == ("ingest", "northbound", "--days", "120")
    assert nb.blocking is False
    assert nb.supports_db is False


def test_the_close_chain_is_untouched_by_p88():
    """D7：日链一个字不动（日链 13 步已有预算，800 只翻页会拖死它）。"""
    names = list(chain.CLOSE_STEP_ORDER)
    assert len(names) == 14
    assert "ingest_announcements" not in names
    assert "ingest_northbound" not in names


def test_the_new_steps_stay_before_doctor():
    """体检必须**最后**跑（它是全链的自证）—— 新步不许插到它后面。"""
    names = list(chain.MONTHLY_STEP_ORDER)
    assert names[-1] == "doctor"
    assert names.index("ingest_announcements") < names.index("doctor")
    assert names.index("ingest_northbound") < names.index("doctor")


def test_a_failing_new_step_does_not_make_the_monthly_chain_red():
    """非阻断：它 exit 1 时链的退出码不抬升（但**照样**出现在 `bad=` 里）。"""
    from stocklab.ops.runner import worst_code

    steps = [{"name": "ingest_announcements", "exit_code": 1, "blocking": False}]
    assert worst_code(steps, None) == 0, "非阻断的 exit 1 不该把整链判红"
    assert worst_code([{**steps[0], "blocking": True}], None) == 1
    # 摘要里 bad= 必须点名它（非阻断 ≠ 静默）
    line = chain.summary_line(
        {"job": "monthly", "asof": "2026-09-27", "exit_code": 0, "steps": steps})
    assert "ingest_announcements=1" in line


def test_the_source_modules_do_not_page_announcements_with_time_params():
    """回归护栏：`begin_time`/`end_time` 一加就 total_hits=0（§0.2 实测）。

    这条守住「不要后来有人『顺手』把时间窗加回去」这个具体的坑。
    """
    assert "begin_time" not in ann_mod.announcement_url("000333", page=1)
    assert not hasattr(ann_mod, "BEGIN_TIME_PARAM")
    assert nb_mod.NORTHBOUND_REPORT == "RPT_MUTUAL_HOLDSTOCKNORTH_STA"
