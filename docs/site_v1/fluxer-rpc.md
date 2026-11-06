# /api/v1 需要 Fluxer 新增的 RPC

协议要求「前端不接触任何后端原生令牌」，并且要有用户名 + 密码登录和站内注册。现有 NATS RPC（见 `identity/fluxer_auth_service.py`）里**没有**服务端用密码登录、注册、改资料、读未读数的接口。`site_v1/fluxer/real.py` 已经按下面的形状调用；Fluxer 实现之前，这些调用会得到 503 `temporarily_unavailable`。

本地和契约测试用 `FLUXER_FAKE=1`（`site_v1/fluxer/fake.py`），行为和这里写的一致，可以当作参考实现。

## 通用约定（和现有 RPC 一样）

- 传输：NATS request/reply，请求体 JSON：`{"version": 1, "request_id": "<uuid hex>", ...字段}`。
- 回复：成功 `{"ok": true, "data": {...}}`；失败 `{"ok": false, "error": {"code": "大写下划线", "message": "...", "retryable": bool}}`。
- `origin`：`{"ip": "CF-Connecting-IP 或直连地址", "user_agent": "...（≤512）", "client_os": "Forum Browser"}`。所有请求都从 Forum 服务器发出，Fluxer 做风控时请用 `origin.ip`，不要用 NATS 连接的地址。
- 返回的会话（下面叫 **SessionData**）：

  ```json
  {
    "token": "flx_...",            // Fluxer 会话令牌，只存 Forum 服务端 Redis
    "user_id": "123456789012345678",
    "username": "demo",
    "display_name": "演示站长",     // 可空
    "avatar_url": "https://...",    // 可空
    "discord_user_id": "…" | null,
    "session_id_hash": "…"          // 和 rpc.auth.v1.session.resolve 一致，用于失效事件
  }
  ```

- 新会话要能被现有 `rpc.auth.v1.session.resolve` 解析、被 `rpc.auth.v1.session.revoke_current` 注销，并照常发 `events.auth.v1.session_invalidated`。
- `fluxer_auth_service.call_rpc` 已经对 `rpc.auth.v1.password.*`、`rpc.auth.v1.account.*`、`rpc.identity.v1.profile.*`、`rpc.content.*` 透传错误码（`IntegrationRpcError`），其他错误一律当 503。

## 1. 密码登录 `rpc.auth.v1.password.login_service`

请求：

```json
{ "login": "用户名或邮箱", "password": "明文（只在 NATS 内网传输）", "origin": { ... } }
```

成功：`data` = SessionData。

错误码：

| code | Forum 映射 | 说明 |
| --- | --- | --- |
| `INVALID_CREDENTIALS` | 422 `validation_failed`，`details.fields.password` | 用户名/邮箱或密码不对，不区分哪个 |
| `MFA_REQUIRED` | 403 `forbidden`，`details.reason=mfa_required` | 开了 2FA；第二步（`/auth/login/mfa`）不在第一期，以后加 `rpc.auth.v1.password.mfa_service` |
| `ACCOUNT_UNAVAILABLE` | 403 `forbidden`，`details.reason=account_unavailable` | 封禁、停用、已合并 |
| 其他 / 超时 | 503 | 可重试 |

要求：按 `origin.ip` 和账号限频；不要返回验证码挑战（Forum 没法转给浏览器），需要人机验证时返回 `RATE_LIMITED`（Forum 映射为 503，以后可细分为 429）。

## 2. 站内注册 `rpc.auth.v1.account.register_service`

只在 `SITE_REGISTRATION_MODE=open` 时调用（10-31 定的正式环境是 `discord_only`，走 Discord 回调建号）。

请求：

```json
{ "username": "3-20 位字母数字下划线", "password": "≥8 位", "email": "可空", "display_name": "可空，≤32", "origin": { ... } }
```

成功：`data` = SessionData（注册后直接登录）。

错误码：`USERNAME_TAKEN` → 422 `fields.username`；`EMAIL_TAKEN` → 422 `fields.email`；其他 → 503。

