#!/usr/bin/env python3
"""启动脚本回归门（2026-09-21 新增）。

为什么需要它（真实事故，不是假设）：
  给 start_service.ps1 加了 -Restart 之后，我"测"了却没测到 —— 因为我在嵌套
  powershell 里跑它，那个子进程**根本没执行**（本环境无控制台宿主不回传/不执行）。
  于是我把"没有任何输出"当成了"跑通了"。真实情况是：

    -Restart 的真实行为 = 先 Stop-Process 杀掉 9620 → 然后抛异常 → **端口直接死掉**，
    而输出看起来像"什么都没发生"。

  真实根因：环境里同时存在 `http_proxy` 与 `HTTP_PROXY`（https 同理）。
  PowerShell 5.1 的环境提供程序 **大小写不敏感**，所以 `Get-ChildItem env:` 抛
  「已添加了具有相同键的项」（实测连续 6 次全抛，非偶发）。
  而 Start-Process 带 -RedirectStandardOutput 时会枚举环境 → 同样抛。

  这个门的作用：把"脚本必须能自愈这个环境坑"和"起不来必须报出来"钉死。
  改了 start_service.ps1 请务必跑它。

用法: py -3.11 tests/test_start_script.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PS1 = BASE / 'start_service.ps1'

passed = failed = 0


def check(name, cond, extra=''):
    global passed, failed
    if cond:
        passed += 1
        print(f'  ✅ {name}')
    else:
        failed += 1
        print(f'  ❌ {name}' + (f' — {extra}' if extra else ''))


def code_only(src):
    """剥掉注释，只留真代码 —— 否则注释里的示例会骗过断言。"""
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith('#'):
            continue
        # 去掉行尾注释（不追求完美，够用即可）
        if ' #' in line:
            line = line.split(' #')[0]
        out.append(line)
    return '\n'.join(out)


def main():
    print('=' * 66)
    print('启动脚本回归门：start_service.ps1')
    print('=' * 66)

    if not PS1.exists():
        print(f'❌ 找不到 {PS1}')
        return 1
    src = PS1.read_text(encoding='utf-8-sig')
    body = code_only(src)

    # ---------- [1] 参数契约 ----------
    print('\n[1] 参数：必须支持 -Restart（改了代码之后唯一的正确入口）')
    check('声明了 [switch]$Restart', re.search(r'\[switch\]\s*\$Restart', body) is not None)
    check('保留 -Target 参数（flux/xhs/tunnel/all）',
          re.search(r'\[string\]\s*\$Target', body) is not None)
    check('头部注释说明了 -Restart 的用途与事故背景',
          '-Restart' in src and '改' in src and ('重启' in src or 'reload' in src.lower()))

    # ---------- [2] 环境自愈（本次事故的核心） ----------
    print('\n[2] 环境自愈：必须消除大小写重复的代理变量')
    check('存在修复函数（消除 env 重复键）',
          re.search(r'function\s+Repair-DuplicateEnv', body) is not None)
    check('★ 覆盖 http_proxy / https_proxy（这两个是本机实际重复的）',
          all(k in body for k in ('http_proxy', 'https_proxy')))
    check('用 .NET SetEnvironmentVariable 置空（不是删 env: 提供程序项）',
          'SetEnvironmentVariable' in body)
    check('★ 在脚本早期就执行修复（不是定义完就丢着不用）',
          len(re.findall(r'Repair-DuplicateEnv', body)) >= 2,
          '必须出现 ≥2 次：定义 1 次 + 调用 1 次')
    check('修复动作只在真的重复时才做（不无条件删用户变量）',
          # 语义：必须同时取到「小写」和「大写」两个值，并**两者都判 non-null** 才动作。
          # 不钉 `-ne $null` 的字面顺序 —— 那是在断言代码风格而不是行为
          # （实测代码写的是 `$null -ne $vLow -and $null -ne $vUp`，同样正确）。
          'GetEnvironmentVariable' in body
          and re.search(r'\$vLow\b', body) is not None
          and re.search(r'\$vUp\b', body) is not None
          and len(re.findall(r'-ne\s+\$v(Low|Up)|\$v(Low|Up)\s+-ne', body)) >= 2,
          '必须取 lowercase/uppercase 两个值且都判空')

    # ---------- [3] 启动必须可验证 + 可回退 ----------
    print('\n[3] 启动封装：起不来必须报出来，且不能让端口空着')
    check('存在 Start-BackgroundService 封装',
          re.search(r'function\s+Start-BackgroundService', body) is not None)
    check('★ 起完要**验证端口在监听**（起了但不监听等于没起）',
          'Test-Port' in body and 'Test-Port $port' in body)
    check('★ 有回退路径（Start-Process 失败时退到不带重定向）',
          body.count('Start-Process') >= 3,
          f'实际出现 {body.count("Start-Process")} 次')
    check('失败时置 hadFailure 标记（不能静默吞掉）',
          '$script:hadFailure' in body)
    check('结尾按 hadFailure 决定退出码（便于机器判定）',
          'if ($script:hadFailure) { exit 1 } else { exit 0 }' in body)

    # ---------- [4] 危险顺序：先杀后起必须夹着验证 ----------
    print('\n[4] -Restart 顺序：先停 → 再起 → 再验证')
    stop_fn = re.search(r'function\s+Stop-PortOwner.*?\n\}', body, re.S)
    check('存在 Stop-PortOwner', stop_fn is not None)
    if stop_fn:
        s = stop_fn.group(0)
        check('停止后等待端口真正释放（不是杀完就走）',
              'Test-Port $port' in s and 'Start-Sleep' in s)
        check('★ 端口 10s 内没释放要打警告（否则会静默起不来）',
              '仍未释放' in src or 'warning' in s.lower() or '⚠️' in src)
    check('FLUX 段在端口被占且未给 -Restart 时打印警告（别让人以为已加载新代码）',
          '不会**加载新代码' in src or '不会**加载新代码' in src or '**不会**加载新代码' in src)

    # ---------- [5] 变异测试：关键断言必须真的会红 ----------
    print('\n[5] 变异测试（验证上面断言本身有效）')
    mutated = body.replace('Test-Port $port', 'Test-Path $port')
    check('变异：把端口验证换成文件验证后，[3] 的断言必须报红',
          'Test-Port $port' not in mutated,
          '断言依赖该字符串，若替换后仍匹配说明断言写空了')
    mutated2 = body.replace('SetEnvironmentVariable', 'Remove-Item')
    check('变异：把环境修复换成删项后，[2] 的断言必须报红',
          'SetEnvironmentVariable' not in mutated2)

    print('\n' + '=' * 66)
    print(f'启动脚本回归门：{passed} 项通过，{failed} 项失败')
    print('=' * 66)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
