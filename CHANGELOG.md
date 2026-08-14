# CHANGELOG

## [v1.0] - 2026-08-15

**通用化落地**：将小红书场景的 FLUX 部署沉淀为通用文生图服务项目，与业务解耦。

- 新增 `server/gen_flux.py`：diffusers 批量文生图（bf16 + CPU offload，分组输出，断点跳过）
- 新增 `server/start_gen.sh`：一键启动（带卡检查→模型检查→幂等→screen 后台）
- 新增 `server/dl_curl.sh`：curl 流式分片下载（断点续传 + 停滞检测）
- 新增 `local/flux_gen_watchdog.py`：开机看门狗（对标转录 bot）
- 新增 `example/prompts.example.json`：通用提示词模板
- 新增 `docs/`：SPEC / ARCHITECTURE / TEST_PLAN / PITFALLS / PROGRESS
- 密钥环境变量化（HF_TOKEN 不硬编码）

**commit**: `归档: flux-t2i-server 2026-08-15` · **tag**: `archive-20260815`