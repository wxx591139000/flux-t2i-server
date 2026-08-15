#!/usr/bin/env python3
"""
用飞书 WebSocket 长连接(lark_oapi)监听机器人收到的消息，提取发送者 open_id。
（同转录bot feishu_channel 的 WS 事件接收范式，可靠且不依赖会话列表权限）

用法:
  1. 运行: python manager/listen_ws.py
  2. 给图图机器人发一条消息（如"你好"）
  3. 打开印到 stdout，同时写入 manager/_owner_openid.txt
把 open_id 填到 manager/.env 的 FEISHU_OWNER_OPEN_ID=
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from manager.feishu_notify import FeishuNotifier

OUT = Path(__file__).parent / '_owner_openid.txt'
n = FeishuNotifier()

def log(msg):
    print(msg, flush=True)
    with open(str(OUT), 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

def on_message(data):
    try:
        ev = data.event
        sender = ev.sender
        if not sender or sender.sender_type == 'app':
            return
        oid = sender.sender_id.open_id if sender.sender_id else ''
        if oid:
            content = ev.message.content if ev.message else ''
            log(f'\n✅ 收到用户消息! 发送者 open_id: {oid}')
            log(f'   sender_type: {sender.sender_type}')
            log(f'   消息内容: {content[:120]}')
            log(f'\n填到 manager/.env 的 FEISHU_OWNER_OPEN_ID={oid}')
            time.sleep(1)
            sys.exit(0)
    except Exception as e:
        log(f'处理异常: {e}')

def main():
    import lark_oapi as lark
    handler = lark.EventDispatcherHandler.builder('', '') \
        .register_p2_im_message_receive_v1(on_message).build()
    client = lark.ws.Client(n.app_id, n.app_secret, event_handler=handler,
                            log_level=lark.LogLevel.WARNING)
    log('🔌 开始监听（请现在给图图机器人发一条消息，如"你好"）...')
    client.start()  # 阻塞，收到消息后 on_message 里 sys.exit(0)

if __name__ == '__main__':
    main()