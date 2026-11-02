"""默认 dry-run；只预建同步 Bot 已验证的正式成员，不查询 Discord。"""

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from identity.member_provisioner import MemberProvisioner, identity_id
from identity.provisioning_journal import ProvisioningJournal
from identity.provisioning_rpc_client import ProvisioningRpcClient
from identity.provisioning_rpc_error import ProvisioningRpcError


def verified_members(path, max_age_hours=24):
    """流式校验可信 Bot 导出的正式资格和白名单资料字段。"""
    seen = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                discord_id = identity_id(row["discord_user_id"])
                verified_at = datetime.fromisoformat(
                    row["verified_at"].replace("Z", "+00:00")
                )
                if (
                    row.get("formal_member") is not True
                    or verified_at.tzinfo is None
                    or verified_at < cutoff
                    or verified_at > datetime.now(timezone.utc) + timedelta(seconds=30)
                ):
                    raise ValueError("INVALID_QUALIFICATION")
                if discord_id in seen:
                    raise ValueError("DUPLICATE_DISCORD_ID")
                profile = row["profile"]
                username = profile["username"]
                display_name = profile.get("display_name")
                avatar_url = profile.get("avatar_url")
                if (
                    not isinstance(username, str)
                    or not 1 <= len(username) <= 80
                    or (
                        display_name is not None
                        and (
                            not isinstance(display_name, str) or len(display_name) > 80
                        )
                    )
                ):
                    raise ValueError("INVALID_PROFILE")
                if avatar_url is not None:
                    if not isinstance(avatar_url, str) or len(avatar_url) > 512:
                        raise ValueError("INVALID_AVATAR")
                    parsed = urlsplit(avatar_url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname != "cdn.discordapp.com"
                        or parsed.username
                        or parsed.password
                        or not parsed.path.startswith(f"/avatars/{discord_id}/")
                    ):
                        raise ValueError("INVALID_AVATAR")
                seen.add(discord_id)
                yield {
                    "discord_user_id": discord_id,
                    "profile": {
                        "username": username,
                        "display_name": display_name,
                        "avatar_url": avatar_url,
                    },
                }
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise ValueError(f"INVALID_MEMBER_LINE_{number}") from exc


async def execute(args):
    """只在显式 execute 后使用独立 Bot 账号，状态与最终文件独立原子落盘。"""
    url = os.environ.get("INTEGRATION_NATS_URL")
    password = os.environ.get("INTEGRATION_BOT_PASSWORD")
    if not url or not password:
        raise ValueError("MISSING_BOT_BROKER_ENV")
    state = args.state or args.output.with_suffix(args.output.suffix + ".state.sqlite3")
    if args.input.resolve() in {args.output.resolve(), state.resolve()}:
        raise ValueError("INPUT_OUTPUT_PATH_CONFLICT")
    # 复制后先完整校验快照，任何坏行都在真实建号之前失败。
    with tempfile.TemporaryDirectory(prefix="forum-members-") as temporary:
        snapshot = Path(temporary) / "verified-members.jsonl"
        shutil.copyfile(args.input, snapshot)
        total = sum(1 for _ in verified_members(snapshot, args.max_age_hours))
        journal = ProvisioningJournal(state)
        rpc = ProvisioningRpcClient(url, password)
        try:
            await rpc.connect()
            result = await MemberProvisioner(
                rpc, journal, args.concurrency, args.attempts
            ).run(
                verified_members(snapshot, args.max_age_hours),
                args.output,
                args.batch_size,
            )
            result["total"] = total
            result["ready"] = result["ready"] and result["exported"] == total
            print(json.dumps(result))
            return 0 if result["ready"] else 1
        finally:
            await rpc.close()
            journal.close()


def main():
    """dry-run 不连接 NATS、不建号、不创建日志或输出；错误只显示安全代号。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--max-age-hours", type=int, default=24)
    args = parser.parse_args()
    if (
        not 1 <= args.batch_size <= 100
        or not 1 <= args.concurrency <= 32
        or not 1 <= args.attempts <= 5
        or not 1 <= args.max_age_hours <= 8760
    ):
        parser.error("INVALID_BOUNDS")
    try:
        if not args.execute:
            total = sum(1 for _ in verified_members(args.input, args.max_age_hours))
            print(json.dumps({"dry_run": True, "validated": total, "rpc_calls": 0}))
            return 0
        return asyncio.run(execute(args))
    except (ProvisioningRpcError, ValueError, OSError, sqlite3.Error) as exc:
        code = (
            exc.code
            if isinstance(exc, ProvisioningRpcError)
            else str(exc)
            if isinstance(exc, ValueError)
            else "LOCAL_STATE_UNAVAILABLE"
        )
        print(json.dumps({"ready": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
