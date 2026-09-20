#!/usr/bin/env python3
"""
FLUX 服务器管理器（对标转录bot orchestrator 的服务器管理模式）
- 监控小红书产线写入的配图任务队列（异步）
- 需要时飞书通知 owner 开机/切带卡
- 服务器可达时自动拉起生成服务（start_gen.sh，幂等）
- 生成完成 → 拉回图片 → 替换 Obsidian 稿子 <!--IMG:N--> 占位符 → 飞书通知

多服务器支持（v2.0，2026-09-16 起支持自动发现）：
- FLUX_SERVERS 注册表维护多台 flux 服务器
- 自动发现：扫 ~/.ssh/config，把匹配 FLUX_SERVER_ALIAS_GLOB 的别名自动登记为候选机
  —— 克隆实例到新机后，只要有一条 Host 别名就自动带上，不用改代码
- probe_full() 一次 SSH 往返拿齐「可达 / 带卡 / 模型文件 / 常驻服务」四态（选机用）
- SSH 操作全部接受 server 参数（None = 默认机；默认机 = 候选机第一台 flux1，
  可用 FLUX_DEFAULT_SERVER 覆盖，向后兼容）
- find_ready_server() 返回第一台可达+带卡+模型就绪的服务器

生成路径（FLUX_GEN_MODE，2026-09-16 起）：
- resident（默认）  走 manager/flux_resident_client.py + server/flux_resident_server.py，
                    模型加载一次常驻显存，每张图只做推理；process_job 逐张提交
- legacy            走 start_gen.sh + gen_flux.py 的冷启动链路（保留作逃生口/回退）

用法:
  python flux_server_manager.py                 # 常驻 daemon
  python flux_server_manager.py --once          # 处理一次队列即退（测试用）
  python flux_server_manager.py --interval 60   # 检测间隔(秒)
"""
import os
import sys
import json
import time
import shlex
import shutil
import fnmatch
import logging
import threading
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

# 确保项目根在 sys.path，才能 import manager.*
BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager.feishu_notify import notify_owner, _load_env
_load_env()   # 同 flux_queue：先把 manager/.env 灌进环境，模块级配置才读得到
JOB_DIR = Path(os.environ.get('FLUX_JOB_DIR',
    r'E:\ObsidianHouse\xiaohongshu-workspace\data\flux_jobs'))
DONE_DIR = JOB_DIR / '_done'
OBSIDIAN_IMAGES = Path(os.environ.get('FLUX_OBSIDIAN_IMAGES',
    r'E:\ObsidianHouse\ObsidW\02 Projects项目\hongshu\02-稿子\images\flux_out'))

# ═══════════════ 多服务器注册表（含自动发现） ═══════════════
# 每台 = {name, alias(~/.ssh/config), remote_base(工作目录), remote_model(模型路径)}
# 优先级：env FLUX_SERVERS_JSON（显式，完全接管） > 显式默认机 + 自动发现
#
# 自动发现解决的实际问题：用户有时换到有卡的机器，或把整个实例克隆到新服务器。
# 克隆实例与原机是同布局（同 remote_base / remote_model），只差一个 SSH 别名，
# 所以只要别名能匹配 FLUX_SERVER_ALIAS_GLOB，就自动成为候选机 —— 不用改代码。
_DEFAULT_SERVERS = [
    {"name": "flux1", "alias": "autodl-flux",  "remote_base": "/root/autodl-tmp/flux-t2i", "remote_model": "/root/autodl-tmp/models/FLUX.1-dev", "offload": "model"},
    {"name": "flux2", "alias": "autodl-flux2", "remote_base": "/root/autodl-tmp/flux-t2i", "remote_model": "/root/autodl-tmp/models/FLUX.1-dev", "offload": "model"},
    # flux3 = 2026-09-16 从 AutoDL 平台克隆出的实例（当日 flux1/flux2 无卡）。
    # 克隆实例与原机同布局，故 remote_base / remote_model 沿用同一组默认路径。
    # alias 必须与 ~/.ssh/config 里的 Host 名逐字一致；换了命名就改这一行，
    # 或走 FLUX_SERVER_ALIAS_GLOB 自动发现（见下方 discover_servers）。
    #
    # offload：拉起常驻服务时透传给服务端的显存档位（none|model|sequential）。
    #   ⚠️ 这几台都是 32G 卡（RTX 4080 SUPER 32760 MiB），fp16 权重约 31.2 GiB，
    #   offload=none（全程显存）**必然 OOM** —— 2026-09-17 实测：自动拉起后
    #   "CUDA out of memory, total capacity 31.48 GiB" 每张图都失败。
    #   所以这里显式声明 model。有更大显存的机器（如 80G）想追速度，在它的
    #   条目上改回 "none" 即可，不用动代码逻辑。
    {"name": "flux3", "alias": "autodl-flux3", "remote_base": "/root/autodl-tmp/flux-t2i", "remote_model": "/root/autodl-tmp/models/FLUX.1-dev", "offload": "model"},
]

