# /api/v1：按共用接口协议 v0.4.1 的接口层

给我们自己的前端（odysseia-hq/web）用的 `/api/v1`。协议权威是 odysseia-hq/api 的 `openapi/openapi.yaml`（只做 `x-phase: 1`），契约测试在同一仓库的 `contract-tests/`。

**和原有接口的关系**：V1（`/v1`，Discord 旧接口）、V2（`/v2`）、fluxer 集成全部原样保留。`/api/v1` 是加在同一个 FastAPI app 上的一层：

- 复用 V2 的服务（`work/*`、`identity/*`）和同一份数据（`user`、`work`、`author_follow`……）；
- 有自己的会话（Cookie + Redis）、错误格式、DTO；
- V2 没有对应的东西（R18 资格、屏蔽、附件、版本、tag 目录、举报审核）用新表，迁移 `alembic/versions/site_v1_schema.py` 一次性建好。

实现了协议第一期的全部接口（账号会话、作品、收藏、tag、上传、版本、下载、聊天外层、举报、审核），外加几个能用现有数据做最简实现的第二期接口（随机、相似、tag 联想、主榜、搜索）。

## 目录

```
src/site_v1/
  __init__.py        install(app)：异常处理器、中间件、路由、测试钩子、启动钩子（src/api/main.py 调用）
  config.py          账号、会话、注册等配置项（环境变量）
  files_config.py    存储、分级的配置项
  errors.py          SiteError 和快捷函数；只对 /api/v1 生效的异常处理器
  middleware.py      ASGI 中间件：建请求上下文、CSRF、X-Content-Mode、未处理异常 → 503
  session.py         Redis 服务端会话、Cookie
  context.py         SiteContext（请求上下文）、Viewer（principal）
  deps.py            Ctx / CurrentUser 依赖、数据库会话工厂、Redis
  mode.py            有效模式、分级映射、可见分级
  paging.py          不透明游标分页
  dto.py             camelCase、ID、ISO UTC、User/Me DTO、load_users 批量取作者
  membership.py      Discord 正式成员身份组检查（真实环境）
  classifier.py      图片分级适配器（stub / http / manual）和图片宽高解析
  fluxer/            FluxerPort（port.py）、RealFluxer（real.py）、FakeFluxer（fake.py）
  storage/           存储适配器：port.py（StoragePort）、local.py（本地文件 + 签名端点）、s3.py（S3 兼容 / R2）
  services/
    accounts.py          账号行、R18 资格条件
    social.py            关注 / 屏蔽
    work_access.py       作品可见性、管理权限（全 /api/v1 唯一一份）
    works.py             作品列表条件、排序、序列化、tag 变更、改分级统一入口、发布
    tag_rules.py         tag 目录和打 tag 规则（照搬 web 仓库 src/shared/tags/rules.ts）
    user_works.py        GET /users/{id}/works
    attachments.py       上传、附件对象（Attachment DTO、签名地址）、分级
    versions.py          版本、最新版本、下载计数、种子用的「版本 + 真实文件」
    chat.py              聊天外层
    chat_messages.py     聊天面板消息代转（作品交流区、私信、公共聊天区）、会话列表
    chat_channels.py     公共聊天区频道的读取约定（fluxer_channel_id）
    chat_notify.py       聊天 @ 提醒的写入点
    moderation.py        举报、审核队列、处理
    moderation_common.py 禁言、原生主帖定位、审核员看到的附件、作品摘要
  routers/           site auth me users works versions uploads tags chat reports moderation；
                     storage.py（/api/storage 本地存储签名端点，不在协议里）
  test_hooks.py      /__test__（SITE_TEST_HOOKS=1 才挂）
src/models/          site_account.py site_tag.py work_version.py attachment.py site_moderation.py；work.py 加了协议字段
scripts/seed_site_v1.py         契约测试账号、tag 目录、预置作品（带真实文件）
scripts/run_contract_tests.sh   本地跑契约测试
docs/site_v1/known-gaps.json    契约测试已知不一致清单
docs/site_v1/fluxer-rpc.md      需要 Fluxer 新增的 RPC
tests/site_v1/                  pytest
```

## 本地跑

前提：PostgreSQL 16、Redis 在跑（开发机见 `/tmp/claude-0/dev/start.sh`）。

```bash
# 1. 建库、迁移、种子
export DATABASE_URL=postgresql+asyncpg://odysseia:changeme@localhost:5432/odysseia_dev
uv run alembic upgrade head
FLUXER_FAKE=1 uv run python scripts/seed_site_v1.py

# 2. 起服务（不需要 config.json、NATS、Discord）
FLUXER_FAKE=1 SITE_REGISTRATION_MODE=open SITE_TEST_HOOKS=1 \
REDIS_URL='redis://:xunmeng123456%21@localhost:6379/0' PYTHONPATH=src \
uv run uvicorn api.main:app --port 8790

curl -i http://127.0.0.1:8790/api/v1/site
# 账号 demo / newbie / lin，密码 demo1234

# 3. 契约测试（重建 odysseia_ct 库、种子、起服务、跑 vitest，一条命令）
scripts/run_contract_tests.sh                    # 全部
scripts/run_contract_tests.sh tests/01 tests/02  # 部分文件

# 4. pytest（create_all 的测试库，FakeFluxer）
uv run pytest tests/site_v1
```

注意：`tests/conftest.py` 用 `create_all` 建表，**不会给已存在的表加列**。如果 `odysseia_test` 是这次之前建的，要先删库重建（`DROP DATABASE odysseia_test; CREATE DATABASE odysseia_test OWNER odysseia`），否则 `work` 表缺新列，旧测试插作品会失败。`TEST_WORK_DB_URL` 指向的库要 `alembic upgrade head`。

## 配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `FLUXER_FAKE` | `0` | `1`：用 FakeFluxer（账号在 `site_fake_credential`，令牌随机存 Redis），不连 NATS，也不启动 V2 的任务消费者 |
| `SITE_REGISTRATION_MODE` | `discord_only` | `open` / `discord_only`；`discord_only` 时 `POST /auth/register` 返回 403 `forbidden`，`details.reason=registration_discord_only` |
| `SITE_TERMS_VERSION` | `2026-10` | 条款版本（`/site.termsVersion`，确认 R18 资格时比对） |
| `SITE_REQUIRE_DISCORD_ROLE` | 真实 `1`，fake `0` | Discord 正式成员身份组检查开关。只影响 R18 资格的 `discord_role` 条件（以及以后需要「正式成员」的写操作），不像 V2 那样每个请求都硬性要求 |
| `SITE_RA_CONDITIONS` | 开身份组检查时 `age_confirm,discord_role`，否则 `age_confirm` | R18 资格条件 ID |
| `SITE_DISCORD_ROLE_IDS` | 空（回退 config.json `auth.role_ids`） | 正式成员身份组 |
| `SITE_COOKIE_NAME` | `odysseia_sid` | 会话 Cookie 名 |
| `SITE_COOKIE_SECURE` | `1` | Cookie 带 `Secure`。浏览器把 `http://localhost` 当安全上下文，本地一般不用关；契约测试检查这个属性（COM-07），所以脚本里保持开 |
| `SITE_SESSION_TTL` / `SITE_ANON_SESSION_TTL` | 30 天 / 1 天 | 登录会话 / 匿名会话有效期（秒） |
| `SITE_NEWCOMER_DAYS` | `5` | 站内注册账号的新人缓冲期 |
| `SITE_TEST_HOOKS` | `0` | `1` 才挂 `/__test__`（不能上生产） |
| `SITE_FRONTEND_BASE` | 空 | Discord 回调跳回的站点根（同源时留空，跳 `/login`） |
| `SITE_DISCORD_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | 空（回退 config.json `auth`；回调地址默认 `/api/v1/auth/discord/callback`） | Discord OAuth；回调地址要在 Discord 后台登记 |
| `SITE_IMAGE_MAX_BYTES` / `SITE_FILE_MAX_BYTES` / `SITE_ALLOWED_MIMES` / `SITE_MAX_TAGS` | 10 MB / 50 MB / 常见类型 / 15 | `/site.uploadLimits`、tag 上限 |
| `FLUXER_API_URL` / `FLUXER_BOT_TOKEN` / `FLUXER_API_TIMEOUT` | 空 / 空 / `10` | 聊天消息代转用的 Fluxer 原生接口（fluxer-rpc.md 第 7 节） |
| `SITE_CHAT_CACHE_TTL` | `15` | 每个聊天频道最新 50 条缓存的秒数（写操作会清） |
| `REDIS_URL` | `redis://localhost:6379/0` | 只在独立用 uvicorn 启动时用（api_main.py 已经初始化 Redis 时不读） |

