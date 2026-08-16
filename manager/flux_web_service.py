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
from manager.flux_quota import current_ym

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

    def _admin_ok(self, body_token: str = '') -> bool:
        """校验管理员：X-Admin-Token 或 Authorization: Bearer == WEB_ADMIN_TOKEN（借鉴转录 admin）。"""
        if not WEB_ADMIN_TOKEN:
            return False
        t = (self.headers.get('X-Admin-Token', '') or
             self.headers.get('Authorization', '').replace('Bearer ', '').strip() or
             body_token)
        return t == WEB_ADMIN_TOKEN

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
            elif path == '/admin':
                self._page_admin()
            elif path == '/health':
                self._json({'status': 'ok'})
            elif path == '/api/status':
                self._api_status(q)
            elif path == '/api/my':
                self._api_my(token)
            elif path == '/api/admin/users':
                self._api_admin_users()
            elif path == '/api/admin/codes':
                self._api_admin_codes()
            elif path == '/api/admin/jobs':
                self._api_admin_jobs()
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
            elif path == '/api/bind':
                self._api_bind(body)
            elif path == '/api/admin/gen_codes':
                self._api_gen_codes(body)
            elif path == '/api/admin/set_remark':
                self._api_admin_set_remark(body)
            elif path == '/api/admin/set_plan':
                self._api_admin_set_plan(body)
            elif path == '/api/admin/set_owner':
                self._api_admin_set_owner(body)
            elif path == '/api/admin/search':
                self._api_admin_search(body)
            elif path == '/api/admin/account_detail':
                self._api_admin_account_detail(body)
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
        existed = self.db.account_get(code) is not None
        if self.db.code_activate(code, token):
            acct = self.db.account_get(code)
            plan = acct['plan'] if acct else self.db.get_user(token)['plan']
            msg = (f'已绑定到已有账户，共享套餐: {plan}' if existed
                   else f'激活成功，套餐: {plan}')
            self._json({'ok': True, 'plan': plan, 'account_id': code, 'msg': msg})
        else:
            self._json({'error': '激活码无效、已过期或不可用'})

    def _api_bind(self, body):
        """换设备：新 token 并入已激活码的账户（共享套餐/用量）。"""
        data = json.loads(body or '{}')
        code = (data.get('code') or '').strip().upper()
        token = data.get('token', '')
        if not code or not token:
            self._json({'error': '缺少激活码或 token'})
            return
        self._resolve_user(token)
        if self.db.code_bind(code, token):
            acct = self.db.account_get(code)
            self._json({'ok': True, 'plan': acct['plan'], 'account_id': code,
                        'msg': f'已绑定账户({code})，共享套餐: {acct["plan"]}'})
        else:
            self._json({'error': '激活码无效或尚未激活'})

    def _api_my(self, token):
        """当前 token 的账户/套餐/用量状态（客户页用）。"""
        u = self._resolve_user(token)
        acct_id = self.db.account_id_of(token)
        acct = self.db.account_get(acct_id) if acct_id else None
        eff = self.quota.effective(token)
        self._json({'token': token, 'account_id': acct_id or '',
                    'plan': eff['plan'], 'is_owner': eff['is_owner'],
                    'remark': acct['remark'] if acct else '',
                    'bound': bool(acct_id),
                    'usage': self.quota.usage_summary(token)})

    def _api_gen_codes(self, body):
        data = json.loads(body or '{}')
        if not self._admin_ok(data.get('admin_token', '')):
            self._json({'error': '无权限'}, 403)
            return
        plan = data.get('plan', 'basic')
        n = int(data.get('n', 1) or 1)
        days = int(data.get('days', 0) or 0)
        remark = data.get('remark', '')
        if plan not in self.quota.plans:
            self._json({'error': f'未知套餐: {plan}'})
            return
        if not (1 <= n <= 100):
            self._json({'error': '数量需在 1-100'})
            return
        if not (0 <= days <= 365):
            self._json({'error': '天数需在 0-365（0=不过期）'})
            return
        codes = self.db.code_generate(plan, n, days, remark)
        self._json({'ok': True, 'codes': codes, 'plan': plan})

    # ── 商户中心 admin API ──
    def _api_admin_users(self):
        """商户中心主表：客户/账户维度（激活码=账户）。"""
        if not self._admin_ok():
            self._json({'error': '无权限'}, 403)
            return
        rows = []
        for a in self.db.list_accounts():
            limit = self.quota.plans.get(a['plan'], {}).get('monthly_images', -1)
            rows.append({'account_id': a['account_id'], 'code': a['code'], 'plan': a['plan'],
                         'is_owner': a['is_owner'], 'remark': a['remark'] or '',
                         'used': a['used'], 'limit': limit, 'token_count': a['token_count'],
                         'created_at': a['activated_at'] or a['created_at']})
        self._json({'accounts': rows})

    def _api_admin_account_detail(self, body):
        """某账户详情：客户信息 + 该账户下所有 token + 各自任务数。"""
        data = json.loads(body or '{}')
        if not self._admin_ok(data.get('admin_token', '')):
            self._json({'error': '无权限'}, 403)
            return
        acct_id = data.get('account_id', '')
        acct = self.db.account_get(acct_id)
        if not acct:
            self._json({'error': '账户不存在'})
            return
        tokens = []
        for t in self.db.account_tokens(acct_id):
            u = self.db.get_user(t['user_id'])
            tokens.append({'token': t['user_id'], 'plan': u['plan'] if u else '',
                           'used': self.db.usage_get(t['user_id'], current_ym()),
                           'created_at': u['created_at'] if u else None})
        self._json({'account': {'account_id': acct['account_id'], 'plan': acct['plan'],
                                'is_owner': acct['is_owner'], 'remark': acct['remark'] or '',
                                'used': self.db.account_usage(acct_id, current_ym())},
                    'tokens': tokens})

    def _api_admin_codes(self):
        if not self._admin_ok():
            self._json({'error': '无权限'}, 403)
            return
        rows = []
        for c in self.db.list_codes():
            rows.append({'code': c['code'], 'plan': c['plan'], 'status': c['status'],
                         'remark': c['remark'] or '', 'expires_at': c['expires_at'],
                         'used_by': c['used_by'], 'used_at': c['used_at'],
                         'created_at': c['created_at']})
        self._json({'codes': rows})

    def _api_admin_jobs(self):
        if not self._admin_ok():
            self._json({'error': '无权限'}, 403)
            return
        rows = []
        for j in self.db.list_jobs(50):
            rows.append({'job_id': j['job_id'], 'user_id': j['user_id'], 'prompt': j['prompt'],
                         'status': j['status'], 'error': j['error'],
                         'created_at': j['created_at'], 'completed_at': j['completed_at']})
        self._json({'jobs': rows})

    def _api_admin_set_remark(self, body):
        data = json.loads(body or '{}')
        if not self._admin_ok(data.get('admin_token', '')):
            self._json({'error': '无权限'}, 403)
            return
        code = (data.get('code') or '').strip()
        self.db.code_set_remark(code, data.get('remark', ''))
        self._json({'ok': True})

    def _api_admin_set_plan(self, body):
        """改套餐：作用于账户（同步 codes 表）。兼容传 account_id 或 user_id。"""
        data = json.loads(body or '{}')
        if not self._admin_ok(data.get('admin_token', '')):
            self._json({'error': '无权限'}, 403)
            return
        plan = data.get('plan', '')
        if plan not in self.quota.plans:
            self._json({'error': f'未知套餐: {plan}'})
            return
        acct_id = data.get('account_id', '') or data.get('user_id', '')
        if not acct_id:
            self._json({'error': '缺少 account_id'})
            return
        if self.db.account_get(acct_id):
            self.db.account_set_plan(acct_id, plan)
        else:
            self._resolve_user(acct_id)
            self.db.set_plan(acct_id, plan)
        self._json({'ok': True, 'plan': plan})

    def _api_admin_set_owner(self, body):
        """切换 owner：作用于账户。"""
        data = json.loads(body or '{}')
        if not self._admin_ok(data.get('admin_token', '')):
            self._json({'error': '无权限'}, 403)
            return
        is_owner = 1 if data.get('is_owner') else 0
        acct_id = data.get('account_id', '') or data.get('user_id', '')
        if not acct_id:
            self._json({'error': '缺少 account_id'})
            return
        if self.db.account_get(acct_id):
            self.db.account_set_owner(acct_id, is_owner)
        else:
            self._resolve_user(acct_id)
            self.db.set_is_owner(acct_id, is_owner)
        self._json({'ok': True, 'is_owner': is_owner})

    def _api_admin_search(self, body):
        data = json.loads(body or '{}')
        if not self._admin_ok(data.get('admin_token', '')):
            self._json({'error': '无权限'}, 403)
            return
        kw = (data.get('keyword') or '').strip()
        if not kw:
            self._json({'accounts': []})
            return
        # 按账户 id(激活码) / 客户名(remark) 搜
        accts = list(self.db._all(
            'SELECT * FROM accounts WHERE account_id LIKE ? OR remark LIKE ? '
            'COLLATE NOCASE ORDER BY COALESCE(activated_at,created_at) DESC LIMIT 50',
            (f'%{kw}%', f'%{kw}%')))
        # 按 token 搜 → 映射到其账户
        seen = {a['account_id'] for a in accts}
        for u in self.db.search_users(kw):
            if u['account_id'] and u['account_id'] not in seen:
                a = self.db.account_get(u['account_id'])
                if a:
                    accts.append(a)
                    seen.add(a['account_id'])
        self._json({'accounts': [{'account_id': a['account_id']} for a in accts]})

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

    def _page_admin(self):
        """商户管理中心（借鉴转录项目 admin.html）：管理套餐/token/激活码。"""
        self._send(200, _HTML_ADMIN.format(service=SERVICE_NAME))


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
<span id="acct" style="color:#4a76f7"></span>
<small>Token 已存入浏览器 Cookie，作为你的身份标识</small>
<small style="float:right"><a href="/admin" style="color:#999">商户管理</a></small></div>

