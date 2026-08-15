#!/usr/bin/env python3
"""
FLUX.1-dev 权重完整性验证（CPU 冒烟测试）
在无卡模式下验证 32GB 分片权重可加载、可推理，避免切带卡(花钱)后发现损坏。
用法: source activate flux && python verify_flux.py
"""
import os, sys, time, torch

MODEL = "/root/autodl-tmp/models/FLUX.1-dev"

def check_files():
    """校验所有 safetensors 分片存在且为合法 safetensors 文件"""
    from safetensors import safe_open
    import glob
    required = [
        "ae.safetensors",
        "text_encoder/model.safetensors",
        "vae/diffusion_pytorch_model.safetensors",
        "transformer/diffusion_pytorch_model-00001-of-00003.safetensors",
        "transformer/diffusion_pytorch_model-00002-of-00003.safetensors",
        "transformer/diffusion_pytorch_model-00003-of-00003.safetensors",
        "text_encoder_2/model-00001-of-00002.safetensors",
        "text_encoder_2/model-00002-of-00002.safetensors",
    ]
    ok = True
    for rel in required:
        p = os.path.join(MODEL, rel)
        if not os.path.exists(p):
            print(f"❌ 缺失: {rel}"); ok = False; continue
        try:
            with safe_open(p, framework="pt", device="cpu") as f:
                keys = f.keys()
                n = len(list(keys))
            print(f"✅ {rel} ({os.path.getsize(p)/1e9:.2f}GB, {n} tensors)")
        except Exception as e:
            print(f"❌ 损坏: {rel}: {e}"); ok = False
    return ok

def smoke_test():
    """加载完整 pipeline，跑 1 步极小分辨率，确认端到端可推理"""
    from diffusers import FluxPipeline
    print("加载 FluxPipeline（CPU, bf16）...")
    t0 = time.time()
    pipe = FluxPipeline.from_pretrained(
        MODEL,
        torch_dtype=torch.bfloat16,
        device_map="cpu",
    )
    print(f"  加载完成 {time.time()-t0:.0f}s")
    print("跑 1 步推理（64x64, 1 step, CPU）...")
    t0 = time.time()
    try:
        img = pipe(
            "a red apple on a table",
            num_inference_steps=1,
            guidance_scale=1.0,
            width=64, height=64,
            generator=torch.Generator("cpu").manual_seed(1),
        ).images[0]
        img.save("/root/verify_smoke.png")
        print(f"  ✅ 推理成功 {time.time()-t0:.0f}s → /root/verify_smoke.png")
        print(f"  图像大小: {img.size}")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"❌ 推理失败: {e}")
        return False

if __name__ == "__main__":
    print("===== FLUX.1-dev 验证 =====")
    f_ok = check_files()
    print(f"\n文件校验: {'✅ 全部完整' if f_ok else '❌ 有损坏'}")
    if not f_ok:
        sys.exit(1)
    ok = smoke_test()
    sys.exit(0 if ok else 1)