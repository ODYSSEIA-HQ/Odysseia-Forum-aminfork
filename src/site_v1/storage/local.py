"""本地文件存储：模拟对象存储的预签名直传和签名读取（类似契约测试 mock server 的 /__storage）。

签名令牌 = base64url(JSON 载荷) + "." + base64url(HMAC-SHA256)。载荷：
  op   put / get
  k    存储 key
  m    MIME（put 时要求请求的 Content-Type 一致；get 时作为响应类型）
  s    put：申请时声明的字节数（传上来的必须一样）
  e    过期时间（unix 秒）
  u    get：签发给哪个账号（只记录，便于审计；地址本身就是短期的）
  n、d get：下载文件名、是否按附件下载
端点在 routers/storage.py：PUT /api/storage/upload/{token}、GET /api/storage/files/{token}。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from site_v1.files_config import FilesConfig

STORAGE_PREFIX = "/api/storage"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class LocalStorage:
    name = "local"

    def __init__(self, config: FilesConfig):
        self.config = config
        self.root = Path(config.local_dir)

    # ---------------- 签名 ----------------

    def sign(self, payload: dict) -> str:
        body = _b64(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        )
        mac = hmac.new(self.config.signing_key, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64(mac)}"

    def verify(self, token: str, op: str) -> dict | None:
        """校验签名、用途和有效期；不通过返回 None。"""
        try:
            body, mac = token.split(".", 1)
            expected = hmac.new(
                self.config.signing_key, body.encode(), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(_unb64(mac), expected):
                return None
            payload = json.loads(_unb64(body))
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("op") != op:
            return None
        if not isinstance(payload.get("e"), int) or payload["e"] < time.time():
            return None
        if not isinstance(payload.get("k"), str):
            return None
        return payload

    def _base(self, base: str | None) -> str:
        return (self.config.public_base or (base or "")).rstrip("/")

    # ---------------- 地址 ----------------

    def upload_ticket(
        self, key: str, mime: str, size: int, *, base: str | None
    ) -> dict:
        token = self.sign(
            {
                "op": "put",
                "k": key,
                "m": mime,
                "s": size,
                "e": int(time.time()) + self.config.ticket_ttl,
            }
        )
        return {
            "method": "PUT",
            "url": f"{self._base(base)}{STORAGE_PREFIX}/upload/{token}",
            "headers": {"Content-Type": mime},
        }

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
        ttl = self.config.public_read_ttl if public else self.config.read_ttl
        now = int(time.time())
        if public:
            # 全年龄图片：按有效期取整，同一时段内地址不变，浏览器能缓存
            expires = (now // ttl + 2) * ttl
        else:
            expires = now + ttl
        payload: dict = {"op": "get", "k": key, "m": mime, "e": expires}
        if viewer_id and not public:
            payload["u"] = viewer_id
        if filename:
            payload["n"] = filename
        if download:
            payload["d"] = 1
        return f"{self._base(None)}{STORAGE_PREFIX}/files/{self.sign(payload)}"

    # ---------------- 文件 ----------------

    def path_of(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("存储 key 越界")
        return path

    async def stat(self, key: str) -> int | None:
        path = self.path_of(key)
        try:
            return (await asyncio.to_thread(path.stat)).st_size
        except FileNotFoundError:
            return None

    async def read_head(self, key: str, length: int) -> bytes:
        def read() -> bytes:
            with self.path_of(key).open("rb") as fh:
                return fh.read(length)

        return await asyncio.to_thread(read)

    async def sha256(self, key: str) -> str:
        def digest() -> str:
            h = hashlib.sha256()
            with self.path_of(key).open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()

        return await asyncio.to_thread(digest)

    async def put(self, key: str, data: bytes, mime: str) -> None:
        await self.write_chunks(key, [data])

    async def write_chunks(self, key: str, chunks) -> None:
        """先写临时文件再改名：传一半断开不会留下残缺对象。chunks 可以是异步迭代器。"""
        path = self.path_of(key)
        tmp = path.with_name(path.name + f".part-{os.getpid()}-{time.monotonic_ns()}")
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        fh = await asyncio.to_thread(tmp.open, "wb")
        try:
            if hasattr(chunks, "__aiter__"):
                async for chunk in chunks:
                    await asyncio.to_thread(fh.write, chunk)
            else:
                for chunk in chunks:
                    await asyncio.to_thread(fh.write, chunk)
            await asyncio.to_thread(fh.close)
            await asyncio.to_thread(os.replace, tmp, path)
        except BaseException:
            fh.close()
            tmp.unlink(missing_ok=True)
            raise

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.path_of(key).unlink, missing_ok=True)
