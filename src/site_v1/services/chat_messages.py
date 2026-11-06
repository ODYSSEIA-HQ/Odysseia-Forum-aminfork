"""聊天消息代转（协议 3.5「NodeBB 路线参考接口」、3.8 会话列表）。

前端用自己的聊天面板（VITE_CHAT_PANEL=nodebb），调这组接口；Forum 用会话里的 Fluxer 令牌
经 FluxerPort.chat_* 读写 Fluxer 的频道消息，Fluxer 令牌不进浏览器：

- 作品交流区 /works/{id}/chat/...  = 作品的 Fluxer 原生主帖（moderation_common.native_thread_id）；
- 私信       /conversations/{id}/... = Fluxer 私信频道（会话 ID 就是私信频道 ID）；
- 公共聊天区 /channels/{id}/chat/... = 频道对象的 fluxer_channel_id（services/chat_channels）。
FLUXER_FAKE=1 时没有原生主帖 / fluxerChannelId 的用假频道 work-{id} / channel-{id}。

协议 3.5 的规则在这一层落实：
- R18 隔离：作品看不到（全年龄模式下的受限作品、下架、草稿）→ 404；公共频道分级不可见 → 404；
  附件按查看者的有效模式给占位（attachment_dto）；私信里 Fluxer 原生附件分级未知，按 R18 处理。
- 发图分级：public 作品的交流区不能发 R18 图、公共频道不能发高于频道分级的图（422 rating_conflict）。
- 作品内禁言（work_user_mute）、全站禁言（user_moderation_record）拦发送（403 muted）。
- 私信隐私（对方 prefs.dmPrivacy）和屏蔽每条消息都检查。
- 删言：本人 by_author；作品交流区里作者 / 卡贴管理员（work_access.can_manage_work）by_manager；
  moderation.content 或公共频道里 channels.manage 的站务 by_staff。
- @ 提醒：<@userId> 经 chat_notify 写提醒。
- 读性能：每个频道最新 50 条缓存在 Redis（site:chat:latest:{channel}），写操作清缓存，
  另有短 TTL（SITE_CHAT_CACHE_TTL，默认 15 秒）兜住从 Fluxer 客户端直接发的消息。
"""

import json
import logging
import os
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.attachment import Attachment
from models.site_account import SiteUserPrefs
from models.site_chat import SiteChatMessageMeta, SiteChatReadState
from models.user import User
from models.work import Work
from shared.time_utils import utc_now
from site_v1 import mode as modes
from site_v1.config import get_config
from site_v1.deps import get_redis
from site_v1.dto import iso_utc, load_users, user_dto
from site_v1.errors import (
    SiteError,
    conflict,
    forbidden,
    invalid,
    not_found,
    unauthenticated,
    unavailable,
)
from site_v1.fluxer import (
    FluxerForbidden,
    FluxerMessage,
    FluxerNotFound,
    FluxerUnauthorized,
    FluxerUnavailable,
    get_fluxer,
)
from site_v1.services import chat_notify
from site_v1.services.attachments import attachment_dto, load_attachments
from site_v1.services.chat_channels import (
    PublicChannelRef,
    fluxer_channel_of,
    get_public_channel,
)
from site_v1.services.moderation_common import (
    missing_user,
    native_thread_id,
    site_muted_until,
    visible_work,
    work_mute,
)
from site_v1.services.work_access import can_manage_work, restricted_ids, work_rating

logger = logging.getLogger(__name__)

KINDS = ("work", "dm", "channel")
MAX_BODY = 2000
MAX_ATTACHMENTS = 10
MAX_REACTIONS = 20
CACHE_SIZE = 50
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
PERMANENT = "9999-12-31T00:00:00.000Z"
_MSG_ID = re.compile(r"[0-9]{1,20}")
_CONTROL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
DEFAULT_FEATURES = {"attach": True, "poll": True, "fun": True}


def cache_ttl() -> int:
    try:
        return max(1, int(os.getenv("SITE_CHAT_CACHE_TTL", "15")))
    except ValueError:
        return 15


def _cache_key(channel_id: str) -> str:
    return f"site:chat:latest:{channel_id}"


def _react_key(channel_id: str, message_id: str) -> str:
    return f"site:chat:rx:{channel_id}:{message_id}"


def _nonce_key(channel_id: str, user_id: str, nonce: str) -> str:
    return f"site:chat:nonce:{channel_id}:{user_id}:{nonce}"


# ---------------- 目标 → Fluxer 频道 ----------------


@dataclass
class Chan:
    kind: str
    id: str
    channel_id: str
    title: str
    work: Work | None = None
    public: PublicChannelRef | None = None
    peer_id: str | None = None

    @property
    def target(self) -> dict:
        return {"kind": self.kind, "id": self.id}

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


