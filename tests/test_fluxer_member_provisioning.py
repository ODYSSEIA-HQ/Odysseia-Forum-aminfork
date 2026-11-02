"""离线正式成员工具的 RPC、断点和文件故障验证，不连接任何服务。"""

import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from identity.member_provisioner import MemberProvisioner, operation_id
from identity.provisioning_journal import ProvisioningJournal
from identity.provisioning_rpc_error import ProvisioningRpcError


@pytest.fixture
def tool():
    """直接加载 Windows CLI，测试时替换网络入口。"""
    path = Path(__file__).parents[1] / "scripts/fluxer_prebuild_members.py"
    spec = importlib.util.spec_from_file_location("member_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_rpc():
    """模拟权威服务与超时后的同操作恢复，不实现真实建号。"""
    bindings = {}
    calls = []
    active = [0]
    maximum = [0]
    timeout_once = set()
    profile_failure = [False]

    async def rpc(subject, **fields):
        calls.append((subject, fields))
        if subject.endswith("lookup_discord"):
            assert 1 <= len(fields["discord_user_ids"]) <= 100
            return {
                "bindings": [
                    bindings[value]
                    for value in fields["discord_user_ids"]
                    if value in bindings
                ]
            }
        if subject.endswith("profiles"):
            assert 1 <= len(fields["user_ids"]) <= 100
            if profile_failure[0]:
                raise ProvisioningRpcError("UNAVAILABLE", True)
            return {
                "profiles": [
                    {
                        "user_id": value,
                        "username": "native_" + value,
                        "display_name": "native display",
                        "avatar_url": None,
                    }
                    for value in fields["user_ids"]
                ]
            }
        assert subject.endswith("ensure_discord")
        discord_id = fields["discord_user_id"]
        assert fields["operation_id"] == operation_id(discord_id)
        active[0] += 1
        maximum[0] = max(maximum[0], active[0])
        try:
            await asyncio.sleep(0.005)
            created = discord_id not in bindings
            bindings.setdefault(
                discord_id,
                {
                    "discord_user_id": discord_id,
                    "user_id": str(int(discord_id) + 1000),
                    "version": 7,
                },
            )
            if discord_id in timeout_once:
                timeout_once.remove(discord_id)
                raise ProvisioningRpcError("RPC_UNAVAILABLE", True)
            return {**bindings[discord_id], "created": created}
        finally:
            active[0] -= 1

    return SimpleNamespace(
        rpc=rpc,
        bindings=bindings,
        calls=calls,
        maximum=maximum,
        timeout_once=timeout_once,
        profile_failure=profile_failure,
    )


def source_record(discord_id):
    """资格由可信 Bot 明确断言，工具不自行查询 Discord。"""
    return {
        "discord_user_id": discord_id,
        "formal_member": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "profile": {
            "username": "discord_" + discord_id,
            "display_name": None,
            "avatar_url": None,
        },
    }


def members(count):
    """生成经过输入校验的最小业务记录。"""
    return [
        {"discord_user_id": str(value), "profile": source_record(str(value))["profile"]}
        for value in range(1, count + 1)
    ]


def test_default_dry_run_has_no_network_or_output(tool, tmp_path, monkeypatch, capsys):
    """不带 execute 只验证，不需要凭据，也不创建状态文件。"""
    source = tmp_path / "verified.jsonl"
    output = tmp_path / "authority.jsonl"
    source.write_text(json.dumps(source_record("1")) + "\n", encoding="utf-8")

    async def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not connect")

    monkeypatch.setattr(tool.ProvisioningRpcClient, "connect", forbidden)
    monkeypatch.setattr(
        tool.sys, "argv", ["tool", "--input", str(source), "--output", str(output)]
    )
    assert tool.main() == 0
    assert json.loads(capsys.readouterr().out)["rpc_calls"] == 0
    assert not output.exists()
    assert not output.with_suffix(".jsonl.state.sqlite3").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(formal_member=False),
        lambda value: value.update(verified_at="2020-01-01T00:00:00Z"),
        lambda value: value.update(discord_user_id="01"),
        lambda value: value["profile"].update(
            avatar_url="https://cdn.discordapp.com/avatars/999/a.png"
        ),
    ],
)
def test_input_rejects_unqualified_stale_or_mismatched_identity(
    tool, tmp_path, mutation
):
    """未经资格断言、过期和跨用户头像不能触发预建。"""
    record = source_record("1")
    mutation(record)
    source = tmp_path / "bad.jsonl"
    source.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="INVALID_MEMBER_LINE_1"):
        list(tool.verified_members(source))


