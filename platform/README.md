# AILab 平台服务（lite / dev profile）

这是架构图里 **“平台服务 · platform”** 那一层的可运行实现，跑在本机（单节点 VM）上：

```
Portal (Next.js) ──> Platform API (FastAPI, 唯一 DB 写入方)
                         ▲        │
                         │        ├── Redis Stream ──> Agent Service ──> Broker ──> OpenSandbox / Docker 沙箱
                         │        │                         │
                         │        └── 文件队列 ──> File Worker
                         └────────── 内部 API（服务令牌）
```

当前是 **dev profile**：组件边界、API、事件流、两阶段提交和权限模型都按架构实现；
T2/T3/T4 尚未就绪的依赖用可替换适配器顶替（见文末对照表）。

## 已经跑通

- `bash scripts/dev_up.sh` 启动 API / Broker / Agent / Worker / Portal。
- `scripts/e2e_demo.py`：创建“帮我做一个 PPT” → Agent 调模型（mock）→ Broker 领沙箱 →
  沙箱内 `pptx-builder` 生成并预览 → 两阶段提交 → 下载到 31 KB 的 `.pptx`（`PK` 魔数校验通过）。
  已分别用 **Docker provider** 和 **OpenSandbox provider** 各跑通一次。
- OpenSandbox dev server（`scripts/osb_up.sh`，:8090）已验证：创建/exec/文件读写/销毁、默认拒绝出网
  （沙箱内 DNS 解析失败）、1 CPU / 1.5 GiB 资源限制。
- `scripts/e2e_file_demo.py`：上传文件 → Worker 解析（text，29 字符）→ 带 `file_id` 建 Run →
  输入注入 `/workspace/inputs` → 产出 `answer.md`。
- `pytest`：约 170 个测试，覆盖率门槛 70%；真实 Redis 和 PostgreSQL 的变体通过环境变量开启（见「测试与 CI」），CI 里两者都跑。
- Portal：`/`、`/runs/[id]`、`/files` 均返回 200，SSE 时间线可用。
- **k3s 部署**：`deploy/k8s/platform.yaml` 已实际跑通（5 个 Pod Running，API NodePort 30080，
  Broker hostNetwork 8081），k3s 版 e2e 同样产出 `ailab-demo.pptx`。

## 组件与职责

| 组件 | 代码 | 职责 | 当前适配器 |
|---|---|---|---|
| **Platform API** | `ailab_platform/api` | 身份、权限、配额、Run 生命周期、文件库、账本、事件（SSE）；**唯一 DB 写入方** | SQLite（T4 换 PostgreSQL/CNPG） |
| **Agent Service** | `ailab_platform/agent` | 消费 Run 队列；模型 tool-calling 循环；在沙箱里执行工具；交付产物 | 自研循环 + Mock LLM（T3 换 Higress/DeepSeek） |
| **Sandbox Broker** | `ailab_platform/broker` | 沙箱租约、输入注入、exec/文件代理、两阶段提交、TTL 回收；**唯一沙箱持有者** | OpenSandbox（Docker runtime）或 Docker CLI |
| **File Worker** | `ailab_platform/worker` | 消费文件队列；病毒扫描桩、文本/PDF/DOCX/PPTX/XLSX 解析、derivative 落盘 | 本地 Worker 线程（可拆独立进程） |
| **Portal** | `portal/` | Next.js 工作台：新建任务、SSE 时间线、产出下载、资料库上传 | Next.js 15 dev server |
| **共享内核** | `ailab_platform/{config,db,models,storage,queue,llm,clients,sandbox}` | 配置、数据模型、对象存储、队列、模型网关、服务间客户端 | 本地 FS / Redis / mock |

## 一次任务的链路

1. Portal `POST /v1/runs` → Platform API 校验 token、配额、输入文件，写 `runs` + `run.created`，`run_id` 入 Redis Stream `ailab:stream:runs`。
2. Agent 消费队列 → `POST /internal/runs/{id}/status` 置 running → `POST :8081/internal/leases` 领沙箱。
3. Broker 通过 provider 启沙箱（OpenSandbox 或 Docker），把输入文件注入 `/workspace/inputs`，返回 `lease_id`。
4. Agent 循环：`LLM.chat(tools)` → `exec / write_file / deliver_file` → Broker 代理到沙箱；每一步写 `agent.*` 事件。
5. `deliver_file`：Broker 读沙箱文件 → 写 `staging/...` → 调 API `POST /internal/artifacts` 校验 sha256/size → `storage.commit` 到 `runs/...` → 写 `files` + `artifacts` + `artifact.created`。
6. Portal 的 SSE 收到事件，任务结束后展示产物下载链接；Broker `DELETE /leases/{id}` 回收沙箱。

