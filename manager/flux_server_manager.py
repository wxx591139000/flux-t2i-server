#!/usr/bin/env python3
"""
FLUX 服务器管理器（对标转录bot orchestrator 的服务器管理模式）
- 监控小红书产线写入的配图任务队列（异步）
- 需要时飞书通知 owner 开机/切带卡
- 服务器可达时自动拉起生成服务（start_gen.sh，幂等）
- 生成完成 → 拉回图片 → 替换 Obsidian 稿子 <!--IMG:N--> 占位符 → 飞书通知

用法:
  python flux_server_manager.py                 # 常驻 daemon
  python flux_server_manager.py --once          # 处理一次队列即退（测试用）
  python flux_server_manager.py --interval 60   # 检测间隔(秒)
"""
import os
import sys
import json
import time
import shutil
import logging
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

# 确保项目根在 sys.path，才能 import manager.*
BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager.feishu_notify import notify_owner
JOB_DIR = Path(os.environ.get('FLUX_JOB_DIR',
    r'E:\ObsidianHouse\xiaohongshu-workspace\data\flux_jobs'))
DONE_DIR = JOB_DIR / '_done'
OBSIDIAN_IMAGES = Path(os.environ.get('FLUX_OBSIDIAN_IMAGES',
    r'E:\ObsidianHouse\ObsidW\02 Projects项目\hongshu\02-稿子\images\flux_out'))

# 服务器配置
SSH_ALIAS = 'autodl-flux'
REMOTE_BASE = '/root/autodl-tmp/flux-t2i'
REMOTE_MODEL = '/root/autodl-tmp/models/FLUX.1-dev'
LOCAL_GEN = BASE_DIR / 'server' / 'gen_flux.py'

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(BASE_DIR / 'manager' / 'flux_manager.log', encoding='utf-8'),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger('flux_manager')


# ═══════════════ SSH / 服务器操作 ═══════════════

