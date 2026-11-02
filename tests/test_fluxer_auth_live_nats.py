"""显式启用的真实 NATS 协议、ACL、失效与重连测试。"""

import asyncio
import json
import os
from pathlib import Path

import nats
import pytest
from fastapi import HTTPException
from test_fluxer_auth_security import auth_service as auth_service


@pytest.mark.asyncio
async def test_live_nats_resolve_invalidation_and_reconnect(monkeypatch, auth_service):  # noqa: F811
    """只连接指定临时 ACL broker，不停止或修改服务器。"""
    config_path = os.environ.get("FLUXER_LIVE_NATS_CONFIG")
    if not config_path:
        pytest.skip("需要显式临时 NATS 元数据 FLUXER_LIVE_NATS_CONFIG")
    metadata = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    endpoint = f"nats://127.0.0.1:{metadata['port']}"
    monkeypatch.setenv("INTEGRATION_NATS_URL", endpoint)
    monkeypatch.setenv("INTEGRATION_NATS_USER", "forum")
    monkeypatch.setenv("INTEGRATION_NATS_PASSWORD", metadata["forum_password"])
    responder = await nats.connect(
        endpoint,
        user="fluxer",
        password=metadata["fluxer_password"],
        inbox_prefix=b"_INBOX.fluxer",
    )
    calls = []

    async def resolve(message):
        request = json.loads(message.data)
        assert request["version"] == 1
        assert request["request_id"]
        calls.append(request["request_id"])
        await message.respond(
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "user_id": "200",
                        "session_id_hash": "opaque_hash",
                        "discord_user_id": "100",
                        "username": "alice",
                    },
                }
            ).encode()
        )

    await responder.subscribe("rpc.auth.v1.session.resolve", cb=resolve)
    await responder.flush()
    service = auth_service.service
    service.connection = None
    service.connector = None
    service.ready = False
    try:
        await service.start()
        assert (await service.resolve("flx_live_test"))["user_id"] == "200"
        await service.resolve("flx_live_test")
        assert len(calls) == 1
        await responder.publish(
            "events.auth.v1.session_invalidated",
            b'{"event_id":"test","session_id_hash":"opaque_hash"}',
        )
        await responder.flush()
        for _ in range(100):
            if auth_service.sync.get(service.PREFIX + ":event_sequence") == "1":
                break
            await asyncio.sleep(0.01)
        await service.resolve("flx_live_test")
        assert len(calls) == 2
        previous_generation = auth_service.sync.get(service.PREFIX + ":generation")
        # 仅断开本测试客户端的传输，broker 与其他代理的连接保持运行。
        await service.connection._process_op_err(
            ConnectionResetError("isolated client disconnect")
        )
        await asyncio.sleep(0.01)
        if not service.ready:
            with pytest.raises(HTTPException) as error:
                await service.resolve("flx_live_test")
            assert error.value.status_code == 503
        for _ in range(500):
            if (
                service.ready
                and auth_service.sync.get(service.PREFIX + ":generation")
                != previous_generation
            ):
                break
            await asyncio.sleep(0.01)
        assert service.ready
        assert (
            auth_service.sync.get(service.PREFIX + ":generation") != previous_generation
        )
        await service.resolve("flx_live_test")
        assert len(calls) == 3
        health = await responder.request(
            "rpc.forum.v1.service_info",
            b'{"version":1,"request_id":"health-test"}',
            timeout=3,
        )
        assert json.loads(health.data)["data"]["ready"] is True
    finally:
        await service.close()
        await responder.close()
