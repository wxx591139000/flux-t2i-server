#!/bin/bash
# ============================================================
#  FLUX.1-dev 分片权重 curl 流式下载（适配无卡 2GB 内存）
#  huggingface_hub 在无卡模式反复失败 → 改用 curl 断点续传
#  内存占用极小（流式写盘），2GB 无压力；断了自动续传
#  用法: bash dl_curl.sh（screen 后台跑）
# ============================================================
set -u
# 密钥从环境变量读，不硬编码入库（防 GitHub 泄露）
# 运行前: export HF_TOKEN=hf_xxx
TOKEN="${HF_TOKEN:?需设置 HF_TOKEN 环境变量}"
BASE="https://hf-mirror.com/black-forest-labs/FLUX.1-dev/resolve/main"
MODEL="/root/autodl-tmp/models/FLUX.1-dev"
LOG="/root/dlcurl.log"

# 分片清单: "相对路径 | 期望字节数"（来自 HF API 真实 LFS 大小）
FILES=(
  "transformer/diffusion_pytorch_model-00001-of-00003.safetensors | 9983040304"
  "transformer/diffusion_pytorch_model-00002-of-00003.safetensors | 9949328904"
  "transformer/diffusion_pytorch_model-00003-of-00003.safetensors | 3870584832"
  "text_encoder_2/model-00001-of-00002.safetensors | 4994582224"
  "text_encoder_2/model-00002-of-00002.safetensors | 4530066360"
)

log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

dl_one(){
  local rel="$1" url="$BASE/$rel" dest="$MODEL/$rel" size="$2"
  mkdir -p "$(dirname "$dest")"
  # 已完整则跳过
  if [ -f "$dest" ] && [ "$(stat -c%s "$dest")" -ge "$size" ]; then
    log "✅ 已完整跳过: $rel"; return 0
  fi
  log "⬇️  开始: $rel (目标 ${size} B)"
  local last=-1 no_growth=0 retry=0
  while [ "$(stat -c%s "$dest" 2>/dev/null || echo 0)" -lt "$size" ]; do
    # curl 断点续传: -C - 从已有字节续传
    curl -sL -C - -o "$dest" \
      -H "Authorization: Bearer $TOKEN" \
      --max-time 600 \
      "$url" >/dev/null 2>&1
    local cur=$(stat -c%s "$dest" 2>/dev/null || echo 0)
    if [ "$cur" -ge "$size" ]; then break; fi
    # 停滞检测：若 3 次续传无增长（连接假死），kill 重来
    if [ "$cur" -eq "$last" ]; then no_growth=$((no_growth+1)); else no_growth=0; fi
    last=$cur
    log "   ${rel}: ${cur}/$size B (retry=$retry stall=$no_growth)"
    if [ "$no_growth" -ge 3 ]; then
      log "⚠️  停滞，重启 curl 续传"
      no_growth=0
    fi
    retry=$((retry+1))
    [ "$retry" -gt 500 ] && { log "❌ 重试过多，放弃: $rel"; return 1; }
    sleep 2
  done
  log "✅ 完成: $rel"
}

log "===== curl 流式分片下载启动 (无卡 2GB 适配) ====="
for entry in "${FILES[@]}"; do
  rel="${entry%%|*}"; rel="${rel// /}"; size="${entry##*|}"; size="${size// /}"
  dl_one "$rel" "$size" || { log "!! 失败: $rel"; }
done
log "===== 全部完成 ====="
touch "$MODEL/DOWNLOAD_DONE"   # 与 start_gen.sh / 看门狗检查位置一致