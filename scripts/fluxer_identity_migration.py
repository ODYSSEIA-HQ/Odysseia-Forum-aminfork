"""显式执行的在线准备工具，导入、回填和审计可独立重启。"""

import argparse
import json
import os
import sys
from pathlib import Path
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from identity.migration_service import MigrationService
from identity.reference_manifest import REFERENCES, AUTHOR_LISTS


def main():
    """默认仅审计；不会自动创建 Fluxer 用户或执行 Alembic。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["import", "backfill", "audit"])
    parser.add_argument("--file", type=Path)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 10000 or args.max_batches < 1:
        parser.error("批大小应为 1..10000，批次数应为正数")
    engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    service = MigrationService(engine)
    try:
        if args.action == "import":
            if args.file is None:
                parser.error("import 需要 Fluxer 权威 JSONL --file")
            batch = []
            count = 0
            with args.file.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        batch.append(json.loads(line))
                    if len(batch) == args.batch_size:
                        count += service.import_batch(batch)
                        batch.clear()
                if batch:
                    count += service.import_batch(batch)
            print(json.dumps({"imported": count}))
        if args.action == "backfill":
            changed = 0
            for _ in range(args.max_batches):
                count = service.backfill_vote_keys(args.batch_size)
                changed += count
                if not count:
                    break
            for table, columns in REFERENCES.items():
                for column in columns:
                    for _ in range(args.max_batches):
                        count = service.backfill_batch(table, column, args.batch_size)
                        changed += count
                        if not count:
                            break
            for table, columns in AUTHOR_LISTS.items():
                for column in columns:
                    for _ in range(args.max_batches):
                        count = service.backfill_lists(table, column, args.batch_size)
                        changed += count
                        if not count:
                            break
            print(json.dumps({"changed": changed}))
        result = service.audit()
        print(json.dumps({"audit": result, "ready": not any(result.values())}))
        return 1 if any(result.values()) else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
