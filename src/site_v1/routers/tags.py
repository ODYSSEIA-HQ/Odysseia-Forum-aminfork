"""协议 3.4：GET /tags/catalog（site_tag_group、site_tag、site_tag_question，maxTags 取配置，
version 取 site_setting[tag_catalog]）。作品上的 tag 在 site_work_tag。

还没实现：这里先建好空 router 并已注册到 /api/v1，接手的人直接往里加路由。
约定见 docs/site_v1/README.md「后面领域怎么接」。
"""

from fastapi import APIRouter

router = APIRouter(tags=["tag 榜单搜索"])
