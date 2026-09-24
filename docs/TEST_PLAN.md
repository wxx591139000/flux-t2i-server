# 项目测试计划 — FLUX 文生图服务（通用）

> 版本：v2.0 · 2026-09-04

## 测试范围与策略

本项目主要是**运维/部署类脚本**（无单元测试框架），采用**手动冒烟测试 + 脚本自检**策略，按部署阶段分层验证。v1.1 新增对外 web 服务，测试覆盖 Web 链路 + 队列 + 配额 + 公网隧道。v2.0 新增**多服务器**与 **VPS 看门狗**，测试覆盖多机选取 + 死机自愈 + 重启恢复 + 服务器 `server` 落库 + 看门狗预热。

## v2.0 新增测试范围

| 用例（已实测 ✅） | 步骤 | 预期 |
|---|---|---|
| `find_ready_server` 挑单 | `python -c "...find_ready_server()"` | 任一台可达+带卡+模型就绪返回；全关返回 None |
| 死机自愈 recovery | 服务器恢复可达后健康循环 | waiting 池任务自动重入队（实测 22:40 5 个恢复） |
| 僵尸 screen 根治 | 服务器留 Dead fluxgen 会话后启动 | `screen -wipe` 清僵尸 → 活会话重启 → 不误判"运行中" |
| 重启恢复孤儿 | 有 queued/generating 任务时重启 daemon | `_recover_orphaned_jobs` 重入队，不丢单 |
| `server` 列落库 | 任务完成后查 DB | `jobs.server='flux1'`（实测 ✅） |
| VPS 看门狗预热 | flux 机开机未就绪 → 看门狗 | 自动跑 `flux_server_ready.sh` → `SERVER_READY` 就绪（待部署后实测） |

**已知测试缺口**：flux2 未接入（clone 未通）；VPS 看门狗代码就绪未部署 vps-aliyun 实测；并发只测单 worker（无多 worker 并行）。

## 分层测试

### L1 脚本语法自检（无 GPU，本地可跑）
- `bash -n server/*.sh`：shell 语法检查
- `python -m py_compile server/gen_flux.py local/flux_gen_watchdog.py`：Python 语法
- `python -c "import json; json.load(open('example/prompts.example.json'))"`：JSON 合法

### L2 下载链路（无卡可测）
| 用例 | 步骤 | 预期 |
|---|---|---|
| curl 断点续传 | 手动中断 curl 后重跑 | 从断点续传，不重复 |
| 停滞检测 | 停网观察 | 3 次无增长后重启 curl |
| 下载完成标记 | 全部分片下完 | 生成 `DOWNLOAD_DONE` |
| 分片大小校验 | 对比 HF API LFS 字节数 | 字节数一致，勿猜 |

### L3 一键启动自检（带卡）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 无卡中止 | 无卡下跑 start_gen.sh | 提示切带卡，exit 1 |
| 模型未就绪中止 | 删 DOWNLOAD_DONE 跑 | 提示模型未就绪 |
| 幂等 | 已在跑时再跑 | 跳过，提示已运行 |
| 强制重启 | `--force` | kill 旧 fluxgen 重启 |
| screen 后台 | 断 SSH 后重连 | 任务继续，日志心跳 |

### L4 生成验证（带卡）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 单张生成 | `--start 0 --end 1` | 输出 PNG，尺寸 768×1024 |
| 断点跳过 | 再跑同任务 | 已存在文件跳过 |
| 分组输出 | 多组 notes | `NN_组名/` 子目录 |
| 结果可读 | 打开 PNG | 图像正常非花屏 |

### L5 对外 Web 服务（v1.1，本地可测）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 服务启动 | `python manager/flux_service.py` | 端口9620监听，`/health` 200 |
| 提交页渲染 | 访问 `/` | 提示词输入 + 任务表格 + Token 显示 |
| 提交任务 | `POST /api/submit?token=` | 返回 job_id，入队 |
| 状态轮询 | `GET /api/status?job_id=` | queued→generating→done |
| 生成结果 | 服务器生成 | 图片落 `web_out/<jobid>/`，status=done |
| 下载 | `GET /api/download/<id>?token=` | 200 PNG，校验归属(403) |
| 激活码 | admin 生成 → 用户兑换 | 套餐升级、配额增加 |
| 配额超限 | 用满后提交 | 拒绝，提示月限 |
| 去重 | 同 token 同提示词再提交 | 拒绝，提示重复 |
| 服务器down | 停服务器后提交 | `[SERVER_DOWN]` 重排队，飞书通知 |
| 队列串行 | 连发多任务 | FIFO 依次执行，无并发 |
| 公网隧道 | `flux.zhuanlu.xyz` | 公网提交页 + 下载 200 |

### L6 E2E 回归（v1.1，已实测通过）
- ✅ 真实提交「小孩在岸边散步」→ FLUX 服务器生成 → 拉回 → 完成任务
- ✅ 公网下载 PNG 有效（768×1024）
- ✅ 下载按钮渲染（每个 done 任务带「⬇ 下载」）

### L7 飞书"图图"对话式出图（v1.7，已实测通过）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 图片上传+回传 | `send_image_direct(owner, 测试png)` | 飞书收到图片消息（SEND OK） |
| 正常提示词 | 私聊发"一只橘猫坐在窗台上" | 确认回执（任务号+用量） |
| 在途拦截 | 任务未完成再发 | "请稍候再发" |
| 斜杠命令 | 发 `/help` | 提示语（暂不支持命令） |
| 完成回传 | 模拟 job done | 上传+发送图片+"请查收" |
| 失败回传 | 模拟 job failed | 回错误信息 |
| 服务集成 | 重启 flux_service | 日志 `🤖 飞书图图机器人已启动`，web 不受影响 |

