#!/usr/bin/env python3
"""
FLUX 常驻生成服务 —— 调用侧客户端 / 传输层

配套 server/flux_resident_server.py（跑在 GPU 机上，模型常驻内存）。
纯标准库（urllib + subprocess），不引入 requests 等新依赖。

【两种传输】
1) DirectTransport  常驻服务地址本机可达（本地部署模型 / 自己已开隧道）
       FLUX_RESIDENT_BASE=http://127.0.0.1:9630
2) SshCurlTransport 常驻服务在远端 GPU 机（AutoDL），本机只有 SSH
       无需端口转发：直接 `ssh <alias> 'curl http://127.0.0.1:9630/...'`。
       好处是不新增公网暴露面、也不必管理隧道生命周期（与项目原有
       fsm.run 走 ssh 的风格一致）。
       图片不走 HTTP：常驻服务已把 PNG 落盘到 <remote_base>/resident_out/<job_id>.png，
       沿用项目原有的 scp 拉回模式。
用哪个由 _transport(server) 决定（FLUX_RESIDENT_BASE 有值 → 直连，否则走 SSH）。

【环境变量】
    FLUX_RESIDENT_PORT          常驻服务端口（默认 9630）
    FLUX_RESIDENT_BASE          直连地址；留空 = 走 SSH
    FLUX_RESIDENT_TOKEN         共享令牌（与服务端一致；留空则不鉴权）
    FLUX_RESIDENT_PY            远端 python（默认 /root/miniconda3/envs/flux/bin/python）
    FLUX_RESIDENT_SCREEN        远端 screen 会话名（默认 fluxd）
    FLUX_RESIDENT_WAIT_MODEL    等首次模型加载的上限秒数（默认 900）
    FLUX_RESIDENT_AUTOSTART     常驻没起来时是否自动拉起（默认 1）
    FLUX_RESIDENT_SYNC          1 = 每次强制重传服务脚本并重启（改了服务端代码时用）
    FLUX_OFFLOAD                none|model|sequential（拉起时透传给服务端）
    FLUX_RESIDENT_USE_PROXY     1 = 直连时也走系统代理（默认绕开，见下方说明）

【命令行】
    python manager/flux_resident_client.py servers               # 列出候选机 + 标出会被选哪台
    python manager/flux_resident_client.py probe                 # 同上（旧名，保留）
    python manager/flux_resident_client.py health  [--server flux1]
    python manager/flux_resident_client.py up      [--server flux1]   # 幂等拉起
    python manager/flux_resident_client.py gen "一双白色运动鞋" --out out.png \
        --width 1024 --height 1024 --seed 7 --negative "blurry, watermark"
    python manager/flux_resident_client.py bench "一双白色运动鞋" --count 5

【多机 / 换机（自动选在线机）】
候选机 = 显式注册的 flux1/flux2 + ~/.ssh/config 中匹配 FLUX_SERVER_ALIAS_GLOB 的别名。
克隆实例到新服务器后，只要 ~/.ssh/config 里有一条对应 Host，就自动成为候选机，不用改代码。
    FLUX_SERVER_DISCOVER        1（默认）| 0 关闭自动发现
    FLUX_SERVER_ALIAS_GLOB      别名匹配模式，默认 autodl-flux*，逗号可写多个
    FLUX_REMOTE_BASE            克隆实例的工作目录（默认 /root/autodl-tmp/flux-t2i）
    FLUX_REMOTE_MODEL           克隆实例的模型路径（默认 …/models/FLUX.1-dev）
    FLUX_PROBE_TTL              探测结果缓存秒数（默认 20，0=关闭）
"""
import argparse
import base64
import json
import logging
import os
import shlex
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import manager.flux_server_manager as fsm   # noqa: E402  （fsm 不反向 import 本模块，无循环）
from manager.feishu_notify import _load_env  # noqa: E402
_load_env()                                  # 先加载 manager/.env，再读下面的 FLUX_* 配置

logger = logging.getLogger('manager.flux_resident')

