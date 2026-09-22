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
HEALTH_TIMEOUT = int(os.environ.get('FLUX_RESIDENT_HEALTH_TIMEOUT', '20'))
# ⚠️ SSH 往返的**固有开销**（建连 + 跳转机转发），不计入 HTTP 超时。
#    旧代码把 HTTP 超时（10s）直接当 ssh 整体超时传给 subprocess，于是
#    「ssh 建连慢一点」就被报成 TIMEOUT，而 curl 那边其实还没到 --max-time。
#    表现为验收脚本在 wait_model_loaded 第一步就崩（2026-09-20 flux5 两次必现）。
#    HTTP 语义（curl --max-time）与传输语义（subprocess timeout）必须分开算。
SSH_ROUNDTRIP_OVERHEAD = int(os.environ.get('FLUX_RESIDENT_SSH_OVERHEAD', '20'))

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

    调用一次 = 一次 ssh 往返。POST 的 JSON 走 **ssh 的 stdin**（`curl --data-binary @-`
    从 stdin 读 body），不再内联进命令行 —— 见下面 `_ssh` 的注释。
    """

    kind = 'ssh-curl'

    def __init__(self, server: dict, token: str = ''):
        self.server = server
        self.alias = server['alias']
        self.token = token

    def _curl_cmd(self, path: str, method: str = 'GET', timeout: int = 60) -> str:
        """只造 curl 命令；body 由 stdin 喂（`@-`），不出现在本方法返回的字符串里。"""
        url = f'http://127.0.0.1:{REMOTE_PORT}{path}'
        parts = ['curl', '-sS', '--max-time', str(max(int(timeout) - 5, 5))]
        if method == 'POST':
            parts += ['-X', 'POST', "-H 'Content-Type: application/json; charset=utf-8'",
                      '--data-binary', '@-']
        if self.token:
            parts.append(f"-H 'X-Auth-Token: {self.token}'")
        parts.append(shlex.quote(url))
        return ' '.join(parts)

    def _ssh(self, remote_cmd: str, timeout: int, stdin_data: str = None) -> str:
        """⚠️ 大 payload 只能走 stdin，不能进 argv（2026-09-20）。

        Windows 的 CreateProcess 命令行上限约 32K。图生图的 body 含参考图 base64
        （8MiB 图 → 约 11MB base64），旧写法 `echo <b64> | base64 -d | curl ...`
        把它整个塞进 ssh 的命令行 → `FileNotFoundError: [WinError 206]`
        → 上层归类成 SERVER_DOWN 一直重试 → 站点任务永远卡在「生成中，已等待」。
        现在命令行只留 curl（约 200 字符），body 由 ssh 转发 stdin 到远端 stdin。
        """
        # timeout 是 **HTTP 语义**（curl --max-time = timeout-5）；ssh 建连/转发的固有
        # 开销另加，否则「ssh 慢一点」会被误报成 TIMEOUT（见 SSH_ROUNDTRIP_OVERHEAD 注释）。
        ok, out = fsm.run(
            f'ssh -o ConnectTimeout={SSH_CONNECT_TIMEOUT} '
            f'{fsm.ssh_target(self.server)} {shlex.quote(remote_cmd)}',
            timeout + SSH_ROUNDTRIP_OVERHEAD, stdin_data=stdin_data)
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
        """body 直接以 UTF-8 文本喂 stdin（不额外 base64：省 33% 体积，
        且 `text=True` 的 subprocess 会按 utf-8 编码，中文提示词无损）。"""
        payload = json.dumps(body, ensure_ascii=False)
        out = self._ssh(self._curl_cmd(path, 'POST', timeout), timeout, stdin_data=payload)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise TransportError(f'返回体不是 JSON: {out[:200]}', kind='failed') from None

    def fetch_png(self, job_id: str, dest: Path, server: dict) -> Path:
        """SSH：常驻服务已把 PNG 落盘，直接 scp 拉回（沿用项目原有模式）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        remote = f'{server["remote_base"]}/resident_out/{job_id}.png'
        # ⚠️ 别急着缩这个超时：AutoDL 中转链路下行实测只有 ~3.6 KB/s，
        #    783KB 的 PNG 要 200s+。180s 是 2026-09-20 验收实测超时炸掉的值
        #    （推理 7s 成功、拉回 TIMEOUT → 任务被判失败）。300s 也只是勉强够用。
        ok, out = fsm.run(f'scp {fsm.scp_target(self.server)}:{remote} '
                          f'"{str(dest).replace(chr(92), "/")}"', 300)
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
        # ★ 2026-09-22：直连模式也必须带上能力字段。
        #   否则 need_caps/need_edit 过滤读不到 caps → 退化成读注册表
        #   supports_edit → 而 SERVER_DEFAULT 可能是**不支持该能力**的那台
        #   （实测：默认机是 flux1/dev，edit 任务被整台滤掉 → 返回 None →
        #    manager 静默走成 /generate → 5 项离线验收超时）。
        #   直连模式的诚实语义是「能力未知 → 退注册表」，但既然 /health 就给了
        #   capabilities，白拿不用是自找的失真。
        d.setdefault('caps', {})
        d.setdefault('capabilities_by_model', {})
        d.setdefault('models', [])
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
        if isinstance(h.get('capabilities'), dict) and h['capabilities']:
            d['caps'] = h['capabilities']
        # stub / 直连模式下 /health 就报当前模型 → 至少让 models 知道有它，
        # 否则 need_model 过滤在直连模式下永远「无清单」而静默不过滤。
        mid = h.get('model_id') or ''
        if mid:
            d['models'] = [mid]
            if d['caps']:
                d['capabilities_by_model'] = {mid: d['caps']}
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


