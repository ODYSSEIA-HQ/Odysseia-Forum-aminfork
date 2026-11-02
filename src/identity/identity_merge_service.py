"""显式管理账户合并的 Forum 业务归属迁移。"""

from sqlalchemy import select, text, update, UniqueConstraint
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import SQLModel
from models.user import User
from models.discord_identity import DiscordIdentity
from models.operation_log import OperationLog
from models.work_event import WorkEvent
from identity.reference_manifest import OWNERSHIP_REFERENCES, fluxer_column
from shared.time_utils import utc_now


class IdentityMergeService:
    """仅处理已由 Fluxer 批准的显式 merge，不允许普通绑定搬移归属。"""

    def __init__(self, session):
        """业务迁移与权威绑定投影共用一个 PG 事务。"""
        self.session = session

    async def merge(self, source_id, target_id, operation_id, actor_id):
        """幂等合并业务、保留原身份审计并拒绝循环合并。"""
        if not source_id or source_id == target_id or not operation_id or not actor_id:
            raise ValueError("无效管理合并契约")
        for identifier in sorted((source_id, target_id)):
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "identity-merge:" + identifier},
            )
        source = await self.session.get(User, source_id, with_for_update=True)
        target = await self.session.get(User, target_id, with_for_update=True)
        if source is None or target is None or target.merged_into_id:
            raise ValueError("合并源或目标账户不可用")
        if source.merged_into_id:
            if source.merged_into_id == target_id:
                return False
            raise ValueError("账户已合并到其他主体")
        # 队列升轮播与归属合并共用scope锁，防止复制旧owner后被漏迁。
        scopes = (
            (
                await self.session.execute(
                    text("""SELECT target_scope AS scope FROM banner_application WHERE fluxer_owner_id IN (:source,:target)
          UNION SELECT channel_id::text FROM banner_carousel WHERE fluxer_owner_id IN (:source,:target)
          UNION SELECT channel_id::text FROM banner_waitlist WHERE fluxer_owner_id IN (:source,:target)"""),
                    {"source": source_id, "target": target_id},
                )
            )
            .scalars()
            .all()
        )
        normalized = {
            None if scope in (None, "global") else int(scope) for scope in scopes
        }
        for scope in sorted(
            normalized, key=lambda value: (value is not None, value or 0)
        ):
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {
                    "key": "banner:scope:"
                    + (str(scope) if scope is not None else "global")
                },
            )

        before = [
            {
                "discord_user_id": row.discord_user_id,
                "user_id": row.user_id,
                "is_current": row.is_current,
                "version": row.version,
            }
            for row in (
                await self.session.execute(
                    select(DiscordIdentity).where(
                        DiscordIdentity.user_id.in_([source_id, target_id])
                    )
                )
            ).scalars()
        ]
        event = (
            insert(WorkEvent)
            .values(
                event_id="identity.merge:" + operation_id,
                payload={
                    "kind": "managed_merge",
                    "source_id": source_id,
                    "target_id": target_id,
                    "actor_id": actor_id,
                    "bindings_before": before,
                },
                received_at=utc_now(),
            )
            .on_conflict_do_nothing()
            .returning(WorkEvent.event_id)
        )
        if (await self.session.execute(event)).scalar_one_or_none() is None:
            return False
        await self.merge_default_booklists(source_id, target_id, operation_id, actor_id)
        # 合并后的自关注没有意义，提前删除以满足两套身份检查。
        await self.session.execute(
            text(
                "DELETE FROM author_follow WHERE fluxer_user_id IN (:source,:target) AND fluxer_author_id IN (:source,:target)"
            ),
            {"source": source_id, "target": target_id},
        )
        for table_name, columns in OWNERSHIP_REFERENCES.items():
            if table_name == "operation_log":
                continue
            table = SQLModel.metadata.tables[table_name]
            for original in columns:
                column = fluxer_column(original)
                await self.remove_collisions(
                    table, column, source_id, target_id, operation_id, actor_id
                )
                await self.session.execute(
                    update(table)
                    .where(table.c[column] == source_id)
                    .values({column: target_id})
                )
        for table, column in (
            ("work", "author_id"),
            ("banner_application", "fluxer_owner_id"),
            ("banner_carousel", "fluxer_owner_id"),
            ("banner_waitlist", "fluxer_owner_id"),
        ):
            if column not in SQLModel.metadata.tables[table].c:
                continue
            await self.session.execute(
                text(f"UPDATE {table} SET {column}=:target WHERE {column}=:source"),
                {"source": source_id, "target": target_id},
            )
        from models.work_user_mute import WorkUserMute

        restrictions = (
            (
                await self.session.execute(
                    select(WorkUserMute)
                    .where(WorkUserMute.user_id == source_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for restriction in restrictions:
            if restriction.revoked_at is None:
                existing = (
                    (
                        await self.session.execute(
                            select(WorkUserMute)
                            .where(
                                WorkUserMute.user_id == target_id,
                                WorkUserMute.work_id == restriction.work_id,
                                WorkUserMute.actor_id == restriction.actor_id,
                                WorkUserMute.revoked_at.is_(None),
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing:
                    existing.expires_at = (
                        None
                        if existing.expires_at is None or restriction.expires_at is None
                        else max(existing.expires_at, restriction.expires_at)
                    )
                    restriction.revoked_at = utc_now()
                    await self.session.flush()
            restriction.user_id = target_id

        for column in ("fluxer_include_authors", "fluxer_exclude_authors"):
            await self.session.execute(
                text(
                    f"UPDATE user_search_preferences p SET {column}=(SELECT json_agg(CASE WHEN value=:source THEN :target ELSE value END ORDER BY ord) FROM json_array_elements_text(p.{column}) WITH ORDINALITY AS entries(value,ord)) WHERE p.{column} IS NOT NULL"
                ),
                {"source": source_id, "target": target_id},
            )
        # 原始绑定和实际操作者保留在审计事件中，canonical 归属可沿显式合并链移动。
        await self.session.execute(
            update(DiscordIdentity)
            .where(DiscordIdentity.user_id.in_([source_id, target_id]))
            .values(is_current=False)
        )
        await self.session.execute(
            update(DiscordIdentity)
            .where(DiscordIdentity.user_id == source_id)
            .values(user_id=target_id)
        )
        source.merged_into_id = target_id
        self.session.add(
            OperationLog(
                type="identity.managed_merge",
                target_type="user",
                target_id=0,
                fluxer_actor_id=actor_id,
                detail={
                    "operation_id": operation_id,
                    "source_user_id": source_id,
                    "target_user_id": target_id,
                    "original_bindings": before,
                },
            )
        )
        # 去掉合并产生的重复票后重建轮次票数，不改变历史审计。
        await self.session.execute(
            text(
                "UPDATE tag_binding b SET upvotes=(SELECT count(*) FROM tag_vote v WHERE v.binding_id=b.id AND v.vote=1),downvotes=(SELECT count(*) FROM tag_vote v WHERE v.binding_id=b.id AND v.vote=-1) WHERE EXISTS(SELECT 1 FROM tag_vote v WHERE v.binding_id=b.id AND v.fluxer_user_id=:target)"
            ),
            {"target": target_id},
        )
        await self.session.flush()
        return True

    async def remove_collisions(
        self, table, column, source_id, target_id, operation_id, actor_id
    ):
        """目标账户已有同一事实时优先保留目标，并记录冲突原行快照。"""
        candidates = [
            list(item.columns)
            for item in table.constraints
            if isinstance(item, UniqueConstraint)
        ]
        candidates.extend(list(item.columns) for item in table.indexes if item.unique)
        seen = set()
        for columns in candidates:
            names = tuple(item.name for item in columns)
            if column not in names or names in seen:
                continue
            seen.add(names)
            match = (
                " AND ".join(
                    f'old."{name}" IS NOT DISTINCT FROM current."{name}"'
                    for name in names
                    if name != column
                )
                or "true"
            )
            parameters = {"source": source_id, "target": target_id}
            predicate = (
                f'old."{column}"=:source AND current."{column}"=:target AND {match}'
            )
            rows = (
                (
                    await self.session.execute(
                        text(
                            f'SELECT row_to_json(old) FROM "{table.name}" old WHERE EXISTS(SELECT 1 FROM "{table.name}" current WHERE {predicate})'
                        ),
                        parameters,
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                self.session.add(
                    OperationLog(
                        type="identity.merge_collision",
                        target_type="user",
                        target_id=0,
                        fluxer_actor_id=actor_id,
                        detail={
                            "operation_id": operation_id,
                            "table": table.name,
                            "original_row": row,
                            "target_user_id": target_id,
                        },
                    )
                )
            if table.name == "thread_follow":
                await self.session.execute(
                    text(
                        f"UPDATE thread_follow current SET active_flag=current.active_flag OR old.active_flag,last_viewed_at=greatest(current.last_viewed_at,old.last_viewed_at) FROM thread_follow old WHERE {predicate}"
                    ),
                    parameters,
                )
            if table.name == "notification":
                await self.session.execute(
                    text(
                        f"UPDATE notification current SET read_at=greatest(current.read_at,old.read_at) FROM notification old WHERE {predicate}"
                    ),
                    parameters,
                )
            await self.session.execute(
                text(
                    f'DELETE FROM "{table.name}" old USING "{table.name}" current WHERE {predicate}'
                ),
                parameters,
            )

    async def merge_default_booklists(
        self, source_id, target_id, operation_id, actor_id
    ):
        """默认书单条目取并集，重复条目的原备注保留在合并审计。"""
        rows = (
            await self.session.execute(
                text(
                    "SELECT id,fluxer_owner_id FROM booklist WHERE is_default AND fluxer_owner_id IN (:source,:target) FOR UPDATE"
                ),
                {"source": source_id, "target": target_id},
            )
        ).all()
        ids = {row.fluxer_owner_id: row.id for row in rows}
        if source_id not in ids or target_id not in ids:
            return
        parameters = {"old": ids[source_id], "current": ids[target_id]}
        duplicates = (
            (
                await self.session.execute(
                    text(
                        "SELECT row_to_json(old) FROM booklist_item old JOIN booklist_item current ON current.booklist_id=:current AND ((old.work_id IS NOT NULL AND old.work_id=current.work_id) OR (old.thread_id IS NOT NULL AND old.thread_id=current.thread_id)) WHERE old.booklist_id=:old"
                    ),
                    parameters,
                )
            )
            .scalars()
            .all()
        )
        for row in duplicates:
            self.session.add(
                OperationLog(
                    type="identity.merge_booklist_item",
                    target_type="booklist",
                    target_id=ids[target_id],
                    fluxer_actor_id=actor_id,
                    detail={
                        "operation_id": operation_id,
                        "source_booklist_id": str(ids[source_id]),
                        "target_booklist_id": str(ids[target_id]),
                        "original_item": row,
                    },
                )
            )
        await self.session.execute(
            text(
                "DELETE FROM booklist_item old USING booklist_item current WHERE old.booklist_id=:old AND current.booklist_id=:current AND ((old.work_id IS NOT NULL AND old.work_id=current.work_id) OR (old.thread_id IS NOT NULL AND old.thread_id=current.thread_id))"
            ),
            parameters,
        )
        await self.session.execute(
            text(
                "UPDATE booklist_item SET booklist_id=:current WHERE booklist_id=:old"
            ),
            parameters,
        )
        await self.session.execute(
            text(
                "UPDATE booklist SET is_default=false,item_count=0,merged_into_id=:current WHERE id=:old"
            ),
            parameters,
        )
        await self.session.execute(
            text(
                "UPDATE booklist SET item_count=(SELECT count(*) FROM booklist_item WHERE booklist_id=:current) WHERE id=:current"
            ),
            parameters,
        )
