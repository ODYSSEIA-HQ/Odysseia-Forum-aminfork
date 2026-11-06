"""协议 3.3 上传和附件对象。

流程：POST /uploads 建 attachment 行（state=uploading，uploadId = attachment.id）并签发直传地址
→ 浏览器把文件原样 PUT 到存储 → POST /uploads/{id}/complete 检查对象已到、大小一致，置 processing，
排分级任务（最多等 SITE_CLASSIFY_INLINE_WAIT 秒，等到了直接返回结果）→ ready / rejected。

附件状态字段（给作品、审核领域读）：
  state         uploading（不对外）/ processing / ready / rejected
  rating        对外分级（general / r18 / r18g）；拿不准时是保守值
  auto_rating   模型结论（general / r18 / r18g / suspect / blocked；非图片为空）
  model_scores  模型分数
  review_state  auto / pending_review（suspect，等人工）/ reviewed（人工改过）
  reject_reason rating_conflict（分级高于作品分级）/ blocked（违禁）
审核领域：待审图片 = attachment WHERE review_state='pending_review' AND deleted_at IS NULL；
处理时调 apply_review()。
"""

import asyncio
import logging
import re
import secrets
from datetime import timedelta

from sqlalchemy import select

from models.attachment import Attachment
from models.work import Work
from shared.time_utils import utc_now
from site_v1 import mode as modes
from site_v1.classifier import Classification, ClassifyInput, get_classifier, image_size
from site_v1.config import get_config
from site_v1.context import SiteContext, Viewer
from site_v1.deps import get_session_factory
from site_v1.dto import id_str
from site_v1.errors import conflict, forbidden, invalid, not_found
from site_v1.files_config import get_files_config
from site_v1.services.work_access import (
    can_manage_work,
    parse_id,
    work_exists,
    work_rating,
)
from site_v1.storage import get_storage

logger = logging.getLogger(__name__)

PURPOSES = ("cover", "gallery", "version_file", "chat", "avatar", "discussion")
WORK_PURPOSES = ("cover", "gallery", "version_file")
IMAGE_ONLY_PURPOSES = ("cover", "gallery", "avatar")
QUEUE_PURPOSES = ("cover", "gallery", "chat", "discussion")
"""拿不准的图片进人工队列的用途（和 moderation.IMAGE_USAGES 一致）。"""
FILENAME_MAX = 200
HEAD_BYTES = 64 * 1024
STALE_PROCESSING_SECONDS = 120

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def is_image(mime: str) -> bool:
    return mime.startswith("image/")


def clean_filename(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    name = re.split(r"[\\/]", raw)[-1]
    name = _CONTROL.sub("", name).strip()
    if not name or name in {".", ".."}:
        return None
    return name[-FILENAME_MAX:]


def storage_key(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-80:].lstrip(".") or "file"
    return f"att/{secrets.token_hex(16)}/{safe}"


# ---------------- 附件对象 ----------------


def attachment_dto(
    att: Attachment,
    ctx: SiteContext,
    *,
    url: str | None = None,
    thumb: bool = True,
) -> dict:
    """协议 Attachment。

    - 有效模式看不到这个分级 → url/thumbUrl 为 null，placeholder=true；
    - 全年龄图：公开地址（或长效签名地址）；R18/R18G：按账号签发的 10 分钟签名地址；
    - 没 ready 的不给地址；url 参数可以覆盖地址（版本文件给下载接口地址）。
    """
    rating = att.rating or modes.GENERAL
    visible = rating in ctx.visible_ratings()
    link = None
    if visible and att.state == "ready":
        if url is not None:
            link = url
        else:
            link = get_storage().read_url(
                att.storage_key,
                mime=att.mime,
                public=rating == modes.GENERAL,
                filename=att.filename,
                viewer_id=ctx.viewer.user_id if ctx.viewer else None,
            )
    data = {
        "id": id_str(att.id),
        "kind": att.kind,
        "mime": att.mime,
        "size": int(att.size),
        "name": att.filename,
        "url": link,
        "placeholder": not visible,
        "thumbUrl": link if (thumb and att.kind == "image") else None,
        "rating": rating,
        "reviewState": att.review_state,
        "state": "processing" if att.state == "uploading" else att.state,
        "rejectReason": att.reject_reason,
    }
    if att.width is not None and att.height is not None:
        data["width"] = att.width
        data["height"] = att.height
    return data


async def load_attachments(session, ids) -> dict[int, Attachment]:
    """批量取附件（未删除的）；作品领域序列化封面、图库时用。"""
    wanted = {i for i in (parse_id(x) for x in ids) if i}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(Attachment).where(
                Attachment.id.in_(wanted), Attachment.deleted_at.is_(None)
            )
        )
    ).scalars()
    return {a.id: a for a in rows}


