"""作品书单领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from sqlalchemy import select, func, or_, delete, text
from urllib.parse import urlsplit
from models.work import Work
from models.user import User
from models.booklist_publish import BooklistPublish
from core.tag_query import load_custom_tags, require_current_tag_ids, tag_filters
from models.user_collection import UserCollection
from shared.time_utils import utc_now
from models.booklist import Booklist
from models.booklist_item import BooklistItem
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkBooklistService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def booklists(self, command: WorkCommandQo):
        """返回当前用户的普通及默认书单。"""
        principal = command.actor
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(Booklist)
                        .where(
                            Booklist.fluxer_owner_id == principal["id"],
                            Booklist.merged_into_id.is_(None),
                            Booklist.is_tournament.is_(False),
                        )
                        .order_by(Booklist.id)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "description": row.description,
                        "is_public": row.is_public,
                        "is_default": row.is_default,
                        "item_count": row.item_count,
                    }
                    for row in rows
                ]
            }

    async def create_booklist(self, command: WorkCommandQo):
        """创建作品书单，所有者来自认证主体。"""
        principal = command.actor
        body = dict(command.values)
        body = self.validate_fields(body, creating=True)
        async with self.sessions() as session:
            row = Booklist(
                **body,
                owner_id=int(principal["discord_user_id"]),
                fluxer_owner_id=principal["id"],
            )
            session.add(row)
            await session.flush()
            result = {
                "id": str(row.id),
                "title": row.title,
                "description": row.description,
                "is_public": row.is_public,
                "is_default": False,
                "item_count": 0,
            }
            await session.commit()
            return result

    async def booklist_detail(
        self, booklist_id: int, offset: int, limit: int, command: WorkCommandQo
    ):
        """公开书单仍按访问者偏好过滤作品。"""
        principal = command.actor
        async with self.sessions() as session:
            from core.booklist_repository import BooklistRepository

            row = await BooklistRepository(session).get_booklist(booklist_id)
            if row is None or (
                not row.is_public and row.fluxer_owner_id != principal["id"]
            ):
                raise HTTPException(404, "书单不存在")
            ids = select(BooklistItem.work_id).where(BooklistItem.booklist_id == row.id)
            rows, total = await WorkRepository(session).page(
                principal, offset, limit, work_ids=ids
            )
            return {
                "id": str(row.id),
                "title": row.title,
                "description": row.description,
                "is_public": row.is_public,
                "is_default": row.is_default,
                "items": await WorkService(session).project(rows),
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def edit_booklist(self, booklist_id: int, command: WorkCommandQo):
        """更新书单元数据，禁止重定向所有权。"""
        principal = command.actor
        body = dict(command.values)
        body = self.validate_fields(body)
        async with self.sessions() as session:
            row = await self.access(session, booklist_id, principal, manage=True)
            for key, value in body.items():
                setattr(row, key, value)
            await session.flush()
            result = {
                "id": str(row.id),
                "title": row.title,
                "description": row.description,
                "is_public": row.is_public,
                "is_default": row.is_default,
                "item_count": row.item_count,
            }
            await session.commit()
            return result

    async def booklist_item(
        self, booklist_id: int, work_id: int, command: WorkCommandQo
    ):
        """书单条目以作品为单位去重。"""
        principal = command.actor
        body = self.validate_item(dict(command.values))
        async with self.sessions() as session:
            # 与作品收藏保持相同锁顺序，默认收藏书单也遵循此约束。
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "default-booklist:" + principal["id"]},
            )
            row = await self.access(session, booklist_id, principal, manage=True)
            service = WorkService(session)
            if command.operation == "remove":
                # 只检查当前用户的条目归属，不泄漏任意隐藏作品编号。
                existing = (
                    await session.execute(
                        select(BooklistItem).where(
                            BooklistItem.booklist_id == row.id,
                            BooklistItem.work_id == work_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    await session.delete(existing)
                    row.item_count = max(0, row.item_count - 1)
                    row.updated_at = utc_now()
                await session.commit()
                return {"collected": False}
            work = await service.visible(work_id, principal)
            item = (
                await session.execute(
                    select(BooklistItem).where(
                        BooklistItem.booklist_id == row.id,
                        BooklistItem.work_id == work.id,
                    )
                )
            ).scalar_one_or_none()
            existing_item = item is not None
            if command.operation == "edit" and item is None:
                raise HTTPException(404, "书单条目不存在")
            if command.operation != "edit":
                await service.collect(
                    work, principal, command.operation == "add", row.id
                )
            if command.operation != "remove":
                item = (
                    await session.execute(
                        select(BooklistItem).where(
                            BooklistItem.booklist_id == row.id,
                            BooklistItem.work_id == work.id,
                        )
                    )
                ).scalar_one()
                if (
                    command.operation == "add"
                    and not existing_item
                    and "display_order" not in body
                ):
                    maximum = (
                        await session.execute(
                            select(func.max(BooklistItem.display_order)).where(
                                BooklistItem.booklist_id == row.id
                            )
                        )
                    ).scalar_one() or 0
                    item.display_order = maximum + 1
                for key, value in body.items():
                    setattr(item, key, value)
            row.updated_at = utc_now()
            await session.commit()
            return {"collected": command.operation != "remove"}

    @staticmethod
    def validate_fields(body, creating=False):
        """仅校验普通编辑字段，不允许改写身份与特殊书单标志。"""
        if set(body) - {
            "title",
            "description",
            "is_public",
            "cover_image_url",
            "display_type",
        }:
            raise HTTPException(422, "无效书单字段")
        if creating or "title" in body:
            title = body.get("title")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
                raise HTTPException(422, "标题长度须为1至80字")
            body["title"] = title.strip()
        if "description" in body and body["description"] is not None:
            if (
                not isinstance(body["description"], str)
                or len(body["description"]) > 600
            ):
                raise HTTPException(422, "简介最长600字")
        if "is_public" in body and type(body["is_public"]) is not bool:
            raise HTTPException(422, "无效公开状态")
        if body.get("cover_image_url") is not None:
            url = body["cover_image_url"]
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid = (
                    parsed
                    and parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                )
            except ValueError:
                valid = False
            if not valid:
                raise HTTPException(422, "封面须为HTTP或HTTPS网址")
        if "display_type" in body:
            display_type = body.pop("display_type")
            if type(display_type) is not int or display_type not in (1, 2):
                raise HTTPException(422, "无效展示方式")
            body["default_sort_method"] = (
                "join_time" if display_type == 1 else "display_order"
            )
            body["default_sort_order"] = "desc" if display_type == 1 else "asc"
        return body

    @staticmethod
    def validate_item(body):
        """校验条目推荐语和排序值，拒绝其他字段。"""
        # 分别限制推荐语类型与有符号排序值范围。
        if set(body) - {"comment", "display_order"}:
            raise HTTPException(422, "无效条目字段")
        if "comment" in body and body["comment"] is not None:
            if not isinstance(body["comment"], str) or len(body["comment"]) > 800:
                raise HTTPException(422, "推荐语最长800字")
        if "display_order" in body and (
            type(body["display_order"]) is not int
            or not -2147483648 <= body["display_order"] <= 2147483647
        ):
            raise HTTPException(422, "无效排序值")
        return body

    @staticmethod
    async def access(session, booklist_id, principal, manage=False):
        """读取书单并按管理需要加锁，统一校验所有权和公开范围。"""
        # 管理操作重新读取并锁定书单，防止权限检查与写入之间发生变化。
        from core.booklist_repository import BooklistRepository

        row = await BooklistRepository(session).get_booklist(booklist_id)
        if row is not None and manage:
            row = (
                await session.execute(
                    select(Booklist)
                    .where(Booklist.id == row.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if (
            row is None
            or row.is_tournament
            or (
                row.fluxer_owner_id != principal["id"] and (manage or not row.is_public)
            )
        ):
            raise HTTPException(404, "书单不存在")
        return row

    @staticmethod
    async def presentation_rows(session, rows, principal):
        """批量读取可见作品统计、收藏与作者信息，避免逐书单查询。"""
        # 按全部书单编号集合查询，投影阶段不再逐项访问数据库。
        filters, _, _ = WorkRepository.visibility_filters(principal)
        counts = (
            dict(
                (
                    await session.execute(
                        select(BooklistItem.booklist_id, func.count())
                        .join(Work, Work.id == BooklistItem.work_id)
                        .where(
                            BooklistItem.booklist_id.in_([row.id for row in rows]),
                            *filters,
                        )
                        .group_by(BooklistItem.booklist_id)
                    )
                ).all()
            )
            if rows
            else {}
        )
        collected = (
            set(
                (
                    await session.execute(
                        select(UserCollection.target_id).where(
                            UserCollection.fluxer_user_id == principal["id"],
                            UserCollection.target_type == 2,
                            UserCollection.target_id.in_([row.id for row in rows]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if rows
            else set()
        )
        owner_ids = {
            row.fluxer_owner_id
            for row in rows
            if not row.is_anonymous or row.fluxer_owner_id == principal["id"]
        }
        authors = (
            {
                user.id: {
                    "id": user.id,
                    "name": user.username,
                    "display_name": user.display_name,
                    "global_name": None,
                    "avatar_url": user.avatar_url,
                }
                for user in await session.execute(
                    select(
                        User.id, User.username, User.display_name, User.avatar_url
                    ).where(User.id.in_(owner_ids), User.merged_into_id.is_(None))
                )
            }
            if owner_ids
            else {}
        )
        tags = (
            await load_custom_tags(session, "booklist", [row.id for row in rows])
            if rows
            else {}
        )
        publications = (
            {
                publication.booklist_id: publication
                for publication in (
                    await session.execute(
                        select(BooklistPublish).where(
                            BooklistPublish.booklist_id.in_([row.id for row in rows])
                        )
                    )
                )
                .scalars()
                .all()
            }
            if rows
            else {}
        )
        result = []
        for row in rows:
            mine = row.fluxer_owner_id == principal["id"]
            value = {
                key: getattr(row, key)
                for key in (
                    "id",
                    "title",
                    "description",
                    "cover_image_url",
                    "is_public",
                    "is_anonymous",
                    "is_default",
                    "is_tournament",
                    "default_sort_method",
                    "default_sort_order",
                    "view_count",
                    "collection_count",
                    "publish_status",
                    "created_at",
                    "updated_at",
                )
            }
            value.update(
                owner_id=row.fluxer_owner_id if mine or not row.is_anonymous else None,
                author=authors.get(row.fluxer_owner_id)
                if mine or not row.is_anonymous
                else None,
                custom_tags=tags.get(row.id, []),
                publish_info=None,
                can_manage=mine,
                collected_flag=row.id in collected,
                item_count=counts.get(row.id, 0),
            )
            publication = publications.get(row.id)
            if publication:
                value["publish_info"] = {
                    "guild_id": str(publication.guild_id),
                    "thread_id": str(publication.thread_id),
                    "thread_url": f"https://discord.com/channels/{publication.guild_id}/{publication.thread_id}",
                    "message_id": str(publication.message_id)
                    if publication.message_id is not None
                    else None,
                    "message_url": publication.message_url,
                    "published_at": publication.created_at,
                }
            result.append(value)
        return result

    async def page(
        self,
        scope,
        owner_id,
        keywords,
        sort_method,
        sort_order,
        offset,
        limit,
        command,
        mark_work_id=None,
        include_tag_ids=None,
        exclude_tag_ids=None,
        tag_logic="and",
    ):
        """在权限过滤后按指定顺序查询并投影分页结果。"""
        principal = command.actor
        async with self.sessions() as session:
            filters = [
                Booklist.merged_into_id.is_(None),
                Booklist.is_tournament.is_(False),
            ]
            filters.append(
                Booklist.fluxer_owner_id == principal["id"]
                if scope == "mine"
                else Booklist.is_public.is_(True)
            )
            if scope == "collected":
                filters[-1] = or_(
                    Booklist.is_public.is_(True),
                    Booklist.fluxer_owner_id == principal["id"],
                )
                filters.append(
                    Booklist.id.in_(
                        select(UserCollection.target_id).where(
                            UserCollection.fluxer_user_id == principal["id"],
                            UserCollection.target_type == 2,
                        )
                    )
                )
            if owner_id is not None:
                filters.extend(
                    [
                        Booklist.fluxer_owner_id == owner_id,
                        or_(
                            Booklist.is_anonymous.is_(False),
                            Booklist.fluxer_owner_id == principal["id"],
                        ),
                    ]
                )
            if keywords:
                from shared.fts_utils import build_fts_conditions

                fts = await build_fts_conditions(Booklist.search_vector, keywords, None)
                filters.extend(fts.include_conditions)
            await require_current_tag_ids(
                session, [*(include_tag_ids or []), *(exclude_tag_ids or [])]
            )
            filters.extend(
                tag_filters(
                    "booklist", Booklist.id, include_tag_ids, exclude_tag_ids, tag_logic
                )
            )
            query = select(Booklist).where(*filters)
            total = (
                await session.execute(
                    select(func.count()).select_from(query.subquery())
                )
            ).scalar_one()
            sort = {
                1: Booklist.item_count,
                2: Booklist.view_count,
                3: Booklist.collection_count,
                4: Booklist.created_at,
                5: Booklist.updated_at,
                6: (
                    select(func.max(UserCollection.created_at))
                    .where(
                        UserCollection.target_id == Booklist.id,
                        UserCollection.target_type == 2,
                        UserCollection.fluxer_user_id == principal["id"],
                    )
                    .correlate(Booklist)
                    .scalar_subquery()
                    if scope == "collected"
                    else Booklist.created_at
                ),
            }[sort_method]
            rows = (
                (
                    await session.execute(
                        query.order_by(
                            sort.asc() if sort_order == "asc" else sort.desc(),
                            Booklist.id.desc(),
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            results = await self.presentation_rows(session, rows, principal)
            marked = set()
            if mark_work_id is not None:
                visible, _, _ = WorkRepository.visibility_filters(principal)
                marked = set(
                    (
                        await session.execute(
                            select(BooklistItem.booklist_id)
                            .join(Work, Work.id == BooklistItem.work_id)
                            .where(
                                BooklistItem.booklist_id.in_([row.id for row in rows]),
                                Work.id == mark_work_id,
                                *visible,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            for value in results:
                value["is_marked"] = value["id"] in marked
            return {
                "results": results,
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def presentation(self, booklist_id, command):
        """在访问校验后生成单个书单的公开展示资料。"""
        async with self.sessions() as session:
            row = await self.access(session, booklist_id, command.actor)
            return (await self.presentation_rows(session, [row], command.actor))[0]

    async def items(self, booklist_id, offset, limit, command):
        """读取可见作品分页并批量合并书单条目元数据。"""
        async with self.sessions() as session:
            row = await self.access(session, booklist_id, command.actor)
            filters, _, abyss_channels = WorkRepository.visibility_filters(
                command.actor
            )
            base = (
                select(BooklistItem, Work)
                .join(Work, Work.id == BooklistItem.work_id)
                .where(BooklistItem.booklist_id == row.id, *filters)
            )
            total = (
                await session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
            sort = (
                BooklistItem.display_order
                if row.default_sort_method == "display_order"
                else BooklistItem.created_at
            )
            pairs = (
                await session.execute(
                    base.order_by(
                        sort.asc() if row.default_sort_order == "asc" else sort.desc(),
                        BooklistItem.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            works = [work for _, work in pairs]
            if command.actor.get("config", {}).get("abyss") is not None:
                for work in works:
                    if work.source_platform == "discord":
                        work.is_abyss = work.source_channel_id in abyss_channels
            projected = await WorkService(session).project(works)
            return {
                "results": [
                    {
                        "booklist_item_id": item.id,
                        "work": work,
                        "comment": item.comment,
                        "display_order": item.display_order,
                        "added_at": item.created_at,
                    }
                    for (item, _), work in zip(pairs, projected)
                ],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def delete_booklist(self, booklist_id, command):
        """校验管理权限后删除普通书单及其关联。"""
        async with self.sessions() as session:
            row = await self.access(session, booklist_id, command.actor, manage=True)
            if row.is_default:
                raise HTTPException(409, "不能删除默认书单")
            from core.booklist_repository import BooklistRepository

            await session.execute(
                delete(UserCollection).where(
                    UserCollection.target_type == 2, UserCollection.target_id == row.id
                )
            )
            await BooklistRepository(session).delete_booklist(row.id)
            return {"deleted": True}

    @staticmethod
    def positive_id(value):
        """只接受有符号长整数范围内的正整数或规范数字字符串。"""
        if type(value) is int and 0 < value <= 9223372036854775807:
            return value
        if (
            isinstance(value, str)
            and value.isascii()
            and value.isdecimal()
            and 0 < int(value) <= 9223372036854775807
        ):
            return int(value)
        raise HTTPException(422, "无效编号")

    @staticmethod
    async def collection_lock(session, principal):
        """按用户序列化收藏变更，避免跨书单操作破坏一致性。"""
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "default-booklist:" + principal["id"]},
        )

    async def add_items(self, booklist_id, command):
        """校验并去重作品输入，在同一事务内更新书单条目。"""
        # 先验证全部输入，再进入事务执行去重和写入。
        body = dict(command.values)
        items = body.get("items")
        if (
            set(body) != {"items"}
            or not isinstance(items, list)
            or not 1 <= len(items) <= 100
        ):
            raise HTTPException(422, "每次须添加1至100个作品")
        validated = {}
        for item in items:
            if not isinstance(item, dict) or "work_id" not in item:
                raise HTTPException(422, "无效作品条目")
            values = dict(item)
            work_id = self.positive_id(values.pop("work_id"))
            if work_id in validated:
                raise HTTPException(422, "作品编号重复")
            validated[work_id] = self.validate_item(values)
        async with self.sessions() as session:
            await self.collection_lock(session, command.actor)
            row = await self.access(session, booklist_id, command.actor, manage=True)
            service = WorkService(session)
            works = [
                await service.visible(work_id, command.actor) for work_id in validated
            ]
            added = []
            for work in works:
                existed = (
                    await session.execute(
                        select(BooklistItem.id).where(
                            BooklistItem.booklist_id == row.id,
                            BooklistItem.work_id == work.id,
                        )
                    )
                ).scalar_one_or_none()
                await service.collect(work, command.actor, True, row.id)
                item = (
                    await session.execute(
                        select(BooklistItem).where(
                            BooklistItem.booklist_id == row.id,
                            BooklistItem.work_id == work.id,
                        )
                    )
                ).scalar_one()
                if existed is None:
                    added.append(str(work.id))
                    if "display_order" not in validated[work.id]:
                        maximum = (
                            await session.execute(
                                select(func.max(BooklistItem.display_order)).where(
                                    BooklistItem.booklist_id == row.id
                                )
                            )
                        ).scalar_one() or 0
                        item.display_order = maximum + 1
                if existed is None:
                    for key, value in validated[work.id].items():
                        setattr(item, key, value)
            row.updated_at = utc_now()
            await session.commit()
            return {"added_work_ids": added, "added_count": len(added)}

    async def sync_item(self, command):
        """规范化作品别名后按固定顺序锁定书单并同步条目。"""
        # 先校验目标范围包含关系，再按统一编号顺序获取锁。
        body = dict(command.values)
        if set(body) - {
            "work_id",
            "scope_booklist_ids",
            "target_booklist_ids",
            "comment",
        }:
            raise HTTPException(422, "无效同步字段")
        work_id = self.positive_id(body.get("work_id"))
        lists = []
        for key in ("scope_booklist_ids", "target_booklist_ids"):
            values = body.get(key)
            if not isinstance(values, list) or len(values) > 100:
                raise HTTPException(422, "无效书单范围")
            lists.append({self.positive_id(value) for value in values})
        scope, targets = lists
        if not scope:
            raise HTTPException(422, "同步范围不能为空")
        if not targets.issubset(scope):
            raise HTTPException(422, "目标书单必须属于同步范围")
        metadata = self.validate_item(
            {"comment": body["comment"]} if "comment" in body else {}
        )
        async with self.sessions() as session:
            await self.collection_lock(session, command.actor)
            # 先解析作品别名，再按规范编号顺序获取行锁。
            from core.booklist_repository import BooklistRepository

            resolved = {}
            for bid in sorted(scope):
                row = await BooklistRepository(session).get_booklist(bid)
                if row is None:
                    raise HTTPException(404, "书单不存在")
                resolved[bid] = row.id
            canonical_scope = set(resolved.values())
            canonical_targets = {resolved[bid] for bid in targets}
            rows = {
                bid: await self.access(session, bid, command.actor, manage=True)
                for bid in sorted(canonical_scope)
            }
            service = WorkService(session)
            work = await service.visible(work_id, command.actor)
            present = set(
                (
                    await session.execute(
                        select(BooklistItem.booklist_id).where(
                            BooklistItem.booklist_id.in_(canonical_scope),
                            BooklistItem.work_id == work_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            added, removed = canonical_targets - present, present - canonical_targets
            for bid in sorted(added | removed):
                await service.collect(work, command.actor, bid in added, bid)
                if bid in added:
                    item = (
                        await session.execute(
                            select(BooklistItem).where(
                                BooklistItem.booklist_id == bid,
                                BooklistItem.work_id == work_id,
                            )
                        )
                    ).scalar_one()
                    maximum = (
                        await session.execute(
                            select(func.max(BooklistItem.display_order)).where(
                                BooklistItem.booklist_id == bid
                            )
                        )
                    ).scalar_one() or 0
                    item.display_order = maximum + 1
                rows[bid].updated_at = utc_now()
            if metadata:
                items = (
                    (
                        await session.execute(
                            select(BooklistItem).where(
                                BooklistItem.booklist_id.in_(canonical_targets),
                                BooklistItem.work_id == work_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for item in items:
                    item.comment = metadata["comment"]
                    rows[item.booklist_id].updated_at = utc_now()
            await session.commit()
            return {
                "work_id": str(work_id),
                "added_to_booklist_ids": sorted(added),
                "removed_from_booklist_ids": sorted(removed),
                "unchanged_booklist_ids": sorted(canonical_scope - added - removed),
            }

    async def collect_booklist(self, booklist_id, command):
        """按当前用户权限更新书单收藏及公开计数。"""
        principal = command.actor
        async with self.sessions() as session:
            # 锁定书单，使收藏关系与公开计数在同一事务内更新。
            row = await self.access(session, booklist_id, principal)
            row = (
                await session.execute(
                    select(Booklist)
                    .where(Booklist.id == row.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if row is None or (
                not row.is_public and row.fluxer_owner_id != principal["id"]
            ):
                raise HTTPException(404, "书单不存在")
            existing = (
                (
                    await session.execute(
                        select(UserCollection).where(
                            UserCollection.target_type == 2,
                            UserCollection.target_id == row.id,
                            UserCollection.fluxer_user_id == principal["id"],
                        )
                    )
                )
                .scalars()
                .all()
            )
            collected = command.operation == "add"
            if collected and not existing:
                session.add(
                    UserCollection(
                        target_type=2,
                        target_id=row.id,
                        fluxer_user_id=principal["id"],
                        user_id=int(principal["discord_user_id"]),
                    )
                )
                row.collection_count += 1
            elif not collected and existing:
                for item in existing:
                    await session.delete(item)
                row.collection_count = max(0, row.collection_count - len(existing))
            await session.commit()
            return {"collected": collected}
