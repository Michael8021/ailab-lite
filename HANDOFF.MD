# HANDOFF

## 开始新任务

- 先读本文，再检查 `git status --short --branch`；保留已有修改。
- Python 设置、engine 和目录在 import 时初始化。测试入口必须先设置隔离环境，参见 `platform/tests/conftest.py`；不要使用开发者 `.env` 或真实数据库跑清库测试。
- `AILAB_TEST_DATABASE_URL` 会清空指定 PostgreSQL 的 public schema。只传一次性数据库。
- 本轮完整审查记录：`docs/reviews/2026-10-23-full-review.md`。

## 2026-10-23 全量审查踩坑与约束

- 预签名 PUT 在完成上传后仍能重用。完成必须先冻结独立对象，再哈希/扫描；不能把已签名的原始 key 作为最终可信对象。
- 普通上传、预签名 complete、沙箱产物都计入资料库配额。检查和写入需在同一个 `user_critical_section` 中；幂等重试先查已存在记录，再检查额度。锁内刷新此前加载的 ORM 用户/文件。
- `users.settings` 是整份 JSON 写入。开关更新必须串行化，不能让多个请求用同一个旧副本各写一次。
- Agent 的 token 上限必须在每个响应记账之后、执行工具之前检查，也涵盖最后回答、收尾回答和恢复执行已有用量。不能只在工具循环底部检查。
- SQLite 读回 datetime 会丢 tzinfo。对外时间统一从 UTCModel schema 输出明确 UTC，否则亚洲时区会误显示 8 小时前。
- Docker `.Id` 是配置 digest；`ctr images ls` 展示 manifest digest。镜像导入后用 CRI inspecti 的配置 ID 比较。`kubectl apply` 失败必须立即中止。
- Kubernetes readiness 必须探测 readyz，消费者退出或依赖故障返回 503。Broker provider 健康检查每次只执行一次；清单探针超时需覆盖外部调用。
- 浏览器上传请求进行中也要禁止发送；扫描轮询的旧响应不能覆盖/复活已删除附件。对话详情请求需防乱序，流需在新轮替换和卸载时取消。
- S3 Body 和浏览器 ReadableStream reader 都要在 finally 中释放；204 不应解析 JSON。

## 验证

Python 3.12，按 constraints 安装 `.[dev,postgres,e2e]`；Portal 使用 `npm ci`。Linux CI 运行完整测试和 GNU 沙箱命令。

```bash
cd platform
.venv/bin/ruff check .
.venv/bin/ruff format --check .
AILAB_TEST_REDIS_URL=redis://127.0.0.1:16379/15 .venv/bin/pytest tests -ra --cov=ailab_platform --cov-report=term-missing
# 一次性 PostgreSQL 16 数据库，端口仅举例；这两个库都会被测试改写
AILAB_TEST_REDIS_URL=redis://127.0.0.1:16379/14 \
AILAB_TEST_DATABASE_URL=postgresql+psycopg://ailab@127.0.0.1:15432/ailab_review \
AILAB_TEST_POSTGRES_URL=postgresql+psycopg://ailab@127.0.0.1:15432/ailab_migrations .venv/bin/pytest tests -ra
AILAB_E2E=1 AILAB_TEST_REDIS_URL=redis://127.0.0.1:16379/13 .venv/bin/pytest e2e -ra
cd portal
npm test
npm run build
```

仓库根目录另跑 `shellcheck -S warning deploy.sh tests/*.sh platform/scripts/*.sh`、`bash -n deploy.sh`，platform 中跑 `bash scripts/lock_deps.sh --check`。

macOS 的 mock 沙箱使用宿主 shell，需 GNU coreutils/findutils（stat -c、find -printf、touch -d）；运行时在 PATH 前加入相应 `libexec/gnubin`。预览失败用例刻意排除 LibreOffice，也必须保留选中的 GNU find。Prometheus 默认 process 指标依赖 Linux /proc，不能在 macOS 上强行断言；Linux 测试仍断言该指标。

浏览器 E2E 按仓库既有 Playwright 流程运行。人工浏览器检查优先 T3 内置 preview。E2E 使用临时副本构建 Portal，并自动结束子服务；不要把它当开发服务器复用。

## 2026-10-23 项目与资料库后端

