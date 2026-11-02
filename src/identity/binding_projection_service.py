"""权威绑定事件和管理RPC结果共用的单调投影。"""

from fastapi import HTTPException
from sqlalchemy import select, update, text
from sqlalchemy.dialects.postgresql import insert
from models.discord_identity import DiscordIdentity


class BindingProjectionService:
    """用户级串行化并拒绝迟到版本，不让旧任务撤销新当前绑定。"""

    def __init__(self, session):
        """复用调用方事务。"""
        self.session = session

    async def apply(self, event):
        """普通事件不能搬移历史归属，显式管理合并先迁移业务。"""
        user_id = event["user_id"]
        version = event["version"]
        from models.user import User

        participants = {user_id}
        if event.get("kind") == "managed_merge" and event.get("merged_user_id"):
            participants.add(event["merged_user_id"])
        for identifier in sorted(participants):
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "identity-merge:" + identifier},
            )
        profile = await self.session.get(User, user_id, populate_existing=True)
        if profile is not None and profile.merged_into_id:
            return False
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "binding:" + user_id},
        )
        rows = (
            (
                await self.session.execute(
                    select(DiscordIdentity)
                    .where(DiscordIdentity.user_id == user_id)
                    .order_by(DiscordIdentity.version.desc())
                )
            )
            .scalars()
            .all()
        )
        if rows and max(row.version for row in rows) >= version:
            return False
        historical = await self.session.get(DiscordIdentity, event["discord_user_id"])
        if historical and historical.user_id != user_id:
            merged = event.get("merged_user_id")
            operation = event.get("operation_id")
            actor = event.get("actor_user_id")
            if (
                event.get("kind") != "managed_merge"
                or merged != historical.user_id
                or not operation
                or not actor
            ):
                raise HTTPException(409, "历史身份仅允许显式管理合并迁移")
            from identity.identity_merge_service import IdentityMergeService

            await IdentityMergeService(self.session).merge(
                merged, user_id, operation, actor
            )
        await self.session.execute(
            update(DiscordIdentity)
            .where(DiscordIdentity.user_id == user_id)
            .values(is_current=False)
        )
        statement = insert(DiscordIdentity).values(
            discord_user_id=event["discord_user_id"],
            user_id=user_id,
            version=version,
            is_current=True,
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[DiscordIdentity.discord_user_id],
                set_={"user_id": user_id, "version": version, "is_current": True},
                where=DiscordIdentity.version < version,
            )
        )
        return True
