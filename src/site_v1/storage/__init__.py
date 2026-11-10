"""作品文件和图片的对象存储适配器（不走 Fluxer 附件：Fluxer 会剥 PNG tEXt，角色卡会坏）。

- StoragePort：接口（port.py）；
- LocalStorage：本地文件 + 本服务签名端点 /api/storage/...（本地开发、测试、契约测试）；
- S3Storage：S3 兼容存储（生产 Cloudflare R2），预签名 PUT / GET。

get_storage() 按 SITE_STORAGE_BACKEND 返回单例，测试用 set_storage() 替换。
"""

from site_v1.storage.port import StoragePort

_storage: StoragePort | None = None


def get_storage() -> StoragePort:
    global _storage
    if _storage is None:
        from site_v1.files_config import get_files_config

        config = get_files_config()
        if config.storage_backend == "s3":
            from site_v1.storage.s3 import S3Storage

            _storage = S3Storage(config)
        else:
            from site_v1.storage.local import LocalStorage

            _storage = LocalStorage(config)
    return _storage


def set_storage(storage: StoragePort | None) -> None:
    global _storage
    _storage = storage


__all__ = ["StoragePort", "get_storage", "set_storage"]