FLUX_SERVER_DISCOVER = (os.environ.get('FLUX_SERVER_DISCOVER', '1').strip() != '0')
# 支持逗号分隔多模式，例如 "autodl-flux*,autodl-clone-gpu"
FLUX_SERVER_ALIAS_GLOB = os.environ.get('FLUX_SERVER_ALIAS_GLOB', 'autodl-flux*')
# 克隆实例沿用同布局，路径用这两个兜底
FLUX_REMOTE_BASE = os.environ.get('FLUX_REMOTE_BASE', '/root/autodl-tmp/flux-t2i')
FLUX_REMOTE_MODEL = os.environ.get('FLUX_REMOTE_MODEL', '/root/autodl-tmp/models/FLUX.1-dev')


# ═══════════════ 仓库内机器注册表（manager/servers.json） ═══════════════
# 为什么要有它（2026-09-20 事故）：候选机原本只从 ~/.ssh/config 匹配
# autodl-flux* 别名「自动发现」，而那个文件在仓库**外**、不在版本控制里。
# 克隆实例到新服务器后忘了加 Host 条目 → manager 永远看不见那台机器 →
# 站点提交的任务进 waiting 池死等（当天卡了 8 分钟无人处理）。
# 现在注册表进仓库（可评审、换人换 AI 都看得见），且支持 host/port/user
# **直连**，不再依赖 ~/.ssh/config。
REGISTRY_PATH = BASE_DIR / 'manager' / 'servers.json'


def load_registry(path=None) -> list:
    """读 manager/servers.json。文件不存在 / 内容写坏 → 返回 []（回退旧逻辑，绝不因此崩）。"""
    p = Path(path) if path else REGISTRY_PATH
    try:
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:                      # noqa: BLE001 注册表坏了不能拖垮整个服务
        logging.getLogger('flux_manager').warning(f'读取服务器注册表 {p} 失败: {e}')
        return []
    servers = data.get('servers') if isinstance(data, dict) else data
    if not isinstance(servers, list):
        return []
    out = []
    for s in servers:
        if not isinstance(s, dict) or not s.get('name'):
            continue
        if s.get('enabled') is False:
            continue                       # 已释放 / 停用的机器：保留记录做留痕，但不探活
        item = dict(s)
        # 直连条目没有 alias，兜一个给缓存 key / 日志 / jobs.server 用
        item.setdefault('alias', item.get('name'))
        item['from_registry'] = True
        out.append(item)
    return out


def _ssh_target_from(host: str, port, user: str, ident: str) -> str:
    parts = []
    ident = (ident or '').strip()
    if ident:
        # 命令由 bash -lc 执行，`~` 会展开；只有路径含空格才加引号
        # （ssh.exe 是原生程序，不解析单引号，无谓加引号反而会连不上）
        parts.append(f'-o IdentitiesOnly=yes -i {ident if " " not in ident else chr(34) + ident + chr(34)}')
    parts.append(f'-p {int(port) if str(port).strip().isdigit() else 22}')
    parts.append(f'{user or "root"}@{host}')
    return ' '.join(parts)


