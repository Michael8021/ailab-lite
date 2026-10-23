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
  沙箱内 `pptx` 技能生成 → 两阶段提交 → 下载 `.pptx`（`PK` 魔数校验通过）。
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
| **Portal** | `portal/` | Next.js 对话界面：多轮对话、流式回答、执行步骤、PPT / Excel 实时预览、资料库 | Next.js 16 |
| **共享内核** | `ailab_platform/{config,db,models,storage,queue,llm,clients,sandbox}` | 配置、数据模型、对象存储、队列、模型网关、服务间客户端 | 本地 FS / Redis / mock |

## 对话、实时预览与办公技能

Portal 的主界面是**对话**：左边对话列表，中间消息，右边预览。同一个对话框里既能普通问答，也能让它动手做文件，
做出来的 PPT / Excel 可以接着对话修改（"第三页换成图表""再加一列占比"）。

- **一条消息 = 一个 Run**（`runs.conversation_id`）。`POST /v1/conversations/{id}/messages` 建 Run；同一对话同一时间只答一条（409）。
  Agent 拿到的 Run 里带 `history`（前面各轮的问题、回答、产出文件名，最近 12 轮）。
- **普通问答不起沙箱**：沙箱在模型第一次调用需要它的工具（exec / 读写文件 / 交付 / 搜图）时才领，纯文字回答几秒内开始出字。
- **对话 / 工作模式**（输入框左下角，浏览器记住选择）：「对话」这一轮不给模型任何工具，保证不起沙箱；「工作」把工具交给模型，
  由它判断要不要动手（`runs.options.mode`）。
- **技能按钮**（工作模式下）：「PPT 生成」「Excel 表格」「数据分析」。点了以后这条消息带 `skill`，Agent 把那份技能说明直接放进系统提示
  （不再等模型自己 `load_skill`），并明确要产出文件；只对这一条消息生效。API 校验技能名必须存在。
- **技能**（侧栏「技能」，参照 Claude.ai 的 Customize > Skills）分两类：
  - **通用技能**：随镜像发布（`images/sbx-office/skills/`），所有人可用。
  - **个人技能**：用户自己的文件，就放在他的资料库里，只有他能看到和用。格式同 Claude 的技能：一个带 `name` / `description`（可选 `title`）
    front matter 的 `SKILL.md`，或者 SKILL.md 连同脚本、素材打成的 `.zip`（SKILL.md 在根目录或唯一的文件夹里）。Worker 扫描上传文件时识别
    （`skillfiles.py`：名字只能小写字母数字和 `-`；zip 拒绝绝对路径、`..`、符号链接，最多 200 个文件、解压后 30 MB），结果记在文件 meta；
    认不出的显示原因；与通用技能重名、或有更新的同名文件时不启用。删掉资料库里的文件，技能就没了。
  - 模型的系统提示里列出通用技能和"个人技能"两组，模型自己判断何时 `load_skill`；读个人技能时 Agent 从资料库取文件（只取 API 在 Run 里
    列出的、属于这个用户的），把它的文件放进沙箱 `/workspace/.ailab/skills/<name>/`（不进工作区存档），脚本按相对路径运行。输入框里个人技能也有按钮。
  - 每个技能一个开关，按用户存在 `users.settings.disabled_skills`。关掉的：系统提示里不出现、`load_skill` 被拒绝、输入框没有按钮、
    消息指定它会被 API 拒绝。API：`GET /v1/skills`（`scope: builtin | personal`）、`PUT /v1/skills/{name}` `{enabled}`；
    上传就是 `POST /v1/files`。管理员统一下发 / 禁用技能尚未实现。
- **附件直接读**：Worker 解析上传文件时把全文存到 `derived/<file>/text.txt`；Agent 拿到的 Run 里每个附件带着抽取出的文字
  （单个最多 3 万字、合计 6 万字，超出的标明"只读了前 N 字"），放进用户消息。文本、PDF、Word、PPT、Excel 的总结和问答因此不用沙箱，
  对话模式下也能用；需要处理原文件（改表、画图）时，工作模式里文件仍在 `/workspace/inputs/`。改动前上传的文件只有 300 字摘录。