## 快速开始

```bash
cd ailab-lite/platform
bash scripts/dev_up.sh          # venv + 4 个 Python 服务 + Portal
bash scripts/status.sh          # 健康检查
.venv/bin/python scripts/e2e_demo.py
.venv/bin/python scripts/e2e_file_demo.py
```

| 服务 | 地址 |
|---|---|
| Platform API / Swagger | http://127.0.0.1:8080/docs |
| Broker | http://127.0.0.1:8081/healthz |
| Agent | http://127.0.0.1:8082/healthz |
| Worker | http://127.0.0.1:8083/healthz |
| Portal | http://127.0.0.1:3000 |

Dev token：`dev-demo-token`；服务令牌：`dev-service-token`（见 `.env.example`）。
`scripts/dev_down.sh` 停止全部进程并清理带 `ailab.lease` 标签的沙箱容器。

## 模型目录（用户选模型，不接触 Key）

模型由平台接入和管理，用户只在界面里**选**，不自带 API Key。`models.toml` 是目录：

```toml
[[models]]
id = "ailab-agent-pro"            # 用户选的、Run 和计量里记录的名字
name = "AILab 旗舰"                # 界面显示名
description = "复杂任务与工具调用，质量优先。"
provider = "openai"               # openai = 任何 OpenAI 兼容接口（DeepSeek / Higress / vLLM…）| mock
base_url = "https://api.deepseek.com"   # 上游在哪（只在服务端）
upstream_model = "deepseek-chat"        # 上游怎么称呼它
api_key_env = "AILAB_KEY_DEEPSEEK"      # Key 在哪个环境变量里；文件里永远没有 Key 本身
weight = 3                        # 计量倍率：一次调用消耗 token × weight 的额度
default = true
```

- 用户能看到的只有 `id / name / description / weight`（`GET /v1/models`）；上游地址、上游模型名、Key 的任何信息都不返回。
- 缺 Key（或 `enabled = false`）的模型对用户**隐藏**，而不是等选了再报错；没有任何可用模型时 API 启动不报错但新建 Run 会 400。
- `api_key_env` 必须以 `AILAB_KEY_` 开头，防止配置写错把 `AILAB_SERVICE_TOKEN` 之类发给上游。
- **以后接本地模型**（GPUStack + Higress）：加一个 `[[models]]`，`base_url` 指向 Higress，`upstream_model` 写网关里的别名，
  `weight` 按设计文档（旗舰 ×3、27B ×1、Chat ×0.3），界面和 Agent 代码都不用改。
- 额度按权重折算：每日/每月 Token 限额统计的是 `token × weight`；`GET /v1/usage` 返回已用/限额，Portal 工作台会显示。
- 每次 Run 走所选模型自己的上游；Run 创建后模型从目录里消失，Agent 会直接把它标为失败（不占沙箱）。
- 仍保留的通用配置：`AILAB_LLM_MAX_RETRIES`（429/5xx/网络错误重试，默认 2 次，退避 1s、2s…）、
  `AILAB_LLM_TIMEOUT_SECONDS`；`finish_reason=length` 视为失败；工具参数不是合法 JSON 时不执行工具、把原文和错误回给模型重试。
- DeepSeek 思考模型是否需要回传 `reasoning_content` 因版本而异，用 `AILAB_LLM_ECHO_REASONING` 开关（默认关）；接思考模型后按报错决定。

本机开发：`models.toml` 随仓库，Key 写在 `platform/.env` 的 `AILAB_KEY_DEEPSEEK=`。
k3s：`bash scripts/k8s_models.sh` 把目录发布成 ConfigMap `platform-models`（改目录不用重建镜像，会重启 api 和 agent），
Key 由 `AILAB_KEY_DEEPSEEK=sk-... bash scripts/k8s_secrets.sh` 写进 Secret。

## 演示（k3s 上跑着的这一套）

| 入口 | 地址 |
|---|---|
| Portal | http://127.0.0.1:3000 （工作台 / 任务详情时间线 / 资料库）；远程看用 `ssh -L 3000:127.0.0.1:3000 <host>` |
| Platform API | http://127.0.0.1:30080/docs （Bearer 令牌见下） |
| 登录令牌 | `eval "$(bash scripts/k8s_secrets.sh env)" && echo $AILAB_DEV_TOKEN`，在 Portal 登录页粘贴（Cookie 保存，页面脚本读不到） |

