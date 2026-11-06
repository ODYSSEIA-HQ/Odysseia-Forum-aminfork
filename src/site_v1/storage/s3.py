"""S3 兼容存储（生产：Cloudflare R2）。

- 预签名用 botocore（纯本地计算，不发请求）；
- stat / read_head / put / delete 用 aioboto3（依赖里已有，备份模块也在用）。
- 直传是预签名 PUT，Content-Type 和 Content-Length 参与签名：浏览器必须带上 ticket.headers 里的
  Content-Type，字节数必须和 POST /uploads 时声明的 size 一样（Content-Length 浏览器自动带）。
- R2 要在桶的 CORS 里允许前端域名 PUT/GET/HEAD 和 Content-Type 头，见 docs/site_v1/README.md。
"""

from functools import cached_property
from urllib.parse import quote

from site_v1.files_config import FilesConfig


class S3Storage:
    name = "s3"

    def __init__(self, config: FilesConfig):
        self.config = config

    def _client_kwargs(self) -> dict:
        from botocore.config import Config

        kwargs = {
            "region_name": self.config.s3_region or "auto",
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }
        if self.config.s3_endpoint:
            kwargs["endpoint_url"] = self.config.s3_endpoint
        if self.config.s3_access_key:
            kwargs["aws_access_key_id"] = self.config.s3_access_key
            kwargs["aws_secret_access_key"] = self.config.s3_secret_key
        return kwargs

    @cached_property
    def _signer(self):
        """只用来生成预签名地址的同步客户端（不联网）。"""
        import botocore.session

        return botocore.session.get_session().create_client(
            "s3", **self._client_kwargs()
        )

    def _client(self):
        import aioboto3

        return aioboto3.Session().client("s3", **self._client_kwargs())

    # ---------------- 地址 ----------------

    def upload_ticket(
        self, key: str, mime: str, size: int, *, base: str | None
    ) -> dict:
        # Content-Length 也签进去：S3 / R2 会拒绝字节数和申请时不一样的直传，
        # 不然拿到地址就能往桶里塞任意大的文件（complete 时才发现大小不对，对象已经占了空间）。
        # 不放进 ticket.headers：浏览器不允许脚本设置 Content-Length，会按实际字节数自动带上。
        url = self._signer.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.config.s3_bucket,
                "Key": key,
                "ContentType": mime,
                "ContentLength": size,
            },
            ExpiresIn=self.config.ticket_ttl,
        )
        return {"method": "PUT", "url": url, "headers": {"Content-Type": mime}}

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
        if public and self.config.s3_public_base and not download:
            return f"{self.config.s3_public_base}/{quote(key)}"
        params = {
            "Bucket": self.config.s3_bucket,
            "Key": key,
            "ResponseContentType": mime,
        }
        if download and filename:
            params["ResponseContentDisposition"] = (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            )
        return self._signer.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=self.config.public_read_ttl if public else self.config.read_ttl,
        )

    # ---------------- 对象 ----------------

    async def stat(self, key: str) -> int | None:
        from botocore.exceptions import ClientError

        async with self._client() as s3:
            try:
                head = await s3.head_object(Bucket=self.config.s3_bucket, Key=key)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return None
                raise
        return int(head["ContentLength"])

    async def read_head(self, key: str, length: int) -> bytes:
        async with self._client() as s3:
            obj = await s3.get_object(
                Bucket=self.config.s3_bucket, Key=key, Range=f"bytes=0-{length - 1}"
            )
            async with obj["Body"] as body:
                return await body.read()

    async def sha256(self, key: str) -> str:
        """流式读整个对象算 SHA-256（分级完成时记进 attachment.sha256，和本地存储一致）。"""
        import hashlib

        h = hashlib.sha256()
        async with self._client() as s3:
            obj = await s3.get_object(Bucket=self.config.s3_bucket, Key=key)
            body = obj["Body"]
            try:
                async for chunk in body.iter_chunks(1 << 20):
                    h.update(chunk)
            finally:
                body.close()
        return h.hexdigest()

    async def put(self, key: str, data: bytes, mime: str) -> None:
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self.config.s3_bucket, Key=key, Body=data, ContentType=mime
            )

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.config.s3_bucket, Key=key)
