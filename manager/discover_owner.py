#!/usr/bin/env python3
"""
发现 owner 的 open_id（用于飞书私信通知）
用法: 先给新机器人发一条消息（如"你好"），然后运行本脚本
  python manager/discover_owner.py
脚本列出机器人的会话，从最近消息提取发送者 open_id。
"""
import sys
import json
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from manager.feishu_notify import FeishuNotifier

logging.basicConfig(level=logging.INFO)
API = 'https://open.feishu.cn'

n = FeishuNotifier()
try:
    tok = n.get_tenant_token()
except Exception as e:
    print(f'❌ token 获取失败: {e}'); sys.exit(1)

headers = {'Authorization': f'Bearer {tok}'}
import requests

# 1. 列出机器人所在的会话
r = requests.get(f'{API}/open-apis/im/v1/chats?page_size=50', headers=headers, timeout=10)
data = r.json()
if data.get('code') != 0:
    print(f'❌ 获取会话失败: {data}')
    sys.exit(1)

chats = data.get('items', [])
print(f'📋 机器人有 {len(chats)} 个会话:\n')
found = False
for c in chats:
    chat_id = c.get('chat_id')
    name = c.get('name') or c.get('chat_mode') or chat_id
    print(f'  - {name} ({chat_id})')
    # 查最近消息，找发送者 open_id
    mr = requests.get(f'{API}/open-apis/im/v1/messages?container_id_type=chat&container_id={chat_id}&page_size=1&sort_type=ByCreateTimeDesc',
                      headers=headers, timeout=10)
    md = mr.json()
    if md.get('code') == 0 and md.get('items'):
        for it in md['items']:
            sender = it.get('sender', {})
            sender_id = sender.get('id', '')
            sender_type = sender.get('sender_type', '')
            if sender_id and sender_type == 'user':
                print(f'      → 最近发送者 open_id: {sender_id}')
                found = True

if not found:
    print('\n⚠️  未找到用户消息。请先给机器人发一条消息（如"你好"），再运行本脚本。')

if found:
    print('\n✅ 把上面的 open_id 填到 manager/.env 的 FEISHU_OWNER_OPEN_ID=')