# ── 配置 ──
REMOTE_PORT = int(os.environ.get('FLUX_RESIDENT_PORT', '9630'))
DIRECT_BASE = os.environ.get('FLUX_RESIDENT_BASE', '').strip()
TOKEN = os.environ.get('FLUX_RESIDENT_TOKEN', '')
RESIDENT_PY = os.environ.get('FLUX_RESIDENT_PY', '/root/miniconda3/envs/flux/bin/python')
RESIDENT_SCREEN = os.environ.get('FLUX_RESIDENT_SCREEN', 'fluxd')
RESIDENT_LOG = os.environ.get('FLUX_RESIDENT_LOG', 'fluxd.log')
WAIT_MODEL = int(os.environ.get('FLUX_RESIDENT_WAIT_MODEL', '900'))
AUTOSTART = os.environ.get('FLUX_RESIDENT_AUTOSTART', '1') == '1'
FORCE_SYNC = os.environ.get('FLUX_RESIDENT_SYNC') == '1'
SSH_CONNECT_TIMEOUT = int(os.environ.get('FLUX_RESIDENT_SSH_CONNECT_TIMEOUT', '8'))
HEALTH_TIMEOUT = int(os.environ.get('FLUX_RESIDENT_HEALTH_TIMEOUT', '10'))

LOCAL_SERVER_PY = BASE_DIR / 'server' / 'flux_resident_server.py'
LOCAL_START_SH = BASE_DIR / 'server' / 'start_resident.sh'

TERMINAL = ('done', 'failed', 'canceled')


# ══════════════════ 代理绕行（本机实测坑，2026-09-16）══════════════════
# 环境中设了 http_proxy/https_proxy 但**没有设 no_proxy**，urllib 默认 opener 会把
# http://127.0.0.1:xxxx 的请求也发给代理。后果：
#   1) 无谓绕一圈代理，代理一挂本地生成就断；
#   2) 连不上时收到的是代理的 502，把真实的 ConnectionRefused 盖掉，排查被误导。
# 常驻服务总是本地/私网地址，故默认绕开代理。
_OPENER_CACHE = {}


def _opener():
    use_proxy = os.environ.get('FLUX_RESIDENT_USE_PROXY') == '1'
    if use_proxy not in _OPENER_CACHE:
        handlers = [] if use_proxy else [urllib.request.ProxyHandler({})]
        _OPENER_CACHE[use_proxy] = urllib.request.build_opener(*handlers)
    return _OPENER_CACHE[use_proxy]


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ══════════════════════════ 异常 ══════════════════════════
class TransportError(RuntimeError):
    """统一异常类型。调用方（flux_queue）只需 catch 这一种，按 kind 分流：

    kind='server_down' → 基础设施不可用（机器关机 / SSH 不通 / 常驻起不来）
                         调用方应把任务放进「等待恢复池」，而不是判失败
    kind='failed'      → 业务失败（模型报错 / 生成失败 / 超时 / 入参非法）
                         调用方应标记任务失败
    """

    def __init__(self, msg: str, kind: str = 'failed'):
        super().__init__(msg)
        self.kind = kind