def ssh_target(server: dict) -> str:
    """把 server 解析成 ssh 的**连接目标**片段（不含 'ssh' 本身）。

    有 host → 直连 `-i <key> -p <port> <user>@<host>`（不查 ~/.ssh/config）
    无 host → 回退旧行为 `<alias>`
    """
    host = (server.get('host') or '').strip()
    if not host:
        return server.get('alias') or server.get('name') or ''
    return _ssh_target_from(host, server.get('port') or 22,
                            server.get('user') or 'root', server.get('identity_file') or '')


def scp_target(server: dict) -> str:
    """同 ssh_target，但 scp 的端口是**大写 -P**（scp 的 -p 是「保留时间戳」，语义完全不同）。"""
    host = (server.get('host') or '').strip()
    if not host:
        return server.get('alias') or server.get('name') or ''
    return _ssh_target_from(host, server.get('port') or 22,
                            server.get('user') or 'root',
                            server.get('identity_file') or '').replace(' -p ', ' -P ', 1)


def ssh_config_aliases() -> list:
    """从 ~/.ssh/config 读出所有 Host 别名（跳过含通配符的模板项）。"""
    cfg = Path(os.path.expanduser('~')) / '.ssh' / 'config'
    if not cfg.exists():
        return []
    out = []
    try:
        for line in cfg.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line[:4].lower() == 'host' and (len(line) == 4 or line[4] in ' \t'):
                for a in line.split()[1:]:
                    if any(c in a for c in '*?!'):
                        continue            # Host * / 模板项，不是真实主机
                    out.append(a)
    except Exception as e:
        # 用局部取 logger：本函数在模块级（FLUX_SERVERS = _load_servers()）就会被调用，
        # 那时模块的 log 还没定义，直接引用会 NameError
        logging.getLogger('flux_manager').warning(f'解析 ~/.ssh/config 失败: {e}')
    return out


def discover_servers() -> list:
    """候选机 = 显式默认机 + ~/.ssh/config 中匹配 FLUX_SERVER_ALIAS_GLOB 的别名。

    显式注册的别名优先（保留 flux1/flux2 的稳定顺序与名字），
    自动发现的机器 name 直接取别名，并打 discovered=True 便于排查。
    """
    servers = [dict(s) for s in _DEFAULT_SERVERS]
    seen = {s.get('alias') for s in servers}
    if not FLUX_SERVER_DISCOVER:
        return servers
    pats = [p.strip() for p in FLUX_SERVER_ALIAS_GLOB.split(',') if p.strip()]
    for alias in ssh_config_aliases():
        if alias in seen:
            continue
        if pats and not any(fnmatch.fnmatchcase(alias, p) for p in pats):
            continue
        servers.append({'name': alias, 'alias': alias,
                        'remote_base': FLUX_REMOTE_BASE,
                        'remote_model': FLUX_REMOTE_MODEL,
                        'discovered': True})
        seen.add(alias)
    return servers


def registry_known_names(path=None) -> set:
    """注册表里**所有**条目的 name/alias，含 enabled:false 的。

    为什么需要它（2026-09-20）：
      `enabled:false` 原本只让条目不进候选，但 `~/.ssh/config` 里若还有同名 alias
      （如 autodl-flux2），自动发现会**把它又塞回候选** —— 已释放的机器照样被探活，
      每次轮询白等 SSH 超时，表现为「任务一直不推进」。
      所以去重集合必须用「注册表里出现过的名字全集」，而不是「生效条目」。
    """
    p = Path(path) if path else REGISTRY_PATH
    try:
        if not p.exists():
            return set()
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:                           # noqa: BLE001
        return set()
    servers = data.get('servers') if isinstance(data, dict) else data
    if not isinstance(servers, list):
        return set()
    out = set()
    for s in servers:
        if not isinstance(s, dict):
            continue
        if s.get('name'):
            out.add(s['name'])
        if s.get('alias'):
            out.add(s['alias'])
    return out