async def call(awaitable):
    """FluxerPort 的错误 → 协议错误。"""
    try:
        return await awaitable
    except FluxerNotFound as exc:
        raise not_found() from exc
    except FluxerForbidden as exc:
        raise forbidden(str(exc) or "没有权限") from exc
    except FluxerUnauthorized as exc:
        raise unauthenticated("登录已失效，请重新登录") from exc
    except FluxerUnavailable as exc:
        raise unavailable("聊天服务暂时不可用") from exc


async def work_channel_id(session, work: Work) -> str | None:
    thread = await native_thread_id(session, work)
    if thread is None and get_config().fluxer_fake:
        thread = f"work-{work.id}"
    return thread


async def resolve(session, ctx, kind: str, raw_id: str) -> Chan:
    """这个查看者在当前有效模式下能打开的目标；不能一律 404（私信未登录 401）。"""
    if kind == "work":
        work = await visible_work(session, ctx, raw_id)
        if work is None:
            raise not_found()
        channel_id = await work_channel_id(session, work)
        if not channel_id:
            raise not_found("交流区还没准备好")
        # 后面会 commit：作品行从会话里摘下来，免得提交后属性过期再去懒加载
        session.expunge(work)
        return Chan("work", str(work.id), channel_id, work.title, work=work)
    if kind == "channel":
        ref = await get_public_channel(session, raw_id)
        if ref is None or ref.rating not in ctx.visible_ratings():
            raise not_found()
        channel_id = fluxer_channel_of(ref)
        if not channel_id:
            raise not_found("这个频道还没准备好")
        return Chan("channel", ref.id, channel_id, ref.name, public=ref)
    if kind == "dm":
        if ctx.viewer is None:
            raise unauthenticated()
        if not raw_id or len(raw_id) > 64:
            raise not_found()
        info = await call(get_fluxer().chat_target_info(ctx.fluxer_token, "dm", raw_id))
        if not info:
            raise not_found()
        peer = str(info.get("peer_user_id") or "") or None
        return Chan(
            "dm", raw_id, str(info.get("channel_id") or raw_id), "私信", peer_id=peer
        )
    raise not_found()


# ---------------- 最新 50 条缓存 ----------------


async def _cache_get(channel_id: str):
    try:
        raw = await get_redis().get(_cache_key(channel_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return [FluxerMessage.from_dict(d) for d in data["items"]], bool(data["more"])
    except (ValueError, KeyError, TypeError):
        return None


async def invalidate(channel_id: str) -> None:
    try:
        await get_redis().delete(_cache_key(channel_id))
    except Exception:
        logger.warning("清聊天缓存失败：%s", channel_id)


async def latest(ctx, chan: Chan) -> tuple[list[FluxerMessage], bool]:
    """频道最新 50 条（正序）和更早还有没有。"""
    cached = await _cache_get(chan.channel_id)
    if cached is not None:
        return cached
    viewer = ctx.viewer
    items = await call(
        get_fluxer().chat_messages(
            ctx.fluxer_token,
            viewer.user_id if viewer else None,
            chan.channel_id,
            limit=CACHE_SIZE + 1,
        )
    )
    more = len(items) > CACHE_SIZE
    items = items[-CACHE_SIZE:]
    try:
        await get_redis().set(
            _cache_key(chan.channel_id),
            json.dumps({"items": [m.to_dict() for m in items], "more": more}),
            ex=cache_ttl(),
        )
    except Exception:
        logger.warning("写聊天缓存失败：%s", chan.channel_id)
    return items, more


# ---------------- 序列化 ----------------


def _num(message_id: str | None) -> int:
    return int(message_id) if message_id and message_id.isdigit() else 0


def _native_rating(chan: Chan) -> str:
    """Fluxer 原生附件没有经过本站分级：作品按作品分级，公共频道按频道分级，私信按 R18（拿不准按 R18）。"""
    if chan.work is not None:
        return work_rating(chan.work)
    if chan.public is not None:
        return chan.public.rating if chan.public.rating in modes.RATINGS else modes.R18
    return modes.R18


def _native_attachment(raw: dict, chan: Chan, ctx) -> dict:
    rating = _native_rating(chan)
    visible = rating in ctx.visible_ratings()
    mime = str(raw.get("content_type") or "application/octet-stream")
    kind = "image" if mime.startswith("image/") else "file"
    url = raw.get("url") if visible else None
    data = {
        "id": f"fx-{raw.get('id')}",
        "kind": kind,
        "mime": mime,
        "size": int(raw.get("size") or 0),
        "name": str(raw.get("filename") or "file"),
        "url": url,
        "placeholder": not visible,
        "thumbUrl": url if kind == "image" else None,
        "rating": rating,
        "reviewState": "auto",
        "state": "ready",
        "rejectReason": None,
    }
    if raw.get("width") and raw.get("height"):
        data["width"] = int(raw["width"])
        data["height"] = int(raw["height"])
    return data


def _native_author(message: FluxerMessage) -> dict:
    info = message.author or {}
    if not info.get("username"):
        return missing_user(message.author_id or "0")
    avatar = info.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{message.author_id}/{avatar}.png"
        if avatar and not str(avatar).startswith("http")
        else avatar
    )
    return user_dto(
        user_id=message.author_id,
        username=info["username"],
        display_name=info.get("global_name"),
        avatar_url=avatar_url,
        created_at=None,
        discord_linked=False,
        is_newcomer=False,
    )


async def _metas(
    session, channel_id: str, ids: list[str]
) -> dict[str, SiteChatMessageMeta]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(SiteChatMessageMeta).where(
                SiteChatMessageMeta.channel_id == channel_id,
                SiteChatMessageMeta.message_id.in_(ids),
            )
        )
    ).scalars()
    return {r.message_id: r for r in rows}