- `0008` 给 conversations/files 添加项目外键时沿用 `0004` 的 SQLite 直接 `ALTER TABLE ... ADD COLUMN ... REFERENCES`；不要 batch 重建被 runs/artifacts 引用的表。带现存引用数据和外键开启的升级、降级、再升级已加入迁移测试。
- 项目删除与上传、预签名、产物登记、移动使用同一个 `user_critical_section`。`transition_run(..., commit=False)` 连同事件参与外层事务，避免提前提交释放 PostgreSQL 用户锁；一般调用仍默认自行提交。发消息在锁内刷新 conversation，防止等待期间项目已被删除。
- 单文件、批量和项目删除统一走 `library.delete_file_rows`，提交后才清理主对象、derived_key、text_key；项目删除还要将软删对话的 project_id 清空并解除外部文件的 parent_id。
- ZIP 使用非 seekable 小缓冲和 ZIP_STORED，按存储块输出；下载前关闭 DB session。当前 Starlette 不会在客户端断开时主动关闭同步生成器，`_ArchiveResponse` 在 finally 中显式关闭，且屏蔽取消以完成存储流清理。ASGI 2.3/2.4 断开测试已验证修复前红、修复后绿。
- 临时 PostgreSQL 测试实例初始化需显式 `initdb -E UTF8`；`--no-locale` 单独使用可能创建 SQL_ASCII，psycopg 返回 bytes 导致 SQLAlchemy 报 `TypeError: cannot use a string pattern on a bytes-like object`。
- 本轮验证：GNU coreutils/findutils 在 PATH；SQLite 全套 346 passed / 8 PostgreSQL 专用项 skipped，覆盖率 82.93%；临时 PostgreSQL 16 两个独立测试库全套 354 passed / 0 skipped；Ruff check/format check 均通过。测试服务已关闭，没有 stash 或提交。

## 2026-10-23 项目与资料库前端

- 项目 = 对话 + 文件的容器。侧栏「最近」只列不属于项目的对话（`GET /v1/conversations` 不带 project_id 即只返回 project_id 为空的）；项目内对话在 `/p/<id>` 里。
- 「围绕文件对话」用 `/?files=a,b`（文件同属一个项目时为 `/p/<id>?files=`）；Chat 读取后立刻 `history.replaceState` 去掉参数，刷新不会重复附加。
- 全局 `button.danger` 有粉底：菜单/批量条里的 `.danger` 按钮必须显式 `background: none`，否则未悬停也是粉色。
- 批量接口和 ZIP 最多 200 个；「全选」在「加载更多」后可能超过，前端已拦截。
- UI 人工检查：T3 内置 preview 访问 `next dev` 会卡死主线程（127.0.0.1 下 dev 资源被 allowedDevOrigins 拦截，localhost 也会超时）。改用 `.venv` 里的 Playwright 无头 Chromium 对 `localhost` 截图；临时 API 用 `/tmp` 下独立 SQLite + `AILAB_ENV_FILE` 指向不存在的文件，不碰开发库。

## 2026-10-23 资料库缩略图与轮询（后端第二轮）

