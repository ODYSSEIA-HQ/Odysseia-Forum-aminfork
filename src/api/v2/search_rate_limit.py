"""作品推荐沿用现有独立限流配置，身份仅由 V2 当前成员依赖提供。"""

from fastapi import Depends, Request, Response
from api.v1.dependencies.rate_limit import similar_rate_limit
from api.v2.dependencies import current_member


async def similar_work_rate_limit(
    request: Request, response: Response, principal=Depends(current_member)
):
    """复用 Redis 窗口、429/Retry-After 和审计，不触发 V1 JWT 认证。"""
    await similar_rate_limit(request, response, current_user=principal)
