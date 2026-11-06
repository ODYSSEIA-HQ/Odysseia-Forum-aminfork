"""协议 3.5：GET /chat/unread-summary、GET /chat/targets/{kind}/{id}。
Fluxer 数据走 FluxerPort.unread_summary / chat_target_info；全年龄模式下受限作品 404。

还没实现：这里先建好空 router 并已注册到 /api/v1，接手的人直接往里加路由。
约定见 docs/site_v1/README.md「后面领域怎么接」。
"""

from fastapi import APIRouter

router = APIRouter(tags=["聊天外层"])