- **流式回答**：模型输出按约 0.25 秒一批写成 `agent.delta` 事件，Portal 经 SSE 逐段显示；`agent.message` 是这一步的完整文本。
- **工作区跨轮保留**：一轮结束时 Broker 把 `/workspace`（不含 inputs 和平台自己的 `.ailab/`）打包存到 `workspaces/<run>.tgz`，
  下一轮领沙箱时自动解开（只恢复同一对话最新的那份，旧的删掉；键名由 API 推导，Agent 无法指定别人的工作区）。
  所以模型能在上一轮的规格文件 `/workspace/work/deck.json` 上改，而不是从头再做。
- **实时预览**：每次 exec / write_file 之后，Broker 检查 `/workspace/outputs` 里的 `.pptx/.docx/.xlsx/.pdf` 有没有变化（大小 + 修改时间），
  有就在沙箱里用 LibreOffice 渲染（`sandbox_tools/render_preview.py`，Broker 现拷进去执行）：幻灯片 / 文档转成每页一张 JPEG，
  表格先重算公式再导出单元格值和样式。结果存到 `previews/<run>/<n>/`，`GET /v1/runs/{id}/previews/<n>/<name>` 只给 Run 的主人。
  渲染在后台进行，不挡模型下一步；Portal 收到 `preview.updated` 立即刷新右侧预览。
