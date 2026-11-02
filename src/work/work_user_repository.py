"""作品账户偏好单表仓储。"""

from sqlalchemy import update
from models.user import User


class WorkUserRepository:
    """集中用户偏好写入，不处理HTTP或身份权限。"""

    def __init__(self, session):
        """复用调用方事务。"""
        self.session = session

    async def set_nsfw_preference(self, user_id, enabled):
        """更新全局成人内容开关，不覆盖其他用户资料。"""
        await self.session.execute(
            update(User).where(User.id == user_id).values(show_nsfw=enabled)
        )