def run(cmd, timeout=30):
    """执行命令。用 bash -lc（避免 Windows cmd 解析管道/引号）+ UTF-8 解码（避免 GBK 解码中文失败）。"""
    try:
        r = subprocess.run(['bash', '-lc', cmd], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return r.returncode == 0, (r.stdout or '').strip()
    except subprocess.TimeoutExpired:
        return False, 'TIMEOUT'
    except Exception as e:
        return False, str(e)


def server_reachable() -> bool:
    ok, _ = run(f'ssh -o ConnectTimeout=8 -o BatchMode=yes {SSH_ALIAS} echo ok', 15)
    return ok


def gpu_ready() -> tuple:
    ok, out = run(f'ssh -o ConnectTimeout=8 {SSH_ALIAS} '
                  "'nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1'", 15)
    return ok and 'NVIDIA' in out, out


def model_ready() -> bool:
    ok, _ = run(f'ssh -o ConnectTimeout=8 {SSH_ALIAS} '
                f"'test -f {REMOTE_MODEL}/DOWNLOAD_DONE && echo READY'", 15)
    return ok


def gen_running() -> bool:
    ok, out = run(f'ssh -o ConnectTimeout=8 {SSH_ALIAS} '
                  "'screen -ls 2>/dev/null | grep -c fluxgen'", 15)
    return ok and '1' in out


def upload_prompts(job: dict) -> bool:
    """把 job 的提示词写成 prompts.json 并上传服务器"""
    notes = [{
        'note': job['note_title'],
        'images': [{"key": f"P{i+1}", "prompt": img['prompt']}
                   for i, img in enumerate(job['images'])],
    }]
    prompts_path = BASE_DIR / 'manager' / 'tmp_prompts.json'
    prompts_path.write_text(json.dumps({'notes': notes}, ensure_ascii=False), encoding='utf-8')
    local_p = str(prompts_path).replace('\\', '/')
    local_g = str(LOCAL_GEN).replace('\\', '/')
    ok1, _ = run(f'scp {local_p} {SSH_ALIAS}:{REMOTE_BASE}/prompts.json', 30)
    ok2, _ = run(f'scp {local_g} {SSH_ALIAS}:{REMOTE_BASE}/gen_flux.py', 30)
    return ok1 and ok2


def start_generation() -> bool:
    ok, out = run(f'ssh -o ConnectTimeout=15 {SSH_ALIAS} '
                  f'bash {REMOTE_BASE}/start_gen.sh', 60)
    log.info(out)
    return ok


def wait_generation(n_images: int, timeout_sec=3600) -> bool:
    """轮询直到生成完成（所有图片出现或 gen.log 标完成）"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(20)
        ok, out = run(f'ssh -o ConnectTimeout=8 {SSH_ALIAS} '
                      f'find {REMOTE_BASE}/out -name "*.png" | wc -l', 15)
        if ok and out.strip().isdigit() and int(out.strip()) >= n_images:
            log.info(f'✅ 生成完成: {out.strip()}/{n_images} 张')
            return True
        # 检查是否报错
        ok2, err = run(f'ssh -o ConnectTimeout=8 {SSH_ALIAS} '
                       f'tail -5 {REMOTE_BASE}/gen.log 2>/dev/null | tr "\\r" "\\n" | grep -E "❌|Error|Traceback" | tail -1', 15)
        if ok2 and err:
            log.warning(f'⚠️  生成疑似报错: {err[:120]}')
    log.warning('⚠️  生成超时')
    return False


def pull_images(job: dict) -> str:
    """把生成图拉回本地 02-稿子/images/flux_out/<batch>/，返回目录"""
    batch = job['job_id']
    dest = OBSIDIAN_IMAGES / batch
    dest.mkdir(parents=True, exist_ok=True)
    dest_posix = str(dest).replace('\\', '/')
    ok, _ = run(f'scp -r {SSH_ALIAS}:{REMOTE_BASE}/out/. "{dest_posix}" 2>/dev/null', 120)
    # gen_flux 输出在 out/00_<title>/xxx.png，需上移一层到 batch/
    moved = 0
    for sub in dest.iterdir():
        if sub.is_dir():
            for f in sub.glob('*.png'):
                shutil.move(str(f), str(dest / f.name))
                moved += 1
            shutil.rmtree(sub, ignore_errors=True)
    # 直接落在 dest 的也保留
    moved += len(list(dest.glob('*.png')))
    log.info(f'📥 拉回 {moved} 张到 {dest}')
    return str(dest)


def insert_into_note(job: dict) -> bool:
    """把 <!--IMG:N--> 替换为 ![[images/flux_out/<batch>/PN.png|500]]"""
    note_path = Path(job['obsidian_note'])
    if not note_path.exists():
        log.error(f'❌ 稿子不存在: {note_path}')
        return False
    text = note_path.read_text(encoding='utf-8')
    batch = job['job_id']
    replaced = 0
    for i, img in enumerate(job['images']):
        n = i + 1
        marker = f'<!--IMG:{n}-->'
        link = f'![[images/flux_out/{batch}/P{n}.png|500]]'
        if marker in text:
            text = text.replace(marker, link)
            replaced += 1
        else:
            log.warning(f'  ⚠️  稿子中未找到 {marker}，追加到末尾')
            text = text + f'\n\n{link}'
            replaced += 1
    note_path.write_text(text, encoding='utf-8')
    log.info(f'🖼️  稿子已插入 {replaced} 张图: {note_path.name}')
    return replaced > 0


# ═══════════════ 任务处理 ═══════════════

def process_job(job: dict) -> bool:
    """处理单个配图任务：确保服务器→生成→拉回→插入→通知"""
    log.info(f'▶ 处理任务: {job["note_title"]} ({len(job["images"])}张)')
    job_id = job['job_id']

    # 1. 检查服务器（down → 通知，但先尝试自动拉起）
    if not server_reachable():
        log.warning('🔴 FLUX 服务器不可达，通知 user 开机')
        notify_owner(f'🔴 FLUX 文生图服务器不可达\n'
                     f'有配图任务待处理: {job["note_title"]} ({len(job["images"])}张)\n'
                     f'请到 AutoDL 控制台开机（带卡模式）。')
        return False  # 任务保留队列，恢复后重试

    gpu_ok, ginfo = gpu_ready()
    if not gpu_ok:
        log.warning('⚠️  服务器无卡，通知 user 切带卡')
        notify_owner(f'⚠️ FLUX 服务器已开机但处于【无卡模式】\n'
                     f'任务: {job["note_title"]}\n请到 AutoDL 控制台切到【带卡模式】。')
        return False

    if not model_ready():
        log.error('❌ 模型未就绪')
        return False

    # 2. 上传 + 启动生成（幂等）
    upload_prompts(job)
    if not gen_running():
        start_generation()
    time.sleep(10)

    # 3. 等待完成
    if not wait_generation(len(job['images'])):
        return False

    # 4. 拉回 + 插入
    pull_images(job)
    insert_into_note(job)

    # 5. 通知完成
    notify_owner(f'✅ FLUX 配图完成: {job["note_title"]} ({len(job["images"])}张)\n'
                 f'已插入稿子，可在 Obsidian 查看。')
    log.info(f'✅ 任务完成: {job_id}')
    return True


def list_pending_jobs() -> list:
    """列出队列中待处理任务（排除 _done）"""
    jobs = []
    if not JOB_DIR.exists():
        return jobs
    for f in sorted(JOB_DIR.glob('*.json')):
        try:
            jobs.append(json.loads(f.read_text(encoding='utf-8')))
        except Exception as e:
            log.warning(f'跳过坏任务文件 {f.name}: {e}')
    return jobs


def mark_done(job_id: str):
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    src = JOB_DIR / f'{job_id}.json'
    if src.exists():
        shutil.move(str(src), str(DONE_DIR / src.name))
        log.info(f'🗂️  任务归档: {job_id}')


# ═══════════════ 健康监控（对标转录bot） ═══════════════

def health_check_pending(pending: list) -> bool:
    """有任务时检查服务器，down → 通知（节流），可达 → 通知恢复。返回是否服务器就绪"""
    if not pending:
        return True
    if server_reachable():
        return True
    log.warning('🔴 FLUX 服务器不可达且有任务，通知开机')
    notify_owner(f'🔴 FLUX 文生图服务器不可达，有 {len(pending)} 个配图任务排队。\n'
                 f'请到 AutoDL 控制台开机（带卡模式）。')
    return False


# ═══════════════ 主循环 ═══════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='处理一次队列即退')
    ap.add_argument('--interval', type=int, default=120, help='检测间隔秒(默认120)')
    args = ap.parse_args()

    log.info('🚀 FLUX 服务器管理器启动')
    log.info(f'   任务队列: {JOB_DIR}')
    log.info(f'   检测间隔: {args.interval}s')

    while True:
        try:
            pending = list_pending_jobs()
            if pending:
                log.info(f'📋 队列有 {len(pending)} 个任务')
                # 先确保服务器就绪（不足则通知）
                if health_check_pending(pending):
                    for job in pending:
                        if process_job(job):
                            mark_done(job['job_id'])
                        # 服务器若中途 down，停止本轮后续任务
                        if not server_reachable():
                            log.warning('服务器中途不可达，暂停后续任务')
                            break
            if args.once:
                log.info('--once 模式，退出')
                return
        except Exception as e:
            log.error(f'主循环异常: {e}')
        time.sleep(args.interval)


if __name__ == '__main__':
    main()