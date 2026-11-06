"""聊天里的 @ 提醒：按协议 Notification 形状组好，交给提醒领域写入。

提醒表和 GET /notifications 由「我的 / 提醒」领域做。这里只留一个调用点：
deliver_notification(session, recipient_id, notification)。提醒领域实现
site_v1.services.notifications.create_notification(session, recipient_id, notification)
（同一个事务里写入，不要自己 commit）后这里自动接上；还没有时只记日志。

Notification（协议 / 前端 src/shared/protocol/models.ts）：
{
  "category": "mention",
  "actor": User,
  "target": {"type": "work" | "conversation" | "channel", "id": "...", "title": "..."},
  "excerpt": "消息摘要（≤120 字）",
  "createdAt": IsoTime,
  "read": false,
  "context": {"workId"?, "conversationId"?, "channelId"?, "messageId"},
  "mention": {"scope": "user"}
}
id 由提醒领域生成。可见性（受限作品在全年龄模式下不出现）由提醒列表读取时按有效模式过滤：
这里不知道收件人以后用什么模式看。
"""

import inspect
import logging
import re

logger = logging.getLogger(__name__)

MENTION = re.compile(r"<@!?([0-9A-Za-z_-]{1,64})>")


def mentioned_user_ids(body: str, author_id: str) -> list[str]:
    seen: list[str] = []
    for uid in MENTION.findall(body or ""):
        if uid != author_id and uid not in seen:
            seen.append(uid)
    return seen[:20]


async def deliver_notification(session, recipient_id: str, notification: dict) -> None:
    """唯一的写入点。"""
    try:
        from site_v1.services.notifications import create_notification  # type: ignore
    except ImportError:
        logger.info(
            "提醒领域还没接上，@ 提醒只记日志：%s → %s",
            notification.get("target"),
            recipient_id,
        )
        return
    # create_notification 是同步函数（只 session.add）；以前这里直接 await 它，
    # 每次都抛 TypeError 被上层吞掉记一条异常日志。两种写法都兼容
    result = create_notification(session, recipient_id, notification)
    if inspect.isawaitable(result):
        await result


async def notify_mentions(
    session,
    *,
    recipients: list[str],
    actor: dict,
    target: dict,
    title: str,
    message_id: str,
    excerpt: str,
    created_at: str,
) -> None:
    kind = target["kind"]
    target_type = {"work": "work", "dm": "conversation", "channel": "channel"}[kind]
    context_key = {"work": "workId", "dm": "conversationId", "channel": "channelId"}[
        kind
    ]
    for recipient in recipients:
        notification = {
            "category": "mention",
            "actor": actor,
            "target": {"type": target_type, "id": target["id"], "title": title},
            "excerpt": excerpt[:120],
            "createdAt": created_at,
            "read": False,
            "context": {context_key: target["id"], "messageId": message_id},
            "mention": {"scope": "user"},
        }
        try:
            await deliver_notification(session, recipient, notification)
        except Exception:
            # 提醒失败不影响发消息
            logger.exception("写 @ 提醒失败：%s", recipient)
