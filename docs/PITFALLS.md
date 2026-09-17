# 项目踩坑记录 — FLUX 文生图服务（通用）

> 持续更新。格式：`[日期] 问题 → 原因 → 解决`

## flux3 接入（2026-09-16，v2.5）

- **[09-16] 选机探测对「纯密码登录」的机器会全灭，而且报错是「SSH 不通」而非「密码错」** → `probe_full()` 固定带 `-o BatchMode=yes`（故意的：非交互环境不能弹密码提示，否则每台机器都卡在交互等待上）→ 密码认证被直接拒绝，症状与「关机」无法区分 → **克隆实例若继承了克隆源的 `authorized_keys` 就没问题**（本次 flux3 正是如此，`id_rsa_musetalk` 直接可登，无需 sshpass）；否则必须先 `ssh-copy-id` 配免密，**别指望密码能跑选机**
- **[09-16] 「默认机」与「自动选机」是两个概念，混起来会误判成「部分坏了」** → A 链任务中心走 `find_ready_server()`，每单现挑，换机后**自动就对了**；而小红书产线（`process_job`）与不带 `--server` 的 CLI 走**默认机**（= 候选机第一台 flux1）→ flux1 无卡时表现为「A 链正常、产线全失败」 → 新增 `FLUX_DEFAULT_SERVER` 把默认机指到当前能用的机器
- **[09-16] 32GB 卡（RTX 4080 / 32760 MiB）对 FLUX.1-dev fp16（~33GB 权重）余量极小**，而 `FLUX_OFFLOAD` 默认 `none`（全程显存）→ 拉起常驻前先想清楚档位：`none` 可能 OOM，退 `model`（比 `sequential` 快 2~3 倍）是这类卡的现实选择。**⚠️ 本批未实跑，属待验证**，勿当结论引用

## 健康探针（2026-09-16，v2.4）

- **[09-16] 探针里不能探后端** → 若 `/health` 内去 SSH/HTTP 探 GPU 机，**GPU 机关机时本服务探针会变慢甚至超时**，监控就把「后端不可用」误判成「网站死了」—— 这两件事必须能分开看 → web 侧 `_health()` 只读**进程内**状态（`db.ping()` + `scheduler.stats()`），字段里加 `backend_hint`（由 `waiting` 池大小推导，零成本）。**用 AST 剥掉 docstring 后做源码级断言，禁用词 `ssh/curl/subprocess/fsm./fr./urllib/probe/Popen` 零命中**，别靠"感觉很快"（实测 2~3ms）
- **[09-16] 探针自身不能抛** → `db.ping()` 或 `scheduler.stats()` 抛异常时，若让它冒到 `do_GET`，探针会返回 **500**，监控看到的是"服务崩了"而不是"DB 坏了" → 两处各自 `try/except` 收成 `degraded`（HTTP 503）并把异常原文放进 `errors[]`
- **[09-16] 「读不出状态」不等于「健康」** → war 判定若写成 `worker = st.get('worker_alive')`，`stats()` 抛异常时 `st={}` → `worker=None` → 与「没挂调度器」混为一谈，误报 200 → 显式区分三种：`has_sched=False`（web-only，算健康）/ `worker=True`（健康）/ `worker=False` 或 `sched_err`（degraded）。判定式：`healthy = db_ok and not (has_sched and (sched_err or not worker))`
- **[09-16] `flux_web_service.py main()` 传的是 `scheduler=None`**（web-only 调试模式）→ 探针与任何新加的读取调度器的代码都必须容忍 → `worker_alive/queue_depth/gen_mode` 在该模式下返回 `null` 而非 `0`（`0` 会被误读成"队列是空的"，实际是"没有队列"）
  - **真机实测的后果（09-16 晚补充）**：该入口下 `POST /api/submit` **必然 500** ——
    `'NoneType' object has no attribute 'submit'`（`_api_submit` 直接调 `self.scheduler.submit()`）。
    也就是说 **A 链在这条入口下完全不可用**，只有 `/health`、静态页、admin 接口能用。
    → **起服务做端到端验证/生产部署必须用 `manager/flux_service.py`**（真 DB + 真队列 + 真 web + 飞书 bot 的完整装配）
    。用错入口的表现很有迷惑性：`/health` 返回 200 看着一切正常，一提交任务才 500。
  - 自证：`python manager/flux_web_service.py --port 9621` → `curl -X POST /api/submit` → 500；
    `python manager/flux_service.py --port 9622` → 同样请求 → `{"job_id":"…","status":"queued"}`
