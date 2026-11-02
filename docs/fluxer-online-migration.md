# V1/V2 兼容运行与在线迁移

迁移分支和集成分支使用同一兼容 runtime。迁移期可以运行数天：旧前端继续使用 V1 Discord ID/JWT 协议，新前端通过功能开关使用 V2 Fluxer Bearer 协议；两者读取和写入同一套收藏、关注、通知、标签与作品事实。`FORUM_V2_ENABLED=false` 时 V2 返回404且不启动新增消费者，不能把旧的二进制程序视为兼容 runtime。

## 部署次序

先备份数据库、导出权威身份并在隔离副本演练。默认 `FORUM_AUTH_MODE=legacy`；显式执行 `alembic upgrade fluxer_identity_prepare` 后可继续旧写入，分批 import/backfill/audit。所有实体外键移除，原 Discord 身份列保留，不把 Discord ID 复制成 Fluxer ID。权威导入允许一个账户多个历史 Discord 身份，但只有唯一当前绑定；旧版本快照不能重新激活旧身份。

```powershell
uv run python scripts/fluxer_identity_migration.py import --file authoritative-members.jsonl --batch-size 1000
uv run python scripts/fluxer_identity_migration.py backfill --batch-size 1000 --max-batches 100
uv run python scripts/fluxer_identity_migration.py audit
```

完成历史投影并确认 audit=0 后，部署本版本 API、Bot、worker 和索引器，再显式 `alembic upgrade head`。DDL使用5秒锁等待上限，锁竞争失败应重试；新增索引构建仍需提前评估大表耗时。身份 runtime 迁移要求已补齐投票代理键等前置数据，不能跳过审计。随后分批运行 `uv run python scripts/backfill_works.py`，重复至完成。生产迁移和真实用户预建不会由应用自动执行。

兼容 runtime 下 V1/V2 可同时写入。PG 同事务触发器捕获旧协议字段、关联 Work、刷新统计并保存投影任务；缺映射列进入持久 `identity_pending_projection`，后台每分钟有界补齐，映射晚到也不丢写入。初始化历史缺口仍须持续 backfill/audit，缺映射不能伪造成已迁移。CLI历史回填、持续捕获及实时新API必须使用同一数据库。

**未经升级的旧二进制必须先升级或停止其删除处理器。** 真正 Discord gateway 删除由兼容 Bot 写入不可恢复 `source_deleted` 和墓碑，保留帖子、收藏、通知、标签和关注。HTTP404/403/超时只记录同步异常，不隐藏、不判定删除。管理移除索引为可恢复 `index_removed`。关联 Work 的 Thread 物理 DELETE 直接报错并回滚整个旧事务，防止先删关系后静默破坏数据；这个保护不能让旧二进制自动获得正确的新删除语义。

## 认证与权限

V1保留原 `/v1/auth/*` 和旧Cookie/JWT；V2使用 `/v2/auth/config`、`checkauth`、`login`、`callback`、`session/claim`、POST `logout`。配置独立 `FORUM_V2_DISCORD_REDIRECT_URI`（或 `auth.redirect_uri_v2`）并在 Discord OAuth 控制台登记 `/v2/auth/callback`；V2浏览器绑定Cookie限于 `/v2/auth`，不扩大V1Cookie路径。前端V2 feature flag必须同步选择V2认证客户端。

旧JWT只允许当前Discord绑定，无旧身份宽限。统一User的历史归属可由管理组显式合并，普通绑定事件不能搬移历史身份；实际操作者、审核者、发布者保持原审计值。新API要求当前Discord正式成员，管理员权限来源当前管理组。普通读取复用30秒角色快照、单飞和共享HTTP池，换绑/撤销代次失效；敏感写入与管理操作重新核验权威会话和成员资格。NSFW关闭时详情404、列表与total过滤，深渊另需对应身份组。

## 原生内容、可靠任务与事件

配置自部署 `FLUXER_API_URL` 和 `FLUXER_WEB_URL`，以及 `config.json.work_forums` 的 `guild_id,normal,normal_nsfw,abyss,abyss_nsfw`。链接使用本站origin，不指向官方公共站。全部新主帖/子帖由Fluxer原生创建；Discord作品的新讨论区仍为Fluxer，绝不伪造本地帖子。

PG WorkTask是可靠事实，Redis仅调度/缓存。网络任务持久operation_id，超时重试同一次操作；分类保存期望状态再异步移动，旧移动按持久次序收敛。创建子帖携带最多100条完整处罚快照，超过上限明确拒绝，不截断。新子帖初始化时原生锁住并归档，全部处罚任务成功后只释放初始化来源；失败保留同步中及可诊断错误。DC作品禁言仅作用于Forum/Fluxer讨论区，不操作Discord主帖。