def _pick_from(cands: list, need_edit: bool = False,
               need_model: str = None, need_caps: dict = None) -> tuple:
    """从 [(server, probe)] 里按分级规则挑一台。返回 (server|None, probe|None)。

    抽出来是为了让 CLI 的 `servers` 视图和生产选机走**同一套**判断 ——
    否则"命令说选 A、实际跑了 B"这种不一致最难查。

    分级（同 find_available_server 的文档）：
      1 常驻在跑+模型已加载 → 2 可达+有卡+模型就绪 → 3 可达+有卡 → 4 可达(含无卡)

    need_edit=True / need_caps={...} → **先按能力过滤**。
    为什么必须有这道闸（2026-09-20）：图生图要 pipeline 的 __call__ 接受 `image`，
    而 FLUX.1-dev 的 FluxPipeline 没有这个参数，edit 任务在 dev 机上必然报
    「当前模型不支持图生图」。全平台只有装了 klein/Qwen 的机器能跑编辑。

    ★ 2026-09-22 新增 need_caps（need_edit 保留为它的便捷写法）：
      单布尔位「支不支持图生图」已经不够用 —— Qwen 带来 multi_ref（多图）、
      mask_param（独立掩码入参）、transparent（透明）等**正交**能力。若每一项都加一个布尔
      参数，调用方要陆续加 6 个 kwarg、内部要 6 段平行过滤，必然漏。
      改成传一个能力字典，过滤逻辑只有一段：
        need_caps={'edit': True, 'multi_ref': True} → 只留两者都满足的机器。
      数据来源 = 探针从 GPU 机 `/models` 拿到的 `capabilities`（**权威**，
      因为它是 resident 按 model_index.json 的 _class_name 现算的），
      而不是注册表里手写的 `supports_edit`（那是给人看的冗余提示）。

      ⚠️ 兼容与降级（关键）：
        · 探针没返回 capabilities（旧版 resident / 直连模式）→ 退回到
          注册表 `supports_edit` 布尔位兜底；连它也没有 → **不过滤**。
        · 「谁都不满足」时**不硬挑**，返回 (None, ...)，把准确原因抛给上游。
          硬挑的后果是「提交成功 → 排队 → GPU 报错」，用户白等一整轮
          （2026-09-21 实测踩过，见下面 need_model 那段同样处理）。

    need_model=<id>（2026-09-21 新增）→ **先按「本机有没有这个模型文件」过滤**。
    这是「界面选模型」能真正生效的关键：dev 只在 flux1 上，klein 在 flux5/flux6 上，
    不按模型过滤就会把「要 dev」的活派给只有 klein 的机器 —— GPU 侧白名单会拒
    （400），但那时已经占了一次排队 + 一次 SSH，用户只看到「提交成功然后失败」。
    分级在过滤后的子集里照常生效。

    probe 里的 `models` 为空（旧版探针 / 直连模式）时**不过滤**：
    保持旧行为，让 GPU 侧白名单去做最终判断，而不是在这里误判成「没有机器」。
    """
    if not cands:
        return None, None

    # ★ 许可闸（2026-09-22 新增）：model 许可**不允许对外经营**的机器直接出局。
    #
    # 为什么必须在这里、而且必须在能力过滤之前：
    #   `commercial_ok` 此前**只写在 servers.json、只被一道门断言**，
    #   **运行时没有任何一处读它** —— 也就是说这个字段一直是「声明」而非「约束」。
    #   后果：Qwen（Qwen Research License，非商用）一旦开机就在候选池里，
    #   对外访客（朋友 / B 链客户）的任务可能被派到 flux7 上跑，
    #   而 flux7 的许可明确不允许这样做。这是**法律风险，不是质量偏好**。
    #
    # 判据取 server 字典的 commercial_ok（注册表声明）：
    #   · False  → 出局（明确不许商用）
    #   · True   → 保留
    #   · 缺字段 → 保留（**向后兼容**：老部署没这个字段，剔除会让全部机器消失）
    #
    # ⚠️ 为什么直连模式要跳过这道闸（2026-09-22 实测踩到，代价是一道门变红）：
    #   直连模式（FLUX_RESIDENT_BASE 有值）下 `probe_all` 返回的候选机是
    #   `fsm.SERVER_DEFAULT` —— 它**只是连接参数的载体**（base/token），
    #   并不代表"这就是 flux1 那台机器"。但 `SERVER_DEFAULT` 是从注册表第一台
    #   解析出来的，**带着 flux1 的 `commercial_ok: false` 标签**。
    #   于是本地部署 / 已开隧道 / 离线测试（假 resident）场景下，
    #   唯一那台"机器"会被误判成非商用 → 全部任务返回"无可用机器" → 超时。
    #   实测症状：`test_manager_edit_offline.py` 6 项红，日志刷
    #   「许可闸：所有候选机的模型均不允许对外经营」。
    #   语义上：**直连模式用户已显式指定了后端**，许可合规由他自己负责
    #   （他知道自己连的是哪台）—— 平台不该替他猜，更不该拿别处的标签拦他。
    if not need_model and not DIRECT_BASE:
        allowed = [c for c in cands
                   if c[0].get('commercial_ok') is not False]
        if allowed and len(allowed) != len(cands):
            # 用 id() 判断而不是 `c not in allowed`：dict 的 == 比较是**值比较**，
            # 两台机器配置相同时会被误判成「在 allowed 里」→ 漏报排除项。
            kept = {id(c) for c in allowed}
            dropped = [c[0].get('name') for c in cands if id(c) not in kept]
            logger.info(f'🔒 许可闸：排除非商用机器 {dropped}（对外自动选机不得用）')
            cands = allowed
        elif not allowed:
            # 全都不许商用 —— 不硬挑，交给调用方报准确原因
            logger.warning('🔴 许可闸：所有候选机的模型均不允许对外经营')
            return None, (cands[0][1] if cands else None)

    # need_edit 是 need_caps 的便捷写法，合并成一份能力要求（别写两段过滤）
    caps_req = dict(need_caps or {})
    if need_edit:
        caps_req.setdefault('edit', True)
    if caps_req:
        filtered = _filter_by_caps(cands, caps_req)
        if filtered is None:
            pass                          # 判断不了 → 保持不过滤（下游白名单兜底）
        elif not filtered:
            # ★ 判断了、且**确实一台都不满足** → 直接返回 None，不硬挑。
            #   与 need_model 的「谁都没有这个模型」同一处理：硬挑的后果是
            #   「提交成功 → 排队 → GPU 报错」，用户白等一整轮。
            #   ⚠️ 这里必须**返回**而不是「把 cands 置空再往下走」——
            #   置空会让下面所有分级 pick 都失败，最后 `cands[0][1]` 直接
            #   IndexError 崩掉（2026-09-22 实测就崩在这）。返回时带上原始
            #   首台的 probe，让调用方仍能显示「机器在线但能力不满足」。
            first = cands[0][1] if cands else None
            logger.warning(f'🔴 没有任何候选机满足能力要求 {caps_req}')
            return None, first
        else:
            cands = filtered

    if need_model:
        probed = [p for _s, p in cands if p.get('models')]
        if probed:                       # 至少一台能报清单 → 才敢按模型过滤
            has = [(s, p) for s, p in cands if need_model in (p.get('models') or [])]
            if has:
                cands = has
            else:
                # ★ 有清单、但谁都没有这个模型 → **返回 (None, ...)**，不硬挑。
                # 为什么不能「仍按原列表挑」（2026-09-21 实测踩到）：
                # 需要 edit+dev 时，edit 过滤先把 dev 机（supports_edit=false）剔掉，
                # 只剩 klein 机；若这里回退成"挑 klein"，用户会看到
                # 「提交成功 → 排队 → GPU 报模型不存在」，白等一整轮。
                # 直接说「没有机器装这个模型」才是准确答案。
                logger.warning(f'🔴 没有任何候选机装有所需模型 {need_model}'
                               f'（候选：{[(p.get("name"), p.get("models")) for _s, p in cands]}）')
                return None, (cands[0][1] if cands else None)
        else:
            logger.warning('🟡 探针未返回模型清单（旧版探针/直连模式），本次不按模型过滤')

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