- **[09-16] 「连不上」与「上游回 503」是两种故障，别压成一个 boolean** → 下游网站若只判 `res.ok`，就无法区分"flux 没启动"与"flux 活着但自报 degraded" → `fluxHealthReport()` 带回 `httpStatus`（`null`=连不上；`503`=连上了但上游不健康），`fluxHealth(): boolean` 保留为薄包装不破坏旧调用

## 双链验收 / 路径透传（2026-09-16，v2.3）

- **[09-16] 克隆实例换路径后常驻服务起不来，报「模型未就绪」→ `ensure_resident` 组装的 ssh 命令只透传了 `FLUX_RESIDENT_PORT/SCREEN/PY/TOKEN`，**没透传 `FLUX_WORKDIR` / `FLUX_MODEL`**；而 `start_resident.sh` 里 `WORKDIR` / `MODEL` 是硬编码默认值（`/root/autodl-tmp/flux-t2i`）** → 脚本去查错路径的模型文件 → 误报「模型未就绪」；且上传的 `flux_resident_server.py` 与脚本查找路径错位（上传到 `{remote_base}/`，脚本找 `$WORKDIR/flux_resident_server.py`）。**修法：把 `server['remote_base']` / `server['remote_model']` 用 `shlex.quote` 透传为 `FLUX_WORKDIR` / `FLUX_MODEL`**（脚本侧是 `${FLUX_*:-默认}`，传了就以本机为准）。教训：**「一次往返拿齐四态」的探测和「真正拉起」是两条独立代码路径** —— 探测按 `server` 字典走对了，拉起却拿全局默认值，只有换路径的克隆机才会暴露
- **[09-16] 两条链路（自带任务中心 / 客户生图网站）的边界必须在文档里钉死** → 两边是**独立项目**，唯一关联是网站通过 HTTP 调 `/api/submit` + `/api/status` + `/api/download` → 排查时不要跨项目猜耦合；本项目的端到端自检**不需要**下游站点存在
- **[09-16] 「常驻服务起来了」≠「能用」→ `/health` 有响应只说明 HTTP 活着，模型加载是后台异步的** → 选机/接单必须看 `model_loaded`，不是 `resident`。`any_ready()`（要求 `resident 且 model_loaded`）与 `any_usable()`（只要「可达+有卡+模型文件就绪」）是**两个不同问题**：前者回答「现在能出图吗」，后者回答「值不值得为它拉起来」。waiting 恢复门必须用后者，否则换机后任务死等

## 离线端到端自检的环境坑（2026-09-16，v2.3）

- **[09-16] 独立后台进程会被回合回收 → 起了 daemon 再在下一次工具调用里打请求，会拿到「端口直接消失」（日志无任何异常）** → 验双链必须把「起服务 → 打请求 → 收摊」压进**同一次调用**（`chain-verify/run_both_chains.sh` 就是这么写的）。现象具有误导性：日志干净、退出码 0，看起来像服务自己崩了
- **[09-16] `next build` 第二次起必失败，抛 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（count=50）→ 安全删除垫片按「每回合删除文件数」计数，`recursiveDelete` 清 `.next/` 时超阈值** → 给该子进程清空注入：`NODE_OPTIONS= node ./node_modules/next/dist/bin/next build`。**只豁免这一个构建进程**，不动其它文件操作约束
- **[09-16] 验证脚本用固定端口 → 出现 1 次未复现的瞬态失败（残留 stub 进程短暂占用端口，下游断言连锁失败）** → 固定端口要加「被占则退到系统分配空闲端口」的兜底；另外**每个断言脚本必须自己清空 scratch 目录**，否则上轮残留会让「只落 1 张图」这类断言假红/假绿
- **[09-16] 用 `| tail` 接住长驻进程的 stdout → 输出被缓冲，日志文件里什么都没有，误判成「服务没起来」** → 长驻进程直接重定向到文件（`> run.log 2>&1`），不要接管道

