#!/usr/bin/env python3
"""
FLUX 对外文生图服务 — Web 服务（对标转录bot web_upload/server.py，stdlib http.server 无框架依赖）
- 网页: 提交提示词・任务列表・看图/下载
- API: /api/submit /api/status /api/download /api/activate /api/admin/gen_codes
- 认证: api token(浏览器 cookie 或 ?token=) → 用户身份 → 配额校验
"""
import os
import sys
import json
import uuid
import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager.feishu_notify import notify_owner, _load_env

_load_env()  # 加载 manager/.env（WEB_ADMIN_TOKEN 等）

logger = logging.getLogger('manager.flux_web_service')

WEB_ADMIN_TOKEN = os.environ.get('WEB_ADMIN_TOKEN', '')
SERVICE_NAME = 'FLUX 文生图'
OWNER_UNLIMITED_HINT = 'owner'


class FluxWebServer:
    def __init__(self, db, quota, scheduler, port=9620):
        self.db = db
        self.quota = quota
        self.scheduler = scheduler
        self.port = port
        self._server = None

    def start(self):
        handler = _Handler
        handler.app = self
        self._server = ThreadingHTTPServer(('0.0.0.0', self.port), handler)
        th = threading.Thread(target=self._server.serve_forever, daemon=True, name='flux-web')
        th.start()
        logger.info(f'🌐 Web 服务已启动: http://localhost:{self.port}')

    def stop(self):
        if self._server:
            self._server.shutdown()


