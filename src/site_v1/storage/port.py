"""存储适配器接口。key 由服务端生成（att/{随机}/{文件名}），调用方不用关心落在哪。"""

from typing import Protocol


class StoragePort(Protocol):
    name: str

    def upload_ticket(
        self, key: str, mime: str, size: int, *, base: str | None
    ) -> dict:
        """直传凭证 {method, url, headers}；base 是本服务对外根地址（本地存储拼绝对地址用）。"""

    def read_url(
        self,
        key: str,
        *,
        mime: str,
        public: bool,
        filename: str | None = None,
        download: bool = False,
        viewer_id: str | None = None,
    ) -> str:
        """读取地址。public=True（全年龄图片）可以给公开地址或长效签名地址；
        否则是短期签名地址（SITE_SIGNED_URL_TTL），viewer_id 记进签名（按账号签发）。
        download=True 时让浏览器按 filename 存成文件。"""

    async def stat(self, key: str) -> int | None:
        """对象大小；不存在返回 None。"""

    async def read_head(self, key: str, length: int) -> bytes:
        """读前 length 字节（取图片宽高、给分级器）。"""

    async def put(self, key: str, data: bytes, mime: str) -> None:
        """服务端直接写（种子数据用）。"""

    async def delete(self, key: str) -> None:
        """删对象；不存在不报错。"""
