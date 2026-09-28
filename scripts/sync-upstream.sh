#!/usr/bin/env bash
# 把本地定制（nas 分支）变基到新的上游版本上。
#
#   ./scripts/sync-upstream.sh              # 上游最新 release tag
#   ./scripts/sync-upstream.sh v1.6.5       # 指定版本
#   ./scripts/sync-upstream.sh --check      # 只检查，不动工作区
#
# 冲突通常只出现在 wb_scheduler.py / wb_accounts.py，语义对齐方法见 CUSTOMIZATIONS.md。
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

BRANCH="${BRANCH:-nas}"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
CHECK_ONLY=0
TARGET=""

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -*) echo "未知参数: $arg" >&2; exit 2 ;;
    *) TARGET="$arg" ;;
  esac
done

echo "==> 拉取上游 ($UPSTREAM_REMOTE)"
git fetch --prune --tags "$UPSTREAM_REMOTE"

if [ -z "$TARGET" ]; then
  TARGET="$(git tag -l 'v*' --sort=-v:refname | head -n 1)"
fi
if ! git rev-parse --verify --quiet "$TARGET" >/dev/null; then
  echo "上游没有这个版本: $TARGET" >&2; exit 2
fi

CURRENT="$(git merge-base "$BRANCH" "$TARGET" 2>/dev/null || true)"
echo "==> 当前分支: $BRANCH"
echo "==> 目标版本: $TARGET ($(git log -1 --format=%ad --date=short "$TARGET"))"
echo "==> 定制提交:"
git log --oneline "$BRANCH" --not "$CURRENT" 2>/dev/null | sed 's/^/    /' || true
echo "==> 上游在 $TARGET 上的新提交:"
git log --oneline "$TARGET" --not "$CURRENT" 2>/dev/null | head -n 20 | sed 's/^/    /' || true

if [ "$CHECK_ONLY" = "1" ]; then
  echo "==> --check：不修改工作区"
  exit 0
fi

if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
  git checkout "$BRANCH"
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "工作区不干净，先提交或 stash 再用本脚本" >&2; exit 1
fi

echo "==> 变基 $BRANCH → $TARGET"
if ! git rebase "$TARGET" "$BRANCH"; then
  cat <<'EOF'

变基停在冲突上。处理办法：
  1. 冲突文件基本只会是 wb_scheduler.py / wb_accounts.py；
  2. 上游新增的任务要在 TASK_WINDOWS / TASK_LABELS 里补一项，
     并在 _run_cycle 里加对应的 do_xxx 分支（语义对齐见 CUSTOMIZATIONS.md）；
  3. 改完 `git add <文件> && git rebase --continue`；
  4. 放弃则 `git rebase --abort`。
EOF
  exit 1
fi

echo "==> 跑测试"
python3 tests/run_all.py

cat <<EOF

全部通过。接下来：
  1. 打标签并推送（会触发 GitHub Actions 构建镜像）：
       git tag -f nas-${TARGET#v}-nas1 && git push -f origin $BRANCH --tags
  2. NAS 上拉取：
       cd /vol1/1000/docker/wbapi-hub && ./update.sh
EOF
