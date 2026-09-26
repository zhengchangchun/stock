#!/usr/bin/env python3
"""生成「stocklab AI 操盘全流程」一张图（Pillow，无 ffmpeg/drawtext 依赖）。"""
from PIL import Image, ImageDraw, ImageFont

CN = "/System/Library/Fonts/Hiragino Sans GB.ttc"
BG = (247, 245, 241)
INK = (46, 42, 38)
GRAY = (110, 104, 96)
LINE = (206, 199, 189)
CARD = (255, 255, 255)
ACCENTS = [(59, 111, 182), (59, 111, 182), (106, 61, 154), (192, 24, 36),
           (30, 120, 50), (185, 138, 94), (90, 90, 96)]

W = 2000
f_title = ImageFont.truetype(CN, 46)
f_sub = ImageFont.truetype(CN, 24)
f_h = ImageFont.truetype(CN, 30)
f_b = ImageFont.truetype(CN, 25)
f_s = ImageFont.truetype(CN, 22)
f_mono = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 23)

STAGES = [
    ("①  数据层  ingest（唯一写入口）", [
        "ingest index sh000300 + sh000905（顺带前滚交易日历）→ bars --days 30 → actions",
        "→ valuation → moneyflow        质量闸门：脏行整批拒写 · 复权链 adj_factors",
        "宇宙投影 universe_memberships（805 只）；取研究池必须显式 --universe，fail-closed",
    ]),
    ("②  复权 / 特征", [
        "data/adjust.py＝唯一价格出口（load_chain / load_bars_adjusted）→ features_daily",
        "单条脏条款按 code 隔离（UnpriceableTerms）· ETF 无复权链 → skipped（ADR-008）",
    ]),
    ("③  候选池  candidate run（12 步主干，顺序不可改）", [
        "排雷(3) → 插桩0 行业排雷(4) → 淘汰入库(5) → 三池分流 short/mid/long(6)",
        "→ 插桩1/2/3 打分(7) → 插桩4 风险调整(8) → 入库(9) → 快照(10) → 报告(11) → 触发(12)",
        "幂等键 (asof, run_kind)；缺任一 active 插桩 → NoActivePlugin，不留半截快照",
    ]),
    ("④  生产模型  pit-rw-v1.0.2（手写统计式，非 ML）", [
        "predict run --asof 今天：方向 / 区间 range_80 / 关键位  →  verify pending 到期打分",
        "review daily：滚动准确率【按 LIVE|REPLAY × model_version 分桶，不可相加】",
        "方向命中 ≈ 38%（随机 50%）；区间覆盖 82.59%；关键位 91.25%",
    ]),
    ("⑤  模块2 操盘  m2 daily（AI 选股在这里）", [
        "通路 B：镜像 arm-now（人工流水复刻）——总是跑",
        "通路 A：每个「在飞版本」跑一遍     A1 选股 → 主干翻译成订单并成交 →",
        "                                  A2 卖出指令 → 结算 → A3 逐持仓预测",
        "打分 m2 score（target_date ≤ asof 且未打分）· 资金闸门在执行层：CashShortfall",
    ]),
    ("⑥  账本 / 对照臂", [
        "paper_trades（唯一写入口，append-only）· paper_nav_daily（每账户每日一行）",
        "arm-hold · arm-now · arm-discipline-05/10/15 · arm-agent(-v1/-ds-v1/-ds-v2)",
        "arm-agent-random 是归因必需：没有随机对照臂，一切结论「不可归因」",
    ]),
    ("⑦  页面  /lab/", [
        "总览 · 模拟盘对照 · 模块2 · 候选池 · 定时任务 · 成交流水 · 现金流 · 风险 · 数据 · 健康检查",
    ]),
]

SIDE = [
    ("时钟：macOS launchd", [
        "close    每交易日 15:30      14 步收盘链",
        "patrol   工作日 09:00–15:30  每 30 分钟",
        "monthly  每月 1 日 08:00     长窗采集+复盘",
        "项目内无常驻进程；回执 latest-<job>.json",
    ], (185, 138, 94)),
    ("侧环（不在收盘链上）", [
        "插桩生命周期: submit → sandbox 契约预检",
        "  → 人工 approve → active →（新版）archive",
        "实验流水线: 预注册 → 单变量变体 → 台账",
        "  （9 个变体全部被否证，append-only）",
        "红线 scripts/verify.sh: 三目标 sha 逐位",
        "  比对 + 库指纹（只读、确定性、禁 regen）",
    ], (106, 61, 154)),
    ("护栏（写死，有测试钉住）", [
        "禁方向择时 / 禁参数搜索 / 不输出买卖建议",
        "不接券商 · 不杠杆负权重 · 不池外标的",
        "不用未来数据（PIT 守卫 check_no_lookahead）",
        "paper/ 不许 import predict/verify/risk/plugin",
    ], (192, 24, 36)),
]

