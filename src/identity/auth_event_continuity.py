"""持久消费者连续性检查，不以 Core 连接健康代替事件历史完整。"""

import asyncio
import json


class AuthEventContinuity:
    """在接收认证缓存前检测流重建、消费者回退及保留窗口缺口。"""

    def __init__(self, redis, prefix="forum:fluxer:auth:v1"):
        """游标与认证缓存共享 Redis，缺少基线时强制推进缓存代次。"""
        self.redis = redis
        self.prefix = prefix

    async def check(self, js, stream, durable, *, startup=False):
        """只有 INFO 与游标持久化均成功才确认连续性，失败由调用方关闭缓存服务。"""
        key = self.prefix + ":continuity:" + stream + ":" + durable
        for _attempt in range(3):
            previous_raw = await self.redis.get(key)
            if isinstance(previous_raw, bytes):
                previous_raw = previous_raw.decode("utf-8")
            consumer, source = await asyncio.gather(
                js.consumer_info(stream, durable), js.stream_info(stream)
            )
            if not consumer.created or not source.created:
                raise RuntimeError("认证事件流缺少可验证创建代次")
            snapshot = {
                "consumer_created": str(consumer.created),
                "stream_created": str(source.created),
                "ack": int(consumer.ack_floor.stream_seq),
                "first": int(source.state.first_seq),
                "last": int(source.state.last_seq),
            }
            if min(snapshot["ack"], snapshot["first"], snapshot["last"]) < 0:
                raise RuntimeError("认证事件流游标无效")
            try:
                previous = json.loads(previous_raw) if previous_raw else None
            except (TypeError, ValueError):
                previous = None
            reset = startup or not isinstance(previous, dict)
            if not reset:
                try:
                    reset = (
                        snapshot["consumer_created"] != previous["consumer_created"]
                        or snapshot["stream_created"] != previous["stream_created"]
                        or snapshot["ack"] < previous["ack"]
                        or snapshot["last"] < previous["last"]
                        or (
                            snapshot["first"] > snapshot["ack"] + 1
                            and (
                                snapshot["first"] > previous["first"]
                                or previous["first"] <= previous["ack"] + 1
                            )
                        )
                    )
                except (KeyError, TypeError):
                    reset = True
            # CAS 失败说明另一进程已推进基线，重新取 INFO 避免把迟到观察误判为回退。
            applied = await self.redis.eval(
                "local old=redis.call('GET',KEYS[1]); if (old or '')~=ARGV[1] then return 0 end; "
                "if ARGV[3]=='1' then redis.call('INCR',KEYS[2]) end; "
                "redis.call('SET',KEYS[1],ARGV[2]); return 1",
                2,
                key,
                self.prefix + ":generation",
                previous_raw or "",
                json.dumps(snapshot, separators=(",", ":")),
                "1" if reset else "0",
            )
            if applied:
                return bool(reset)
        raise RuntimeError("认证事件连续性游标竞争，需重新验证")
