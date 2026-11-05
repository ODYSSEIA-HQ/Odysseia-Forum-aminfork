"""作品联想与相似推荐，复用搜索仓储的权限和反选语义。"""

from fastapi import HTTPException
from sqlalchemy import String, cast, func, or_, select, union
from api.v2.schemas.work_search_request import WorkSearchRequest
from core.preferences_repository import PreferencesRepository
from identity.canonical_user_id import CanonicalUserId
from models.booklist_item import BooklistItem
from models.booklist import Booklist
from models.tag import Tag
from models.tag_alias import TagAlias
from models.tag_binding import TagBinding
from models.user import User
from models.work import Work
from models.work_native_tag_binding import WorkNativeTagBinding
from models.work_native_tag_source import WorkNativeTagSource
from work.work_preferences_service import search_preference_guild
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkDiscoveryService:
    """不缓存个体权限；每次请求读取当前偏好与可见作品。"""

    def __init__(self, sessions):
        """保存请求内使用的会话工厂与服务配置。"""
        self.sessions = sessions

    async def exclusions(self, session, principal, apply_preferences):
        """推荐只继承反选项，不将上次正选搜索范围带入推荐。"""
        query = WorkSearchRequest(sort_method="reddit_hot")
        if not apply_preferences:
            return query
        try:
            prefs = await PreferencesRepository(session).get_user_preferences(
                CanonicalUserId(str(principal["id"])),
                search_preference_guild(principal),
            )
        except ValueError as exc:
            raise HTTPException(503, "旧作者偏好身份映射尚未就绪") from exc
        if prefs:
            for field in (
                "exclude_authors",
                "exclude_tags",
                "exclude_keywords",
                "exclude_keyword_exemption_markers",
            ):
                value = getattr(prefs, field)
                if value is not None:
                    setattr(query, field, value)
        return query

    @staticmethod
    def bindings():
        """统一当前本地和原生标签，并去除同作品重复来源。"""
        return union(
            select(
                TagBinding.work_id.label("work_id"), TagBinding.tag_id.label("tag_id")
            ).where(TagBinding.work_id.is_not(None), TagBinding.ended_at.is_(None)),
            select(
                TagBinding.target_id.label("work_id"), TagBinding.tag_id.label("tag_id")
            ).where(TagBinding.target_type == "work", TagBinding.ended_at.is_(None)),
            select(WorkNativeTagBinding.work_id, WorkNativeTagSource.tag_id).join(
                WorkNativeTagSource,
                WorkNativeTagSource.id == WorkNativeTagBinding.source_id,
            ),
        ).subquery()

    async def similar(self, work_id, limit, apply_preferences, command):
        """按可见来源的共有标签逐步放宽候选并补齐相似作品。"""
        # 先检查来源作品的可见性，再计算当前标签范围。
        principal = command.actor
        async with self.sessions() as session:
            repository = WorkRepository(session)
            # 与列表一致，不允许通过推荐探测暂不可见来源或隐藏分类。
            sources, _ = await repository.page(principal, 0, 1, work_ids=[work_id])
            if not sources:
                raise HTTPException(404, "作品不存在")
            bindings = self.bindings()
            visibility, _, _ = repository.visibility_filters(principal)
            source_tags = select(bindings.c.tag_id).where(bindings.c.work_id == work_id)
            statement = (
                select(
                    Tag.id, Tag.name, func.count(func.distinct(Work.id)).label("uses")
                )
                .join(bindings, bindings.c.tag_id == Tag.id)
                .join(Work, Work.id == bindings.c.work_id)
                .where(
                    Tag.id.in_(source_tags),
                    Tag.deleted_at.is_(None),
                    *visibility,
                )
                .group_by(Tag.id, Tag.name)
            )
            if not principal["can_view_abyss"]:
                statement = statement.where(Tag.is_abyss.is_(False))
            # 与 V1 一致合并同名概念，按流行度稳定降级。
            counts = {}
            for _, name, count in (await session.execute(statement)).all():
                counts[name] = counts.get(name, 0) + count
            names = sorted(counts, key=lambda name: (-counts[name], name))
            query = await self.exclusions(session, principal, apply_preferences)
            rows, matched = [], 0
            query.exclude_work_ids = [str(work_id)]
            for size in range(len(names), 0, -1):
                query.include_tags = names[:size]
                found, _ = await repository.page(
                    principal, 0, limit - len(rows), advanced=query
                )
                if found:
                    matched = size
                    rows.extend(found)
                    query.exclude_work_ids.extend(str(row.id) for row in found)
                if len(rows) >= limit:
                    break
            return {
                "items": await WorkService(session).project(rows),
                "matched_tag_count": matched,
            }

    async def suggestions(self, keyword, limit, apply_preferences, command):
        """在当前可见范围内批量查询作品、作者、标签与书单联想。"""
        # 空关键词直接返回，非空内容先转义再构造匹配条件。
        principal = command.actor
        empty = {"works": [], "authors": [], "tags": [], "booklists": []}
        keyword = keyword.strip()
        if not keyword:
            return empty
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        async with self.sessions() as session:
            query = await self.exclusions(session, principal, apply_preferences)
            query.sort_method = "last_active"
            match = Work.title.ilike(pattern, escape="\\")
            if keyword.isdecimal():
                match = or_(match, cast(Work.id, String).like(pattern, escape="\\"))
            works, _ = await WorkRepository(session).page(
                principal, 0, limit, advanced=query, extra_filters=[match]
            )
            authors = select(User).where(
                User.merged_into_id.is_(None),
                or_(
                    User.username.ilike(pattern, escape="\\"),
                    User.display_name.ilike(pattern, escape="\\"),
                    User.id.like(pattern, escape="\\")
                    if keyword.isdecimal()
                    else False,
                ),
            )
            if query.exclude_authors:
                authors = authors.where(
                    User.id.not_in([str(value) for value in query.exclude_authors])
                )
            tags = select(Tag).where(
                Tag.deleted_at.is_(None),
                Tag.enabled.is_(True),
                or_(
                    Tag.name.ilike(pattern, escape="\\"),
                    Tag.id.in_(
                        select(TagAlias.tag_id).where(
                            TagAlias.name.ilike(pattern, escape="\\")
                        )
                    ),
                ),
            )
            if not principal["can_view_abyss"]:
                tags = tags.where(Tag.is_abyss.is_(False))
            booklists = select(Booklist).where(
                Booklist.merged_into_id.is_(None),
                or_(
                    Booklist.is_public.is_(True),
                    Booklist.fluxer_owner_id == principal["id"],
                ),
                or_(
                    Booklist.title.ilike(pattern, escape="\\"),
                    Booklist.description.ilike(pattern, escape="\\"),
                    cast(Booklist.id, String).like(pattern, escape="\\")
                    if keyword.isdecimal()
                    else False,
                ),
            )
            author_rows = (
                (
                    await session.execute(
                        authors.order_by(User.username, User.id).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            tag_rows = (
                (
                    await session.execute(
                        tags.order_by(Tag.name, Tag.category, Tag.id).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            booklist_rows = (
                (
                    await session.execute(
                        booklists.order_by(
                            Booklist.view_count.desc(), Booklist.id
                        ).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            visibility, _, _ = WorkRepository.visibility_filters(principal)
            visible_counts = (
                dict(
                    (
                        await session.execute(
                            select(
                                BooklistItem.booklist_id,
                                func.count(func.distinct(Work.id)),
                            )
                            .join(Work, Work.id == BooklistItem.work_id)
                            .where(
                                BooklistItem.booklist_id.in_(
                                    [row.id for row in booklist_rows]
                                ),
                                *visibility,
                            )
                            .group_by(BooklistItem.booklist_id)
                        )
                    ).all()
                )
                if booklist_rows
                else {}
            )
            return {
                "works": await WorkService(session).project(works),
                "authors": [
                    {
                        "id": str(row.id),
                        "name": row.username,
                        "display_name": row.display_name or row.username,
                        "avatar_url": row.avatar_url,
                    }
                    for row in author_rows
                ],
                "tags": [
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "category": row.category,
                        "is_abyss": row.is_abyss,
                    }
                    for row in tag_rows
                ],
                "booklists": [
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "item_count": visible_counts.get(row.id, 0),
                    }
                    for row in booklist_rows
                ],
            }
