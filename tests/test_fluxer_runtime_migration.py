"""隔离 PostgreSQL 上验证准备到运行态的实际 DDL。"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from identity.migration_service import MigrationService
from test_fluxer_identity_migration import migration_engine as migration_engine, members


def runtime_migration(engine):
    """以实际 Alembic 事务执行含并发索引的功能迁移。"""
    path = Path(__file__).parents[1] / "alembic/versions/fluxer_identity_runtime.py"
    spec = importlib.util.spec_from_file_location("runtime_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"transaction_per_migration": True}
        )
        module.op = Operations(context)
        with context.begin_transaction(_per_migration=True):
            module.upgrade()


def test_runtime_accepts_native_votes_and_enforces_canonical_uniqueness(
    migration_engine,  # noqa: F811
):
    """新身份无 Discord ID 可写入，复合旧键不会阻断原生投票。"""
    service = MigrationService(migration_engine)
    service.import_batch(members())
    with migration_engine.begin() as conn:
        conn.execute(text("INSERT INTO tag_vote(binding_id,user_id) VALUES(1,100)"))
    assert not any(service.audit().values())
    runtime_migration(migration_engine)
    with migration_engine.begin() as conn:
        conn.execute(
            text('INSERT INTO "user"(id,username) VALUES(:id,:name)'),
            {"id": "300", "name": "native"},
        )
        conn.execute(
            text(
                "INSERT INTO tag_vote(binding_id,user_id,fluxer_user_id) VALUES(1,NULL,'300')"
            )
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM tag_vote WHERE user_id IS NULL")
            ).scalar_one()
            == 1
        )
    with pytest.raises(Exception):
        with migration_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tag_vote(binding_id,user_id,fluxer_user_id) VALUES(1,NULL,'300')"
                )
            )


def test_runtime_blocks_cutover_if_legacy_mapping_missing(migration_engine):  # noqa: F811
    """旧系统仍产生缺失映射时，功能迁移必须退出。"""
    with migration_engine.begin() as conn:
        conn.execute(text("INSERT INTO thread(author_id) VALUES(999)"))
    with pytest.raises(RuntimeError, match="未完成的身份映射"):
        runtime_migration(migration_engine)