def _filter_by_caps(cands: list, caps_req: dict) -> list:
    """按能力要求过滤候选机。返回过滤后的列表；**无法判定时返回 None**（=不过滤）。

    判定顺序（从权威到兜底）：
      1. 探针的 `caps`（resident /health 或 /models 报的 capabilities）—— 权威。
         每台机可能报多个模型的能力（capabilities_by_model），取**目标模型**的；
         取不到就用该机当前已加载模型的能力（`caps`）。
      2. 都没有 → 退到注册表 `supports_edit` 布尔位（只够回答 edit 一项）。
      3. 连布尔位也没有（老部署）→ 返回 None，让调用方维持旧行为，别在这里误判。

    ⚠️ 为什么 None 与 空列表 必须区分（与 active 三态同一道理）：
        None  = 「我判断不了」→ 上游**保持不过滤**，宁可让 GPU 侧白名单去拒。
        []    = 「我判断了，确实一台都没有」→ 上游应报「没有可用机器」。
        把两者混成一个 `if filtered:` 会让「确实没有」静默退回全量，
        于是用户的请求又被派给跑不了的机器 —— 正是这道闸要防的事。
    """
    def _caps_of(p: dict, model_id: str = None) -> dict:
        by_model = p.get('capabilities_by_model') or {}
        if model_id and model_id in by_model:
            return by_model[model_id] or {}
        return p.get('caps') or {}

    judged = []                      # [(server, probe, caps)]，caps 可能为 {}（无信息）
    for s, p in cands:
        c = _caps_of(p, model_id=None)
        if c:
            judged.append((s, p, c, 'resident'))
        else:
            # 兜底一：注册表的 supports_edit 布尔位（能答 edit 一项）
            se = s.get('supports_edit')
            if se is None:
                judged.append((s, p, {}, 'unknown'))
            else:
                judged.append((s, p, {'edit': bool(se)}, 'registry'))

    if all(j[3] == 'unknown' for j in judged):
        logger.warning('🟡 探针与注册表都没有能力信息，本次不按能力过滤')
        return None

    def _ok(caps: dict, src: str) -> bool:
        for k, want in caps_req.items():
            if k not in caps:
                # 该能力无从判定 → 保守放行（让 GPU 侧白名单做最终判断），
                # **不能**当成「不支持」剔掉，否则缺一项字段就等于整台机不可用。
                continue
            if bool(caps[k]) is not bool(want):
                return False
        return True

    out = [(s, p) for s, p, c, src in judged if _ok(c, src)]
    if not out:
        # 只在这里记「各机能力」的细节（外层报「空集」结论）——
        # 两处都报会让同一件事刷两行，排查时反而要多读一遍。
        logger.warning(f'🔴 能力过滤后无候选：要求 {caps_req}，'
                       f'各机能力 {[(p.get("name"), c) for _s, p, c, _x in judged]}')
    return out