# ══════════════════════════ 传输层 ══════════════════════════
class DirectTransport:
    """直连（本机可访问常驻服务）。"""

    kind = 'direct'

    def __init__(self, base: str, token: str = ''):
        self.base = (base or f'http://127.0.0.1:{REMOTE_PORT}').rstrip('/')
        self.token = token

    def _req(self, path: str, method: str = 'GET', body: dict = None,
             raw: bool = False, timeout: int = 30):
        url = f'{self.base}{path}'
        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf-8'
        if self.token:
            headers['X-Auth-Token'] = self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _opener().open(req, timeout=timeout) as r:
                payload = r.read()
                return (r.status, payload) if raw else (r.status, json.loads(payload.decode('utf-8') or '{}'))
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = json.loads(e.read().decode('utf-8')).get('error', '')
            except Exception:
                pass
            raise TransportError(f'HTTP {e.code}: {detail or e.reason}', kind='failed') from None
        except urllib.error.URLError as e:
            extra = '' if _port_open(urllib.parse.urlparse(self.base).hostname or '127.0.0.1',
                                    urllib.parse.urlparse(self.base).port or 80) \
                else f'（端口无监听）'
            raise TransportError(f'连接不上常驻服务 {self.base}: {e.reason}{extra}',
                                 kind='server_down') from None
        except json.JSONDecodeError as e:
            raise TransportError(f'返回体不是 JSON: {e}', kind='failed') from None

    def get_json(self, path: str, timeout: int = 30) -> dict:
        return self._req(path, timeout=timeout)[1]

    def post_json(self, path: str, body: dict, timeout: int = 60) -> dict:
        return self._req(path, 'POST', body, timeout=timeout)[1]

    def fetch_png(self, job_id: str, dest: Path, server: dict) -> Path:
        """直连：走 HTTP /image 取图（不依赖远端文件路径）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._req(f'/image?job_id={urllib.parse.quote(job_id)}',
                                   raw=True, timeout=180)[1])
        return dest


class SshCurlTransport:
    """远端 GPU 机：HTTP 调用都通过 `ssh <alias> curl ...` 执行。

    调用一次 = 一次 ssh 往返。POST 的 JSON 用 base64 内联（base64 字符集
    只有 A-Za-z0-9+/=，不含任何 shell 元字符），避免临时文件与第二次 scp 往返。
    """

    kind = 'ssh-curl'

    def __init__(self, server: dict, token: str = ''):
        self.server = server
        self.alias = server['alias']
        self.token = token

    def _curl_cmd(self, path: str, method: str = 'GET', body_b64: str = None,
                  timeout: int = 60) -> str:
        url = f'http://127.0.0.1:{REMOTE_PORT}{path}'
        parts = ['curl', '-sS', '--max-time', str(max(int(timeout) - 5, 5))]
        if method == 'POST':
            parts += ['-X', 'POST', "-H 'Content-Type: application/json; charset=utf-8'",
                      '--data-binary', '@-']
        if self.token:
            parts.append(f"-H 'X-Auth-Token: {self.token}'")
        parts.append(shlex.quote(url))
        cmd = ' '.join(parts)
        if body_b64:
            cmd = f'echo {body_b64} | base64 -d | {cmd}'
        return cmd

    def _ssh(self, remote_cmd: str, timeout: int) -> str:
        ok, out = fsm.run(
            f'ssh -o ConnectTimeout={SSH_CONNECT_TIMEOUT} {self.alias} {shlex.quote(remote_cmd)}',
            timeout)
        if not ok:
            raise TransportError(f'SSH 调用失败（{self.alias}）: {out[:200]}', kind='server_down')
        return out

    def get_json(self, path: str, timeout: int = 30) -> dict:
        out = self._ssh(self._curl_cmd(path, timeout=timeout), timeout)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            # 常驻没起来时 curl 打不通端口，返回的是空串 → 这是「服务不可用」而非业务错误
            if not out.strip():
                raise TransportError(
                    f'常驻服务无响应（{self.alias} → 127.0.0.1:{REMOTE_PORT}）',
                    kind='server_down') from None
            raise TransportError(f'返回体不是 JSON: {out[:200]}', kind='failed') from None

    def post_json(self, path: str, body: dict, timeout: int = 60) -> dict:
        b64 = base64.b64encode(json.dumps(body, ensure_ascii=False).encode('utf-8')).decode('ascii')
        out = self._ssh(self._curl_cmd(path, 'POST', b64, timeout), timeout)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise TransportError(f'返回体不是 JSON: {out[:200]}', kind='failed') from None

    def fetch_png(self, job_id: str, dest: Path, server: dict) -> Path:
        """SSH：常驻服务已把 PNG 落盘，直接 scp 拉回（沿用项目原有模式）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        remote = f'{server["remote_base"]}/resident_out/{job_id}.png'
        ok, out = fsm.run(f'scp {self.alias}:{remote} "{str(dest).replace(chr(92), "/")}"', 180)
        if not ok or not dest.exists():
            raise TransportError(f'拉回图片失败: {out[:200]}', kind='server_down')
        return dest


def _transport(server: dict):
    if DIRECT_BASE:
        return DirectTransport(DIRECT_BASE, TOKEN)
    return SshCurlTransport(server, TOKEN)


# ══════════════════════════ 探活 / 选机 ══════════════════════════
def probe(server: dict, force: bool = False) -> dict:
    """探测单台，拿齐：可达 / 有卡 / 模型文件 / 常驻在跑 / 模型已加载。

    两种后端分流（这是必须的，否则直连模式会去连不存在的 SSH 别名）：
      · FLUX_RESIDENT_BASE 有值 → 直连模式，一次 HTTP /health 即可，不查 GPU/模型文件
      · 否则                    → 委托 fsm.probe_full：固定 1 次 SSH 往返拿齐四态

    与旧实现的差别：旧版只探测「可达 + 常驻 + 模型已加载」，
    常驻没跑时还要补第 2 次往返去问 server_reachable，且**完全不知道有没有 GPU**
    —— 而「有没有卡」正是「自动选在线机」的必要判据。
    """
    if DIRECT_BASE:
        d = {'name': server.get('name'), 'alias': server.get('alias'),
             'reachable': False, 'gpu_ok': True, 'gpu': '(直连模式不查)',
             'model_ok': True, 'resident': False, 'model_loaded': False,
             'status': '', 'health': None, 'error': ''}
        try:
            h = _transport(server).get_json('/health', timeout=HEALTH_TIMEOUT)
        except TransportError as e:
            d['error'] = str(e)
            return d
        d.update(reachable=True, resident=True, health=h)
        d['status'] = h.get('status', '')
        d['model_loaded'] = bool(h.get('model_loaded'))
        if h.get('model_error'):
            d['error'] = h['model_error']
        return d
    return fsm.probe_full(server, force=force)


