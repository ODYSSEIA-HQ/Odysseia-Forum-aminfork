"""旧标签迁成协议 tag（协议 7.3）。规则见 src/site_v1/legacy_tag_migration.py，对照表 scripts/legacy_tag_map.yaml。

用法：
  DATABASE_URL=postgresql+asyncpg://... uv run python scripts/migrate_legacy_tags.py --dry-run --json /tmp/tag-dry.json
  DATABASE_URL=postgresql+asyncpg://... uv run python scripts/migrate_legacy_tags.py --json /tmp/tag-run.json

- --dry-run：在一个事务里全部算完再回滚（库里什么都不留，包括迁移记录），报告照样输出；
- 正式跑：结果和迁移记录（site_tag_migration_record 表）一起提交；可以重跑（幂等，只补缺的）；
- --map：换对照表；--json：完整报告（每个旧标签、作品、屏蔽偏好的明细）写到文件；
- 终端打印统计。需要先 alembic upgrade head（迁移记录表）和 seed 过官方 tag 目录。
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


async def main() -> int:
    parser = argparse.ArgumentParser(description="旧标签迁成协议 tag（协议 7.3）")
    parser.add_argument("--dry-run", action="store_true", help="算完回滚，不改库")
    parser.add_argument("--map", default=None, help="对照表 YAML（默认 scripts/legacy_tag_map.yaml）")
    parser.add_argument("--json", default=None, help="完整报告写到这个文件")
    parser.add_argument("--max-tags", type=int, default=None, help="每个作品最多几个 tag（默认取对照表和 SITE_MAX_TAGS 较小的）")
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("需要显式设置 DATABASE_URL")

    from shared.database import AsyncSessionFactory
    from site_v1.config import get_config
    from site_v1.legacy_tag_migration import DEFAULT_MAP, Mapping, migrate, write_json

    mapping = Mapping.load(args.map or DEFAULT_MAP)
    report = await migrate(
        AsyncSessionFactory,
        mapping=mapping,
        dry_run=args.dry_run,
        max_tags=args.max_tags or get_config().max_tags,
    )
    if args.json:
        write_json(report, args.json)
    print(
        json.dumps(
            {"runId": report.run_id, "dryRun": report.dry_run, "stats": report.as_json()["stats"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
