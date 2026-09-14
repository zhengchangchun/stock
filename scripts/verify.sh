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
  echo "  ▶ pytest -q"; python3 -m pytest -q || fail=1
elif [ -f Makefile ] && grep -qE '^test:' Makefile; then
  echo "  ▶ make test"; make test || fail=1
else
  echo "  ⚠️  未发现测试命令，请手动确认或补充本项目测试命令"
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