def find_available_server(force: bool = False, need_edit: bool = False,
                          need_model: str = None, need_caps: dict = None) -> tuple:
    """挑一台「当前最该用」的服务器。返回 (server, probe)；全不可用返回 (None, probe)。

    need_edit=True：只在支持图生图的机器里挑（= need_caps={'edit': True} 的便捷写法）。
    need_caps={...}：按细粒度能力过滤，如 {'edit': True, 'multi_ref': True}。
                     Qwen 带来 multi_ref/mask_param/transparent 等正交能力后，
                     单布尔位不够用（见 _pick_from 的说明）。
    need_model=<id>：只在装了这个模型的机器里挑（界面选模型用），见 _pick_from。

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
    return _pick_from(probe_all(force=force), need_edit=need_edit,
                      need_model=need_model, need_caps=need_caps)


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
    ok, out = fsm.run(f'ssh -o ConnectTimeout={SSH_CONNECT_TIMEOUT} {fsm.ssh_target(server)} '
                      f'"tail -{lines} {server["remote_base"]}/{RESIDENT_LOG} 2>/dev/null"', 25)
    return out if ok and out else '(日志为空或读取失败)'


def upload_scripts(server: dict) -> tuple:
    """把服务端脚本 scp 到远端工作目录。返回 (ok, msg)。"""
    for local in (LOCAL_SERVER_PY, LOCAL_START_SH):
        if not local.exists():
            return False, f'本地缺少 {local}'
    rb = server['remote_base']
    fsm.run(f'ssh -o ConnectTimeout={SSH_CONNECT_TIMEOUT} {fsm.ssh_target(server)} '
            f'"mkdir -p {rb}"', 20)
    for local in (LOCAL_SERVER_PY, LOCAL_START_SH):
        # ⚠️ 选项必须在文件名**之前**：写成 `scp "<file>" <opts> user@host:` 时
        #    scp 的 getopt 在遇到第一个非选项参数后就停止解析 → -i / accept-new /
        #    BatchMode 全失效 → "Host key verification failed"（2026-09-20 flux5 实测）。
        #    统一走 fsm.scp_upload_cmd()，别再手写 scp 命令。
        ok, out = fsm.run(fsm.scp_upload_cmd(server, local, rb), 60)
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
    ok, out = fsm.run(f'ssh -o ConnectTimeout=15 {fsm.ssh_target(server)} '
                      f'{shlex.quote(ssh_cmd)}', 120)
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
    # 把模型记进日志：出图与预期不符时，第一个要回答的问题就是「这张用的是哪个模型」。
    # 不记的话只能靠时间戳去 /models 的历史里猜，而换模型是随时会发生的。
    _m = body.get('model')
    logger.info(f'🎬 常驻任务 {job_id} 已提交（{endpoint}'
                f'{f"，模型 {_m}" if _m else ""}，队列深度 {sub.get("queue_depth")}）')

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
            print(f'  {"":3s}{"NAME":9s}{"连接目标":34s}{"来源":7s}{"可达":5s}{"带卡":5s}'
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
                src = '注册表' if s.get('from_registry') else 'ssh配置'
                tgt = (f'{s.get("host")}:{s.get("port")}' if s.get('host')
                       else (s.get('alias') or ''))
                print(f'  {mark:3s}{s["name"]:9s}{tgt[:33]:34s}{src:7s}'
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