上传、存储、分级（`site_v1/files_config.py`）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `SITE_STORAGE_BACKEND` | 配了 `SITE_S3_BUCKET` 是 `s3`，否则 `local` | `local`：本地文件，直传和读取走本服务的签名端点 `/api/storage/...`；`s3`：S3 兼容存储（生产 Cloudflare R2），预签名 PUT/GET |
| `SITE_STORAGE_LOCAL_DIR` | 仓库根 `data/site_uploads` | 本地存储目录（种子脚本和服务要用同一个） |
| `SITE_STORAGE_SIGNING_KEY` | 每个进程随机 | 本地存储签名密钥；多进程部署、重启后旧地址要继续有效时必须配置 |
| `SITE_PUBLIC_BASE` | 空 | 本服务对外根地址，本地存储拼绝对地址用；空时直传地址按请求 Host 拼，读取地址写相对路径 |
| `SITE_UPLOAD_TICKET_TTL` | `3600` | 直传地址有效期（秒） |
| `SITE_SIGNED_URL_TTL` | `600` | R18/R18G 图片、下载的签名地址有效期（协议：10 分钟） |
| `SITE_PUBLIC_URL_TTL` | `86400` | 全年龄图片的签名地址有效期（没配公开桶地址时） |
| `SITE_S3_ENDPOINT` | 空 | R2：`https://<account>.r2.cloudflarestorage.com` |
| `SITE_S3_REGION` | `auto` | |
| `SITE_S3_BUCKET` / `SITE_S3_ACCESS_KEY_ID` / `SITE_S3_SECRET_ACCESS_KEY` | 空 | 桶和凭证。桶的 CORS 要允许前端域名 PUT/GET 和 `Content-Type` 头 |
| `SITE_S3_PUBLIC_BASE` | 空 | 全年龄图片的公开地址前缀（R2 自定义域名）；空时全年龄图也用签名地址 |
| `SITE_CLASSIFIER` | 配了 `SITE_CLASSIFIER_URL` 是 `http`；否则 `FLUXER_FAKE` 或 `SITE_TEST_HOOKS` 时 `stub`，再否则 `manual` | `stub`：文件名带 `blocked`/`r18g`/`r18`/`suspect` 判对应结果，其余全年龄；`http`：外部分级服务；`manual`：一律「拿不准」，按 R18 处理并待人工审核 |
| `SITE_CLASSIFIER_URL` / `SITE_CLASSIFIER_TOKEN` / `SITE_CLASSIFIER_TIMEOUT` | 空 / 空 / `15` | 外部分级服务（请求和响应格式见 `classifier.HttpClassifier`）；出错按「拿不准」处理 |
| `SITE_CLASSIFIER_R18_THRESHOLD` / `_R18G_THRESHOLD` / `_CONFIDENCE` | `0.5` / `0.5` / `0.7` | 分数映射：超过阈值判 r18/r18g；最高分低于 CONFIDENCE 判 suspect |
| `SITE_CLASSIFY_INLINE_WAIT` | `3` | `complete` 最多等分级几秒，等不到返回 `processing`（前端轮询 `GET /uploads/{id}`） |

## 请求流程

1. `SiteV1Middleware`（只管 `/api/v1`）：
   - Cookie → Redis `site:sess:{sha256(sid)}`（`{user_id, csrf, token, created_at}`）→ `FluxerPort.resolve_session(token)` → `load_viewer()`（每个请求从库里现读账号、偏好、资格，**不缓存**）→ 有效模式；
   - Fluxer/Redis/数据库不可用 → 503 `temporarily_unavailable` + `Retry-After`（不会变成 401）；Fluxer 说令牌无效 → 会话降为匿名（csrf 保留）。
   - 写请求校验 `X-CSRF-Token`（缺少、不对、没有会话 → 403 `forbidden`），在路由之前。
   - 响应头补 `X-Content-Mode`（按请求结束时的有效模式，登录、切模式后跟着变）、`Cache-Control: no-store`、待写的 `Set-Cookie`。
   - 路由里没接住的异常 → 503。
2. 异常处理器（`errors.install_exception_handlers`）：`SiteError` 原样；`HTTPException`（含 V2 服务抛的）按状态码映射，5xx/504 → 503；`RequestValidationError` → 422 `validation_failed` + `details.fields`。路径不在 `/api/v1` 下的交回 FastAPI 默认处理，V1/V2 行为不变。
3. 会话：`GET /session` 没会话时建匿名会话（只存 csrf）并下发 Cookie；登录/注册时轮换 sid 和 csrf；退出删会话、注销 Fluxer 会话、清 Cookie。**Fluxer 令牌只在 Redis**，响应里从不出现。

## FluxerPort

`site_v1/fluxer/port.py` 定义 `/api/v1` 对 Fluxer 的全部依赖（Protocol 类），`get_fluxer()` 按 `FLUXER_FAKE` 返回实现，测试用 `set_fluxer()` 替换。

| 方法 | 真实实现 | 假实现 |
| --- | --- | --- |
| `password_login` | 新 RPC `rpc.auth.v1.password.login_service` | 查 `site_fake_credential`（scrypt） |
| `register` | 新 RPC `rpc.auth.v1.account.register_service` | 建 `user` + `site_fake_credential` |
| `resolve_session` | `FluxerAuthService.resolve`（复用缓存和 epoch 失效）+ 本地 user 投影 | Redis `site:fake:tok:*` |
| `revoke_session` | `rpc.auth.v1.session.revoke_current` + 会话失效事件（同 V2 退出） | 删令牌 |
| `discord_exchange` | `rpc.auth.v1.external.exchange` + user/discord_identity 投影（同 V2 回调） | 不支持（回调跳 `/login?error=discord_unavailable`） |
| `update_profile` | 新 RPC `rpc.identity.v1.profile.update_service` | 改本地 user |
| `unread_summary` | 新 RPC `rpc.content.v1.read_states.summary_service` | 空（测试可往 `unread_seed[user_id]` 塞） |
| `chat_target_info` | 新 RPC `rpc.content.v1.channel.info_service` | 作品：固定假频道（成员数 1）；私信：只认 `dm_seed` 里且查看者是成员的会话，否则不存在 |
| `chat_messages` `chat_message` `chat_send` `chat_edit` `chat_delete` `chat_react` `chat_ack` `chat_typing` `chat_dm_channels` `chat_open_dm` | Fluxer 原生 HTTP 接口（`FLUXER_API_URL`，用户令牌；删别人的消息用 `FLUXER_BOT_TOKEN`），见 fluxer-rpc.md 第 7 节 | `site_chat_message` / `site_chat_dm_channel` |

