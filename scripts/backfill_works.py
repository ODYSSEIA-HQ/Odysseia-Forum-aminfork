"""显式执行作品小批回填，不连接未配置的生产数据库。"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shared.database import AsyncSessionFactory
from work.work_backfill_service import WorkBackfillService


async def main():
    """重复执行直到审计清零，崩溃后从未映射记录继续。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-batches", type=int, default=100)
    args = parser.parse_args()
    if (
        not os.environ.get("DATABASE_URL")
        or not 1 <= args.batch_size <= 10000
        or args.max_batches < 1
    ):
        parser.error("需要显式DATABASE_URL、合法batch-size和max-batches")
    changed = 0
    for table in ("thread", *WorkBackfillService.ASSOCIATIONS):
        for _ in range(args.max_batches):
            async with AsyncSessionFactory() as session:
                service = WorkBackfillService(session)
                count = (
                    await service.threads(args.batch_size)
                    if table == "thread"
                    else await service.association(table, args.batch_size)
                )
                await session.commit()
            changed += count
            if count == 0:
                break
    async with AsyncSessionFactory() as session:
        audit = await WorkBackfillService(session).audit()
    print(
        json.dumps(
            {"changed": changed, "audit": audit, "ready": not any(audit.values())}
        )
    )
    return int(any(audit.values()))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