def probe_all(force: bool = False) -> list:
    """并行探测所有候选机，返回 [(server, probe)]，保持注册顺序。

    直连模式下所有注册项其实指向同一个后端，只探一次，避免 N 次重复 /health。
    远端模式下委托 fsm.probe_all（并行 + TTL 缓存）。
    """
    if DIRECT_BASE:
        s = fsm.SERVER_DEFAULT
        return [(s, probe(s, force=True))]
    return fsm.probe_all(force=force)


def _pick_from(cands: list) -> tuple:
    """从 [(server, probe)] 里按分级规则挑一台。返回 (server|None, probe|None)。

    抽出来是为了让 CLI 的 `servers` 视图和生产选机走**同一套**判断 ——
    否则"命令说选 A、实际跑了 B"这种不一致最难查。

    分级（同 find_available_server 的文档）：
      1 常驻在跑+模型已加载 → 2 可达+有卡+模型就绪 → 3 可达+有卡 → 4 可达(含无卡)
    """
    if not cands:
        return None, None

    def pick(pred):
        for s, p in cands:
            if pred(p):
                return s, p
        return None, None

    s, p = pick(lambda p: p['resident'] and p['model_loaded'])
    if s:
        logger.info(f'🟢 选中 {p["name"]}（常驻在跑·模型已加载，零冷启动）')
        return s, p

    s, p = pick(lambda p: p['reachable'] and p['gpu_ok'] and p['model_ok'])
    if s:
        logger.info(f'🟢 选中 {p["name"]}（可达·有卡·模型就绪，待自动拉起常驻）')
        return s, p

    s, p = pick(lambda p: p['reachable'] and p['gpu_ok'])
    if s:
        logger.warning(f'🟡 选中 {p["name"]}（有卡但模型未就绪，拉起预计失败）')
        return s, p

    s, p = pick(lambda p: p['reachable'])
    if s:
        logger.warning(f'🟡 {p["name"]} 可达但无 GPU，交由 ensure_resident 报准确原因')
        return s, p

    return None, cands[0][1]


def find_available_server(force: bool = False) -> tuple:
    """挑一台「当前最该用」的服务器。返回 (server, probe)；全不可用返回 (None, probe)。

    分级选择（这是「换了机器 / 克隆到新机后自动接上」的核心）：

      1) 常驻在跑 + 模型已加载      —— 零冷启动，最好
      2) 可达 + 有卡 + 模型文件就绪 —— 可被 ensure_resident 自动拉起
      3) 可达 + 有卡                —— 模型没就绪，仍返回，让 ensure_resident 报准确原因
      4) 可达（即使无卡）           —— 保留旧行为：准确报出「需切带卡模式」
      都不满足 → None

    注意第 4 级：旧实现是「第一台可达就选」，无卡会在 ensure_resident 里得到
    「可达但无卡（需到 AutoDL 控制台切【带卡模式】）」这条很有用的报错。
    这里把无卡机器降到最低优先级，但**不剔除**，正是为了不丢这条诊断信息。
    """
    return _pick_from(probe_all(force=force))


def any_ready() -> bool:
    """至少一台「常驻在跑 + 模型已加载」（最严格口径：直接就能出图）。"""
    return any(p['resident'] and p['model_loaded'] for _s, p in probe_all())


def any_usable() -> bool:
    """至少一台「能干活」的机器：可达 + 有卡 + 模型文件就绪。

    刻意**不要求常驻服务已在跑**：新克隆 / 刚开机的机器上常驻还没起来，
    但 ensure_resident 会自动拉起。旧版拿「常驻已跑且模型已加载」当 waiting 恢复门，
    换机场景下会让任务一直卡在 waiting 死等。
    """
    return any(p['reachable'] and p['gpu_ok'] and p['model_ok'] for _s, p in probe_all())


