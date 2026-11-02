import asyncio
import datetime
import logging
import re
from typing import TYPE_CHECKING, List, Optional, Union
from weakref import WeakValueDictionary

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col

from core.author_repository import AuthorRepository
from core.notification_fanout_service import NotificationFanoutService
from core.tag_repository import TagRepository
from core.tag_source_write_required import TagSourceWriteRequired
from core.thread_repository import ThreadRepository
from dto.events.discord_tags_snapshot import DiscordTagsSnapshot
from dto.open_graph import ThreadSyncResult
from dto.thread_message_snapshot import ThreadMessageSnapshot
from models import Thread, TagBinding
from models.discord_tag_source import DiscordTagSource
from shared.discord_utils import DiscordUtils
from shared.image_url_utils import extract_message_image_urls
from shared.redis_client import RedisManager
from shared.similar_threads_cache import invalidate_similar_candidate_pools
from shared.time_utils import utc_now

if TYPE_CHECKING:
    from bot_main import MyBot

logger = logging.getLogger(__name__)


class SyncService:
    """将 Discord 频道和消息数据同步到数据库。"""

    def __init__(
        self,
        bot: "MyBot",
        session_factory: async_sessionmaker,
    ):
        self.bot = bot
        self.session_factory = session_factory
        self._thread_locks: WeakValueDictionary[int, asyncio.Lock] = (
            WeakValueDictionary()
        )

    async def pre_sync_forum_tags(self, channel: discord.ForumChannel | int) -> None:
        """按论坛对象或频道 ID 预同步全部可用标签。"""
        # 同时兼容现有论坛对象调用和审计中的频道 ID 调用。
        channel_id = channel if isinstance(channel, int) else channel.id
        observed_at = utc_now()
        fresh = await self.bot.api_scheduler.submit(
            coro_factory=lambda: self.bot.fetch_channel(channel_id), priority=10
        )
        if not isinstance(fresh, discord.ForumChannel):
            return
        await self.bot.event_mediator.publish(
            DiscordTagsSnapshot(
                fresh.id,
                {tag.id: tag.name for tag in fresh.available_tags},
                observed_at,
            )
        )

    async def _invalidate_similarity_cache(self, thread_id: int) -> None:
        """尽力失效源帖候选池，Redis 故障不影响同步主流程。"""
        try:
            await invalidate_similar_candidate_pools(
                RedisManager.get_client(), thread_id
            )
        except Exception:
            logger.warning(
                "失效相似帖子候选池失败: thread_id=%s", thread_id, exc_info=True
            )

    async def _save_author_to_db(
        self,
        author_id: int,
        guild: discord.Guild,
        source_member: discord.Member | discord.User | None = None,
    ) -> None:
        """获取作者信息并保存到数据库。"""
        # 获取用户对象
        user_obj = await DiscordUtils.get_or_fetch_user(
            bot=self.bot,
            user_id=author_id,
            guild=guild,
            source_member=source_member,
        )

        if not user_obj:
            return

        # 准备要插入或更新的数据
        author_data = {
            "id": user_obj.id,
            "name": user_obj.name,
            "global_name": user_obj.global_name,
            "display_name": user_obj.display_name,
            "avatar_url": user_obj.display_avatar.url,
        }

        # 存储数据
        try:
            async with self.session_factory() as session:
                repository = AuthorRepository(session)
                await repository.upsert_author(author_data)
        except Exception as e:
            logger.error(f"更新作者 {author_id} 信息到数据库时失败: {e}", exc_info=True)

    def _thread_lock(self, thread_id: int) -> asyncio.Lock:
        """复用同帖锁，并在没有持有者和等待者后自动释放锁登记。"""
        lock = self._thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[thread_id] = lock
        return lock

    @staticmethod
    def _channel_data(thread: discord.Thread) -> dict:
        """提取频道负责的字段，不携带作者或消息内容。"""
        created_at = discord.utils.snowflake_time(thread.id).replace(tzinfo=None)
        return {
            "thread_id": thread.id,
            "guild_id": thread.guild.id if thread.guild else 0,
            "channel_id": thread.parent_id,
            "title": thread.name,
            "last_active_at": (
                discord.utils.snowflake_time(thread.last_message_id).replace(
                    tzinfo=None
                )
                if thread.last_message_id
                else created_at
            ),
            "reply_count": thread.message_count,
        }

    async def _parse_message_data(
        self,
        thread_id: int,
        messages: list[discord.Message],
        default_author_id: int,
        default_created_at: datetime.datetime,
        priority: int,
    ) -> ThreadMessageSnapshot | None:
        """共用普通帖及重建帖的消息解析，不刷新作者资料。"""
        if not messages:
            return None

        first_msg = messages[0]
        second_msg = messages[1] if len(messages) > 1 else None

        # 默认值
        final_author_id = default_author_id
        final_created_at = default_created_at
        excerpt = ""
        thumbnail_urls: List[str] = []

        reaction_count = (
            max([r.count for r in first_msg.reactions]) if first_msg.reactions else 0
        )

        first_msg_content = first_msg.content or ""

        # 检查是否为重建帖。
        match_id = re.search(r"发帖人[:：\s*]*<@(\d+)>", first_msg_content)
        match_time = re.search(
            r"原始创建时间[:：\s*]*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日.*?(\d{2}):(\d{2})",
            first_msg_content,
        )

        if match_id:
            # 解析重建帖元数据。

            # 解析作者和时间。
            try:
                final_author_id = int(match_id.group(1))
                if match_time:
                    year, month, day, hour, minute = map(int, match_time.groups())
                    # 假设原始时间是 UTC+8 时区
                    local_tz = datetime.timezone(datetime.timedelta(hours=8))
                    local_dt = datetime.datetime(
                        year, month, day, hour, minute, tzinfo=local_tz
                    )
                    final_created_at = local_dt.astimezone(
                        datetime.timezone.utc
                    ).replace(tzinfo=None)
            except Exception as e:
                logger.warning(
                    f"重建帖 {thread_id} 解析作者或时间失败: {e}。中止对其的索引"
                )
                return None

            # 检查并解析补档链接: https://discord.com/channels/GUILD_ID/CHANNEL_ID/MESSAGE_ID
            match_url = re.search(
                r"补档[:：\s*]*\d+\.\s*\[.*?\]\((https://discord.com/channels/(\d+)/(\d+)/(\d+))\)",
                first_msg_content,
            )
            if not match_url:
                logger.debug(
                    f"重建帖 {thread_id} 的补档图片链接提取失败。中止对其的索引"
                )
                return None

            # 获取补档消息的附件。
            try:
                _, channel_id, message_id = map(int, match_url.groups()[1:])
                target_channel = self.bot.get_partial_messageable(channel_id)

                # 使用调度器提交API请求
                target_message = await self.bot.api_scheduler.submit(
                    coro_factory=lambda: target_channel.fetch_message(message_id),
                    priority=priority,
                )

                if not target_message:
                    logger.debug(
                        f"重建帖 {thread_id} 的补档消息 ({message_id}) 不存在。中止对其的索引"
                    )
                    return None

                attachment_urls = extract_message_image_urls(
                    attachments=target_message.attachments,
                    embeds=target_message.embeds,
                    content=target_message.content,
                )
                if not attachment_urls:
                    logger.debug(
                        f"重建帖 {thread_id} 的补档消息 ({message_id}) 没有图片，将保存空数组"
                    )
                else:
                    thumbnail_urls.extend(attachment_urls)

            except Exception as e:
                logger.error(
                    f"重建帖 {thread_id} 获取补档消息时出错: {e}。中止对其的索引",
                    exc_info=True,
                )
                return None

            # 从第二楼获取内容摘要。
            if second_msg:
                excerpt_ori = second_msg.content
                potential_reaction_numbers = []

                # 使用正则表达式查找所有“表情符号 + 空格 + 数字”的模式
                emoji_pattern = r"(?:<:\w+:\d+>|[\U00002600-\U000027BF\U0001F000-\U0001FFFF\U0001F900-\U0001F9FF])"
                matches = re.findall(
                    rf"{emoji_pattern}\s*(\d+)(?=\s|\||$)", second_msg.content
                )

                for num_str in matches:
                    try:
                        potential_reaction_numbers.append(int(num_str))
                    except ValueError:
                        logger.warning(
                            f"无法将重建帖 {thread_id} 中的反应数 '{num_str}' 转换为整数",
                            exc_info=True,
                        )

                if potential_reaction_numbers:
                    reaction_count_ori = max(potential_reaction_numbers)
                    reaction_count += reaction_count_ori

                # 从摘要中删除元数据行
                reaction_line_pattern = rf"^-#\s*{emoji_pattern}\s*\d+.*$"
                attachment_line_pattern = r"^-#\s*📎\s+.*?\s*\(.*?\)$"
                edited_line_pattern = r"^-#\s*\(已编辑\)$"

                cleaning_pattern = re.compile(
                    f"(?:{reaction_line_pattern}|{attachment_line_pattern}|{edited_line_pattern})\n?",
                    re.MULTILINE,
                )

                cleaned_excerpt = cleaning_pattern.sub("", excerpt_ori).strip()
                excerpt = cleaned_excerpt

        else:
            # 从普通帖首楼提取摘要和图片。
            excerpt = first_msg.content
            thumbnail_urls.extend(
                extract_message_image_urls(
                    attachments=first_msg.attachments,
                    embeds=first_msg.embeds,
                    content=first_msg.content,
                )
            )

        return ThreadMessageSnapshot(
            thread_id=thread_id,
            author_id=final_author_id,
            created_at=final_created_at,
            reaction_count=reaction_count,
            first_message_excerpt=excerpt or "",
            thumbnail_urls=thumbnail_urls,
        )

    async def _parse_thread_data(
        self, thread: discord.Thread, priority: int = 10
    ) -> Optional[dict]:
        """为完整同步合并频道数据和共用消息解析结果。"""

        # 调度器需要协程工厂，重试时在此重新创建消息迭代器。
        async def read_first_messages() -> list[discord.Message]:
            """为本次同步读取最早两条消息。"""
            return [msg async for msg in thread.history(limit=2, oldest_first=True)]

        messages = await self.bot.api_scheduler.submit(
            coro_factory=read_first_messages, priority=priority
        )
        snapshot = await self._parse_message_data(
            thread.id,
            messages,
            thread.owner_id or 0,
            discord.utils.snowflake_time(thread.id).replace(tzinfo=None),
            priority,
        )
        if snapshot is None:
            return None
        if snapshot.author_id and thread.guild:
            asyncio.create_task(
                self._save_author_to_db(snapshot.author_id, thread.guild, thread.owner)
            )
        return {
            **self._channel_data(thread),
            **snapshot.model_dump(),
            "not_found_count": 0,
        }

    async def sync_thread(
        self,
        thread: Union[discord.Thread, int],
        priority: int = 10,
        *,
        fetch_if_incomplete: bool = False,
    ) -> ThreadSyncResult:
        """串行执行同帖完整同步，保留现有调用接口和创建行为。"""
        thread_id = thread if isinstance(thread, int) else thread.id
        async with self._thread_lock(thread_id):
            return await self._sync_thread(
                thread, priority, fetch_if_incomplete=fetch_if_incomplete
            )

    async def _sync_thread(
        self,
        thread: Union[discord.Thread, int],
        priority: int = 10,
        *,
        fetch_if_incomplete: bool = False,
    ) -> ThreadSyncResult:
        """同步频道和消息数据，保留新帖创建、标签和通知行为。"""
        if isinstance(thread, int) or fetch_if_incomplete:
            thread_id = thread if isinstance(thread, int) else thread.id
            fetched, error = await self._fetch_thread_channel_for_sync(
                thread_id, priority
            )
            if error is not None:
                return error
            # 成功获取分支必须有帖子对象，显式收窄可空返回类型。
            assert fetched is not None
            thread = fetched

        assert isinstance(thread, discord.Thread)

        # 调用辅助方法解析帖子数据
        try:
            thread_data = await self._parse_thread_data(thread, priority)
        except Exception:
            logger.exception(f"帖子 {thread.id} 解析失败")
            return ThreadSyncResult(success=False, error_code="parse_failed")

        # 检查解析结果，如果为 None 则中止同步
        if thread_data is None:
            # logger.info(f"帖子 {thread.id} 不满足索引条件或无效，同步中止。")
            return ThreadSyncResult(success=False, error_code="parse_failed")

        try:
            tags_data = await self._prepare_thread_tags(thread)
            for allow_source_changes in (False, True):
                try:
                    async with self.session_factory() as session:
                        tags = await TagRepository(session).get_or_create_tags(
                            tags_data,
                            update_names=False,
                            channel_id=thread.parent_id,
                            allow_source_changes=allow_source_changes,
                        )
                        repo = ThreadRepository(session=session)
                        mutation = await repo.add_or_update_thread_with_tags(
                            thread_data=thread_data, tags=tags
                        )
                        if mutation.created and thread_data.get("author_id"):
                            # 来源、绑定与新帖通知仍在同一事务中提交。
                            await NotificationFanoutService(
                                session
                            ).fanout_author_new_thread(
                                author_id=thread_data["author_id"],
                                event_source_id=thread.id,
                                thread_id=thread.id,
                            )
                        await session.commit()
                    break
                except TagSourceWriteRequired:
                    # 上一会话已关闭并回滚，不能在共享锁事务内原地升级。
                    if allow_source_changes:
                        raise
        except Exception:
            logger.exception(f"帖子 {thread.id} 数据库提交失败")
            return ThreadSyncResult(success=False, error_code="database_failed")

        if mutation.tags_changed:
            await self._invalidate_similarity_cache(thread.id)

        await self._ensure_auto_follow(thread)

        return ThreadSyncResult(success=True)

    async def _prepare_thread_tags(self, thread: discord.Thread) -> dict[int, str]:
        """核对完整频道来源，保留标签移除和转本地绑定的语义。"""
        tags_data = {t.id: t.name for t in thread.applied_tags or []}
        async with self.session_factory() as session:
            existing = set(
                (
                    await session.execute(
                        select(col(DiscordTagSource.discord_tag_id))
                        .join(
                            TagBinding,
                            col(TagBinding.discord_source_id)
                            == col(DiscordTagSource.id),
                        )
                        .join(Thread, col(Thread.id) == col(TagBinding.target_id))
                        .where(
                            col(Thread.thread_id) == thread.id,
                            col(TagBinding.target_type) == "thread",
                            col(TagBinding.binding_source) == "discord_sync",
                            col(TagBinding.ended_at).is_(None),
                        )
                    )
                ).scalars()
            )
            known = list(
                (
                    await session.execute(
                        select(DiscordTagSource).where(
                            col(DiscordTagSource.discord_tag_id).in_(tags_data)
                        )
                    )
                ).scalars()
            )
            needs_refresh = set(tags_data) - {s.discord_tag_id for s in known} or any(
                s.channel_id is None
                or s.name != tags_data[s.discord_tag_id]
                or s.deleted_at is not None
                for s in known
            )
        if existing - set(tags_data) or needs_refresh:
            await self.pre_sync_forum_tags(thread.parent_id)
        # 返回标量数据，标签创建与绑定更新必须位于同一提交事务。
        return tags_data

    async def _ensure_auto_follow(self, thread: discord.Thread) -> None:
        """尽力为尚无关注记录的既有帖子补充自动关注。"""
        try:
            # 检查是否是首次被关注（检查关注表而不是帖子表）
            is_first_follow = False
            async with self.session_factory() as session:
                from sqlmodel import func

                from models import ThreadFollow

                statement = (
                    select(func.count())
                    .select_from(ThreadFollow)
                    .where(col(ThreadFollow.thread_id) == thread.id)
                )
                result = await session.execute(statement)
                follow_count = result.scalar() or 0
                is_first_follow = follow_count == 0

            # 如果是首次被关注的帖子，批量添加所有成员到关注列表
            if is_first_follow:
                await self._auto_follow_on_first_detect(thread)
        except Exception:
            logger.exception(f"帖子 {thread.id} 提交后的自动关注检查失败")

    async def _fetch_thread_channel_for_sync(
        self, thread_id: int, priority: int
    ) -> tuple[discord.Thread | None, ThreadSyncResult | None]:
        """获取同步所需的帖子频道，确认不存在时记录失败次数。"""
        try:
            channel = await self.bot.api_scheduler.submit(
                coro_factory=lambda: self.bot.fetch_channel(thread_id),
                priority=priority,
            )
            if isinstance(channel, discord.Thread):
                return channel, None
            error_code = "discord_invalid_channel"
        except discord.NotFound:
            error_code = "discord_not_found"
        except Exception:
            logger.exception("获取帖子频道失败: thread_id=%s", thread_id)
            return None, ThreadSyncResult(
                success=False, error_code="discord_fetch_failed"
            )

        # 消息和补档失败不会进入本方法的不存在计数路径。
        try:
            async with self.session_factory() as session:
                changed = await ThreadRepository(session).increment_not_found_count(
                    thread_id
                )
        except Exception:
            logger.exception("帖子不存在计数提交失败: thread_id=%s", thread_id)
            return None, ThreadSyncResult(success=False, error_code="database_failed")
        if changed:
            await self._invalidate_similarity_cache(thread_id)
        return None, ThreadSyncResult(success=False, error_code=error_code)

    async def sync_thread_channel(
        self, thread_id: int, priority: int = 10
    ) -> ThreadSyncResult:
        """只审计既有帖子的频道数据，保留慢速作者和自动关注检查。"""
        async with self._thread_lock(thread_id):
            thread, error = await self._fetch_thread_channel_for_sync(
                thread_id, priority
            )
            if error is not None:
                return error
            assert thread is not None
            try:
                tags_data = await self._prepare_thread_tags(thread)
                for allow_source_changes in (False, True):
                    try:
                        async with self.session_factory() as session:
                            tags = await TagRepository(session).get_or_create_tags(
                                tags_data,
                                update_names=False,
                                channel_id=thread.parent_id,
                                allow_source_changes=allow_source_changes,
                            )
                            mutation = await ThreadRepository(
                                session
                            ).update_thread_channel_with_tags(
                                self._channel_data(thread), tags
                            )
                            if mutation is None:
                                return ThreadSyncResult(
                                    success=False, error_code="thread_missing"
                                )
                            await session.commit()
                        break
                    except TagSourceWriteRequired:
                        # 释放共享锁后新建会话，独占重读来源和权威检查点。
                        if allow_source_changes:
                            raise
            except Exception:
                logger.exception("频道审计提交失败: thread_id=%s", thread_id)
                return ThreadSyncResult(success=False, error_code="database_failed")
            if mutation.tags_changed or mutation.visibility_changed:
                await self._invalidate_similarity_cache(thread_id)

            # 使用数据库中的解析作者，不能将重建帖作者换成发帖机器人。
            try:
                if mutation.author_id and thread.guild:
                    await self._save_author_to_db(
                        mutation.author_id, thread.guild, thread.owner
                    )
            except Exception:
                logger.exception("频道审计作者资料更新失败: thread_id=%s", thread_id)
            await self._ensure_auto_follow(thread)
            return ThreadSyncResult(success=True)

    async def sync_thread_messages(
        self, thread_id: int, priority: int = 10
    ) -> ThreadSyncResult:
        """按帖子 ID 读取首楼，快速同步消息字段且不改变删除状态。"""
        async with self._thread_lock(thread_id):
            try:
                channel = self.bot.get_partial_messageable(thread_id)

                # 请求工厂就地定义，重试时不复用已读取过的迭代器。
                async def read_first_messages() -> list[discord.Message]:
                    """为本次消息审计读取最早两条消息。"""
                    return [
                        msg async for msg in channel.history(limit=2, oldest_first=True)
                    ]

                messages = await self.bot.api_scheduler.submit(
                    coro_factory=read_first_messages, priority=priority
                )
                if not messages or messages[0].id != thread_id:
                    return ThreadSyncResult(
                        success=False, error_code="starter_message_missing"
                    )
                snapshot = await self._parse_message_data(
                    thread_id,
                    messages,
                    messages[0].author.id,
                    discord.utils.snowflake_time(thread_id).replace(tzinfo=None),
                    priority,
                )
                if snapshot is None:
                    return ThreadSyncResult(success=False, error_code="parse_failed")
            except Exception:
                logger.exception("消息审计读取或解析失败: thread_id=%s", thread_id)
                return ThreadSyncResult(
                    success=False, error_code="message_fetch_failed"
                )
            try:
                async with self.session_factory() as session:
                    updated = await ThreadRepository(session).update_thread_messages(
                        snapshot
                    )
                    if not updated:
                        return ThreadSyncResult(
                            success=False, error_code="thread_missing"
                        )
                    await session.commit()
            except Exception:
                logger.exception("消息审计提交失败: thread_id=%s", thread_id)
                return ThreadSyncResult(success=False, error_code="database_failed")
            return ThreadSyncResult(success=True)

    async def _auto_follow_on_first_detect(self, thread: discord.Thread):
        """首次检测到老帖子时，自动为所有成员添加关注"""
        try:
            # 获取帖子中的所有成员ID
            member_ids = []

            # fetch_members()返回AsyncIterator，直接迭代
            members_iterator = await self.bot.api_scheduler.submit(
                coro_factory=lambda: thread.fetch_members(), priority=10
            )
            for member in members_iterator:
                # 不用检测机器人，fetch member太慢了
                member_ids.append(member.id)

            if member_ids:
                from core.follow_repository import ThreadFollowRepository

                async with self.session_factory() as session:
                    follow_service = ThreadFollowRepository(session)
                    added_count = await follow_service.batch_add_follows(
                        thread_id=thread.id, user_ids=member_ids
                    )
                    if added_count > 0:
                        logger.info(
                            f"帖子 {thread.id} 首次检测，为 {added_count} 个成员添加了自动关注"
                        )
        except discord.NotFound:
            logger.warning(f"帖子 {thread.id} 已被删除，跳过自动关注")
        except discord.Forbidden:
            logger.warning(f"帖子 {thread.id} 没有权限获取成员列表，跳过自动关注")
        except Exception as e:
            logger.error(f"帖子自动关注失败 (帖子 {thread.id}): {e}", exc_info=True)
