# FLUX 对外文生图服务

FLUX 文生图像转录bot 一样对外提供服务，带排队调度与配额计费。对标 `server-pdf-converter` 的 web 服务 + orchestrator 队列。

## 架构

```
公网用户(浏览器) ── cloudflared 隧道 flux.zhuanlu.xyz → localhost:9620 ──► flux_web_service.py
                                                                            │ 网页提交提示词 / API / 激活码 / admin
                                                                            ▼
                                            flux_queue.py FluxQueueScheduler（优先队列 + 单worker串行）
                                              │ 去重 → 配额precheck → 队列上限 → 入队
                                              ▼
                                         worker: SSH 调 FLUX 服务器(复用 flux_server_manager) → 生成 → 拉图 web_out/ → done
                                              ▼
                                    flux_db.py(SQLite) + flux_quota.py + plans.yaml
```

## 组件（`manager/`）

| 文件 | 职责 |
|------|------|
| `flux_service.py` | main 入口，wiring DB→quota→queue→web + 健康监控 |
| `flux_web_service.py` | 对外 HTTP 服务（stdlib http.server，无框架），网页 + API + 认证 |
| `flux_queue.py` | 队列调度器（核心）：PriorityQueue + 单 worker，复用 manager SSH 函数 |
| `flux_db.py` | SQLite 存储：users/codes/jobs/usage |
| `flux_quota.py` | 月度图片配额（owner 无限） |
| `plans.yaml` | 套餐（default/basic/pro，月度图片数） |
| `feishu_notify.py` | 飞书通知（服务器 down 提醒开机） |

## 启动

```bash
python manager/flux_service.py            # 默认端口 9620
python manager/flux_service.py --port 9620
```

## API

| 端点 | 说明 |
|------|------|
| `GET /` | 提交页（提示词输入 + 任务列表 + 看图） |
| `GET /center` | 我的任务中心 |
| `GET /health` | 健康检查 |
| `POST /api/submit` | `{prompt}` 提交生成任务（token 来自 cookie/query）→ 返回 job_id |
| `GET /api/status?job_id=` | 轮询任务状态 |
| `GET /api/download/<job_id>` | 下载图片（校验归属） |
| `POST /api/activate` | `{code}` 兑换激活码 |
| `POST /api/admin/gen_codes` | `{admin_token, plan, n}` owner 生成激活码 |

认证：浏览器首次访问生成 `flux_token` cookie 作为身份；激活码兑换套餐；配额按自然月图片数。

## 公网隧道

`flux.zhuanlu.xyz` 挂到**本地 xhs-tunnel**（`C:\Users\Dancing\.cloudflared\config.yml`，tunnel `27da88b4`）：
```yaml
ingress:
  - hostname: xhs.zhuanlu.xyz
    service: http://localhost:8800
  - hostname: flux.zhuanlu.xyz
    service: http://localhost:9620
  - service: http_status:404
```
DNS 路由：`cloudflared tunnel route dns xhs-tunnel flux.zhuanlu.xyz`
重启隧道：`cloudflared.exe tunnel run xhs-tunnel`（遵守双隧道约定，不新建隧道抢道）

## 配置（manager/.env）

| 变量 | 说明 |
|------|------|
| `WEB_PORT` | web 端口（默认 9620） |
| `WEB_ADMIN_TOKEN` | owner 管理 token（生成激活码用） |
| `FEISHU_APP_ID/SECRET/OWNER_OPEN_ID` | 飞书通知 |

## 验证

- 本地：`localhost:9620` 提交提示词 → 排队 → 生成 → 看图/下载
- 配额：admin 生成激活码 → 用户兑换 → 提交 → 配额扣减 → 超限拒绝
- 排队：连发多任务 → FIFO 串行（GPU 单卡）+ 状态轮询
- 公网：`flux.zhuanlu.xyz` 访问提交页
- 服务器 down：worker 标 `[SERVER_DOWN]` 重排队（3 次），健康监控飞书通知开机