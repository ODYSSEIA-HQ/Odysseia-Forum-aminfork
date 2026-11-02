"""持久 NATS 认证连接与有界、选择性失效的哈希会话缓存。"""

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid

from fastapi import HTTPException
from identity.integration_rpc_error import IntegrationRpcError


class FluxerAuthService:
    """会话与用户 epoch 只影响对应主体，全局代次仅用于启动及重连。"""

    PREFIX = "forum:fluxer:auth:v1"

    def __init__(self, redis, connector=None):
        """初始化可复用的认证连接、恢复状态与请求并发边界。"""
        self.redis = redis
        self.connector = connector
        self.connection = None
        self.ready = False
        self.start_lock = asyncio.Lock()
        self.misses = asyncio.Semaphore(64)
        self.reset_task = None
        self.pending_bindings = {}
        self.closing = False

    async def start(self):
        """订阅失效事件、轮换断线代次并恢复投影后才开放认证。"""
        if self.reset_task and self.reset_task is not asyncio.current_task():
            await self.reset_task
        async with self.start_lock:
            if self.connection is not None and self.ready:
                return
            if self.connection is not None:
                raise HTTPException(503, "Fluxer 认证连接恢复中")
            self.closing = False
            try:
                options = dict(
                    servers=os.environ.get(
                        "INTEGRATION_NATS_URL", "nats://127.0.0.1:4222"
                    ).split(","),
                    user=os.environ.get("INTEGRATION_NATS_USER"),
                    password=os.environ.get("INTEGRATION_NATS_PASSWORD"),
                    inbox_prefix=b"_INBOX.forum",
                    connect_timeout=3,
                    max_reconnect_attempts=-1,
                    disconnected_cb=self.disconnected,
                    reconnected_cb=self.reconnected,
                )
                if self.connector:
                    self.connection = await asyncio.wait_for(
                        self.connector(**options), timeout=5
                    )
                else:
                    from nats.aio.client import Client

                    self.connection = Client()
                    await asyncio.wait_for(
                        self.connection.connect(**options), timeout=5
                    )
                for subject in (
                    "events.auth.v1.session_invalidated",
                    "events.auth.v1.user_invalidated",
                    "events.identity.v1.binding_changed",
                ):
                    await self.connection.subscribe(subject, cb=self.invalidated)
                await self.connection.subscribe(
                    "rpc.forum.v1.service_info", cb=self.service_info
                )
                await self.connection.flush(timeout=3)
                await self.redis.incr(self.PREFIX + ":generation")
                for event in list(self.pending_bindings.values()):
                    await self.apply_binding(event)
                    if self.pending_bindings.get(event["discord_user_id"]) is event:
                        self.pending_bindings.pop(event["discord_user_id"], None)
                self.ready = True
            except Exception as exc:
                self.ready = False
                connection, self.connection = self.connection, None
                if connection:
                    await connection.close()
                raise HTTPException(503, "Fluxer 认证服务暂不可用") from exc

    async def disconnected(self):
        """断线期间不接受可能漏掉失效通知的缓存。"""
        self.ready = False

    async def reconnected(self):
        """重连才轮换全局代次，恢复未完成投影失败时重建连接。"""
        try:
            await self.redis.incr(self.PREFIX + ":generation")
            for event in list(self.pending_bindings.values()):
                await self.apply_binding(event)
                if self.pending_bindings.get(event["discord_user_id"]) is event:
                    self.pending_bindings.pop(event["discord_user_id"], None)
            self.ready = True
        except Exception:
            await self.request_reset()

    async def request_reset(self):
        """失败后排队关闭旧连接，下次请求可重新连接而不是永久锁在 503。"""
        self.ready = False
        if self.closing or (self.reset_task and not self.reset_task.done()):
            return
        self.reset_task = asyncio.create_task(self.reset_connection())

    async def reset_connection(self):
        """在订阅回调之外关闭连接，避免等待自身回调造成死锁。"""
        async with self.start_lock:
            self.ready = False
            connection, self.connection = self.connection, None
            if connection:
                await connection.close()

    async def invalidated(self, message):
        """原子推进目标 epoch，事件序号只用作未知用户的在途回填屏障。"""
        try:
            event = json.loads(message.data)
            subject = message.subject
            user_id = event.get("user_id")
            session_hash = event.get("session_id_hash")
            if subject == "events.auth.v1.session_invalidated":
                if not isinstance(session_hash, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,128}", session_hash
                ):
                    raise ValueError("invalid session event")
                keys = [self.PREFIX + ":session_epoch:" + session_hash]
            elif subject in {
                "events.auth.v1.user_invalidated",
                "events.identity.v1.binding_changed",
            }:
                if not isinstance(user_id, str) or not re.fullmatch(
                    r"[1-9][0-9]{0,18}", user_id
                ):
                    raise ValueError("invalid user event")
                keys = [self.PREFIX + ":user_epoch:" + user_id]
            else:
                return
            if subject == "events.identity.v1.binding_changed":
                discord_id = event.get("discord_user_id")
                if (
                    not isinstance(discord_id, str)
                    or not re.fullmatch(r"[1-9][0-9]{0,18}", discord_id)
                    or type(event.get("version")) is not int
                    or event["version"] < 1
                ):
                    raise ValueError("invalid binding event")
                pending = self.pending_bindings.get(discord_id)
                if not pending or pending["version"] <= event["version"]:
                    self.pending_bindings[discord_id] = event
            # 每个目标保存最后事件序号；无关事件不会让其他主体缓存失效。
            await self.redis.eval(
                """local sequence=redis.call('INCR',KEYS[1]);
                for i=2,#KEYS do redis.call('SET',KEYS[i],sequence,'EX',3700) end; return sequence""",
                1 + len(keys),
                self.PREFIX + ":event_sequence",
                *keys,
            )
            if subject == "events.identity.v1.binding_changed":
                current_event = self.pending_bindings[discord_id]
                await self.apply_binding(current_event)
                if self.pending_bindings.get(discord_id) is current_event:
                    self.pending_bindings.pop(discord_id, None)
        except Exception:
            await self.request_reset()

    async def apply_binding(self, event):
        """绑定和权威用户资料在同一事务更新，失败事件保留以供重连重试。"""
        from sqlalchemy.dialects.postgresql import insert
        from models.user import User
        from shared.database import AsyncSessionFactory

        result = await self.call_rpc(
            "rpc.identity.v1.profiles", user_ids=[event["user_id"]]
        )
        profiles = result.get("profiles", [])
        if (
            len(profiles) != 1
            or profiles[0].get("user_id") != event["user_id"]
            or not isinstance(profiles[0].get("username"), str)
        ):
            raise HTTPException(503, "绑定用户资料暂不可用")
        profile = profiles[0]
        async with AsyncSessionFactory() as session:
            values = {
                "id": event["user_id"],
                "username": profile["username"],
                "display_name": profile.get("display_name"),
                "avatar_url": profile.get("avatar_url"),
            }
            user = insert(User).values(**values)
            await session.execute(
                user.on_conflict_do_update(index_elements=[User.id], set_=values)
            )
            from identity.binding_projection_service import BindingProjectionService

            await BindingProjectionService(session).apply(event)
            await session.commit()

    async def service_info(self, message):
        """只对版本化请求返回当前服务状态。"""
        try:
            request = json.loads(message.data)
            valid = request.get("version") == 1 and isinstance(
                request.get("request_id"), str
            )
            reply = (
                {
                    "ok": True,
                    "data": {"service": "forum", "version": 1, "ready": self.ready},
                }
                if valid
                else {
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "Invalid envelope",
                        "retryable": False,
                    },
                }
            )
            await message.respond(json.dumps(reply).encode())
        except Exception:
            return

    async def rpc(self, subject, **fields):
        """普通入口确保连接就绪，再发送有界版本化服务请求。"""
        await self.start()
        return await self.call_rpc(subject, **fields)

    async def call_rpc(self, subject, **fields):
        """内部投影恢复不递归取得启动锁，首次登录创建允许二十五秒。"""
        try:
            message = await self.connection.request(
                subject,
                json.dumps(
                    {"version": 1, "request_id": uuid.uuid4().hex, **fields}
                ).encode(),
                timeout=25 if subject == "rpc.auth.v1.external.exchange" else 5,
            )
            reply = json.loads(message.data)
            if reply.get("ok") is True and isinstance(reply.get("data"), dict):
                return reply["data"]
            error = reply.get("error", {})
            if (
                subject == "rpc.auth.v1.session.resolve"
                and error.get("code") in {"INVALID_TOKEN", "ACCOUNT_UNAVAILABLE"}
                and error.get("retryable") is False
            ):
                return None
            if (
                subject == "rpc.auth.v1.external.prove"
                and error.get("code") == "PROOF_NOT_AVAILABLE"
            ):
                return None
            if (
                subject.startswith("rpc.content.")
                or subject == "rpc.identity.v1.rebind_service"
            ):
                code = str(error.get("code", "RPC_UNAVAILABLE"))
                if not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
                    code = "RPC_UNAVAILABLE"
                raise IntegrationRpcError(code, error.get("retryable", True))
            raise HTTPException(503, "Fluxer 认证服务暂不可用")
        except HTTPException:
            raise
        except IntegrationRpcError:
            raise
        except Exception as exc:
            raise HTTPException(503, "Fluxer 认证服务暂不可用") from exc

    async def resolve(self, token, *, force_authority=False):
        """正缓存从请求开始最多一小时，读写同时检查目标 epoch 防止撤销竞态。"""
        started = time.time()
        if (
            not isinstance(token, str)
            or not token.startswith("flx_")
            or len(token) > 4096
        ):
            return None
        await self.start()
        generation_key = self.PREFIX + ":generation"
        sequence_key = self.PREFIX + ":event_sequence"
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            generation = str(await self.redis.get(generation_key) or "0")
            request_sequence = int(await self.redis.get(sequence_key) or 0)
            key = f"{self.PREFIX}:{generation}:{digest}"
            cached = await self.redis.eval(
                """if (redis.call('GET',KEYS[1]) or '0')~=ARGV[1] then return nil end
                local raw=redis.call('GET',KEYS[2]); if not raw then return nil end
                local snapshot=cjson.decode(raw); if snapshot.user~=cjson.null then
                    local user_epoch=tonumber(redis.call('GET',ARGV[2]..':user_epoch:'..snapshot.user.user_id) or '0');
                    local session_epoch=tonumber(redis.call('GET',ARGV[2]..':session_epoch:'..snapshot.user.session_id_hash) or '0');
                    if user_epoch~=snapshot.user_epoch or session_epoch~=snapshot.session_epoch then redis.call('DEL',KEYS[2]); return nil end
                end; return raw""",
                2,
                generation_key,
                key,
                generation,
                self.PREFIX,
            )
            if cached and not force_authority:
                snapshot = json.loads(cached)
                if self.ready and snapshot["expires_at"] > started:
                    return snapshot["user"]
            try:
                await asyncio.wait_for(self.misses.acquire(), timeout=0.1)
            except TimeoutError as exc:
                raise HTTPException(503, "Fluxer 认证请求繁忙") from exc
            try:
                user = await self.rpc("rpc.auth.v1.session.resolve", token=token)
            finally:
                self.misses.release()
            if user is not None and (
                not isinstance(user.get("user_id"), str)
                or not re.fullmatch(r"[1-9][0-9]{0,18}", user["user_id"])
                or not isinstance(user.get("session_id_hash"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user["session_id_hash"])
                or not isinstance(user.get("username"), str)
                or (
                    user.get("discord_user_id") is not None
                    and (
                        not isinstance(user["discord_user_id"], str)
                        or not re.fullmatch(
                            r"[1-9][0-9]{0,18}", user["discord_user_id"]
                        )
                    )
                )
            ):
                raise HTTPException(503, "Fluxer 返回了无效身份")
            expires_at = started + (3600 if user else 5)
            ttl = math.floor(expires_at - time.time())
            if not self.ready or ttl < 1:
                raise HTTPException(503, "Fluxer 认证结果已过期")
            accepted = await self.redis.eval(
                """if (redis.call('GET',KEYS[1]) or '0')~=ARGV[1] then return 0 end
                local snapshot=cjson.decode(ARGV[2]); snapshot.user_epoch=0; snapshot.session_epoch=0;
                if snapshot.user~=cjson.null then
                    snapshot.user_epoch=tonumber(redis.call('GET',ARGV[4]..':user_epoch:'..snapshot.user.user_id) or '0');
                    snapshot.session_epoch=tonumber(redis.call('GET',ARGV[4]..':session_epoch:'..snapshot.user.session_id_hash) or '0');
                    if snapshot.user_epoch>tonumber(ARGV[5]) or snapshot.session_epoch>tonumber(ARGV[5]) then return 0 end
                end; redis.call('SET',KEYS[2],cjson.encode(snapshot),'EX',ARGV[3]); return 1""",
                2,
                generation_key,
                key,
                generation,
                json.dumps({"user": user, "expires_at": expires_at}),
                ttl,
                self.PREFIX,
                request_sequence,
            )
            if not accepted:
                raise HTTPException(503, "认证状态已变化，请重试")
            return user
        except HTTPException:
            raise
        except Exception as exc:
            await self.request_reset()
            raise HTTPException(503, "认证缓存暂不可用") from exc

    async def close(self):
        """退出时结束恢复任务和持久连接，不留下后台订阅。"""
        self.closing = True
        self.ready = False
        if self.reset_task:
            await self.reset_task
        connection, self.connection = self.connection, None
        if connection:
            await connection.close()
