"""作品领域操作输入。"""

from dataclasses import dataclass, field
from typing import Any
from dto.work.work_actor_dto import WorkActorDTO


@dataclass(frozen=True, slots=True)
class WorkCommandQo:
    """路由完成协议翻译后传入领域，不携带FastAPI Request。"""

    actor: WorkActorDTO
    """已经验证的主体DTO。"""

    values: dict[str, Any] = field(default_factory=dict)
    """已经解析的操作数据，不包含数据库实体或会话。"""

    operation: str | None = None
    """明确的领域动作，例如collect、withdraw或review。"""