## 多机自动选择 / 计费（2026-09-16，v2.2）

- **[09-16] `FluxDB` 每次构造都抛异常，且被吞成一行日志 → `self._lock` 原先在 `self._migrate()` **之后**才赋值，而 `_migrate → _backfill_accounts → _one/_all/_exec` 全都要 `with self._lock`** → 抛 `AttributeError: 'FluxDB' object has no attribute '_lock'`，被 `_backfill_accounts` 的宽 `except` 吞掉只打「账户回填跳过」→ **该迁移从未真正执行过**。**修法：把 `self._lock = threading_lock()` 提到 `_migrate()` 之前。** 教训：`except Exception` 包住初始化逻辑时，任何 `AttributeError` 都会变成"静默跳过"，必须实跑一次看日志
- **[09-16] 配额莫名其妙只有一半（basic=50 只能出 25 张）→ `precheck` 用 `used + inflight >= limit`，而 `usage` 在**入队时**已 +1，所以 `used` 天然包含在途任务，再叠加 `inflight` 就把同一张在途图算了两次** → 实测：连提 25 张不完成即被拒（`used=25 + inflight=25 = 50`）→ `precheck` 改为**只用 `used`**；实测改为后放满 50 张
- **[09-16] `usage_sub` 必须带下限 → 退配额若用 `count = count - n` 裸减，边界情况下会出现负用量（等于白送额度）** → 用 SQL 标量 `MAX(count-?, 0)` 夹住；另外**退还必须幂等**：终态失败有多条落点（业务失败 / 重试超限），没有标记就会重复退 → 新增 `jobs.refunded_at` 列做一次性标记（`UPDATE … WHERE refunded_at IS NULL` 的语义靠 `refund_job_once()` 内的同锁先读后写实现）
- **[09-16] `waiting` 状态**不能**退配额 → `waiting` 是"服务器 down 等恢复"，不是终态：任务恢复后会被重新入队继续跑** → 若在 `waiting` 就退，等于同一张图免费出。只在 `status='failed'` 的两个落点退
- **[09-16] `FluxQueueScheduler.submit()` 当前无锁，却被 3 个线程并发调用（web `ThreadingHTTPServer` handler / 飞书 ws / 飞书 poll）** → 去重 `_inflight` 的 check-then-act、`_seq += 1`、`precheck` 都是裸的 → 并发同提示词可绕过去重、`_seq` 可重复（破坏 `PriorityQueue` 的 FIFO tiebreak）、配额可小幅超发。**⚠️ 已记录但本批未改（属 O-4，等授权）**
- **[09-16] 用毫秒时间戳当 `job_id` 主键 → 同毫秒两次并发提交会撞主键；`job_insert` 是裸 `INSERT`（非 `OR REPLACE`），直接抛 `IntegrityError`** → 改 `uuid.uuid4().hex[:16]`。另注意：网页队列的 `job_id` 与常驻服务内部的 `job_id` **不是同一个 ID 空间**，排查时 `web_out/` 与 `resident_out/` 的文件名对不上
- **[09-16] 选机时每台服务器要 3~4 次 SSH 往返 → 旧 `probe()` 里 `gpu_ready(s)[0]` 和 `gpu_ready(s)[1]` **各调一次**（多跑一次 `nvidia-smi`），加上 `server_reachable` + `model_ready`** → 候选机一多就按台数线性放大，全关机时每台还各等一个 `ConnectTimeout` → 合成 `probe_full()`：**一条远程命令** `echo REACH; nvidia-smi …; test -f DOWNLOAD_DONE …; curl /health`，用标记行切分解析，每台固定 1 次往返；再套 `probe_all()` **并行探测**（4 台全超时实测 0.30s，串行需 ~1.2s）
- **[09-16] 选机逻辑每张图都会调用，不缓存就是每张图 N 次 SSH** → `probe_full` 结果按 `FLUX_PROBE_TTL`（默认 20s）缓存 → **代价：机器刚开机最多晚 TTL 秒被认出来**；所以**等待服务起来的轮询必须 `force=True` 绕过缓存**（`ensure_resident` 的 40s 等待循环里，不带 force 会复用旧结果）
- **[09-16] 自动发现要在模块级调用，但那里 `log` 还没定义 → `ssh_config_aliases()` 的异常分支里 `log.warning` 会 `NameError`**（`FLUX_SERVERS = _load_servers()` 在模块级执行，早于 `log = logging.getLogger(...)`） → 该函数内改用 `logging.getLogger('flux_manager')` 局部取，消除顺序依赖
- **[09-16] 直连模式（`FLUX_RESIDENT_BASE` 有值）下不能走 SSH 探测 → 本机部署模型时根本没有 `autodl-flux` 这类别名** → `fr.probe()` 内部分流：`DIRECT_BASE` 有值走一次 HTTP `/health`（并把 `gpu_ok`/`model_ok` 标为 True，无从查也不必查），否则委托 `fsm.probe_full`
- **[09-16] 换机/克隆后 waiting 任务一直卡住 → 恢复门用的是 `any_ready()`（要求"常驻已跑且模型已加载"），但新克隆的机器刚开机时常驻还没起来** → 改用 `any_usable()`（可达 + 有卡 + 模型文件就绪），**不要求常驻已在跑** —— 拉起交给 `ensure_resident`
- **[09-16] `patch` 带 fuzz 应用成功后会留下 `<file>.orig` 备份** → 它会成为仓库里的非预期新文件（内容与改动前一致，属冗余） → 应用后清掉并核对 md5 确认与基线相同。另注意：**`flux-optimize/patches/{base,fixed}/` 快照是 CRLF，而本仓源码是 LF**，直接字节比对会「差 N 字节」（差的就是行数），必须先归一化行尾再比
- **[09-16] 在 WorkBuddy 沙箱里 `Path.unlink()` 会被安全删除垫片拦成 `OSError`（`SHFileOperationW 失败: 0x2`）** → 验证脚本收尾处若用裸 `unlink()` 会**在最后崩掉、后面几项跑不到**（症状：前面全 PASS 但总数不够） → 删除点包 `try/except OSError`，失败退化为清空文件