错误：`FluxerUnavailable` → 503；`InvalidCredentials` → 422 `fields.password`；`AccountExists` → 422 `fields.username|email`；`MfaRequired`、`AccountUnavailable` → 403。新 RPC 的请求、响应、错误码见 [fluxer-rpc.md](./fluxer-rpc.md)。

## 已定的语义（实现在哪）

- **有效模式**（`mode.effective_mode`）：已登录 且 `prefs.contentMode=r18` 且 `restrictedAccess.state=granted` → `r18`，否则 `general`。撤销、切换下一个请求立即生效。
- **分级映射**（`mode.work_rating` / `rating_flags`）：`is_abyss` → r18g，`is_nsfw` → r18，否则 general；反向 r18g → (is_nsfw, is_abyss) = (true, true)。
- **r18g 可见性**：有效模式 r18 且 `prefs.showR18G`（`mode.visible_ratings`），不再看 V2 的 Discord 深渊身份组。`Viewer.to_v2_principal(mode)` 给 V2 服务的 `show_nsfw` / `can_view_abyss` 也按这个算。
- **visibility**：rating≠general 或绑了 `restricts=true` 的 tag → restricted（`mode.work_visibility`）；`work.visibility` 是冗余列，改 tag/分级时要同步。
- **R18 资格**：条件见 `services/accounts.access_conditions`；`POST /me/restricted-access/confirm` 条件都满足时授予，`revoked` 不恢复；`PATCH /me/prefs` 设 r18 时检查，不满足 403 `restricted_access_required`，`details.unmet` 是条件 ID（撤销时含 `revoked`）。
- **注册模式**：`/site.features` 里是布尔值（协议要求 `features` 的值都是 boolean）：`registration`（能否站内注册）、`registrationDiscordOnly`；另外顶层加了 `registrationMode: "open" | "discord_only"`。
- **关注用户**复用 `author_follow`（和 V2 同一份事实，按 `fluxer_user_id` / `fluxer_author_id` 定位；没有 Discord 绑定的账号 Discord ID 列留空）。**屏蔽**在 `site_user_block`，屏蔽时顺带取消关注。

## 作品可见性和权限（`services/work_access.py`）

全 `/api/v1`（作品、tag、版本、下载、上传、聊天外层、举报、审核）只用这一份判断，对齐假后端 `workVisibleTo`：

- **不存在**：`deleted_at` 有值、`status=removed`、`moderation_state=removed`（`work_exists`）。审核下架 = `status=removed` 且 `deleted_at` 有值（同时走 V2 软删除）。
- **对所有人开放**：`status` 是 published / archived / unclaimed、`moderation_state` 不是 hidden、来源可见（`source_visible`，或还没建好原生主帖的 Fluxer 作品——可见性只看本地状态，原生帖没建好只影响聊天 `canOpen`）。
- 草稿、被隐藏的只有**能管理的人**看得到：作者或 `works.manage_any`（`can_manage_work`；卡贴管理员表是第二期，以后只改这一处）。
- 再按**有效模式**：分级由 `is_nsfw/is_abyss` 得出；restricted = 分级不是 general 或绑了 `restricts=true` 的 tag（现算，不依赖 `work.visibility` 冗余列，V2 改分级不会同步那一列；写操作仍会同步它）；r18g 只在有效模式 r18 且 `showR18G` 时可见。
- 看不到一律 404 `not_found`，不暴露存在。能看到但不能管理的写操作 403。
- 函数：`get_visible_work`（详情、子资源、收藏、版本、下载、聊天、举报）、`get_editable_work`（写操作）、`work_visible`（已经拿到行时）、`visible_to`（批量时，restricted tag 由 `restricted_ids` 批量查）。
- **列表**（3.12）：`services/works.listable_conditions` 是同一语义的 SQL 版，只给 `status=published`，另外过滤屏蔽 tag（`prefs.blockedTags`）和屏蔽用户；`rating` 参数只能收窄。`GET /works`、`/users/{id}/works`、`/boards/{id}`、`/search`、`/works/random`、`/works/{id}/similar` 共用。

## 作品、收藏、tag（3.2、3.4）

接口：`GET/POST /works`、`GET/PATCH/DELETE /works/{id}`、`POST /works/{id}/publish`、`POST/DELETE /works/{id}/favorite`、`POST /works/{id}/tags`、`DELETE /works/{id}/tags/{tagId}`、`GET /tags/catalog`、`GET /users/{id}/works`；第二期最简实现：`GET /works/random`、`GET /works/{id}/similar`（按共有 tag）、`GET /tags/suggest`、`GET /boards`、`GET /boards/{id}`、`GET /search`（关键词包含匹配）。

- **生命周期**：`POST /works` 建 `status=draft`、`source_platform=fluxer`、`source_visible=false` 的作品，不调 Fluxer（V2 列表因为 source_visible=false 看不到草稿）。发布时：真实环境排 V2 的 `create_main` 任务（论坛取 config.json `work_forums`，未配置只记日志），主帖投影触发器把 source_visible 置真；`FLUXER_FAKE=1` 直接置真。
- **软删除**：`deleted_at` + `deletion_reason=moderation`（同 V2），真实环境再排 `forum_soft_delete` 策略任务。
- **tag**：规则在 `tag_rules.py`。作品 tag 在 `site_work_tag`（移除留记录），自由 tag 是 `site_tag` 里 `group_id=free` 的行（`{name}` 先按名字不分大小写匹配官方 tag）。已发布作品降分级、去掉 restricts tag → 403 `forbidden`（`details.reason=review_required`）；必选组不能删空 → 422。非作者：官方 tag 403、新人 403 `onboarding_required`、tag 锁定 403、只能删自己加的。没做：同义词、每日次数、作者删过的不能加回、tag 日志接口。
- **改分级的统一入口**（`works.set_classification`；作者侧经 `change_classification`，审核侧经 `moderation.set_work_rating`）：有 Fluxer 帖子且非 fake 时走 V2 `WorkService.update`（排分类移动任务，帖子在 4 个论坛之间移动），否则直接改 `is_nsfw/is_abyss`。分级 tag 决定分级（r18g → 两个都真）；作品从来没有分级 tag（V2/DC 作品）就保持原分级。审核改分级另外换分级组 tag、重算 visibility、写 `rating_lock`。
- **图片分级冲突**：发布时检查封面、图库、最新版本文件里的图，高于作品分级 → 422 `rating_conflict`；已发布作品 PATCH 换封面/图库同样检查。`coverId`/`galleryIds` 必须是为这个作品上传的（`POST /uploads` 带 `workId`，purpose=cover/gallery）、`state=ready` 的图片。
- **封面、图库、latestVersion**：用 `attachments.attachment_dto`（有效模式看不到 → `url=null`、`placeholder=true`；全年龄图公开或长效签名地址；R18 图按账号签发 10 分钟签名地址）和 `versions.latest_versions`。
- **收藏**：复用 V2 默认书单（`WorkService.collect`）；`stats.favorites` 是 `work_statistics.collection_count`（触发器维护）。改了 V2 的 `collect`：没绑 Discord 的账号 `owner_id` 留空（原来 `int(None)` 报错）。
- **排序**：`latest` 按 updated_at、`new` 按发布时间（keyset 游标）；`hot_*`/`rising` 按「互动加权 × 半衰期衰减」（offset 游标），`hall` 发布满 90 天按收藏；热度分数不下发，只给 `badges: ["hot"]`（周热度前 15%，5 分钟缓存）。`needsAck` 按最新公告序号 / 规则版本 / 最新版本 ID 和本人确认记录（`site_work_ack`）比对，作者和管理员恒为 false（`services/work_extras.needs_ack_map`）。

