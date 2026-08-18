# 项目测试计划 — FLUX 文生图服务（通用）

> 版本：v1.8 · 2026-08-18

## 测试范围与策略

本项目主要是**运维/部署类脚本**（无单元测试框架），采用**手动冒烟测试 + 脚本自检**策略，按部署阶段分层验证。v1.1 新增对外 web 服务，测试覆盖 Web 链路 + 队列 + 配额 + 公网隧道。

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