PAD = 60
LEFT_W = 1230
GAP = 22
ARROW = 30
title_h = 120


def wrap_draw(d, xy, text, font, fill, max_w):
    """按宽度硬折行（中英混排按像素算）。"""
    words = list(text)
    line = ""
    lines = []
    for ch in words:
        if d.textlength(line + ch, font=font) <= max_w:
            line += ch
        else:
            lines.append(line)
            line = ch
    if line:
        lines.append(line)
    x, y = xy
    for i, ln in enumerate(lines):
        d.text((x, y + i * (font.size + 8)), ln, font=font, fill=fill)
    return len(lines)


def main():
    # 先算高度
    heights = []
    for _, lines in STAGES:
        heights.append(46 + len(lines) * 34 + 26)
    body_h = sum(heights) + ARROW * (len(STAGES) - 1)
    H = title_h + 20 + body_h + PAD
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 标题
    d.text((PAD, 48), "stocklab · AI 操盘全流程", font=f_title, fill=INK)
    d.text((PAD, 104), "2026-09-25 现状快照 · 图上每一步都有对应 CLI 与落库表 · 详细版见 docs/architecture/ai-pipeline.md",
           font=f_sub, fill=GRAY)
    d.line((PAD, title_h - 8, W - PAD, title_h - 8), fill=LINE, width=2)

    # 主栈
    y = title_h + 20
    for i, (head, lines) in enumerate(STAGES):
        h = heights[i]
        accent = ACCENTS[i % len(ACCENTS)]
        d.rounded_rectangle((PAD, y, PAD + LEFT_W, y + h), radius=16,
                            fill=CARD, outline=LINE, width=2)
        d.rounded_rectangle((PAD, y, PAD + 10, y + h), radius=5, fill=accent)
        d.text((PAD + 28, y + 14), head, font=f_h, fill=accent)
        ty = y + 58
        for ln in lines:
            d.text((PAD + 40, ty), ln, font=f_b, fill=INK)
            ty += 34
        # 箭头
        if i < len(STAGES) - 1:
            cx = PAD + 60
            d.line((cx, y + h + 4, cx, y + h + ARROW - 6), fill=GRAY, width=3)
            d.polygon([(cx - 8, y + h + ARROW - 12), (cx + 8, y + h + ARROW - 12),
                       (cx, y + h + ARROW - 1)], fill=GRAY)
        y += h + ARROW

    # 右侧栏
    sx = PAD + LEFT_W + 40
    sw = W - sx - PAD
    sy = title_h + 20
    for head, lines, accent in SIDE:
        need = 40
        tmp = Image.new("RGB", (10, 10))
        td = ImageDraw.Draw(tmp)
        wrapped = []
        for ln in lines:
            n = wrap_draw(td, (0, 0), ln, f_b if len(lines) < 7 else f_s, INK, sw - 56)
            wrapped.append((ln, n))
        hh = 46 + sum(n * 30 for _, n in wrapped) + 24
        d.rounded_rectangle((sx, sy, sx + sw, sy + hh), radius=16, fill=CARD,
                            outline=LINE, width=2)
        d.rounded_rectangle((sx, sy, sx + sw, sy + 8), radius=4, fill=accent)
        d.text((sx + 24, sy + 18), head, font=f_h, fill=accent)
        ty = sy + 62
        for ln, n in wrapped:
            wrap_draw(d, (sx + 24, ty), ln, f_b, INK, sw - 48)
            ty += n * 30
        sy += hh + 28

    out = "/Users/zhengchangchun/.nanobot/workspace/artifacts/stock/2026-09-25-ai-pipeline.png"
    import os
    os.makedirs(os.path.dirname(out), exist_ok=True)
    img.save(out, "PNG")
    print(out, img.size)


if __name__ == "__main__":
    main()
