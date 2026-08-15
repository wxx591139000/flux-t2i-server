# 项目推进进度 — FLUX 文生图服务（通用）

> 版本：v1.1 · 2026-08-15

## 里程碑回顾

- **2026-08-14**：小红书场景下最初部署 FLUX（山西旅游配图），踩坑记录到方案文档
- **2026-08-15**：沉淀为**通用项目** `flux-t2i-server`，与小红书解耦；完成 curl 流式下载解法；全流程跑通（下载32GB→切带卡→生成36张→拉回→智能插入7篇笔记）
- **2026-08-15（同日 v1.1）**：新增**对外文生图服务**（对标转录bot）：Web 提交 + 排队调度 + 配额计费 + 公网隧道 `flux.zhuanlu.xyz`；E2E 全链路实测通过

## 已完成功能清单

### 核心文生图（v1.0）
- [x] `server/gen_flux.py`：diffusers 批量文生图（bf16 + CPU offload，分组输出，断点跳过）
- [x] `server/start_gen.sh`：一键启动（带卡检查→模型检查→幂等→screen 后台）
- [x] `server/dl_curl.sh`：curl 流式分片下载（断点续传 + 停滞检测 + DOWNLOAD_DONE 标记）
- [x] `local/flux_gen_watchdog.py`：开机看门狗（对标转录 bot：检测→上传→启动→拉回）
- [x] `example/prompts.example.json`：通用提示词模板
- [x] 山西旅游 7 篇 36 张配图全流程跑通并插入笔记

### 对外服务（v1.1）
- [x] `manager/flux_service.py`：main 入口（wiring DB→quota→queue→web）
- [x] `manager/flux_web_service.py`：网页提交页 + API + 认证（cookie/token）
- [x] `manager/flux_queue.py`：优先队列 + 单 worker 串行 + 去重/配额/上限门 + 服务器down重排队
- [x] `manager/flux_db.py`：SQLite（users/codes/jobs/usage）
- [x] `manager/flux_quota.py` + `plans.yaml`：月度配额 + 套餐（default10/basic50/pro200）
- [x] `manager/feishu_notify.py`：飞书通知（独立机器人"图图"，与转录"小白"分离）
- [x] `manager/flux_server_manager.py`：SSH 生成/拉图/插稿/飞书，run() 已修 Windows 坑
- [x] 公网隧道 `flux.zhuanlu.xyz`（本地 xhs-tunnel）
- [x] **E2E 全链路实测**：提交→排队→FLUX 生成→拉图 web_out→公网下载（橘猫+小孩在岸边散步均成功）
- [x] 每个 done 任务网页带「⬇ 下载」按钮
- [x] `docs/WEB_SERVICE.md` + `docs/USER_GUIDE.md` 使用指南

## 进行中工作

- [ ] `flux-t2i-server` 二次归档（v1.2，tag archive-20260815-v2）

## 待办事项 / Roadmap

- [ ] **git 提交 + 打 tag** 本次 v1.1→v1.2 归档
- [ ] **小红书产线回归**：`flux_server_manager` 文件队列（run() 修复后）未再实测，需在服务器可用时跑通
- [ ] **长期守护**：服务进程 + cloudflared 隧道目前是手动/后台进程，会话结束会被回收；建议做开机自启守护脚本
- [ ] **owner 无限配额**：当前 owner 用预留 token，未配置 is_owner 无限逻辑（quota 里 owner 跳过）
- [ ] 清理测试失败记录（早期服务器未起时的 failed 任务）
- [ ] （可选）接入监控/告警（任务进度、下载完成通知）