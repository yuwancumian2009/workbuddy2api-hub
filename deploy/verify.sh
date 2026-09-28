#!/usr/bin/env bash
# 部署后复验：确认镜像、健康、调度器窗口行为和"不重复签到"。
# 在 NAS 部署目录执行：./verify.sh
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"

DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then
  DOCKER="sudo docker"
fi

echo "=== 1. 容器实际使用的镜像 ==="
$DOCKER ps --filter name=wb-proxy --format '{{.Names}} | {{.Image}} | {{.Status}}'
$DOCKER inspect -f 'restarts={{.RestartCount}}' wb-proxy 2>/dev/null || true

echo
echo "=== 2. 健康检查 ==="
curl -sS -m 10 http://127.0.0.1:8788/health || echo "(失败)"

echo
echo "=== 3. 面板首页 ==="
curl -sS -m 10 -o /dev/null -w "HTTP %{http_code}  %{size_download} bytes\n" http://127.0.0.1:8788/

echo
echo "=== 4. 异常堆栈计数（应为 0） ==="
$DOCKER logs wb-proxy 2>&1 | grep -icE "traceback|exception"

echo
echo "=== 5. 调度器窗口行为 ==="
$DOCKER logs wb-proxy 2>&1 | grep -E "窗口|补跑|跳过|调度器已启动" | head -12

echo
echo "=== 6. 签到是否被正确拦截/执行 ==="
$DOCKER logs wb-proxy 2>&1 | grep -E "签到|未发出请求|旅行|夜猫子|国际活跃" | tail -10

echo
echo "=== 7. 账号 lastCheckin（当天已签到则不应被改写） ==="
python3 - <<'PY'
import json, glob
for f in sorted(glob.glob("accounts/*.json")):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if d.get("lastCheckin"):
        print(f, "->", d["lastCheckin"])
PY
