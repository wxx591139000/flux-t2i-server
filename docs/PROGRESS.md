# 项目推进进度 — FLUX 文生图服务（通用）

> 版本：v1.0 · 2026-08-15

## 里程碑回顾

- **2026-08-14**：小红书场景下最初部署 FLUX（山西旅游配图），踩坑记录到方案文档
- **2026-08-15**：沉淀为**通用项目** `flux-t2i-server`，与小红书解耦，可服务所有文生图需求；完成 curl 流式下载解法

## 已完成功能清单

- [x] `server/gen_flux.py`：diffusers 批量文生图（bf16 + CPU offload，分组输出，断点跳过）
- [x] `server/start_gen.sh`：一键启动（带卡检查→模型检查→幂等→screen 后台）
- [x] `server/dl_curl.sh`：curl 流式分片下载（断点续传 + 停滞检测 + DOWNLOAD_DONE 标记）
- [x] `local/flux_gen_watchdog.py`：开机看门狗（对标转录 bot：检测→上传→启动→拉回）
- [x] `example/prompts.example.json`：通用提示词模板
- [x] README + docs 5 份核心文档
- [x] Git 归档（tag archive-20260815）

## 进行中工作

- [ ] **模型大权重下载**：服务器上 curl 流式下载 31GB（transformer 22GB + T5 9GB），进行中（screen: fluxdl）
- [ ] 下载完成后验证模型可加载（L2/L4 测试）

## 待办事项 / Roadmap

- [ ] 下载完成 → 验证模型可加载（CPU 冒烟测试）
- [ ] 切带卡模式 → 跑 L3/L4 生成测试
- [ ] 为具体业务生成（如小红书 30 张、公众号配图等）
- [ ] （可选）封装更友好的 CLI / 配置中心化
- [ ] （可选）接入监控/告警（下载进度、生成完成通知）