# ══════════════════ 生命周期：幂等拉起常驻服务 ══════════════════
def tail_log(server: dict, lines: int = 30) -> str:
    ok, out = fsm.run(f'ssh -o ConnectTimeout={SSH_CONNECT_TIMEOUT} {server["alias"]} '
                      f'"tail -{lines} {server["remote_base"]}/{RESIDENT_LOG} 2>/dev/null"', 25)
    return out if ok and out else '(日志为空或读取失败)'


def upload_scripts(server: dict) -> tuple:
    """把服务端脚本 scp 到远端工作目录。返回 (ok, msg)。"""
    for local in (LOCAL_SERVER_PY, LOCAL_START_SH):
        if not local.exists():
            return False, f'本地缺少 {local}'
    alias, rb = server['alias'], server['remote_base']
    fsm.run(f'ssh -o ConnectTimeout={SSH_CONNECT_TIMEOUT} {alias} "mkdir -p {rb}"', 20)
    for local in (LOCAL_SERVER_PY, LOCAL_START_SH):
        ok, out = fsm.run(f'scp "{str(local).replace(chr(92), "/")}" {alias}:{rb}/', 60)
        if not ok:
            return False, f'上传 {local.name} 失败: {out[:160]}'
    return True, 'ok'


def ensure_resident(server: dict, p: dict = None, wait_ready: bool = True) -> dict:
    """幂等拉起远端常驻服务。返回 {ok, msg, kind}。

    只保证「进程起来 + HTTP 响应」；模型加载是后台异步的，
    由 wait_model_loaded 负责等。这样首次冷启动不会占住 SSH 会话。
    """
    p = p or probe(server)

    if p['resident'] and p.get('health', {}).get('status') in ('ok', 'loading') and not FORCE_SYNC:
        return {'ok': True, 'msg': '常驻服务已在运行', 'kind': ''}

    # 机器在不在
    if not p['reachable']:
        return {'ok': False, 'kind': 'server_down',
                'msg': f'服务器 {server["name"]} 不可达（SSH 不通，可能关机）'}
    # 带卡模式？（先于拉起检查，避免无卡时白起一次再报模型加载失败）
    # 探测结果里已经带了 gpu_ok / model_ok（fsm.probe_full 一次往返就查了），
    # 有就直接用 —— 省掉每张图 2 次 SSH 往返。直连模式下这两项标为 True（无从查，也不必查）。
    if p.get('gpu_ok') is not None:
        gpu_ok, ginfo = bool(p['gpu_ok']), p.get('gpu', '')
    else:
        gpu_ok, ginfo = fsm.gpu_ready(server)
    if not gpu_ok:
        return {'ok': False, 'kind': 'server_down',
                'msg': f'服务器 {server["name"]} 可达但无卡（需到 AutoDL 控制台切【带卡模式】）'}
    # 模型文件？（DOWNLOAD_DONE 标记）
    if p.get('model_ok') is not None:
        model_ok = bool(p['model_ok'])
    else:
        model_ok = fsm.model_ready(server)
    if not model_ok:
        return {'ok': False, 'kind': 'server_down',
                'msg': f'服务器 {server["name"]} 模型未就绪（缺 DOWNLOAD_DONE，见 dl_curl.sh）'}
    if not AUTOSTART:
        return {'ok': False, 'kind': 'server_down',
                'msg': '常驻服务未运行，且 FLUX_RESIDENT_AUTOSTART=0 不自动拉起'}

    ok, msg = upload_scripts(server)
    if not ok:
        return {'ok': False, 'kind': 'server_down', 'msg': msg}

    flag = '--force' if FORCE_SYNC else ''
    # ⚠️ FLUX_WORKDIR / FLUX_MODEL 必须按「这台服务器」的实际路径透传。
    #    start_resident.sh 里 WORKDIR/MODEL 是硬编码默认值，只传 PORT/SCREEN/PY 的话，
    #    克隆实例（换工作目录 / 换模型路径，正是「克隆到新机」场景）会被脚本拿去查
    #    错路径的模型 → 误报「模型未就绪」，且上传的 server_py 与脚本查找路径错位。
    #    脚本侧写法是 ${FLUX_WORKDIR:-默认}，所以只要传了就以本机的值为准。
    remote_base = server['remote_base']
    remote_model = server.get('remote_model') or ''
    # ⚠️ FLUX_OFFLOAD：默认**必须安全**，不能是「最快」。
    #    offload=none 把 31.2 GiB fp16 权重全塞进显存，32G 卡（36864/32760 MiB）
    #    + 推理激活值必然 OOM —— 2026-09-17 生产实测：自动拉起常驻服务后
    #    第一张图就 "CUDA out of memory, total capacity 31.48 GiB"，任务连续失败。
    #    优先级：环境变量 FLUX_OFFLOAD > 该机器条目的 offload 字段 > 安全默认 model。
    #    想追速度的机器（80G 卡等）在自己条目上写 "none" 即可，不必改这段逻辑。
    offload = (os.environ.get('FLUX_OFFLOAD') or server.get('offload') or 'model').strip()
    env_parts = [
        f'FLUX_OFFLOAD={offload}',
        f'FLUX_RESIDENT_PORT={REMOTE_PORT}',
        f'FLUX_RESIDENT_SCREEN={RESIDENT_SCREEN}',
        f'FLUX_RESIDENT_PY={shlex.quote(RESIDENT_PY)}',
        f'FLUX_WORKDIR={shlex.quote(remote_base)}',
    ]
    if remote_model:
        env_parts.append(f'FLUX_MODEL={shlex.quote(remote_model)}')
    if TOKEN:
        env_parts.append(f'FLUX_RESIDENT_TOKEN={shlex.quote(TOKEN)}')
    ssh_cmd = (f'cd {shlex.quote(remote_base)} && ' + ' '.join(env_parts)
               + f' bash start_resident.sh {flag}').strip()
    ok, out = fsm.run(f'ssh -o ConnectTimeout=15 {server["alias"]} {shlex.quote(ssh_cmd)}', 120)
    logger.info(f'拉起常驻服务({server["name"]}): {out[:400]}')
    if not ok:
        return {'ok': False, 'kind': 'server_down',
                'msg': f'拉起常驻服务失败: {out[:200]}\n{tail_log(server, 20)}'}

    # 等 HTTP 起来。必须 force=True：探测结果有 TTL 缓存（默认 20s），
    # 不复用缓存才可能在 2s 粒度上及时看到服务起来。
    deadline = time.time() + 40
    while time.time() < deadline:
        if probe(server, force=True)['resident']:
            return {'ok': True, 'msg': '常驻服务已拉起', 'kind': ''}
        time.sleep(2)
    return {'ok': False, 'kind': 'server_down',
            'msg': f'常驻服务 40s 内未响应 /health\n{tail_log(server, 25)}'}


