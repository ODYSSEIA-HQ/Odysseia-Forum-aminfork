"""直接用 Fluxer 生产 Zod schema 验证 Forum worker 实际序列化的 RPC。"""

import json
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from identity.fluxer_auth_service import FluxerAuthService
from work.work_task_worker import WorkTaskWorker

FLUXER_ROOT = Path(
    os.environ.get(
        "FLUXER_CONTRACT_ROOT", str(Path(__file__).resolve().parents[2] / "fluxer")
    )
)
SCHEMA_PATH = FLUXER_ROOT / "fluxer_api/src/integration/IntegrationSchemas.ts"
LOADER_PATH = FLUXER_ROOT / "fluxer_api/node_modules/tsx/dist/loader.mjs"
NODE_PATH = shutil.which("node")
pytestmark = pytest.mark.skipif(
    not NODE_PATH or not SCHEMA_PATH.is_file() or not LOADER_PATH.is_file(),
    reason="跨项目合同需要相邻 Fluxer checkout、Node 和现有 tsx 依赖，不访问部署环境",
)


@pytest.fixture
def native_schema():
    """通过标准输入传递无凭据测试请求，校验目标为实际生产 schema 而非 Python 副本。"""
    script = """
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const {IntegrationSchemas} = await import(input.schema);
const failures = input.requests.flatMap(({subject, request}) => {
  const schema = IntegrationSchemas[subject];
  if (!schema) return [{subject, error: 'unknown production subject'}];
  const parsed = schema.safeParse(request);
  return parsed.success ? [] : [{subject, issues: parsed.error.issues}];
});
process.stdout.write(JSON.stringify(failures));
process.exitCode = failures.length ? 1 : 0;
"""

    def validate(requests):
        """不启动 Cassandra、NATS 或应用，只加载纯 DTO 模块并返回校验错误。"""
        environment = dict(os.environ)
        environment["TSX_TSCONFIG_PATH"] = str(FLUXER_ROOT / "fluxer_api/tsconfig.json")
        result = subprocess.run(
            [
                NODE_PATH,
                "--import",
                LOADER_PATH.as_uri(),
                "--input-type=module",
                "-e",
                script,
            ],
            input=json.dumps({"schema": SCHEMA_PATH.as_uri(), "requests": requests}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=FLUXER_ROOT / "fluxer_api",
            env=environment,
            timeout=20,
            check=False,
        )
        assert not result.stderr.strip(), result.stderr
        return result.returncode, json.loads(result.stdout)

    return validate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload", "subjects"),
    [
        (
            "create_main",
            {
                "guild_id": "201",
                "channel_id": "301",
                "title": "作品",
                "body": "原生正文",
            },
            ["rpc.content.v1.thread.create_service"],
        ),
        (
            "create_child",
            {
                "guild_id": "201",
                "channel_id": "301",
                "parent_thread_id": "401",
                "title": "子卡贴",
                "body": "子帖正文",
            },
            ["rpc.content.v1.thread.create_service"],
        ),
        (
            "move",
            {
                "platform": "fluxer",
                "guild_id": "201",
                "thread_id": "401",
                "target_channel_id": "302",
            },
            ["rpc.content.v1.thread.move_service"],
        ),
        (
            "policy",
            {
                "guild_id": "201",
                "thread_id": "401",
                "source": "forum_work_lock",
                "active": True,
            },
            ["rpc.content.v1.thread.policy_service"],
        ),
        (
            "policy",
            {
                "guild_id": "201",
                "thread_id": "401",
                "source": "forum_work_mute",
                "active": True,
                "target_user_id": "501",
                "expires_at": "2027-01-01T00:00:00Z",
            },
            ["rpc.content.v1.thread.policy_service"],
        ),
        (
            "edit_thread",
            {
                "guild_id": "201",
                "thread_id": "401",
                "title": "新标题",
                "archived": True,
                "body": "编辑正文",
            },
            [
                "rpc.content.v1.thread.update_service",
                "rpc.content.v1.starter.edit_service",
            ],
        ),
        (
            "create_main",
            {
                "guild_id": "201",
                "channel_id": "301",
                "title": "纯图片作品",
                "body": "",
                "attachments": [
                    {
                        "id": 0,
                        "filename": "cover.png",
                        "file_size": 100,
                        "content_type": "image/png",
                        "upload_filename": "owned-upload",
                    }
                ],
            },
            ["rpc.content.v1.thread.create_service"],
        ),
        (
            "edit_thread",
            {
                "guild_id": "201",
                "thread_id": "401",
                "attachments": [
                    {
                        "id": 0,
                        "filename": "new-cover.png",
                        "file_size": 100,
                        "content_type": "image/png",
                        "upload_filename": "owned-edit-upload",
                    },
                    {"id": "987654321098765432"},
                ],
            },
            [
                "rpc.content.v1.starter.edit_service",
                "rpc.content.v1.thread.read_service",
            ],
        ),
        (
            "edit_thread",
            {"guild_id": "201", "thread_id": "401", "attachments": []},
            [
                "rpc.content.v1.starter.edit_service",
                "rpc.content.v1.thread.read_service",
            ],
        ),
        (
            "managed_rebind",
            {
                "user_id": "601",
                "discord_user_id": "701",
                "reason": "管理批准换绑",
                "profile": {
                    "username": "member",
                    "display_name": None,
                    "avatar_url": None,
                },
            },
            ["rpc.identity.v1.rebind_service"],
        ),
    ],
)
async def test_actual_worker_requests_match_native_schema(
    kind, payload, subjects, native_schema, monkeypatch
):
    """捕获真实 worker 到真实 RPC envelope 的所有字段，并逐项交给生产严格 DTO。"""
    from work.work_moderation_service import WorkModerationService

    requests = []

    async def request(subject, data, timeout):
        """只替代传输层，保留生产 Envelope 和 worker 的实际分支。"""
        requests.append({"subject": subject, "request": json.loads(data)})
        result = (
            {"message": {"content": "原生最新正文", "attachments": []}}
            if subject == "rpc.content.v1.thread.read_service"
            else {}
        )
        return SimpleNamespace(data=json.dumps({"ok": True, "data": result}).encode())

    @asynccontextmanager
    async def sessions():
        """创建子帖只替代资格查询，真实 PG 约束另由作品业务集成测试覆盖。"""
        yield SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(id=77)),
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)),
        )

    monkeypatch.setattr(WorkModerationService, "require_speech", AsyncMock())
    auth = FluxerAuthService(redis=None)
    auth.ready = True
    auth.connection = SimpleNamespace(request=request)
    worker = WorkTaskWorker(sessions, rpc=auth.rpc)
    await worker.execute(123, 77, kind, {"actor_id": "101", **payload})
    assert [entry["subject"] for entry in requests] == subjects
    for entry in requests:
        assert entry["request"]["actor_user_id"] == "101"
        assert entry["request"]["operation_id"].startswith("forum_work_operation_123")
        assert entry["request"]["version"] == 1
        if entry["subject"].startswith("rpc.content."):
            assert entry["request"]["guild_id"] == "201"
    if "attachments" in payload:
        mutation = next(
            entry["request"]
            for entry in requests
            if entry["subject"]
            in (
                "rpc.content.v1.thread.create_service",
                "rpc.content.v1.starter.edit_service",
            )
        )
        assert mutation["attachments"] == payload["attachments"]
        assert "content" in mutation if "body" in payload else "content" not in mutation
    status, failures = native_schema(requests)
    assert status == 0, failures


def test_production_schema_rejects_missing_guild_and_extra_service_fields(
    native_schema,
):
    """防止测试夹具退化为宽松校验，缺字段和越权字段均由实际 schema 拒绝。"""
    base = {
        "version": 1,
        "request_id": "contract_request_123",
        "operation_id": "forum_work_operation_123",
        "actor_user_id": "101",
        "thread_id": "401",
        "content": "正文",
    }
    status, failures = native_schema(
        [{"subject": "rpc.content.v1.starter.edit_service", "request": base}]
    )
    assert status != 0
    assert any(issue.get("path") == ["guild_id"] for issue in failures[0]["issues"])
    status, failures = native_schema(
        [
            {
                "subject": "rpc.content.v1.starter.edit_service",
                "request": {**base, "guild_id": "201", "bypass_permissions": True},
            }
        ]
    )
    assert status != 0
    assert any(
        issue.get("code") == "unrecognized_keys" for issue in failures[0]["issues"]
    )