## 3. 改资料 `rpc.identity.v1.profile.update_service`

`PATCH /me` 改显示名。Fluxer 是资料权威；只改本地投影会被下一次 `session.resolve` 覆盖（`identity/auth_runtime.py` 每次解析都 upsert 用户资料）。

请求：`{ "token": "flx_...", "user_id": "...", "display_name": "1-32 字" }`

成功：`data` = `{user_id, username, display_name, avatar_url, discord_user_id}`（新资料）。之后最好发 `events.auth.v1.user_invalidated`，让解析缓存刷新。

## 4. 未读汇总 `rpc.content.v1.read_states.summary_service`

`GET /chat/unread-summary` 用（聊天外层，3.5）。实现：`FluxerPort.unread_summary`，Forum 侧映射在 `site_v1/services/chat.py`。

请求：`{ "token": "flx_...", "user_id": "..." }`

成功：

```json
{
  "works": [{ "thread_id": "原生主帖 ID", "unread": 3, "mentions": 1, "last_message_at": "2026-11-01T10:00:00Z" }],
  "dms":   [{ "channel_id": "私信频道 ID", "unread": 1, "mentions": 0, "last_message_at": 1761991200000 }]
}
```

- `works` 只列这个账号进过（有已读记录）的作品交流区，和假后端一致；没进过的交流区不算未读。私信列全部会话。
- `unread` 不算自己发的、已删除的消息；`mentions` = @我 + 回复我的消息。
- `last_message_at`：频道最后一条消息时间，ISO 8601 UTC 或毫秒时间戳；没有消息为 `null`。
- Forum 把 `thread_id` 映射到作品（`work.source_thread_id`，或 `thread` 表 platform=fluxer、role=main 的投影），再按有效模式剔除受限、下架、删除的作品（全年龄模式下不返回、不计数）。认不出的 `thread_id` 直接丢掉。

错误：任何失败 → 503。

## 5. 频道信息 `rpc.content.v1.channel.info_service`

`GET /chat/targets/{kind}/{id}` 用。实现：`FluxerPort.chat_target_info`。

请求：`{ "token": "flx_..." | null, "kind": "work" | "dm", "target_id": "原生主帖 ID 或私信频道 ID" }`（`kind=work` 时未登录也会调，token 为 null）

成功：

- `kind=work`：`{ "channel_id": "...", "member_count": 12 }`。`member_count` = 在这个交流区发过言或订阅了的人数（Fluxer 怎么定义都行，前端只显示）。
- `kind=dm`：`{ "channel_id": "...", "member_count": 2, "peer_user_id": "对方用户 ID" }`。**只有令牌对应的账号是会话成员时才返回**，否则按 `NOT_FOUND`。

错误码：`NOT_FOUND` → 作品：`canOpen=false`（作品存在、原生帖还不可用）；私信：404。其他 → 503。

Forum 侧：作品可见性（有效模式、status、下架）先在本地判断，看不到的直接 404，不会调 Fluxer；原生主帖还没建好时也不调，`canOpen=false`、`memberCount=0`。作品内禁言（`work_user_mute`）和全站禁言（`user_moderation_record.kind=mute`）由 Forum 自己算，不需要 Fluxer 返回。

## 6. 以后要的（不在第一期 40 个接口里，先记下）

- **给已登录账号绑定 Discord**（`GET /auth/discord/start?intent=link`）：`rpc.identity.v1.discord.link_service {token, discord_user_id, verified_at, profile}`。现在回调里 `intent=link` 返回 `/login?error=link_unsupported`。
- **DC 联动注册**（10-31 设计的 `/auth/register/*` 七个接口）：注册票据、设密码（服务端可调的 `rpc.auth.v1.password.set_service`，替代现在发给浏览器的 `external.prove` 凭证）、2FA。

## 7. 聊天消息代转：Fluxer 原生 HTTP 接口