## 上传、附件、版本、下载（3.3、3.2）

- **不走 Fluxer 附件**（Fluxer 会剥 PNG tEXt，角色卡会坏）：`POST /uploads` 建 `attachment` 行（`state=uploading`，uploadId = 附件 ID），签发直传地址；浏览器原样 PUT；`complete` 检查对象到了、大小一致（否则 409），置 `processing`，排分级任务。
- **本地存储**：直传地址 `PUT /api/storage/upload/{token}`、读取地址 `GET /api/storage/files/{token}`（HMAC 签名，带过期时间；不在 `/api/v1` 下，不要会话和 CSRF）。用 R2 时这两个端点 404，地址是 R2 预签名地址。
- **分级**（`services/attachments.classify_attachment`）：先算 sha256、记宽高；同一张图（按 sha256）人工复核过的直接用复核结论（`review_state=reviewed`，违禁的照样拦），不再调分级服务（01 号文档 5.1）；否则图片过分级器，非图片按作品当前分级。已发布作品的封面/图库/版本文件分级高于作品分级 → `rejected` + `rating_conflict`；草稿不拦（发布时检查）；头像只能全年龄。违禁 → `rejected` + `blocked`。拿不准（suspect）→ `ready`，`review_state=pending_review`，`rating` 按 01 号文档 5.1 的表取保守值：全年龄作品（或不属于作品）→ r18，R18/R18G 作品 → r18g（模型偏向更高时取更高）；封面/图库/聊天/讨论区图片同一事务里直接进图片审核队列，所属作品 `moderation_state` 置 `pending_review`（审核处理完、作品没有别的待审图片时回到 ok）。
- **过期清理**（`services/attachments.purge_stale_uploads()`）：申请了直传却一直没 `complete` 的附件（`state=uploading`，创建超过 `SITE_UPLOAD_TICKET_TTL` + 1 小时），删存储里的对象（传了一半或传完没确认的），行标 `deleted_at`。幂等，要由定时任务调用（每小时一次即可；还没接进调度）。
- **附件对象**（`services/attachments.attachment_dto(att, ctx)`）：有效模式看不到 → `url=null`、`placeholder=true`；全年龄图给公开或长效签名地址；R18 图给按账号签发的 10 分钟签名地址。
- **版本**：`GET /works/{id}/versions`（keyset 分页，新的在前）、`POST`（作者或 `works.manage_any`；草稿也能发；不传 `fileIds`/`dependencies`/`license` 时沿用上一版）。`fileIds` 必须是本作品 `purpose=version_file`、`ready` 的附件。版本文件的 `url` 是下载接口地址。
- **下载**：要登录；作品看不到、版本/文件不匹配都 404；`work_statistics.download_count +1`，302 到 10 分钟签名地址。
- **S3 / R2**（`storage/s3.py`）：直传是预签名 PUT，`Content-Type` 和 `Content-Length` 都签进地址——字节数和 `POST /uploads` 声明的 `size` 不一样，R2 直接 403（不签的话拿到地址就能往桶里塞任意大的文件）。`ticket.headers` 只有 `Content-Type`，`Content-Length` 浏览器按实际字节自动带（脚本不能设）。读取、下载是预签名 GET（`ResponseContentType`、下载时 `ResponseContentDisposition: attachment; filename*=UTF-8''...`），R18 和下载 10 分钟；配了 `SITE_S3_PUBLIC_BASE` 的全年龄图给公开地址。对象原样存取，角色卡 PNG 的 tEXt 块不动（集成测试按字节比对）。`sha256` 流式读整个对象算。
- **审核接口**：待审图片 = `attachment WHERE review_state='pending_review' AND deleted_at IS NULL`（`auto_rating`、`model_scores`、`purpose`、`work_id`、`owner_id` 都在附件行上）；人工结论用 `attachments.apply_review(att, rating=..., blocked=...)`（不提交事务）。审核员看到的地址由 `moderation_common.reviewer_attachment` 经存储适配器签发。

## 聊天外层、举报审核（3.5、3.10）

代码：`routers/chat.py reports.py moderation.py`，逻辑在 `services/chat.py`、`services/moderation.py`、`services/moderation_common.py`。没有新迁移，只用 `site_v1_schema` 建好的表。规则对齐假后端（`contract-tests/mock-server/vendor/src/mock/handlers/{chat,moderation}.ts`、`shared/lib/moderation.ts`）。

作品可见性用 `work_access.work_visible`（见上），看不到一律 404。

**聊天外层**
- 作品交流区 = Fluxer 原生主帖：`source_platform=fluxer` 看 `work.source_thread_id`，Discord 来源看 `thread` 表 platform=fluxer、role=main 的投影。主帖没建好 → `canOpen=false`、`memberCount=0`。`FLUXER_FAKE=1` 时没有主帖的作品用假频道 `work-{id}`，视为能打开（fake 模式不排 create_main）。
- 扩展字段（协议 ChatTargetInfo 之外，ajv 允许）：`canPost`（已登录、能打开、没被作品内禁言 `work_user_mute`、没被全站禁言）、`mutedUntil`（禁言截止；永久的作品内禁言为 null 且 `canPost=false`）。
- 未读汇总：Fluxer 按原生主帖和私信频道给，Forum 映射成作品并剔除看不到的。RPC 形状见 fluxer-rpc.md 第 4、5 节。

**聊天面板（代转 Fluxer 消息）**

代码：`routers/chat_messages.py`、`services/chat_messages.py`（代转层）、`services/chat_channels.py`（公共频道读取约定）、`services/chat_notify.py`（@ 提醒调用点）、`fluxer/native_http.py`（真实实现）。迁移 `site_v1_chat_tables`。

