"""协议 3.10：POST /reports。表：report（同一账号对同一待处理对象重复举报 409，
由部分唯一索引 uq_report_pending_reporter_target 保证）、moderation_queue_item（举报聚合）。

还没实现：这里先建好空 router 并已注册到 /api/v1，接手的人直接往里加路由。
约定见 docs/site_v1/README.md「后面领域怎么接」。
"""

from fastapi import APIRouter

router = APIRouter(tags=["举报审核"])
