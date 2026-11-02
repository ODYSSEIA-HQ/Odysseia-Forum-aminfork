"""V2 与既有服务使用同一份启动配置。"""

import json
from functools import lru_cache


@lru_cache(maxsize=1)
def get_work_config():
    """每进程读取一次配置，部署变更通过进程重启生效。"""
    with open("config.json", encoding="utf-8") as stream:
        return json.load(stream)
