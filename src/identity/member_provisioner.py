"""已验证正式成员的有界幂等预建与批量权威导出。"""

import asyncio
import hashlib
import uuid
from identity.provisioning_rpc_error import ProvisioningRpcError


def identity_id(value):
    """拒绝零、前导零、布尔值及超出权威 ID 范围的值。"""
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
        or len(value) > 19
        or int(value) > 9223372036854775807
    ):
        raise ValueError("INVALID_IDENTITY")
    return value


def operation_id(discord_id):
    """操作编号只依赖命名空间与 Discord 主体，不随文件位置或重跑变化。"""
    return hashlib.sha256(
        ("forum-formal-members-v1:" + discord_id).encode()
    ).hexdigest()


class MemberProvisioner:
    """不抓取 Discord 数据，不创建会话，不接受客户端指定的 Fluxer ID。"""

    def __init__(self, rpc, journal, concurrency=8, attempts=3):
        """校验并发与重试边界，复用持久日志和 RPC 客户端。"""
        if not 1 <= concurrency <= 32 or not 1 <= attempts <= 5:
            raise ValueError("INVALID_BOUNDS")
        self.rpc = rpc
        self.journal = journal
        self.semaphore = asyncio.Semaphore(concurrency)
        self.attempts = attempts

    async def request(self, subject, **fields):
        """仅对可重试错误进行有限重试，ensure 重试始终复用操作编号。"""
        for attempt in range(self.attempts):
            try:
                return await self.rpc.rpc(subject, **fields)
            except ProvisioningRpcError as exc:
                if not exc.retryable or attempt + 1 == self.attempts:
                    raise
                await asyncio.sleep(min(0.2 * (2**attempt), 2))

    async def ensure(self, member):
        """每条成功立即持久记录，再进行整批映射和资料读取。"""
        discord_id = member["discord_user_id"]
        operation = operation_id(discord_id)
        async with self.semaphore:
            result = await self.request(
                "rpc.identity.v1.ensure_discord",
                discord_user_id=discord_id,
                profile=member["profile"],
                operation_id=operation,
            )
            user_id = identity_id(result.get("user_id"))
            if result.get("discord_user_id") != discord_id or not isinstance(
                result.get("created"), bool
            ):
                raise ValueError("INVALID_ENSURE_RESPONSE")
            self.journal.record_ensured(discord_id, user_id, operation)
            return user_id

    async def batch(self, members, run_id):
        """先批量查询已有绑定，只给缺失主体预建；资料始终取 Fluxer 原生值。"""
        requested = {member["discord_user_id"]: member for member in members}
        initial = await self.request(
            "rpc.identity.v1.lookup_discord", discord_user_ids=list(requested)
        )
        bindings = self.bindings(initial, requested)
        missing = [
            member
            for discord_id, member in requested.items()
            if discord_id not in bindings
        ]
        results = await asyncio.gather(
            *(self.ensure(member) for member in missing), return_exceptions=True
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise failures[0]
        # 创建回复不包含绑定版本，统一批量复验，不能编造版本号。
        confirmed = self.bindings(
            await self.request(
                "rpc.identity.v1.lookup_discord", discord_user_ids=list(requested)
            ),
            requested,
        )
        if set(confirmed) != set(requested):
            raise ValueError("MISSING_AUTHORITATIVE_BINDING")
        expected = {
            member["discord_user_id"]: user_id
            for member, user_id in zip(missing, results)
        }
        expected.update({key: value["user_id"] for key, value in bindings.items()})
        for discord_id, binding in confirmed.items():
            if expected[discord_id] != binding["user_id"]:
                raise ValueError("IDENTITY_CONFLICT")
            self.journal.record_ensured(
                discord_id, binding["user_id"], operation_id(discord_id)
            )
        user_ids = [binding["user_id"] for binding in confirmed.values()]
        response = await self.request("rpc.identity.v1.profiles", user_ids=user_ids)
        profiles = {}
        if not isinstance(response.get("profiles"), list):
            raise ValueError("INVALID_AUTHORITATIVE_PROFILE")
        for profile in response["profiles"]:
            if not isinstance(profile, dict):
                raise ValueError("INVALID_AUTHORITATIVE_PROFILE")
            user_id = identity_id(profile.get("user_id"))
            if (
                user_id not in user_ids
                or user_id in profiles
                or not isinstance(profile.get("username"), str)
                or not profile["username"]
            ):
                raise ValueError("INVALID_AUTHORITATIVE_PROFILE")
            if any(
                profile.get(key) is not None and not isinstance(profile[key], str)
                for key in ("display_name", "avatar_url")
            ):
                raise ValueError("INVALID_AUTHORITATIVE_PROFILE")
            profiles[user_id] = profile
        if set(profiles) != set(user_ids):
            raise ValueError("MISSING_AUTHORITATIVE_PROFILE")
        for discord_id, binding in confirmed.items():
            profile = profiles[binding["user_id"]]
            record = {
                **binding,
                "username": profile["username"],
                "display_name": profile.get("display_name"),
                "avatar_url": profile.get("avatar_url"),
            }
            self.journal.record_complete(record, operation_id(discord_id), run_id)
        return len(confirmed)

    def bindings(self, response, requested):
        """严格核对批量回复的主体、唯一性与正数版本，碰撞时拒绝输出。"""
        bindings = {}
        seen_users = set()
        if not isinstance(response.get("bindings"), list):
            raise ValueError("INVALID_AUTHORITATIVE_BINDING")
        for value in response["bindings"]:
            if not isinstance(value, dict):
                raise ValueError("INVALID_AUTHORITATIVE_BINDING")
            discord_id = identity_id(value.get("discord_user_id"))
            user_id = identity_id(value.get("user_id"))
            version = value.get("version")
            if (
                discord_id not in requested
                or discord_id in bindings
                or user_id in seen_users
                or type(version) is not int
                or version < 1
            ):
                raise ValueError("INVALID_AUTHORITATIVE_BINDING")
            bindings[discord_id] = {
                "discord_user_id": discord_id,
                "user_id": user_id,
                "version": version,
            }
            seen_users.add(user_id)
        return bindings

    async def run(self, members, output, batch_size=100):
        """整批队列限制为一百条，失败时导出本轮完整成功记录并返回非零。"""
        if not 1 <= batch_size <= 100:
            raise ValueError("INVALID_BATCH_SIZE")
        run_id = uuid.uuid4().hex
        batch = []
        errors = []
        completed = 0
        for member in members:
            batch.append(member)
            if len(batch) == batch_size:
                try:
                    completed += await self.batch(batch, run_id)
                except (ProvisioningRpcError, ValueError) as exc:
                    errors.append(
                        exc.code if isinstance(exc, ProvisioningRpcError) else str(exc)
                    )
                batch.clear()
        if batch:
            try:
                completed += await self.batch(batch, run_id)
            except (ProvisioningRpcError, ValueError) as exc:
                errors.append(
                    exc.code if isinstance(exc, ProvisioningRpcError) else str(exc)
                )
        exported = self.journal.export(output, run_id)
        return {
            "completed": completed,
            "exported": exported,
            "failed_batches": len(errors),
            "error_codes": sorted(set(errors)),
            "ready": not errors,
        }
