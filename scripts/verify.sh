#!/usr/bin/env bash
# ============================================
# 验证辅助脚本
# 用法: ./scripts/verify.sh [task-name]
# ============================================
set -uo pipefail

TASK="${1:-all}"
fail=0

echo "=========================================="
echo "🔍 验证: $TASK"
echo "=========================================="

echo ""
echo "--- 1. 文档结构 ---"
for dir in docs docs/architecture docs/tasks docs/plans docs/decisions docs/errors; do
  if [ -d "$dir" ]; then echo "  ✅ $dir"; else echo "  ❌ $dir 缺失"; fail=1; fi
done

echo ""
echo "--- 2. 错误日记已查阅 ---"
if [ -s docs/errors/ERROR_DIARY.md ]; then
  echo "  ✅ docs/errors/ERROR_DIARY.md 存在"
else
  echo "  ❌ docs/errors/ERROR_DIARY.md 缺失或为空"; fail=1
fi

echo ""
echo "--- 3. shell 脚本语法 ---"
shopt -s nullglob
for f in scripts/*.sh; do
  if bash -n "$f" 2>/dev/null; then echo "  ✅ $f"; else echo "  ❌ $f 语法错误"; fail=1; fi
done
shopt -u nullglob

echo ""
echo "--- 4. 项目测试 ---"
if [ -f package.json ] && grep -q '"test"' package.json 2>/dev/null; then
  echo "  ▶ npm test"; npm test --silent || fail=1
elif [ -f pytest.ini ] || [ -f pyproject.toml ]; then
  # 必须用项目 venv（系统 python3 是 3.14 且无法创建 venv）
  PY=".venv/bin/python"
  if [ ! -x "$PY" ]; then
    echo "  ❌ 缺少 $PY —— 请先运行: /opt/nanobot-venv/bin/python -m venv .venv"; fail=1
  else
    # 不要再加 `-q`：pyproject 的 `addopts` 已有 `-q`，叠加成 `-qq` 会把
    # `N passed` 汇总行整个吃掉，导致「贴不出通过条数」（见 ERROR_DIARY 2026-09-15）。
    echo "  ▶ $PY -m pytest"; "$PY" -m pytest || fail=1
  fi
elif [ -f Makefile ] && grep -qE '^test:' Makefile; then
  echo "  ▶ make test"; make test || fail=1
else
  echo "  ⚠️  未发现测试命令，请手动确认或补充本项目测试命令"
fi

echo ""
echo "--- 4b. 必须被 git 忽略的路径 ---"
if git rev-parse --git-dir >/dev/null 2>&1; then
  for p in .venv/ data/ reports/; do
    if git check-ignore -q "$p" 2>/dev/null; then
      echo "  ✅ $p 已忽略"
    else
      echo "  ❌ $p 未被忽略（会把虚拟环境/数据提交进仓库）"; fail=1
    fi
  done
else
  echo "  ⚠️  不是 git 仓库，跳过"
fi

echo ""
echo "--- 4c. 回归红线（P25 / 错误日记 #34）---"
# 两档判据：字节级 sha256 + 数字叶子级键路径比对。见 docs/baselines/redlines.json。
# ⚠️ `predict` / `backfill` 两个目标**依赖本机真实库** data/stocklab.db（未提交）；
#    库不存在时脚本会显式打印「⏭ 跳过」并退出 0 —— **不假装 hermetic**。
#    hermetic 的那一半是 tests/test_redline_baseline.py（合成夹具），已含在第 4 步里。
PY="${PY:-.venv/bin/python}"     # 第 4 步若没走到，这里也不能因 set -u 炸掉
if [ -x "$PY" ] && [ -f scripts/check_redlines.py ]; then
  "$PY" scripts/check_redlines.py || fail=1
else
  echo "  ⚠️  跳过：缺少 $PY 或 scripts/check_redlines.py"
fi

echo ""
echo "--- 5. git 工作区状态 ---"
if git rev-parse --git-dir >/dev/null 2>&1; then
  if [ -n "$(git status --porcelain)" ]; then
    echo "  ⚠️  有未提交改动："
    git status --short
  else
    echo "  ✅ 工作区干净"
  fi
else
  echo "  ⚠️  不是 git 仓库"
fi

echo ""
echo "=========================================="
if [ "$fail" -eq 0 ]; then
  echo "✅ 验证通过: $TASK"
else
  echo "❌ 验证失败: $TASK（见上方 ❌ 项）"
fi
echo "=========================================="
exit "$fail"
