# /api/v1：按共用接口协议 v0.4.1 的接口层

给我们自己的前端（odysseia-hq/web）用的 `/api/v1`。协议权威是 odysseia-hq/api 的 `openapi/openapi.yaml`（只做 `x-phase: 1`），契约测试在同一仓库的 `contract-tests/`。

**和原有接口的关系**：V1（`/v1`，Discord 旧接口）、V2（`/v2`）、fluxer 集成全部原样保留。`/api/v1` 是加在同一个 FastAPI app 上的一层：

- 复用 V2 的服务（`work/*`、`identity/*`）和同一份数据（`user`、`work`、`author_follow`……）；
- 有自己的会话（Cookie + Redis）、错误格式、DTO；
- V2 没有对应的东西（R18 资格、屏蔽、附件、版本、tag 目录、举报审核）用新表，迁移 `alembic/versions/site_v1_schema.py` 一次性建好。

这一轮做了打底和账号会话部分；作品、上传、tag、聊天外层、举报审核由后面的人在这个骨架上接（见最后一节）。

## 目录

```
src/site_v1/
  __init__.py        install(app)：异常处理器、中间件、路由、测试钩子、启动钩子（src/api/main.py 调用）
  config.py          全部配置项（环境变量）
  errors.py          SiteError 和快捷函数；只对 /api/v1 生效的异常处理器
  middleware.py      ASGI 中间件：建请求上下文、CSRF、X-Content-Mode、未处理异常 → 503
  session.py         Redis 服务端会话、Cookie
  context.py         SiteContext（请求上下文）、Viewer（principal）
  deps.py            Ctx / CurrentUser 依赖、数据库会话工厂、Redis
  mode.py            有效模式、分级映射、可见性
  paging.py          不透明游标分页
  dto.py             camelCase、ID、ISO UTC、User/Me/附件 DTO、load_users 批量取作者
  membership.py      Discord 正式成员身份组检查（真实环境）
  fluxer/            FluxerPort（port.py）、RealFluxer（real.py）、FakeFluxer（fake.py）
  services/          accounts.py（账号行、资格条件）、social.py（关注/屏蔽）、user_works.py（占位）、
                     attachments.py（上传、附件对象、分级）、versions.py（版本、下载、种子）、work_access.py（作品可见/可管理）
  storage/           存储适配器：port.py（StoragePort）、local.py（本地文件 + 签名端点）、s3.py（S3 兼容 / R2）
  classifier.py      图片分级适配器（stub / http / manual）和图片宽高解析
  files_config.py    存储、分级的配置项
  routers/           site auth me users works versions uploads tags chat reports moderation；
                     storage.py（/api/storage 本地存储签名端点，不在协议里）
  test_hooks.py      /__test__（SITE_TEST_HOOKS=1 才挂）
src/models/          site_account.py site_tag.py work_version.py attachment.py site_moderation.py；work.py 加了协议字段
scripts/seed_site_v1.py         契约测试账号、tag 目录
scripts/run_contract_tests.sh   本地跑契约测试
docs/site_v1/known-gaps.json    本路线的已知不一致清单
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
| `unread_summary` | 新 RPC `rpc.content.v1.read_states.summary_service` | 空 |
| `chat_target_info` | 新 RPC `rpc.content.v1.channel.info_service` | 固定假频道 |

错误：`FluxerUnavailable` → 503；`InvalidCredentials` → 422 `fields.password`；`AccountExists` → 422 `fields.username|email`；`MfaRequired`、`AccountUnavailable` → 403。新 RPC 的请求、响应、错误码见 [fluxer-rpc.md](./fluxer-rpc.md)。

## 已定的语义（实现在哪）

- **有效模式**（`mode.effective_mode`）：已登录 且 `prefs.contentMode=r18` 且 `restrictedAccess.state=granted` → `r18`，否则 `general`。撤销、切换下一个请求立即生效。
- **分级映射**（`mode.work_rating` / `rating_flags`）：`is_abyss` → r18g，`is_nsfw` → r18，否则 general；反向 r18g → (is_nsfw, is_abyss) = (true, true)。
- **r18g 可见性**：有效模式 r18 且 `prefs.showR18G`（`mode.visible_ratings`），不再看 V2 的 Discord 深渊身份组。`Viewer.to_v2_principal(mode)` 给 V2 服务的 `show_nsfw` / `can_view_abyss` 也按这个算。
- **visibility**：rating≠general 或绑了 `restricts=true` 的 tag → restricted（`mode.work_visibility`）；`work.visibility` 是冗余列，改 tag/分级时要同步。
- **可见性只看本地状态**：作品 `status=published` 就可见，Fluxer 原生帖没建好只影响聊天能否打开（`canOpen`）。现有作品迁移时回填为 published。
- **R18 资格**：条件见 `services/accounts.access_conditions`；`POST /me/restricted-access/confirm` 条件都满足时授予，`revoked` 不恢复；`PATCH /me/prefs` 设 r18 时检查，不满足 403 `restricted_access_required`，`details.unmet` 是条件 ID（撤销时含 `revoked`）。
- **注册模式**：`/site.features` 里是布尔值（协议要求 `features` 的值都是 boolean）：`registration`（能否站内注册）、`registrationDiscordOnly`；另外顶层加了 `registrationMode: "open" | "discord_only"`。
- **关注用户**复用 `author_follow`（和 V2 同一份事实，按 `fluxer_user_id` / `fluxer_author_id` 定位；没有 Discord 绑定的账号 Discord ID 列留空）。**屏蔽**在 `site_user_block`，屏蔽时顺带取消关注。

## 上传、版本、下载

- **不走 Fluxer 附件**（Fluxer 会剥 PNG tEXt，角色卡会坏）：`POST /uploads` 建 `attachment` 行（`state=uploading`，uploadId = 附件 ID），签发直传地址；浏览器原样 PUT；`complete` 检查对象到了、大小一致（否则 409），置 `processing`，排分级任务。
- **本地存储**：直传地址 `PUT /api/storage/upload/{token}`、读取地址 `GET /api/storage/files/{token}`（HMAC 签名，带过期时间；不在 `/api/v1` 下，不要会话和 CSRF）。用 R2 时这两个端点 404，地址是 R2 预签名地址。
- **分级**（`services/attachments.classify_attachment`）：图片过分级器、记宽高；非图片按作品当前分级。已发布作品的封面/图库/版本文件分级高于作品分级 → `rejected` + `rating_conflict`；草稿不拦（发布时由作品领域检查）；头像只能全年龄。违禁 → `rejected` + `blocked`。拿不准（suspect）→ `ready`，`rating` 取保守值（至少 r18），`review_state=pending_review`。
- **附件对象**（`services/attachments.attachment_dto(att, ctx)`）：有效模式看不到 → `url=null`、`placeholder=true`；全年龄图给公开或长效签名地址；R18 图给按账号签发的 10 分钟签名地址。
- **版本**：`GET /works/{id}/versions`（keyset 分页，新的在前）、`POST`（作者或 `works.manage_any`；草稿也能发；不传 `fileIds`/`dependencies`/`license` 时沿用上一版）。`fileIds` 必须是本作品 `purpose=version_file`、`ready` 的附件。版本文件的 `url` 是下载接口地址。
- **下载**：要登录；作品看不到、版本/文件不匹配都 404；`work_statistics.download_count +1`，302 到 10 分钟签名地址。

给作品领域：
- `services/versions.latest_versions(session, work_ids, ctx)` → `{work_id: Version dto}`（Work.latestVersion）；
- `services/attachments.load_attachments(session, ids)` + `attachment_dto(att, ctx)`（cover、gallery）；
- `services/attachments.usable_work_attachments(session, ids, work_id=, purposes=("cover",))` 校验 coverId/galleryIds（不合格返回 None，报 422）；
- `services/work_access.can_manage_work / load_visible_work / load_manageable_work`（以后有卡贴管理员表改 `can_manage_work`）；
- 种子：`scripts/seed_site_v1.py` 的 `seed_work_version(session_factory, work_id, owner_id, name="v1.0", filename=..., data=None, mime=...)` → `(version_id, attachment_id)`，幂等。

给审核领域：待审图片 = `attachment WHERE review_state='pending_review' AND deleted_at IS NULL`（`auto_rating`、`model_scores`、`purpose`、`work_id`、`owner_id` 都在附件行上）；处理时调 `services/attachments.apply_review(att, rating=..., blocked=...)`（不提交事务），它把 `review_state` 置 `reviewed`、`state` 置 `ready` 或 `rejected/blocked`。

## 迁移里的新表和字段

| 表 / 字段 | 用途 | 谁用 |
| --- | --- | --- |
| `site_account` | 站内账号补充：邮箱、创建时间、新人期 `newcomer_until`、`quiz_passed`、额外权限键 `permissions` | 账号（已用） |
| `site_user_prefs` | 协议 Prefs：内容模式、showR18G、blurR18、屏蔽/关注 tag、私信隐私、钉选榜单 | 账号（已用）；作品列表按屏蔽 tag 过滤 |
| `site_restricted_access` | R18 资格：state、source、确认时间、条款版本、撤销人和时间 | 账号（已用） |
| `site_user_block` | 屏蔽用户 | 账号（已用）；所有作品列表过滤（`social.blocked_user_ids`） |
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

tag 目录单独建表，没有复用 V1/V2 的 `tag` 表：那张表有 `source`/`category` 约束、自定义标签治理和 DC 同步逻辑，新 tag 混进去会出现在旧接口的标签列表和治理里。旧自定义标签迁成自由 tag（协议 7.3）是后续的数据任务。新表都没有挂 thread/booklist 投影触发器；`work` 新列都有数据库默认值，`project_thread_work` 触发器按列名插入时由默认值补齐。

## 后面领域怎么接

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
6. **列表过滤**（3.12）：`status=published`、`ctx.visible_ratings()` / visibility、`viewer.prefs.blocked_tags`、`social.blocked_user_ids()`。`services/user_works.list_user_works` 是 `GET /users/{id}/works` 的占位，作品领域实现它即可。
7. **种子和契约测试**：已发布作品、版本文件、主榜补在 `scripts/seed_site_v1.py` 的 `seed_works()`；做完一个领域就把 `docs/site_v1/known-gaps.json` 里对应用例删掉（意外通过会报红）。
8. **fake 模式和异步任务**：`FLUXER_FAKE=1` 时不启动 `WorkTaskWorker` 和事件消费者；发布作品如果要排 V2 的 `create_main` 任务，fake 模式下直接跳过或同步视为成功，作品可见性不依赖它。

## 契约测试现状

上传：06 的 UPL-01～04、06 在 `beforeAll` 建草稿（`POST /works`）通过后就能跑；UPL-05 还要 `PATCH /works` 设封面。版本、下载（WRK-13～16）还要作品详情/列表给 `latestVersion`、种子作品带版本文件。


`scripts/run_contract_tests.sh`：00-spec、01-common、02-session、09-dm、99-rate-limit 全绿（01 里依赖 `/works` 的 COM-01、12–15，09 的 `POST /conversations`，99 的限频按 known-gaps 记为预期失败）。03–08 在 `beforeAll` 就失败（要 `GET /tags/catalog`、`POST /works`），对应领域做好后才会真正跑起来。