- 接口（三种目标同一组，前端 `src/features/chat-nodebb/api.ts`）：作品交流区 `/works/{id}/chat`、私信 `/conversations/{id}`、公共聊天区 `/channels/{id}/chat` 下的 `GET/POST messages`（`before`/`after`/`around`/`limit`，响应 `{items(正序), hasMoreBefore, hasMoreAfter}`）、`PATCH/DELETE messages/{mid}`、`PUT/DELETE messages/{mid}/reactions/{emoji}`、`GET state`、`POST read {messageId}`、`POST typing`（204）。另有 `GET/POST /conversations`、`POST /works/{id}/mutes`、`DELETE /works/{id}/mutes/{userId}`。消息形状同前端 `src/shared/protocol/chatMessage.ts`（`channelId` 是 `{kind}:{id}`，`nonce` 只还给发送者）。
- 目标 → Fluxer 频道：作品 = 原生主帖（`native_thread_id`）；私信会话 ID 就是 Fluxer 私信频道 ID；公共频道 = 频道对象的 `fluxer_channel_id`（频道领域做，可空；这里在已注册的表里找带 `id,name,rating,fluxer_channel_id` 列的表，或由频道领域调 `set_public_channel_source` 注册）。`FLUXER_FAKE=1` 时没有的用 `work-{id}` / `channel-{id}`；真实环境没有 → 404 / `canOpen=false`。
- 假 Fluxer：消息 `site_chat_message`（软删除留占位）、私信频道 `site_chat_dm_channel`。Forum 自己的叠加：`site_chat_message_meta`（本站附件、删除记录）、`site_chat_read_state`（已读位置，只往后走；没进过的交流区和公共频道不算未读，私信算）。
- 规则：看不到的作品 / 分级不可见的公共频道 404；附件按查看者有效模式给占位（私信里 Fluxer 原生附件分级未知按 R18）；public 作品交流区不能发 R18 图、公共频道不能发高于频道分级的图（422 `rating_conflict`）；作品内禁言 / 全站禁言 403 `muted`（`details.until`，永久为 null）；私信每条消息都按对方 `dmPrivacy` 和屏蔽检查（403）；删言：本人 `by_author`，作品作者 / 卡贴管理员（`can_manage_work`）`by_manager`，`moderation.content`、公共频道里 `channels.manage` 的站务 `by_staff`；`nonce` 10 分钟内去重（Redis）。
- @ 提醒：消息里的 `<@userId>` 经 `chat_notify.deliver_notification(session, recipient_id, notification)` 写提醒（协议 Notification 形状，`category=mention`、`context.{workId|conversationId|channelId, messageId}`）；提醒领域实现 `site_v1.services.notifications.create_notification` 后自动接上，之前只记日志。
- 读性能：每个频道最新 50 条缓存在 Redis `site:chat:latest:{channel}`，写操作清缓存，另有 `SITE_CHAT_CACHE_TTL`（默认 15 秒）兜住从 Fluxer 客户端直接发的消息；前端 8 秒轮询（`after=最后一条`）直接从缓存给。真实环境的 `reactions.me` 用本站记下的点击（Redis `site:chat:rx:*`），不用缓存里取消息那个人的。
- 实时（没做，建议）：Forum 订阅 Fluxer 网关事件（NATS `events.content.v1.message.*`），按频道 → 目标映射后发到 Centrifugo 频道 `chat:{kind}:{id}`（前端 `ChatChannelEvent`：`message.created/updated`、`subscription.closed`），按每个订阅者的有效模式序列化（R18 图占位），切到全年龄模式 / 作品转受限时由 Forum 调 Centrifugo `unsubscribe` 并推 `subscription.closed`；订阅令牌由 Forum 签发（`/realtime/token` 带频道权限），未读走外层 `unread.updated`。有了实时后可以把缓存 TTL 调长、写时更新缓存而不是删除。

**举报**（`POST /reports`）
- 理由按对象类型限定（同假后端），`other` 要写 note，note ≤ 500，不能举报自己。
- 看不到的对象 404：作品按上面的规则；`upload` 要 ready 且分级和所属作品可见（上传者本人总能举报自己的）；`tag` 在全年龄模式下不能是限制可见的；`user` 要存在且没合并；`collection`/`event`/`event_entry` 本路线还没有，一律 404。`message`/`post`/`dc_archive_post` 原文在 Fluxer/DC 存档，Forum 读不到，**只记 ID 不校验**（快照标题是「消息 xxx」）。
- 去重：同一对象的待处理举报聚合到一个 report 队列项；同一账号对同一待处理对象再举报 409（先查，`uq_report_pending_reporter_target` 兜底）。队列项处理后举报一起变 resolved，可以再举报（进新队列项）。

**审核队列**
- 权限：`image` 要 `moderation.images` 且本人 R18 资格 granted；`report` 要 `moderation.reports` 或 `moderation.content`（`site.admin` 视为全部）。权限来自 `site_account.permissions`（种子里 demo 有全部）。
- 排序：待处理举报按不同举报人数降序、再按入队时间；待处理图片按入队时间；已处理按处理时间倒序。offset 游标。
- 图片项来自 `attachment`：读 `type=image&state=pending` 时，把 `kind=image`、`review_state=pending_review`、未删除、用途是 cover/gallery/chat/discussion、还没有待处理项的附件补成队列项（`source=model`，`auto_rating`/`model_scores`/`purpose` 从附件取）。也可以直接调 `services.moderation.enqueue_image(session, att, source=..., appeal_note=...)`（申诉、举报来源要这样传）。附件被删的图片项不再显示。
- 处理（`resolve`，已处理 409，行锁防并发）：
  - 图片 `approve`（分级 = 请求的 rating，否则作品当前分级）/ `set_rating` / `block`（附件 rejected+blocked，从作品封面、图库里拿掉，给作者记一次违规调整）。图片分级高于作品时作品随之上调（记调整，不计违规）。作品没有别的待审图片时 `moderation_state` 从 pending_review 回到 ok。
  - 举报 `dismiss` / `adjust`（只对作品：改分级会写 `rating_lock`；增删 tag 只能是目录里非分级、非资源类型的 tag）/ `remove`（作品：`status=removed`、`moderation_state=removed`，并复用 V2 `WorkModerationService.soft_delete`——站务没绑 Discord 时按同样步骤做、`OperationLog.actor_id` 留空；tag：软删 tag 并从所有作品摘掉；upload：同图片拦截；消息/帖子：只记录）/ `penalize`。`penalty` 可附在 adjust/remove 上，禁言写 `user_moderation_record(kind=mute, until)`，警告 `kind=warning`。
  - 调整记录 `user_moderation_record(kind=adjustment)`，`strike=true` 的计入 `authorStrikes90d`。
- 改作品分级（`services.moderation.set_work_rating`）：分级字段走统一入口 `works.set_classification`（有 Fluxer 帖子时排 V2 分类移动任务），再换分级组 tag、重算 `visibility`。

## 我的、提醒、公告规则、作品内、书单、好友（第二期 me 块）

路由：`routers/me_extra.py notifications.py work_extras.py collections.py friends.py`；迁移 `site_v1_me_tables`（表结构见 `models/site_me.py`）。

- **作品内**：公告复用 V2 `work_announcement`（`version` = 作品内按发布时间的行号，撤回的不列出但占号），发公告按 V2 同样方式给关注者写 `notification(work_announcement)`；关注复用 `thread_follow`（没绑 Discord 时 `user_id` 留空）（作品内禁言 `/works/{id}/mutes` 在聊天块）。确认、规则、钉选、DC 存档是新表 `site_work_ack / site_work_rules / site_work_pin / site_dc_archive_post`（存档数据由迁移任务导入，现在为空；非 DC 来源作品 404）。
- **提醒**：V2 `notification`（公告、旧作品更新 → `work_update`；作者新作 → `followed_author_new_work`；`tag_review` 不出）和新表 `site_notification`（好友申请、规则更新……）合并分页，ID 分别是 `v<id>`、`n<id>`；已读和 V1/V2 共用 `read_at`。其他块写提醒用 `services.notifications.create_notification(session, recipient_id, notification)`（协议 Notification 形状，只 add 不 commit）。来源作品不在 `listable_conditions` 里的不返回、不计数、不在「全部已读」里改。`category` 接受逗号分隔多个。
- **站务公告、规则中心**：`site_announcement`（只存一份，`audience=r18` 只给有效模式 r18）、`site_announcement_ack`（按版本号比对）、`site_rule_doc`。发布公告的后台接口（`/admin/announcements`）不在本块。
- **书单**：复用 V2 `booklist / booklist_item / user_collection`，加删条目走 V2 `WorkService.collect`；默认收藏书单和赛事书单不算书单；`itemId` 是作品 ID。协议多出来的 tag、上传封面、`defaultSort=updated` 在 `site_collection_meta`。
- **我的**：`/me/preferences`（带版本号，409 带 `details.current`）、`/me/tag-habits`、`/me/pinned-works`（`GET /users/{id}` 带 `pinnedWorks`）、`/me/decorations`（装饰身份组 `site_decoration_role`，拥有关系 `site_user_decoration_grant`；协议 User.decorations 还没接上）、`/me/plugin-tokens`（只存 sha256 和末 4 位；`/plugin/v1/*` 没做）。
- **好友**：`site_friend`（a<b 一行）、`site_friend_request`；`GET /users/{id}` 的 `viewer.friend / friendRequest / dm` 按好友关系算。
- 种子 `seed_me_block`（只在 `FLUXER_FAKE=1`）：规则中心 4 篇、站内公告 6 条、3 个装饰身份组发给 demo、lin→demo 好友申请、第一个预置作品的规则/公告/钉选。