前端的聊天面板（`VITE_CHAT_PANEL=nodebb`）调 Forum 的 `/api/v1/works/{id}/chat/*`、`/conversations/*`、`/channels/{id}/chat/*`（见 README「聊天面板」），Forum 用会话里的 Fluxer 令牌**在服务端**调 Fluxer 原生 HTTP 接口（Discord 兼容形状，同微雨前端 `src/features/works/nativeApi.ts`），Fluxer 令牌不进浏览器。实现在 `site_v1/fluxer/native_http.py`（`RealFluxer` 的 `chat_*` 方法）。

配置：

| 环境变量 | 说明 |
| --- | --- |
| `FLUXER_API_URL` | Fluxer 原生接口基地址（同前端 auth config 的 `fluxer_api_url`，例如 `https://api.fluxer.app/v1`）。没配时聊天接口 503 |
| `FLUXER_BOT_TOKEN` | 删别人消息（作品作者 / 卡贴管理员 / 站务删言）用的管理身份，`Authorization: Bot <token>`，需要在作品论坛、公共频道有 MANAGE_MESSAGES。没配时退回用户令牌 |
| `FLUXER_API_TIMEOUT` | 秒，默认 10 |

请求头：`Authorization: <用户令牌>`（不带 Bearer，同 nativeApi.ts）、`X-Fluxer-Features: channel_threads`。

| FluxerPort 方法 | 原生接口 | 说明 |
| --- | --- | --- |
| `chat_messages` | `GET /channels/{id}/messages?limit=&before=&after=&around=` | 返回按新到旧，Forum 统一成正序 |
| `chat_message` | `GET /channels/{id}/messages/{mid}` | 404 / `10008` → 不存在 |
| `chat_send` | `POST /channels/{id}/messages` `{content, nonce, enforce_nonce: true, message_reference?: {message_id, fail_if_not_exists: false}, allowed_mentions}` | 本站附件不经过 Fluxer（会剥 PNG tEXt），挂在 Forum 的 `site_chat_message_meta`；只有附件时 content 发「[图片]」 |
| `chat_edit` | `PATCH /channels/{id}/messages/{mid}` `{content}` | |
| `chat_delete` | `DELETE /channels/{id}/messages/{mid}` | 删别人的用 `FLUXER_BOT_TOKEN` |
| `chat_react` | `PUT` / `DELETE /channels/{id}/messages/{mid}/reactions/{emoji}/@me` | 自定义表情是 `name:id` |
| `chat_ack` | `POST /channels/{id}/messages/{mid}/ack` `{}` | Forum 自己也记已读（`site_chat_read_state`），这里只是同步给 Fluxer 客户端 |
| `chat_typing` | `POST /channels/{id}/typing` | 失败忽略 |
| `chat_dm_channels` | `GET /users/@me/channels` | 只取 `type=1`（私信，不做群组）；对方 = `recipients` 里不是自己的那个 |
| `chat_open_dm` | `POST /users/@me/channels` `{recipient_id}` | 已有时 Fluxer 返回原频道；隐私、屏蔽 Forum 先判断 |

错误映射：401 → 401 `unauthenticated`（下一个请求会话降为匿名）；404、403 `50001 Missing Access`、`10003 Unknown Channel`、`10008 Unknown Message` → 404；其他 403 → 403 `forbidden`；超时、5xx、429 → 503。

还需要 Fluxer 配合的：

- 第 5 节 `channel.info_service` 要支持 `kind: "channel"`（公共聊天区频道 ID → `{channel_id, member_count}`）。
- 第 4 节 `read_states.summary_service` 加 `channels`（公共聊天区，形状同 `dms`，只列进过的）。
- 原生 Message 带 `referenced_message.author.id`（回复我的算 @我）和创建响应里回显 `nonce`。
- 本站上传的图片在 Fluxer 客户端里看不到（只有「[图片]」）；要互通需要 Fluxer 支持外链附件 / embed。
- 实时：见 README「聊天面板」的实时方案，需要 Fluxer 网关事件（MESSAGE_CREATE/UPDATE/DELETE、MESSAGE_REACTION_*）经 NATS 发给 Forum。