def _load_servers() -> list:
    """优先级：FLUX_SERVERS_JSON（完全接管） > manager/servers.json（注册表）
    > 代码内 _DEFAULT_SERVERS + ~/.ssh/config 自动发现（旧行为，兜底）。

    注册表存在时接管，但 ssh config 里「同名之外」的匹配别名仍会追加进来，
    保证老机器上原来能用的别名不会因为加了注册表而失效。
    """
    raw = os.environ.get('FLUX_SERVERS_JSON')
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list) and loaded:
                return loaded
        except Exception:                  # noqa: BLE001
            pass
    reg = load_registry()
    if reg:
        servers = list(reg)
        # ⚠️ 用「注册表里出现过的名字全集」去重，而不是「生效条目」：
        #    否则 enabled:false 的机器会被 ssh config 自动发现重新塞回候选（已实测）。
        seen = {(s.get('alias') or '') for s in servers} \
            | {(s.get('name') or '') for s in servers} | registry_known_names()
        for s in discover_servers():
            if (s.get('alias') or '') in seen or (s.get('name') or '') in seen:
                continue
            servers.append(s)
        return servers
    return discover_servers()


FLUX_SERVERS = _load_servers()


def _resolve_default(servers: list) -> dict:
    """默认机 = FLUX_DEFAULT_SERVER 指定的那台；未指定则候选机第一台（向后兼容）。

    为什么需要这个开关：默认机被两条路径使用 ——
      ① 小红书产线（本模块 process_job / health_check_pending）
      ② 不带 --server 的 CLI 调用（flux_resident_client._server_by_name(None)）
    当候选机第一台「开机但没卡」（GPU 被占 / AutoDL 切成无卡模式）时，
    希望不改代码就能把默认机临时指到当前有卡的机器：FLUX_DEFAULT_SERVER=flux3。
    注意它只改「默认值」，不改变候选机集合，也不影响分级自动选机
    （A 链走的是 find_ready_server，本来就每单自己挑）。
    """
    want = (os.environ.get('FLUX_DEFAULT_SERVER') or '').strip()
    if want:
        for s in servers:
            if want in (s.get('name'), s.get('alias')):
                return s
        logging.getLogger('flux_manager').warning(
            f'FLUX_DEFAULT_SERVER={want!r} 不在候选机 '
            f'{[s["name"] for s in servers]} 中，回退第一台')
    return servers[0]


SERVER_DEFAULT = _resolve_default(FLUX_SERVERS)

# 探测结果缓存：SSH 往返是这个模块最贵的操作，而选机逻辑每张图都会调用。
# FLUX_PROBE_TTL=0 可关闭（调试/测试时用）。
_PROBE_TTL = float(os.environ.get('FLUX_PROBE_TTL', '20'))
_probe_cache = {}                      # alias -> (ts, probe_dict)
_probe_cache_lock = threading.Lock()


def _get_server(server=None) -> dict:
    """把 server 参数解析成注册表条目（dict / name / alias / None=默认）。"""
    if server is None:
        return SERVER_DEFAULT
    if isinstance(server, dict):
        return server
    for s in FLUX_SERVERS:
        if s.get('name') == server or s.get('alias') == server:
            return s
    return SERVER_DEFAULT


# 旧常量 = 默认服务器（候选机第一台；FLUX_DEFAULT_SERVER 可覆盖）。
# 向后兼容：小红书产线 process_job 等仍走默认机。
SSH_ALIAS = SERVER_DEFAULT['alias']
REMOTE_BASE = SERVER_DEFAULT['remote_base']
REMOTE_MODEL = SERVER_DEFAULT['remote_model']
LOCAL_GEN = BASE_DIR / 'server' / 'gen_flux.py'

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(BASE_DIR / 'manager' / 'flux_manager.log', encoding='utf-8'),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger('flux_manager')

# 生成路径：resident（常驻服务，默认）/ legacy（每张冷启动）
GEN_MODE = (os.environ.get('FLUX_GEN_MODE') or 'resident').strip().lower()
if GEN_MODE not in ('resident', 'legacy'):
    log.warning(f'未知 FLUX_GEN_MODE={GEN_MODE!r}，回退为 resident')
    GEN_MODE = 'resident'


# ═══════════════ SSH / 服务器操作 ═══════════════

