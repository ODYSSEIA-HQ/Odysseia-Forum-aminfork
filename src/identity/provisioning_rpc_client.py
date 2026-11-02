"""使用独立 discord-sync 凭据的持久离线 RPC 客户端。"""

import json
import uuid
from identity.provisioning_rpc_error import ProvisioningRpcError


class ProvisioningRpcClient:
    """只支持身份预建与批量读取，不能铸造登录会话。"""

    def __init__(self, url, password):
        """保存独立 Bot 连接配置，连接由执行入口显式建立。"""
        self.url = url
        self.password = password
        self.connection = None

    async def connect(self):
        """建立独立 Bot 回复命名空间，连接失败不会输出认证信息。"""
        try:
            import nats

            self.connection = await nats.connect(
                self.url,
                user="discord-sync",
                password=self.password,
                inbox_prefix=b"_INBOX.bot",
                connect_timeout=3,
                max_reconnect_attempts=3,
            )
        except Exception as exc:
            raise ProvisioningRpcError("CONNECTION_UNAVAILABLE", True) from exc

    async def rpc(self, subject, **fields):
        """首次预建允许二十五秒，其余批量读取使用五秒固定边界。"""
        allowed = {
            "rpc.identity.v1.ensure_discord",
            "rpc.identity.v1.lookup_discord",
            "rpc.identity.v1.profiles",
        }
        if subject not in allowed or self.connection is None:
            raise ProvisioningRpcError("INVALID_OPERATION")
        try:
            response = await self.connection.request(
                subject,
                json.dumps(
                    {"version": 1, "request_id": uuid.uuid4().hex, **fields}
                ).encode(),
                timeout=25 if subject.endswith("ensure_discord") else 5,
            )
            payload = json.loads(response.data)
            if payload.get("ok") is True and isinstance(payload.get("data"), dict):
                return payload["data"]
            error = payload.get("error", {})
            raise ProvisioningRpcError(
                error.get("code", "INVALID_RESPONSE"), error.get("retryable", False)
            )
        except ProvisioningRpcError:
            raise
        except Exception as exc:
            raise ProvisioningRpcError("RPC_UNAVAILABLE", True) from exc

    async def close(self):
        """在成功或失败退出时关闭唯一 NATS 连接。"""
        if self.connection:
            await self.connection.close()
            self.connection = None
