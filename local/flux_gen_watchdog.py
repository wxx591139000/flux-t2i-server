#!/usr/bin/env python3
"""
FLUX 文生图开机看门狗（本地 · 通用）
对标转录 bot 机制：定时检测服务器是否开机+带卡 → 一键启动生成服务 → 完成文生图。
失败(服务器关机/无卡)则按间隔重试，直到完成。

用法:
  python flux_gen_watchdog.py            # 前台循环
  python flux_gen_watchdog.py --once     # 只检测+启动一次即退
  python flux_gen_watchdog.py --interval 300  # 自定义检测间隔(秒)
  python flux_gen_watchdog.py --download # 完成后把结果拉回本地
"""
import subprocess, sys, time, argparse, os, json
from datetime import datetime

SSH_ALIAS = "autodl-flux"          # ~/.ssh/config 别名
REMOTE_BASE = "/root/autodl-tmp/flux-t2i"
REMOTE_MODEL = "/root/autodl-tmp/models/FLUX.1-dev"
LOCAL_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "gen_flux.py")
LOCAL_START = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "start_gen.sh")
LOCAL_PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "prompts.json")
LOCAL_DL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "dl_curl.sh")
LOCAL_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=True)
        return r.returncode == 0, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)

def server_reachable():
    ok, _ = run(f"ssh -o ConnectTimeout=10 -o BatchMode=yes {SSH_ALIAS} echo OK", 20)
    return ok

def gpu_ready():
    ok, out = run(f"ssh -o ConnectTimeout=10 {SSH_ALIAS} 'nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1'", 20)
    return ok and "NVIDIA" in out, out

def model_ready():
    ok, _ = run(f"ssh -o ConnectTimeout=10 {SSH_ALIAS} 'test -f {REMOTE_MODEL}/DOWNLOAD_DONE && echo READY'", 20)
    return ok

def gen_running():
    ok, out = run(f"ssh -o ConnectTimeout=10 {SSH_ALIAS} 'screen -ls 2>/dev/null | grep -c fluxgen'", 20)
    return ok and '1' in out

def ensure_remote_files():
    """上传脚本到服务器（server/ 目录全部）"""
    run(f"ssh {SSH_ALIAS} 'mkdir -p {REMOTE_BASE}'", 20)
    for local, remote in [(LOCAL_PY, f"{REMOTE_BASE}/gen_flux.py"),
                          (LOCAL_PROMPTS, f"{REMOTE_BASE}/prompts.json"),
                          (LOCAL_DL, f"{REMOTE_BASE}/dl_curl.sh")]:
        if os.path.exists(local):
            run(f"scp {local} {SSH_ALIAS}:{remote}", 30)
    log("✅ 脚本已上传")

def start_generation():
    ok, out = run(f"ssh -o ConnectTimeout=15 {SSH_ALIAS} 'bash {REMOTE_BASE}/start_gen.sh'", 30)
    log(out)
    return ok

def download_results():
    """把生成结果拉回本地 output/"""
    os.makedirs(LOCAL_IMG, exist_ok=True)
    ok, out = run(f"scp -r {SSH_ALIAS}:{REMOTE_BASE}/out/* {LOCAL_IMG}/ 2>/dev/null", 120)
    log(f"📥 结果拉回: {LOCAL_IMG}")
    return ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只检测+启动一次即退")
    ap.add_argument("--interval", type=int, default=300, help="检测间隔秒(默认300)")
    ap.add_argument("--download", action="store_true", help="完成后拉回结果")
    args = ap.parse_args()

    log("🚀 FLUX 文生图看门狗启动")
    log(f"   服务器: {SSH_ALIAS} | 检测间隔: {args.interval}s")

    while True:
        if not server_reachable():
            log("🔴 服务器未开机，等待中...")
            if args.once: return
            time.sleep(args.interval); continue

        gpu_ok, ginfo = gpu_ready()
        if not gpu_ok:
            log("⚠️  服务器已开机但处于无卡模式，请切到带卡模式...")
            if args.once: return
            time.sleep(args.interval); continue
        log(f"✅ 服务器开机+带卡: {ginfo}")

        if not model_ready():
            log("⚠️  模型未下载完成，等待中...")
            if args.once: return
            time.sleep(args.interval); continue
        log("✅ 模型就绪")

        if gen_running():
            log("✅ 生成任务已在运行，看门狗退出")
            return

        log("🔄 执行一键启动...")
        ensure_remote_files()
        if start_generation():
            log("✅ 生成任务已启动")
            if args.once: return
            time.sleep(15)
            if gen_running():
                log("✅ 确认生成中。看门狗完成使命，退出。")
                if args.download:
                    time.sleep(5)
                    download_results()
                return
            log("⚠️ 生成未确认启动，继续监控...")
            time.sleep(args.interval); continue
        else:
            log("❌ 启动失败，重试中...")
            if args.once: return
            time.sleep(args.interval); continue

        if args.once: return

if __name__ == "__main__":
    main()