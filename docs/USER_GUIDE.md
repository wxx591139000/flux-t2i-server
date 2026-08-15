# FLUX 文生图服务 · 用户使用指南

> 版本：v1.0 · 2026-08-15
> 公网入口：`https://flux.zhuanlu.xyz`（本地 cloudflared 隧道 xhs-tunnel → localhost:9620）
> 服务对象：对外售卖（对标转录bot），带排队调度 + 配额计费。

## 一、普通用户（浏览器，无需安装）

### 1. 打开网址
```
https://flux.zhuanlu.xyz
```
首次访问浏览器自动生成一个 `flux_token` Cookie 作为身份标识。
页面顶部显示：`你的Token xxx · 套餐 default · 用量 0/10`。

### 2. 写提示词，点提交
- 输入框填**英文**描述（页面有示例，如 `a cute shiba inu running on a beach, cinematic lighting, photorealistic`）
- 点「提交生成」→ 提示 `已入队`，拿到任务ID
- 每张约 **85 秒**（GPU 单卡串行，多人排队时依次执行属正常）

### 3. 看结果
- 页面下方「我的任务」表格自动刷新，状态：`排队中 → 生成中 → 已完成`
- 标题栏「我的任务中心」可查看更多历史
- 完成后图片直接内嵌显示，可点开下载

### 4. 换套餐（扩容）
- owner 发给你一个**激活码** → 粘贴到「兑换激活码」框 → 激活后套餐升级、月度额度增加

## 二、owner / 管理员

### 生成激活码（给用户扩容）
需要 `WEB_ADMIN_TOKEN`（`manager/.env` 中，默认 `flux-admin-2026`）：
```bash
curl -s -X POST https://flux.zhuanlu.xyz/api/admin/gen_codes \
  -H "Content-Type: application/json" \
  -d '{"admin_token":"flux-admin-2026","plan":"basic","n":5}'
# → {"ok":true,"codes":["ABC123","..."],"plan":"basic"}
```
`plan`：`basic`(50张/月) / `pro`(200张/月)；`default`=10张/月。

### API 调用（给接了程序的用户）
```bash
# 提交任务（token 放 query 或 cookie）
curl -s -X POST "https://flux.zhuanlu.xyz/api/submit?token=用户token" \
  -H "Content-Type: application/json" -d '{"prompt":"a red fox"}'
# → {"job_id":"...","status":"queued"}

# 轮询状态
curl -s "https://flux.zhuanlu.xyz/api/status?job_id=xxx"

# 下载图片（校验归属，token 必须匹配该任务用户）
curl -s -o fox.png "https://flux.zhuanlu.xyz/api/download/xxx?token=用户token"
```

## 三、配额规则（默认生效）
- 每个 token 每月可生成图片数 = 套餐额度（default=10 / basic=50 / pro=200），**入队即扣 1 张**
- 额度用满 → 提交被拒，提示"达到月度配额"
- owner 预留 token 无限量（需额外配置）

## 四、API 端点速览
| 端点 | 说明 |
|------|------|
| `GET /` | 提交页（提示词输入 + 任务列表 + 看图 + 兑换激活码） |
| `GET /center` | 我的任务中心 |
| `GET /health` | 健康检查 |
| `POST /api/submit?token=` | `{prompt, priority?}` 提交生成任务 |
| `GET /api/status?job_id=` | 轮询任务状态 |
| `GET /api/download/<job_id>?token=` | 下载图片（校验归属） |
| `POST /api/activate` | `{code, token}` 兑换激活码 |
| `POST /api/admin/gen_codes` | `{admin_token, plan, n}` owner 生成激活码 |

## 五、维护备注（admin）
- 服务启动：`python manager/flux_service.py`（默认端口 9620）
- 公网隧道：本地 `cloudflared.exe tunnel run xhs-tunnel`（tunnel `27da88b4`，勿用旧配置抢道）
- 服务器 down：队列 worker 标 `[SERVER_DOWN]` 重排队（3次），健康监控飞书通知开机
- 服务器生成前会自动 `rm -rf out/*` 清空，避免历史图污染 web_out
- 详细架构/组件见 `docs/WEB_SERVICE.md`