### L8 运维启动脚本（v1.8，已实测通过）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 脚本幂等 | `-Target flux/xhs` 连点两次 | 不产生重复进程（端口各 1 个监听）|
| 分别拉起 | `-Target flux` / `-Target xhs` | FLUX(:9620) / 小红书(:8800) 各自独立启动 |
| 隧道启动 | `-Target tunnel` | cloudflared xhs-tunnel 连上，公网恢复 |
| admin 登录 | 输 `flux-admin-2026` | 进入面板（bug 修复后）|

## 已知测试缺口

- ❌ 无自动化单元测试（脚本为部署工具，未引入 pytest）
- ❌ 无 GPU 环境无法做生成回归
- ⚠️ CPU offload 下生成速度慢，批量 30 张需较长等待，未做性能基准
- ⚠️ 分片大小是硬编码，若 HF 仓库文件变更需手动更新（脚本已注释来源）
- ⚠️ 小红书产线文件队列（`flux_server_manager` 的 run()）修复后未再实测（v1.1 改动复用同一 SSH 函数）
- ⚠️ 公网下载用 urllib/python 有 TLS EOO 怪癖，curl 正常（非服务问题）

---

## 增量（2026-09-23）：本轮验证记录

### A 链全量门禁

- `py -3.11 tests/run_all.py` → **18/18 道门通过，总耗时 48.8s**（无长期红的门）

### 新增/加强的验证

| 验证对象 | 方式 | 结果 |
|---|---|---|
| `ssh_config_aliases()` 读不到配置不崩 | `test_server_registry.py` 第 [6] 组：mock `Path.exists` 抛 `PermissionError` | 45/45 全绿；变异测试拆掉防护 → 3 项变红 |
| 公网是否真的指向 3000 | **Playwright 真浏览器**抓三方 `<title>` 比对 | `公网 == 3000 → True`；截图字节数一致 |
| 商户后台口令是否轮换生效 | 直接打 `/api/admin/users`（带/不带 token）+ Playwright 注入 localStorage 登录 | 新口令 200（进入面板）/ 旧口令 403 / 无口令 403 |
| 隧道 ingress 是否正确 | `cloudflared tunnel ingress validate` + `ingress rule <url>` | validate=OK；三条规则全部匹配正确 |

### 测试缺口（诚实记录）

- **`pause`/`Read-Host` 类交互阻塞无法在管道环境下自动化验证** —— 它只在真实控制台发作。
  对策：不靠测试，改为①禁用数组 splatting（源码级不变量检查）②进度文件/结论文件留痕
- 飞书机器人侧**目前没有任何自动化测试**（现有 `feishu_bot.py` 无对应用例）；
  设计文档 P1 已要求状态机与去重先补回归测试再上功能

## 增量（2026-09-24）：第 20 道门 + T11（v2.10.0）

- `py -3.11 tests/run_all.py` → **20/20 道门通过，总耗时 77.5s**（无长期红的门）

### 新增 / 扩展的门

| 门 | 项数 | 覆盖 |
|---|---|---|
| `tests/test_feishu_bot_cancel.py`（**新增**） | 55 | 命令解析矩阵 / 命令不产生提交 / 列表与权限 / 取消副作用（墓碑 + 剔队列 + 跟踪终态 + 不退款）/ 按状态措辞 / 序号与短码边界 / 跨渠道取消 |
| `tests/test_delete_job.py`（**扩展**） | 34 → **37** | 新增 T11（第 9 条不变量）：已删 waiting 不复活 / 不退款 / 不改状态，含源码级顺序断言 |

**替身策略**：`test_feishu_bot_cancel.py` 用**真实 FluxDB + 真实 FluxQueueScheduler**
（临时库），**只有飞书通知器是替身** —— 避免"替身自证替身"，断言的才是上生产的代码。

**阳性对照**（防假绿的关键）：T11 里有一条"正常 waiting 任务**确实**被重新入队"。
没有它，一旦函数"压根没跑"，其余断言会**静默通过**。

### 变异测试（7/7 全被抓住，还原后全绿）

| 变异 | 门反应 |
|---|---|
| 命令分派被摘掉（`cmd = None`） | **25 项红**（命令全被当提示词提交） |
| 命令判据放宽（`if True: return cancel`） | 8 项红（提示词被吞） |
| 取消时不打墓碑 | 10 项红 |
| 取消时不从调度器剔除 | 4 项红 |
| `jobs_inflight_by_user` 不排除墓碑 | 4 项红 |
| 轮询去掉墓碑检测 | 4 项红 |
| **`_recover_waiting_tasks` 去掉墓碑过滤** | T11c/d/g/h/j 红 → `实际退款 [('sAlive','…'),('sDead','等待服务器超时(250分钟)')]` |

最后一条是最有价值的证据：它证明「已删任务拿到退款」这个漏洞**在修复前真实存在**。

### 夹具教训（已进 PITFALLS）

门的第一版用了 `gen00001` 这类**非十六进制**假 job_id（真实是 `uuid4().hex[:16]`），
被严格判据**正确地**判为"不是命令" → 误判成"实现有 bug"。
**实现是对的，测试数据是错的。** 写"按格式识别"的测试时，夹具必须满足格式约束。
