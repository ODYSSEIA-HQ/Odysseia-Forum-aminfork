"""S3 兼容存储（生产：Cloudflare R2）。

- 预签名用 botocore（纯本地计算，不发请求）；
- stat / read_head / put / delete 用 aioboto3（依赖里已有，备份模块也在用）。
- 直传是预签名 PUT，Content-Type 参与签名，浏览器必须带上 ticket.headers 里的 Content-Type。
- R2 要在桶的 CORS 里允许前端域名 PUT/GET 和 Content-Type 头。
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
        url = self._signer.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.config.s3_bucket, "Key": key, "ContentType": mime},
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

    async def put(self, key: str, data: bytes, mime: str) -> None:
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self.config.s3_bucket, Key=key, Body=data, ContentType=mime
            )

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.config.s3_bucket, Key=key)