- **办公技能**（`images/sbx-office/skills/`，同时打进沙箱镜像和平台镜像）：系统提示里只列技能名和一句话说明，
  模型做对应任务前用 `load_skill` 读全文（普通问答不为几千字的 PPT 说明付 token）。
  - `pptx`：JSON 规格 → `build.py` → 可编辑的 .pptx。7 套配色主题（可自定义主色 / 强调色），14 种版式（封面、目录、章节、
    要点、卡片、对比、指标、大数字、原生图表、时间线、表格、引言、整页图、结尾），138 个 Lucide 图标（ISC），
    按中文字宽估算自动缩字号，放不下的输出 `WARN`。设计规则参考了开源 skill
    [claude-powerpoint-skill](https://github.com/PeterKalmstrom/claude-powerpoint-skill)（MIT）与
    [MiniMax-AI/skills](https://github.com/MiniMax-AI/skills)（MIT）的做法（思路借鉴，未拷贝代码）；
    Anthropic 官方的 pptx/xlsx skill 是专有许可，不能放进本仓库。
  - `xlsx`：JSON 规格或辅助函数 → 表头样式、数字格式、公式（`{r}` 行号占位）、合计行、条件格式、原生图表。
- **配图**：`search_images` 由 Agent 在沙箱**外**执行（沙箱仍然不联网）：查 [Openverse](https://openverse.org)
  （开放授权图片，不需要 Key；只取允许商用且允许修改的授权），下载后重新编码成纯 JPEG 再放进 `/workspace/assets/images/`，
  署名写进同目录 `credits.json`，pptx 生成器自动印在图片角上。只访问公网地址（拒绝内网 / 元数据地址，重定向也检查）。
  匿名额度约每分钟 20 次、每天 200 次，接口较慢（单次 10～20 秒），结果缓存 1 小时；`AILAB_IMAGE_SEARCH_PROVIDER=none` 关闭，
  内网部署时要么关掉、要么换成内部图库。
- **步数**：默认 20 步（k3s 24 步），还剩 3 步时提醒模型收尾；用完后再让模型（不带工具）总结进度，有产出就算成功。

实测（DeepSeek V4.1 Flash，k3s + OpenSandbox）：普通问答约 4 秒出字；"做一个 8 页汇报 PPT，要配图和图表"约 3 分钟
（大半时间在搜图）；"换成绿色系、第 3 页改成对比版式"约 35 秒；带公式和图表的三表 Excel 约 70 秒。

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

Portal 账号：`.env` 里的 `AILAB_ADMIN_USERNAME` / `AILAB_ADMIN_PASSWORD`（见 `.env.example`，首次启动时创建）；
API 令牌 `dev-demo-token`（脚本用）、服务令牌 `dev-service-token`（见 `.env.example`）。
`scripts/dev_down.sh` 停止全部进程并清理带 `ailab.lease` 标签的沙箱容器。

## 账号

Portal 用账号密码登录，新用户在登录页**自己注册**（注册完直接登录）。注册方式由 `AILAB_REGISTRATION` 决定
（`deploy.sh --registration`）：

| 模式 | 谁能注册 |
|---|---|
| `open`（默认） | 任何能打开 Portal 的人 |
| `invite` | 输入正确邀请码的人（`AILAB_INVITE_CODE`，k3s 里在 Secret；`--invite-code`）；没设邀请码等同 closed |
| `closed` | 不开放，只能由管理员用命令行建号 |

每个来源地址每小时最多 30 次注册尝试（没有反向代理传 `X-Forwarded-For` 时所有人共用 Portal 的地址，所以这只是挡脚本的闸门）。
Portal 监听公网或局域网时建议用 `invite`。

第一个账号在 API 启动时由 `AILAB_ADMIN_USERNAME` / `AILAB_ADMIN_PASSWORD` 创建（已存在则不动，
之后改环境变量不会覆盖已改过的密码）；k3s 里用户名是 `admin`，密码是 Secret `platform-secrets` 里随机生成的 `AILAB_ADMIN_PASSWORD`。
管理员用命令行管理账号（没有自助找回密码）：

```bash
python -m ailab_platform.users add alice        # 交互输入密码；--password-stdin 可从标准输入读
python -m ailab_platform.users passwd alice     # 同时让 alice 的所有会话失效
python -m ailab_platform.users disable alice    # 禁用 / enable 恢复
python -m ailab_platform.users list
# k3s：kubectl -n platform exec -it deploy/platform-api -- python -m ailab_platform.users add alice
```

用户名 3–64 位小写字母、数字和 `. _ -`，密码至少 8 个字符。登录后可在「账号设置」页自己改密码（改完所有设备要重新登录）。
API：`GET /v1/auth/options`（是否开放注册，无需登录）、`POST /v1/auth/register`、`POST /v1/auth/login`、`GET /v1/auth/me`、
`POST /v1/auth/logout`、`POST /v1/auth/password`。

## 模型目录（用户选模型，不接触 Key）

模型由平台接入和管理，用户只在界面里**选**，不自带 API Key。`models.toml` 是目录：

```toml
[[models]]
id = "deepseek-v4.1-flash"       # 用户选的、Run 和计量里记录的名字
name = "DeepSeek V4.1 Flash"      # 界面显示名
description = "速度快、成本低，适合日常任务与工具调用。"
provider = "openai"               # openai = 任何 OpenAI 兼容接口（DeepSeek / Higress / vLLM…）| mock
base_url = "https://api.deepseek.com"   # 上游在哪（只在服务端）
upstream_model = "deepseek-flash"       # 上游怎么称呼它
api_key_env = "AILAB_KEY_DEEPSEEK"      # Key 在哪个环境变量里；文件里永远没有 Key 本身
weight = 1                        # 计量倍率：一次调用消耗 token × weight 的额度
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
- DeepSeek V4.x 是思考模型：带工具的多轮对话里必须把上一轮的 `reasoning_content` 原样回传，否则上游返回 400。目录里用 `echo_reasoning = true` 按模型打开（实测 `deepseek-flash` 需要）；`AILAB_LLM_ECHO_REASONING` 是对所有模型的全局开关。

**部署时接自己的模型**：`./deploy.sh --model-base-url URL --model-name 模型名 [--model-key KEY] [--model-label 显示名] [--model-no-tools]`
生成 `platform/models.local.toml`（不进 git，之后重复部署会沿用；`--models-file platform/models.toml` 回到默认目录），
Key 存进 Secret 的 `AILAB_KEY_MAIN`。`--models-file FILE` 可以直接给一整份目录（多个模型）。本机开发时 `models.local.toml`
存在就优先于 `models.toml`。

**思考模型**：模型返回的 `reasoning_content` 会作为 `agent.thinking` 事件流式推给 Portal（"思考中…"，结束后折叠成"已深度思考 N 秒"，
内容随 Run 保存）。像 DeepSeek V4.x 这样要求把它在工具调用的多轮里**回传**的模型，平台第一次收到这类 400 时会自动打开回传
（不用手动配 `echo_reasoning`；不支持该字段的接口则一直不发）。模型不支持工具调用时在目录里写 `tools = false`，就只聊天不动手。

本机开发：`models.toml` 随仓库，Key 写在 `platform/.env` 的 `AILAB_KEY_DEEPSEEK=`。
k3s：`bash scripts/k8s_models.sh` 把目录发布成 ConfigMap `platform-models`（改目录不用重建镜像，会重启 api 和 agent），
Key 由 `AILAB_KEY_DEEPSEEK=sk-... bash scripts/k8s_secrets.sh` 写进 Secret。

## 演示（k3s 上跑着的这一套）

| 入口 | 地址 |
|---|---|
| Portal | http://127.0.0.1:3000 （工作台 / 任务详情时间线 / 资料库）；远程看用 `ssh -L 3000:127.0.0.1:3000 <host>` |
| Platform API | http://127.0.0.1:30080/docs （Bearer 令牌见下） |
| 登录账号 | 用户名 `admin`，密码 `eval "$(bash scripts/k8s_secrets.sh env)" && echo $AILAB_ADMIN_PASSWORD`（登录后会话令牌在 httpOnly Cookie 里，页面脚本读不到） |

当前配置：两个模型可选（DeepSeek V4.1 Flash = DeepSeek 官方 API 的 `deepseek-flash`，离线演示 = mock），沙箱 OpenSandbox dev server（Docker runtime，无网络）。
Portal 顶部横幅来自 `GET /v1/info`，显示的就是实际运行的模型和沙箱。

文档 → PPT 的完整演示（上传 → 隔离扫描 → Worker 解析 → Agent 在沙箱里读文件、生成并自检 PPT → 交付 → 下载）：

```bash
eval "$(bash scripts/k8s_secrets.sh env)"
.venv/bin/python scripts/showcase_doc.py path/to/document.md   # 产出在 <repo>/demo-output/
```

实测（2026-10-21）：一份 22 KB 的设计文档 → 7 页汇报 PPT，8 次模型调用、约 6.8 万输入 token、约 1.9 千输出 token，1 分钟左右。

## 队列与可靠性

栅格图片在 Worker 扫描通过后生成 WebP 缩略图（最长边 480 px，quality 75、method 4，保留透明度、应用 EXIF 方向，动画取第一帧）。
输入最多 20 MiB / 1600 万像素；SVG、损坏或超限图片跳过缩略图，文件仍可 READY。图片沙箱产物通过同一队列的
`{"file_id": "...", "thumbnail_only": true}` 单独生成；失败不会改变 READY 状态。

已有图片回填：在 `platform/` 中加载与 API/Worker 相同的数据库、存储及 Redis 配置后运行：

```bash
AILAB_QUEUE_BACKEND=redis .venv/bin/python -m ailab_platform.worker.backfill_thumbnails --batch-size 200
```

命令只读账本、按 id 分批入队（批大小 1–1000），选择 READY 且缺少 `meta.thumb_key` 的图片 MIME 或常见栅格扩展名，排除 SVG。
Worker 使用内部 API 更新元数据；已生成的跳过。可重复运行，尚未消费的重复消息也安全；队列失败记日志并以非零退出，修复后重跑。
损坏或超限图片保持缺少缩略图，再次回填会再次尝试。单独进程的 memory 队列无法交付给 Worker，命令会拒绝此配置。

`FileOut.thumbnail_url` 有缩略图时为 `/v1/files/{id}/thumbnail`，否则 `null`。此端点仅供拥有者访问 READY 文件，返回
`image/webp`、`Cache-Control: private, max-age=31536000, immutable` 和基于原始 SHA256 + 格式版本的 ETag；匹配
`If-None-Match` 返回 304，缺图/非 READY/无权限返回 404。删除文件也删除缩略图对象。
`GET /v1/files?ids=a&ids=b&limit=200` 可一次获取指定文件（最多 200 个，未知/非本人 id 忽略），与 category/project_id/q 交集筛选；
默认 limit 仍为 50，顺序仍按创建时间或收藏时间，不按 ids 输入顺序。

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
eval "$(bash scripts/k8s_secrets.sh env)"   # 之后的 e2e 脚本用 API 令牌，登录 Portal 用 $AILAB_ADMIN_PASSWORD
k3s kubectl apply -f deploy/k8s/platform.yaml
k3s kubectl -n platform rollout status deploy/platform-api --timeout=180s
bash scripts/k8s_status.sh

# 4) Portal 指向 NodePort
AILAB_API_BASE=http://127.0.0.1:30080 bash scripts/portal.sh   # 登录：admin / $AILAB_ADMIN_PASSWORD

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

对话：`GET/POST /v1/conversations`、`GET/PATCH/DELETE /v1/conversations/{id}`（详情含各轮 Run 和最新预览）、
`POST /v1/conversations/{id}/messages`（`{prompt, model?, file_ids?}`，返回这一轮的 Run）、
`GET /v1/runs/{id}/previews/{n}/{p-01.jpg | sheets.json}`。

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
- **账号密码登录**：用户在 Portal 登录页输入用户名和密码，Portal 服务端调用 `POST /v1/auth/login`，API 校验后签发**会话令牌**
  （随机、只存 sha256、默认 8 小时过期，退出登录或改密码即作废），Portal 把它写进 httpOnly、SameSite=Strict 的 Cookie。
  密码用标准库 scrypt 加盐哈希（`passwords.py`）；用户名不存在和密码错误返回同样的错误、耗时也相近；
  同一用户名 5 次失败（或同一来源 20 次失败）后 5 分钟内拒绝登录（429，进程内计数，多副本时每个副本各算各的）。
  静态 `AILAB_DEV_TOKEN` 仍是 API 令牌（脚本用），**不能**当 Portal 密码。
- **Portal 不持有令牌**：浏览器只访问 Portal 自己的 `/api/v1/*` 同源代理（只转发 `/v1/*`，`/internal/*` 不可达），
  由代理在服务端把 Cookie 里的会话令牌加成 `Authorization` 头。前端代码、页面源码、下载链接里都没有令牌或密码；
  写操作还校验同源（`Sec-Fetch-Site`/`Origin`）。API 默认关闭 CORS。
- 仍需注意：内部路由（`/internal/*`）和公开路由在同一个端口；Broker 在 k3s 里用 `hostNetwork` 监听宿主所有网卡。
  两者的唯一防线是服务令牌，所以必须用随机值，并且不要把这些端口暴露到不可信网络。API 令牌 `dev-demo-token` 只用于本机。

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
| Keycloak | 自带账号密码（`users` 表 + 会话令牌），静态 API 令牌给脚本 | 后续接 OIDC |

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
4. **Keycloak**：Platform API 加 OIDC 校验，替换自带的账号密码和静态 API 令牌；给各服务分别签发令牌（现在共用一个）。
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