def wait_model_loaded(server: dict, timeout: int = None, interval: float = 5) -> dict:
    """等模型加载完（只发生一次，之后常驻）。返回 health dict。"""
    deadline = time.time() + (timeout or WAIT_MODEL)
    t0 = time.time()
    last_log = 0.0
    while time.time() < deadline:
        h = _transport(server).get_json('/health', timeout=HEALTH_TIMEOUT)
        if h.get('model_loaded'):
            logger.info(f'⌛ 模型常驻就绪（{h.get("offload")}）用时 {time.time()-t0:.0f}s')
            return h
        if h.get('model_error'):
            raise TransportError(f"模型加载失败: {h['model_error']}", kind='failed')
        if time.time() - last_log > 30:
            last_log = time.time()
            logger.info(f'⌛ 等待模型加载… {time.time()-t0:.0f}s（首次冷启动，之后常驻不再等）')
        time.sleep(interval)
    raise TransportError(f'等待模型加载超时（{timeout or WAIT_MODEL}s），见远端 {RESIDENT_LOG}',
                         kind='failed')


# ══════════════════ 高层入口：生成一张 ══════════════════
def generate_via_resident(server: dict, prompt: str, dest=None, timeout: int = 1800,
                          wait_model: int = None, ref_image_b64: str = None,
                          **gen_kwargs) -> dict:
    """提交 → 等模型就绪 → 轮询完成 →（给了 dest 才）拉图。返回 status dict（含 path / seed）。

    `dest` 可省略（None）：`bench` 只关心推理耗时，不需要把 PNG 拉回本地 —— 白花时间，
    且这条 AutoDL 中转链路下行只有 ~3.6 KB/s，320KB 的图要 90s+，
    会把吞吐数据完全淹没在传输耗时里。旧版 `dest` 是必填位置参数，导致
    `bench` 一调用就 `TypeError`（该命令此前从未跑通过）。

    `ref_image_b64`（2026-09-18 新增）—— 给了它就走 **`POST /edit`**（图生图），
    不给就走 `POST /generate`（文生图）。两条路径除端点名与这一个字段外完全同构，
    所以共用本函数，不做成两份代码。

    参考图传**原样 base64**（可带 `data:` 前缀，服务端会剥）。不要在这里解码或
    重新编码 —— 服务端 `_save_ref_image` 已经做了严格的图片校验与体积卡口，
    客户端再动一次只会多一处口径漂移（这个项目在「体积换算」上已经栽过两次）。
    体积上限由**上游**兜底：`MAX_EDIT_BYTES=12MiB`(HTTP body) / `MAX_REF_BYTES=8MiB`
    (解码后)。超限时上游返回 400，本函数会把它转成 `TransportError(kind='failed')`
    → 任务标记 failed → 配额原路退还，不会让用户白扣一张。

    gen_kwargs 支持 width / height / steps / seed / negative_prompt / guidance_scale
    —— 这是旧链路（gen_flux.py 固定 768×1024 / steps 25、web 层只传 prompt）做不到的。
    """
    dest = Path(dest) if dest else None
    t = _transport(server)
    h = t.get_json('/health', timeout=max(HEALTH_TIMEOUT, 15))
    if h.get('model_error'):
        raise TransportError(f"模型不可用: {h['model_error']}", kind='failed')
    if not h.get('model_loaded'):
        wait_model_loaded(server, timeout=wait_model)

    body = {'prompt': prompt}
    body.update({k: v for k, v in gen_kwargs.items() if v not in (None, '')})
    if ref_image_b64:
        body['image'] = ref_image_b64
    endpoint = '/edit' if ref_image_b64 else '/generate'
    sub = t.post_json(endpoint, body, timeout=90)
    job_id = sub['job_id']
    logger.info(f'🎬 常驻任务 {job_id} 已提交（{endpoint}，队列深度 {sub.get("queue_depth")}）')

    # 自适应退避轮询：SSH 传输下每次轮询都是一次往返，固定 3s 太密
    interval, deadline, t0 = 1.5, time.time() + timeout, time.time()
    last = ''
    while time.time() < deadline:
        st = t.get_json(f'/status?job_id={urllib.parse.quote(job_id)}', timeout=40)
        if st['status'] != last:
            logger.info(f'   {job_id} → {st["status"]}')
            last = st['status']
        if st['status'] in TERMINAL:
            if st['status'] != 'done':
                raise TransportError(
                    f"生成未成功（{st['status']}）: {st.get('error') or ''}".strip(), kind='failed')
            if dest is not None:
                st['path'] = str(t.fetch_png(job_id, dest, server))
            st['resident_job_id'] = job_id
            st['total_elapsed'] = round(time.time() - t0, 1)
            return st
        time.sleep(interval)
        interval = min(interval * 1.5, 6.0)
    raise TransportError(f'生成超时（{timeout}s），远端任务 {job_id} 可能仍在跑', kind='failed')


