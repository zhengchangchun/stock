#!/usr/bin/env python3
"""AI 模拟盘「买卖点」图 —— 把每条臂的净值曲线 + 当天的买入/卖出直接画在一张图上。

只读真库（`file:...?mode=ro`），零写入。数据源：
  paper_nav_daily  逐日净值 / 累计收益（cum_return 已按净入金口径）
  paper_trades     逐笔成交（date / account_id / side / code / qty / fill_price / fee_total）
  bars_daily       沪深300 用 index_300_level 列（真库里已有），不另取

用法：
  ~/.nanobot/workspace/.venvs/video/bin/python paper-chart.py \
      --out ~/.nanobot/workspace/artifacts/stock/<日期>/ai-paper-buysell.png

设计：上图=累计收益曲线（含买卖点标记 B 买 / S 卖，标在**该臂当天那条线**上），
下图=成交明细表（日期/臂/动作/标的/数量/成交价/费用）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date as _date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DB = "/Users/zhengchangchun/Documents/Workspace/stock/data/stocklab.db"

# 每条线：(账户, 颜色, 线宽, 虚线?, 是否画买卖标记) —— **显示名一律查 arm-names.json**
LINES = [
    ("sh000300",         (150, 150, 155), 3, True,  False),
    ("arm-hold",         ( 70, 100, 140), 4, False, False),
    ("arm-agent-random", (120, 190, 120), 3, True,  True),
    ("arm-agent-v1",     (205, 140,  40), 4, False, True),
    ("arm-agent-ds-v2",  (200,  40,  40), 5, False, True),
    ("arm-agent-ds-v1",  (120,  60, 170), 5, False, True),
    ("arm-agent-ds-v3",  ( 20, 130, 130), 5, False, True),
]
NAMES_FILE = Path("/Users/zhengchangchun/.nanobot/workspace/tools/stock/arm-names.json")


def load_names() -> dict:
    try:
        return json.loads(NAMES_FILE.read_text(encoding="utf-8"))["arms"]
    except Exception:
        return {}


NAMES = load_names()


def label_of(key: str) -> str:
    rec = NAMES.get(key) or {}
    return str(rec.get("name") or key)


def short_of(key: str) -> str:
    rec = NAMES.get(key) or {}
    return str(rec.get("short") or key)

BG = (255, 255, 255)
INK = (30, 30, 34)
GRID = (232, 232, 236)
BUY = (200, 30, 30)
SELL = (20, 140, 70)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Songti.ttc",
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def load(db: str):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    navs: dict[str, dict[str, float]] = {}
    index: dict[str, float] = {}
    for r in c.execute("select account_id, date, cum_return, nav, net_deposits, index_300_level"
                       " from paper_nav_daily order by date"):
        if r["cum_return"] is not None:
            navs.setdefault(r["account_id"], {})[r["date"]] = round(float(r["cum_return"]) * 100, 4)
        if r["index_300_level"]:
            index[r["date"]] = float(r["index_300_level"])
    trades = [dict(r) for r in c.execute(
        "select account_id, date, side, code, qty, fill_price, fee_total, reason"
        " from paper_trades order by date, account_id, side, trade_id")]
    accounts = {r["account_id"]: dict(r) for r in c.execute(
        "select account_id, arm, start_date, initial_nav from paper_accounts")}
    dates = sorted({d for s in navs.values() for d in s} | set(index))
    c.close()
    return navs, index, trades, accounts, dates


def series_for(key: str, navs, index, dates, start: str):
    """返回 [(date, pct)]，起点按 start 锚到 0。"""
    if key == "sh000300":
        base = next((index[d] for d in dates if d in index), None)
        pts = [(start, 0.0)] + [(d, round((index[d] / base - 1) * 100, 4))
                                for d in dates if d in index]
        return pts
    s = navs.get(key) or {}
    return [(start, 0.0)] + [(d, s[d]) for d in dates if d in s]


def dashed(draw, pts, color, width, dash=14, gap=10):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        import math
        L = math.hypot(x1 - x0, y1 - y0)
        if L == 0:
            continue
        n = max(1, int(L // (dash + gap)) + 1)
        for i in range(n):
            a = i * (dash + gap) / L
            b = min(1.0, (i * (dash + gap) + dash) / L)
            draw.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a),
                       (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)], fill=color, width=width)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default=None)
    a = ap.parse_args()

    navs, index, trades, accounts, dates = load(a.db)
    start = min(accounts.values(), key=lambda r: r["start_date"])["start_date"]
    xs = sorted({start} | set(dates))
    W, H = 1920, 1540
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_title, f_sub = font(46), font(26)
    f_ax, f_lab, f_lg = font(24), font(22), font(24)
    f_cell = font(23)

    title = a.title or "AI 模拟操盘 · 什么时候买 / 什么时候卖（净值曲线 + 买卖点）"
    d.text((70, 44), title, fill=INK, font=f_title)
    sub = (f"真库只读 · 起跑日 {start} · 至最后交易日 {xs[-1]} · 共 {len(xs)} 个交易日 · "
           f"口径：累计收益 = 净值/净入金 - 1（已扣各臂自己的成本）")
    d.text((70, 104), sub, fill=(110, 110, 116), font=f_sub)

    # ---------- 上图：曲线 ----------
    L, R, T, B = 150, W - 120, 200, 800
    plot: dict[str, list[tuple[float, float]]] = {}
    all_pct = [0.0]
    for key, color, width, is_dash, _m in LINES:
        pts = series_for(key, navs, index, xs, start)
        plot[key] = pts
        all_pct += [p for _d, p in pts]
    lo, hi = min(all_pct), max(all_pct)
    pad = max(0.25, (hi - lo) * 0.16)
    lo, hi = lo - pad, hi + pad

    def X(dstr): return L + (xs.index(dstr) / max(1, len(xs) - 1)) * (R - L)
    def Y(pct): return B - (pct - lo) / (hi - lo) * (B - T)

    for gy in [lo + (hi - lo) * i / 6 for i in range(7)]:
        y = Y(gy)
        d.line([(L, y), (R, y)], fill=GRID, width=2)
        d.text((L - 18, y - 12), f"{gy:+.2f}%", fill=(120, 120, 126), font=f_ax, anchor="ra")
    y0 = Y(0.0)
    d.line([(L, y0), (R, y0)], fill=(200, 200, 206), width=3)
    for i, dstr in enumerate(xs):
        x = X(dstr)
        d.line([(x, B), (x, B + 10)], fill=(170, 170, 176), width=2)
        lbl = dstr[5:]
        if len(xs) > 12 and i % 2:
            continue
        d.text((x, B + 18), lbl, fill=(105, 105, 112), font=f_ax, anchor="ma")

    for key, color, width, is_dash, _m in LINES:
        pts = [(X(dt), Y(p)) for dt, p in plot[key]]
        if len(pts) == 1:
            continue
        if is_dash:
            dashed(d, pts, color, width)
        else:
            d.line(pts, fill=color, width=width, joint="curve")

    # 图例（右上，两列）—— 名字 + 账户 id，两者都给
    lx, ly = L + 20, T + 8
    for i, (key, color, width, is_dash, _m) in enumerate(LINES):
        cx = lx + (i % 2) * 620
        cy = ly + (i // 2) * 36
        d.line([(cx, cy + 12), (cx + 52, cy + 12)], fill=color, width=width)
        d.text((cx + 64, cy - 2), label_of(key), fill=(45, 45, 50), font=f_lg)
        off = d.textbbox((0, 0), label_of(key), font=f_lg)[2]
        d.text((cx + 76 + off, cy + 4), key, fill=(160, 160, 166), font=font(18))

    # 买卖标记：按（臂, 日期）合并成**一个点**，标签列全代码 —— 同一天多笔不会挤成一团
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for t in trades:
        if t["account_id"] not in plot:
            continue
        groups.setdefault((t["account_id"], t["date"], t["side"]), []).append(t)

    boxes: list[tuple[float, float, float, float]] = []
    for (arm, dt, side), items in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        pts = {d_: p for d_, p in plot[arm]}
        if dt not in pts:
            continue
        x, y = X(dt), Y(pts[dt])
        buy = side == "buy"
        col = BUY if buy else SELL
        d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=col, outline=(255, 255, 255), width=3)
        codes = "·".join(sorted({t["code"] for t in items}))
        tag = ("买" if buy else "卖") + (f" x{len(items)} " if len(items) > 1 else " ") + codes
        tw = d.textbbox((0, 0), tag, font=f_cell)[2]
        bw, bh = tw + 18, 34
        left = x > R - (bw + 60)
        for k in range(6):                      # 竖直错开，避免同一天不同臂叠在一起
            tx = (x - bw - 26) if left else (x + 26)
            ty = y - 17 + (k * 40 * (1 if k % 2 == 0 else -1))
            box = (tx, ty, tx + bw, ty + bh)
            if ty < T + 4 or ty + bh > B - 4:
                continue
            if all(not (box[0] < bx2 and box[2] > bx1 and box[1] < by2 and box[3] > by1)
                   for bx1, by1, bx2, by2 in boxes):
                boxes.append(box)
                break
        else:
            tx, ty = (x - bw - 26) if left else (x + 26), y - 17
            boxes.append((tx, ty, tx + bw, ty + bh))
        ax = tx + bw if left else tx
        ay = ty + bh / 2
        d.line([(x, y), (ax, ay)], fill=col, width=2)
        d.rectangle([tx, ty, tx + bw, ty + bh], fill=(255, 255, 255), outline=col, width=2)
        d.text((tx + 9, ty + 4), tag, fill=col, font=f_cell)
        for t in items:
            t["mark"] = (dt, arm)

    # ---------- 下图：成交明细 ----------
    ty = 1000
    AI_ARMS = {"arm-agent", "arm-agent-v1", "arm-agent-ds-v1", "arm-agent-ds-v2",
               "arm-agent-ds-v3", "arm-agent-random"}
    shown = [t for t in trades if t["account_id"] in AI_ARMS]
    skipped = len(trades) - len(shown)
    d.text((70, ty - 68), f"成交明细（AI 臂 {len(shown)} 笔 · paper_trades append-only）",
           fill=INK, font=font(32))
    cols = [("日期", 70, 150), ("谁（显示名）", 245, 200), ("动作", 470, 60), ("标的", 600),
            ("数量", 780, 90), ("成交价（含滑点）", 910, 200), ("费用", 1140, 80),
            ("决策依据（截断）", 1250)]
    hy = ty
    d.line([(60, hy - 8), (W - 60, hy - 8)], fill=(200, 200, 206), width=2)
    for c in cols:
        d.text((c[1], hy), c[0], fill=(90, 90, 96), font=f_lg)
    hy += 40
    d.line([(60, hy), (W - 60, hy)], fill=(200, 200, 206), width=2)
    hy += 10
    for t in shown:
        buy = t["side"] == "buy"
        vals = [t["date"], f"{short_of(t['account_id'])}",
                "买入" if buy else "卖出", t["code"], f"{t['qty']:,}",
                f"￥{t['fill_price']:.4f}", f"￥{t['fee_total']:.2f}",
                (t["reason"] or "")[:36]]
        for (label, x, *_), v in zip(cols, vals):
            col = BUY if (label == "动作" and buy) else SELL if label == "动作" else (60, 60, 66)
            d.text((x, hy), str(v), fill=col, font=f_cell)
        hy += 38
        d.line([(60, hy - 10), (W - 60, hy - 10)], fill=(238, 238, 242), width=1)

    extra = f"另有 {skipped} 笔非 AI 臂成交未列（09-15 起跑种子 / 纪律臂 ETF 定投）。" if skipped else ""
    legend = (f"阅读方式：圆点标在该臂当天的净值曲线上，红=买入、绿=卖出；标签是标的代码（同日多笔合并成一个点）。{extra}"
              "AI一版 的 4 笔买入在 09-23 被判「差额不足 1 手 → 不动」，所以图上只有它的卖出点。")
    pairs = " · ".join(f"{short_of(k)}={k}" for k, *_ in LINES)
    d.text((70, H - 42), "名字 = 账户 id：" + pairs, fill=(150, 150, 156), font=font(20))
    d.text((70, H - 80), legend, fill=(120, 120, 126), font=font(22))

    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"wrote {out} ({out.stat().st_size:,} B)")
    print(f"trades={len(trades)} dates={len(xs)} arms_with_marks={sorted({t['account_id'] for t in trades})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