- 图片解码仅在 Worker：扫描通过后生成 `derived/{id}/thumb.webp`，文件 meta 写 `thumb_key`；WebP 480×480 内、quality 75/method 4、第一帧、EXIF 方向、保留透明度。输入封顶 20 MiB / 1600 万像素（bomb warning 也当失败），SVG/矢量、损坏、编码/存储失败都跳过缩略图，文件仍 READY。先缩小再 EXIF-transpose，避免复制完整解码帧；方形边界下结果等价。
- 沙箱产物登记仅按 MIME/扩展名判候选，不在 API 解析；入 `thumbnail_only: true` 作业。此分支只处理 READY 且缺 thumb_key 的记录，失败重试或耗尽只能丢缩略图任务，绝不能套普通 pending 文件的拒绝逻辑。产物幂等登记重试也可补入队，队列异常仅日志。
- 回填（platform 目录，使用同一数据库/存储/Redis 配置）：`AILAB_QUEUE_BACKEND=redis .venv/bin/python -m ailab_platform.worker.backfill_thumbnails --batch-size 200`。只读 DB，按 id keyset 分批入队；独立 memory 队列不能交付，CLI 明确拒绝。已完成跳过、重复消息安全；损坏/超限文件再次执行仍会重试。通用 MIME 且无图片扩展名的历史文件不自动识别，避免回填读取每份原文件。
- `FileOut.thumbnail_url` 为 `/v1/files/{id}/thumbnail` 或 null。缩略图仅 owned+READY 可取，否则 404；200 为 image/webp、private 一年 immutable、SHA256+格式版本 ETag，If-None-Match（含弱验证器/列表/*）匹配返回 304。先关 DB，再流存储，断开用共享 `_ClosingStreamingResponse` 释放流。删除统一清理 thumb_key。
- `GET /v1/files?ids=a&ids=b&limit=200`：最多 200 个 ids，未知/非本人静默忽略，与现有筛选取交集；默认 limit 仍是 50，顺序仍按创建/收藏时间。
- 0008 尚未发布，直接补收藏/图片部分索引、(user_id, project_id, created_at) 与存活项目对话计数部分索引。SQLite 的图片部分索引必须用固定 LIKE 字面量匹配，绑定 `LIKE ?` 不会命中；用户 q 仍是转义绑定参数。新增索引后必须 ANALYZE 再比较计划，否则 SQLite 的旧统计可能使 all/q 误选项目索引。完整双库 EXPLAIN 与合成负载边界见 `docs/reviews/2026-10-23-library-query-plans.md`；未加不能加速 `%q%` 的普通文件名索引。
- 验证：新增 25 项回归；SQLite 全套 371 passed / 8 PostgreSQL 专用项 skipped，覆盖率 83.03%；临时 PostgreSQL 16 两个独立测试库全套 379 passed / 0 skipped，迁移升级/降级/漂移通过；Ruff check/format、`bash scripts/lock_deps.sh --check` 通过。仍有既存 Starlette/httpx 弃用警告。未改 portal、未跑前端/E2E、未提交/stash/发布；临时 DB/Redis 服务已关闭。

## 2026-10-23 资料库第二轮（缩略图与性能）

- 侧栏是 `position: sticky`，会形成层叠上下文：在侧栏里打开的弹窗即使 `z-index` 很高也会被主区域盖住。Modal 和会话「⋯」菜单都必须 `createPortal` 到 `document.body`；会话菜单还要用 fixed 定位，否则会被侧栏滚动区裁掉。
- `lib/projects.ts` 是全页共享的项目列表缓存。读取方同时挂载时复用同一个请求；只有 `projectsChanged()` 才会在请求进行中标记过期并再拉一次。把这两者混为一谈会导致每页重复请求（生产构建实测过）。
- Portal 代理原先对所有响应强制 `no-store`，连 API 的 immutable 预览图都缓存不了。现在会透传 `cache-control`、`etag` 和 `if-none-match`；API 未声明时仍是 `no-store`。
- 缩略图像素上限分格式：JPEG 走 `draft()`，最低按 1/8 比例解码，上限 128 MP；PNG/GIF/WebP 等需要全尺寸解码，上限 16 MP（Worker pod 256 MiB）。只设一个 16 MP 上限会让 24–48 MP 的手机照片都没有缩略图。
- 前端不会为了填一个小图标去加载大图原文件：没有缩略图时只内联显示 ≤256 KB 的图片（例如 SVG），否则显示类型色块。只有打开大图查看时才加载原图。

## 2026-10-23 PR 合并检查

- fork PR 的 CI 可能显示 `action_required`，而 PR 检查列表为空、合并状态为 `UNSTABLE`；这表示工作流等待维护者批准，不代表测试已通过。协作者权限生效后，先批准已有 workflow run，再核对实际检查结果。
- #26 基于 #25。依次使用 merge commit 保留提交祖先关系，避免 squash/rebase 后后续 PR 重复展示或冲突；每次合并前重新核对 head SHA、base 和检查状态。
- GitHub App 连接器与本机 `gh` 使用不同权限。连接器合并返回 `Resource not accessible by integration` 时，先确认本机账号仍有 WRITE 权限，再通过 `gh pr merge --match-head-commit <已验证 SHA>` 合并；不要将连接器的权限错误误判为协作者权限未生效。
- 资料库改版后要同步 E2E：上传 input 是隐藏的，以可见「上传」按钮确认登录；文件行是 `.lib-row`，可用后才出现带文件名的下载链接；文件/会话删除经「更多操作」菜单；个人技能显示在资料元数据链接中。扫描 mock 必须匹配带 ids 的批量 GET 并返回数组，继续断言上传/扫描期间不能发送、移除附件后旧响应不能复活附件。
- #26 原 CI 的 6 项 E2E 失败来自上述旧定位器/旧 mock。更新后本地生产构建 + 真实服务/独立 Redis 完整 E2E：34 passed；Ruff check/format、diff check 通过。未改产品代码。
- 技能开关是乐观更新，Composer 默认按钮也先于技能 GET 完成显示。E2E 不能用开关变色或 Excel 按钮出现判断保存/加载已完成：关闭和恢复都要等待 PUT 成功，并等待被禁用的 PPT 按钮实际从 DOM 消失。临时延迟 Chat 技能响应 750ms，旧断言稳定失败（1 failed），修复后同样延迟下完整 E2E 34 passed；不要用固定 sleep 或重跑 CI 掩盖这类竞态。
