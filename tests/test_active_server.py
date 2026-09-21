#!/usr/bin/env python3
"""回归门：active 收敛（VPS 看门狗只使能「当前开机的那台」）

起因（2026-09-21 用户需求）
  用户有多台 GPU 机（flux1 / flux5 / flux6…），但**同一时刻只开一台**，
  每次手动去 AutoDL 控制台选一台开机。
  现状是 `enabled: true` = 「永远探活这台」，于是多数条目 `true` 却离线 →
  **每轮选机都要为每台关机机白等一次 SSH 超时**（这正是当天延迟事故的主因之一）。
  期望：由 VPS 看门狗（唯一知道谁在线的组件）维护「当前开着哪台」，
  manager 每轮直接读，候选集收敛成开着的那台 —— 关机机器**零 SSH 开销**。

本门守住八条不变量（全离线：不连 SSH、不要 GPU、不碰数据库、不碰真 VPS）
  1. servers.json 里有 `watchdog` 配置块，且 ssh/active_path 可读
  2. read_active 解析 JSON；前后夹带杂音（motd）也能取到
  3. active_names 三态语义分明：
       None = 读不到事实（降级） / set() = 确实一台没开 / {'flux6'} = 开着这些
  4. filter_active：命中 name 或 alias 都算；VPS 不可得时**原样返回**（可用性优先）
  5. filter_active：读到空集时返回 **[]**（不许瞎猜成全量）
  6. probe_all 在并发路径上真的做了收敛（源码级 AST 断言，不只靠函数单测）
  7. 看门狗脚本：写 active.json 是**原子**的（tmp + mv），且有迟滞计数 +
     离线立即清零（用户原话「探测到离线就立即剔除」）
  8. 变异测试：把「读到空集」和「读不到」合并成一个分支 → 本门必须变红

用法: python tests/test_active_server.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'manager'))

import manager.flux_server_manager as fsm          # noqa: E402

REGISTRY = os.path.join(BASE, 'manager', 'servers.json')
WATCHDOG = os.path.join(BASE, 'watchdog', 'flux_watchdog.sh')
GEN_TARGETS = os.path.join(BASE, 'watchdog', 'gen_targets.py')

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + detail) if detail else ""}')


def _fake_read_active(payload):
    """把 fsm.read_active 换成返回固定 payload 的桩，返回还原函数。"""
    orig = fsm.read_active
    fsm.read_active = lambda force=False, cfg=None: payload
    fsm.clear_active_cache()
    return lambda: setattr(fsm, 'read_active', orig)


def _srv(name, alias=None, **kw):
    d = {'name': name, 'alias': alias or name}
    d.update(kw)
    return d


# ── [1] 配置块 ────────────────────────────────────────────────
def t_cfg():
    print('\n[1] servers.json 的 watchdog 配置块')
    with open(REGISTRY, encoding='utf-8') as f:
        raw = json.load(f)
    blk = raw.get('watchdog')
    check('顶层有 watchdog 块', isinstance(blk, dict), str(blk))
    check('watchdog.ssh 非空（VPS 地址）', bool((blk or {}).get('ssh')), str((blk or {}).get('ssh')))
    check('watchdog.active_path 指向 active.json',
          str((blk or {}).get('active_path', '')).endswith('active.json'),
          str((blk or {}).get('active_path')))

    cfg = fsm.watchdog_cfg()
    check('watchdog_cfg 读得到 ssh', bool(cfg.get('ssh')), str(cfg.get('ssh')))
    check('watchdog_cfg 的 ttl_sec 是数字', isinstance(cfg.get('ttl_sec'), float), str(cfg.get('ttl_sec')))

    # 环境变量覆盖（调试用，不该依赖改仓库）
    os.environ['FLUX_WATCHDOG_SSH'] = 'other-vps'
    try:
        check('环境变量 FLUX_WATCHDOG_SSH 可覆盖', fsm.watchdog_cfg().get('ssh') == 'other-vps')
    finally:
        os.environ.pop('FLUX_WATCHDOG_SSH', None)

    # 老注册表（没有 watchdog 块）不能崩：要给出默认值，然后走降级
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump({'servers': [{'name': 'x', 'host': 'h'}]}, f)
        tmp = f.name
    try:
        c2 = fsm.watchdog_cfg(tmp)
        check('缺 watchdog 块时不崩、给默认值（老注册表兼容）',
              c2.get('active_path', '').endswith('active.json'), str(c2))
    finally:
        os.unlink(tmp)


# ── [2] 解析 ──────────────────────────────────────────────────
def t_parse():
    print('\n[2] read_active 的解析健壮性')
    # 直接测解析逻辑（走 run 桩，避免真 ssh）
    orig_run = fsm.run

    def fake_run(cmd, timeout=120):
        return True, fsm.__dict__.get('_FAKE_OUT', '')

    fsm.run = fake_run
    try:
        good = json.dumps({'active': ['flux6'], 'checked_at': 123,
                           'servers': {'flux6': {'state': 'ready'}}})
        fsm.__dict__['_FAKE_OUT'] = good
        fsm.clear_active_cache()
        d = fsm.read_active(force=True)
        check('正常 JSON 能解析', isinstance(d, dict) and d.get('active') == ['flux6'], str(d))

        # 远端 shell 常夹带 motd / 告警行，取首 { 到末 } 即可
        fsm.__dict__['_FAKE_OUT'] = 'Welcome to Ubuntu\nLast login: ...\n' + good + '\ntrailing noise'
        fsm.clear_active_cache()
        d2 = fsm.read_active(force=True)
        check('前后夹带杂音仍能取到 JSON', isinstance(d2, dict) and d2.get('active') == ['flux6'],
              str(d2))

        fsm.__dict__['_FAKE_OUT'] = 'not json at all'
        fsm.clear_active_cache()
        check('非 JSON → None（不抛异常）', fsm.read_active(force=True) is None)

        fsm.__dict__['_FAKE_OUT'] = ''
        fsm.clear_active_cache()
        check('空输出 → None', fsm.read_active(force=True) is None)
    finally:
        fsm.__dict__.pop('_FAKE_OUT', None)
        fsm.run = orig_run

    # 没配 VPS → 直接 None，且不尝试 ssh（不该有网络动作）
    orig_run2 = fsm.run
    called = {'n': 0}

    def counting_run(cmd, timeout=120):
        called['n'] += 1
        return True, ''

    fsm.run = counting_run
    try:
        fsm.clear_active_cache()
        r = fsm.read_active(force=True, cfg={'ssh': '', 'active_path': '/x', 'ttl_sec': 0})
        check('未配 VPS 时返回 None 且不发 ssh', r is None and called['n'] == 0,
              f'calls={called["n"]}')
    finally:
        fsm.run = orig_run2
        fsm.clear_active_cache()


# ── [3] active_names 三态 ─────────────────────────────────────
def t_tristate():
    print('\n[3] active_names 的三态语义（None / 空集 / 有值 不能混）')
    cases = [
        ({'active': ['flux6'], 'servers': {}}, {'flux6'}, '有值'),
        ({'active': [], 'servers': {}}, set(), '空集 = 确实一台没开'),
        (None, None, 'None = 读不到事实（降级）'),
        ({'servers': {}}, None, '缺 active 字段 → 不猜，降级'),
        ({'active': 'flux6'}, {'flux6'}, '容忍单台字符串写法'),
        ({'active': ['flux6', '', None]}, {'flux6'}, '过滤空元素'),
    ]
    for payload, want, label in cases:
        restore = _fake_read_active(payload)
        try:
            got = fsm.active_names(force=True)
            if want is None:
                check(f'active_names: {label}', got is None, f'got={got}')
            else:
                check(f'active_names: {label}', got == want, f'got={got} want={want}')
        finally:
            restore()

    # 关键区分：None 与 set() **必须**是不同分支
    restore = _fake_read_active(None)
    try:
        a = fsm.active_names(force=True)
    finally:
        restore()
    restore = _fake_read_active({'active': []})
    try:
        b = fsm.active_names(force=True)
    finally:
        restore()
    check('None 与空集语义不同（这是整道门最关键的一条）',
          a is None and b == set(), f'None→{a} 空集→{b}')


# ── [4][5] filter_active ──────────────────────────────────────
def t_filter():
    print('\n[4] filter_active：收敛 / 匹配 / 降级')
    s1 = _srv('flux1', remote_model='/m/dev')
    s5 = _srv('flux5', remote_model='/m/klein')
    s6 = _srv('flux6', remote_model='/m/klein')
    alls = [s1, s5, s6]

    restore = _fake_read_active({'active': ['flux6']})
    try:
        got = [s['name'] for s in fsm.filter_active(alls)]
        check('只留开着的那台', got == ['flux6'], str(got))
    finally:
        restore()

    # 多台同时开着（用户偶尔会开两台做对比）→ 都留，交给分级选机
    restore = _fake_read_active({'active': ['flux1', 'flux6']})
    try:
        got = [s['name'] for s in fsm.filter_active(alls)]
        check('多台开着时都保留（交给分级选机）', got == ['flux1', 'flux6'], str(got))
    finally:
        restore()

    # alias 命中也算（自动发现的机器可能只有 alias）
    alias_srv = [{'name': 'auto1', 'alias': 'autodl-flux9'}]
    restore = _fake_read_active({'active': ['autodl-flux9']})
    try:
        got = [s['name'] for s in fsm.filter_active(alias_srv)]
        check('按 alias 也能命中', got == ['auto1'], str(got))
    finally:
        restore()

    # ★ 降级：读不到 VPS → 全量返回（可用性优先）
    restore = _fake_read_active(None)
    try:
        got = [s['name'] for s in fsm.filter_active(alls)]
        check('★ 读不到 VPS → 原样全量返回（降级，可用性优先）', got == ['flux1', 'flux5', 'flux6'],
              str(got))
    finally:
        restore()

    # ★ 读到空集 → 返回 []（准确答案，不许瞎猜成全量）
    restore = _fake_read_active({'active': []})
    try:
        got = fsm.filter_active(alls)
        check('★ 读到空集 → 返回 []（不瞎猜成全量）', got == [], str(got))
    finally:
        restore()

    # ★★ CRLF 陷阱（2026-09-21 真机实测踩到，必须钉住）
    #    targets.conf 由 Windows 侧生成，行尾 CRLF → 远端 bash 的 read 把 \r 留在
    #    最后一个字段 → name 变 "flux1\r"。若 manager 不归一，就**永远匹配不上**，
    #    表现为「机器明明开着，候选集却是空的」且全程无报错。
    #    真机证据：active.json = {"active":[],...,"flux1\r":{...}}（部署后实测）
    restore = _fake_read_active({'active': ['flux1\r', ' flux6 \n']})
    try:
        names = fsm.active_names(force=True)
        check('active 名单做了 strip（\\r / 空白 被剥掉）', names == {'flux1', 'flux6'}, str(names))
        got = [s['name'] for s in fsm.filter_active([_srv('flux1'), _srv('flux6')])]
        check('★ 带 \\r 的 active 名单仍能匹配上机器（CRLF 陷阱不复发）',
              got == ['flux1', 'flux6'], str(got))
    finally:
        restore()

    # active 里有不存在的名字 → 不匹配任何机器 → 空
    restore = _fake_read_active({'active': ['flux99']})
    try:
        got = fsm.filter_active(alls)
        check('active 名字对不上任何机器 → 空（不误留）', got == [], str(got))
    finally:
        restore()


# ── [6] probe_all 真收敛（源码级） ────────────────────────────
def t_probe_all_wired():
    print('\n[5] probe_all 在并发路径上真的做了收敛（源码级 AST）')
    src = open(os.path.join(BASE, 'manager', 'flux_server_manager.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'probe_all':
            fn = node
            break
    check('找到 probe_all 定义', fn is not None)

    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            nm = getattr(f, 'attr', None) or getattr(f, 'id', None)
            if nm:
                calls.append(nm)
    check('probe_all 调用了 filter_active（收敛落点）', 'filter_active' in calls, str(sorted(set(calls))))

    # 显式传 servers 时不该收敛（否则「--server flux6」会被 active 打回）
    src_has_guard = 'explicit' in src and 'if not explicit' in src
    check('显式指定 servers 时不做收敛（CLI 指定优先）', src_has_guard)


# ── [7] 看门狗脚本 ────────────────────────────────────────────
def t_watchdog_script():
    print('\n[6] 看门狗脚本：原子写 + 迟滞 + 离线立即清零')
    try:
        src = open(WATCHDOG, encoding='utf-8').read()
    except OSError as e:
        check('读得到 flux_watchdog.sh', False, str(e))
        return
    check('定义了 write_active', 'write_active()' in src)
    check('原子写（先写 .tmp 再 mv）', 'ACTIVE_TMP' in src and 'mv -f "$ACTIVE_TMP"' in src,
          '避免 manager 读到半个 JSON')
    check('输出 active.json 路径', 'active.json' in src)
    check('有迟滞确认（CONFIRM 轮）', 'CONFIRM' in src and 'confirm' in src)
    check('离线立即清零计数（用户要求：探测到离线就立即剔除）',
          'rm -f "$cf"' in src.lower() or 'rm -f "$cf"' in src, '离线分支里 rm 计数文件')
    check('read 出第 7 段 name', 'offload name' in src, 'host:port:user:workdir:model:offload:name')
    check('name 缺失时用 host 兜底', 'tname=${name:-$host}' in src)
    # ★ CRLF 归一：targets.conf 由 Windows 侧生成，\r 会落到最后一个字段（= name）
    check('★ 剥掉行尾 \\r（CRLF 归一，真机实测踩到）',
          "${line%$'\\r'}" in src, '否则 name 变成 "flux1\\r" → active 永远匹配不上')

    # bash 语法自检（不需要真机；-n 只做语法分析）
    bash = None
    for cand in (r'C:\Program Files\Git\bin\bash.exe', r'C:\Program Files\Git\usr\bin\bash.exe'):
        if os.path.exists(cand):
            bash = cand
            break
    if not bash:
        import shutil
        bash = shutil.which('bash')
    if bash:
        r = subprocess.run([bash, '-n', WATCHDOG], capture_output=True, text=True)
        check('bash -n 语法检查通过', r.returncode == 0, (r.stderr or '').strip()[:200])
    else:
        check('bash -n 语法检查通过', False, '本机找不到 bash（跳过=不通过，需显式修）')


# ── [7] gen_targets 的 name 字段 ──────────────────────────────
def t_gen_targets():
    print('\n[7] gen_targets：targets.conf 带第 7 段 name')
    sys.path.insert(0, os.path.join(BASE, 'watchdog'))
    import gen_targets                              # noqa: E402
    lines, _ = gen_targets.build_lines()
    check('至少生成 1 行', len(lines) >= 1, f'{len(lines)} 行')
    ok = True
    for ln in lines:
        parts = ln.split(':')
        if len(parts) < 7 or not parts[6].strip():
            ok = False
            break
    check('每行都有非空 name（第 7 段）', ok, lines[0] if lines else '')

    # ★ 生成端必须写 LF（不写 CRLF）：本机 Windows 的 write_text 默认 CRLF，
    #   会在真机造成 name="flux1\r" → active 收敛静默失效。这里直接落盘验字节。
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        outp = os.path.join(td, 'targets.conf')
        r = subprocess.run([sys.executable, GEN_TARGETS, '--out', outp],
                           capture_output=True, text=True, cwd=BASE)
        if r.returncode != 0:
            check('gen_targets 能落盘', False, (r.stderr or r.stdout)[-200:])
        else:
            raw = open(outp, 'rb').read()
            check('★ 生成端写 LF 而非 CRLF（真机 \\r 陷阱的根因）',
                  b'\r\n' not in raw and raw.count(b'\n') >= 1,
                  f'CRLF={raw.count(chr(13).encode()+chr(10).encode())}')

    # import 后 sys.path 污染要清掉，避免影响同进程后续门
    try:
        sys.path.remove(os.path.join(BASE, 'watchdog'))
    except ValueError:
        pass


# ── [8] 变异测试 ──────────────────────────────────────────────
def t_mutation():
    print('\n[8] 变异测试：把「空集」和「读不到」合并 → 门必须变红')

    def good_filter(servers, names):
        if names is None:
            return servers                      # 降级：全量
        if not names:
            return []                           # 确实没开机
        return [s for s in servers if s.get('name') in names or s.get('alias') in names]

    def broken_filter(servers, names):
        """变异版：把 None 和空集一起当「没信息」→ 都退回全量（最省事也最错）"""
        if not names:
            return servers
        return [s for s in servers if s.get('name') in names or s.get('alias') in names]

    alls = [_srv('flux1'), _srv('flux6')]
    good_empty = good_filter(alls, set())
    bad_empty = broken_filter(alls, set())
    check('正常实现：空集 → []（知识 = 一台没开）',
          good_empty == [], str([s['name'] for s in good_empty]))
    check('变异实现：空集 → 全量（门能抓住这个错）',
          len(bad_empty) == 2, str([s['name'] for s in bad_empty]))
    check('两者在「读不到(None)」这一支上行为一致（对照，证明差异只来自空集处理）',
          len(good_filter(alls, None)) == len(broken_filter(alls, None)) == 2)


def main():
    print('=' * 64)
    print('active 收敛回归门（VPS 看门狗只使能当前开机的机器）')
    print('=' * 64)
    t_cfg()
    t_parse()
    t_tristate()
    t_filter()
    t_probe_all_wired()
    t_watchdog_script()
    t_gen_targets()
    t_mutation()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print('\n' + '=' * 64)
    print(f'共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}')
    if bad:
        for n in bad:
            print(f'  ❌ {n}')
        return 1
    print('✅ 全绿')
    return 0


if __name__ == '__main__':
    sys.exit(main())
