"""协议 3.3：POST /uploads、POST /uploads/{id}/complete、GET /uploads/{id}。
表：attachment（state: uploading → processing → ready/rejected；rating、auto_rating、model_scores）。
分级器要有测试桩（1×1 PNG 判 general/ready）。

还没实现：这里先建好空 router 并已注册到 /api/v1，接手的人直接往里加路由。
约定见 docs/site_v1/README.md「后面领域怎么接」。
"""

from fastapi import APIRouter

router = APIRouter(tags=["上传"])