# ── bash 定位 ──────────────────────────────────────────────────────────
# 为什么需要它（2026-09-17 踩坑，事故级）：
#   Git for Windows 安装时默认只把 <Git>\cmd 写进 PATH，**那个目录里只有 git.exe**；
#   bash.exe 在 <Git>\bin（和 <Git>\usr\bin），不在 PATH。
#   run() 原先直接写死 subprocess.run(['bash', '-lc', cmd])，于是：
#     · 从 Git Bash 里跑服务 → PATH 有 /usr/bin → bash 找得到 → 一切正常
#     · 从资源管理器双击 .bat 跑服务 → PowerShell 环境 PATH 无 bash → FileNotFoundError
#       → 被 run() 的 except 吞成 (False, '...') → 每次探测 ~0.07s 就"不可达"
#       → 三台机器全被判「SSH 不通（可能关机）」→ waiting 池永久卡住、健康监控误报。
#   教训：**「找不到本机工具」绝不能退化成「远程服务器不可达」** —— 会把运维引向错误方向。
_BASH_EXE = None      # None=尚未探测；'' = 已确认没有；其它 = 可用路径


def find_bash():
    """定位可用的 bash（结果缓存）。找不到返回 None。"""
    global _BASH_EXE
    if _BASH_EXE is not None:
        return _BASH_EXE or None
    cands = []
    w = shutil.which('bash')
    if w:
        cands.append(w)
    if os.name == 'nt':
        g = shutil.which('git')                      # 从 <Git>\cmd\git.exe 反推 Git 根
        if g:
            root = Path(g).resolve().parent.parent
            cands += [root / 'bin' / 'bash.exe', root / 'usr' / 'bin' / 'bash.exe']
        for p in (r'C:\Program Files\Git\bin\bash.exe',
                  r'C:\Program Files\Git\usr\bin\bash.exe',
                  r'C:\Program Files (x86)\Git\bin\bash.exe',
                  r'C:\Program Files (x86)\Git\usr\bin\bash.exe'):
            cands.append(Path(p))
    for c in cands:
        try:
            if Path(c).is_file():
                _BASH_EXE = str(c)
                if not w or str(c) != str(w):
                    log.info(f'🔧 bash 不在 PATH，改按 Git 安装位置定位: {c}')
                return _BASH_EXE
        except Exception:
            pass
    _BASH_EXE = ''
    log.error('❌ 本机未找到 bash —— ssh/scp 全部无法执行，'
              '所有服务器都会被判「不可达」。请安装 Git for Windows，'
              '或把 <Git>\\bin 加入 PATH。')
    return None


def run(cmd, timeout=30):
    """执行命令。用 bash -lc（避免 Windows cmd 解析管道/引号）+ UTF-8 解码（避免 GBK 解码中文失败）。

    与旧版的差别：
      1. bash 走 find_bash() 定位，不再假定它在 PATH 里；
      2. 失败时把 **stderr** 带回来（旧版只取 stdout，ssh 的报错全在 stderr，
         于是所有失败都退化成一句'SSH 不通'，真因不可见）。
    """
    bash = find_bash()
    if not bash:
        return False, ('NO_BASH: 本机未找到 bash，无法执行 ssh/scp'
                       '（装 Git for Windows 或把 <Git>\\bin 加入 PATH）')
    try:
        r = subprocess.run([bash, '-lc', cmd], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or '').strip()
            return False, err[:400] or f'退出码 {r.returncode}'
        return True, (r.stdout or '').strip()
    except subprocess.TimeoutExpired:
        return False, 'TIMEOUT'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def server_reachable(server=None) -> bool:
    s = _get_server(server)
    ok, _ = run(f'ssh -o ConnectTimeout=8 -o BatchMode=yes {ssh_target(s)} echo ok', 15)
    return ok


def gpu_ready(server=None) -> tuple:
    s = _get_server(server)
    ok, out = run(f'ssh -o ConnectTimeout=8 {ssh_target(s)} '
                  "'nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1'", 15)
    return ok and 'NVIDIA' in out, out


def model_ready(server=None) -> bool:
    s = _get_server(server)
    ok, _ = run(f'ssh -o ConnectTimeout=8 {ssh_target(s)} '
                f"'test -f {s['remote_model']}/DOWNLOAD_DONE && echo READY'", 15)
    return ok


