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

`GET /chat/unread-summary` 用（聊天外层，3.5）。

请求：`{ "token": "flx_...", "user_id": "..." }`

成功：

```json
{
  "works": [{ "thread_id": "原生主帖 ID", "unread": 3, "mentions": 1 }],
  "dms":   [{ "channel_id": "私信频道 ID", "unread": 1, "mentions": 0 }]
}
```

Forum 再按有效模式剔除受限作品（全年龄模式下不计数）。

## 5. 频道信息 `rpc.content.v1.channel.info_service`

`GET /chat/targets/{kind}/{id}` 用。

请求：`{ "token": "flx_..." | null, "kind": "work" | "dm", "target_id": "原生主帖 ID 或私信频道 ID" }`

成功：`{ "channel_id": "...", "member_count": 12 }`；不存在返回错误码 `NOT_FOUND`（Forum 映射 404）。

## 6. 以后要的（不在第一期 40 个接口里，先记下）

- **给已登录账号绑定 Discord**（`GET /auth/discord/start?intent=link`）：`rpc.identity.v1.discord.link_service {token, discord_user_id, verified_at, profile}`。现在回调里 `intent=link` 返回 `/login?error=link_unsupported`。
- **DC 联动注册**（10-31 设计的 `/auth/register/*` 七个接口）：注册票据、设密码（服务端可调的 `rpc.auth.v1.password.set_service`，替代现在发给浏览器的 `external.prove` 凭证）、2FA。
