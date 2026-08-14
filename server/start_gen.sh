#!/bin/bash
# ============================================================
#  FLUX 文生图一键启动脚本（服务器端 · 通用）
#  用法: bash /root/autodl-tmp/flux-t2i/start_gen.sh [--force]
#  幂等安全：生成已在跑则跳过；--force 强制重启
#  依赖: server/gen_flux.py + 一份 prompts.json（路径见下方变量）
# ============================================================
set -u
WORKDIR=/root/autodl-tmp/flux-t2i
MODEL=/root/autodl-tmp/models/FLUX.1-dev
ENVDIR=/root/miniconda3/envs/flux/bin/python
PROMPTS="$WORKDIR/prompts.json"          # ← 换成你要生成的提示词
OUT="$WORKDIR/out"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

echo "===== FLUX 文生图一键启动 ====="

# ── Step 1: 检查 GPU 是否带卡模式 ──
if ! nvidia-smi >/dev/null 2>&1; then
    echo "⚠️  当前是无卡模式，请先在 AutoDL 控制台切到【带卡模式】再启动"
    exit 1
fi
GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)
if [ -z "$GPU" ]; then
    echo "⚠️  nvidia-smi 有输出但 GPU 名为空，确认带卡模式"
    exit 1
fi
echo "[1/4] GPU 就绪: $GPU"

# ── Step 2: 检查模型是否下载完成 ──
if [ ! -d "$MODEL/transformer" ]; then
    echo "⚠️  模型未就绪，请先下载 FLUX.1-dev 到 $MODEL"
    echo "    下载脚本: $WORKDIR/dl_curl.sh"
    exit 1
fi
if [ ! -f "$MODEL/DOWNLOAD_DONE" ]; then
    echo "⚠️  模型可能未下载完整（缺 DOWNLOAD_DONE 标记），确认 $MODEL"
    exit 1
fi
echo "[2/4] 模型就绪: $MODEL"

# ── Step 3: 检查生成脚本与提示词 ──
if [ ! -f "$WORKDIR/gen_flux.py" ]; then
    echo "⚠️  缺少 gen_flux.py"; exit 1
fi
if [ ! -f "$PROMPTS" ]; then
    echo "⚠️  缺少提示词文件: $PROMPTS"; exit 1
fi
echo "[3/4] 脚本与提示词就绪 ($PROMPTS)"

# ── Step 4: 启动生成（screen 后台，可断SSH）──
if [ $FORCE -eq 0 ] && screen -ls 2>/dev/null | grep -q "fluxgen"; then
    echo "✅ 生成任务已在运行，跳过启动（如需重启加 --force）"
    screen -ls | grep fluxgen
    exit 0
fi
screen -S fluxgen -X quit 2>/dev/null
sleep 1

mkdir -p "$OUT"
screen -dmS fluxgen bash -c "source /root/miniconda3/etc/profile.d/conda.sh && conda activate flux && cd $WORKDIR && python gen_flux.py --prompts $PROMPTS --out $OUT > $WORKDIR/gen.log 2>&1"
sleep 3
if screen -ls 2>/dev/null | grep -q "fluxgen"; then
    echo "✅ 生成任务已启动 (screen: fluxgen)"
    echo "   日志: $WORKDIR/gen.log"
    echo "   输出: $OUT/"
    echo "   查看: tail -f $WORKDIR/gen.log ; screen -r fluxgen"
else
    echo "❌ 生成任务启动失败，查看日志: $WORKDIR/gen.log"
    cat "$WORKDIR/gen.log" 2>/dev/null | tail -20
    exit 1
fi

echo "===== 启动完成 ====="