<div class="card"><h2>生成图片</h2>
<textarea id="p" placeholder="用英文描述你想生成的图片，例如: a cute shiba inu running on a beach, cinematic lighting, photorealistic"></textarea>
<button onclick="submit()">提交生成</button>
<div id="msg" class="msg"></div></div>

<div class="card"><h2>激活 / 绑定账户</h2>
<input id="code" placeholder="粘贴激活码" onkeydown="if(event.key==='Enter')activate()"><button onclick="activate()">激活 / 绑定</button>
<div id="amsg" class="msg"></div>
<small style="color:#888">新客户输入激活码创建账户；换设备时输入已激活的码可并入同一账户，共享套餐与用量</small></div>

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
  if(!c){{return}}
  fetch('/api/activate',{{method:'POST',body:JSON.stringify({{code:c}})}})
    .then(r=>r.json()).then(d=>{{amsg(d.ok?d.msg:(d.error||''),d.ok?'ok':'err');if(d.ok)setTimeout(()=>location.reload(),1500)}})
}}
function amsg(t,cls){{var m=document.getElementById('amsg');m.innerHTML='<span class="'+cls+'">'+t+'</span>'}}
function msg(t,cls){{var m=document.getElementById('msg');m.innerHTML='<span class="'+cls+'">'+t+'</span>'}}
fetch('/api/my').then(r=>r.json()).then(d=>{{
  var el=document.getElementById('acct');
  if(d.bound){{el.innerHTML='账户: <code>'+d.account_id+'</code> · 共享套餐: <b>'+d.plan+'</b> · '+d.usage}}else{{el.innerHTML='未绑定账户，可用独立套餐'}}
}}).catch(()=>{{}})
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