## 迁移里的新表和字段

| 表 / 字段 | 用途 | 用在 |
| --- | --- | --- |
| `site_account` | 站内账号补充：邮箱、创建时间、新人期 `newcomer_until`、`quiz_passed`、额外权限键 `permissions` | 账号 |
| `site_user_prefs` | 协议 Prefs：内容模式、showR18G、blurR18、屏蔽/关注 tag、私信隐私、钉选榜单 | 账号；作品列表按屏蔽 tag 过滤 |
| `site_restricted_access` | R18 资格：state、source、确认时间、条款版本、撤销人和时间 | 账号 |
| `site_user_block` | 屏蔽用户 | 账号；所有作品列表过滤（`social.blocked_user_ids`） |
| `site_fake_credential` | 假 Fluxer 的用户名/邮箱/密码哈希，真实环境为空 | FakeFluxer |
| `site_setting` | 站点键值，`tag_catalog.version` 是 tag 目录版本号 | `/site`、tag 领域 |
| `work.status` | draft/published/archived/unclaimed/removed，现有作品回填 published，默认值 published（V2 和触发器建的作品都是已发布） | 作品 |
| `work.summary` `layout` `declarations` `rating_lock` `moderation_state` `tag_locked` `published_at` `updated_at` | 协议 Work 对应字段 | 作品、审核 |
| `work.cover_attachment_id` `gallery_attachment_ids` | 封面和图库附件（`attachment.id`） | 作品、上传 |
| `work.visibility` | public/restricted 冗余列（列表过滤用） | 作品、tag、审核 |
| `work_statistics.download_count` | 下载次数（Work.stats.downloads） | 版本下载 |
| `work_version` `work_version_file` | 版本（名字同作品唯一）和版本文件 | 版本 |
| `attachment` | 附件：用途、所属作品/频道、存储 key、state（uploading/processing/ready/rejected）、rating、auto_rating、模型分数、审核状态、拒绝原因、宽高 | 上传、作品、审核 |
| `site_tag_group` `site_tag` `site_tag_question` | tag 目录：组（必选、maxSelect、onlyForTypes、requiredForTypes）、官方/自由 tag（group_id=free 为自由 tag；restricts、rating、forTypes）、必答问题 | tag |
| `site_work_tag` | 作品上的 tag（addedBy、移除记录保留给 tag 日志） | tag、作品 |
| `report` | 举报；同一账号对同一待处理对象只能一条（部分唯一索引 → 409） | 举报 |
| `moderation_queue_item` | 审核队列（image / report），同一对象只有一个待处理项 | 审核 |
| `user_moderation_record` | 警告、全站禁言（mute）、作品调整记录 | 审核 |

| `site_tag_migration_record` | 旧标签迁移记录（`kind` = tag / work / prefs，按 `(kind, legacy_key)` 唯一，重跑覆盖）：旧标签迁成了什么、原分类；作品加了哪些 tag、超额丢了哪些；屏蔽偏好换成了哪些 ID | `scripts/migrate_legacy_tags.py` |
| `ix_attachment_sha256_reviewed` | `attachment(sha256)` 部分索引（`review_state='reviewed'`），按内容哈希找复核过的同一张图 | 上传分级 |

tag 目录单独建表，没有复用 V1/V2 的 `tag` 表：那张表有 `source`/`category` 约束、自定义标签治理和 DC 同步逻辑，新 tag 混进去会出现在旧接口的标签列表和治理里。旧标签迁成协议 tag 见下一节。

## 旧标签迁成协议 tag（协议 7.3）

`scripts/migrate_legacy_tags.py`（逻辑在 `site_v1/legacy_tag_migration.py`，对照表 `scripts/legacy_tag_map.yaml`）：

```bash
DATABASE_URL=... uv run python scripts/migrate_legacy_tags.py --dry-run --json /tmp/tags-dry.json   # 算完回滚
DATABASE_URL=... uv run python scripts/migrate_legacy_tags.py --json /tmp/tags.json                # 正式跑，可重跑
```

- 前提：`alembic upgrade head`（迁移记录表 `site_v1_storage_tag_migration`），官方 tag 目录已 seed。
- **DC 原生标签**（`tag.source` = discord / fluxer，以及 DC 转过来还没分类的 custom）：按对照表归入题材、功能、内容提示、属性（和形态「纯文字」）；「A/B/C」按表一拆多，拆出来目录里没有的部分变自由 tag；「其他」「其它」丢弃。对不上的：原名清洗后变自由 tag，作品打「待分类」（官方 tag，默认在题材组，没有时脚本建）。对照表来自「DC旧帖迁移完整方案」5.2 和 tag 方案「现有频道和 DC tag 怎么改」，【待定 D12】的拆法先按 5.2 写；最终版由管理员在导出的原标签清单上逐行填，改 YAML 重跑即可。
- **自定义标签 7 类**（癖好、作品、角色、特质、情节、背景、玩法）和深渊向标签：全部变自由 tag（`group=free`），原分类、是否深渊向记在迁移记录 `detail.legacyCategory` / `isAbyss`。
- **清洗**：NFKC；分隔符（`/ 、 ， | + & _ -` 等）换成 `·`；去空白、emoji 和其他符号；最长 20 字；清洗后为空或是禁用名的丢弃。**撞名合并**：清洗后和已有官方 tag、自由 tag、同义词不分大小写同名的直接用已有的；几个旧标签清洗成同一个名字的合成一个。
- **作品上的 tag**（`tag_binding` 当前轮次 + `work_native_tag_binding`）：已有的协议 tag 不动、先占名额；再加官方 tag（DC 原顺序），再加自由 tag（原赞数从高到低，一样的按添加时间早的）；超过 15 个（`SITE_MAX_TAGS`）和组上限（题材 3、形态 1）的丢弃，写进迁移记录。官方 tag 和原生标签来的自由 tag `addedBy` = 作者；自定义标签来的为空（系统）。已删除作品、已结束的绑定、书单上的绑定不迁。
- **屏蔽标签**：`user_search_preferences.exclude_tags`（名字）按同一映射换成新 tag ID，并进 `site_user_prefs.blocked_tag_ids`；用户按 `fluxer_user_id`，再按 DC ID（`discord_identity`）找，找不到的记 `user_not_found`（用户注册后重跑即可补上）；已关注的 tag 不屏蔽；「待分类」不进屏蔽。
- **不迁**：赞踩、提议、包含/互斥关系、操作记录；也不删旧表里的任何东西。
- **幂等**：只增不删；迁移记录按 `(kind, legacy_key)` 覆盖成最新一次（`run_id`）。整个迁移一个事务（加了 advisory lock，不会两个同时跑）。
- 没做：频道 → 分级/受众/形态/资源类型（7.1，频道对照表还没定）；子服虚拟标签（7.4）；`forTypes` 不检查（作品还没有资源类型 tag）。新表都没有挂 thread/booklist 投影触发器；`work` 新列都有数据库默认值，`project_thread_work` 触发器按列名插入时由默认值补齐。

