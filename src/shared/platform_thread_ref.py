"""按部署配置构造平台来源链接。"""

import os
from urllib.parse import urlsplit


def fluxer_thread_ref(guild_id, thread_id, web_origin=None):
    """自部署来源必须使用配置站点，缺配置时不指向上游官方站。"""
    origin = web_origin or os.environ.get("FLUXER_WEB_URL", "")
    if not origin:
        api = urlsplit(os.environ.get("FLUXER_API_URL", ""))
        origin = f"{api.scheme}://{api.netloc}" if api.scheme and api.netloc else ""
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return ""
    return origin.rstrip("/") + "/channels/" + str(guild_id) + "/" + str(thread_id)