def gen_running(server=None) -> bool:
    s = _get_server(server)
    # 先 screen -wipe 清僵尸会话，避免 Dead 会话被 grep -c 误判为"运行中"（否则崩溃后残留会挡住重启）
    ok, out = run(f'ssh -o ConnectTimeout=8 {ssh_target(s)} '
                  "'screen -wipe 2>/dev/null; screen -ls 2>/dev/null | grep -c fluxgen'", 15)
    return ok and '1' in out


def upload_prompts(job: dict, server=None) -> bool:
    """把 job 的提示词写成 prompts.json 并上传服务器"""
    s = _get_server(server)
    notes = [{
        'note': job['note_title'],
        'images': [{"key": f"P{i+1}", "prompt": img['prompt']}
                   for i, img in enumerate(job['images'])],
    }]
    prompts_path = BASE_DIR / 'manager' / 'tmp_prompts.json'
    prompts_path.write_text(json.dumps({'notes': notes}, ensure_ascii=False), encoding='utf-8')
    local_p = str(prompts_path).replace('\\', '/')
    local_g = str(LOCAL_GEN).replace('\\', '/')
    ok1, _ = run(f'scp {local_p} {scp_target(s)}:{s["remote_base"]}/prompts.json', 30)
    ok2, _ = run(f'scp {local_g} {scp_target(s)}:{s["remote_base"]}/gen_flux.py', 30)
    return ok1 and ok2


def start_generation(server=None) -> bool:
    s = _get_server(server)
    ok, out = run(f'ssh -o ConnectTimeout=15 {ssh_target(s)} '
                  f'bash {s["remote_base"]}/start_gen.sh', 60)
    log.info(out)
    return ok


def wait_generation(n_images: int, server=None, timeout_sec=3600) -> bool:
    """轮询直到生成完成（所有图片出现或 gen.log 标完成）"""
    s = _get_server(server)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(20)
        ok, out = run(f'ssh -o ConnectTimeout=8 {ssh_target(s)} '
                      f'find {s["remote_base"]}/out -name "*.png" | wc -l', 15)
        if ok and out.strip().isdigit() and int(out.strip()) >= n_images:
            log.info(f'✅ 生成完成: {out.strip()}/{n_images} 张 ({s["name"]})')
            return True
        # 检查是否报错
        ok2, err = run(f'ssh -o ConnectTimeout=8 {ssh_target(s)} '
                       f'tail -5 {s["remote_base"]}/gen.log 2>/dev/null | tr "\\r" "\\n" | grep -E "❌|Error|Traceback" | tail -1', 15)
        if ok2 and err:
            log.warning(f'⚠️  生成疑似报错: {err[:120]}')
    log.warning('⚠️  生成超时')
    return False


def pull_images(job: dict, server=None) -> str:
    """把生成图拉回本地 02-稿子/images/flux_out/<batch>/，返回目录"""
    s = _get_server(server)
    batch = job['job_id']
    dest = OBSIDIAN_IMAGES / batch
    dest.mkdir(parents=True, exist_ok=True)
    dest_posix = str(dest).replace('\\', '/')
    ok, _ = run(f'scp -r {scp_target(s)}:{s["remote_base"]}/out/. "{dest_posix}" 2>/dev/null', 120)
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


# ═══════════════ 多服务器探活 / 选择 ═══════════════

def probe(server=None) -> dict:
    """探测单台服务器完整状态：reachable / gpu_ok / gpu / model_ok。

    注意 gpu_ready() 只调一次 —— 原实现用 [0]/[1] 各调一次，
    等于每台服务器多跑一次 nvidia-smi 的 SSH 往返。
    """
    s = _get_server(server)
    gpu_ok, gpu = gpu_ready(s)
    return {
        'name': s['name'],
        'alias': s['alias'],
        'reachable': server_reachable(s),
        'gpu_ok': gpu_ok,
        'gpu': gpu,
        'model_ok': model_ready(s),
    }


