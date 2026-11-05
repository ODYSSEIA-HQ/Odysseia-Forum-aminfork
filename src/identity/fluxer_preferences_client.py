"""原生成人内容偏好的权威 RPC 边界，不保留本地授权副本。"""

import re

from fastapi import HTTPException
from identity.auth_runtime import get_auth_service
from identity.integration_rpc_error import IntegrationRpcError


class FluxerPreferencesClient:
    """复用已有受信 NATS 连接；调用者仅传入已认证主体和配置服务器。"""

    def __init__(self, rpc=None):
        """复用指定传输层或当前进程内的认证服务连接。"""
        self._rpc = rpc

    async def _request(self, subject, user_id, guild_id, **fields):
        """校验主体、请求权威状态并转换明确的协议错误。"""
        # 在发送请求前验证两个主体标识，拒绝隐式转换后的无效编号。
        for identifier in (user_id, guild_id):
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[1-9][0-9]{0,18}", identifier)
                or int(identifier) > 2**63 - 1
            ):
                raise HTTPException(422, "无效的偏好主体或服务器编号")
        try:
            rpc = self._rpc or get_auth_service().rpc
            result = await rpc(
                subject, actor_user_id=user_id, guild_id=guild_id, **fields
            )
        except IntegrationRpcError as exc:
            if exc.code == "STALE_VERSION":
                raise HTTPException(409, "成人内容偏好已变化，请刷新后重试") from exc
            if exc.code in {"RESOURCE_NOT_ALLOWED", "ACCOUNT_UNAVAILABLE"}:
                raise HTTPException(403, "不能访问此服务器的成人内容偏好") from exc
            if exc.code == "INVALID_REQUEST":
                raise HTTPException(422, "无效的成人内容偏好请求") from exc
            raise HTTPException(503, "Fluxer 成人内容偏好暂不可用") from exc
        except Exception as exc:
            # 不暴露服务端错误正文，也不把本地 User.show_nsfw 当作降级来源。
            raise HTTPException(503, "Fluxer 成人内容偏好暂不可用") from exc
        # 响应必须属于请求服务器，且包含严格的布尔状态和整数版本。
        if (
            not isinstance(result, dict)
            or result.get("guild_id") != guild_id
            or type(result.get("show_nsfw")) is not bool
            or type(result.get("nsfw_eligible")) is not bool
            or type(result.get("version")) is not int
            or not 0 <= result["version"] <= 2**53 - 1
        ):
            raise HTTPException(503, "Fluxer 返回了无效的成人内容偏好")
        return {
            key: result[key]
            for key in ("guild_id", "show_nsfw", "nsfw_eligible", "version")
        }

    async def get_preferences(self, user_id, guild_id):
        """每个请求重新读取权威值，不复用跨请求的展示偏好缓存。"""
        return await self._request("rpc.preferences.v1.get_service", user_id, guild_id)

    async def update_preferences(self, user_id, guild_id, show_nsfw, expected_version):
        """按权威版本比较更新，冲突或结果未知时不自动重试写入。"""
        if (
            type(show_nsfw) is not bool
            or type(expected_version) is not int
            or not 0 <= expected_version < 2**53 - 1
        ):
            raise HTTPException(422, "需要成人内容布尔偏好与非负整数版本")
        # 使用调用方读到的版本更新，不在冲突或未知结果后自动重试。
        result = await self._request(
            "rpc.preferences.v1.update_service",
            user_id,
            guild_id,
            show_nsfw=show_nsfw,
            expected_version=expected_version,
        )
        if (
            result["show_nsfw"] is not show_nsfw
            or result["version"] != expected_version + 1
        ):
            raise HTTPException(503, "Fluxer 返回了无效的成人内容偏好更新")
        return result
