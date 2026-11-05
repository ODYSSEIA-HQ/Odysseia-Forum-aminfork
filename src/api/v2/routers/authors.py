"""作者关注 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import Request, APIRouter, Depends
from api.v2.dependencies import current_member
from work.work_author_service import WorkAuthorService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.get("/authors/{author_id}")
async def author_profile(author_id: str, principal=Depends(current_member)):
    """统一作者资料取自 Fluxer 用户投影，作品数量先过滤可见性。"""
    return await WorkAuthorService(get_session_factory()).author_profile(
        author_id=author_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.put("/authors/{author_id}/follow")
@router.delete("/authors/{author_id}/follow")
async def follow_author(
    author_id: str, request: Request, principal=Depends(current_member)
):
    """作者关注沿用 V1 的同一 AuthorFollow 事实。"""
    return await WorkAuthorService(get_session_factory()).follow_author(
        author_id=author_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values={},
            operation="follow" if request.method == "PUT" else "unfollow",
        ),
    )


@router.get("/me/author-follows")
async def author_follows(principal=Depends(current_member)):
    """返回统一作者关注资料，不复制关注状态。"""
    return await WorkAuthorService(get_session_factory()).author_follows(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        )
    )


@router.get("/authors/discord/{discord_id}")
async def resolve_discord_author(discord_id: str, principal=Depends(current_member)):
    """旧作者分享链接只解析当前映射，不按数字碰撞猜测作者。"""
    return await WorkAuthorService(get_session_factory()).resolve_discord_author(
        discord_id,
        WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