async def usable_work_attachments(
    session, raw_ids, *, work_id: int, purposes: tuple[str, ...]
) -> list[Attachment] | None:
    """作品要引用的附件（coverId、galleryIds、fileIds）：都要存在、属于这个作品、用途对、state=ready。
    全部合格返回附件列表（顺序同 raw_ids），有一个不合格返回 None（调用方报 422）。"""
    if not isinstance(raw_ids, list):
        return None
    ids = [parse_id(x) for x in raw_ids]
    if any(i is None for i in ids) or len(set(ids)) != len(ids):
        return None
    found = await load_attachments(session, ids)
    result = []
    for i in ids:
        att = found.get(i)
        if (
            att is None
            or att.work_id != work_id
            or att.purpose not in purposes
            or att.state != "ready"
        ):
            return None
        result.append(att)
    return result


# ---------------- POST /uploads ----------------


async def create_upload(viewer: Viewer, body: dict, *, base: str | None) -> dict:
    config = get_config()
    fields: dict[str, str] = {}
    purpose = body.get("purpose")
    if purpose not in PURPOSES:
        fields["purpose"] = "用途不对"
    filename = clean_filename(body.get("filename"))
    if filename is None:
        fields["filename"] = "缺少文件名"
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        fields["size"] = "文件是空的"
        size = None
    raw_mime = body.get("mime")
    mime = raw_mime.split(";")[0].strip().lower() if isinstance(raw_mime, str) else ""
    if mime not in {m.lower() for m in config.allowed_mimes}:
        fields["mime"] = "不支持这种文件类型（支持 PNG/JPEG/WebP/GIF、JSON、ZIP、MP4）"
    elif purpose in IMAGE_ONLY_PURPOSES and not is_image(mime):
        fields["mime"] = "封面、图库和头像只能用图片"
    elif size is not None:
        limit = config.image_max_bytes if is_image(mime) else config.file_max_bytes
        if size > limit:
            kind = "图片" if is_image(mime) else "文件"
            fields["size"] = f"文件太大，{kind}最大 {limit // 1024 // 1024} MB"
    channel_id = body.get("channelId")
    if channel_id is not None and (not isinstance(channel_id, str) or not channel_id):
        fields["channelId"] = "频道 ID 不对"
    # 聊天附件：私信带 channelId（会话 ID），作品交流区带 workId，公共聊天区两个都可以不带
    # （前端 chat-nodebb/api.ts 的 uploadChatFile）；发送时再按目标检查分级
    work_id = None
    if purpose in WORK_PURPOSES:
        work_id = parse_id(body.get("workId"))
        if work_id is None:
            fields["workId"] = "先建草稿再上传"
    if fields:
        raise invalid(fields)

    async with get_session_factory()() as session:
        if work_id is not None:
            work = await session.get(Work, work_id)
            if not work_exists(work):
                raise invalid({"workId": "先建草稿再上传"})
            if not can_manage_work(work, viewer):
                raise forbidden("只有作者和管理员能给作品上传文件")
        att = Attachment(
            owner_id=viewer.user_id,
            purpose=purpose,
            work_id=work_id,
            channel_id=(
                channel_id
                or (f"work-{str(body.get('workId'))[:40]}" if purpose == "chat" and body.get("workId") else None)
                if purpose in ("chat", "discussion")
                else None
            ),
            kind="image" if is_image(mime) and purpose != "version_file" else "file",
            filename=filename,
            mime=mime,
            size=size,
            storage_key=storage_key(filename),
            state="uploading",
        )
        session.add(att)
        await session.commit()
        await session.refresh(att)

    ticket = get_storage().upload_ticket(att.storage_key, mime, size, base=base)
    return {"uploadId": id_str(att.id), **ticket}


# ---------------- complete、分级 ----------------

_inflight: dict[int, asyncio.Task] = {}


def schedule_classification(attachment_id: int) -> asyncio.Task:
    task = _inflight.get(attachment_id)
    if task is not None and not task.done():
        return task
    task = asyncio.create_task(_classify_safely(attachment_id))
    _inflight[attachment_id] = task
    task.add_done_callback(lambda _t: _inflight.pop(attachment_id, None))
    return task


async def _classify_safely(attachment_id: int) -> None:
    try:
        await classify_attachment(attachment_id)
    except Exception:
        logger.exception(
            "附件 %s 分级失败，留在 processing，稍后查询时重试", attachment_id
        )


