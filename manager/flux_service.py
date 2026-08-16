#!/usr/bin/env python3
"""
FLUX 对外文生图服务 — main 入口（对标转录bot main.py 装配各组件）
wiring: DB → quota → queue → web → 各自线程启动 + 健康监控

用法:
  python manager/flux_service.py                # 常驻 daemon
  python manager/flux_service.py --port 9620    # 指定端口
"""
import os
import sys
import time
import logging
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager.flux_db import FluxDB
from manager.flux_quota import QuotaService
from manager.flux_queue import FluxQueueScheduler
from manager.flux_web_service import FluxWebServer
from manager.feishu_bot import FeishuBot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(BASE_DIR / 'manager' / 'flux_service.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)])
log = logging.getLogger('manager.flux_service')


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('WEB_PORT', '9620')))
    ap.add_argument('--once', action='store_true', help='启动后处理完当前队列即退（测试）')
    args = ap.parse_args()

    log.info('🚀 FLUX 对外文生图服务启动')
    db = FluxDB()
    quota = QuotaService(db)
    scheduler = FluxQueueScheduler(db, quota)
    web = FluxWebServer(db, quota, scheduler, args.port)

    scheduler.start()
    web.start()
    bot = FeishuBot(scheduler, db, quota)
    bot.start()

    log.info(f'   Web: http://localhost:{args.port}')
    log.info(f'   队列调度器: 单 worker 串行')
    log.info(f'   飞书图图机器人: 对话式出图')

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info('⏹️  停止服务')
        scheduler.stop()
        web.stop()


if __name__ == '__main__':
    main()