## 加新接口的约定

1. **路由**：往 `src/site_v1/routers/<领域>.py` 的 `router` 里加（已注册到 `/api/v1`，路径写协议里 `/api/v1` 之后的部分）。
2. **拿当前用户**：

   ```python
   from site_v1.deps import Ctx, CurrentUser

   @router.get("/works/{id}")
   async def get_work(id: str, ctx: Ctx):          # 公开接口；ctx.viewer 可能是 None
       if not ctx.can_view(rating, visibility):     # 有效模式不允许 → 404，不暴露存在
           raise not_found()

   @router.post("/works", status_code=201)
   async def create_work(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):  # 未登录自动 401
       ...
   ```

   `Viewer` 字段：`user_id`、`username`、`display_name`、`avatar_url`、`discord_user_id`、`email`、`created_at`、`newcomer_until`/`is_newcomer`、`quiz_passed`、`permissions`（`has_permission("moderation.images")`，`site.admin` 视为全部）、`prefs`（`content_mode`、`show_r18g`、`blur_r18`、`blocked_tags`、`followed_tags`、`dm_privacy`、`pinned_boards`）、`access`（`state` 等）。
   `ctx.effective_mode`、`ctx.visible_ratings()`、`ctx.can_view(rating, visibility)`、`ctx.fluxer_token`（调 FluxerPort 用）。调 V2 `work/*` 服务时用 `viewer.to_v2_principal(ctx.effective_mode)`，V2 需要的 `forums`/`config` 自己补（参考 `api/v2/dependencies.current_member`）。
3. **错误**：`from site_v1.errors import not_found, invalid, forbidden, conflict, unavailable, SiteError`，`raise invalid({"title": "标题 1-100 字"})`。`rating_conflict`、`muted`、`onboarding_required` 用 `SiteError(422/403, code, message, details)`。V2 服务抛的 `HTTPException` 不用接，会自动映射。
4. **DTO**：响应一律 camelCase、ID 字符串、时间 `iso_utc()`。作者用 `await load_users(session, ids)` 批量取协议 User。不要透传 V2 的 dict，不要输出热度数字（只给 `stats{favorites,downloads,chatMessages}` 和 `badges`）。注意 `to_camel("show_r18g")` 得到的是 `showR18G`，和协议一致；别的缩写字段自己核对。
5. **分页**：

   ```python
   from site_v1.paging import pagination, PageRequest, page_response

   @router.get("/works")
   async def list_works(ctx: Ctx, page: PageRequest = Depends(pagination)):
       rows = (await session.execute(q.offset(page.offset).limit(page.limit + 1))).all()
       return page_response(rows, page, serialize)          # offset 游标
       # 排序会变的列表用 keyset：q.where(tuple_(col, Work.id) < tuple(page.after)) ...
       # return page_response(rows, page, serialize, key=lambda r: [iso_utc(r.created_at), r.id])
   ```

   `limit` 超过 100 按 100，坏游标 422。
6. **作品可见性和权限**：一律用 `services/work_access`（`get_visible_work`、`get_editable_work`、`work_visible`、`can_manage_work`），列表用 `services/works.listable_conditions` + `query_page`。不要自己再写一份判断。
7. **附件和版本 DTO**：`services/attachments.attachment_dto(att, ctx)`、`services/versions.latest_versions(session, work_ids, ctx)`。
8. **改作品分级**：`services/works.change_classification(session, ctx, work, rating)` 或 `set_classification(session, work, rating, actor=, mode=, is_admin=)`，不要直接改 `is_nsfw/is_abyss`（真实环境要排 V2 的分类移动任务）。
9. **fake 模式和异步任务**：`FLUXER_FAKE=1` 时不启动 `WorkTaskWorker` 和事件消费者；要排 V2 任务的地方（建主帖、移动、编辑）在 fake 模式下跳过并视为同步成功，可见性不依赖它们。
10. **种子和契约测试**：契约测试要的预置数据加在 `scripts/seed_site_v1.py`；做完一个用例就把 `docs/site_v1/known-gaps.json` 里对应的删掉（意外通过会报红）。

## 契约测试和 pytest 现状

`scripts/run_contract_tests.sh`（`CT_DB`、`PORT`、`REDIS_URL`、`LOG` 都可用环境变量覆盖）：重建库、迁移、种子、起服务、跑 vitest。00–09、99 全部文件通过：113 通过（其中 6 个按 known-gaps 记为预期失败）、1 跳过（RL-02 默认不跑）。

`known-gaps.json` 只剩确实做不了的：

| 用例 | 原因 |
| --- | --- |
| WRK-19 | `POST /works/{id}/ack` 是第二期，`needsAck` 恒为 false |
| WRK-20 | 作品钉选 `/works/{id}/pins` 是第二期 |

| DM-01～03 | 用例要 `POST /conversations` 发起私信（第二期，且要 Fluxer 私信 RPC）；`GET /users/{id}` 的 `viewer.dm` 已按隐私设置给出 |
| RL-01 | `/api/v1` 还没有按账号限频（1.6）；旧 `RateLimitMiddleware` 只管 `/v1`、`/v2` |
| RL-02 | 按 IP 限频同上；契约测试默认不跑 |

CHT-04、MOD-05、R18-05、R18-17 是「不存在 / 看不到 → 404」的用例，现在路由都在，是处理函数按可见性返回的 404。

pytest：`uv run pytest tests/site_v1` 全过；全量和接入前基线相比没有新增失败。

- `test_site_v1_s3_integration.py`：真的起一个 S3 兼容服务跑上传、complete、下载 302、图片地址、角色卡 PNG 字节比对、CORS 预检、过期清理。没设 `SITE_S3_IT_ENDPOINT` 或连不上时整个文件跳过。MinIO 官方已停止分发二进制（dl.min.io 返回 410），本地可以用 moto（`pip install 'moto[server]'; moto_server -p 9100`）；moto 不校验签名和过期，相关两条断言会自动跳过，换 MinIO / R2 时会跑。
- `test_site_v1_classifier_http.py`：站点和假分级服务（`scripts/fake_classifier.py`）都用 uvicorn 在测试进程里起（端口 8834/8835，`SITE_IT_SITE_PORT` / `SITE_IT_CLASSIFIER_PORT` 可改），分级服务真的按签名地址回来拉图。
- `test_site_v1_legacy_tag_migration.py`：造旧数据跑 dry-run、正式迁移、重跑。

## 上线前需要的外部配合

**Fluxer**（细节见 [fluxer-rpc.md](./fluxer-rpc.md)；没实现之前对应接口返回 503 `temporarily_unavailable`）：

- 新 RPC：`rpc.auth.v1.password.login_service`（密码登录）、`rpc.auth.v1.account.register_service`（站内注册）、`rpc.identity.v1.profile.update_service`（改资料）、`rpc.content.v1.read_states.summary_service`（未读汇总）、`rpc.content.v1.channel.info_service`（聊天目标的频道信息）。
- 新会话要能被现有 `rpc.auth.v1.session.resolve` 解析、`revoke_current` 注销，并照常发 `events.auth.v1.session_invalidated`。
- 第二期再要：给已登录账号绑定 Discord（`rpc.identity.v1.discord.link_service`）、DC 联动注册、私信发起。
- Forum 这边：真实环境 `FLUXER_FAKE=0`，NATS 连上，`WorkTaskWorker` 和事件消费者要跑（发布建主帖、改分级移动、改标题同步、软删除都靠 V2 任务）；config.json 的 `work_forums`（4 个作品论坛 + guild_id）要配，否则发布只记日志不建主帖。

