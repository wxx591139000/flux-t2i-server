#!/usr/bin/env python3
"""回归门：机器注册表 / 连接目标 / 编辑能力选机 / 等待判死口径 / 参考图落盘

起因（2026-09-20 一次真实事故，全部都是同一个现象：网站上一直转圈）
  1) 候选机只从 ~/.ssh/config 匹配 autodl-flux* 自动发现。新克隆的 GPU 机没写进
     那个仓库外的文件 → manager 永远看不见它 → 任务进 waiting 池死等 8 分钟。
     → 修法：注册表搬进仓库 manager/servers.json，且支持 host/port/user **直连**。
  2) 图生图要 pipeline 的 __call__ 接受 `image`，而 FLUX.1-dev 的 FluxPipeline
     没有这个参数 → edit 任务落在 dev 机上必然报错。
     → 修法：注册表声明 supports_edit，选机阶段按能力过滤。
  3) waiting 任务按「重试 3 次」判死，而机器开机要 1~3 分钟 → 用户刚点开机，
     任务已被判死并退款，机器白开。
     → 修法：改成按**等待总时长**（WAIT_MAX_SEC）判死。
  4) 编辑任务的参考图只在内存 → manager 一重启 / FIFO 淘汰，编辑任务必失败。
     → 修法：同时落盘 web_out/<job_id>/ref.png，用时磁盘兜底。

本门守住八条不变量（全离线：不连 SSH、不要 GPU、不碰数据库）
  1. 注册表能被解析，enabled=false 的记录被跳过（释放的机器不再白等 SSH 超时）
  2. 直连条目解析出 -p/-i（ssh）与 -P/-i（scp）；无 host 的条目回退 alias
  3. 生效候选机 = 注册表 + 代码内默认机的并集，不重复
  4. need_edit=True 时只在 supports_edit 的机器里挑（dev 机常驻在跑也不选它）
  5. 没有任何机器声明 supports_edit 时不崩、不过滤（向后兼容老部署）
  6. flux_queue 里 waiting 按等待时长判死，且 edit 任务按能力选机（源码级）
  7. 参考图双写：_put_ref 落盘 / _get_ref 磁盘兜底 / _drop_ref 终态清理（源码级）
  8. ~/.ssh/config 不可读（权限受限/不存在）时**降级返回空**，
     discover_servers 不崩、仍用显式注册表兜底（2026-09-22 加）

用法: python tests/test_server_registry.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'manager'))

import manager.flux_server_manager as fsm          # noqa: E402
import manager.flux_resident_client as frc        # noqa: E402

REGISTRY = os.path.join(BASE, 'manager', 'servers.json')
QUEUE_SRC = os.path.join(BASE, 'manager', 'flux_queue.py')

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + detail) if detail else ""}')


def _strip_comments(src: str) -> str:
    """剥掉注释与字符串字面量 + 抹平所有空白，返回**可直接做子串查找**的代码骨架。

    ★ 两个必须踩过的坑（2026-09-22 本门自身各踩一次）：

    1. **必须在剥离后的骨架上定位**，不能对全文 find：
       docstring/注释里**正当地**会提到被断言的关键词（例如解释某个 bug 时
       写出 `cfg.exists()`），全文 find 会命中那处文本 → **假红**。

    2. **必须 `''.join()` 而不是 `'\\n'.join()`**：
       tokenize 把每个 token 都单独列出来，`'\\n'.join()` 会在**每个 token 之间**
       插换行 —— `def ssh_config_aliases` 变成 `def\\nssh_config_aliases\\n(...`
       → 子串查找**必然落空** → 又一种假红（第一次修完仍是 try@-1 exists@-1）。
       正确做法：`''.join()` 拼回连续文本，再用正则把空白一并抹平。

    与项目已定的「字面串断言要归一化/钉可观察行为」同源：
    **断言的目标是代码，不是文本排布。**
    """
    import io
    import re
    import tokenize
    keep = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        keep.append(tok.string)
    return re.sub(r'\s+', '', ''.join(keep))


def t_registry_enabled():
    print('\n[1] 注册表：解析 + enabled 过滤')
    raw = fsm.load_registry.__wrapped__ if hasattr(fsm.load_registry, '__wrapped__') else fsm.load_registry
    all_items = raw(REGISTRY) if raw is not fsm.load_registry else _load_raw()
    check('注册表可解析且非空', len(all_items) > 0, f'{len(all_items)} 条')
    names_all = [s['name'] for s in all_items]
    check('注册表里所有条目都留痕（含停用的）', len(names_all) >= 2, str(names_all))
    active = [s for s in fsm.FLUX_SERVERS]
    names_active = [s['name'] for s in active]
    check('生效候选机不重复', len(names_active) == len(set(names_active)), str(names_active))
    check('每台生效机器都有 remote_base / remote_model',
          all(s.get('remote_base') and s.get('remote_model') for s in active))

    # 数据驱动：谁被标了 enabled:false（用户释放了实例），谁就不许出现在候选里。
    # 不要硬编码机器名 —— 2026-09-20 就是硬编码 flux4 的用例在它重新启用后立刻误报。
    disabled = [s['name'] for s in all_items if s.get('enabled') is False]
    leaked = [n for n in disabled if n in names_active]
    check('enabled=false 的机器不参与探活', not leaked, f'停用={disabled} 泄漏={leaked}')
    check('注册表里至少有 1 台生效机', len(names_active) >= 1, str(names_active))

    # ⚠️ 2026-09-20 真 bug：enabled:false 只是不加载该条目，但 ~/.ssh/config 里若还留着
    #    autodl-flux2 这类同名 alias，自动发现会把它**又塞回候选** → 已释放的机器照样被探活，
    #    每次轮询白等 SSH 超时。修法：去重用「注册表里出现过的名字全集」（含 disabled）。
    known = fsm.registry_known_names()
    check('registry_known_names 含停用机器（去重集合的来源）',
          all(n in known for n in disabled), f'known={sorted(known)} disabled={disabled}')
    check('别名自动发现不会把停用机器塞回候选',
          not [s for s in active if s.get('name') in disabled], str(names_active))


def _load_raw():
    import json
    with open(REGISTRY, encoding='utf-8') as f:
        data = json.load(f)
    return data.get('servers', [])


def t_targets():
    print('\n[2] 连接目标：ssh -p / scp -P / alias 回退')
    s = {'name': 'x', 'host': 'h.example.com', 'port': 26081, 'user': 'root',
         'identity_file': '~/.ssh/id_rsa_musetalk'}
    ssh_t = fsm.ssh_target(s)
    scp_t = fsm.scp_target(s)
    check('ssh 用小写 -p', ' -p 26081 ' in f' {ssh_t} ' or ssh_t.endswith(' -p 26081'), ssh_t)
    check('scp 用大写 -P', ' -P 26081' in scp_t, scp_t)
    check('两者都带上私钥 -i', '-i ~/.ssh/id_rsa_musetalk' in ssh_t and '-i ~/.ssh/id_rsa_musetalk' in scp_t)
    # ⚠️ 新克隆的机器不在 known_hosts：没有 accept-new 首次必失败；
    #    没有 BatchMode 会卡在 "Are you sure you want to continue" 的交互提示上，
    #    非 tty 下等到超时，报错退化成没头没尾的 "TIMEOUT"（2026-09-20 flux5 实测）。
    check('带 StrictHostKeyChecking=accept-new（新机自动信任）',
          'StrictHostKeyChecking=accept-new' in ssh_t and 'StrictHostKeyChecking=accept-new' in scp_t,
          ssh_t)
    check('带 BatchMode=yes（不卡交互提示）',
          'BatchMode=yes' in ssh_t and 'BatchMode=yes' in scp_t, scp_t)
    check('两者都是 user@host', ssh_t.endswith('root@h.example.com') and scp_t.endswith('root@h.example.com'))
    check('scp 不残留小写 -p（语义是保留时间戳）', ' -p ' not in scp_t, scp_t)
    a = {'name': 'y', 'alias': 'autodl-flux'}
    check('无 host 时回退 alias', fsm.ssh_target(a) == 'autodl-flux' and fsm.scp_target(a) == 'autodl-flux')
    p = {'name': 'z', 'host': 'h2', 'port': 22, 'user': 'ubuntu'}
    check('无 identity_file 时不带 -i', '-i' not in fsm.ssh_target(p), fsm.ssh_target(p))

    # ⚠️ scp 的 getopt 遇到第一个非选项参数就**停止解析选项**。
    #    所以上传（源文件在前）必须走 scp_upload_cmd()，让选项整体排在文件名之前；
    #    写成 `scp "<file>" <opts> user@host:` 时 -i / accept-new / BatchMode 全失效
    #    → "Host key verification failed"（2026-09-20 flux5 上传常驻脚本必失败）。
    up = fsm.scp_upload_cmd(s, 'E:/repo/server/flux_resident_server.py', '/root/autodl-tmp/flux-t2i')
    toks = up.split()
    check('上传命令以 scp 开头', toks[0] == 'scp', up)
    # 跳掉「带参数的选项」再找第一个操作数：-o / -i / -P / -p 各吃掉后面一个 token
    i, first_op = 1, None            # 跳过 'scp' 本身
    while i < len(toks):
        if toks[i] in ('-o', '-i', '-P', '-p'):
            i += 2
            continue
        if toks[i].startswith('-'):
            i += 1
            continue
        first_op = i
        break
    tail = ' '.join(toks[first_op:]) if first_op is not None else ''
    check('上传时选项全在文件名之前（scp 才会真的解析它们）',
          first_op is not None and '-o ' not in tail and '-P ' not in tail
          and '-i ' not in tail, up)
    check('上传命令的目标带 user@host 且选项里没有重复 endpoint',
          'root@h.example.com:/root/autodl-tmp/flux-t2i/' in up
          and up.count('root@h.example.com') == 1, up)
    check('scp_endpoint 只返回 user@host（不含选项）',
          fsm.scp_endpoint(s) == 'root@h.example.com', fsm.scp_endpoint(s))


def _cand(name, supports_edit, **probe):
    p = {'name': name, 'reachable': False, 'gpu_ok': False, 'model_ok': False,
         'resident': False, 'model_loaded': False, 'error': ''}
    p.update(probe)
    return ({'name': name, 'supports_edit': supports_edit}, p)


def t_edit_capability():
    print('\n[3] 编辑能力：need_edit 只在 supports_edit 的机器里挑')
    dev = _cand('dev-machine', False, reachable=True, gpu_ok=True, model_ok=True,
                resident=True, model_loaded=True)     # 常驻在跑·模型已加载（最高级）
    klein = _cand('klein-machine', True, reachable=True, gpu_ok=True, model_ok=True)
    s, p = frc._pick_from([dev, klein], need_edit=True)
    check('选 klein 而不是"常驻在跑"的 dev 机', s and s['name'] == 'klein-machine',
          s['name'] if s else 'None')
    s2, _ = frc._pick_from([dev, klein], need_edit=False)
    check('need_edit=False 仍按分级选（dev 优先）', s2 and s2['name'] == 'dev-machine',
          s2['name'] if s2 else 'None')
    s3, p3 = frc._pick_from([_cand('only-dev', False, reachable=True, gpu_ok=True,
                                   model_ok=True, resident=True, model_loaded=True)],
                            need_edit=True)
    # ⚠️ 2026-09-22：契约**刻意变更**，断言跟着改（不是把门削弱）。
    #  旧行为：「无 capable 机器时不过滤」→ 把 edit 任务派给 dev 机 → GPU 侧
    #          必然报「当前模型不支持图生图」→ 用户白等一整轮。
    #          当时的理由是「supports_edit 只是提示位，别在这里误判成没机器」。
    #  新行为：能力表已是**权威**（resident 按类名现算），一台机明确不支持 edit
    #          就是跑不了 → 返回 (None, probe)，让上游报准确原因，别白等。
    #  这才与 need_model 的「谁都没有这个模型 → 返回 None」口径一致
    #  （同一个文件里两处同性质判断，口径必须一致，否则最难查）。
    #  ★ 同时**必须保住**旧断言里真正的不变量：**不崩**。
    #    2026-09-22 实测崩过 —— 过滤后 cands 被置空，末尾 `cands[0][1]` IndexError。
    check('★ 无 capable 机器时返回 None（不硬派给跑不了的机器）',
          s3 is None, s3['name'] if s3 else 'None')
    check('★ 返回 None 时仍带回 probe（调用方能显示「在线但能力不满足」）',
          isinstance(p3, dict) and p3.get('name') == 'only-dev',
          str(p3.get('name') if isinstance(p3, dict) else p3))
    s4, _ = frc._pick_from([], need_edit=True)
    check('空候选返回 None', s4 is None)
    # 判定不了时必须**保持不过滤**（旧行为），否则「探针拿不到能力」这种
    # 正常情况会被误判成「没有机器」→ 整条链路瘫掉。
    s5, _ = frc._pick_from([({'name': 'noinfo', 'supports_edit': None},
                             {'name': 'noinfo', 'reachable': True, 'gpu_ok': True,
                              'model_ok': True, 'resident': True,
                              'model_loaded': True, 'error': ''})],
                           need_edit=True)
    check('★ 能力无从判定时不误杀（保持不过滤）',
          s5 is not None and s5['name'] == 'noinfo',
          s5['name'] if s5 else 'None')


def t_source_level():
    print('\n[4] 源码级：waiting 判死 / 能力选机 / 参考图落盘')
    src = open(QUEUE_SRC, encoding='utf-8').read()
    check('WAIT_MAX_SEC 已定义', 'WAIT_MAX_SEC' in src)
    check('waiting 判死用等待时长而非重试次数',
          'waited > WAIT_MAX_SEC' in src and 'retry >= MAX_RETRY' not in src)
    # 改成 AST 断言「选机调用里带了哪些关键字」而不是匹配
    # 'find_available_server(need_edit=is_edit)' 这段字面量。
    # 为什么必须换（2026-09-21）：加模型过滤时只是把调用拆成两行、多传一个参数，
    # 字面量匹配立刻假红 —— 这是「测试测的是写法而不是行为」的典型。
    # 现在断言的是「两个过滤器都在」，这才是有意义的不变量：
    #   need_edit  丢了 → dev 机接 edit 任务（报「模型不支持图生图」）
    #   need_model 丢了 → 要 dev 的活派给只有 klein 的机器（GPU 侧白名单拒）
    _calls = []
    for _n in ast.walk(ast.parse(src)):
        if (isinstance(_n, ast.Call)
                and isinstance(_n.func, ast.Attribute)
                and _n.func.attr == 'find_available_server'):
            _calls.append({kw.arg for kw in _n.keywords})
    _all_kw = set().union(*_calls) if _calls else set()
    check('选机调用点名了缺省机群', bool(_calls), f'{len(_calls)} 处调用')
    check('edit 任务按能力选机', 'need_edit' in _all_kw, f'关键字={sorted(_all_kw)}')
    check('文生图按模型选机（模型选择器）', 'need_model' in _all_kw,
          f'关键字={sorted(_all_kw)}')
    check('参考图落盘 _ref_dir', 'def _ref_dir(self, job_id: str)' in src)
    check('参考图磁盘兜底 _get_ref', 'def _get_ref(self, job_id: str)' in src)
    check('参考图终态清理 _drop_ref', 'def _drop_ref(self, job_id: str)' in src)
    check('done/failed 两处都调用 _drop_ref', src.count('self._drop_ref(job_id)') >= 2,
          f'{src.count("self._drop_ref(job_id)")} 处')

    tree = ast.parse(src)
    # 改成「看 _put_ref 整个函数体」而不是匹配 `self._ref_dir(...).write_bytes(...)`
    # 这种具体写法 —— 后者会因为一次无害的重构（先赋给局部变量）就假红。
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == '_put_ref'), None)
    body = ast.unparse(fn) if fn else ''
    check('_put_ref 确实写盘（AST 断言）',
          '_ref_dir' in body and 'write_bytes' in body, body[:120])
    # ⚠️ 真正的坑：WEB_OUT/<job_id>/ 此时还不存在 → write_bytes 直接 FileNotFoundError，
    #    被 except 吞成一条 warning →「重启不丢参考图」从来没兑现过（2026-09-20 发现）。
    check('_put_ref 写盘前先 mkdir（父目录不存在会静默丢图）', 'mkdir' in body, body[:160])
    check('WAIT_MAX_SEC 默认 4 小时', 'str(4 * 3600)' in src or '14400' in src)


def t_mutation():
    """变异断言：把过滤条件拿掉，门必须变红（证明门真的在起作用，不是恒真）。"""
    print('\n[5] 变异断言：拆掉能力过滤，选机必须退化')
    dev = _cand('dev-machine', False, reachable=True, gpu_ok=True, model_ok=True,
                resident=True, model_loaded=True)
    klein = _cand('klein-machine', True, reachable=True, gpu_ok=True, model_ok=True)

    def broken_pick(cands, need_edit=False):
        """变异版：忽略 need_edit（等价于改回旧行为）"""
        for s, p in cands:
            if p['resident'] and p['model_loaded']:
                return s, p
        for s, p in cands:
            if p['reachable'] and p['gpu_ok'] and p['model_ok']:
                return s, p
        return None, None

    good = frc._pick_from([dev, klein], need_edit=True)[0]['name']
    bad = broken_pick([dev, klein], need_edit=True)[0]['name']
    check('正常实现选 klein，变异版选 dev（门能抓住）',
          good == 'klein-machine' and bad == 'dev-machine', f'正常={good} 变异={bad}')


def t_ssh_config_resilience():
    """~/.ssh/config 读不到时必须**降级**，不能把整个服务带崩。

    ★ 为什么加这一组（2026-09-22 实测事故）：
      9620 启动时崩在模块级 `FLUX_SERVERS = _load_servers()` →
      `discover_servers()` → `ssh_config_aliases()` → `cfg.exists()`，
      抛 `PermissionError: [WinError 5] 拒绝访问`。
      错误栈指向 `~/.ssh/config`，**看着像 SSH 配置坏了**；
      真因是「读不到一个可选配置文件」被当成了致命错误。

      原实现的防护只做了一半：`read_text()` 有 try 包住，
      但 `cfg.exists()` 在 try **外面**（而 `Path.exists()` 内部就是 `os.stat()`，
      **权限受限时抛异常而不是返回 False**）。

      语义：本函数是「**尽力**发现额外机器」，读不到就该返回 [] 并退回
      显式注册表（servers.json 才是权威来源）。
      **候选机可用性绝不能依赖一个可选配置文件的可读性。**
    """
    print('\n[6] ~/.ssh/config 不可读时必须降级（不能崩）')
    import pathlib
    import unittest.mock as mock

    real_exists = pathlib.Path.exists

    def boom(self):
        # 只模拟 .ssh/config 这一条路径的 stat 失败，其它路径照常
        if str(self).replace('\\', '/').endswith('.ssh/config'):
            raise PermissionError(13, 'Access denied (simulated)')
        return real_exists(self)

    with mock.patch.object(pathlib.Path, 'exists', boom):
        try:
            got = fsm.ssh_config_aliases()
            check('ssh_config_aliases 权限受限时不抛异常', True, f'返回 {got}')
            check('降级返回空列表（而非半截结果）', got == [], str(got))
        except PermissionError as e:
            check('ssh_config_aliases 权限受限时不抛异常', False,
                  f'抛了 PermissionError: {e}')

        # 更关键的一条：模块级加载路径也不能崩
        try:
            servers = fsm.discover_servers()
            check('discover_servers 权限受限时不崩', True, f'{len(servers)} 台')
            check('降级后仍能用显式注册表兜底', len(servers) > 0,
                  f'{len(servers)} 台（全是注册表条目）')
        except PermissionError as e:
            check('discover_servers 权限受限时不崩', False,
                  f'抛了 PermissionError —— 9620 会整个起不来: {e}')

    # 源码级：确认防御没有又被挪到 try 外面（防"整理代码"时回归）
    #
    # ⚠️ _strip_comments 返回的是**已抹平空白**的骨架，所以下面的针也必须
    #    写成无空格形式（`cfg.exists()` → `cfg.exists()`；`try:` → `try:`）。
    src = open(os.path.join(BASE, 'manager', 'flux_server_manager.py'),
               encoding='utf-8').read()
    code = _strip_comments(src)
    i_def = code.find('defssh_config_aliases')
    i_try = code.find('try:', i_def)
    i_exists = code.find('cfg.exists()', i_def)
    check('cfg.exists() 在 try 块内（不是半截防护）',
          i_def > 0 and -1 < i_try < i_exists,
          f'def@{i_def} try@{i_try} exists@{i_exists}（exists 必须 > try）')


def main():
    print('=' * 60)
    print('服务器注册表 / 选机 / 等待策略 回归门')
    print('=' * 60)
    t_registry_enabled()
    t_targets()
    t_edit_capability()
    t_source_level()
    t_mutation()
    t_ssh_config_resilience()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print('\n' + '=' * 60)
    print(f'共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}')
    if bad:
        for n in bad:
            print(f'  ❌ {n}')
        return 1
    print('✅ 全绿')
    return 0


if __name__ == '__main__':
    sys.exit(main())
