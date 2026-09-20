#!/usr/bin/env python3
"""一次性验证：图生图（图生图 = /edit）大 payload 走 ssh stdin 能否打通。

背景（2026-09-20）：站点任务 ceff1b2c 卡死，error =
  [SERVER_DOWN] SSH 调用失败（flux5）: FileNotFoundError: [WinError 206] 文件名或扩展名太长
真因 = 参考图 base64 内联进 ssh 命令行，超过 Windows CreateProcess 32K 上限。
修法 = body 改走 ssh stdin。本脚本用一张真实体积的参考图实测。

用法： python tests/manual/test_edit_stdin.py [参考图路径]
"""
import base64
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from manager import flux_server_manager as fsm
from manager import flux_resident_client as frc

ref = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / 'web_out/dc8aa2bef5f343ea/dc8aa2bef5f343ea.png'
assert ref.exists(), f'参考图不存在: {ref}'

raw = ref.read_bytes()
ref_b64 = base64.b64encode(raw).decode('ascii')
print(f'参考图 {ref.name}: {len(raw)} bytes → base64 {len(ref_b64)} 字符 '
      f'（远超 Windows 命令行 32K 上限，正是旧写法爆炸的规模）')

cands = frc.probe_all(force=True)
srv, _p = frc._pick_from(cands, need_edit=True)
if not srv:
    print('❌ 没有支持图生图（supports_edit）且在线的机器')
    sys.exit(1)
print(f'▶ 选用 {srv["name"]}（supports_edit={srv.get("supports_edit")}）')

out = BASE / 'tests/manual/_edit_out.png'
t0 = time.time()
try:
    st = frc.generate_via_resident(
        srv, 'give this product a colorful ceramic glaze pattern with a crescent moon logo',
        dest=out, width=1024, height=1024, steps=4, seed=1274429120,
        ref_image_b64=ref_b64, timeout=900)
except Exception as e:
    print(f'❌ 失败: {type(e).__name__}: {e}')
    sys.exit(1)

print(f'✅ 图生图成功：{st.get("path")}')
if out.exists():
    print(f'   拉回 {out.stat().st_size} bytes · 总耗时 {time.time()-t0:.1f}s')
print(f'   推理耗时 {st.get("elapsed")} · seed {st.get("seed")}')