async def _owned(session, raw_id, viewer: Viewer, *, for_update=False) -> Attachment:
    att_id = parse_id(raw_id)
    att = (
        await session.get(Attachment, att_id, with_for_update=for_update)
        if att_id
        else None
    )
    if att is None or att.owner_id != viewer.user_id or att.deleted_at is not None:
        raise not_found()
    return att


async def complete_upload(viewer: Viewer, raw_id) -> str:
    """返回 processing / ready / rejected；对象还没到 409。"""
    storage = get_storage()
    async with get_session_factory()() as session:
        att = await _owned(session, raw_id, viewer, for_update=True)
        if att.state != "uploading":
            state = att.state
            att_id = att.id
            stale = _stale(att)
            await session.rollback()
            if stale:
                schedule_classification(att_id)
            return state
        size = await storage.stat(att.storage_key)
        if size is None:
            raise conflict("文件还没有传完")
        if size != att.size:
            raise conflict("文件大小和申请时不一致，请重新上传", reason="size_mismatch")
        att.state = "processing"
        att.completed_at = utc_now()
        att_id = att.id
        await session.commit()

    task = schedule_classification(att_id)
    wait = get_files_config().classify_inline_wait
    if wait > 0:
        await asyncio.wait({task}, timeout=wait)
    async with get_session_factory()() as session:
        att = await session.get(Attachment, att_id)
        return att.state


def _stale(att: Attachment) -> bool:
    return (
        att.state == "processing"
        and att.completed_at is not None
        and (utc_now() - att.completed_at).total_seconds() > STALE_PROCESSING_SECONDS
        and att.id not in _inflight
    )


def rating_ceiling(att: Attachment, work: Work | None) -> str | None:
    """附件分级上限：已发布作品的封面、图库、版本文件不能高于作品分级；头像只能全年龄。
    草稿还没定分级，先不拦（发布时由作品领域检查）。"""
    if att.purpose in WORK_PURPOSES and work is not None and work.status != "draft":
        return work_rating(work)
    if att.purpose == "avatar":
        return modes.GENERAL
    return None


def conservative_rating(result_rating: str | None, work: Work | None) -> str:
    """拿不准（suspect）时先按什么分级处理（01 号文档 5.1 表格）：
    全年龄作品（或不属于作品）→ r18；R18 作品 → r18g；R18G 作品 → r18g（通过，可抽查）。
    模型偏向的分级更高时取更高的。"""
    base = work_rating(work) if work is not None else modes.GENERAL
    above = modes.R18 if base == modes.GENERAL else modes.R18G
    lean = result_rating or modes.R18
    return lean if modes.rating_rank(lean) > modes.rating_rank(above) else above


