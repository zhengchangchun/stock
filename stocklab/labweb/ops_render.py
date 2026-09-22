"""定时任务页渲染（`/ops`）：三条链**自己写的回执**摆出来。

## 复用，不新建版式

壳（`layout` / `.rail`）、表格（`table.tbl` / `table.kv`）、空行（`tr.empty`）、
折叠（`more`）、横幅（`banner` / `alert`）全部复用 `render.py` 的既有件 ——
**一行 CSS 都不加**（实测 `.tbl` / `.kv` / `tr.empty` / `.scroll-x` / `.s-pass`
/ `.s-fail` 都在 `app.css` 里）。

## 页面只说回执里写着的话

- 摘要行**逐字等于 launchd 日志里那一行**（同一个 `chain.summary_line` /
  `patrol.summary_line`），所以「页面上说 exit=1」与「日志里说 exit=1」是同一条读法；
- 没跑过 → 「还没有回执」，**不写 0**；回执损坏 → 原文报出来，**不退回旧数字**；
- 退出码的含义（0/1/2）写在页面上，不指望读者去翻 `--help`。

## 不代跑 `launchctl`

「被 launchd 加载了没 / 上次退出码几」的唯一真源是 `launchctl list`，而 Web 线程里
起系统命令是另一条会咬人的线，所以页面只把命令给出来（`ops schedule status`）。
页面能答的是另一件事：**plist 文件在不在**、**回执里上一步跑成什么样**。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from stocklab.labweb.render import (alert, esc, glance, glance_html, layout,
                                    more, rich, section)

#: `job_runs.status` → (CSS 类, 人话)。**只换标签，不改判据**：
#: 认不出的状态原样显示机器名（宁可难看，也不许猜它是什么意思）。
STATUS_CN: dict[str, tuple[str, str]] = {
    "ok": ("s-pass", "成功"),
    "failed": ("s-fail", "失败"),
    "running": ("s-warn", "运行中"),
    "skipped": ("s-unknown", "跳过"),
}

#: 退出码 → 人话。**与 `ops/patrol.py` / `ops/chain.py` 的模块 docstring 同源**。
EXIT_CN: dict[int, str] = {
    0: "0 = 全绿（含非交易日整轮跳过）",
    1: "1 = 链路跑完了，但有异常（`missing`/`stale`，或某一步如实报了 1）",
    2: "2 = 判不了或断链（库不在 / 还没收盘 / 预算用尽 / 某步 ≥2 / 事后体检判不了）",
}


def _exit_pill(code: Any) -> str:
    if code is None:
        return '<span class="s-unknown">未知（回执里没有 exit_code）</span>'
    try:
        n = int(code)
    except (TypeError, ValueError):
        return f'<span class="s-unknown">exit={esc(code)}</span>'
    cls = "s-pass" if n == 0 else ("s-warn" if n == 1 else "s-fail")
    return f'<span class="{cls}">exit={n}</span>'


def _status_pill(status: Any) -> str:
    cls, cn = STATUS_CN.get(str(status), ("s-unknown", "认不出的状态"))
    text = cn if str(status) in STATUS_CN else str(status)
    return f'<span class="{cls}">{esc(text)}</span>'


def _present_pill(present: bool) -> str:
    return ('<span class="s-pass">文件在位</span>' if present else
            '<span class="s-fail">还没写（跑 ops schedule install）</span>')


def _verdict(latest: Mapping) -> str:
    """回执自己写的「结论句」：跳过 / 拒绝 / 受阻 / 完成。"""
    if latest.get("skipped"):
        return f'**整轮跳过**（{latest["skipped"]}）'
    if latest.get("refused"):
        return f'**拒绝执行**（{latest["refused"]}）'
    if latest.get("error"):
        return f'**没跑起来**（{latest["error"]}）'
    steps = latest.get("steps") or []
    total = latest.get("steps_total")
    return f'跑完 {len(steps)} 步' + (f'／共 {total} 步' if total else "")


def _steps_table(steps: Sequence[Mapping]) -> str:
    if not steps:
        return ('<table class="tbl"><tr class="empty"><td>这一轮没有跑任何步骤'
                '（非交易日跳过 / 还没收盘 / 库不存在）—— 不是「跑了 0 步都成功」。'
                '</td></tr></table>')
    rows = "".join(
        f'<tr><td class="l">{esc(s["name"])}</td>'
        f'<td>{_exit_pill(s["exit_code"])}'
        + ('<span class="sub"> 超时</span>' if s.get("timeout") else "")
        + f'</td><td>{_duration(s)}</td>'
        f'<td class="l sub">{rich(s.get("why") or "")}</td></tr>'
        for s in steps)
    return ('<div class="scroll-x"><table class="tbl">'
            '<tr><th>步骤</th><th>退出码</th><th>耗时</th><th>这一步为什么在这里</th></tr>'
            f'{rows}</table></div>')


def _duration(s: Mapping) -> str:
    v = s.get("duration_s")
    return "—" if v is None else f"{float(v):.2f} s"


def _anomalies_block(anomalies: Sequence[Mapping]) -> str:
    if not anomalies:
        return '<p class="note">回执里没有异常条目。</p>'
    items = "".join(
        f'<li><code>{esc(a.get("kind"))}</code>　'
        f'{rich(a.get("detail") or a.get("step") or "")}</li>'
        for a in anomalies)
    return f'<ul class="list">{items}</ul>'


def _job_section(job: Mapping) -> str:
    """一条任务 = 一段：窗口 / 命令 / 回执 / 最近一次 / 历史。"""
    latest = job.get("latest")
    lines = [
        f'<table class="kv">'
        f'<tr><th>窗口</th><td>{esc(job["window"])}</td></tr>'
        f'<tr><th>为什么</th><td>{esc(job["why"])}</td></tr>'
        f'<tr><th>命令</th><td><code>{esc(" ".join(job["argv"]))}</code></td></tr>'
        f'<tr><th>plist</th><td><code>{esc(job["plist"])}</code>　'
        f'{_present_pill(job["plist_present"])}</td></tr>'
        f'<tr><th>下一次触发</th><td>{esc(job["next_trigger"] or "推算不出来（形态认不出）")}'
        f'　<span class="sub">按 plist 里的 `StartCalendarInterval` 推算；'
        f'机器睡过去时 launchd 会在唤醒后补跑一次</span></td></tr>'
        f'<tr><th>回执</th><td><code>{esc(job["receipt_path"])}</code></td></tr>'
        f'<tr><th>日志</th><td><code>{esc(job["stdout_log"])}</code>　/　'
        f'<code>{esc(job["stderr_log"])}</code></td></tr>'
        f'</table>',
    ]
    if job.get("latest_error"):
        lines.append(alert(f'回执读不出来：{job["latest_error"]}'
                           '（页面显示「读不出来」，不退回上一次的旧数字）'))
    elif latest is None:
        lines.append('<p class="note">还没有回执 —— 这条任务在本机还没跑过。'
                     '（不是「跑了 0 步都成功」，也不是失败。）</p>')
    else:
        lines.append(
            f'<p>{_exit_pill(job["exit_code"])}　'
            f'<span class="sub">{esc(latest.get("now") or "（回执没写时刻）")}</span>　'
            f'{rich(_verdict(latest))}</p>')
        if job.get("summary"):
            lines.append(f'<p class="note">摘要行（与 launchd 日志逐字相同）：'
                         f'<code>{esc(job["summary"])}</code></p>')
        lines.append(_steps_table(job["steps"]))
        lines.append(more(_anomalies_block(job["anomalies"]),
                          label=f'查看异常（{len(job["anomalies"])} 条）'))
    if job.get("history"):
        rows = "".join(_run_row(r) for r in job["history"])
        lines.append(more(
            '<div class="scroll-x"><table class="tbl">'
            '<tr><th>#</th><th>状态</th><th>开始</th><th>结束</th><th>摘要</th></tr>'
            f'{rows}</table></div>',
            label=f'查看历史（{len(job["history"])} 次，来自 `job_runs`）'))
    right = "launchd 时钟 + 项目回执"
    if job["next_trigger"]:
        right = f'下次 {esc(job["next_trigger"][5:16])}'
    return section(f'{job["name"]}', "".join(lines), right=right)


def _run_row(r: Mapping) -> str:
    return (f'<tr><td class="sub">{esc(r.get("run_id"))}</td>'
            f'<td>{_status_pill(r.get("status"))}</td>'
            f'<td class="sub">{esc(r.get("started_at"))}</td>'
            f'<td class="sub">{esc(r.get("finished_at"))}</td>'
            f'<td class="l sub">{esc(r.get("detail"))}</td></tr>')


def ops_page(data: Mapping, *, base: str, built_at: str) -> str:
    """定时任务页（整页）。"""
    head = '<p class="note">' + rich(
        "**时钟在系统里**（launchd 的 `~/Library/LaunchAgents/com.stocklab.*.plist`），"
        "**跑什么在项目里**（`ops/chain.py` 的顺序与停止线），**回执在 "
        "`reports/ops/`**。这一页只把回执摆出来 —— 它不起子进程、不判红、不改库。") \
        + '</p>'
    if not data["db_exists"]:
        head += alert(f'库不在（{data["db_path"]}）—— 任务的历史读不出来，'
                      '下面的任务清单与回执仍然照常显示。')

    marks = glance([
        "　".join([f'{job["window"]}' for job in data["jobs"]]),
        "退出码：0 全绿 / 1 跑完了但有异常 / 2 判不了或断链。",
        f'回执目录 <code>{esc(data["receipts_dir"])}</code>，'
        f'日志目录 <code>{esc(data["log_dir"])}</code>。',
    ])
    limits = glance([
        "本页**不代跑 `launchctl`**：被加载了没 / 上次退出码几，看 "
        "`stocklab ops schedule status`（页面只负责回执）。",
        "「下一次触发」是从 plist 的 `StartCalendarInterval` **推算**的，"
        "不是系统的承诺。",
        "没有回执就写「还没有回执」；读不出来就写「读不出来」——"
        "**不用 0 顶替，也不拿旧数字冒充今天**。",
    ])
    body = [head, glance_html(marks)]
    for job in data["jobs"]:
        body.append(_job_section(job))

    if data["runs"]:
        rows = "".join(_run_row(r) for r in data["runs"])
        history = ('<div class="scroll-x"><table class="tbl">'
                   '<tr><th>#</th><th>作业</th><th>状态</th><th>开始</th>'
                   '<th>结束</th><th>摘要</th></tr>' + rows + '</table></div>')
    else:
        history = ('<table class="tbl"><tr class="empty"><td>'
                   '`job_runs` 里还没有记录（新库，或这一轮什么都没跑）。'
                   '</td></tr></table>')
    body.append(section(f'库里最近跑了什么（最近 {data["history_limit"]} 行）',
                        history, right="`job_runs` 是全部作业共用的表"))
    exit_lines = "".join(f'<li>{rich(EXIT_CN[k])}</li>' for k in sorted(EXIT_CN))
    body.append(section("退出码怎么读", f'<ul class="list">{exit_lines}</ul>',
                        note="这三行与 `ops patrol` / `ops close` 的模块 docstring "
                             "同源；launchd 只看退出码，所以它必须只有一种读法。"))
    body.append(section("口径与限制", limits, right="页面只读"))
    return layout(base=base, title="定时任务", body="".join(body),
                  asof=data["now"][:10], built_at=built_at, current="/ops")


__all__ = ["EXIT_CN", "STATUS_CN", "ops_page"]
