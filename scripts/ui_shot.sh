#!/usr/bin/env bash
# ============================================================
# P16 截图：6 个页面 × 2 个视口，落到 reports/ui/
#
# 用法: bash scripts/ui_shot.sh [db] [asof]
#   db    默认 data/stocklab.db（**只读**：只发 GET，不写账本）
#   asof  默认 2026-09-14
#
# 依赖本机 headless chrome（已验证可用）：
#   /tmp/chrome/chrome-headless-shell-linux64/chrome-headless-shell
# 没有就跳过并明确报出来，不假装截到了。
# ============================================================
set -uo pipefail

DB="${1:-data/stocklab.db}"
ASOF="${2:-2026-09-14}"
BASE="/lab"
OUT="reports/ui"
CHROME="/tmp/chrome/chrome-headless-shell-linux64/chrome-headless-shell"
LOG="/tmp/ui_shot_server.log"

mkdir -p "$OUT"

if [ ! -x "$CHROME" ]; then
  echo "❌ 找不到 headless chrome：$CHROME —— 截图未跑"
  exit 1
fi

# 端口要**现挑一个空的**。写死 8791 会撞上正在跑的正式服务：
# 自己的服务 bind 失败退出，curl 却连上了那台**旧版本**的服务，
# 于是截出来的是上一个版本的页面，而脚本一声不响。
PORT="${PORT:-$(.venv/bin/python -c \
  'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')}"

: > "$LOG"
.venv/bin/python -m stocklab.cli.main lab serve \
  --db "$DB" --asof "$ASOF" --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null; wait $SRV 2>/dev/null' EXIT

ready=0
for _ in $(seq 1 40); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT$BASE/health"; then ready=1; break; fi
  kill -0 $SRV 2>/dev/null || break
  sleep 0.25
done
if [ "$ready" -ne 1 ]; then
  echo "❌ 服务没起来（端口 $PORT）—— 截图未跑。日志："
  cat "$LOG"
  exit 1
fi

# 确认连上的是**本版本**的服务，不是别处飘来的旧进程：
# 新样式表里一定有 --ground 这个 token。
if ! curl -fsS "http://127.0.0.1:$PORT$BASE/static/app.css" | grep -q -- "--ground"; then
  echo "❌ $PORT 上的服务不是本版本（静态样式表不含 --ground）—— 拒绝截图"
  exit 1
fi

# 页面清单：路径 → 文件名。成交详情页的 id 从库里取第一笔。
TID="$(.venv/bin/python -c "
import sqlite3,sys
c=sqlite3.connect('$DB')
r=c.execute('SELECT MIN(trade_id) FROM real_trades').fetchone()
print(r[0] if r and r[0] is not None else 1)
")"

PAGES=(
  "/|overview"
  "/trades|trades"
  "/trades/$TID|trade-detail"
  "/cash|cash"
  "/risk|risk"
  "/data|data"
)
VIEWPORTS=("1280,900|desktop" "390,844|mobile")

echo "截图目标：http://127.0.0.1:$PORT$BASE/　库=$DB　asof=$ASOF　成交 id=$TID"
echo

for vp in "${VIEWPORTS[@]}"; do
  size="${vp%%|*}"; tag="${vp##*|}"
  for page in "${PAGES[@]}"; do
    path="${page%%|*}"; name="${page##*|}"
    file="$OUT/$name-$tag.png"
    "$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
      --screenshot="$file" --window-size="$size" --virtual-time-budget=3000 \
      "http://127.0.0.1:$PORT$BASE$path" >/dev/null 2>&1 \
      || echo "⚠️  $file 失败"
  done
done

echo "生成文件："
ls -l "$OUT"/*.png | awk '{printf "  %-46s %8d 字节\n", $9, $5}'
echo
echo "合计：$(ls "$OUT"/*.png | wc -l) 张，$(du -cb "$OUT"/*.png | tail -1 | cut -f1) 字节"