图片使用原生预上传附件：创建只接受新上传描述，编辑可混合保留附件ID和新描述，顺序决定首图封面；Forum不存登录Bearer，不用URL元数据冒充原生图片更新。新作品先返回202及task_id，原生来源映射建立前不在公共列表出现。原生修改完成后镜像标题、正文与图片；Discord来源作品的本地标题、简介与图片编辑只覆盖作品元数据，保留来源正文，不跨平台修改Discord原帖，后续同步不清除显式覆盖。作品统计镜像主帖，不累计子帖。

部署管理员预建 `ODYSSEIA_CONTENT/forum-index` 与 `ODYSSEIA_AUTH/forum-auth`。CONTENT subjects=`events.content.v1.>`；AUTH包括 `events.auth.v1.>` 与 `events.identity.v1.binding_changed`。Forum仅拉取/ACK和读取AUTH stream INFO，不授予建流或任意发布权限。事件仅PG提交后ACK，独立投影版本与永久event_id/来源墓碑抵抗重复、乱序和删除先到。认证durable启动、重建、ack回退、保留缺口均轮换缓存代次，不能只依赖Core即时消息；断线/连续性不可验证时拒绝缓存授权。跨服失效不是零延迟承诺。

## 验证与回退边界

`TEST_WORK_DB_URL` 必须显式指定隔离PG；`FLUXER_MIGRATION_TEST_URL` 使用隔离schema；真实NATS测试仅显式临时配置。不要把测试指向生产服务。生产依赖、真实OAuth回调登记和灰度部署仍需部署方完成。身份历史/作品数据已有后不做破坏性自动downgrade，应关闭灰度入口并前向修复；不得删旧身份列或审计数据。

## 正式成员离线预建与权威导出

未找到等价批量工具，因此新增 `scripts/fluxer_prebuild_members.py`。本次只实现和测试工具，未执行真实建号。它不抓取 Discord，也不从浏览器接收账户 ID；输入必须由可信同步 Bot 导出、已经验证正式资格的 UTF-8 JSONL。每行格式如下，ID 为无前导零的十进制字符串：

```json
{"discord_user_id":"123456789012345678","formal_member":true,"verified_at":"2026-10-29T01:00:00Z","profile":{"username":"discord_name","display_name":"显示名","avatar_url":null}}
```

默认要求验证时间不超过 24 小时，可通过显式 `--max-age-hours` 调整到已批准的快照窗口。`formal_member` 必须严格为 true，头像只能来自该 Discord 用户自己的 CDN 路径。任何坏行、重复主体或资格失败都会在第一次 RPC 前阻断整个输入快照。

Windows PowerShell 示例：

```powershell
uv run python scripts/fluxer_prebuild_members.py --input verified-formal-members.jsonl --output authoritative-members.jsonl
$env:INTEGRATION_NATS_URL = "nats://integration-broker:4222"
$env:INTEGRATION_BOT_PASSWORD = "独立 discord-sync 密码"
uv run python scripts/fluxer_prebuild_members.py --input verified-formal-members.jsonl --output authoritative-members.jsonl --state member-prebuild.state.sqlite3 --concurrency 8 --batch-size 100 --execute
```

不带 `--execute` 时仅 dry-run 校验，不连接 NATS、不建号、不生成日志或输出。执行模式强制使用 `discord-sync` 账号和 `_INBOX.bot`，只调用 ensure_discord、批量 lookup_discord 与 profiles，不能铸造 token。每个 Discord 主体的 operation_id 由固定命名空间和主体生成，与文件路径、行顺序、重试次数无关。ensure 采用 25 秒有界超时，可重试错误最多重试三次；默认并发八个，最大三十二个，批量最多一百条。

每条 ensure 成功立即以 SQLite FULL 同步事务记录，Windows 操作系统锁阻止并发运行同一日志，崩溃会自动释放锁。资料查询失败后重跑会先批量查现有权威绑定，避免重复 ensure；版本必须来自权威 lookup，用户名/显示名/头像来自 Fluxer 原生 profiles，绝不把 Discord ID 当 Fluxer ID。输出只含现有 import 工具需要的六个字段，通过临时文件 fsync 后原子替换；文件被占用时旧快照保持完整，成功日志仍可恢复。

只有退出码 0 且 `ready:true` 才可把输出交给 `fluxer_identity_migration.py import`；失败返回非零，输出仅含本轮完整成功记录，不代表正式成员已全量建号。缺失的历史作者或书单所有者映射仍会阻止后续审计/切换，不能通过伪造正式资格或复制 ID 绕过。保留输入、SQLite 状态日志与权威输出以备审计，日志和输出均不包含登录 token。
