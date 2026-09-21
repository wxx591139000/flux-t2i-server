"""GPU 机资产体检：磁盘、模型目录、显存、常驻进程、运行环境。

用途：关机前留档 —— 记录「这台机器上到底有什么」，以便下次开机核对，
以及判断哪些模型资产值得保留（复用时免去重新下载）。

跑法：py -3.11 tools/_inspect_gpu.py [机器名]     默认 flux5
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'manager'))
sys.stdout.reconfigure(encoding='utf-8')
import flux_server_manager as fsm  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else 'flux5'
servers = fsm.load_registry()
srv = next((s for s in servers if s['name'] == name), None)
if not srv:
    print(f'注册表里没有 {name}；现有：{[s["name"] for s in servers]}')
    sys.exit(1)

tgt = fsm.ssh_target(srv)
print(f'=== {name} 资产体检 ===\n')
print('注册表声明: model=%s  offload=%s  supports_edit=%s\n'
      % (srv.get('remote_model'), srv.get('offload'), srv.get('supports_edit')))

CMDS = [
    ('GPU', 'nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader'),
    ('磁盘', 'df -h /root/autodl-tmp | tail -1'),
    ('模型目录', 'ls -la /root/klein-models/ 2>/dev/null'),
    ('模型大小', 'du -sh /root/klein-models/* 2>/dev/null'),
    ('DOWNLOAD_DONE', 'find /root/klein-models -maxdepth 2 -name DOWNLOAD_DONE 2>/dev/null'),
    ('常驻进程', 'pgrep -af flux_resident_server || echo "未运行"'),
    ('常驻端口', 'ss -ltn 2>/dev/null | grep 9630 || echo "9630 未监听"'),
    ('工作目录', 'ls /root/autodl-tmp/flux-t2i/ 2>/dev/null'),
    ('输出张数', 'ls /root/autodl-tmp/flux-t2i/resident_out/ 2>/dev/null | wc -l'),
    ('运行环境', 'python -c "import torch,diffusers;print(torch.__version__, diffusers.__version__)" 2>&1 | tail -1'),
    ('镜像磁盘', 'du -sh /root/autodl-tmp 2>/dev/null'),
]
for label, cmd in CMDS:
    full = f'ssh {tgt} "{cmd}"'   # ssh_target() 只给选项片段，ssh 本身要自己拼
    try:
        ok, out = fsm.run(full, timeout=90)
        out = (out or '').strip()
        print(f'── {label} ──')
        print(out if out else '(空)')
        print()
    except Exception as e:
        print(f'── {label} ──  ✗ {type(e).__name__}: {str(e)[:150]}\n')