当前配置：两个模型可选（AILab 旗舰 = DeepSeek 上游，离线演示 = mock），沙箱 OpenSandbox dev server（Docker runtime，无网络）。
Portal 顶部横幅来自 `GET /v1/info`，显示的就是实际运行的模型和沙箱。

文档 → PPT 的完整演示（上传 → 隔离扫描 → Worker 解析 → Agent 在沙箱里读文件、生成并自检 PPT → 交付 → 下载）：

```bash
eval "$(bash scripts/k8s_secrets.sh env)"
.venv/bin/python scripts/showcase_doc.py path/to/document.md   # 产出在 <repo>/demo-output/
```

实测（2026-10-21）：一份 22 KB 的设计文档 → 7 页汇报 PPT，8 次模型调用、约 6.8 万输入 token、约 1.9 千输出 token，1 分钟左右。

## 队列与可靠性

队列是 Redis（Valkey 兼容）**Streams + 消费组**，至少一次投递：

- 消费者取出消息后，**处理完才 ack**；超过可见性超时（Agent：`AILAB_RUN_TIMEOUT_SECONDS + 60` 秒；Worker：600 秒）没 ack 的消息会被别的消费者认领，`deliveries` 加一。
- 同一 Run 的执行由 Redis 锁保证唯一（TTL = 运行超时 + 30 秒），重复消息直接丢弃。
- 一条消息投递超过 3 次（`AILAB_RUN_MAX_ATTEMPTS` / `AILAB_FILE_MAX_ATTEMPTS`）视为毒消息：Run 置 `failed`，文件保持隔离并标 `processing: error`。
- 重新认领的 Run 从头再来，事件流里会出现 `agent.recovered`（带第几次尝试）。
- API 里有后台 **reaper**（每 `AILAB_REAPER_INTERVAL_SECONDS` 秒一轮，只处理超过 `AILAB_REAPER_GRACE_SECONDS` 的记录）：
  已提交但没有队列消息的 `queued` Run / `pending` 文件重新入队；`running` 但没有 Agent 锁、也没有队列消息的 Run 置 `failed`（Redis 重启丢消息时发生）。
  多个 API 副本共用一把 Redis 锁（`ailab:lock:reaper`，不主动释放，到期自动失效），每个间隔只有一个副本执行一轮。
- 旧版的列表队列键 `ailab:queue:*` 不再使用，升级后可手动 `DEL`。
- 带真实 Redis 的队列测试：`AILAB_TEST_REDIS_URL=redis://127.0.0.1:6379/15 .venv/bin/pytest tests/test_queue.py`。

## 沙箱 provider

Broker 通过 `AILAB_SANDBOX_PROVIDER` 选择实现，Agent/API 不需要改：

| 值 | 说明 | 前置条件 |
|---|---|---|
| `docker` | 直接 `docker run`，`--network none`、1 CPU / 1.5 GiB | docker CLI + `sbx-office:lite` 镜像 |
| `opensandbox` | 走 OpenSandbox server 的生命周期/exec/文件 API，默认拒绝出网 | 先 `bash scripts/osb_up.sh`（:8090） |
| `mock` | 宿主临时目录 + subprocess，**无隔离**，仅测试 | 无 |

```bash
# 切到 OpenSandbox（OrbStack/Docker runtime 的 T2 dev 替身）
bash scripts/osb_up.sh
sed -i 's/^AILAB_SANDBOX_PROVIDER=.*/AILAB_SANDBOX_PROVIDER=opensandbox/' .env
bash scripts/dev_up.sh

# 回到 Docker
sed -i 's/^AILAB_SANDBOX_PROVIDER=.*/AILAB_SANDBOX_PROVIDER=docker/' .env
bash scripts/dev_up.sh
```

`osb_up.sh` 会在 `$HOME/osb-venv` 安装 `opensandbox-server==1.1.1`、生成 `$HOME/osb/sandbox.toml`
（端口 8090、API key `dev-osb-key`），并等待 `/health`。`scripts/osb_down.sh` 只停 server，不动平台服务。
SDK 是可选依赖：`pip install -e '.[opensandbox]'`。

## k3s 部署（已验证）

> 最省事的方式是仓库根目录的 `./deploy.sh`（下面每一步它都做了，并带冒烟测试）。以下是手动步骤，便于理解和排错。