async def _my_reactions(
    channel_id: str, messages: list[FluxerMessage], user_id: str
) -> dict[str, set[str]]:
    """真实环境的 reactions.me：缓存里的 me 是取消息那个人的，改用本站记下的「谁点了什么」。"""
    wanted = [
        m for m in messages if any(r.get("user_ids") is None for r in m.reactions)
    ]
    if not wanted:
        return {}
    result: dict[str, set[str]] = {}
    try:
        redis = get_redis()
        for m in wanted:
            members = await redis.smembers(_react_key(channel_id, m.id))
            result[m.id] = {
                x.split("|", 1)[1] for x in members if x.startswith(f"{user_id}|")
            }
    except Exception:
        return {}
    return result


async def to_dtos(
    session, ctx, chan: Chan, messages: list[FluxerMessage]
) -> list[dict]:
    viewer_id = ctx.viewer.user_id if ctx.viewer else None
    users = await load_users(session, [m.author_id for m in messages])
    metas = await _metas(session, chan.channel_id, [m.id for m in messages])
    att_ids = [a for meta in metas.values() for a in (meta.attachment_ids or [])]
    atts = await load_attachments(session, att_ids)
    mine = (
        await _my_reactions(chan.channel_id, messages, viewer_id) if viewer_id else {}
    )
    out = []
    for m in messages:
        meta = metas.get(m.id)
        deleted = m.deleted_state or (meta.deleted_state if meta else None)
        attachments = []
        reactions = []
        if not deleted:
            for aid in (meta.attachment_ids if meta else []) or []:
                att = atts.get(int(aid)) if str(aid).isdigit() else None
                if att is not None:
                    attachments.append(attachment_dto(att, ctx))
            attachments.extend(_native_attachment(a, chan, ctx) for a in m.attachments)
            for r in m.reactions:
                if int(r.get("count") or 0) <= 0:
                    continue
                if r.get("user_ids") is not None:
                    me = bool(viewer_id and viewer_id in r["user_ids"])
                else:
                    me = r["emoji"] in mine.get(m.id, set())
                reactions.append(
                    {"emoji": r["emoji"], "count": int(r["count"]), "me": me}
                )
        out.append(
            {
                "id": m.id,
                "channelId": chan.key,
                "author": users.get(m.author_id) or _native_author(m),
                "body": "" if deleted else m.content,
                "attachments": attachments,
                "replyTo": m.reply_to,
                "reactions": reactions,
                "editedAt": None if deleted else m.edited_at,
                "deletedState": deleted,
                "createdAt": m.created_at,
                # nonce 只还给发送者本人
                "nonce": m.nonce if viewer_id and viewer_id == m.author_id else None,
            }
        )
    return out


# ---------------- GET .../messages ----------------


