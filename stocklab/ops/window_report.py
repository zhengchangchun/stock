"""汇总报告渲染（P89 B3/B4）：`ops weekly` / `ops quarterly` 的最后一步。

## 这一层只做一件事：把已经跑出来的读数摆整齐

**只汇总，不重算**（D5/D6 第 6/7 步）。本模块**不读库、不算任何业务量** ——
它的全部输入是链里各步的 `stdout_tail`（`runner.default_runner` 留的最后 800 字符）
＋ 链本身已经定稿的元信息（asof / 退出码 / 耗时 / 库路径）。
所以「报告里的数与链里的数不一致」这种错**在结构上不可能**：它一个数都没自己算。

## 每个数字旁边必须有「数据出处」

不做成一句话，做成**表格里的一列**（`数据出处`）：写清它来自哪一步的哪一行 stdout /
哪个字段。理由是 ERROR_DIARY #50 的同型教训 —— 「无数据」写成 0、或者留空，
读报告的人就分不清「真的是 0」和「这一步没跑成」。所以：

- 某一步没有 stdout ⇒ 写 **`无数据（<原因>）`**，绝不写 0、绝不留空；
- `m2 report` 的 JSON 解析不出来 ⇒ 写 `无数据（stdout 尾部没有可解析的 JSON）`，
  而不是把正文里的数字猜一个。

## `m2 report` 的 JSON 从哪来（**实测修正**）

任务书 D5.6 写的是「各步 `stdout` 载荷 + `m2 report` 的 JSON」；**实测**（只读跑
`m2 report --asof 2026-09-24 --db /tmp/p89/copy.db`，并对照 `cli/main.py` 的
`cmd_m2_report`）：它的长文本报告走 **stdout**，而那一行紧凑 JSON 是
`print(..., file=sys.stderr)` —— **在 stderr 末尾**。所以这里两个流都扫：
先 `stderr_tail`、再 `stdout_tail`，取第一条能 `json.loads` 成 dict 的行。

`runner` 只留每个流的**尾部** 800 字符，而 JSON 恰好是最后一行 ⇒ 从尾部倒着找即可。
这条取法不依赖文本报告的排版，只依赖「最后一行是 JSON」这一个已实现的约定；
两个流都扫是为了不把「实现把 JSON 挪去 stdout」当成解析失败。
"""

from __future__ import annotations

import json
from pathlib import Path

from stocklab.config import paths

#: `kind` → 报告标题（B3 / B4 是审计文档里的编号，写进报告便于回查需求）。
KIND_TITLE: dict[str, str] = {
    "weekly": "每周全量扫描（B3）",
    "quarterly": "季度深度复盘（B4）",
}

#: 每步 stdout 原文引用多少字符（与 `runner.TAIL_CHARS` 同量级；原文另存不截断的是回执）。
RAW_TAIL_CHARS = 800

#: 「没有数据」的统一说法（**不许**用 0 或空串顶替）。
NO_DATA = "无数据"


def report_dir_for(kind: str, report_dir: Path | str | None = None) -> Path:
    """报告目录 = **报告根**下的 `<kind>/`（与回执共用同一个根，见 `journal`）。"""
    root = Path(report_dir) if report_dir else paths.REPORT_DIR
    return root / kind


def report_path(kind: str, asof: str, report_dir: Path | str | None = None) -> Path:
    return report_dir_for(kind, report_dir) / f"{asof}-{kind}.md"