四个 Python 服务已经用同一份镜像跑在现有单节点 k3s 上（`platform` 命名空间），Broker 用
`hostNetwork` 访问宿主机的 OpenSandbox `:8090`，API 用 NodePort `30080` 暴露给 Portal / e2e。
这与宿主 `scripts/dev_up.sh` 是两种互斥模式（8080/8081 会冲突）。

```bash
cd ailab-lite/platform
# 1) 构建并导入镜像（只需一次，代码变更后重复）
( cd .. && docker build -f platform/deploy/Dockerfile -t ailab-platform:lite . )
docker save -o /tmp/ailab-platform.tar ailab-platform:lite
sudo k3s ctr -n k8s.io images import /tmp/ailab-platform.tar

# 2) 确认 OpenSandbox dev server 在跑
bash scripts/osb_up.sh

# 3) 部署（先建随机令牌，清单里不含 Secret）
# 脚本会自动找 kubeconfig（$KUBECONFIG、~/.kube/config、/etc/rancher/k3s/k3s.yaml）
AILAB_KEY_DEEPSEEK=sk-... bash scripts/k8s_secrets.sh   # 随机令牌 + 模型 Key；已存在则不动
NO_RESTART=1 bash scripts/k8s_models.sh               # 模型目录 → ConfigMap
eval "$(bash scripts/k8s_secrets.sh env)"   # 之后的 e2e / Portal 用这两个令牌
k3s kubectl apply -f deploy/k8s/platform.yaml
k3s kubectl -n platform rollout status deploy/platform-api --timeout=180s
bash scripts/k8s_status.sh

# 4) Portal 指向 NodePort
AILAB_API_BASE=http://127.0.0.1:30080 bash scripts/portal.sh   # 打开后在登录页粘贴 $AILAB_DEV_TOKEN

# 5) 端到端
AILAB_API_BASE_URL=http://127.0.0.1:30080 .venv/bin/python scripts/e2e_demo.py   # 用的是 $AILAB_DEV_TOKEN

# 拆除
bash scripts/k8s_down.sh
```

清单里包含：`platform-api`（NodePort 30080）、`platform-broker`（hostNetwork:8081，`strategy: Recreate`）、
`platform-agent`、`platform-worker`、`platform-redis`、PVC `platform-data`（SQLite + 本地对象存储）、
ConfigMap/Secret。PVC 用 initContainer 把 `/data` chown 给 10001；镜像里已装 `opensandbox` SDK。

## API 摘要

