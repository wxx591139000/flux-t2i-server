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
import time
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


def _row_value(row, col, default=None):
    """从 sqlite3.Row **安全取列**（列不存在返回 default，而非抛 IndexError）。

    与 flux_queue._row_value 是同一份逻辑，**故意各留一份**：web 层 import 调度器会
    引入反向依赖（flux_queue 已 import 本模块的兄弟），为一个 5 行工具冒循环导入风险
    不值得。触发场景：老库没跑到 ALTER TABLE 迁移（缺 deleted_at 列）时直接索引即炸，
    而 web 层一炸就是整页 500（2026-09-21）。
    """
    try:
        return row[col]
    except (IndexError, KeyError):
        return default

WEB_ADMIN_TOKEN = os.environ.get('WEB_ADMIN_TOKEN', '')
SERVICE_NAME = 'FLUX 文生图'
OWNER_UNLIMITED_HINT = 'owner'
WEB_STARTED_AT = time.time()   # web 进程启动时刻，供 /health 报 uptime


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

    # ── 健康探针 ──
    def _health(self):
        """浅探针：GET /health。

        **刻意不做任何 SSH / 上游 HTTP 外呼。** 探针必须恒定快：若在这里去探 GPU 机器，
        一台机器关机就会让本服务 /health 变慢甚至超时，监控会把「后端不可用」误判成
        「网站死了」——这两件事必须能分开看。GPU 侧可用性请直接问常驻服务：
            curl http://127.0.0.1:$FLUX_RESIDENT_PORT/health
        （那是另一台机器上的另一个服务，字段见 server/flux_resident_server.py:_health）

        为什么要有这个探针：worker 线程若异常退出，web 仍会照常 200 —— 用户提交
        得到 queued 却永远不出图，从外部完全看不出来。`worker_alive` 是唯一能区分
        「活着」与「僵尸」的信号，且不需要碰网络。

        字段：
          status        ok / degraded；degraded 只表示**本进程**有问题（DB 或 worker）
          db_ok         SQLite 可读
          worker_alive  worker 线程活着？null = 未挂调度器（web-only 模式，见本文件 main()）
          health_alive  健康监控线程活着？（它负责把 waiting 任务捞回来）
          gen_mode      本进程生效的生成路径 resident / legacy
          queue_depth   待处理任务数（内存优先队列）
          inflight      去重集合大小（排队中 + 生成中）
          waiting       等 GPU 侧恢复的任务数；>0 说明后端当前不可用
          backend_hint  由 waiting/queue 推导的后端状态提示（**推导值，非实测**）
          web_uptime_sec / uptime_sec   web 进程 / 调度器 运行秒数
          errors        探针过程中捕获到的异常（空数组 = 正常）
        """
        st, sched_err = {}, ''
        has_sched = self.scheduler is not None
        if has_sched:
            try:
                st = self.scheduler.stats() or {}
            except Exception as e:      # 探针自身绝不能抛，否则 200 变 500
                sched_err = f'{type(e).__name__}: {e}'
                logger.warning(f'/health 读取调度器状态失败: {sched_err}')
        # 读不出状态时报 None（未知），而不是 False —— 别把「未知」冒充「已死」，
        # 也别把它和「没挂调度器」混为一谈（下面判定要区分）。
        worker = (st.get('worker_alive') if not sched_err else None) if has_sched else None

        db_ok, db_err = False, ''
        try:
            db_ok = bool(self.db.ping())
        except Exception as e:
            db_err = f'{type(e).__name__}: {e}'
            logger.warning(f'/health 数据库探活失败: {db_err}')

        # 健康判定：DB 必须可用；挂了调度器则必须能读出它、且其 worker 活着。
        # 未挂调度器（web-only 模式，见本文件 main()）算健康 —— 那是合法运行模式，
        # 只是本进程不产出图（生成在别的进程里跑）。
        healthy = bool(db_ok) and not (has_sched and (bool(sched_err) or not worker))

        depth = st.get('queue_depth') or 0
        if st.get('waiting'):
            hint = 'server_down'        # waiting 池只装 [SERVER_DOWN] 任务
        elif depth or st.get('inflight'):
            hint = 'busy'
        else:
            hint = 'idle'

        self._json({
            'status': 'ok' if healthy else 'degraded',
            'service': SERVICE_NAME,
            'db_ok': db_ok,
            'worker_alive': worker,
            'health_alive': st.get('health_alive') if has_sched else None,
            'gen_mode': st.get('gen_mode') if has_sched else None,
            'queue_depth': depth if has_sched else None,
            'inflight': st.get('inflight') if has_sched else None,
            'waiting': st.get('waiting') if has_sched else None,
            'backend_hint': hint,
            'web_uptime_sec': round(time.time() - WEB_STARTED_AT, 1),
            'uptime_sec': st.get('uptime_sec'),
            'errors': [x for x in (db_err, sched_err) if x],
        }, 200 if healthy else 503)

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
                self._health()
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
            elif path == '/api/edit':
                self._api_edit(token, body)
            elif path == '/api/delete':
                self._api_delete(token, body)
            elif path == '/api/activate':
                self._api_activate(body, token)
            elif path == '/api/bind':
                self._api_bind(body, token)
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

        def _pint(name):
            v = data.get(name)
            if v in (None, ''):
                return None
            if isinstance(v, bool):  # bool 是 int 子类，int(True)=1，需显式拒绝
                raise ValueError(f'{name} 参数非法（应为整数）：{v!r}')
            try:
                return int(v)
            except (TypeError, ValueError):
                # 诚信：参数给了却没法用，明确报 400，而不是静默退回默认值
                raise ValueError(f'{name} 参数非法（应为整数）：{v!r}')

        try:
            width = _pint('width')
            height = _pint('height')
            seed = _pint('seed')
            steps = _pint('steps')
        except ValueError as e:
            self._json({'error': str(e)}, 400)
            return

        negative_prompt = data.get('negative_prompt') or None

        self._resolve_user(token)
        result = self.scheduler.submit(token, prompt, priority,
                                       width, height, seed, steps, negative_prompt)
        self._json(result)

    def _api_edit(self, token, body):
        """图生图提交（2026-09-18 新增）。

        与 `/api/submit` 唯一的差别 = body 多一个 `image`（base64 参考图）。
        参数解析、翻译、计费、配额、队列全部复用 `scheduler.submit`，本条只负责
        「把参考图取出来 + 把参数异常翻译成 400」。

        ⚠️ 缺 `image` 时**明确 400 拒绝，不静默降级成文生图** —— 那会照常出图、
        照常扣费，但完全不是用户要的东西，是最难被发现的一类错误。
        """
        data = json.loads(body or '{}')
        prompt = data.get('prompt', '')
        priority = int(data.get('priority', 0) or 0)
        ref_image = (data.get('image') or '').strip()
        if not ref_image:
            self._json({'error': '缺少参考图（image 字段，base64 或 data URL）'}, 400)
            return

        def _pint(name):
            v = data.get(name)
            if v in (None, ''):
                return None
            if isinstance(v, bool):  # bool 是 int 子类，int(True)=1，需显式拒绝
                raise ValueError(f'{name} 参数非法（应为整数）：{v!r}')
            try:
                return int(v)
            except (TypeError, ValueError):
                raise ValueError(f'{name} 参数非法（应为整数）：{v!r}')

        try:
            width = _pint('width')
            height = _pint('height')
            seed = _pint('seed')
            steps = _pint('steps')
        except ValueError as e:
            self._json({'error': str(e)}, 400)
            return

        negative_prompt = data.get('negative_prompt') or None

        self._resolve_user(token)
        result = self.scheduler.submit(token, prompt, priority,
                                       width, height, seed, steps, negative_prompt,
                                       ref_image=ref_image)
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
            'seed': job['seed'], 'width': job['width'], 'height': job['height'],
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
        # 已删任务的图片必须立刻不可达：否则「删了但直链还能下载」，
        # 用户会认为删除没生效（图片字节也确实还在被服务）。
        if _row_value(job, 'deleted_at') is not None:
            self._send(404, '图片已被删除')
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

    def _api_delete(self, token, body):
        """彻底删除某个任务（记录 + 图片文件）。

        **权限**：只能删自己的。SQL 层用 `user_id=?` 钉死（见 db.job_mark_deleted），
        所以越权在数据层就不可能发生；这里的 403 只是为了给出人话报错。

        **删除是"彻底"的**，分两种情形：
          · 已完成/已失败（终态）→ 立即删图片文件 + 打墓碑，用户视角立刻消失。
          · 排队中/生成中（在途）  → 打墓碑 + 从待跑队列剔除；**图片交给 worker 收尾**。
            为什么不当场删：任务可能已经派给 GPU 在跑了，现在删文件它跑完还会再写一张；
            而且 worker 完成后需要知道"这活用户不要了"，才能丢弃产物。
            worker 侧靠 jobs_abandoned() / job_get() 看到墓碑后自行清理。

        **不退配额**：用完即消耗。若删除退额度，用户可"删了重传"无限刷，套餐形同虚设
        （已失败任务本来就退过款，不受影响）。

        返回里带 `purged` 字段，告诉调用方这次是"立即清干净了"还是"交给后台收尾"。
        """
        data = json.loads(body or '{}')
        job_id = (data.get('job_id') or '').strip()
        if not job_id:
            self._json({'error': '缺少 job_id'})
            return
        job = self.db.job_get(job_id)
        if not job:
            self._json({'error': '任务不存在'})
            return
        if job['user_id'] != token:
            # 措辞刻意与"不存在"一致：不泄漏"这个 id 确实存在、只是不是你的"
            self._json({'error': '任务不存在或无权删除'}, 403)
            return
        if _row_value(job, 'deleted_at') is not None:
            self._json({'ok': True, 'job_id': job_id, 'purged': True, 'note': '任务此前已删除'})
            return

        status = job['status']
        inflight = status in ('queued', 'generating')

        # ① 先打墓碑 —— 这一步成功才算"删除受理"。
        #    顺序很重要：先打墓碑再清文件，任何时刻中断都不会出现"文件没了但任务还在"的
        #    半死状态（那种状态下用户看到任务却永远打不开图，比不删更糟）。
        if not self.db.job_mark_deleted(job_id, token):
            self._json({'error': '删除失败，请重试'}, 500)
            return

        purged = False
        if not inflight:
            # ② 终态任务：图片文件立即物理删除
            purged = self._purge_job_image(job_id)

        # ③ 在途任务：从调度器内存里剔掉（队列/等待池/去重键），别让它再被捞起来
        if inflight:
            try:
                self.scheduler.drop_job(job_id, job)
            except Exception as e:                                # noqa: BLE001
                # 剔除失败不能回滚删除 —— 墓碑已经打了，worker 会兜住（跑完自查发现已删则丢弃）
                logger.warning(f'删除任务 {job_id} 时从调度器剔除失败（worker 会兜住）: {e}')

        logger.info(f'🗑️  用户删除任务 {job_id}（原状态 {status}，'
                    f'{"图片已清理" if purged else "在途，交 worker 收尾"}）')
        self._json({'ok': True, 'job_id': job_id, 'purged': purged,
                    'was_inflight': inflight,
                    'note': '已删除' if purged else '已删除，生成中的任务会在结束后自动清理'})

    def _purge_job_image(self, job_id: str) -> bool:
        """删除该任务产出的图片文件（含图生图的参考图目录）。返回是否有文件被删。

        ⚠️ **绝不做目录级 rm -rf**：image_path 来自数据库，历史数据里可能是任意路径。
        只允许删除"确实存在、且是普通文件/我们自己的产物目录"的目标，且逐个操作，
        宁可少删也不要因为一条脏数据误删整个目录（2026-09-21）。
        """
        removed = False
        try:
            job = self.db.job_get(job_id)
            if not job:
                return False
            # 产物 PNG：只删文件本身
            p = _row_value(job, 'image_path')
            if p:
                f = Path(p)
                if f.is_file():
                    f.unlink()
                    removed = True
                    logger.info(f'🗑️  已删除图片文件 {f}')
            # 该任务的输出目录 web_out/<job_id>/（若有），仅当它确实是我们约定的产物目录
            for d in (Path('web_out') / job_id,):
                if d.is_dir() and d.name == job_id:
                    for child in d.iterdir():
                        if child.is_file():
                            child.unlink()
                            removed = True
                    try:
                        d.rmdir()          # 只删空目录；非空说明还有别的东西，留着
                    except OSError:
                        pass
        except Exception as e:                                    # noqa: BLE001
            logger.warning(f'清理任务 {job_id} 图片时出错（墓碑已打，图片可能残留）: {e}')
        return removed

    def _api_activate(self, body, token: str = ''):
        """激活激活码（新建账户 / 并入已有账户）。

        `token` 由 do_POST 从 cookie 或 `?token=` 解析后传入 —— **identity 属于 HTTP 层，
        不该要求前端在 body 里重复一遍**。旧版只认 body.token，而页面上的"激活 / 绑定"
        按钮只发了 `{code}`，于是**每一次点击都必然报「缺少激活码或 token」**（2026-09-17 实测）。
        现在 body.token 优先（兼容脚本调用），缺了就用 HTTP 身份兜底。
        """
        data = json.loads(body or '{}')
        code = (data.get('code') or '').strip().upper()
        token = (data.get('token') or token or '').strip()
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

    def _api_bind(self, body, token: str = ''):
        """换设备：新 token 并入已激活码的账户（共享套餐/用量）。

        token 解析规则同 `_api_activate`（body 优先，HTTP 身份兜底）。
        """
        data = json.loads(body or '{}')
        code = (data.get('code') or '').strip().upper()
        token = (data.get('token') or token or '').strip()
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
                    f'<td>{j["created_at"]}</td>'
                    f'<td><button type="button" class="delbtn" '
                    f'onclick="delJob(\'{j["job_id"]}\', this)">删除</button></td></tr>')
    if not rows:
        return '<tr><td colspan="7" style="text-align:center;color:#888">暂无任务</td></tr>'
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
.delbtn{{background:#fff;color:#dc2626;border:1px solid #f0b4b4;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}}
.delbtn:hover{{background:#fef2f2}}
.delbtn[disabled]{{opacity:.45;cursor:default}}
</style></head><body>
<h1>{service}</h1>
<div class="card meta">你的Token: <code>{token}</code> · 套餐: <b>{plan}</b> · 用量: {usage}<br>
<span id="acct" style="color:#4a76f7"></span>
<small>Token 已存入浏览器 Cookie，作为你的身份标识</small>
<small style="float:right"><a href="/admin" style="color:#999">商户管理</a></small></div>

<div class="card"><h2>生成图片</h2>
<textarea id="p" placeholder="中文 / English 都可以。中文示例：一只白色陶瓷马克杯，置于木桌，自然光；English: a white ceramic mug on a wooden table, soft natural light"></textarea>
<small style="color:#888;display:block;margin-top:6px">支持中文与英文。中文会由后台自动翻译成 FLUX 适用的英文提示词后再出图（约多花几秒）</small>
<button onclick="submit()" style="margin-top:10px">提交生成</button>
<div id="msg" class="msg"></div></div>

<div class="card"><h2>激活 / 绑定账户</h2>
<input id="code" placeholder="粘贴激活码" onkeydown="if(event.key==='Enter')activate()"><button onclick="activate()">激活 / 绑定</button>
<div id="amsg" class="msg"></div>
<small style="color:#888">新客户输入激活码创建账户；换设备时输入已激活的码可并入同一账户，共享套餐与用量</small></div>

<div class="card"><h2>我的任务</h2><table>
<tr><th>任务ID</th><th>状态</th><th>提示词</th><th>图片</th><th>错误</th><th>时间</th><th>操作</th></tr>
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
// 彻底删除任务（记录 + 图片）。二次确认是必须的：此操作不可恢复，
// 且配额不退（用完即消耗），用户可能以为删了就能退回次数。
function delJob(id,btn){{
  if(!confirm('确定彻底删除任务 '+id+' 吗？\\n\\n· 记录与图片都会被删除，不可恢复\\n· 已消耗的额度不会退回'))return;
  btn.disabled=true;btn.textContent='删除中';
  fetch('/api/delete',{{method:'POST',body:JSON.stringify({{job_id:id}})}})
    .then(r=>r.json()).then(d=>{{
      if(d.ok){{
        var tr=btn.closest('tr'); if(tr)tr.style.opacity='.35';
        btn.textContent='已删除';
        setTimeout(()=>location.reload(),600);
      }}else{{
        btn.disabled=false;btn.textContent='删除';
        alert(d.error||'删除失败');
      }}
    }}).catch(e=>{{btn.disabled=false;btn.textContent='删除';alert('删除失败: '+e)}})
}}
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
.delbtn{{background:#fff;color:#dc2626;border:1px solid #f0b4b4;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}}
.delbtn:hover{{background:#fef2f2}} .delbtn[disabled]{{opacity:.45;cursor:default}}
</style></head><body>
<div class="card"><h1>{service} · 我的任务</h1>
<div class="meta">Token: <code>{token}</code> · 套餐: <b>{plan}</b> · 用量: {usage}</div>
<a href="/">← 返回生成页</a></div>
<div class="card"><table>
<tr><th>任务ID</th><th>状态</th><th>提示词</th><th>图片</th><th>错误</th><th>时间</th><th>操作</th></tr>
{jobs}</table></div>
<script>
// 彻底删除任务（记录 + 图片）。二次确认是必须的：不可恢复，且配额不退。
function delJob(id,btn){{
  if(!confirm('确定彻底删除任务 '+id+' 吗？\\n\\n· 记录与图片都会被删除，不可恢复\\n· 已消耗的额度不会退回'))return;
  btn.disabled=true;btn.textContent='删除中';
  fetch('/api/delete',{{method:'POST',body:JSON.stringify({{job_id:id}})}})
    .then(r=>r.json()).then(d=>{{
      if(d.ok){{
        var tr=btn.closest('tr'); if(tr)tr.style.opacity='.35';
        btn.textContent='已删除';
        setTimeout(()=>location.reload(),600);
      }}else{{btn.disabled=false;btn.textContent='删除';alert(d.error||'删除失败')}}
    }}).catch(e=>{{btn.disabled=false;btn.textContent='删除';alert('删除失败: '+e)}})
}}
</script></body></html>"""

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

  <div class="card"><h2>用户码绑定 <span class="small">（把一个用户 / 设备并入某个账户，共享套餐与用量）</span></h2>
  <input id="btk" placeholder="用户 token" style="width:260px">
  <input id="bcode" placeholder="账户 = 激活码" style="width:180px">
  <button onclick="bindUser()">绑定</button> <span id="bmsg" class="msg"></span>
  <div class="small" style="margin-top:6px">
    token 从哪来：客户页（<a href="/" target="_blank">/</a>）与「我的任务」页（<a href="/center" target="_blank">/center</a>）顶部都会显示「你的Token」；
    也可以在上面账户表格里搜到客户后点「详情」，看到该账户下所有设备 token。
    绑定要求目标激活码**已被激活过**（账户已存在）。
  </div>
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
  const d=await api('/api/admin/users'); if(d.accounts!==undefined){{localStorage.setItem(K,tk);showPanel();loadAll()}}
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
    ' <button class="mini" onclick="bindTo(\\''+esc(a.account_id)+'\\')">绑设备</button>'+
    ' <button class="mini" onclick="accountDetail(\\''+esc(a.account_id)+'\\')">详情</button></td>';
    tb.appendChild(tr);}})}}

async function setPlan(t,p){{const d=await api('/api/admin/set_plan',{{method:'POST',body:JSON.stringify({{account_id:t,plan:p}})}});
  if(d.ok)loadUsers(); else alert(d.error||'失败')}}

// 用户码绑定：把一个用户/设备(token)并入某个账户(激活码)，共享套餐与用量。
// 服务端 /api/bind 的 token 走 HTTP 身份兜底，但这里是管理员代客户操作，
// 目标 token 必须显式传 body.token。
async function bindUser(){{
  const t=document.getElementById('btk').value.trim();
  const c=document.getElementById('bcode').value.trim().toUpperCase();
  const el=document.getElementById('bmsg');
  if(!t||!c){{el.innerHTML='<span class="err">请填 token 与激活码</span>';return}}
  const d=await api('/api/bind',{{method:'POST',body:JSON.stringify({{code:c,token:t}})}});
  if(d.ok){{el.innerHTML='<span class="ok">'+esc(d.msg||'已绑定')+'</span>';document.getElementById('btk').value='';loadUsers();loadCodes()}}
  else el.innerHTML='<span class="err">'+esc(d.error||'失败')+'</span>';
}}
async function bindTo(acct){{
  const t=prompt('要并入账户 '+acct+' 的用户 token（客户页/我的任务页顶部的「你的Token」）：','');
  if(!t||!t.trim())return;
  const d=await api('/api/bind',{{method:'POST',body:JSON.stringify({{code:acct,token:t.trim()}})}});
  if(d.ok){{alert('已绑定：'+(d.msg||''));loadUsers();loadCodes()}}else alert(d.error||'失败');
}}
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

(async function(){{if(!tk)return showGate();try{{const d=await api('/api/admin/users');if(d.accounts!==undefined){{showPanel();loadAll()}}else showGate()}}catch(e){{showGate()}}}})();
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