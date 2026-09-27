"""`ops close` / `ops monthly`（P38）＋ `ops weekly` / `ops quarterly`（P89）：
把定时任务的**执行顺序**也搬进项目。

## 四条链的 asof 语义（P89 加的两条与收盘链**刻意不同**）

| 链 | asof | 为什么 |
|---|---|---|
| `close` | **运行当天** | 它负责把当天数据落地；收盘前拒绝执行（半截 bar 是 append-only） |
| `monthly` / `weekly` / `quarterly` | **最近已收盘交易日** | 维护/汇总活：读某一天的数据做汇总，运行当天可能休市（10-05 恰好是周一、国庆） |

`weekly` / `quarterly` 的 asof **取不到就整轮拒绝**（exit 2，`resolve_asof` 的
fail-closed）：猜一个日期跑会得到一份**看起来正常的空报告**，而且不报错。
两条链都**不做非交易日跳过**（与 `monthly` 同构）：休市周跑出来的是与上周同样的
读数 —— 无害、可复核；跳过反而会「连续两周没扫」。详见 D2 / D3。

## 为什么要搬（还有一次真出过的事故）

2026-09-22 用户拍板：调度不用 nanobot cron —— **时钟交给系统**（launchd，见
`ops/schedule.py`），「什么时候跑」由 plist 定义，「跑什么、按什么顺序、什么算失败」
由本模块定义。理由与 ADR-019 同源：写在 agent 任务正文里的知识改不了一次、测不了、
丢了只能手抄（09-18 网关重置丢过一次）。

**事故**：旧正文在跑 ingest **之前**就算 `LATEST = max(bars_daily.date)`，而 15:30 那一刻
库里最新的一行还是**上一个交易日** —— 于是 `session backfill-close` / `predict run` /
`review daily` / `paper step --asof $LATEST` 全部在补昨天。幂等，所以不报错、不留痕，
只是「今天的链看起来跑过了」。本模块的口径改成 **`asof` = 运行当天**（收盘日），
收盘前**拒绝执行**（exit 2）—— 半截 bar 与当日 LIVE 预测都是 append-only，写错了
退不回来。

## 三条判据（都有测试钉住）

1. **非交易日整轮跳过**：判定复用巡检那一份（`patrol.session_day`：周末 → 休市表 →
   交易日历），`False` 就写一行 `job_runs(skipped)` 并 exit 0 —— 与 07 §非交易日全部
   跳过一致；
2. **收盘前拒绝执行**：`is_trade_date_closed(今天, now)` 为假 → exit 2，**一步都不跑**
   （连备份都不做：备份会覆盖 `data/backups/` 里当天的文件）；
3. **跑完再体检一次**：退出码取「链本身」与「事后体检（`patrol.check_db`）」的较大者
   —— 链跑完了但数据不健康（`missing`/`stale`）同样是红的。这一条让 cron 侧只剩
   「非 0 就贴回来看」。

### 判据说「不知道」时照跑（不是漏判）

`session_day()` 在「休市表没覆盖今年、日历最大日 < 今天」时返回 `None`（判不了），
本模块**只在明确 `False` 时跳过**。这是刻意选的：正常交易日在 15:30 那一刻，日历里
**还没有今天**（`ingest index` 就是本轮的第一步），所以 `None` 是**每个交易日的常态**
—— 拿它当闸门等于这条链永远不跑。「今天休市」的可靠证据只有两种：周末，或休市表
（`market_holidays`，ADR-013）覆盖到的节假日。

## `report_dir` 只回答一件事

它 = **报告根目录**（默认 `paths.REPORT_DIR`）：⑤ 复盘报告读 `<根>/<date>-review.md`，
回执写 `<根>/ops/latest-<job>.json`。两处用同一个根，才有「回执里说缺的 ⑤，就是页面上
那一份报告」这种一致性；一个参数两个含义迟早让两边各看一份文件。

## 与巡检的分工

| | 问题 | 触发 |
|---|---|---|
| `ops patrol --fix` | 「**现在**缺哪一步」→ 补哪一步（只补缺口） | 盘中每 30 分钟 |
| `ops close` | 「今天这一天**整条链**跑一遍」→ 顺序固定、asof 固定 | 工作日 15:30（非交易日整轮跳过） |

两者共用 `ops/runner.py`（同一个执行器、同三条停止线、同一份跨库守卫）。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.config import paths
from stocklab.ops import journal, window_report
from stocklab.ops.patrol import check_db, ro_connect, session_day
from stocklab.ops.runner import (EXIT_ANOMALY, EXIT_BLOCKED, EXIT_OK, Step,
                                 cross_db_refusal, default_runner, run_steps,
                                 worst_code)
from stocklab.session.tick import is_trade_date_closed
from stocklab.store.migrate import backup_db
from stocklab.verify.pending import CLOSED_RULE, latest_closed_session

TZ = ZoneInfo("Asia/Shanghai")

#: 收盘链整轮预算：实测全量约 90 s（ingest 段占大头），给 15 分钟余量。
CLOSE_TIMEOUT_S = 900.0

#: 月度刷新整轮预算：长窗 `ingest bars --days 12000` 实测 22 s，但它是联网长跑。
MONTHLY_TIMEOUT_S = 1800.0

#: 关闭链步骤（**顺序 = 依赖顺序**，一个都不能少）。
#:
#: `supports_db` 为 `False` 的三类是「不接受 `--db`」的命令：采集（固定写
#: `paths.DB_PATH`）与 `doctor`（它**不接受任何参数**）。它们出现在计划里就意味着
#: 这条链只能在默认库上跑 —— 由 `runner.cross_db_refusal` 拒绝执行，不是靠自觉。
CLOSE_STEPS: tuple[Step, ...] = (
    Step("ingest_index", ("ingest", "index"), False,
         "基准 sh000300 日线，并**顺带前滚交易日历**（日历是指数日线唯一的产物）"),
    Step("ingest_index_500", ("ingest", "index", "--symbol", "sh000905"), False,
         "第二个基准 sh000905（中证500，P55）—— **同一采集路径、同一 `adj_mode='none'`**，"
         "紧跟 sh000300：日历由两个指数共同前滚（`INSERT OR IGNORE`，只增不减）"),
    Step("ingest_bars", ("ingest", "bars", "--days", "30"), False,
         "21 只标的的日 K 增量（长窗 12000 天是月度刷新的事：日链会撞源站脏行）"),
    Step("ingest_actions", ("ingest", "actions", "--start", "{action_start}"), False,
         "除权事件 + 复权因子链（回看 400 天覆盖新事件；复权口径是预测的输入）"),
    Step("ingest_valuation", ("ingest", "valuation", "--days", "30"), False,
         "PIT 估值表（东财 datacenter；首写保留，增量口径）"),
    Step("ingest_moneyflow", ("ingest", "moneyflow", "--days", "30"), False,
         "资金流表（新浪 MoneyFlow；首写保留，增量口径）"),
    Step("session_tick", ("session", "tick"), True,
         "采一次盘中/收盘快照（append-only；盘中跑也就是多几个时点）"),
    Step("session_backfill_close", ("session", "backfill-close", "--date", "{asof}"),
         True,
         "用**当日收盘后**快照回填 amount/turnover（只补 NULL，取不到就不写）"),
    Step("predict_run", ("predict", "run", "--asof", "{asof}"), True,
         "明日预测落库（PIT：只用 <= asof 的数据；asof = 运行当天，不是库里最新那行）"),
    Step("verify_pending", ("verify", "pending"), True,
         "补齐「已到期却没有验证行」的预测（必须紧跟 predict run）"),
    Step("review_daily", ("review", "daily", "--date", "{asof}"), True,
         "当日复盘报告（离线只读，产出 reports/<asof>-review.md）"),
    Step("paper_step", ("paper", "step", "--asof", "{asof}"), True,
         "模拟盘按 asof 收盘推进一天（幂等：同日重跑不重复下单）"),
    Step("m2_daily", ("m2", "daily", "--asof", "{asof}"), True,
         "模块2 日更（P54）：通路 B → 通路 A（遍历**在飞**策略版本）→ 事后打分。"
         "**一步**：遍历在 CLI 里，因为「这一轮跑哪些版本」只有运行期才知道"
         "（`CLOSE_STEPS` 是静态元组）；没有在飞版本 ⇒ 记一行 `skipped`，不是失败"),
    Step("doctor", ("doctor",), False,
         "数据健康度报告（不接受任何参数；放在最后当全链的自证）"),
)

CLOSE_STEP_BY_NAME: dict[str, Step] = {s.name: s for s in CLOSE_STEPS}
CLOSE_STEP_ORDER: tuple[str, ...] = tuple(s.name for s in CLOSE_STEPS)

#: 月度刷新步骤。它替代 nanobot 侧原来的「C 月度刷新（日历+财报+长窗行情）」。
MONTHLY_STEPS: tuple[Step, ...] = (
    Step("calendar_holidays_fetch", ("calendar", "holidays", "fetch"), True,
         "抓上交所休市安排并落库（fail-closed：抓不到就不写，不是猜）"),
    Step("ingest_bars_long", ("ingest", "bars", "--days", "12000"), False,
         "长窗行情回补（捕获源站修订 / 覆盖当月新加入的标的；日链只跑 30 天）"),
    Step("ingest_index_500", ("ingest", "index", "--symbol", "sh000905",
                              "--days", "12000"), False,
         "第二个基准 sh000905 的**长窗**回补（P55）：与 `ingest bars --days 12000` "
         "同批 —— 日链只跑默认窗口，深历史是月度的事"),
    Step("ingest_financials", ("ingest", "financials"), False,
         "东财三表全量复核（季报频率，月度一次足够；已入库的期数 rows=0）"),
    # P88：公告 + 北向季度持股。**只挂月链**（D7）：日链 13 步已有预算，
    # 800 只逐只翻页会把它拖死；两者都是**非阻断**（失败不挡后续步）——
    # 它们补的是「以后要用的数据」，不是当天链路的依赖。
    Step("ingest_announcements", ("ingest", "announcements", "--days", "90",
                                  "--page-limit", "3"), False,
         "公告增量（东财 np-anotice；接口不支持时间窗 ⇒ 翻页到 cutoff 即停，"
         "page-limit=3 是硬上限，跑满记 truncated 不报错）。**非阻断**："
         "公告本档只落表、不进任何消费路径（D8），失败不该把维护链判红",
         blocking=False),
    Step("ingest_northbound", ("ingest", "northbound", "--days", "120"), False,
         "北向**季度**持股（东财 datacenter）。⚠️ 公开源上日度早已不存在（§0.3）——"
         "本步只落 frequency='quarterly' 的行，不编日度序列。**非阻断**（同上一句）",
         blocking=False),
    Step("candidate_review", ("candidate", "review", "--asof", "{asof}"), True,
         "插桩5「定期复盘分析」（P58）：读历史快照 / 回测台账 / 模块2 回流，"
         "落 `reports/plugin-review/<asof>.md` ＋ append-only 台账。**非阻断** —— "
         "复盘是派生读数，它失败不该把整条维护链判红、也不该挡住后面的体检"
         "（失败仍逐条记在回执的 `steps` / `bad=` / `anomalies` 里，不静默）",
         blocking=False),
    Step("doctor", ("doctor",), False,
         "数据健康度报告（含 financial_reports 覆盖检查）"),
)

MONTHLY_STEP_BY_NAME: dict[str, Step] = {s.name: s for s in MONTHLY_STEPS}
MONTHLY_STEP_ORDER: tuple[str, ...] = tuple(s.name for s in MONTHLY_STEPS)

#: 周/季度链整轮预算（P89）：候选池全量重打分 + 财报复核都是长跑，给 30 分钟。
WEEKLY_TIMEOUT_S = 1800.0
QUARTERLY_TIMEOUT_S = 1800.0

#: 汇总报告那一步的**名字**（它不是子进程，由链自己渲染 —— 见 `_render_step`）。
WEEKLY_REPORT_STEP = "weekly_report"
QUARTERLY_REPORT_STEP = "quarterly_report"

#: 每周全量扫描（B3，07 需求任务 3）＝ 5 步 + 汇总报告。
#:
#: **与收盘链的关键区别是 asof**：收盘链写**运行当天**（它负责把当天数据落地），
#: 周链是**读**当天数据做汇总 ⇒ `asof = 最近已收盘交易日`（D2）。理由是运行当天
#: 可能休市：10-05 恰好是周一、国庆休市，拿当天当 asof 会让「全量扫描」扫一个
#: 没有数据的日期。**两条链都不做非交易日跳过**（D3，与 `ops monthly` 同构）：
#: 休市周跑出来的是与上周同样的读数 —— 无害、可复核；跳过反而会「连续两周没扫」。
WEEKLY_STEPS: tuple[Step, ...] = (
    Step("candidate_run", ("candidate", "run", "--run-kind", "weekly",
                           "--asof", "{asof}"), True,
         "模块1 全流程 → 新快照（PIT：只用 <= asof 的数据）。**阻断** —— "
         "它没跑成，这一周的「新增/剔除合格标的」就没发生"),
    Step("candidate_review", ("candidate", "review", "--asof", "{asof}"), True,
         "插桩5「定期复盘分析」（P58）：错判案例回流台账，落 "
         "`reports/plugin-review/<asof>.md`。**非阻断**（同月链：派生读数，"
         "失败不该把整条扫描判红，但仍逐条记进回执的 steps/anomalies）",
         blocking=False),
    Step("m2_report", ("m2", "report", "--asof", "{asof}"), True,
         "双账户 + 双基准绩效读数（**只读**；与 `/lab/m2` 同一个取数函数）。"
         "**实测**：长文本走 stdout、那一行紧凑 JSON 走 **stderr 末尾** ——"
         "汇总报告直接引用那一行（见 `ops/window_report.py`），不重算"),
    Step("m2_lifecycle_due", ("m2", "lifecycle", "due", "--asof", "{asof}"), True,
         "冻结到期 / 已过期的策略版本清单（P87，只读）。**阻断** —— 失败说明"
         "判定台账读不了，季度复盘的前提就不成立"),
    Step("plugin_list", ("plugin", "list"), True,
         "在飞版本清单与状态（只读）。季度报告据此列出「待人工决定是否跑 "
         "`plugin sandbox`」，本档**不自动**跑批"),
    Step(WEEKLY_REPORT_STEP, (), True,
         "汇总报告（本档新增的**渲染**步骤，不是子进程）：把上面 5 步的 stdout 载荷 "
         "+ `m2 report` 的 JSON 汇总成 `<报告根>/weekly/<asof>-weekly.md`。"
         "**只汇总，不重算**；写不出来只记 exit 1，不把「链跑完了」改写成「链挂了」"),
)

WEEKLY_STEP_BY_NAME: dict[str, Step] = {s.name: s for s in WEEKLY_STEPS}
WEEKLY_STEP_ORDER: tuple[str, ...] = tuple(s.name for s in WEEKLY_STEPS)

#: 季度深度复盘（B4，07 需求任务 4）＝ 6 步 + 汇总报告。
#:
#: 与周链的区别：多**基本面复核**（财报复核 → 重估基本面）与**全量重打分**
#: （`--run-kind quarterly`），以及归因候选扫描（`m2 attribute scan`，落库但只给候选）。
#: 时刻表在法定披露截止日之后（4/30、8/31、10/31 的次日），见 `ops/schedule.py`。
#:
#: **本档刻意不做**（D7，都不是遗漏）：① 自动开新验证周期（P87 留待用户拍板，
#: 这里只出到期清单）；② 全量 `plugin sandbox` 跑批（一次一个 script_id、离线回放，
#: 很贵且是人工判断）；③ 集中度打分（新口径，只出 `portfolio show` 原始读数）。
QUARTERLY_STEPS: tuple[Step, ...] = (
    Step("ingest_financials", ("ingest", "financials"), False,
         "东财三表全量复核（已入库的期数 rows=0）→ 重估基本面。**阻断** —— "
         "它是本链与周链唯一的实质差别"),
    Step("candidate_run", ("candidate", "run", "--run-kind", "quarterly",
                           "--asof", "{asof}"), True,
         "模块1 **全量重打分**（`--run-kind quarterly`）→ 新快照。**阻断**"),
    Step("m2_report", ("m2", "report", "--asof", "{asof}"), True,
         "双账户 + 双基准绩效读数（只读；读数 JSON 在 stderr 末尾，见 window_report）"),
    Step("m2_lifecycle_due", ("m2", "lifecycle", "due", "--asof", "{asof}"), True,
         "冻结到期 / 已过期清单（只读）。**阻断**"),
    Step("m2_attribute_scan", ("m2", "attribute", "scan", "--asof", "{asof}"), True,
         "误差归因**候选**扫描（P50：程序只给候选、**不写结论**；结论只能人工确认）。"
         "**非阻断** —— 归因候选是派生读数", blocking=False),
    Step("plugin_list", ("plugin", "list"), True,
         "在飞版本清单与状态（只读）；报告据此列出「待人工决定是否跑 "
         "`plugin sandbox`」"),
    Step("portfolio_show", ("portfolio", "show", "--asof", "{asof}"), True,
         "持仓**原始读数**（只读）。D7：07 任务4 item4 的「行业集中度 / 单票仓位风险」"
         "本档**只出原始读数**，不做集中度打分（那是新口径，须另立任务书）。"
         "**非阻断** —— 它 exit 1 的含义是「缺现价或纪律 FAIL，要人来看」（见 `--help`），"
         "不是链断", blocking=False),
    Step(QUARTERLY_REPORT_STEP, (), True,
         "汇总报告（**渲染**步骤）：`<报告根>/quarterly/<asof>-quarterly.md`。"
         "**只汇总，不重算**"),
)

QUARTERLY_STEP_BY_NAME: dict[str, Step] = {s.name: s for s in QUARTERLY_STEPS}
QUARTERLY_STEP_ORDER: tuple[str, ...] = tuple(s.name for s in QUARTERLY_STEPS)

#: `job` → 这条链的完整步骤数（`summary_line` 的 `steps=n/total` 用）。
STEP_TOTALS: dict[str, int] = {
    "close": len(CLOSE_STEPS), "monthly": len(MONTHLY_STEPS),
    "weekly": len(WEEKLY_STEPS), "quarterly": len(QUARTERLY_STEPS),
}


def _as_datetime(value: datetime | str) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _bars_rows(db_path: Path, trade_date: str) -> int | None:
    """asof 当天入库了几行日 K（`None` = 连表都读不到）。"""
    conn = ro_connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM bars_daily WHERE date=?",
                           (trade_date,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return int(row[0]) if row else 0


def resolve_asof(db_path: Path | str, now: str) -> tuple[str | None, dict]:
    """`(最近已收盘交易日, 证据)`；判不出 / 读不了 → `(None, 证据)`。

    **fail-closed**（D2）：取不到就整轮拒绝，不许猜一个日期跑 —— 周/季度链是
    「读某一天的数据做汇总」，猜错日期得到的是一份**看起来正常的空报告**，
    而它不会报错（ERROR_DIARY #83 同族：不报错、不丢数据，只是数字没意义）。

    判定**复用** `verify.pending.latest_closed_session` —— 与 `patrol.check_db`
    是**同一个函数**（不是同一套规则的两次实现）：它已经在算「最新已收盘交易日」，
    并且拿 `session.tick.load_calendar` / `is_trade_date_closed` 做两侧证据
    （日历侧 `closed_through`、行情侧 `bars_daily` 且已收盘）。再写一遍就多一处会分叉。
    """
    try:
        conn = ro_connect(Path(db_path))
    except sqlite3.Error as exc:
        return None, {"rule": CLOSED_RULE, "now": now,
                      "error": f"{type(exc).__name__}: {exc}"}
    try:
        return latest_closed_session(conn, now)
    except sqlite3.Error as exc:                # 表不存在（老库未前滚）等
        return None, {"rule": CLOSED_RULE, "now": now,
                      "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def _render_step(steps_out: list[dict], *, step: Step, kind: str, db: Path,
                 asof: str, now: str, report_dir: Path | str | None,
                 asof_evidence: dict) -> dict:
    """跑「汇总报告」这一步，返回一个与 `runner` 同形的步骤结果（**不抛异常**）。

    它不是子进程：`run_steps` 是给 CLI 命令用的（见 `ops/runner.py` 的模块
    docstring），而汇总报告是**本档新增的渲染**，没有对应的命令可调（D1 不许为
    「凑齐需求条目」新写业务命令）。所以它写在链里，但结果形状与别的步**逐字段一致**
    —— 页面、回执、`summary_line` 都按同一种形状读，不为它开一个特例。
    """
    started = time.monotonic()
    try:
        out = window_report.write_report(
            kind=kind, asof=asof, now=now, db=str(db), steps=steps_out,
            report_dir=report_dir, asof_evidence=asof_evidence)
    except OSError as exc:              # write_report 已兜住，这里是最后一道
        out = {"path": None, "error": f"{type(exc).__name__}: {exc}"}
    duration = round(time.monotonic() - started, 3)
    written = out.get("path") is not None
    return {"name": step.name, "why": step.why, "blocking": step.blocking,
            "args": [], "exit_code": EXIT_OK if written else EXIT_ANOMALY,
            "timeout": False, "duration_s": duration,
            "report_path": out.get("path"),
            "stdout_tail": (f"报告已写入 {out['path']}" if written else ""),
            "stderr_tail": (out.get("error") or "")}


def _run_window_chain(*, job: str, kind: str, order: tuple[str, ...],
                      step_by_name: dict[str, Step], report_step: str,
                      timeout_s: float, db_path: Path | str | None, now: str | None,
                      runner, report_dir: Path | str | None) -> dict:
    """周/季度链的公共骨架（**顺序：asof → 跨库守卫 → 逐步 → 渲染 → 事后体检 → 回执**）。

    两条链逐字段同构，差别只在 `order` / `kind` —— 所以骨架共用，步骤表分开。
    这正是 `ops close` / `ops monthly` 的关系，不是新抽象。
    """
    started_at = datetime.now(TZ).isoformat(timespec="seconds")
    stamp = now or started_at
    db = Path(db_path) if db_path else paths.DB_PATH
    runner = runner or default_runner
    payload: dict = {"job": job, "now": stamp, "db": str(db)}

    if not db.exists():
        payload.update({
            "error": f"库不存在（{db}）；先跑 `stocklab db init`", "steps": [],
            "anomalies": [{"kind": "db_missing", "detail": f"{db} 不存在"}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return payload

    asof, evidence = resolve_asof(db, stamp)
    payload["asof"], payload["asof_evidence"] = asof, evidence
    if asof is None:
        reason = ("取不到「最近已收盘交易日」→ 整轮拒绝（fail-closed）："
                  f"{evidence.get('error') or '日历与行情两侧都没有证据'}。"
                  "本链是读某一天的数据做汇总，猜一个日期跑会得到一份**看起来正常的"
                  "空报告**，且不会报错")
        anomalies = [{"kind": "asof_unavailable", "detail": reason}]
        payload.update({"error": reason, "steps": [], "anomalies": anomalies,
                        "exit_code": EXIT_BLOCKED, "ok": False})
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    refusal = cross_db_refusal(db, list(order), step_by_name, what=f"ops {job}")
    if refusal:
        payload.update({
            "refused": refusal, "steps": [],
            "anomalies": [{"kind": "cross_db_refused", "detail": refusal}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    run_names = [name for name in order if name != report_step]
    steps_out, aborted = run_steps(run_names, registry=step_by_name, runner=runner,
                                   db_path=db, now=stamp, asof=asof,
                                   timeout_s=timeout_s)
    if aborted:
        payload["aborted"] = aborted
    steps_out.append(_render_step(steps_out, step=step_by_name[report_step],
                                  kind=kind, db=db, asof=asof, now=stamp,
                                  report_dir=report_dir, asof_evidence=evidence))

    after = check_db(db, stamp, report_dir=report_dir)
    anomalies = _step_anomalies(steps_out)
    if steps_out[-1].get("report_path") is None:
        anomalies.append({"kind": "report_failed", "step": report_step,
                          "detail": steps_out[-1].get("stderr_tail")
                          or "汇总报告没写出来"})
    payload.update({
        "steps": steps_out,
        "report": steps_out[-1].get("report_path"),
        "after": {k: after[k] for k in ("checks", "titles", "verdict",
                                        "calendar", "latest_closed_session",
                                        "anomalies", "exit_code", "ok")},
        "anomalies": anomalies,
        "exit_code": max(worst_code(steps_out, aborted), after["exit_code"]),
    })
    payload["ok"] = payload["exit_code"] == EXIT_OK
    return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                 report_dir=report_dir, job=job)


def run_weekly(*, db_path: Path | str | None = None, now: str | None = None,
               runner=None, timeout_s: float = WEEKLY_TIMEOUT_S,
               report_dir: Path | str | None = None) -> dict:
    """每周全量扫描（B3，周一 16:30）＝ 5 步 + 汇总报告；`asof = 最近已收盘交易日`。

    退出码与既有链同构（D9）：0 全绿（含**休市日照跑**）/ 1 链跑完但有非 0 步或
    事后体检 `missing`/`stale` / 2 断链（库不在 / **取不到 asof** / 某步 ≥2 /
    预算用尽 / 跨库守卫拒）。

    `runner` 是注入点（测试用假执行器，绝不真起子进程）。
    """
    return _run_window_chain(
        job="weekly", kind="weekly", order=WEEKLY_STEP_ORDER,
        step_by_name=WEEKLY_STEP_BY_NAME, report_step=WEEKLY_REPORT_STEP,
        timeout_s=timeout_s, db_path=db_path, now=now, runner=runner,
        report_dir=report_dir)


def run_quarterly(*, db_path: Path | str | None = None, now: str | None = None,
                  runner=None, timeout_s: float = QUARTERLY_TIMEOUT_S,
                  report_dir: Path | str | None = None) -> dict:
    """季度深度复盘（B4，5/1 + 9/1 + 11/1 09:00）＝ 6 步 + 汇总报告。

    退出码语义、asof 取法与 `run_weekly` **逐字相同**（同一条骨架）。
    """
    return _run_window_chain(
        job="quarterly", kind="quarterly", order=QUARTERLY_STEP_ORDER,
        step_by_name=QUARTERLY_STEP_BY_NAME,
        report_step=QUARTERLY_REPORT_STEP, timeout_s=timeout_s, db_path=db_path,
        now=now, runner=runner, report_dir=report_dir)


def _seal(payload: dict, *, db: Path, stamp: str, started_at: str,
          report_dir: Path | str | None, job: str) -> dict:
    """写回执（`<报告根>/ops/latest-<job>.json` + `job_runs` 一行）并把结果塞回载荷。"""
    return journal.seal(payload, db_path=db, job_name=job, stamp=stamp,
                        detail=summary_line(payload), started_at=started_at,
                        report_dir=report_dir)


def summary_line(payload: dict) -> str:
    """**一行**回执（launchd 日志与 `/lab/ops` 页面用同一份）。"""
    job = payload.get("job") or "ops"
    if payload.get("skipped"):
        return f"{job}: skipped（{payload['skipped']}）"
    if payload.get("error") or payload.get("refused"):
        return (f"{job}: blocked exit={payload.get('exit_code')}"
                f"（{payload.get('error') or payload.get('refused')}）")
    steps = payload.get("steps") or []
    total = STEP_TOTALS.get(job, 0)
    bad = [f"{s['name']}={s.get('exit_code')}" for s in steps
           if s.get("exit_code") != 0]
    after = (payload.get("after") or {}).get("exit_code")
    return (f"{job}: exit={payload.get('exit_code')} asof={payload.get('asof')}"
            f" steps={len(steps)}/{total} bad={','.join(bad) or '-'}"
            f" after={after} anomalies={len(payload.get('anomalies') or [])}")


def _step_anomalies(steps: list[dict]) -> list[dict]:
    """跑过的步骤里非 0 的 → 异常条目（1 = 如实报了异常，≥2 = 链断在这里）。"""
    out: list[dict] = []
    for s in steps:
        if s.get("exit_code") == 0:
            continue
        code = s.get("exit_code")
        out.append({
            "kind": "step_failed" if (code is None or code >= EXIT_BLOCKED)
                    else "step_anomaly",
            "step": s["name"], "exit_code": code,
            "detail": (s.get("stderr_tail") or s.get("stdout_tail")
                       or s.get("error") or "")[-200:],
        })
    return out


def run_close(*, db_path: Path | str | None = None, now: str | None = None,
              runner=None, timeout_s: float = CLOSE_TIMEOUT_S,
              report_dir: Path | str | None = None,
              backup_dir: Path | str | None = None) -> dict:
    """收盘链 = 库备份 → 13 步（**asof = 运行当天**）→ 事后体检 → 回执。

    退出码：0 全绿（含非交易日跳过）/ 1 链跑完但事后体检有 `missing`/`stale` 或某步
    退出 1 / 2 断链（库不在 / 还没收盘 / 某步 ≥2 / **整轮预算用尽或单步超时** /
    事后体检判不了 / 事后仍无当日 bar）。

    `runner` 是注入点（测试用假执行器，绝不真起子进程）。
    """
    job = "close"
    started_at = datetime.now(TZ).isoformat(timespec="seconds")
    stamp = now or started_at
    dt = _as_datetime(stamp)
    asof = dt.date().isoformat()
    db = Path(db_path) if db_path else paths.DB_PATH
    runner = runner or default_runner
    payload: dict = {"job": job, "now": stamp, "db": str(db), "asof": asof}

    if not db.exists():
        payload.update({
            "error": f"库不存在（{db}）；先跑 `stocklab db init`",
            "steps": [], "anomalies": [{"kind": "db_missing",
                                        "detail": f"{db} 不存在"}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return payload

    # 非交易日 / 还没收盘：两条都必须**在任何写入之前**判掉。
    pre = check_db(db, stamp, report_dir=report_dir)
    sday = pre["session_day"]
    payload["session_day"] = sday
    if sday["is_trading_day"] is False:
        payload.update({
            "steps": [], "anomalies": [], "exit_code": EXIT_OK, "ok": True,
            "skipped": f"非交易日（{sday['why']}）→ 整轮跳过（07 §非交易日全部跳过）",
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    if not is_trade_date_closed(asof, stamp):
        reason = (f"今天（{asof} {stamp[11:16]}）还没收盘 → 拒绝执行："
                  "半截 bar 与当日 LIVE 预测都是 append-only，写错了退不回来")
        payload.update({
            "refused": reason, "steps": [],
            "anomalies": [{"kind": "before_close", "detail": reason}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    refusal = cross_db_refusal(db, list(CLOSE_STEP_ORDER), CLOSE_STEP_BY_NAME,
                               what="ops close")
    if refusal:
        payload.update({
            "refused": refusal, "steps": [],
            "anomalies": [{"kind": "cross_db_refused", "detail": refusal}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    # 0) 库备份（07 D-7 的固定步）。备份失败就**不跑链**：这条链会往库里写一整天的
    #    数据，没有当天备份时继续跑，等于把「能回退」这个前提悄悄丢掉。
    bdir = Path(backup_dir) if backup_dir else paths.BACKUP_DIR
    try:
        payload["backup"] = str(backup_db(db, bdir, "preclose"))
    except (OSError, sqlite3.Error) as exc:
        payload.update({
            "error": f"库备份失败：{exc}", "steps": [],
            "anomalies": [{"kind": "backup_failed", "detail": str(exc)}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    steps_out, aborted = run_steps(
        list(CLOSE_STEP_ORDER), registry=CLOSE_STEP_BY_NAME, runner=runner,
        db_path=db, now=stamp, asof=asof, timeout_s=timeout_s)

    after = check_db(db, stamp, report_dir=report_dir)
    rows = _bars_rows(db, asof)
    anomalies = _step_anomalies(steps_out)
    if rows == 0:
        reason = (f"{asof} 的日线一行都没有 —— ingest 段跑过之后仍然没有，"
                  "要么源站没给，要么这根本不是交易日（但判定说它是）")
        anomalies.append({"kind": "bars_missing_after_ingest",
                          "date": asof, "detail": reason})
    if aborted:
        payload["aborted"] = aborted

    codes = [worst_code(steps_out, aborted)]
    if rows == 0:
        codes.append(EXIT_ANOMALY)
    codes.append(after["exit_code"])          # 事后体检：判不了（2）也照实传上去

    payload.update({
        "steps": steps_out,
        "bars_rows": rows,
        "after": {k: after[k] for k in ("checks", "titles", "verdict",
                                        "calendar", "latest_closed_session",
                                        "anomalies", "exit_code", "ok")},
        "anomalies": anomalies,
        "exit_code": max(codes),
    })
    payload["ok"] = payload["exit_code"] == EXIT_OK
    return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                 report_dir=report_dir, job=job)


def run_monthly(*, db_path: Path | str | None = None, now: str | None = None,
                runner=None, timeout_s: float = MONTHLY_TIMEOUT_S,
                report_dir: Path | str | None = None) -> dict:
    """月度刷新 = 休市公告 → 长窗行情 → 财报 → 体检（跑在每月 1 日 08:00）。

    **没有交易日判定**：它是「不管今天是不是交易日都要做」的维护活，且全部幂等。
    退出码与收盘链同构（0 / 1 / 2）。
    """
    job = "monthly"
    started_at = datetime.now(TZ).isoformat(timespec="seconds")
    stamp = now or started_at
    db = Path(db_path) if db_path else paths.DB_PATH
    runner = runner or default_runner
    payload: dict = {"job": job, "now": stamp, "db": str(db),
                     "asof": _as_datetime(stamp).date().isoformat()}

    if not db.exists():
        payload.update({
            "error": f"库不存在（{db}）；先跑 `stocklab db init`", "steps": [],
            "anomalies": [{"kind": "db_missing", "detail": f"{db} 不存在"}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return payload

    refusal = cross_db_refusal(db, list(MONTHLY_STEP_ORDER), MONTHLY_STEP_BY_NAME,
                               what="ops monthly")
    if refusal:
        payload.update({
            "refused": refusal, "steps": [],
            "anomalies": [{"kind": "cross_db_refused", "detail": refusal}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    steps_out, aborted = run_steps(
        list(MONTHLY_STEP_ORDER), registry=MONTHLY_STEP_BY_NAME, runner=runner,
        db_path=db, now=stamp, asof=_as_datetime(stamp).date().isoformat(),
        timeout_s=timeout_s)

    after = check_db(db, stamp, report_dir=report_dir)
    if aborted:
        payload["aborted"] = aborted
    codes = [worst_code(steps_out, aborted), after["exit_code"]]
    payload.update({
        "steps": steps_out,
        "after": {k: after[k] for k in ("checks", "titles", "verdict",
                                        "calendar", "latest_closed_session",
                                        "anomalies", "exit_code", "ok")},
        "anomalies": _step_anomalies(steps_out),
        "exit_code": max(codes),
    })
    payload["ok"] = payload["exit_code"] == EXIT_OK
    return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                 report_dir=report_dir, job=job)


__all__ = ["CLOSE_STEPS", "CLOSE_STEP_BY_NAME", "CLOSE_STEP_ORDER",
           "CLOSE_TIMEOUT_S", "MONTHLY_STEPS", "MONTHLY_STEP_BY_NAME",
           "MONTHLY_STEP_ORDER", "MONTHLY_TIMEOUT_S",
           "QUARTERLY_REPORT_STEP", "QUARTERLY_STEPS", "QUARTERLY_STEP_BY_NAME",
           "QUARTERLY_STEP_ORDER", "QUARTERLY_TIMEOUT_S", "STEP_TOTALS",
           "WEEKLY_REPORT_STEP", "WEEKLY_STEPS", "WEEKLY_STEP_BY_NAME",
           "WEEKLY_STEP_ORDER", "WEEKLY_TIMEOUT_S",
           "resolve_asof", "run_close", "run_monthly", "run_quarterly",
           "run_weekly", "summary_line"]
