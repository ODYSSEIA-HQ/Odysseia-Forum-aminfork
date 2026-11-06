"""协议 3.2：GET/POST /works、GET/PATCH/DELETE /works/{id}、POST /works/{id}/publish、
POST/DELETE /works/{id}/favorite、POST /works/{id}/tags、DELETE /works/{id}/tags/{tagId}。
复用 V2：work/work_repository.py（page、visibility_filters）、work/work_service.py（visible、project、collect）、
work/work_content_service.py；可见性只看本地 status=published（原生帖未建好只影响聊天）。
做好后实现 site_v1/services/user_works.list_user_works。

还没实现：这里先建好空 router 并已注册到 /api/v1，接手的人直接往里加路由。
约定见 docs/site_v1/README.md「后面领域怎么接」。
"""

from fastapi import APIRouter

router = APIRouter(tags=["作品"])
