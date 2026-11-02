"""作者关注领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from sqlalchemy import select
from shared.time_utils import utc_now
from models.user import User
from work.work_repository import WorkRepository


class WorkAuthorService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def author_profile(self, author_id: str, command: WorkCommandQo):
        """统一作者资料取自 Fluxer 用户投影，作品数量先过滤可见性。"""
        principal = command.actor
        from models.author_follow import AuthorFollow

        async with self.sessions() as session:
            author = await session.get(User, author_id)
            if author is None:
                raise HTTPException(404, "作者不存在")
            _, total = await WorkRepository(session).page(
                principal, 0, 1, author_id=author_id
            )
            followed = (
                await session.execute(
                    select(AuthorFollow.id).where(
                        AuthorFollow.fluxer_user_id == principal["id"],
                        AuthorFollow.fluxer_author_id == author_id,
                        AuthorFollow.active_flag.is_(True),
                    )
                )
            ).first() is not None
            return {
                "id": author.id,
                "username": author.username,
                "display_name": author.display_name,
                "avatar_url": author.avatar_url,
                "work_count": total,
                "followed": followed,
            }

    async def follow_author(self, author_id: str, command: WorkCommandQo):
        """作者关注沿用 V1 的同一 AuthorFollow 事实。"""
        principal = command.actor
        from models.author_follow import AuthorFollow
        from models.discord_identity import DiscordIdentity

        if author_id == principal["id"]:
            raise HTTPException(422, "不能关注自己")
        async with self.sessions() as session:
            if await session.get(User, author_id) is None:
                raise HTTPException(404, "作者不存在")
            row = (
                (
                    await session.execute(
                        select(AuthorFollow)
                        .where(
                            AuthorFollow.fluxer_user_id == principal["id"],
                            AuthorFollow.fluxer_author_id == author_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if row is None and command.operation == "follow":
                legacy = (
                    await session.execute(
                        select(DiscordIdentity.discord_user_id).where(
                            DiscordIdentity.user_id == author_id,
                            DiscordIdentity.is_current.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                session.add(
                    AuthorFollow(
                        user_id=int(principal["discord_user_id"]),
                        author_id=int(legacy) if legacy else None,
                        fluxer_user_id=principal["id"],
                        fluxer_author_id=author_id,
                        active_flag=True,
                    )
                )
            elif row is not None:
                row.active_flag = command.operation == "follow"
                if row.active_flag:
                    row.followed_at = utc_now()
            await session.commit()
        return {"followed": command.operation == "follow"}

    async def author_follows(self, command: WorkCommandQo):
        """返回统一作者关注资料，不复制关注状态。"""
        principal = command.actor
        from models.author_follow import AuthorFollow

        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(User)
                        .join(AuthorFollow, AuthorFollow.fluxer_author_id == User.id)
                        .where(
                            AuthorFollow.fluxer_user_id == principal["id"],
                            AuthorFollow.active_flag.is_(True),
                        )
                        .order_by(AuthorFollow.followed_at.desc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "username": row.username,
                        "display_name": row.display_name,
                        "avatar_url": row.avatar_url,
                    }
                    for row in rows
                ]
            }
