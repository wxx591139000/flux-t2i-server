#!/bin/bash
# flux_server_ready.sh 自测 —— 在 /tmp 沙箱里用「假 GPU + 假常驻服务」验证就绪判据。
#
# 为什么要有它（2026-09-20）：
#   看门狗的判据错了是**静默失败**：脚本语法没问题、systemd 也 active，但机器永远
#   进不了就绪态，表现是「服务器开着、网站一直转圈」，且没有任何报错。
#   这类错误只有拿到一台真机（要花钱开机）才测得出来。本脚本用 falsk 桩件在 /tmp 里
#   把正反路径都跑一遍，**不开机也能验证判据本身**。
#
# 用法（在任意 Linux 机上，含 VPS 自身）：
#   bash selftest_ready.sh                # 默认测 /opt/flux-watchdog/flux_server_ready.sh
#   bash selftest_ready.sh /path/to/flux_server_ready.sh
#
# 沙箱一律建在 /tmp/flux-ready-selftest，**不碰真实工作目录**，跑完自动清理。
set -u
READY=${1:-/opt/flux-watchdog/flux_server_ready.sh}
SB=/tmp/flux-ready-selftest
PORT=${SELFTEST_PORT:-9630}
PASS=0; FAIL=0

if [ ! -f "$READY" ]; then echo "❌ 找不到被测脚本 $READY"; exit 1; fi

cleanup() {
  [ -n "${PID:-}" ] && kill "$PID" 2>/dev/null
  rm -rf "$SB"
}
trap cleanup EXIT

rm -rf "$SB"; mkdir -p "$SB/bin" "$SB/nogpu" "$SB/work" "$SB/model"
# 桩件 1：假 nvidia-smi（模拟带卡模式）
printf '#!/bin/bash\necho "FAKE-GPU, 32760 MiB"\n' > "$SB/bin/nvidia-smi"; chmod +x "$SB/bin/nvidia-smi"
# 桩件 2：失败的 nvidia-smi（模拟无卡模式）。要遮住真机的 /usr/bin/nvidia-smi，
#         否则在 GPU 机上跑自测时这一条会被真的 nvidia-smi 顶掉 → 用例假失败。
printf '#!/bin/bash\nexit 1\n' > "$SB/nogpu/nvidia-smi"; chmod +x "$SB/nogpu/nvidia-smi"
# 桩件 3：「静默」nvidia-smi —— 命令存在、**exit 0**、但**一个字节都不输出**。
#         这是 AutoDL 无卡模式的真实形态（2026-09-20 于 flux4 实测），
#         只判退出码会被骗过，是最容易漏掉的一种。
mkdir -p "$SB/silent"; printf '#!/bin/bash\nexit 0\n' > "$SB/silent/nvidia-smi"
chmod +x "$SB/silent/nvidia-smi"
touch "$SB/model/DOWNLOAD_DONE"
# 判据里要求工作目录有这两个文件（真实环境由看门狗补传），沙箱里放空壳
: > "$SB/work/flux_resident_server.py"
: > "$SB/work/start_resident.sh"
export PATH="$SB/bin:$PATH"

chk() { # chk <说明> <期望退出码>
  local desc=$1 want=$2 got
  FLUX_WORKDIR="$SB/work" FLUX_MODEL="$SB/model" FLUX_RESIDENT_PORT="$PORT" \
    FLUX_OFFLOAD=model bash "$READY" --check >/dev/null 2>&1
  got=$?
  if [ "$got" = "$want" ]; then echo "  ✅ $desc（退出码 $got）"; PASS=$((PASS+1))
  else echo "  ❌ $desc：期望退出码 $want，实际 $got"; FAIL=$((FAIL+1)); fi
}

echo "===== flux_server_ready.sh 自测（沙箱 $SB）====="

echo "[1] 负向：nvidia-smi 不可用 → 必须判未就绪"
( export PATH="$SB/nogpu:/usr/bin:/bin"
  FLUX_WORKDIR="$SB/work" FLUX_MODEL="$SB/model" FLUX_RESIDENT_PORT="$PORT" \
    bash "$READY" --check >/dev/null 2>&1 )
[ $? -ne 0 ] && { echo "  ✅ 无卡 → 未就绪"; PASS=$((PASS+1)); } \
             || { echo "  ❌ 无卡居然判成就绪（判据失效）"; FAIL=$((FAIL+1)); }

echo "[2] 负向：有卡+有模型，但常驻没起 → 必须判未就绪"
chk "常驻未起" 1

echo "[3] 正向：常驻在跑且 model_loaded=true → 必须判就绪"
python3 - "$PORT" <<'PY' &
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps({'status':'ok','model_loaded':True}).encode())
    def log_message(self, *a): pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
PY
PID=$!
for _ in $(seq 1 20); do
  curl -s -m 1 "http://127.0.0.1:$PORT/health" | grep -q model_loaded && break
  sleep 0.5
done
chk "常驻已加载模型" 0

echo "[4] 负向：模型目录缺 DOWNLOAD_DONE → 必须判未就绪（哪怕常驻在跑）"
mv "$SB/model/DOWNLOAD_DONE" "$SB/model/DOWNLOAD_DONE.bak"
chk "模型不完整" 1
mv "$SB/model/DOWNLOAD_DONE.bak" "$SB/model/DOWNLOAD_DONE"

echo "[5] 负向：AutoDL 无卡模式（nvidia-smi 存在、exit 0、但输出为空）→ 必须判未就绪"
# 这是最阴的一种：常驻服务此时还在跑（用例 3 起的），只有"空输出"这一个信号能识别。
( export PATH="$SB/silent:$SB/bin:/usr/bin:/bin"
  FLUX_WORKDIR="$SB/work" FLUX_MODEL="$SB/model" FLUX_RESIDENT_PORT="$PORT" \
    bash "$READY" --check >/dev/null 2>&1 )
[ $? -ne 0 ] && { echo "  ✅ 空输出 nvidia-smi → 未就绪（没被 exit 0 骗过）"; PASS=$((PASS+1)); } \
             || { echo "  ❌ 空输出 nvidia-smi 被判成就绪 —— 无卡机会被当成可接单"; FAIL=$((FAIL+1)); }

echo "──────────────────────────────"
echo "通过 $PASS / 失败 $FAIL"
[ $FAIL -eq 0 ] || exit 1