@pytest.mark.asyncio
async def test_bounded_ensure_retries_and_exports_authoritative_profiles(
    fake_rpc, tmp_path
):
    """只预建缺失成员，资料来自原生批量查询，操作编号在超时重试中不变。"""
    fake_rpc.bindings["1"] = {"discord_user_id": "1", "user_id": "9000", "version": 3}
    fake_rpc.timeout_once.add("2")
    journal = ProvisioningJournal(tmp_path / "state.sqlite3")
    output = tmp_path / "authoritative.jsonl"
    try:
        result = await MemberProvisioner(fake_rpc, journal, concurrency=2).run(
            members(6), output, batch_size=3
        )
        assert result["ready"] is True
        assert result["exported"] == 6
        assert fake_rpc.maximum[0] <= 2
        ensure = [
            fields
            for subject, fields in fake_rpc.calls
            if subject.endswith("ensure_discord")
        ]
        assert all(fields["discord_user_id"] != "1" for fields in ensure)
        assert (
            len([fields for fields in ensure if fields["discord_user_id"] == "2"]) == 2
        )
        records = [
            json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
        ]
        assert records[0]["user_id"] == "9000"
        assert records[0]["username"] == "native_9000"
        assert records[0]["version"] == 3
        assert all(
            set(record)
            == {
                "discord_user_id",
                "user_id",
                "version",
                "username",
                "display_name",
                "avatar_url",
            }
            for record in records
        )
        assert (
            len([1 for subject, _ in fake_rpc.calls if subject.endswith("profiles")])
            == 2
        )
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_restart_after_profile_failure_does_not_ensure_again(fake_rpc, tmp_path):
    """创建成功而资料查询失败时，SQLite 成功日志保留，重跑走权威批量读取。"""
    state = tmp_path / "state.sqlite3"
    output = tmp_path / "authority.jsonl"
    journal = ProvisioningJournal(state)
    fake_rpc.profile_failure[0] = True
    try:
        result = await MemberProvisioner(fake_rpc, journal, attempts=1).run(
            members(2), output
        )
        assert result["ready"] is False
        assert (
            journal.connection.execute("SELECT count(*) FROM provisioned").fetchone()[0]
            == 2
        )
    finally:
        journal.close()
    before = len(
        [1 for subject, _ in fake_rpc.calls if subject.endswith("ensure_discord")]
    )
    fake_rpc.profile_failure[0] = False
    journal = ProvisioningJournal(state)
    try:
        result = await MemberProvisioner(fake_rpc, journal, attempts=1).run(
            members(2), output
        )
        assert result["ready"] is True
        assert (
            len(
                [
                    1
                    for subject, _ in fake_rpc.calls
                    if subject.endswith("ensure_discord")
                ]
            )
            == before
        )
    finally:
        journal.close()


def test_atomic_export_preserves_prior_file_on_replace_failure(tmp_path, monkeypatch):
    """Windows 文件被占用时旧输出保持完整，成功日志可在之后重新导出。"""
    from identity import provisioning_journal

    journal = ProvisioningJournal(tmp_path / "state.sqlite3")
    output = tmp_path / "authority.jsonl"
    output.write_text("prior snapshot\n", encoding="utf-8")
    record = {
        "discord_user_id": "1",
        "user_id": "2",
        "version": 1,
        "username": "native",
        "display_name": None,
        "avatar_url": None,
    }
    journal.record_complete(record, operation_id("1"), "run")

    def fail_replace(*args):
        raise PermissionError("locked destination")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(provisioning_journal.os, "replace", fail_replace)
            with pytest.raises(PermissionError):
                journal.export(output, "run")
        assert output.read_text(encoding="utf-8") == "prior snapshot\n"
        assert not list(tmp_path.glob(".authority.jsonl.*.tmp"))
        assert journal.export(output, "run") == 1
        assert json.loads(output.read_text(encoding="utf-8"))["user_id"] == "2"
    finally:
        journal.close()


def test_journal_lock_and_identity_collision_fail_closed(tmp_path):
    """同一状态日志不可并发运行，已记录主体不能被其他权威 ID 覆盖。"""
    path = tmp_path / "state.sqlite3"
    journal = ProvisioningJournal(path)
    try:
        with pytest.raises(OSError):
            ProvisioningJournal(path)
        journal.record_ensured("1", "2", operation_id("1"))
        with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
            journal.record_ensured("1", "3", operation_id("1"))
    finally:
        journal.close()
    recovered = ProvisioningJournal(path)
    recovered.close()


@pytest.mark.asyncio
async def test_execute_validates_entire_snapshot_before_connect(
    tool, tmp_path, monkeypatch
):
    """文件后半部损坏也必须在任何 RPC 之前被发现。"""
    source = tmp_path / "invalid.jsonl"
    source.write_text(json.dumps(source_record("1")) + "\n{broken", encoding="utf-8")
    monkeypatch.setenv("INTEGRATION_NATS_URL", "nats://invalid.example")
    monkeypatch.setenv("INTEGRATION_BOT_PASSWORD", "must-not-print")

    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid source must not connect")

    monkeypatch.setattr(tool.ProvisioningRpcClient, "connect", forbidden)
    args = SimpleNamespace(
        input=source,
        output=tmp_path / "out.jsonl",
        state=None,
        max_age_hours=24,
        concurrency=2,
        attempts=1,
        batch_size=100,
    )
    with pytest.raises(ValueError, match="INVALID_MEMBER_LINE_2"):
        await tool.execute(args)
    assert not args.output.exists()