def extract_json_tail(text: str | None) -> dict | None:
    """stdout 尾部里**最后一行**能解析成 JSON 对象的 → 那个对象；否则 `None`。

    倒着扫是为了避开正文里恰好长得像 JSON 的行：真正的载荷是**最后**一行。
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def m2_json(step: dict | None) -> dict | None:
    """从 `m2_report` 那一步取出结构化载荷（**先 stderr、再 stdout**，见模块 docstring）。"""
    if not step:
        return None
    return (extract_json_tail(step.get("stderr_tail"))
            or extract_json_tail(step.get("stdout_tail")))


def _exit_cn(code: object) -> str:
    if code is None:
        return "判不了（没有退出码）"
    return {0: "0 全绿", 1: "1 跑完了但有异常", 2: "2 断链/判不了"}.get(
        int(code), f"{code}") if isinstance(code, int) else str(code)


def _num(payload: dict, path: str) -> str:
    """按 `a.b.c` 取值；缺任意一段 ⇒ `无数据（<路径> 不在载荷里）`。"""
    cur: object = payload
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return f"{NO_DATA}（`{path}` 不在 m2 载荷里）"
        cur = cur[key]
    return f"{cur}"


def _raw(step: dict) -> str:
    """一步的原始读数：stdout 尾部，**stderr 尾部另列**（`m2 report` 的 JSON 在那）。"""
    parts: list[str] = []
    text = (step.get("stdout_tail") or "").strip()
    if text:
        parts.append("stdout 尾部：\n```\n" + text[-RAW_TAIL_CHARS:] + "\n```")
    else:
        parts.append(f"{NO_DATA}（`{step['name']}` 没有 stdout："
                     f"exit={step.get('exit_code')}）")
    err = (step.get("stderr_tail") or "").strip()
    if err:
        parts.append("stderr 尾部：\n```\n" + err[-RAW_TAIL_CHARS:] + "\n```")
    return "\n\n".join(parts)


def _source_cell(step: dict) -> str:
    """**数据出处**：这一步的读数来自哪条命令的哪一部分。"""
    argv = " ".join(step.get("args") or [])
    src = f"`{argv}` 的 stdout 尾部（≤{RAW_TAIL_CHARS} 字符）"
    if step["name"] == "m2_report":
        src += "；结构化读数来自它**写在 stderr 末尾的那一行 JSON**"
    return src


def _step_table(steps) -> str:
    rows = "".join(
        f'| `{s["name"]}` | {_exit_cn(s.get("exit_code"))} | '
        f'{s.get("duration_s") if s.get("duration_s") is not None else NO_DATA} | '
        f'{_source_cell(s)} | {s.get("why") or ""} |\n'
        for s in steps)
    return ("| 步骤 | 退出码 | 耗时(s) | **数据出处** | 这一步为什么在这里 |\n"
            "|---|---|---|---|---|\n" + rows)


def render_report(*, kind: str, asof: str, now: str, db: str, steps,
                  asof_evidence: dict | None = None) -> str:
    """把链的读数渲染成一份 Markdown 报告（**纯函数**：不读库、不算数、不写盘）。"""
    title = KIND_TITLE.get(kind, kind)
    measured = {s["name"]: s for s in steps}
    m2 = m2_json(measured.get("m2_report"))
    ev = asof_evidence or {}

    out: list[str] = [
        f"# {title} — {asof}",
        "",
        f"> 生成时刻 `{now}`；库 `{db}`；报告由 `ops {kind}` 的汇总步骤渲染。",
        "> **本报告只汇总，不重算**：每个数字旁边都写着它的出处；"
        f"没有读数的写 `{NO_DATA}（原因）`，不写 0。",
        "",
        "## 一、这一轮怎么定的 asof",
        "",
        f"- `asof = {asof}`（**最近已收盘交易日**，不是运行当天 —— 运行当天可能休市）；",
        f"- 判据：{ev.get('rule') or f'{NO_DATA}（回执里没有 asof_evidence）'}；",
        f"- 日历侧 `{ev.get('calendar_side') or NO_DATA}` / "
        f"行情侧 `{ev.get('bars_side') or NO_DATA}` / "
        f"日历最大日 `{ev.get('calendar_range') or NO_DATA}`；",
        "- 数据出处：`ops/chain.py` 的 asof 解析步骤（复用 `verify.pending."
        "latest_closed_session` 与 `patrol.check_db` 同一份判定），记在回执的 "
        "`asof_evidence` 里。",
        "",
        "## 二、逐步读数",
        "",
        _step_table(steps),
    ]

    out += ["", "## 三、m2 report 的结构化读数", ""]
    if m2 is None:
        out += [f"{NO_DATA}（`m2_report` 的 stdout/stderr 尾部都没有可解析的 JSON —— "
                "要么这一步没跑成，要么它没按「最后一行 JSON」的口径打印）。",
                "完整载荷见回执 `ops/latest-"
                f"{kind}.json` 的 `steps[]`。"]
    else:
        out += [
            "| 读数 | 值 | **数据出处** |",
            "|---|---|---|",
            f"| 截止日 `asof` | {_num(m2, 'asof')} | `m2 report` stderr 末尾那一行 JSON |",
            f"| 有效交易日 `n_sessions` | {_num(m2, 'n_sessions')} | 同上 |",
            f"| 样本门槛 `threshold` | {_num(m2, 'threshold')} | 同上 |",
            f"| 门槛状态 `gate_status` | {_num(m2, 'gate_status')} | 同上 |",
            f"| 对照臂数 `n_sides` | {_num(m2, 'n_sides')} | 同上 |",
            f"| 已打分预测 `forecast.n_scored` | {_num(m2, 'forecast.n_scored')} | 同上 |",
            f"| 未打分预测 `forecast.n_unscored` | {_num(m2, 'forecast.n_unscored')} | 同上 |",
            f"| 错判总数 `cases.n_miss_total` | {_num(m2, 'cases.n_miss_total')} | 同上 |",
            f"| 错判案例数 `cases.n_cases` | {_num(m2, 'cases.n_cases')} | 同上 |",
        ]

    out += ["", "## 四、各步 stdout / stderr 尾部（原文，未加工）", ""]
    for s in steps:
        out += [f"### `{s['name']}`（exit={s.get('exit_code')}）", "", _raw(s), ""]

    if kind == "quarterly":
        out += _quarterly_sections(measured)
    else:
        out += _weekly_sections(measured)

    out += [
        "## 口径与限制",
        "",
        "- 退出码：0 全绿（含非交易日**照跑**）/ 1 链跑完了但有异常 / 2 断链或判不了；",
        "- 两条链**都不做非交易日跳过**（D3）：休市周跑出来的是与上周同样的读数，"
        "无害、可复核；跳过反而会「连续两周没扫」；",
        "- 本报告与 `reports/ops/latest-"
        f"{kind}.json`（回执）同源：报告是人读的汇总，回执是机器的完整载荷；",
        "- 报告不是判据：判据是 `job_runs` 的 status 与回执的 `exit_code`。",
        "",
    ]
    return "\n".join(out)


def _weekly_sections(measured: dict) -> list[str]:
    review = measured.get("candidate_review")
    return [
        "## 五、插桩5 复盘回流（非阻断）",
        "",
        (f"`candidate review` exit={review.get('exit_code')}；"
         f"报告落在 `reports/plugin-review/{'<asof>'}.md`。"
         if review else f"{NO_DATA}（这一步不在本轮计划里）"),
        "",
        f"- 数据出处：`candidate review --asof <asof>` 的 stdout 尾部（见第四节 "
        "`candidate_review` 小节）；复盘台账是 append-only，本报告只引用不解读。",
        "",
    ]


def _quarterly_sections(measured: dict) -> list[str]:
    plugins = measured.get("plugin_list")
    portfolio = measured.get("portfolio_show")
    ingest = measured.get("ingest_financials")
    return [
        "## 五、待人工决定是否跑 `plugin sandbox`",
        "",
        "本档**不自动**跑全量过拟合检测。原因（D7）：`plugin sandbox` 是「一次一个 "
        "`script_id`、离线回放」，多版本连跑很贵；且「哪些版本该复检」是人工判断。"
        "所以这里**只列 active 版本清单**，把「要不要逐个跑 `plugin sandbox`」"
        "留给人工决定 —— 不静默跳过，也不假装跑过。",
        "",
        (f"- active 版本清单：见第四节 `plugin_list` 小节（`plugin list` "
         f"exit={plugins.get('exit_code')}）；"
         if plugins else f"- {NO_DATA}（`plugin list` 不在本轮计划里）；"),
        "- 数据出处：`plugin list` 的 stdout 尾部（本节不解读、不排序、不筛选）；",
        "- 下一步（人工）：从上面的清单里挑 `script_id`，逐个跑 "
        "`stocklab plugin sandbox <script_id> --db <库>` 并与基线对照。",
        "",
        "## 六、风控全量复核（只出原始读数）",
        "",
        "07 任务4 item4 要「行业集中度 / 单票仓位风险」。**本档只出 `portfolio show` "
        "的原始读数**，不做任何集中度打分 —— 打分是新口径，须另立任务书"
        "（D7：本档不得自创风控算法）。",
        "",
        (f"- `portfolio show` exit={portfolio.get('exit_code')}"
         "（1 = 缺现价或纪律 FAIL，「要人来看」，不是链断）；"
         if portfolio else f"- {NO_DATA}（`portfolio show` 不在本轮计划里）；"),
        "- 原始读数见第四节 `portfolio_show` 小节；本报告**不做**任何阈值判定。",
        "",
        "## 七、基本面复核",
        "",
        (f"- `ingest financials` exit={ingest.get('exit_code')}：三表全量复核，"
         "把重估过的基本面读回来（口径见该命令自己的 `--help`）；"
         if ingest else f"- {NO_DATA}（`ingest financials` 不在本轮计划里）；"),
        "- 数据出处：`ingest financials` 的 stdout 尾部（第四节）。",
        "",
    ]


def write_report(*, kind: str, asof: str, now: str, db: str, steps,
                 report_dir: Path | str | None = None,
                 asof_evidence: dict | None = None) -> dict:
    """渲染并落盘。**任何失败都不抛** —— 只回 `{path, error}`（链据此记一条异常）。

    报告写不出来（目录只读 / 盘满）时链条本身是跑完了的，不能把它改写成「链挂了」；
    但也不能静默：`error` 会进回执的 `anomalies`（`report_failed`）。
    """
    path = report_path(kind, asof, report_dir)
    try:
        text = render_report(kind=kind, asof=asof, now=now, db=db, steps=steps,
                             asof_evidence=asof_evidence)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        return {"path": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"path": str(path), "error": None}


__all__ = ["KIND_TITLE", "NO_DATA", "RAW_TAIL_CHARS", "extract_json_tail", "m2_json",
           "render_report", "report_dir_for", "report_path", "write_report"]