公开（`Authorization: Bearer <token>`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/runs` | 建 Run：`{prompt, title?, file_ids?, model?}` |
| GET | `/v1/runs` | 当前用户 Run 列表 |
| GET | `/v1/runs/{id}` | Run 详情（含 artifacts / usage / input_files） |
| GET | `/v1/runs/{id}/events` | SSE（`Last-Event-ID` 或 `?after=`） |
| GET | `/v1/runs/{id}/events/history` | 事件历史（调试/测试） |
| POST | `/v1/runs/{id}/cancel` | 取消 |
| POST | `/v1/files` | multipart 上传（≤ 50 MB）；文件先是 `pending`，Worker 扫描通过才变 `ready`，之间不可下载、不可作为 Run 输入 |
| GET | `/v1/files` / `/{id}` / `/{id}/content` | 列表 / 元数据 / 下载 |
| POST | `/v1/files/presign` + `/{id}/complete` | S3 直传；本地存储返回 501 |

内部（`X-Service-Token`）：`/internal/runs/{id}`、`/status`、`/events`、`/usage`、`/artifacts`、
`/internal/files/{id}`、`/content`、`PATCH`。Broker 另有 `/internal/leases*`。

## 数据模型

`users` / `api_tokens` / `runs` / `run_events` / `files` / `artifacts` / `model_usage`。
事件按 `(run_id, seq)` 唯一；`files` 有 `version`、`parent_id`、`sha256`、`meta`；
`artifacts` 关联 Run 与 File，`files.storage_key` 指向对象存储。

## 安全边界

- **模型 Key 不进沙箱**：只有 Agent 持有模型网关凭据；沙箱只跑工具。
- **DB 单写**：Broker/Agent/Worker 通过内部 API 写账本，不直连数据库。
- **沙箱最小权限**：Docker provider 用 `--network none`、1 CPU / 1.5 GiB、非 root（10001）、
  `--cap-drop ALL`、`no-new-privileges`、`/tmp` tmpfs；OpenSandbox provider 用 `NetworkPolicy(default deny)`
  和 `resource={cpu:1, memory:1536M}`（已实测 DNS/出网失败）。沙箱存活时间 = 租约 TTL（默认 4 小时）+ 5 分钟，
  Broker 先回收，provider 自带过期兜底；单沙箱配额（`AILAB_MAX_SANDBOXES=1`），满了就排队（最多 `AILAB_SANDBOX_WAIT_SECONDS`）。
- **Broker 是唯一沙箱持有者，且自己执法**：Agent 只拿 `lease_id`。路径统一经 `paths.normalize_path` 规范化，
  只允许 `/workspace` 之内；只有 `/workspace/outputs` 能交付；读/写/交付各有大小上限；
  注入哪些输入文件由 Broker 向 API 查（不信调用方传的 `file_id`），重名输入自动改名（`a (2).txt`）。
- **交付两阶段且可重试**：Broker 写 `staging/`；API 校验 sha256/大小 → 复制到 `runs/` → 提交数据库 → 删 staging。
  同 Run、同文件名、同内容再次提交返回已有 artifact；Run 非 `running` 时拒收。Run 结束时 Agent 还会把
  `outputs/` 里没交付的文件兜底交付。
- **上传隔离**：新文件 `pending`，Worker 扫描通过才 `ready`；`rejected`/`pending` 不能下载、不能建 Run，Broker 注入前再校验一次。
  上传边读边限长（`AILAB_MAX_UPLOAD_BYTES`）。
- **Run 状态机**（`run_state.py`）：只有 queued→running→succeeded/failed/canceled 这些合法转移；终态不再变化
  （Agent 迟到的 succeeded 盖不掉用户的取消）；写入是对"读到的旧状态"的比较并交换。取消会中断正在执行的模型调用/工具并释放沙箱。
- **配额**：每用户每日/每月 Token、资料库容量（拒收的文件不计）、并发 Run、单 Run Token 上限（超限停止但保留已完成产出）。
- **默认凭据**：`AILAB_ENV` 不在 `dev/test/smoke/k3s` 时，仍使用公开默认令牌（或服务令牌短于 24 字符）则四个服务都拒绝启动。
  k3s 的令牌由 `scripts/k8s_secrets.sh` 随机生成，不写进清单。
- **令牌**：内部路由强制 `X-Service-Token`（目前所有服务共用一个）；用户 token 只存 sha256；API 只认
  `Authorization: Bearer` 头，**不接受 URL 里的 `?token=`**（会进浏览器历史和访问日志）。
- **Portal 不持有令牌**：浏览器只访问 Portal 自己的 `/api/v1/*` 同源代理（只转发 `/v1/*`，`/internal/*` 不可达）；
  用户在登录页粘贴一次令牌，服务端先向 API 校验，再写入 httpOnly、SameSite=Strict 的 Cookie，由代理在服务端加成
  `Authorization` 头。前端代码、页面源码、下载链接里都没有令牌；写操作还校验同源（`Sec-Fetch-Site`/`Origin`）。
  API 默认关闭 CORS。
- 仍需注意：内部路由（`/internal/*`）和公开路由在同一个端口；Broker 在 k3s 里用 `hostNetwork` 监听宿主所有网卡。
  两者的唯一防线是服务令牌，所以必须用随机值，并且不要把这些端口暴露到不可信网络。Portal 内置 dev token 只用于本机。

## 与架构图的对应关系

| 架构图组件 | 本仓库 | 状态 |
|---|---|---|
| Portal | `portal/` Next.js | ✅ dev |
| Platform API | `ailab_platform/api` | ✅ dev（SQLite） |
| Agent Service | `ailab_platform/agent` | ✅ 自研 tool-loop（可替换 deepagents） |
| Sandbox Broker | `ailab_platform/broker` | ✅ OpenSandbox + Docker 两个 provider |
| 文件处理 Worker | `ailab_platform/worker` | ✅ 解析/扫描桩 |
| PostgreSQL | SQLite | T4 换 CNPG |
| Valkey | 本机 Redis 6379 | ✅ 协议兼容 |
| SeaweedFS S3 | 本地目录 | T4 换 S3（`AILAB_STORAGE_BACKEND=s3`） |
| Higress / GPUStack / DeepSeek | 模型目录 `models.toml`（DeepSeek 官方 API + mock） | T3：加一条指向 Higress 的 `[[models]]` |
| OpenSandbox + gVisor | OpenSandbox dev server（Docker/runc） | ✅ API 链路；k8s 控制面 + gVisor 待 T2 |
| Keycloak | 静态 dev token | 后续接 OIDC |

## 可观测性

**请求 ID 与日志**（`ailab_platform/obs.py`）
- 每个请求有一个 ID：接受调用方的 `X-Request-ID`（只收 1~64 位字母数字和 `._-`，否则换一个新的），写进响应头和每一行日志，
  并随内部调用一路传给下一个服务（Portal → API → Broker …），一次用户操作能跨服务查到。
- Agent 处理任务时直接用 **run_id** 当关联 ID，Worker 用文件 ID：`grep "[run_xxx]"` 就能在 API、Broker、Agent 三个服务的日志里看到同一个任务的全部记录。
- `AILAB_LOG_FORMAT=json` 输出每行一个 JSON（`ts/level/service/logger/request_id/msg/exc`），默认是带 `[request_id]` 的文本。
  探针和 `/metrics` 不进访问日志（会淹没日志）。

**指标**（每个服务各有 `/metrics`，Prometheus 文本格式）
- 认证：服务令牌，`Authorization: Bearer <AILAB_SERVICE_TOKEN>`（Prometheus 抓取配置的 `authorization.credentials`）或 `X-Service-Token`；**用户令牌读不了**。
- 只放能回答运维问题的：

| 问题 | 指标 |
|---|---|
| 有没有积压？ | `ailab_queue_depth{queue}`、`ailab_runs_in_state{status}`（API） |
| 任务耗时/失败率？ | `ailab_runs_total{status}`、`ailab_run_seconds{status}`（API）、`ailab_agent_run_seconds{outcome}`（Agent） |
| 沙箱够不够、慢不慢？ | `ailab_sandbox_leases_active`、`ailab_sandbox_capacity`、`ailab_sandbox_acquire_seconds`、`ailab_sandbox_exec_seconds`、`ailab_sandbox_capacity_rejections_total`（Broker） |
| 模型调用？ | `ailab_llm_calls_total{model,outcome}`、`ailab_llm_call_seconds{model}`、`ailab_model_tokens_total{model,kind=prompt|completion|cached}` |
| 工具调用？ | `ailab_tool_calls_total{tool,ok}`、`ailab_tool_seconds{tool}` |
| 配额/重试？ | `ailab_quota_rejections_total{kind}`、`ailab_reaper_actions_total{action}`、`ailab_agent_recovered_total` |
| 文件/产出？ | `ailab_files_processed_total{outcome}`、`ailab_file_processing_seconds`、`ailab_artifact_bytes_total` |
| HTTP | `ailab_http_requests_total{service,method,route,status}`、`ailab_http_request_seconds`（首字节时间，SSE 不会被算成慢请求） |
| 内存/CPU | `process_resident_memory_bytes`、`process_cpu_seconds_total`（prometheus_client 自带） |

- **标签基数有界**：HTTP 的 `route` 是路由模板（`/v1/runs/{run_id}`），不是具体路径；工具名和模型名来自外部（模型可能编出工具名），
  不在已知集合里的一律记为 `other`；run_id、用户 ID 绝不做标签。
- 抓取配置示例（集群里还没装 Prometheus；装了以后这样接）：
```yaml
scrape_configs:
  - job_name: ailab
    authorization: {credentials_file: /etc/prometheus/ailab-service-token}   # AILAB_SERVICE_TOKEN
    metrics_path: /metrics
    static_configs:
      - targets: ["platform-api:8080", "127.0.0.1:8081", "platform-agent:8082", "platform-worker:8083"]   # 8081 是 Broker（hostNetwork）
```
- 手动看一眼：`curl -s -H "Authorization: Bearer $AILAB_SERVICE_TOKEN" http://127.0.0.1:30080/metrics | grep ailab_`

## 数据库迁移（Alembic）

表结构由迁移管理，不再用 `create_all`（它不会改已有的表）。

- API 启动时自动升级到最新版（`ailab_platform/migrate.py`）：
  全新数据库直接建表；**在迁移出现之前建出来的旧库**（有表、没有 `alembic_version`）先盖上 0001 的戳再升级，数据原样保留；
  PostgreSQL 上用咨询锁，多个副本同时启动也只会有一个在迁移。
- 改了 `models.py` 就要写迁移：`AILAB_DATABASE_URL=sqlite:////tmp/x.db .venv/bin/alembic upgrade head`，
  然后 `.venv/bin/alembic revision --autogenerate -m "说明"`，检查生成的文件再提交。
  测试 `test_migrations_and_models_do_not_drift` 会在你忘了写迁移时失败。
- 现有迁移：`0001` 初始表结构；`0002` 用量账本（见下）。
- 本地验证 PostgreSQL：`docker run -d --name pg -e POSTGRES_PASSWORD=pg -e POSTGRES_DB=ailab -p 127.0.0.1:55432:5432 postgres:16-alpine`，
  再 `AILAB_TEST_DATABASE_URL=postgresql+psycopg://postgres:pg@127.0.0.1:55432/ailab pytest`
  （整套跑在 PG 上；会清空该库，只能用一次性的库）；`AILAB_TEST_POSTGRES_URL` 指向另一个一次性库则额外跑迁移测试。

## 用量账本

每次模型调用记录 token 数、**其中被上游缓存命中的部分**、以及**调用当时**模型的倍率。额度按
`(未命中缓存的输入 + 命中缓存的输入 × 0.1 + 输出) × 记录时的倍率` 累计（`AILAB_CACHED_TOKEN_WEIGHT`）。
之后改 `models.toml` 里的倍率只影响新的调用，不会改写历史；升级到 0002 时，旧记录按它们此前实际被计的倍率冻结。

## 依赖锁定、格式与 CI

- **依赖锁定**：`pyproject.toml` 只写版本下限，实际安装的版本固定在 `platform/constraints.txt`（所有直接和间接依赖）。
  Docker 镜像和 CI 都用 `pip install -c constraints.txt …`，所以每次构建得到同一套版本。
  更新：`bash scripts/lock_deps.sh`（用 uv 重新解析并重写文件，改完跑一遍测试）；`--check` 在 CI 里确认它和 `pyproject.toml` 一致。
  Portal 用 `package-lock.json`（`npm ci`）。
- **格式与检查**：`ruff check .` 和 `ruff format --check .`（版本固定为 0.17.1）；提交前跑 `ruff format .`。
- **CI**（`.github/workflows/ci.yml`）：Python（锁文件一致性 + ruff + pytest 带覆盖率门槛，并起 Redis 服务让真实 Streams 测试也跑）、
  Portal 构建、ShellCheck。`audit.yml` 每周对锁定的依赖做已知漏洞扫描（`pip-audit`、`npm audit`）。

## 浏览器 E2E 测试（`platform/e2e/`）

`pytest tests` 只测后端；Portal 的行为靠真实浏览器测试守住（`e2e/test_portal.py` 12 个 + `e2e/test_operations.py` 5 个：四个服务各自的指标、一个任务的 ID 贯穿三个服务的日志）：匿名访问只见登录页、
错误令牌被拒、登录后回到原页面、**令牌不进页面脚本/存储/下载链接/任何 JS 包**（Cookie 为 httpOnly + SameSite=Strict）、
代理只暴露 `/v1`、跨站写请求被拒、选模型、建任务并看到 SSE 时间线跑到 `run.completed`、下载产出、
上传先隔离后可下载、感染文件被拒且无法下载、删除、退出。

```bash
cd ailab-lite/platform && .venv/bin/pip install -c constraints.txt -e '.[dev,e2e]' && .venv/bin/playwright install chromium
cd portal && npm ci && cd ..
AILAB_E2E=1 .venv/bin/pytest e2e          # 需要 Redis（默认 127.0.0.1:6379 的 db 15，用完清理）
```

它会起真实的 API/Broker/Agent/Worker（mock 沙箱和 mock 模型）和 Portal 的生产构建（在临时副本里构建，不动你的 `.next`）。
已用"故意破坏"验证过测试会报警（Cookie 去掉 httpOnly、关掉同源校验、把令牌打进前端包，各自都会失败）。
`AILAB_E2E_PORTAL_DIR` 可以指向另一份 Portal 代码（比如依赖大版本升级分支），`PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定浏览器。
大版本升级（Next、React 等）不要只看构建通过：先跑这套测试。

## 测试与 CI

```bash
cd ailab-lite/platform
.venv/bin/pytest -q --cov=ailab_platform --cov-report=term-missing
.venv/bin/ruff check .
```

覆盖率门槛 70%（`pyproject.toml` 的 `[tool.coverage.report]`）；lint 规则同样在 `pyproject.toml`
（`E/F/I/UP/RUF`，FastAPI 的 `Depends()` 默认参数是框架约定，不按 B008 报错）。
`.github/workflows/ci.yml` 在 PR 和 main 的 push 上跑 Python 测试（带 Redis service，跑真实 Streams 分支；再在 PostgreSQL 16 上整套重跑一遍并跑迁移测试）+ 覆盖率 + ruff、Portal 的
`npm ci && npm run build`，`shellcheck`（`deploy.sh`、`tests/*.sh`、`platform/scripts/*.sh`），以及上面的浏览器 E2E 任务（失败时上传服务日志）。

覆盖：健康检查、鉴权、Run 生命周期/取消、事件历史、内部服务令牌、文件上传下载、
Worker 文本解析/EICAR、Mock LLM 的 PPT 工具序列、上传隔离（pending→ready/rejected）、
Broker 租约 TTL 与过期回收（假 Redis）、技能提示词、LLM 重试、Agent 循环、队列语义、reaper、
沙箱符号链接逃逸与 Redis ack 原子性。

## 目录

```
platform/
  ailab_platform/        # 共享内核 + 4 个服务
  portal/                # Next.js
  scripts/               # dev_up/dev_down/status/e2e
  tests/                 # pytest
  .env.example
```

## 下一步（按架构顺序）

1. **T2 收口**：把 OpenSandbox dev server 换成 k8s controller + ingress-gateway + gVisor，
   加每沙箱令牌/Secure Access；Broker 只改 `AILAB_SANDBOX_PROVIDER=opensandbox` 与 `AILAB_OSB_DOMAIN`。
2. **T3**：部署 Higress + GPUStack，在 `models.toml` 里加指向 Higress 的模型（别名 = 网关路由），保留 `ailab-demo`（mock）做离线回归。
   上线前验证：思考模型是否需要 `AILAB_LLM_ECHO_REASONING`；思考模型和 prompt cache 的实际返回格式（缓存命中数已按 DeepSeek 与 OpenAI/vLLM 两种字段解析并记账）；
   按用户限流依赖网关收到 `x-user-id`（Agent 已带）。模型级访问控制（谁能用哪个模型）目前没有，目录对所有登录用户一视同仁。
3. **T4**：`AILAB_DATABASE_URL=postgresql+psycopg://...`、`AILAB_STORAGE_BACKEND=s3`，
   开启 presign 上传。数据库迁移（Alembic）已就绪并在 PostgreSQL 16 上验证：整套测试和迁移测试都在 PG 上跑过，换库只改连接串。
4. **Keycloak**：Platform API 加 OIDC 校验，去掉 dev token；给各服务分别签发令牌（现在共用一个）。
5. **多副本前**：`/internal` 拆独立端口或 NetworkPolicy（reaper 已有锁，迁移在 PG 上有咨询锁）。

## 已知限制

- Agent 是轻量 tool-loop，不是 `deepagents` 库；消息格式是 OpenAI tool-calling，替换只改 `agent/loop.py` 与依赖。
- 被接管的 Run 从头再来，没有逐步 checkpoint（设计文档里的 LangGraph checkpoint 恢复未做）。
- Broker 重启会清理所有租约/沙箱（dev 策略），生产应改为 `SandboxSync.connect` 恢复。
- OpenSandbox dev server 用 Docker/runc，不是 k8s controller/ingress-gateway；gVisor 只用于 T0/T1
  的 RuntimeClass 验证，平台沙箱还没跑在 gVisor 里。Secure Access/单沙箱令牌也还没接。
- 病毒扫描是 EICAR 桩；无 OCR，扫描件按架构先跳过。Worker 在自己的进程里解析不可信文档（PDF/Office），
  生产应放进沙箱或独立受限 Pod。
- 单文件上限默认 50 MB（设计 200 MB），上传和下载都把整个文件读进内存；大文件需要流式 + 预签名直传（T4）。
- 配额（并发 Run 数、资料库容量）的“检查 + 写入”在每用户临界区里完成（`locks.py`：进程内锁 + PostgreSQL 上 `SELECT … FOR UPDATE` 用户行），并发请求不会超限；Token 日/月额度是在建 Run 时检查，一个 Run 内部的多次调用仍可能略超（由单 Run Token 上限兜底）。
- 平台服务跑在宿主机**或** k3s（两种模式互斥）；k3s 里平台沙箱仍走宿主机上的 OpenSandbox dev server。
- `settings`、数据库 engine 在 import 时创建，所以测试要在 import 前设环境变量（`tests/conftest.py`）。
