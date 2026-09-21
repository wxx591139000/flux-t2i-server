# 生成「重启 FLUX 服务」的 .bat —— 按 win-bat-debug skill 规范：GBK + CRLF
#
# 为什么用脚本生成而不是直接写文件：我需要精确控制**编码**（GBK）与**行尾**（CRLF）。
# 用文本编辑器写会存成 UTF-8 + LF，而 cmd.exe 按系统 ANSI 代码页（中文=GBK）解析 .bat，
# 于是中文变乱码、goto/label 失效（详见 ~/.workbuddy/skills/win-bat-debug）。
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / '启动-01R-FLUX服务重启.bat'

PS = 'powershell'

lines = [
    '@echo off',
    'setlocal',
    'rem [注意] 不要在这里加 chcp 65001：本文件是 GBK 编码，切到 UTF-8 代码页会把中文变乱码。',
    'rem        cmd.exe 按系统 ANSI(GBK) 解析 .bat，保持默认即可（详见 win-bat-debug skill）。',
    'rem 重启 FLUX 文生图服务 (port 9620) 以加载最新代码。',
    'rem',
    'rem 为什么需要它：Python 进程 import 后不再读盘，改了代码不重启 = 还是旧逻辑。',
    'rem 原启动脚本对已占端口只打印「已在运行，跳过」，没有任何重启路径',
    'rem （2026-09-21 实测：两次点击间隔 24 分钟，/health 的 web_uptime_sec 一路涨，从未重启）。',
    'rem 改完 manager/ 或 server/ 下的代码，点这个即可。',
    '',
    'rem 用完整路径调用 powershell —— 只写 powershell 在某些 PATH 环境下会「不是内部或外部命令」',
    f'{PS} -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_service.ps1" -Target flux -Restart',
    'set RC=%ERRORLEVEL%',
    '',
    'echo.',
    'if not "%RC%"=="0" (',
    '    echo [警告] 重启脚本返回 %RC% —— 服务可能没起来。',
    '    echo        请看上面的错误行，并查 manager\\flux_service.err.log',
    ') else (',
    '    echo 已重启 FLUX 服务（port 9620）。',
    ')',
    '',
    'rem ── 自动验收：证明「跑着的确实是新代码」，而不是靠肉眼 ──',
    'echo.',
    'echo === 验收：进程新鲜度 ===',
    'rem 用 py -3.11（本机装了业务依赖的解释器）；tools\\check_stale.py 退出码 0=新 1=需重启 2=没跑',
    'py -3.11 "%~dp0tools\\check_stale.py"',
    'set RC2=%ERRORLEVEL%',
    'if "%RC2%"=="2" (',
    '    echo [失败] 服务没在跑。',
    ') else if "%RC2%"=="1" (',
    '    echo [失败] 跑着的不是当前代码 —— 请把上面的输出发给 Hermes。',
    ') else (',
    '    echo [通过] 进程是当前代码，删除等功能应已可用。',
    ')',
    '',
    'pause',
    'endlocal',
]

text = '\r\n'.join(lines) + '\r\n'
OUT.write_bytes(text.encode('gbk'))

# ── 自检：编码 / 行尾必须符合规范，否则拒绝交付 ──
d = OUT.read_bytes()
ok_bom = d[:3] != b'\xef\xbb\xbf'
ok_crlf = d.count(b'\r\n') > 0 and (d.count(b'\n') - d.count(b'\r\n')) == 0
try:
    d.decode('gbk')
    ok_gbk = True
except Exception as e:
    ok_gbk = False
    print('GBK 解码失败:', e)

print(f'输出: {OUT.name}')
print(f'  无 BOM : {ok_bom}')
print(f'  CRLF   : {ok_crlf}')
print(f'  GBK    : {ok_gbk}')
sys.exit(0 if (ok_bom and ok_crlf and ok_gbk) else 1)
