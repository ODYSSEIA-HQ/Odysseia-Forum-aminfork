"""协议 3.5：GET /chat/unread-summary、GET /chat/targets/{kind}/{id}。

实现在 services/chat.py：Fluxer 数据走 FluxerPort.unread_summary / chat_target_info；
全年龄模式下受限作品 404，未读汇总里也不出现。
"""

from fastapi import APIRouter

from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.services import chat

router = APIRouter(tags=["聊天外层"])


@router.get("/chat/unread-summary", summary="所有交流区和会话的未读数、@我 数")
async def get_unread_summary(viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        return await chat.unread_summary(session, ctx)


@router.get("/chat/targets/{kind}/{id}", summary="面板能否打开这个目标、标题、成员数")
async def get_chat_target(kind: str, id: str, ctx: Ctx):
    async with get_session_factory()() as session:
        return await chat.target_info(session, ctx, kind, id)