_HTML_ADMIN = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{service} · 商户中心</title><style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f7f8fa;color:#222}}
.card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 12px}}
.meta{{color:#888;font-size:13px;margin-bottom:12px}}
input,select{{border:1px solid #ddd;border-radius:8px;padding:8px;font-size:14px;margin-right:8px}}
button{{background:#4a76f7;color:#fff;border:none;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer}}
button:hover{{background:#3a63d6}} button.mini{{padding:3px 8px;font-size:12px;background:#e5e7eb;color:#333}}
button.mini:hover{{background:#d1d5db}} button.danger{{background:#dc2626}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:7px;border-bottom:1px solid #eee;text-align:left}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}}
.b-owner{{background:#fef3c7;color:#92400e}} .b-pro{{background:#dbeafe;color:#1e40af}}
.b-basic{{background:#dcfce7;color:#166534}} .b-default{{background:#f3f4f6;color:#4b5563}}
.b-used{{background:#fce7f3;color:#9d174d}} .b-unused{{background:#dcfce7;color:#166534}} .b-expired{{background:#fee2e2;color:#991b1b}}
.msg{{margin-top:8px;font-size:13px}} .ok{{color:#16a34a}} .err{{color:#dc2626}}
.gate{{text-align:center;padding:60px 0}} .gate input{{width:280px}}
.hl{{background:#fef08a!important}}
.small{{color:#888;font-size:12px}} .ta{{vertical-align:top}}
</style></head><body>
<div class="card"><h1>{service} · 商户中心</h1>
<div class="meta">管理用户套餐 / Token / 激活码 · <a href="/">← 生成页</a>
<button class="mini" onclick="logout()" style="float:right">退出</button></div></div>

<div id="gate" class="card gate">
<h2>管理员登录</h2>
<input id="ptk" type="password" placeholder="管理员 Token" onkeydown="if(event.key==='Enter')login()">
<button onclick="login()">登录</button>
<div id="gmsg" class="msg"></div>
</div>

<div id="panel" style="display:none">
  <div class="card"><h2>生成激活码</h2>
  数量 <input id="gn" type="number" value="1" min="1" max="100" style="width:60px">
  天数 <input id="gd" type="number" value="30" min="0" max="365" style="width:70px" title="0=不过期">
  套餐 <select id="gp"><option value="basic">basic(50)</option><option value="pro">pro(200)</option><option value="default">default(10)</option></select>
  备注 <input id="gr" placeholder="客户/用途" style="width:180px">
  <button onclick="genCodes()">生成</button> <span id="gmsg2" class="msg"></span>
  </div>

  <div class="card"><h2>客户 / 账户管理 <span class="small">（激活码=账户，一客户一账户）</span></h2>
  <input id="sk" placeholder="按激活码 / 客户名(token) 搜索..." style="width:260px" onkeydown="if(event.key==='Enter')searchUser()">
  <button class="mini" onclick="searchUser()">搜索</button>
  <table><thead><tr><th>账户(激活码)</th><th>客户名</th><th>套餐</th><th>本月用量</th><th>设备数</th><th>创建</th><th>操作</th></tr></thead><tbody id="atb"></tbody></table>
  <div id="adetail" style="margin-top:10px"></div>
  </div>

  <div class="card"><h2>激活码</h2>
  <table><thead><tr><th>激活码</th><th>套餐</th><th>状态</th><th>备注</th><th>到期</th><th>使用人</th><th>操作</th></tr></thead><tbody id="ctb"></tbody></table>
  </div>

  <div class="card"><h2>最近任务</h2>
  <table><thead><tr><th>任务ID</th><th>用户</th><th>状态</th><th>提示词</th><th>时间</th></tr></thead><tbody id="jtb"></tbody></table>
  </div>
</div>

<script>
const K='flux_admin_token', PLANS={{'basic':'basic(50)','pro':'pro(200)','default':'default(10)'}};
let tk=localStorage.getItem(K)||'';
function esc(s){{return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]))}}
function msg(el,t,c){{document.getElementById(el).innerHTML='<span class="'+c+'">'+esc(t)+'</span>'}}
async function api(p,o){{o=o||{{}};o.headers=Object.assign({{'Content-Type':'application/json'}},o.headers||{{}});
  if(tk)o.headers['X-Admin-Token']=tk;
  const r=await fetch(p,o);const d=await r.json().catch(()=>({{}}));
  if(r.status===403){{showGate();throw new Error('无权限')}} return d;}}
function showGate(){{document.getElementById('gate').style.display='';document.getElementById('panel').style.display='none'}}
function showPanel(){{document.getElementById('gate').style.display='none';document.getElementById('panel').style.display=''}}
async function login(){{tk=document.getElementById('ptk').value.trim();
  const d=await api('/api/admin/users'); if(d.users){{localStorage.setItem(K,tk);showPanel();loadAll()}}
  else msg('gmsg','Token 错误','err')}}
function logout(){{localStorage.removeItem(K);tk='';showGate();document.getElementById('ptk').value=''}}
function escNice(ts){{if(!ts)return '—';const t=new Date(ts*1000);return isNaN(t)?'—':t.toLocaleString('zh-CN',{{hour12:false}})}}
function limitTxt(plan){{const l=PLANS[plan];return l?l:'—'}}

async function loadUsers(){{const d=await api('/api/admin/users');const tb=document.getElementById('atb');tb.innerHTML='';
  (d.accounts||[]).forEach(a=>{{const tr=document.createElement('tr');tr.dataset.id=a.account_id;tr.dataset.rem=a.remark||'';
    tr.innerHTML='<td><code>'+esc(a.account_id)+'</code></td>'+
    '<td>'+esc(a.remark||'—')+' <button class="mini" onclick="setRemark(this.parentNode.parentNode.dataset.id,this.parentNode.parentNode.dataset.rem)">改</button></td>'+
    '<td>'+(a.is_owner?'<span class="badge b-owner">owner无限</span>':'<span class="badge b-'+esc(a.plan)+'">'+esc(a.plan)+'</span>')+'</td>'+
    '<td>'+a.used+'/'+(a.limit<0?'∞':a.limit)+'</td>'+
    '<td>'+a.token_count+'</td>'+
    '<td class="small">'+escNice(a.created_at)+'</td>'+
    '<td><select onchange="setPlan(\\''+esc(a.account_id)+'\\',this.value)">'+
      Object.keys(PLANS).map(p=>'<option value="'+p+'"'+(p===a.plan&&!a.is_owner?' selected':'')+'>'+PLANS[p]+'</option>').join('')+'</select>'+
    ' <button class="mini" onclick="setOwner(\\''+esc(a.account_id)+'\\','+(a.is_owner?1:0)+')">'+(a.is_owner?'取消owner':'设owner')+'</button>'+
    ' <button class="mini" onclick="accountDetail(\\''+esc(a.account_id)+'\\')">详情</button></td>';
    tb.appendChild(tr);}})}}

async function setPlan(t,p){{const d=await api('/api/admin/set_plan',{{method:'POST',body:JSON.stringify({{account_id:t,plan:p}})}});
  if(d.ok)loadUsers(); else alert(d.error||'失败')}}
async function setOwner(t,cur){{const d=await api('/api/admin/set_owner',{{method:'POST',body:JSON.stringify({{account_id:t,is_owner:cur?0:1}})}});
  if(d.ok)loadUsers(); else alert(d.error||'失败')}}

async function accountDetail(acct){{const d=await api('/api/admin/account_detail',{{method:'POST',body:JSON.stringify({{account_id:acct}})}});
  const box=document.getElementById('adetail');if(!d.account){{box.innerHTML='<div class="err">'+esc(d.error||'')+'</div>';return}}
  let h='<div class="card"><h2>账户详情: <code>'+esc(d.account.account_id)+'</code> · '+
    +(d.account.is_owner?'<span class="badge b-owner">owner无限</span>':'<span class="badge b-'+esc(d.account.plan)+'">'+esc(d.account.plan)+'</span>')+
    ' · 本月用量 '+d.account.used+'</h2><table><thead><tr><th>Token(设备)</th><th>套餐</th><th>本月用量</th><th>创建</th><th>下钻</th></tr></thead><tbody>';
  (d.tokens||[]).forEach(t=>{{h+='<tr><td><code>'+esc(t.token)+'</code></td><td>'+esc(t.plan)+'</td><td>'+t.used+'</td>'+
    '<td class="small">'+escNice(t.created_at)+'</td><td><button class="mini" onclick="drill(\\''+esc(t.token)+'\\')">任务</button></td></tr>'}});
  h+='</tbody></table><button class="mini" onclick="document.getElementById(\\'adetail\\').innerHTML=\\'\\'">关闭</button></div>';
  box.innerHTML=h;}}

async function loadCodes(){{const d=await api('/api/admin/codes');const tb=document.getElementById('ctb');tb.innerHTML='';
  (d.codes||[]).forEach(c=>{{const used=c.status&&c.status!=='unused',exp=c.expires_at;
    const stc=used?'b-used':(exp&&exp*1000<Date.now()?'b-expired':'b-unused');
    const stl=used?'已用':(exp&&exp*1000<Date.now()?'已过期':'可用');
    const tr=document.createElement('tr');tr.dataset.code=c.code;tr.dataset.rem=c.remark||'';
    tr.innerHTML='<td><code>'+esc(c.code)+'</code> <button class="mini" onclick="copyCode(\\''+c.code+'\\')">复制</button></td>'+
    '<td>'+esc(c.plan)+'</td><td><span class="badge '+stc+'">'+stl+'</span></td>'+
    '<td>'+esc(c.remark)+' <button class="mini" onclick="setRemark(this.parentNode.parentNode.dataset.code,this.parentNode.parentNode.dataset.rem)">备注</button></td>'+
    '<td class="small">'+(exp?escNice(exp):'永久')+'</td>'+
    '<td class="small">'+(c.used_by?('<code>'+esc(c.used_by)+'</code> '+(c.used_at?'('+escNice(c.used_at)+')':'')):'—')+'</td>'+
    '<td>'+(c.used_by?'<button class="mini" onclick="accountDetail(\\''+esc(c.used_by)+'\\')">账户</button>':'—')+'</td>';
    tb.appendChild(tr);}})}}

async function setRemark(code,dft){{const r=prompt('客户名 / 备注:',dft||'');if(r==null)return;
  const d=await api('/api/admin/set_remark',{{method:'POST',body:JSON.stringify({{code,remark:r}})}});if(d.ok){{loadCodes();loadUsers()}}}}
function copyCode(c){{navigator.clipboard.writeText(c);alert('已复制 '+c)}}

async function loadJobs(){{const d=await api('/api/admin/jobs');const tb=document.getElementById('jtb');tb.innerHTML='';
  (d.jobs||[]).forEach(j=>{{const tr=document.createElement('tr');
    tr.innerHTML='<td class="small">'+esc(j.job_id)+'</td><td><code>'+esc(j.user_id)+'</code></td>'+
    '<td>'+esc(j.status)+'</td><td>'+esc((j.prompt||'').slice(0,40))+'</td>'+
    '<td class="small">'+escNice(j.created_at)+'</td>';tb.appendChild(tr);}})}}

async function genCodes(){{const d=await api('/api/admin/gen_codes',{{method:'POST',body:JSON.stringify({{plan:document.getElementById('gp').value,n:+document.getElementById('gn').value,days:+document.getElementById('gd').value,remark:document.getElementById('gr').value}})}});
  if(d.codes){{msg('gmsg2','已生成：'+d.codes.map(c=>c).join(' , '),'ok');loadCodes()}}else msg('gmsg2',d.error||'失败','err')}}
async function searchUser(){{const kw=document.getElementById('sk').value.trim();const d=await api('/api/admin/search',{{method:'POST',body:JSON.stringify({{keyword:kw}})}});
  document.querySelectorAll('#atb tr').forEach(r=>r.classList.remove('hl'));
  (d.accounts||[]).forEach(a=>document.querySelectorAll('#atb tr').forEach(r=>{{if(r.dataset.id===a.account_id)r.classList.add('hl')}}))}}
function drill(t){{loadJobs();if(t)setTimeout(()=>document.querySelectorAll('#jtb tr').forEach(r=>{{if(!r.innerText.includes(t))r.style.display='none'}}),400);}}

(async function(){{if(!tk)return showGate();try{{const d=await api('/api/admin/users');if(d.users){{showPanel();loadAll()}}else showGate()}}catch(e){{showGate()}}}})();
function loadAll(){{loadUsers();loadCodes();loadJobs()}}
</script></body></html>"""


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