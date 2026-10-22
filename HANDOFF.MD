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