async def reviewed_by_hash(session, sha: str | None, exclude_id: int) -> Attachment | None:
    """同一张图（按内容哈希）人工复核过：再上传直接用复核结果（01 号文档 5.1）。"""
    if not sha:
        return None
    return (
        await session.execute(
            select(Attachment)
            .where(
                Attachment.sha256 == sha,
                Attachment.kind == "image",
                Attachment.review_state == "reviewed",
                Attachment.id != exclude_id,
            )
            .order_by(Attachment.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def classify_attachment(attachment_id: int) -> None:
    storage = get_storage()
    factory = get_session_factory()
    async with factory() as session:
        att = await session.get(Attachment, attachment_id)
        if att is None or att.state != "processing":
            return
        snapshot = (att.storage_key, att.mime, att.filename, att.size, att.purpose, att.kind)
    key, mime, filename, size, _purpose, kind = snapshot

    width = height = None
    sha = await storage.sha256(key) if hasattr(storage, "sha256") else None
    result: Classification | None = None
    prior: Attachment | None = None
    image = is_image(mime)
    if image:
        head = await storage.read_head(key, HEAD_BYTES)
        dims = image_size(head)
        if dims:
            width, height = dims
        if kind == "image":
            async with factory() as session:
                prior = await reviewed_by_hash(session, sha, attachment_id)
        if prior is None:
            result = await get_classifier().classify(
                ClassifyInput(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime=mime,
                    size=size,
                    width=width,
                    height=height,
                    url=storage.read_url(key, mime=mime, public=False),
                    sha256=sha,
                    head=head,
                )
            )

    async with factory() as session:
        att = await session.get(Attachment, attachment_id, with_for_update=True)
        if att is None or att.state != "processing":
            return
        work = await session.get(Work, att.work_id) if att.work_id else None
        att.width, att.height = width, height
        att.sha256 = sha
        ceiling = rating_ceiling(att, work)
        if prior is not None:
            # 复核过的同一张图：沿用人工结论，不再过模型、不再进队列
            att.auto_rating = prior.auto_rating
            att.model_scores = prior.model_scores
            att.review_state = "reviewed"
            if prior.state == "rejected" and prior.reject_reason == "blocked":
                att.state = "rejected"
                att.reject_reason = "blocked"
            else:
                att.rating = prior.rating or modes.GENERAL
                _ready_or_conflict(att, ceiling)
        elif result is None:
            # 非图片不过模型：按作品当前分级记
            att.rating = work_rating(work) if work is not None else modes.GENERAL
            att.state = "ready"
        else:
            att.auto_rating = result.auto_rating
            att.model_scores = result.scores
            att.review_state = "pending_review" if result.needs_review else "auto"
            if result.blocked:
                att.state = "rejected"
                att.reject_reason = "blocked"
            elif result.needs_review:
                # 拿不准：先按保守分级放行（只在更高的模式下可见），进人工队列，不拦上传
                att.rating = conservative_rating(result.rating, work)
                att.state = "ready"
            else:
                att.rating = result.rating or modes.GENERAL
                _ready_or_conflict(att, ceiling)
        needs_queue = (
            att.review_state == "pending_review"
            and att.kind == "image"
            and att.state == "ready"
            and att.purpose in QUEUE_PURPOSES
        )
        if needs_queue and work is not None and (work.moderation_state or "ok") == "ok":
            work.moderation_state = "pending_review"
        if needs_queue:
            # 同一个事务里入队：拿不准的图和它的队列项一起出现
            from site_v1.services.moderation import enqueue_image

            await session.flush()
            await enqueue_image(session, att)
        await session.commit()


def _ready_or_conflict(att: Attachment, ceiling: str | None) -> None:
    if ceiling is not None and modes.rating_rank(att.rating) > modes.rating_rank(ceiling):
        att.state = "rejected"
        att.reject_reason = "rating_conflict"
    else:
        att.state = "ready"
        att.reject_reason = None


async def get_upload(viewer: Viewer, raw_id, ctx: SiteContext) -> dict:
    async with get_session_factory()() as session:
        att = await _owned(session, raw_id, viewer)
    if att.state == "uploading":
        raise conflict("还没有通知上传完成")
    if _stale(att):
        schedule_classification(att.id)
    return attachment_dto(att, ctx)


def apply_review(
    att: Attachment,
    *,
    rating: str | None = None,
    blocked: bool = False,
) -> None:
    """人工审核结果写回附件（审核领域调用，不提交事务）。

    blocked=True → rejected/blocked；否则 rating（不给就保留当前值）生效，state=ready。
    分级是否和作品冲突由审核领域决定（审核员可以同时调整作品分级）。
    """
    if blocked:
        att.state = "rejected"
        att.reject_reason = "blocked"
    else:
        if rating is not None:
            if rating not in modes.RATINGS:
                raise ValueError(f"分级取值不对：{rating}")
            att.rating = rating
        att.state = "ready"
        att.reject_reason = None
    att.review_state = "reviewed"


# ---------------- 过期清理 ----------------

STALE_UPLOAD_GRACE_SECONDS = 3600


async def purge_stale_uploads(*, limit: int = 500) -> int:
    """清理申请了直传却一直没 complete 的附件（state=uploading）：直传地址过期
    （SITE_UPLOAD_TICKET_TTL）再加 1 小时宽限后，删存储里的对象（传了一半或传完没确认的），
    行标记 deleted_at。返回这次清理的条数；幂等。由定时任务调用（见 docs/site_v1/README.md）。"""
    cutoff = utc_now() - timedelta(
        seconds=get_files_config().ticket_ttl + STALE_UPLOAD_GRACE_SECONDS
    )
    storage = get_storage()
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(Attachment.id, Attachment.storage_key)
                .where(
                    Attachment.state == "uploading",
                    Attachment.deleted_at.is_(None),
                    Attachment.created_at < cutoff,
                )
                .order_by(Attachment.id)
                .limit(limit)
            )
        ).all()
    purged = 0
    for att_id, key in rows:
        try:
            await storage.delete(key)
        except Exception:
            logger.exception("删除过期直传对象 %s 失败，下次再试", key)
            continue
        async with factory() as session:
            att = await session.get(Attachment, att_id, with_for_update=True)
            if att is None or att.state != "uploading" or att.deleted_at is not None:
                continue
            att.deleted_at = utc_now()
            await session.commit()
            purged += 1
    return purged
