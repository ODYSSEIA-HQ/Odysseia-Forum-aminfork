"""显式临时部署配置的真实 NATS ACL 与 JetStream 验证。"""

import asyncio
import json
import os
from pathlib import Path

import nats
import pytest
from nats.errors import Error as NatsError


@pytest.mark.asyncio
async def test_actual_deployment_acls_and_forum_pull():
    """依赖已由实际部署脚本建立的临时流，不创建生产资源。"""
    config = os.environ.get("FLUXER_DEPLOY_NATS_CONFIG")
    if not config:
        pytest.skip("需要显式临时部署 broker 元数据")
    metadata = json.loads(Path(config).read_text(encoding="utf-8-sig"))
    endpoint = f"nats://127.0.0.1:{metadata['port']}"
    errors = []

    async def on_error(error):
        errors.append(str(error))

    with pytest.raises(NatsError, match="Authorization Violation"):
        await nats.connect(
            endpoint,
            allow_reconnect=False,
            max_reconnect_attempts=0,
            connect_timeout=1,
            error_cb=on_error,
        )
    passwords = metadata["passwords"]
    forum = await nats.connect(
        endpoint,
        user="forum-service",
        password=passwords["FORUM"],
        inbox_prefix=b"_INBOX.forum",
        error_cb=on_error,
    )
    bot = await nats.connect(
        endpoint,
        user="discord-sync",
        password=passwords["BOT"],
        inbox_prefix=b"_INBOX.bot",
        error_cb=on_error,
    )
    integration = await nats.connect(
        endpoint,
        user="fluxer-integration",
        password=passwords["SERVICE"],
        inbox_prefix=b"_INBOX.fluxer",
        error_cb=on_error,
    )
    admin = await nats.connect(
        endpoint,
        user="integration-admin",
        password=passwords["ADMIN"],
        inbox_prefix=b"_INBOX.admin",
        error_cb=on_error,
    )
    worker = await nats.connect(
        endpoint,
        user="fluxer-worker",
        password=passwords["WORKER"],
        inbox_prefix=b"_INBOX.worker",
        error_cb=on_error,
    )
    try:

        async def resolve(message):
            await message.respond(b'{"ok":true,"data":{"user_id":"200"}}')

        await integration.subscribe("rpc.auth.v1.session.resolve", cb=resolve)
        await integration.flush()
        result = await forum.request(
            "rpc.auth.v1.session.resolve",
            b'{"version":1,"request_id":"acl-test","token":"flx_test"}',
            timeout=2,
        )
        assert json.loads(result.data)["data"]["user_id"] == "200"

        async def forum_health(message):
            await message.respond(
                b'{"ok":true,"data":{"service":"forum","ready":true}}'
            )

        await forum.subscribe("rpc.forum.v1.service_info", cb=forum_health)
        await forum.flush()
        health = await bot.request(
            "rpc.forum.v1.service_info",
            b'{"version":1,"request_id":"bot_health_test_1"}',
            timeout=2,
        )
        assert json.loads(health.data)["data"]["ready"] is True
        received = asyncio.Event()

        async def worker_event(message):
            received.set()

        await forum.subscribe("events.auth.v1.user_invalidated", cb=worker_event)
        await forum.flush()
        await worker.publish(
            "events.auth.v1.user_invalidated",
            b'{"event_id":"worker-test","user_id":"200"}',
        )
        await worker.flush()
        await asyncio.wait_for(received.wait(), timeout=2)
        errors.clear()
        await worker.subscribe("rpc.auth.v1.session.resolve")
        await worker.flush()
        await forum.publish("rpc.identity.v1.ensure_discord", b"{}")
        await bot.publish("rpc.auth.v1.external.exchange", b"{}")
        await bot.subscribe("events.auth.v1.session_invalidated")
        await forum.publish("$JS.API.STREAM.CREATE.FORBIDDEN", b"{}")
        await forum.flush()
        await bot.flush()
        await asyncio.sleep(0.1)
        assert any(
            "rpc.identity.v1.ensure_discord" in item and "publish" in item.lower()
            for item in errors
        )
        assert any(
            "rpc.auth.v1.external.exchange" in item and "publish" in item.lower()
            for item in errors
        )
        assert any(
            "events.auth.v1.session_invalidated" in item
            and "subscription" in item.lower()
            for item in errors
        )
        assert any(
            "$js.api.stream.create.forbidden" in item.lower() for item in errors
        ), errors
        assert any(
            "rpc.auth.v1.session.resolve" in item and "subscription" in item.lower()
            for item in errors
        )
        # 真正使用预建消费者的拉取与 ACK 权限，不请求创建流或消费者。
        js_admin = admin.jetstream()
        stream = await js_admin.stream_info("ODYSSEIA_CONTENT")
        assert stream.config.name == "ODYSSEIA_CONTENT"
        await js_admin.publish(
            "events.content.v1.thread_created", b'{"test":"isolated-content"}'
        )
        pull = await forum.jetstream().pull_subscribe(
            "events.content.v1.>", durable="forum-index", stream="ODYSSEIA_CONTENT"
        )
        messages = await pull.fetch(batch=1, timeout=2)
        assert json.loads(messages[0].data)["test"] == "isolated-content"
        await messages[0].ack()
        await forum.flush()
    finally:
        await forum.close()
        await bot.close()
        await integration.close()
        await admin.close()
        await worker.close()