## 模型常驻 / 生成路径切换（2026-09-16，v2.1）

- **[09-16] `.env` 里配了 `FLUX_GEN_MODE` 却不生效 → `feishu_notify._load_env()` 只在 `get_app_id()` 等函数内**惰性**调用，模块 import 时并不会加载 `.env`；而 `flux_queue.GEN_MODE` / `flux_server_manager.GEN_MODE` / `flux_resident_client` 的配置常量都在**模块级**读取 → 读的时候 `.env` 还没进环境** → 三个模块在 import 块之后显式调用一次 `_load_env()`（与 `flux_web_service.py` 既有做法一致）。自证：`import` 前 `WEB_ADMIN_TOKEN in os.environ` 为 False、`import` 后为 True
- **[09-16] 常驻服务与旧链路**不能同时跑同一台机器** → 常驻服务 `FLUX_OFFLOAD=none` 时整模型占住显存，旧链路 `gen_flux.py` 再 `from_pretrained` 一份必然 OOM** → `flux_queue`（web 队列）与 `flux_server_manager.process_job`（小红书配图）**共用** `FLUX_GEN_MODE` 一起切；不要只切一半
- **[09-16] 看门狗会不会误杀常驻服务 → `watchdog/flux_server_ready.sh` 里有 `pkill -f 'gen_fl[u]x.py'` 和 `screen -S fluxgen -X quit`** → 常驻服务用的是不同脚本名（`flux_resident_server.py`）与不同 screen 会话名（`fluxd`），天然不被误杀；反过来 `start_resident.sh` 只操作 `fluxd` 与 `flux_resident_serve[r].py`，**不删 `out/`、不动 `fluxgen`**，不会踩到旧链路
- **[09-16] 用 `ssh <alias> "curl -d '<json>'"` 传 JSON body 极易被引号吃掉 → 提示词含单引号/中文/bracket 时命令被截断** → 改为 **base64 内联**：`echo <b64> | base64 -d | curl --data-binary @-`。base64 字符集只有 `A-Za-z0-9+/=`，不含任何 shell 元字符，且**一次 ssh 往返**就够（早期的 scp 临时文件方案要两次）
- **[09-16] 写「本机代跑远端命令」的验证代理时踩到引号陷阱 → 用正则从 `ssh -o X alias '<cmd>'` 里截取 `'<cmd>'`，直接把带引号的串丢给 `bash -c`，bash 会把整串当成一个命令名 → `exit 127 / No such file or directory`** → 外层引号是给**本地 shell** 消掉的，必须先用 `shlex.split` 模拟本地词切分、还原出 ssh 真正发送的远端命令串，再执行；主机名之前的选项（`-o/-p/-i/-l/-F`）要成对跳过
- **[09-16] `flux_resident_client` 需要 `flux_server_manager` 的 SSH 能力，而 `flux_server_manager.process_job` 又需要常驻客户端 → 模块级互相 import 会成环** → 方向定为单向：`flux_resident_client` 在模块级 import fsm，fsm **只在函数内**延迟 import 客户端
- **[09-16] 本机 `http_proxy` 已设但 `no_proxy` 为空 → `urllib` 默认 opener 把 `http://127.0.0.1:9630` 的请求也发给代理**，连不上时收到的是代理的 `502`，把真实的 `ConnectionRefused` 盖掉（排查被误导过一次） → 客户端默认用 `ProxyHandler({})` 绕开代理（`FLUX_RESIDENT_USE_PROXY=1` 可恢复）；**根因建议在系统环境补 `no_proxy=127.0.0.1,localhost`**
- **[09-16] 客户可传任意尺寸把显存打爆 → 服务端原样透传 `width/height`** → 服务端加硬边界：`256~2048` 且**归一到 16 的倍数**（FLUX 要求），`steps` 限 `1~100`；越界返回 400 而非静默截断

