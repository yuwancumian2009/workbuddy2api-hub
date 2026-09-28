#!/usr/bin/env bash
# 在 NAS 的部署目录执行：拉取新镜像并重建容器（不编译）。
#
#   ./update.sh                          # 用当前 compose 文件拉取并重建
#   ./update.sh nas-v1.6.4-nas1          # 顺便把 image tag 改成指定版本再拉取
#   COMPOSE_FILE=docker-compose.pull.yml ./update.sh   # 用拉取式 compose
#
# 回滚：把 image tag 改回旧值（或直接用旧的 docker-compose.yml）再跑一次。
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
export COMPOSE_FILE

# NAS 上普通用户通常不在 docker 组，退回 sudo。
DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then
  DOCKER="sudo docker"
fi

if [ "$#" -ge 1 ]; then
  NEW_TAG="$1"
  echo "==> 切换 $COMPOSE_FILE 的 image tag → $NEW_TAG"
  cp -f "$COMPOSE_FILE" "$COMPOSE_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  sed -i "s|^\( *image: .*workbuddy2api-hub:\).*$|\1${NEW_TAG}|" "$COMPOSE_FILE"
  grep -n "image:" "$COMPOSE_FILE"
fi

echo "==> 当前使用中的镜像"
$DOCKER compose images || true

echo "==> 拉取"
$DOCKER compose pull

echo "==> 重建容器"
$DOCKER compose up -d

echo "==> 等待就绪"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8788/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "==> 容器状态"
$DOCKER compose ps

echo "==> 健康检查"
curl -fsS "http://127.0.0.1:8788/health" && echo

echo "==> 调度器日志（最近 15 行）"
$DOCKER compose logs --tail 15 | grep -E "调度器|窗口" || $DOCKER compose logs --tail 15