def _cursor(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    if not _MSG_ID.fullmatch(raw):
        raise not_found()
    return raw


def _limit(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


async def list_messages(
    session, ctx, kind, raw_id, *, before, after, around, limit
) -> dict:
    chan = await resolve(session, ctx, kind, raw_id)
    before, after, around = _cursor(before), _cursor(after), _cursor(around)
    if sum(x is not None for x in (before, after, around)) > 1:
        raise invalid({"before": "before、after、around 只能带一个"})
    limit = _limit(limit)
    port = get_fluxer()
    uid = ctx.viewer.user_id if ctx.viewer else None
    token = ctx.fluxer_token

    if before is None and around is None:
        items, more = await latest(ctx, chan)
        ids = [m.id for m in items]
        if after is None and limit <= CACHE_SIZE:
            page = items[-limit:]
            return await _page(
                session, ctx, chan, page, more or len(items) > len(page), False
            )
        if after is not None and after in ids:
            # 轮询（after=最后一条）直接从缓存给
            rest = items[ids.index(after) + 1 :]
            return await _page(
                session, ctx, chan, rest[:limit], True, len(rest) > limit
            )

    if before is not None:
        items = await call(
            port.chat_messages(
                token, uid, chan.channel_id, before=before, limit=limit + 1
            )
        )
        more_before = len(items) > limit
        return await _page(session, ctx, chan, items[-limit:], more_before, True)
    if after is not None:
        items = await call(
            port.chat_messages(
                token, uid, chan.channel_id, after=after, limit=limit + 1
            )
        )
        return await _page(session, ctx, chan, items[:limit], True, len(items) > limit)
    if around is not None:
        items = await call(
            port.chat_messages(token, uid, chan.channel_id, around=around, limit=limit)
        )
        if not items:
            raise not_found()
        older = await call(
            port.chat_messages(token, uid, chan.channel_id, before=items[0].id, limit=1)
        )
        newer = await call(
            port.chat_messages(token, uid, chan.channel_id, after=items[-1].id, limit=1)
        )
        return await _page(session, ctx, chan, items, bool(older), bool(newer))
    # 最新一页但 limit 超过缓存大小
    items = await call(port.chat_messages(token, uid, chan.channel_id, limit=limit + 1))
    return await _page(session, ctx, chan, items[-limit:], len(items) > limit, False)


async def _page(session, ctx, chan, items, more_before, more_after) -> dict:
    return {
        "items": await to_dtos(session, ctx, chan, items),
        "hasMoreBefore": bool(more_before),
        "hasMoreAfter": bool(more_after),
    }


# ---------------- 发送 ----------------


def clean_body(raw) -> str:
    if not isinstance(raw, str):
        return ""
    return _CONTROL.sub("", raw).strip()


async def _mute_check(session, chan: Chan, user_id: str) -> None:
    if chan.work is not None:
        muted, until = await work_mute(session, chan.work.id, user_id)
        if muted:
            raise SiteError(
                403,
                "muted",
                "你在这个作品的交流区被禁言了",
                {"until": iso_utc(until), "scope": "work"},
            )
    site_until = await site_muted_until(session, user_id)
    if site_until is not None:
        raise SiteError(
            403, "muted", "你被禁言了", {"until": iso_utc(site_until), "scope": "site"}
        )


async def dm_allowed(session, sender_id: str, peer_id: str) -> None:
    """对方的私信设置（和 GET /users/{id} 的 viewer.dm 同一规则）、屏蔽。"""
    from site_v1.routers.users import _dm_state
    from site_v1.services import social

    prefs = await session.get(SiteUserPrefs, peer_id)
    state = _dm_state(prefs, False)
    if state == "closed":
        raise forbidden("对方不接收私信")
    if state == "friends_only":
        raise forbidden("对方只接收好友的私信，先加好友吧")
    if await social.is_blocked(session, peer_id, sender_id):
        raise forbidden("对方不接收你的私信")


async def _work_is_public(session, work: Work) -> bool:
    rating = work_rating(work)
    restricted = bool(await restricted_ids(session, [work.id]))
    return modes.work_visibility(rating, restricted) == "public"


async def _check_attachments(session, chan: Chan, user_id: str, raw) -> list[int]:
    ids = [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []
    if len(ids) > MAX_ATTACHMENTS:
        raise invalid({"attachments": f"一次最多 {MAX_ATTACHMENTS} 个附件"})
    if not ids:
        return []
    atts = await load_attachments(session, ids)
    result: list[int] = []
    public_work = (
        await _work_is_public(session, chan.work) if chan.work is not None else False
    )
    for raw_id in ids:
        att: Attachment | None = atts.get(int(raw_id)) if raw_id.isdigit() else None
        if att is None or att.owner_id != user_id:
            raise invalid({"attachments": "附件还没上传完成"})
        if att.state in ("uploading", "processing"):
            raise invalid({"attachments": "附件还在自动审核，稍等再发"})
        if att.state == "rejected":
            raise invalid({"attachments": "附件没有通过审核"})
        rating = att.rating or modes.GENERAL
        # 01 号文档 3.3：public 作品的交流区直接拦下 R18 图
        if chan.work is not None and public_work and rating != modes.GENERAL:
            raise SiteError(
                422,
                "rating_conflict",
                "这个作品对所有人可见，不能发 R18 图",
                {"rating": rating, "options": ["remove_attachment"]},
            )
        # 公共聊天区：图的分级不能高于频道
        if chan.public is not None and modes.rating_rank(rating) > modes.rating_rank(
            chan.public.rating
        ):
            raise SiteError(
                422,
                "rating_conflict",
                "这个频道的分级不能发这张图",
                {"rating": rating, "options": ["remove_attachment"]},
            )
        if att.id not in result:
            result.append(att.id)
    return result


async def _set_read(session, chan: Chan, user_id: str, message_id: str) -> bool:
    """已读只往后走；返回是否前进了。"""
    row = await session.get(
        SiteChatReadState, (user_id, chan.channel_id), with_for_update=True
    )
    if row is not None and _num(row.last_read_message_id) >= _num(message_id):
        return False
    if row is None:
        await session.execute(
            pg_insert(SiteChatReadState)
            .values(
                user_id=user_id,
                channel_id=chan.channel_id,
                target_kind=chan.kind,
                target_id=chan.id,
                last_read_message_id=message_id,
                updated_at=utc_now(),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "channel_id"],
                set_={"last_read_message_id": message_id, "updated_at": utc_now()},
            )
        )
    else:
        row.last_read_message_id = message_id
        row.updated_at = utc_now()
    return True


def excerpt_of(
    body: str, deleted: bool, has_attachments: bool, names: dict[str, str]
) -> str:
    if deleted:
        return "消息已删除"
    text = re.sub(
        r"<@!?([^>]+)>", lambda m: f"@{names.get(m.group(1), '用户')}", body or ""
    )
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        return text[:60]
    return "[图片]" if has_attachments else ""


async def send_message(session, ctx, kind, raw_id, body: dict) -> tuple[int, dict]:
    viewer = ctx.require_user()
    chan = await resolve(session, ctx, kind, raw_id)
    body = body if isinstance(body, dict) else {}
    port = get_fluxer()
    token = ctx.fluxer_token
    nonce = body.get("nonce") if isinstance(body.get("nonce"), str) else None
    nonce = nonce[:64] if nonce else None

    # 同一个人带同一个 nonce 重发：直接返回已经存下的那条
    if nonce:
        try:
            existing_id = await get_redis().get(
                _nonce_key(chan.channel_id, viewer.user_id, nonce)
            )
        except Exception:
            existing_id = None
        if existing_id:
            existing = await call(
                port.chat_message(token, viewer.user_id, chan.channel_id, existing_id)
            )
            if existing is not None:
                existing.nonce = nonce
                return 200, (await to_dtos(session, ctx, chan, [existing]))[0]

    if chan.kind == "dm" and chan.peer_id:
        await dm_allowed(session, viewer.user_id, chan.peer_id)
    await _mute_check(session, chan, viewer.user_id)

    text = clean_body(body.get("body"))
    if len(text) > MAX_BODY:
        raise invalid({"body": f"消息最多 {MAX_BODY} 字"})
    raw_attachments = body.get("attachments")
    if not text and not (isinstance(raw_attachments, list) and raw_attachments):
        raise invalid({"body": "消息不能为空"})
    attachment_ids = await _check_attachments(
        session, chan, viewer.user_id, raw_attachments
    )

    reply_to = body.get("replyTo") if isinstance(body.get("replyTo"), str) else None
    if reply_to:
        if (
            not _MSG_ID.fullmatch(reply_to)
            or await call(
                port.chat_message(token, viewer.user_id, chan.channel_id, reply_to)
            )
            is None
        ):
            raise invalid({"replyTo": "回复的消息不存在"})

    # 只有附件时 Fluxer 那边也要有点内容（真实 Fluxer 不收空消息；本站附件不经过 Fluxer）
    content = text
    if not content and attachment_ids and not get_config().fluxer_fake:
        content = "[图片]"
    message = await call(
        port.chat_send(
            token,
            viewer.user_id,
            chan.channel_id,
            content=content,
            nonce=nonce,
            reply_to=reply_to,
        )
    )
    if not get_config().fluxer_fake and not text:
        message.content = ""
    message.nonce = nonce
    session.add(
        SiteChatMessageMeta(
            channel_id=chan.channel_id,
            message_id=message.id,
            author_id=viewer.user_id,
            target_kind=chan.kind,
            target_id=chan.id,
            attachment_ids=[str(a) for a in attachment_ids],
        )
    )
    # 自己发的消息算已读
    await _set_read(session, chan, viewer.user_id, message.id)
    await session.flush()
    dto = (await to_dtos(session, ctx, chan, [message]))[0]

    recipients = chat_notify.mentioned_user_ids(text, viewer.user_id)
    if chan.kind == "dm":
        recipients = [r for r in recipients if r == chan.peer_id]
    if recipients:
        names = {
            u: d["displayName"]
            for u, d in (
                await load_users(session, chat_notify.MENTION.findall(text))
            ).items()
        }
        recipients = [r for r in recipients if r in names]
        await chat_notify.notify_mentions(
            session,
            recipients=recipients,
            actor=dto["author"],
            target=chan.target,
            title=chan.title if chan.kind != "dm" else dto["author"]["displayName"],
            message_id=message.id,
            excerpt=excerpt_of(text, False, bool(attachment_ids), names),
            created_at=message.created_at,
        )
    await session.commit()
    await invalidate(chan.channel_id)
    if nonce:
        try:
            await get_redis().set(
                _nonce_key(chan.channel_id, viewer.user_id, nonce), message.id, ex=600
            )
        except Exception:
            pass
    if not get_config().fluxer_fake:
        try:
            await port.chat_ack(token, viewer.user_id, chan.channel_id, message.id)
        except Exception:
            pass
    return 201, dto


# ---------------- 编辑、删除、表情 ----------------


async def _message(ctx, chan: Chan, message_id: str) -> FluxerMessage:
    if not _MSG_ID.fullmatch(message_id or ""):
        raise not_found()
    viewer = ctx.viewer
    m = await call(
        get_fluxer().chat_message(
            ctx.fluxer_token,
            viewer.user_id if viewer else None,
            chan.channel_id,
            message_id,
        )
    )
    if m is None:
        raise not_found()
    return m


async def _deleted_state(session, chan: Chan, m: FluxerMessage) -> str | None:
    if m.deleted_state:
        return m.deleted_state
    meta = (await _metas(session, chan.channel_id, [m.id])).get(m.id)
    return meta.deleted_state if meta else None


async def edit_message(session, ctx, kind, raw_id, message_id, body: dict) -> dict:
    viewer = ctx.require_user()
    chan = await resolve(session, ctx, kind, raw_id)
    m = await _message(ctx, chan, message_id)
    if m.author_id != viewer.user_id:
        raise forbidden("只能编辑自己的消息")
    if await _deleted_state(session, chan, m):
        raise conflict("消息已被删除")
    text = clean_body((body or {}).get("body") if isinstance(body, dict) else None)
    meta = (await _metas(session, chan.channel_id, [m.id])).get(m.id)
    has_attachments = bool(meta and meta.attachment_ids) or bool(m.attachments)
    if not text and not has_attachments:
        raise invalid({"body": "消息不能为空"})
    if len(text) > MAX_BODY:
        raise invalid({"body": f"消息最多 {MAX_BODY} 字"})
    updated = await call(
        get_fluxer().chat_edit(
            ctx.fluxer_token,
            viewer.user_id,
            chan.channel_id,
            m.id,
            content=text or "[图片]",
        )
    )
    if not text:
        updated.content = ""
    await invalidate(chan.channel_id)
    return (await to_dtos(session, ctx, chan, [updated]))[0]


async def delete_message(session, ctx, kind, raw_id, message_id) -> dict:
    viewer = ctx.require_user()
    chan = await resolve(session, ctx, kind, raw_id)
    m = await _message(ctx, chan, message_id)
    if await _deleted_state(session, chan, m):
        return (await to_dtos(session, ctx, chan, [m]))[0]
    if m.author_id == viewer.user_id:
        state = "by_author"
    elif chan.work is not None and can_manage_work(chan.work, viewer):
        state = "by_manager"
    elif viewer.has_permission("moderation.content"):
        state = "by_staff"
    elif chan.public is not None and viewer.has_permission("channels.manage"):
        state = "by_staff"
    else:
        raise forbidden(
            "只有站务能删除别人的消息"
            if chan.public is not None
            else "只有作者和卡贴管理员能删除别人的消息"
        )
    await call(
        get_fluxer().chat_delete(
            ctx.fluxer_token,
            viewer.user_id,
            chan.channel_id,
            m.id,
            deleted_state=state,
            as_moderator=state != "by_author",
        )
    )
    now = utc_now()
    await session.execute(
        pg_insert(SiteChatMessageMeta)
        .values(
            channel_id=chan.channel_id,
            message_id=m.id,
            author_id=m.author_id,
            target_kind=chan.kind,
            target_id=chan.id,
            attachment_ids=[],
            deleted_state=state,
            deleted_by=viewer.user_id,
            deleted_at=now,
            created_at=now,
        )
        .on_conflict_do_update(
            index_elements=["channel_id", "message_id"],
            set_={
                "deleted_state": state,
                "deleted_by": viewer.user_id,
                "deleted_at": now,
            },
        )
    )
    await session.commit()
    await invalidate(chan.channel_id)
    m.deleted_state = state
    return (await to_dtos(session, ctx, chan, [m]))[0]


async def react(session, ctx, kind, raw_id, message_id, emoji: str, on: bool) -> dict:
    viewer = ctx.require_user()
    chan = await resolve(session, ctx, kind, raw_id)
    m = await _message(ctx, chan, message_id)
    if await _deleted_state(session, chan, m):
        raise conflict("消息已被删除")
    if not emoji or len(emoji) > 8 or _CONTROL.search(emoji) or emoji.strip() != emoji:
        raise invalid({"emoji": "表情不对"})
    existing = {r["emoji"] for r in m.reactions if int(r.get("count") or 0) > 0}
    if on and emoji not in existing and len(existing) >= MAX_REACTIONS:
        raise invalid({"emoji": "这条消息的表情太多了"})
    port = get_fluxer()
    await call(
        port.chat_react(
            ctx.fluxer_token, viewer.user_id, chan.channel_id, m.id, emoji, on=on
        )
    )
    if (
        any(r.get("user_ids") is None for r in m.reactions)
        or not get_config().fluxer_fake
    ):
        try:
            redis = get_redis()
            key = _react_key(chan.channel_id, m.id)
            if on:
                await redis.sadd(key, f"{viewer.user_id}|{emoji}")
            else:
                await redis.srem(key, f"{viewer.user_id}|{emoji}")
            await redis.expire(key, 30 * 86400)
        except Exception:
            pass
    await invalidate(chan.channel_id)
    fresh = await _message(ctx, chan, m.id)
    return (await to_dtos(session, ctx, chan, [fresh]))[0]


# ---------------- 已读、状态 ----------------


async def unread_of(
    session, ctx, chan: Chan, user_id: str
) -> tuple[str | None, int, int]:
    """(已读位置, 未读数, @我数)。没进过的作品交流区、公共聊天区不算未读，私信算。"""
    row = await session.get(SiteChatReadState, (user_id, chan.channel_id))
    last_read = row.last_read_message_id if row else None
    if last_read is None and chan.kind != "dm":
        return None, 0, 0
    items, _more = await latest(ctx, chan)
    if last_read is not None:
        if items and _num(items[0].id) > _num(last_read):
            # 已读位置比缓存还旧：再往后取最多 100 条
            items = await call(
                get_fluxer().chat_messages(
                    ctx.fluxer_token,
                    user_id,
                    chan.channel_id,
                    after=last_read,
                    limit=MAX_LIMIT,
                )
            )
        items = [m for m in items if _num(m.id) > _num(last_read)]
    metas = await _metas(session, chan.channel_id, [m.id for m in items])
    mine = {m.id for m in items if m.author_id == user_id}
    count = mentions = 0
    for m in items:
        deleted = m.deleted_state or (
            metas[m.id].deleted_state if m.id in metas else None
        )
        if m.author_id == user_id or deleted:
            continue
        count += 1
        if (
            f"<@{user_id}>" in m.content
            or f"<@!{user_id}>" in m.content
            or (m.reply_to and (m.reply_to in mine or m.reply_to_author_id == user_id))
        ):
            mentions += 1
    return last_read, count, mentions


async def state_of(session, ctx, chan: Chan) -> dict:
    viewer = ctx.viewer
    result = {
        "target": chan.target,
        "lastReadMessageId": None,
        "unread": 0,
        "mentions": 0,
        "mutedUntil": None,
        "features": dict(DEFAULT_FEATURES),
    }
    if viewer is None:
        return result
    last_read, count, mentions = await unread_of(session, ctx, chan, viewer.user_id)
    deadlines = []
    permanent = False
    if chan.work is not None:
        muted, until = await work_mute(session, chan.work.id, viewer.user_id)
        if muted:
            if until is None:
                permanent = True
            else:
                deadlines.append(until)
    site_until = await site_muted_until(session, viewer.user_id)
    if site_until is not None:
        deadlines.append(site_until)
    result.update(
        lastReadMessageId=last_read,
        unread=count,
        mentions=mentions,
        mutedUntil=PERMANENT
        if permanent
        else iso_utc(max(deadlines))
        if deadlines
        else None,
    )
    return result


async def get_state(session, ctx, kind, raw_id) -> dict:
    chan = await resolve(session, ctx, kind, raw_id)
    return await state_of(session, ctx, chan)


async def mark_read(session, ctx, kind, raw_id, body) -> dict:
    viewer = ctx.require_user()
    chan = await resolve(session, ctx, kind, raw_id)
    message_id = body.get("messageId") if isinstance(body, dict) else None
    if not isinstance(message_id, str):
        raise invalid({"messageId": "缺少 messageId"})
    m = await _message(ctx, chan, message_id)
    moved = await _set_read(session, chan, viewer.user_id, m.id)
    await session.commit()
    if moved and not get_config().fluxer_fake:
        try:
            await get_fluxer().chat_ack(
                ctx.fluxer_token, viewer.user_id, chan.channel_id, m.id
            )
        except Exception:
            logger.info("同步已读到 Fluxer 失败：%s", chan.channel_id)
    return await state_of(session, ctx, chan)


async def typing(session, ctx, kind, raw_id) -> None:
    viewer = ctx.require_user()
    chan = await resolve(session, ctx, kind, raw_id)
    try:
        await get_fluxer().chat_typing(
            ctx.fluxer_token, viewer.user_id, chan.channel_id
        )
    except Exception:
        pass


# ---------------- 会话列表（3.8） ----------------


async def _peer_user(session, peer_id: str) -> dict:
    return (await load_users(session, [peer_id])).get(peer_id) or missing_user(peer_id)


async def conversation_dto(session, ctx, chan: Chan, peer: dict) -> dict:
    items, _ = await latest(ctx, chan)
    last = items[-1] if items else None
    last_message = None
    if last is not None:
        deleted = await _deleted_state(session, chan, last)
        meta = (await _metas(session, chan.channel_id, [last.id])).get(last.id)
        names = {}
        mentioned = chat_notify.MENTION.findall(last.content or "")
        if mentioned:
            names = {
                u: d["displayName"]
                for u, d in (await load_users(session, mentioned)).items()
            }
        last_message = {
            "excerpt": excerpt_of(
                last.content,
                bool(deleted),
                bool(meta and meta.attachment_ids) or bool(last.attachments),
                names,
            ),
            "at": last.created_at,
        }
    _, count, _ = await unread_of(session, ctx, chan, ctx.viewer.user_id)
    return {
        "id": chan.id,
        "type": "dm",
        "title": peer["displayName"],
        "avatar": peer.get("avatar"),
        "peer": peer,
        "lastMessage": last_message,
        "unread": count,
    }


async def list_conversations(session, ctx, page) -> dict:
    from site_v1.paging import encode_cursor

    viewer = ctx.require_user()
    channels = await call(
        get_fluxer().chat_dm_channels(ctx.fluxer_token, viewer.user_id)
    )
    rows = []
    for ch in channels:
        chan = Chan(
            "dm", ch["channel_id"], ch["channel_id"], "私信", peer_id=ch["peer_user_id"]
        )
        peer = await _peer_user(session, ch["peer_user_id"])
        rows.append((chan, peer, ch))
    convs = [await conversation_dto(session, ctx, chan, peer) for chan, peer, _ in rows]
    created = {chan.id: ch.get("created_at") or "" for chan, _, ch in rows}
    convs.sort(
        key=lambda c: (
            c["lastMessage"]["at"] if c["lastMessage"] else created.get(c["id"], "")
        ),
        reverse=True,
    )
    window = convs[page.offset : page.offset + page.limit]
    more = len(convs) > page.offset + page.limit
    return {
        "items": window,
        "nextCursor": encode_cursor({"o": page.offset + len(window)}) if more else None,
    }


async def create_conversation(session, ctx, body) -> tuple[int, dict]:
    viewer = ctx.require_user()
    raw = body.get("userId") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise invalid({"userId": "请填写对方的用户 ID"})
    key = raw.strip()
    peer_row = await session.get(User, key) if len(key) <= 64 else None
    if peer_row is None:
        peer_row = (
            await session.execute(select(User).where(User.username == key).limit(1))
        ).scalar_one_or_none()
    if peer_row is None or peer_row.merged_into_id:
        raise invalid({"userId": "找不到这个用户"})
    if peer_row.id == viewer.user_id:
        raise invalid({"userId": "不能给自己发私信"})
    port = get_fluxer()
    existing = next(
        (
            c
            for c in await call(port.chat_dm_channels(ctx.fluxer_token, viewer.user_id))
            if c["peer_user_id"] == peer_row.id
        ),
        None,
    )
    peer = await _peer_user(session, peer_row.id)
    if existing is not None:
        chan = Chan(
            "dm",
            existing["channel_id"],
            existing["channel_id"],
            "私信",
            peer_id=peer_row.id,
        )
        return 200, await conversation_dto(session, ctx, chan, peer)
    await dm_allowed(session, viewer.user_id, peer_row.id)
    channel_id = await call(
        port.chat_open_dm(ctx.fluxer_token, viewer.user_id, peer_row.id)
    )
    chan = Chan("dm", channel_id, channel_id, "私信", peer_id=peer_row.id)
    return 201, await conversation_dto(session, ctx, chan, peer)
