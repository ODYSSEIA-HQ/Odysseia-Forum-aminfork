"""作品领域的已认证主体快照。"""

from dataclasses import dataclass, field, fields
from collections.abc import Mapping
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class WorkActorDTO(Mapping[str, Any]):
    """脱离HTTP请求与数据库会话的身份、能力和配置快照。"""

    id: str
    """统一用户编号。"""

    discord_user_id: str
    """已验证的当前Discord绑定。"""

    username: str = ""
    """原生用户名。"""

    display_name: str | None = None
    """原生显示名称。"""

    avatar_url: str | None = None
    """原生头像。"""

    is_admin: bool = False
    """已核验的管理资格。"""

    can_create_work: bool = False
    """能否创建作品。"""

    can_view_abyss: bool = False
    """能否访问深渊作品。"""

    can_speak: bool = False
    """全局发言资格，不影响收藏和标签。"""

    show_nsfw: bool = False
    """全局成人内容偏好。"""

    forums: dict = field(default_factory=dict, repr=False)
    """已初始化的平台论坛配置。"""

    config: dict = field(default_factory=dict, repr=False)
    """仅供服务端权限和领域配置使用，不对外序列化。"""

    @classmethod
    def from_principal(cls, principal):
        """只复制白名单字段，不传递会话、原始token或HTTP对象。"""
        if isinstance(principal, cls):
            return principal
        names = {item.name for item in fields(cls)}
        values = {key: value for key, value in principal.items() if key in names}
        values["id"] = str(values["id"])
        values["discord_user_id"] = str(values["discord_user_id"])
        return cls(**values)

    def __getitem__(self, key):
        """兼容领域现有Mapping读取风格。"""
        if key not in {item.name for item in fields(self)}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        """返回DTO声明的字段名。"""
        return iter(item.name for item in fields(self))

    def __len__(self):
        """返回DTO白名单字段数。"""
        return len(fields(self))