def probe_full(server=None, force: bool = False) -> dict:
    """一次 SSH 往返拿齐四态：可达 / 带卡 / 模型文件 / 常驻服务。**选机专用**。

    为什么合成一条命令：原选机路径每台要 3~4 次 SSH 往返
    （echo + nvidia-smi + test -f + curl），候选机一多就按台数线性放大，
    且全关机时每台还要各等一个 ConnectTimeout。合成后每台固定 1 次往返。

    结果按 FLUX_PROBE_TTL 秒缓存（默认 20，设 0 关闭）。选机逻辑每张图都会调用，
    不缓存的话候选机一多就是每张图 N 次 SSH。代价：机器刚开机最多晚 TTL 秒被认出来，
    需要立刻刷新就传 force=True。

    输出用标记行切分：REACH / <GPU 名> / MODEL_OK|MODEL_MISSING / <health JSON>|HEALTH_FAIL
    """
    s = _get_server(server)
    alias = s['alias']
    if not force and _PROBE_TTL > 0:
        with _probe_cache_lock:
            hit = _probe_cache.get(alias)
        if hit and (time.time() - hit[0]) < _PROBE_TTL:
            return hit[1]

    port = int(os.environ.get('FLUX_RESIDENT_PORT', '9630'))
    remote = (f"echo REACH; "
              f"nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1; "
              f"test -f {s['remote_model']}/DOWNLOAD_DONE && echo MODEL_OK || echo MODEL_MISSING; "
              f"curl -s -m 5 http://127.0.0.1:{port}/health || echo HEALTH_FAIL")
    d = {'name': s.get('name'), 'alias': alias, 'reachable': False,
         'gpu_ok': False, 'gpu': '', 'model_ok': False,
         'resident': False, 'model_loaded': False, 'status': '',
         'health': None, 'error': ''}
    ok, out = run(f'ssh -o ConnectTimeout=8 -o BatchMode=yes {ssh_target(s)} '
                  f'{shlex.quote(remote)}', 20)
    if not ok:
        reason = (out or '').strip()[:160]
        if 'NO_BASH' in reason:
            d['error'] = reason            # 本机缺 bash：必须一眼看出，不能伪装成"远程关机"
        elif reason == 'TIMEOUT':
            d['error'] = 'SSH 超时（连接 8s 无响应）'
        elif reason:
            d['error'] = f'SSH 不通（{reason}）'
        else:
            d['error'] = 'SSH 不通（可能关机 / 别名未在 ~/.ssh/config 中）'
    else:
        lines = [ln.strip() for ln in (out or '').splitlines()]
        if 'REACH' not in lines:
            d['error'] = (out or '')[:160] or 'SSH 无输出'
        else:
            d['reachable'] = True
            marks = {'REACH', 'MODEL_OK', 'MODEL_MISSING', 'HEALTH_FAIL'}
            for ln in lines[1:]:                  # 跳过 REACH 行；GPU 名是第 2 行
                if ln in marks or ln.startswith('{'):
                    continue
                d['gpu'] = ln[:80]
                break
            d['gpu_ok'] = bool(d['gpu']) and 'NVIDIA' in d['gpu']
            d['model_ok'] = 'MODEL_OK' in lines
            for ln in reversed(lines):            # /health 的 JSON 落在最后一段
                if not (ln.startswith('{') and ln.endswith('}')):
                    continue
                try:
                    h = json.loads(ln)
                except Exception:
                    break
                d['health'] = h
                d['resident'] = True
                d['status'] = h.get('status', '')
                d['model_loaded'] = bool(h.get('model_loaded'))
                if h.get('model_error'):
                    d['error'] = h['model_error']
                break
    if _PROBE_TTL > 0:
        with _probe_cache_lock:
            _probe_cache[alias] = (time.time(), d)
    return d


def probe_all(servers=None, force: bool = False) -> list:
    """并行探测所有候选机，返回 [(server, probe), ...]，保持注册顺序。

    并行是关键：N 台全关机时，串行要 N × ConnectTimeout(8s)；
    并行后总耗时 ≈ 1 个 timeout。
    """
    servers = list(servers if servers is not None else FLUX_SERVERS)
    if not servers:
        return []
    if len(servers) == 1:
        return [(servers[0], probe_full(servers[0], force=force))]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(servers), 8)) as ex:
        probes = list(ex.map(lambda s: probe_full(s, force=force), servers))
    return list(zip(servers, probes))