# ══════════════════════════ 命令行 ══════════════════════════
def _server_by_name(name: str = None) -> dict:
    if not name:
        return fsm.SERVER_DEFAULT
    for s in fsm.FLUX_SERVERS:
        if name in (s.get('name'), s.get('alias')):
            return s
    raise SystemExit(f'未知服务器: {name}（可选 {[s["name"] for s in fsm.FLUX_SERVERS]}）')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    ap = argparse.ArgumentParser(description='FLUX 常驻服务客户端')
    ap.add_argument('--server', default=None, help='服务器名/别名（默认第一台）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('servers', help='列出候选机 + 标出会被选哪台')
    sub.add_parser('probe', help='同 servers（旧名，保留）')
    sub.add_parser('health', help='打印 /health')
    sub.add_parser('up', help='幂等拉起常驻服务')

    g = sub.add_parser('gen')
    g.add_argument('prompt')
    g.add_argument('--out', default='resident_out.png')
    g.add_argument('--width', type=int)
    g.add_argument('--height', type=int)
    g.add_argument('--steps', type=int)
    g.add_argument('--seed', type=int)
    g.add_argument('--negative', default='')
    g.add_argument('--timeout', type=int, default=1800)

    b = sub.add_parser('bench')
    b.add_argument('prompt')
    b.add_argument('--count', type=int, default=5)
    b.add_argument('--width', type=int)
    b.add_argument('--height', type=int)
    b.add_argument('--steps', type=int)

    args = ap.parse_args()

    try:
        if args.cmd in ('servers', 'probe'):
            cands = probe_all(force=True)
            chosen, _cp = _pick_from(cands)
            chosen_alias = chosen['alias'] if chosen else None
            src = (f'自动发现开（{fsm.FLUX_SERVER_ALIAS_GLOB}）'
                   if fsm.FLUX_SERVER_DISCOVER else '自动发现关')
            print(f'候选机 {len(cands)} 台 · {src} · 连线方式='
                  f'{"直连 " + DIRECT_BASE if DIRECT_BASE else "SSH"}')
            print(f'  {"":3s}{"NAME":11s}{"ALIAS":19s}{"可达":5s}{"带卡":5s}'
                  f'{"模型":5s}{"常驻":5s}{"已加载":7s}备注')
            for s, p in cands:
                mark = '▶' if s['alias'] == chosen_alias else ' '
                note = p.get('error') or ''
                if not p['reachable']:
                    note = note or 'SSH 不通'
                elif p['resident'] and p['model_loaded']:
                    note = '可直接出图'
                elif p['reachable'] and not p['gpu_ok']:
                    note = note or '无卡（需切带卡模式）'
                print(f'  {mark:3s}{s["name"]:11s}{s["alias"]:19s}'
                      f'{"✓" if p["reachable"] else "✗":5s}'
                      f'{"✓" if p["gpu_ok"] else "✗":5s}'
                      f'{"✓" if p["model_ok"] else "✗":5s}'
                      f'{"✓" if p["resident"] else "✗":5s}'
                      f'{"✓" if p["model_loaded"] else "✗":7s}'
                      f'{(p.get("gpu") or "")[:34]} {note}'.rstrip())
            print(f'\n▶ = 会被选用的机器: {chosen_alias}' if chosen_alias
                  else '\n❌ 无可用机器（全部不可达 / 无卡）')
            # 提示没被纳入的别名：克隆机换了命名时，一眼就能看出该调哪个开关
            if fsm.FLUX_SERVER_DISCOVER:
                used = {s['alias'] for s, _ in cands}
                skipped = [a for a in fsm.ssh_config_aliases() if a not in used]
                if skipped:
                    print(f'\n未纳入的 ~/.ssh/config 别名（要用就调 FLUX_SERVER_ALIAS_GLOB，'
                          f'当前={fsm.FLUX_SERVER_ALIAS_GLOB}）：')
                    print('  ' + ', '.join(skipped))
            return 0

        if args.cmd == 'health':
            print(json.dumps(_transport(_server_by_name(args.server)).get_json('/health'),
                             ensure_ascii=False, indent=2))
            return 0

        server, p = (find_available_server() if not args.server
                     else (_server_by_name(args.server), probe(_server_by_name(args.server))))
        if args.cmd == 'up':
            if not server:
                print('❌ 没有 SSH 可达的服务器')
                return 1
            r = ensure_resident(server, p)
            print(('✅ ' if r['ok'] else '❌ ') + r['msg'])
            return 0 if r['ok'] else 1

        if not server:
            print('❌ 没有 SSH 可达的服务器（关机 / SSH 不通）')
            return 1
        r = ensure_resident(server, p)
        if not r['ok']:
            print(f'❌ {r["msg"]}')
            return 1

        if args.cmd == 'gen':
            st = generate_via_resident(server, args.prompt, args.out, timeout=args.timeout,
                                       width=args.width, height=args.height, steps=args.steps,
                                       seed=args.seed, negative_prompt=args.negative)
            print(json.dumps({k: st.get(k) for k in ('resident_job_id', 'status', 'runtime',
                                                     'seed', 'total_elapsed')},
                             ensure_ascii=False))
            print(f'已保存: {st["path"]}')
            return 0

        if args.cmd == 'bench':
            times = []
            print(f'[bench] 连打 {args.count} 张（模型常驻，仅首张前加载一次）')
            for i in range(args.count):
                st = generate_via_resident(server, args.prompt, timeout=1800,
                                           width=args.width, height=args.height, steps=args.steps)
                times.append(st.get('runtime') or 0)
                print(f'  #{i+1} {st["resident_job_id"]} 推理 {st.get("runtime")}s '
                      f'端到端 {st.get("total_elapsed")}s', flush=True)
            avg = sum(times) / len(times) if times else 0
            print(f'[bench] 单张推理均值 {avg:.1f}s，合计 {sum(times):.1f}s')
            return 0
    except TransportError as e:
        print(f'❌ [{e.kind}] {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