class _Handler(BaseHTTPRequestHandler):
    app = None  # FluxWebServer

    @property
    def db(self):
        return self.app.db

    @property
    def quota(self):
        return self.app.quota

    @property
    def scheduler(self):
        return self.app.scheduler

    # ── 工具 ──
    def _send(self, code, body: str, ctype='text/html; charset=utf-8'):
        data = body.encode('utf-8')
        self.send_response(code)
        if getattr(self, '_cookie', None):
            self.send_header('Set-Cookie', self._cookie)
            self._cookie = None
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8')

    def _read_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            return ''
        raw = self.rfile.read(length)
        # 浏览器用 UTF-8，Windows curl 可能是 GBK；兜底 replace 避免崩溃
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return raw.decode('utf-8', errors='replace')

    def _get_token(self, q) -> str:
        """从 cookie / query 取 api token，无则生成。"""
        t = q.get('token', [''])[0] or ''
        raw = self.headers.get('Cookie', '')
        for part in raw.split(';'):
            part = part.strip()
            if part.startswith('flux_token='):
                t = part[len('flux_token='):]
        if not t:
            t = uuid.uuid4().hex[:16]
        return t

    def _set_token_cookie(self, token):
        self._cookie = f'flux_token={token}; Path=/; Max-Age=31536000'

    def _resolve_user(self, token):
        """按 token 找到/创建用户。owner 用预留 api key。"""
        u = self.db.get_user(token)
        if not u:
            u = self.db.create_user(token)
        return u

    # ── 路由 ──
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        token = self._get_token(q)
        self._set_token_cookie(token)

        try:
            if path == '/':
                self._page_index(token)
            elif path == '/center':
                self._page_center(token)
            elif path == '/health':
                self._json({'status': 'ok'})
            elif path == '/api/status':
                self._api_status(q)
            elif path.startswith('/api/download/'):
                self._api_download(path, token)
            else:
                self._send(404, 'Not Found')
        except Exception as e:
            logger.error(f'GET {path} 异常: {e}')
            self._json({'error': str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        token = self._get_token(q)
        self._set_token_cookie(token)
        body = self._read_body()

        try:
            if path == '/api/submit':
                self._api_submit(token, body)
            elif path == '/api/activate':
                self._api_activate(body)
            elif path == '/api/admin/gen_codes':
                self._api_gen_codes(body)
            else:
                self._send(404, 'Not Found')
        except Exception as e:
            logger.error(f'POST {path} 异常: {e}')
            self._json({'error': str(e)}, 500)

    # ── API ──
    def _api_submit(self, token, body):
        data = json.loads(body or '{}')
        prompt = data.get('prompt', '')
        priority = int(data.get('priority', 0) or 0)
        self._resolve_user(token)
        result = self.scheduler.submit(token, prompt, priority)
        self._json(result)

    def _api_status(self, q):
        job_id = q.get('job_id', [''])[0]
        job = self.db.job_get(job_id)
        if not job:
            self._json({'error': '任务不存在'}, 404)
            return
        self._json({
            'job_id': job['job_id'], 'status': job['status'],
            'prompt': job['prompt'], 'error': job['error'],
            'image_path': job['image_path'],
            'created_at': job['created_at'], 'completed_at': job['completed_at'],
        })

    def _api_download(self, path, token):
        job_id = path.rsplit('/', 1)[-1]
        job = self.db.job_get(job_id)
        if not job or job['status'] != 'done' or not job['image_path']:
            self._send(404, '图片不存在或未完成')
            return
        if job['user_id'] != token:
            self._send(403, '无权访问')
            return
        img = Path(job['image_path'])
        if not img.exists():
            self._send(404, '图片文件缺失')
            return
        data = img.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Disposition', f'inline; filename="{job_id}.png"')
        self.end_headers()
        self.wfile.write(data)

    def _api_activate(self, body):
        data = json.loads(body or '{}')
        code = (data.get('code') or '').strip().upper()
        token = data.get('token', '')
        if not code or not token:
            self._json({'error': '缺少激活码或 token'})
            return
        self._resolve_user(token)
        if self.db.code_activate(code, token):
            plan = self.db.get_user(token)['plan']
            self._json({'ok': True, 'plan': plan, 'msg': f'激活成功，套餐: {plan}'})
        else:
            self._json({'error': '激活码无效或已被使用'})

    def _api_gen_codes(self, body):
        data = json.loads(body or '{}')
        admin = data.get('admin_token', '')
        if not WEB_ADMIN_TOKEN or admin != WEB_ADMIN_TOKEN:
            self._json({'error': '无权限'}, 403)
            return
        plan = data.get('plan', 'basic')
        n = int(data.get('n', 1) or 1)
        codes = self.db.code_generate(plan, n)
        self._json({'ok': True, 'codes': codes, 'plan': plan})

    # ── 页面 ──
    def _page_index(self, token):
        u = self._resolve_user(token)
        jobs = self.db.job_by_user(token, 20)
        page = _HTML_INDEX.format(
            service=SERVICE_NAME,
            token=token,
            plan=u['plan'],
            usage=self.quota.usage_summary(token),
            jobs=_render_jobs(jobs, token),
        )
        self._send(200, page)

    def _page_center(self, token):
        u = self._resolve_user(token)
        jobs = self.db.job_by_user(token, 50)
        page = _HTML_CENTER.format(
            service=SERVICE_NAME,
            token=token,
            plan=u['plan'],
            usage=self.quota.usage_summary(token),
            jobs=_render_jobs(jobs, token),
        )
        self._send(200, page)


def _render_jobs(jobs, token=''):
    rows = []
    for j in jobs:
        status = j['status']
        badge = {'queued': '排队中', 'generating': '生成中', 'waiting': '等待服务恢复', 'done': '已完成', 'failed': '失败'}.get(status, status)
        if status == 'done' and j['image_path']:
            dl = f'/api/download/{j["job_id"]}?token={token}'
            img = (f'<img src="{dl}" style="max-height:120px;border-radius:8px;display:block">'
                   f'<a href="{dl}" download style="display:inline-block;margin-top:6px;'
                   f'background:#16a34a;color:#fff;padding:4px 12px;border-radius:6px;'
                   f'text-decoration:none;font-size:13px">⬇ 下载</a>')
        else:
            img = '—'
        err = html.escape((j['error'] or '')[:60])
        # 展示原始中文提示词（若有），英文为次要信息（sqlite3.Row 用索引访问，无 .get()）
        orig = j['original_prompt'] or ''
        prompt = j['prompt'] or ''
        if orig and orig != prompt:
            full = f'{html.escape(str(orig)[:30])}<br><small style="color:#888">{html.escape(str(prompt)[:40])}</small>'
        else:
            full = html.escape(str(prompt))[:60]
        rows.append(f'<tr><td>{j["job_id"]}</td><td>{status}</td>'
                    f'<td>{full}</td>'
                    f'<td>{img}</td><td>{err}</td>'
                    f'<td>{j["created_at"]}</td></tr>')
    if not rows:
        return '<tr><td colspan="6" style="text-align:center;color:#888">暂无任务</td></tr>'
    return ''.join(rows)


_HTML_INDEX = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{service}</title><style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f7f8fa;color:#222}}
.card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 12px}}
.meta{{color:#888;font-size:13px;margin-bottom:12px}}
textarea{{width:100%;height:90px;border:1px solid #ddd;border-radius:8px;padding:10px;font-size:14px;box-sizing:border-box}}
button{{background:#4a76f7;color:#fff;border:none;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer}}
button:hover{{background:#3a63d6}} input{{border:1px solid #ddd;border-radius:8px;padding:8px;font-size:14px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:8px;border-bottom:1px solid #eee;text-align:left}}
.status{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}}
.msg{{margin-top:10px;font-size:13px}} .ok{{color:#16a34a}} .err{{color:#dc2626}}
</style></head><body>
<h1>{service}</h1>
<div class="card meta">你的Token: <code>{token}</code> · 套餐: <b>{plan}</b> · 用量: {usage}<br>
<small>Token 已存入浏览器 Cookie，作为你的身份标识</small></div>

<div class="card"><h2>生成图片</h2>
<textarea id="p" placeholder="用英文描述你想生成的图片，例如: a cute shiba inu running on a beach, cinematic lighting, photorealistic"></textarea>
<button onclick="submit()">提交生成</button>
<div id="msg" class="msg"></div></div>

<div class="card"><h2>兑换激活码</h2>
<input id="code" placeholder="粘贴激活码"><button onclick="activate()">激活套餐</button>
<div id="amsg" class="msg"></div></div>

<div class="card"><h2>我的任务</h2><table>
<tr><th>任务ID</th><th>状态</th><th>提示词</th><th>图片</th><th>错误</th><th>时间</th></tr>
{jobs}</table></div>

<script>
function submit(){{
  var p=document.getElementById('p').value.trim();
  if(!p){{return msg('请输入提示词','err')}}
  fetch('/api/submit',{{method:'POST',body:JSON.stringify({{prompt:p}})}})
    .then(r=>r.json()).then(d=>{{
      if(d.job_id){{msg('已入队，任务ID: '+d.job_id,'ok');setTimeout(()=>location.reload(),1500)}}
      else msg(d.error||'提交失败','err')
    }}).catch(e=>msg(e,'err'))
}}
function activate(){{
  var c=document.getElementById('code').value.trim();
  fetch('/api/activate',{{method:'POST',body:JSON.stringify({{code:c}})}})
    .then(r=>r.json()).then(d=>{{msg(d.ok?d.msg:(d.error||''),d.ok?'ok':'err');if(d.ok)setTimeout(()=>location.reload(),1500)}})
}}
function msg(t,cls){{var m=document.getElementById('msg');m.innerHTML='<span class="'+cls+'">'+t+'</span>'}}
</script></body></html>"""

_HTML_CENTER = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{service} · 我的任务</title><style>
body{{font-family:system-ui,sans-serif;max-width:1000px;margin:0 auto;padding:20px;background:#f7f8fa;color:#222}}
.card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:8px;border-bottom:1px solid #eee;text-align:left}}
img{{border-radius:8px}} a{{color:#4a76f7}}
</style></head><body>
<div class="card"><h1>{service} · 我的任务</h1>
<div class="meta">Token: <code>{token}</code> · 套餐: <b>{plan}</b> · 用量: {usage}</div>
<a href="/">← 返回生成页</a></div>
<div class="card"><table>
<tr><th>任务ID</th><th>状态</th><th>提示词</th><th>图片</th><th>错误</th><th>时间</th></tr>
{jobs}</table></div></body></html>"""


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('WEB_PORT', '9620')))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    from manager.flux_db import FluxDB
    from manager.flux_quota import QuotaService
    db = FluxDB()
    quota = QuotaService(db)
    srv = FluxWebServer(db, quota, None, args.port)
    srv.start()
    try:
        import time
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == '__main__':
    main()