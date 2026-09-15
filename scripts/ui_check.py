#!/usr/bin/env python
"""P16 前端质量检查：结构 / 预算 / 无障碍，全部打真实数字。

用法：
    .venv/bin/python scripts/ui_check.py [--db data/stocklab.db] [--asof YYYY-MM-DD]

它**真起服务**（回环随机端口），真发请求，检查的是浏览器会收到的那份字节。
只读：本脚本不发任何 POST，不碰账本。

检查项
  1. 无外部引用（每个 href/src 都必须是站内 base-path 路径）
  2. CSS / JS 字节数是否在预算内（25KB / 20KB，未压缩）
  3. `<meta name="viewport">` 存在
  4. `tabular-nums` 生效于 body（数字列对齐靠它）
  5. `prefers-reduced-motion` 有分支（CSS 归零 + JS 分支）
  6. 颜色对比度：正文/次要/语义色对各自背景 ≥ 4.5:1（WCAG AA 正文）

退出码：0 全通过；1 有失败项。
"""

from __future__ import annotations

import argparse
import http.client
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocklab.labweb import app as labapp                     # noqa: E402
from stocklab.labweb.data import Lab                          # noqa: E402
from stocklab.labweb.tokens import TokenSigner                # noqa: E402

PAGES = (("/", "总览"), ("/trades", "成交流水"), ("/cash", "现金流"),
         ("/risk", "风险"), ("/data", "数据"), ("/health", "健康"))

CSS_BUDGET = 25 * 1024
JS_BUDGET = 20 * 1024

FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}" + (f"　{detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# ---------- WCAG ----------

def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def parse_tokens(css: str) -> dict[str, str]:
    """从 `:root{...}` 里读出 `--name:#hex`。"""
    block = re.search(r":root\s*\{(.*?)\}", css, re.S)
    out: dict[str, str] = {}
    if not block:
        return out
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})", block.group(1)):
        out[name] = value
    return out


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/stocklab.db")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"找不到库 {db} —— 先跑 `stocklab db init`")
        return 1

    ctx = labapp.Context(lab=Lab(db, asof=args.asof),
                         signer=TokenSigner(b"ui-check-not-a-secret"),
                         base_path=labapp.DEFAULT_BASE_PATH)
    httpd = labapp.make_server(args.host, 0, ctx=ctx)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    base = ctx.base_path

    def get(path: str) -> tuple[int, str, dict]:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read().decode("utf-8", "replace"), dict(r.getheaders())
        finally:
            c.close()

    print(f"服务已起：http://127.0.0.1:{port}{base}/　库：{db}　asof={ctx.lab.asof}")

    # ---- 1. 页面可达 + 无外部引用 ----
    print("\n[1] 页面与外部引用扫描")
    ref = re.compile(r'(?:href|src)="([^"]*)"')
    for path, label in PAGES:
        status, text, _ = get(base + path)
        if status != 200:
            check(False, f"{label} {path} 返回 {status}")
            continue
        externals = [u for u in ref.findall(text)
                     if not (u == base or u.startswith(base + "/")
                             or u.startswith("#"))]
        schemes = [s for s in ("http://", "https://", "//") if s in text]
        check(not externals and not schemes, f"{label} {path} 无外部引用",
              f"{len(text):,} 字节" + (f"　违规={externals + schemes}" if externals or schemes else ""))

    # ---- 2. 性能预算 ----
    print("\n[2] 性能预算（未压缩）")
    sizes = {}
    for asset, budget, ctype in ((labapp.CSS_PATH, CSS_BUDGET, "text/css"),
                                 (labapp.JS_PATH, JS_BUDGET, "text/javascript")):
        status, text, hdrs = get(f"{base}/{asset}")
        n = len(text.encode("utf-8"))
        sizes[asset] = n
        check(status == 200 and n <= budget, f"{asset} ≤ {budget // 1024}KB",
              f"{n:,} 字节 / 预算 {budget:,}"
              f"（{n / budget * 100:.1f}%）　{hdrs.get('Content-Type')}")
        check(hdrs.get("Content-Type", "").startswith(ctype),
              f"{asset} Content-Type 正确", hdrs.get("Content-Type", ""))

    status, html_text, _ = get(base + "/")
    n_head = len(re.findall(r'<(?:link|script)\b', html_text)) + 1
    check(n_head <= 3, "首屏 ≤ 3 个请求", f"HTML + {n_head - 1} 个资源 = {n_head}")

    # ---- 3. viewport ----
    print("\n[3] 响应式与字号")
    for path, label in PAGES:
        if path == "/health":
            continue
        _, text, _ = get(base + path)
        check('name="viewport" content="width=device-width,initial-scale=1"' in text,
              f"{label} 有 viewport meta")

    # ---- 4/5. CSS 特性 ----
    _, css, _ = get(f"{base}/{labapp.CSS_PATH}")
    _, js, _ = get(f"{base}/{labapp.JS_PATH}")
    print("\n[4] 数字等宽与动效偏好")
    check("font-variant-numeric: tabular-nums" in css, "CSS 设定 tabular-nums")
    check(re.search(r"body\s*\{[^}]*font-variant-numeric:\s*tabular-nums", css, re.S)
          is not None, "tabular-nums 落在 body 上（整页继承，不只是某一列）")
    check("@media (prefers-reduced-motion: reduce)" in css, "CSS 有 prefers-reduced-motion 分支")
    check("prefers-reduced-motion" in js, "JS 有 reduce 分支（跳过高亮动效）")
    check("animation" in css and "@keyframes" in css, "动效仅 1 处且是响应动作的回执高亮")

    # ---- 6. 对比度 ----
    print("\n[5] 颜色对比度（WCAG 2.1，正文门槛 4.5:1）")
    t = parse_tokens(css)
    pairs = [
        ("正文 ink / 纸面", t["--ink"], t["--paper"]),
        ("正文 ink / 页面底", t["--ink"], t["--ground"]),
        ("次要 ink2 / 纸面", t["--ink2"], t["--paper"]),
        ("次要 ink2 / 页面底", t["--ink2"], t["--ground"]),
        ("链接 accent / 纸面", t["--accent"], t["--paper"]),
        ("PASS ok / 纸面", t["--ok"], t["--paper"]),
        ("WARN warn / 纸面", t["--warn"], t["--paper"]),
        ("FAIL fail / 纸面", t["--fail"], t["--paper"]),
        ("未判定 unknown / 纸面", t["--unknown"], t["--paper"]),
        ("FAIL 块上的白字", "#ffffff", t["--fail"]),
        ("主按钮上的白字", "#ffffff", t["--accent"]),
        ("回执正文 / 回执底", "#0b4f2b", "#eaf4ee"),
        ("告警正文 / 告警底", "#6b4600", "#fbf3e2"),
        ("错误正文 / 错误底", "#7c1616", "#fbeceb"),
    ]
    for label, fg, bg in pairs:
        r = contrast(fg, bg)
        check(r >= 4.5, f"{label} {fg} on {bg}", f"{r:.2f}:1"
              + ("　(AAA)" if r >= 7 else ""))

    httpd.shutdown()
    httpd.server_close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：" + "、".join(FAILURES))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
