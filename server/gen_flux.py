#!/usr/bin/env python3
"""
FLUX.1 通用批量文生图脚本（服务器端）
服务所有文生图需求，与具体业务解耦。读 prompts.json，逐张生成。

用法:
  python gen_flux.py --prompts prompts.json --out ./out [选项]
  python gen_flux.py --prompts prompts.json --out ./out --start 0 --end 5 --steps 25

prompts.json 格式:
  {
    "style_prefix": "cinematic, photorealistic, ...",   # 可选，全局追加到每个 prompt
    "negative_prompt": "blurry, low quality, ...",       # 可选
    "notes": [                                            # 分组（每个分组一个输出子目录）
      {"note": "组名", "images": [
        {"key": "cover", "prompt": "..."},
        {"key": "P1",    "prompt": "..."}
      ]}
    ]
  }
"""
import json, os, sys, argparse, glob, time
import torch
from diffusers import FluxPipeline

def sanitize(name):
    return "".join(c for c in name if c not in '\\/:*?"<>|').strip()[:40]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", default="prompts.json")
    ap.add_argument("--out", default="/root/autodl-tmp/flux_out")
    ap.add_argument("--model", default="/root/autodl-tmp/models/FLUX.1-dev")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=-1)
    ap.add_argument("--steps", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--height", type=int, default=1024)
    args = ap.parse_args()

    if not os.path.exists(args.prompts):
        print(f"❌ 找不到提示词文件: {args.prompts}"); sys.exit(1)

    data = json.load(open(args.prompts, encoding="utf-8"))
    notes = data["notes"]
    neg = data.get("negative_prompt", "")
    style = data.get("style_prefix", "")

    # 展平所有图
    tasks = []
    for ni, n in enumerate(notes):
        for img in n["images"]:
            tasks.append((ni, n["note"], img["key"], img["prompt"]))
    if args.end >= 0:
        tasks = tasks[args.start:args.end]
    else:
        tasks = tasks[args.start:]
    print(f"任务总数: {len(tasks)}")

    print(f"加载模型 {args.model} ...")
    pipe = FluxPipeline.from_pretrained(args.model, torch_dtype=torch.bfloat16)
    # 大显存用 cpu offload 最稳（兼容低显存/内存）
    pipe.enable_sequential_cpu_offload()
    print("模型加载完成")

    os.makedirs(args.out, exist_ok=True)
    for idx, (ni, note, key, prompt) in enumerate(tasks):
        full_prompt = f"{prompt}, {style}" if style else prompt
        seed = args.seed + idx          # 同组相邻 seed → 风格一致
        generator = torch.Generator("cpu").manual_seed(seed)
        note_dir = os.path.join(args.out, f"{ni:02d}_{sanitize(note)}")
        os.makedirs(note_dir, exist_ok=True)
        out_path = os.path.join(note_dir, f"{key}.png")
        if os.path.exists(out_path):
            print(f"[{idx+1}/{len(tasks)}] 跳过(已存在): {out_path}")
            continue
        t0 = time.time()
        try:
            image = pipe(
                full_prompt,
                negative_prompt=neg,
                num_inference_steps=args.steps,
                guidance_scale=3.5,
                width=args.width, height=args.height,
                generator=generator,
            ).images[0]
            image.save(out_path)
            print(f"[{idx+1}/{len(tasks)}] ✅ {note} / {key} ({time.time()-t0:.0f}s) seed={seed}")
        except Exception as e:
            print(f"[{idx+1}/{len(tasks)}] ❌ {note}/{key}: {e}")
        torch.cuda.empty_cache()

    print("🎉 全部完成")

if __name__ == "__main__":
    main()