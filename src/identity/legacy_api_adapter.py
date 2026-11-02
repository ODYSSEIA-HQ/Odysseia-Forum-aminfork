"""旧前端身份字段与统一存储的显式协议适配。"""

from sqlalchemy import select
from models.discord_identity import DiscordIdentity
from identity.canonical_user_id import CanonicalUserId


class LegacyApiAdapter:
    """对外仅返回 Discord 身份，不能把 Fluxer 字符串伪装成旧 ID。"""

    def __init__(self, session):
        """复用当前请求数据库会话。"""
        self.session = session

    async def preferences_to_wire(self, dto, discord_user_id):
        """作者筛选优先当前绑定，未当前绑定时使用合法历史来源。"""
        ids = {
            str(value)
            for field in ("include_authors", "exclude_authors")
            for value in (getattr(dto, field) or [])
        }
        rows = (
            (
                await self.session.execute(
                    select(DiscordIdentity)
                    .where(DiscordIdentity.user_id.in_(ids))
                    .order_by(
                        DiscordIdentity.is_current.desc(),
                        DiscordIdentity.discord_user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        mapping = {}
        for row in rows:
            mapping.setdefault(row.user_id, int(row.discord_user_id))
        values = dto.model_dump()
        values["user_id"] = int(discord_user_id)
        for field in ("include_authors", "exclude_authors"):
            authors = values.get(field)
            values[field] = (
                None
                if authors is None
                else [mapping[str(value)] for value in authors if str(value) in mapping]
            )
        return dto.__class__.model_validate(values)

    async def preserve_native_preferences(self, data, existing):
        """旧前端只能编辑可表示部分，原样往返不得删除纯原生作者筛选。"""
        if existing is None:
            return data
        values = dict(data)
        ids = {
            str(value)
            for field in ("include_authors", "exclude_authors")
            for value in (getattr(existing, field) or [])
        }
        represented = set(
            (
                await self.session.execute(
                    select(DiscordIdentity.user_id).where(
                        DiscordIdentity.user_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for field in ("include_authors", "exclude_authors"):
            if field not in values or values[field] is None:
                continue
            hidden = [
                CanonicalUserId(str(value))
                for value in (getattr(existing, field) or [])
                if str(value) not in represented
            ]
            values[field] = list(values[field]) + hidden
        return values