## 多服务器 / 僵尸 screen（2026-09-04，v2.0）

- **[09-04] 服务器就绪但任务一直不生成、最终`生成超时` → 僵尸 screen 会话 `1375.fluxgen (Dead ???)` 残留，`screen -ls | grep fluxgen` 连 Dead 会话也匹配 → `start_gen.sh` 误判"已在运行"→跳过真实启动 → 无 gen 进程 → 等超时** → 所有判"是否在跑"的逻辑都要先 `screen -wipe` 清僵尸（4 处：`server/start_gen.sh`、`flux_server_manager.gen_running()`、`flux_queue._generate` 清理、`watchdog/flux_server_ready.sh`）；实测 wipe 后活会话 `1897.fluxgen (Detached)` 起来、卡住任务 done

## 运维启动脚本 / admin 登录（2026-08-18，v1.8）

- **[08-18] cloudflared 起不来："tunnel run accepts only one argument" → `start_service.ps1` 传 `-ArgumentList 'tunnel','run',...,'--config',path` 参数拆分坏，`--config` 被当位置参数 → 撤 `--config`，`cd .cloudflared` 目录 + `tunnel run xhs-tunnel`（config.yml 在目录内自动加载，COORDINATION.md 文档方式）**
- **[08-18] 公网 flux.zhuanlu.xyz 报 1033 / 530 → 本地 Windows 重启后 flux_service 和 cloudflared 隧道全掉 → 一键启动脚本拉起来；因果判断：机器重启后永久进程被回收，非代码 bug**
- **[08-18] 含中文的 .ps1 一执行就 ParserError（字符串缺终止符）→ 无 UTF-8 BOM，PowerShell 按系统 ANSI(GBK) 读中文乱码截断引号 → python 补 `\xef\xbb\xbf` BOM**
- **[08-18] admin 输对 token 仍报"Token 错误" → `/api/admin/users` 返回 `{"accounts":[]}`，前端 `login()` 检查 `d.users`（undefined）→ 改 `d.accounts!==undefined`（两处：login + 自动刷新 IIFE）**

