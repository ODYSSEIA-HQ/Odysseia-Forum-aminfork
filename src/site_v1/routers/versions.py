"""协议 3.2 版本：GET/POST /works/{id}/versions、
GET /works/{id}/versions/{vid}/files/{fileId}/download（302 签名地址，work_statistics.download_count +1）。
表：work_version、work_version_file、attachment。

还没实现：这里先建好空 router 并已注册到 /api/v1，接手的人直接往里加路由。
约定见 docs/site_v1/README.md「后面领域怎么接」。
"""

from fastapi import APIRouter

router = APIRouter(tags=["作品"])