**对象存储（Cloudflare R2）**：

- 建桶，配 `SITE_S3_ENDPOINT`（`https://<account_id>.r2.cloudflarestorage.com`）、`SITE_S3_BUCKET`、`SITE_S3_ACCESS_KEY_ID`、`SITE_S3_SECRET_ACCESS_KEY`（R2 API 令牌，权限「对象读和写」，只限这个桶；`SITE_STORAGE_BACKEND` 会自动是 `s3`，`SITE_S3_REGION` 保持 `auto`）。
- **桶 CORS（必须配，否则浏览器直传和跨域读图都会被拦）**：R2 控制台 → 桶 → 设置 → CORS 策略，填 [`r2-cors.json`](./r2-cors.json) 的内容（把 `AllowedOrigins` 换成前端的正式域名，本地开发再加 `http://localhost:5173`；不要写 `*`）：

  ```json
  [
    {
      "AllowedOrigins": ["https://forum.example.com", "http://localhost:5173"],
      "AllowedMethods": ["GET", "HEAD", "PUT"],
      "AllowedHeaders": ["Content-Type"],
      "ExposeHeaders": ["ETag", "Content-Length", "Content-Type", "Content-Disposition"],
      "MaxAgeSeconds": 3600
    }
  ]
  ```

  也可以用 `wrangler r2 bucket cors set <桶名> --file docs/site_v1/r2-cors.json`，或任何 S3 客户端 `put-bucket-cors`（格式同上，外面包一层 `{"CORSRules": [...]}`）。要点：`PUT` + `Content-Type` 头给直传（预检请求带 `Access-Control-Request-Headers: content-type`）；`GET`/`HEAD` 给前端 `fetch` 读图、读角色卡 PNG 元数据（`<img>` 不需要 CORS，`fetch`/canvas 需要）；`ExposeHeaders` 里的 `Content-Disposition` 让前端拿到下载文件名。下载走 302 到签名地址是浏览器导航，不受 CORS 限制。`AllowedMethods` 不要加 `DELETE`/`POST`（删除只在服务端用 API 令牌做）。集成测试 `test_s3_cors_preflight_with_documented_rules` 按这份配置测预检。
- 自定义域名（`SITE_S3_PUBLIC_BASE`）上的公开读取同样受这份 CORS 约束；R2 自定义域名开缓存后，CSAM 扫描工具才会覆盖（01 号文档第六节）。
- 全年龄图想走公开地址：给桶绑自定义域名，填 `SITE_S3_PUBLIC_BASE`；不填则全年龄图也用签名地址（`SITE_PUBLIC_URL_TTL`）。R18/R18G 和下载始终是 10 分钟签名地址，**不要**把整个桶公开。
- 现有本地文件（如果用过本地存储）要迁进桶，`storage_key` 不变。

**图片分级服务**：配 `SITE_CLASSIFIER_URL`（和 `SITE_CLASSIFIER_TOKEN`），接口约定见下一节；没配时是 `manual`（一律「拿不准」，按 R18 处理并进人工审核队列）。阈值按服务的分数分布调 `SITE_CLASSIFIER_*`。用本地存储时必须配 `SITE_PUBLIC_BASE`（分级服务按绝对地址拉图，否则启动报错）。

### 图片分级服务接口

后端是调用方（`classifier.HttpClassifier`），分级服务实现下面这一个接口。本地假服务 `scripts/fake_classifier.py`（`uv run python scripts/fake_classifier.py --port 8835 --token secret`）照这个形状实现，集成测试 `tests/site_v1/test_site_v1_classifier_http.py` 用它跑通全链路。

请求：`POST {SITE_CLASSIFIER_URL}`，`Content-Type: application/json`，配了 `SITE_CLASSIFIER_TOKEN` 时带 `Authorization: Bearer <token>`。

```json
{
  "attachmentId": "123",
  "url": "https://<R2 预签名地址或 SITE_PUBLIC_BASE/api/storage/files/...>",
  "mime": "image/png",
  "filename": "cover.png",
  "size": 182044,
  "sha256": "9f86d0…",
  "width": 512,
  "height": 768
}
```

- `url` 是 10 分钟有效的签名读取地址（绝对地址），服务自己去拉原图；`size`、`sha256` 可用来校验和做哈希库比对；`width`/`height` 认不出时为 null。
- 超时 `SITE_CLASSIFIER_TIMEOUT` 秒（默认 15），同一张图只调一次（复核过的同图不再调）。`complete` 最多等 `SITE_CLASSIFY_INLINE_WAIT` 秒，等不到先返回 `processing`，分级在后台继续。

响应：HTTP 200，JSON：

```json
{
  "scores": {"general": 0.91, "r18": 0.07, "r18g": 0.02},
  "blocked": false,
  "rating": "general"
}
```

- `scores`：必填，三个键都是 0–1 的数（缺的键按 0）；原样存进 `attachment.model_scores`，审核队列里给审核员看。
- `blocked`：可选，`true` = 命中违法内容（哈希库等），附件 `rejected` + `blocked`。
- `rating`：可选，服务自己的结论 `general` / `r18` / `r18g` / `suspect` / `blocked`（例如 WD tagger 按幼态、无码标签组合判出来的），给了就优先用它；不给就按阈值：`r18g ≥ SITE_CLASSIFIER_R18G_THRESHOLD` → r18g，`r18 ≥ SITE_CLASSIFIER_R18_THRESHOLD` → r18，否则 general；最高分低于 `SITE_CLASSIFIER_CONFIDENCE` → suspect（拿不准）。
- 出错一律按「拿不准」处理（不会因为服务挂了把图当全年龄放出去）：超时、连不上、非 2xx（包括 401 令牌不对）、不是 JSON、`scores` 缺失或不是对象、分数不是数 / NaN / 不在 [0,1]。拿不准 → 保守分级 + 进人工审核队列（见上文「分级」）。

**Discord OAuth**：在 Discord 开发者后台给应用加回调地址 `https://<站点>/api/v1/auth/discord/callback`（或 `SITE_DISCORD_REDIRECT_URI` 指定的地址），配 `SITE_DISCORD_CLIENT_ID` / `_SECRET`（不配回退 config.json `auth`）；前后端不同源时配 `SITE_FRONTEND_BASE`。正式成员身份组 `SITE_DISCORD_ROLE_IDS`（R18 资格的 `discord_role` 条件）。

**部署配置**：

- `SITE_STORAGE_SIGNING_KEY`（只用本地存储时需要；多进程、重启后旧地址要继续有效）、`SITE_PUBLIC_BASE`（对外根地址）。
- `SITE_REGISTRATION_MODE`（默认 `discord_only`）、`SITE_TERMS_VERSION`、`SITE_COOKIE_SECURE=1`。
- **不要**开 `SITE_TEST_HOOKS`。
- `alembic upgrade head`（`site_v1_schema`）；`scripts/seed_site_v1.py` 在真实环境只建 tag 目录、给已有同名账号补站内状态（不建账号）；预置示例作品只给测试环境用。
- 站务权限写在 `site_account.permissions`（`moderation.images`、`moderation.reports`、`works.manage_any`、`site.admin` 等），上线时给站务账号配好。
- `/api/v1` 的限频（1.6）还没做，上线前要补（或在网关层限）。