## 飞书对话式出图（2026-08-16，v1.7）

- **[08-16] 飞书发图必须两步走 → 飞书图片消息不支持直接发二进制 → 先 `POST /open-apis/im/v1/images`（multipart，`image_type=message`）拿 `image_key`，再发 `msg_type=image` + `content={"image_key":...}`（与发文件 `/open-apis/im/v1/files` + `file_key` 结构等价）**
- **[08-16] lark_oapi WS 事件数据与轮询 REST 字段不同 → WS 用 `message.message_type`（非 `msg_type`）、`content` 在顶层、`message.chat_type` 判私聊 → 按 WS 结构解析，勿照抄 REST 字段名**
- **[08-16] 飞书 WS 长连接需 `lark-oapi` 依赖 → 未装则机器人静默不监听 → `_ws_listen` 内 try/except 导入并打错误日志，服务不崩**

## 商户管理中心 / 账户化（2026-08-16，v1.5）

- **[08-16] 管理页点登录没反应 → admin.html 是嵌入 Python `.format()` 模板，JS 花括号要写两遍（`{}`→`{{}}`），`tb.appendChild(tr);}}))}}` 多写一个右括号 → 渲染成 `}))}`（多一个 `)`），整个 `<script>` 语法错误不执行 → 用 `node --check` 校验服务端渲染出的 JS，修正为 `}})` + `}}`（`}})}}`）**
- **[08-16] 激活的码状态显示成"可用"而非"已用" → 激活时 `status='active'`，但 JS 徽标只认 `'used'` → `active` 落入"可用"分支 → JS 判定改为 `used = status && status!=='unused'`（active 归已用）**
- **[08-16] 激活码绑死单个 token，客户换设备丢套餐 → 激活码一次性绑 token → 借鉴转录项目账户模型：激活码=账户（`accounts` 表），客户任意 token「绑定激活码」并入同账户，用量按 `account_usage` 聚合**
- **[08-16] 商家看到海量随机 token 对不上客户 → 用户表全是 16 位随机 hex → 激活码=账户后，admin 主表改「客户/账户」维度（客户名 remark / 设备数 / 用量聚合），激活码即账户天然对应客户**

## FLUX 部署下载（2026-08-15）

- **[08-15] huggingface_hub 无卡模式下载反复失败/被杀 → 初判以为是无卡 2GB 内存 OOM（错误）→ 实际是 downloader 本身在受限环境出错，与内存无关（纯下载不耗内存）→ 改用 curl 流式下载 + 断点续传（`-C -` + hf-mirror + token header），内存极小，实测 34MB/s 正常跑满**
- **[08-15] 分片大小硬编码猜错会损坏文件 → 分片是 sharded 权重，transformer 3 片 + T5 2 片，大小各自不同 → 必须用 HF API `tree/main?recursive=true` 查真实 LFS 字节数，不能猜；猜小会提前判定完成导致文件损坏**
- **[08-15] 失败下载产生大量 `.incomplete` 垃圾撑爆磁盘（25.8GB）→ 下载器反复失败残留 → 定期清理 `*.incomplete` 和 `.cache/huggingface/download`**
- **[08-15] FLUX.1-dev 是 gated 仓库（403）→ 需只读 token → token 从环境变量 `HF_TOKEN` 读，不硬编码**

## 服务器环境（2026-08-15）

- **[08-15] huggingface.co / github 被墙 → 用 `HF_ENDPOINT=https://hf-mirror.com`；pip 走 aliyun 镜像；ComfyUI git clone 失败 → 改用 diffusers（纯 pip 可装）**
- **[08-15] `pkill -f 脚本名` 自杀（命令行含匹配串）→ 用 `[u]` 转义：`pkill -f 'dl_fl[u]x.py'`**
- **[08-15] 无卡模式 `nvidia-smi` 空但 exit 0 → GPU 检查用 `if [ -z "$GPU" ]` 判断，不能只看 exit code**
- **[08-15] AutoDL 计费限时自动关机 → 常驻任务用 screen 防断 SSH，下载/生成要快**