def clear_probe_cache():
    """清掉探测缓存（测试/强制刷新用）。"""
    with _probe_cache_lock:
        _probe_cache.clear()



def find_ready_server() -> dict | None:
    """返回第一台 可达 + 带卡 + 模型就绪 的服务器；都没就绪返回 None。

    语义与旧版完全一致（同样的优先级顺序），但探测改为并行 probe_full：
    每台 1 次 SSH 往返 + TTL 缓存。旧版是串行 3~4 次往返/台。
    """
    for s, p in probe_all():
        if p['reachable'] and p['gpu_ok'] and p['model_ok']:
            log.info(f'🟢 选中服务器: {p["name"]} ({p["alias"]}) gpu={p["gpu"]}')
            return s
    return None


def any_ready() -> bool:
    """是否至少有一台可用服务器（供健康监控判断是否可接单/可恢复）。"""
    return find_ready_server() is not None


# ═══════════════ 任务处理 ═══════════════

def process_job(job: dict) -> bool:
    """处理单个配图任务：确保服务器→生成→拉回→插入→通知（小红书产线专用）"""
    if GEN_MODE == 'resident':
        return process_job_resident(job)
    return process_job_legacy(job)


def process_job_resident(job: dict) -> bool:
    """常驻服务版：逐张提交给 GPU 机上的常驻服务。

    与原链路的差别：
      · 模型不再每张重载 —— N 张图只加载一次模型（原链路是 N 次）
      · 逐张落盘，中途失败时前面的成品保留（重跑会覆盖重生成，但不影响已插入的稿子）
      · 产物布局与 pull_images 一致（images/flux_out/<batch>/P<n>.png），
        所以 insert_into_note 完全不用改
    """
    # 延迟导入：flux_resident_client 反向依赖本模块，模块级导入会成环
    import manager.flux_resident_client as fr

    n = len(job['images'])
    job_id = job['job_id']
    log.info(f'▶ 处理任务(常驻): {job["note_title"]} ({n}张)')

    server, p = fr.find_available_server()
    if not server:
        log.warning('🔴 FLUX 服务器不可达，通知 user 开机')
        notify_owner(f'🔴 FLUX 文生图服务器不可达\n'
                     f'有配图任务待处理: {job["note_title"]} ({n}张)\n'
                     f'请到 AutoDL 控制台开机（带卡模式）。')
        return False                             # 任务保留队列，恢复后重试

    r = fr.ensure_resident(server, p)
    if not r['ok']:
        log.error(f'❌ 常驻服务不可用: {r["msg"][:200]}')
        if r['kind'] == 'server_down':
            notify_owner(f'⚠️ FLUX 常驻服务不可用（{server["name"]}）\n'
                         f'任务: {job["note_title"]}\n{r["msg"][:300]}')
        return False

    dest_dir = OBSIDIAN_IMAGES / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(job['images'], start=1):
        out = dest_dir / f'P{i}.png'
        try:
            st = fr.generate_via_resident(server, img['prompt'], out)
        except fr.TransportError as e:
            log.error(f'❌ 第 {i}/{n} 张失败 [{e.kind}]: {e}')
            return False                         # 保留队列；恢复后整批重跑（已存在的 P*.png 会被覆盖）
        log.info(f'  [{i}/{n}] ✅ {out.name} 推理 {st.get("runtime")}s seed={st.get("seed")}')

    insert_into_note(job)
    notify_owner(f'✅ FLUX 配图完成: {job["note_title"]} ({n}张)\n'
                 f'已插入稿子，可在 Obsidian 查看。')
    log.info(f'✅ 任务完成(常驻): {job_id}')
    return True


def process_job_legacy(job: dict) -> bool:
    """旧链路：确保服务器→冷启动生成→拉回→插入→通知（保留作逃生口）"""
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
    if server_reachable():  # 小红书产线 process_job 绑定默认服务器(flux1)
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
    log.info(f'   服务器注册: {[s["name"] for s in FLUX_SERVERS]}')
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