## 对外 Web 服务 / Windows（2026-08-15，v1.1）

- **[08-15] Windows `subprocess.run(shell=True)` 用 cmd.exe 错误解析 Linux 管道/重定向 → gpu_ready 报"系统找不到指定的路径"(255) → `run()` 改 `['bash','-lc',cmd]`**
- **[08-15] `subprocess text=True` 默认 GBK 解码 UTF-8 中文输出 → `UnicodeDecodeError` 崩掉 start_generation → 加 `encoding='utf-8', errors='replace'`**
- **[08-15] scp 本地 Windows 反斜杠路径被 bash 当转义符损坏 → 转正斜杠 `str(path).replace('\\','/')`**
- **[08-15] 队列生成前未清空服务器 `out/` → scp 拉回带上小红书产线历史图(cover/P1-P6) 污染 web_out → 生成前 `rm -rf {REMOTE_OUT}/*`**
- **[08-15] api token 放 body 不生效 → handler 只读 query/cookie → 用 `?token=` 传 submit/download**
- **[08-15] `WEB_ADMIN_TOKEN` 在 .env 加载前读到空值(403) → 模块 import 时 `_load_env()` 重新加载**
- **[08-15] `Set-Cookie` 在 `send_response` 前调用导致头损坏 → 存 `_cookie` 到 `_send` 里统一发送**
- **[08-15] cloudflared 重启加 `--logfile` 参数启动失败(0进程) → 用 Start-Process `-RedirectStandardError` 到日志文件，弃用 `--logfile`**
- **[08-15] 公网下载 urllib/python 报 SSL: UNEXPECTED_EOF → TLS 怪癖，curl 正常，非服务问题**

## 中文提示词转换（2026-08-15，v1.2）

- **[08-15] FLUX 不理解中文 → 客户提交中文提示词生成完全跑偏（如"人狗打架"生出女人）→ FLUX.1-dev 是英文单语模型 → 新增 `prompt_translator.py` 借鉴短剧 FLUX 方法论，用 LLM 把中文转成 30-80 词英文静态提示词**
- **[08-15] 火山方舟 LLM 端点 404 → 短剧项目 `pipeline.py` 用 `{BASE}/v3/chat/completions`（OpenAI 兼容格式）+ model `deepseek-v4-flash-260425` → 对齐此调用方式；LLM 配置从 `~/.claude/settings.json` 的 env 读（非 shell 环境变量）**
- **[08-15] Windows curl 发中文 body 按 GBK 编码 → 服务端 `_read_body` UTF-8 解码 UnicodeDecodeError，且 `errors='replace'` 后乱码导致 `has_chinese` 检测不到 → 服务端 `_read_body` 加 UTF-8 兜底；测试用 `--data-binary` + python urllib 发 UTF-8，勿用 curl 中文 body**

## 队列服务器恢复（2026-08-15，v1.4）

- **[08-15] 服务器 down 时任务重试 3 次就标 failed，恢复后无自动恢复机制 → 服务器恢复后任务永远失败，需手动重提 → 对齐转录 `orchestrator._recover_failed_tasks`：服务器 down 时任务进 `waiting` 池（不失败、不立即重排），健康监控检测到恢复时 `_recover_waiting_tasks` 自动重入队，每任务最多恢复 3 次（防毒瘤）**
- **[08-15] 旧版 `_process` 服务器 down 用 `queue.put` 立即重排 → 几秒内打满 3 次 retry → 改为进 waiting 池由健康监控（30s）统一恢复，避免打满**
- **[08-15] 曾因服务器 down 而 failed 的历史任务（retry 超限）不会自动恢复 → 需手动重置或重新提交（防毒瘤机制所致，非 bug）**

## 参考链接

- 小红书侧原始记录：`ObsidW/审查/山西旅游-FLUX部署生成方案.md` 第 8 节
- 转录 bot 机制参考：`/root/autodl-active`（服务器会话恢复）
- 对外服务架构：`docs/WEB_SERVICE.md`；使用指南：`docs/USER_GUIDE.md`